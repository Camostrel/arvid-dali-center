// Мобильная карта ПУСКОНАЛАДКИ DALI-устройств (интеграция arvid_dali_center).
//   Цель: быстро найти устройство среди многих и сразу назвать.
//   - выбор контроллера (серийник, автосохранение выбора в localStorage)
//   - три вкладки: Лампы / Датчики / Кнопки (один класс за раз — меньше на экране)
//   - фильтр «только неназванные» (по флагу named из devices)
//   - исходный DALI-адрес виден всегда (ch/addr)
//   - лампа: быстрый тумблер вкл/выкл (light.toggle, НЕ identify) — найти глазами
//   - датчик/кнопка: последнее состояние (из hass.states)
//   - нейминг тремя полями (этаж/линия/номер) → тело; префикс по типу:
//       лампа l_, панель kp_ (формирует фронт); датчик — тело, бэкенд добавит ms_/il_
//       и переименует пару движение+люкс (централизованный rename).
//   - mobile-first: без горизонтального скролла, крупные тач-таргеты (≥44px).
//   - вкладка «Карта» (v0.5, экран групп — v0.6) — ПЕРЕЕЗД объекта: карта «DALI-адрес → проектное имя» из
//       /config/arvid_namemap/*.csv, сшитая со сканом. Видно, что сопоставилось, чего нет на
//       шине и чего нет в карте; имя можно поправить руками, строки — отметить точечно.
//       ⚠ Имена применяются СУЩЕСТВУЮЩИМ rename по одному устройству (тот же путь, что при
//       ручном переименовании), область — отдельным вызовом set_area. Ничего из работающего
//       не переписано. Карты нет на боксе → вкладки нет вовсе.
//       Пространство в строке — тоже поле: предзаполнено из карты, правится руками.
//       Внутри вкладки два экрана: «Устройства» (имена+области) и «Группы» (ярлык
//       общим группам помещений, по умолчанию `ba_area_light`; зонным не ставим).
// Бэкенд НЕ дублируем: используем те же WS arvid_dali_center/* и сервисы HA.

const VERSION = '0.14';
const LS_KEY = 'arvid-dali-commissioning';

// классы вкладок и префикс имени по типу устройства
const TABS = [
  { key: 'lamp', title: 'Лампы', match: (t) => t.startsWith('01') },
  { key: 'sensor', title: 'Датчики', match: (t) => t === '0201' || t === '0202' },
  { key: 'button', title: 'Кнопки', match: (t) => t.startsWith('03') },
];

// Общие группы помещений: их DALI-имя = slug из `general_light_entity` проекта
// (`512_koridor_obshchii`), поэтому опознаём по хвосту `obshchii`. Зонным группам ярлык не нужен.
const GENERAL_GROUP_RE = /obshchii/i;
const DEFAULT_GROUP_LABEL = 'ba_area_light';
// Прежний дефолт (до 2026-08-12). Он МОГ осесть в localStorage браузера, и тогда новый
// дефолт не применился бы — поле молча подставляло бы старое значение. Поэтому ровно
// это значение считаем «не выбором человека» и заменяем на актуальное.
const LEGACY_GROUP_LABEL = 'ba_room_light';

// фильтры вкладки «Карта»: что показывать в таблице сопоставления
const MAP_FILTERS = [
  // «к работе» считает БЭКЕНД (namemap.needs_work): ренейм нужен ИЛИ область расходится с
  // картой. Держать это правило в двух местах нельзя — разъедется (v0.7).
  { key: 'work', title: 'К работе', match: (r) => !!r.needs_work },
  { key: 'verify', title: 'Проверить', match: (r) => r.verify || r.danger },
  { key: 'danger', title: 'Опасные', match: (r) => !!r.danger },
  { key: 'problem', title: 'Проблемы', match: (r) => r.status === 'not_on_bus' || r.status === 'not_in_map' },
  { key: 'all', title: 'Все', match: () => true },
];

class ArvidDaliCommissioning extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: 'open' });
    this._hass = null;
    this._rendered = false;
    this._syncPending = false;
    // восстановить прошлый выбор (контроллер/вкладка/фильтр)
    let saved = {};
    try { saved = JSON.parse(localStorage.getItem(LS_KEY) || '{}'); } catch (e) { /* ignore */ }
    this._s = {
      gateways: [], gw: saved.gw || '', tab: saved.tab || 'lamp',
      onlyUnnamed: !!saved.onlyUnnamed, devices: [], loading: false, rename: null,
      // вкладка «Карта» (переезд объекта)
      mapFiles: [], mapFile: saved.mapFile || '', mapTable: [], mapSummary: null,
      mapProblems: [], mapFilter: saved.mapFilter || 'work',
      mapSel: {},              // ключ строки → отмечена ли (человек может снять точечно)
      mapEdit: {},             // ключ строки → правка имени руками (перебивает карту)
      mapSpaceEdit: {},        // ключ строки → правка пространства руками
      mapArea: saved.mapArea !== false,   // ставить ли область (по area_id) вместе с именем
      mapBusy: '',             // текст прогресса, пока идёт применение
      mapView: 'dev',          // что показываем: устройства ('dev') или группы ('grp')
      groups: [], grpSel: {},
      grpLabel: (saved.grpLabel && saved.grpLabel !== LEGACY_GROUP_LABEL)
        ? saved.grpLabel : DEFAULT_GROUP_LABEL,
      // экран «План» — ЗАПУСК АВТОПУСКОНАЛАДКИ. Логику НЕ дублируем и в ядро не тащим
      // (решение пользователя 2026-08-11): карточка лишь дёргает shell_command, который
      // запускает сгенерированный apply_*.py. Токен живёт в этом shell_command, не у нас.
      planScript: saved.planScript || '', planPhase: saved.planPhase || '',
      planOut: '', planBusy: '', planCode: null, planLog: '', planRunning: false,
      planProg: null,   // {done,total,ok,warn,bad,skip,soft} — разбор журнала
    };
  }

  setConfig(config) { this._config = config || {}; }
  getCardSize() { return 12; }

  set hass(hass) {
    const first = !this._hass;
    this._hass = hass;
    if (first) this._init();
    else if (this._rendered && !this._s.rename) this._scheduleSync();
  }

  connectedCallback() { if (this._hass && !this._rendered) this._init(); }
  disconnectedCallback() {
    // F10 (v1.2.20): сбросить и флаг — иначе колбэк RAF (сбрасывал _syncPending) не выполнится,
    // и после повторного добавления карты в DOM _scheduleSync навсегда выходит на первой строке.
    if (this._syncRaf) { cancelAnimationFrame(this._syncRaf); this._syncRaf = null; }
    this._syncPending = false;
    clearTimeout(this._tt);
    clearTimeout(this._logTimer);      // автообновление журнала не должно жить без карточки
  }

  // ── утилиты ───────────────────────────────────────────────────────────────
  _ws(msg) { return this._hass.connection.sendMessagePromise(msg); }
  _st(ent) { return ent && this._hass && this._hass.states ? this._hass.states[ent] : null; }
  _esc(s) { return String(s == null ? '' : s).replace(/[&<>]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;' }[c])); }
  _save() {
    try {
      localStorage.setItem(LS_KEY, JSON.stringify({
        gw: this._s.gw, tab: this._s.tab, onlyUnnamed: this._s.onlyUnnamed,
        mapFile: this._s.mapFile, mapFilter: this._s.mapFilter, mapArea: this._s.mapArea,
        grpLabel: this._s.grpLabel, planScript: this._s.planScript,
        planPhase: this._s.planPhase }));
    } catch (e) { /* ignore */ }
  }

  async _init() {
    this._renderShell();
    await this._loadGateways();
    await this._loadMapFiles();
    if (this._s.gw) await this._loadDevices();
    else this._render();
  }

  // ── вкладка «Карта» ─────────────────────────────────────────────────────────
  async _loadMapFiles() {
    // Карт нет → вкладки не будет. На обычных объектах карточка выглядит как раньше.
    try {
      const r = await this._ws({ type: 'arvid_dali_center/namemap_files' });
      this._s.mapFiles = r.files || [];
      if (!this._s.mapFiles.some((f) => f.name === this._s.mapFile)) {
        this._s.mapFile = (this._s.mapFiles[0] || {}).name || '';
      }
      if (!this._s.mapFiles.length && this._s.tab === 'map') this._s.tab = 'lamp';
    } catch (e) { this._s.mapFiles = []; }
  }

  _mapKey(r) { return `${r.devType}:${r.address}`; }

  async _loadMapTable() {
    const gw = this._s.gw, file = this._s.mapFile;
    if (!gw || !file) { this._s.mapTable = []; this._s.mapSummary = null; this._render(); return; }
    this._s.loading = true; this._render();
    try {
      const r = await this._ws({ type: 'arvid_dali_center/namemap_table', gw_sn: gw, file });
      if (this._s.gw !== gw || this._s.mapFile !== file) return;   // ответ устарел
      this._s.mapTable = r.table || [];
      this._s.mapSummary = r.summary || null;
      this._s.mapProblems = r.problems || [];
      // по умолчанию отмечено то, что реально требует работы; остальное человек включит сам
      this._s.mapSel = {};
      this._s.mapEdit = {};
      this._s.mapSpaceEdit = {};
      for (const row of this._s.mapTable) {
        // «опасные» (отвалившиеся / проблема с адресом) НЕ отмечаем: их адрес мог уехать,
        // и применять имя вслепую нельзя — человек включает такие строки сам
        if (row.needs_work) this._s.mapSel[this._mapKey(row)] = true;
      }
      if (this._s.mapProblems.length) {
        this._toast(`В карте ${this._s.mapProblems.length} замечани${this._s.mapProblems.length === 1 ? 'е' : 'й'}`, true);
      }
    } catch (e) { this._toast('Карта: ' + e.message, true); }
    this._s.loading = false; this._render();
  }

  async _loadGroups() {
    const gw = this._s.gw;
    if (!gw) { this._s.groups = []; this._render(); return; }
    this._s.loading = true; this._render();
    try {
      const r = await this._ws({ type: 'arvid_dali_center/groups', gw_sn: gw });
      if (this._s.gw !== gw) return;                       // ответ устарел
      // КРОСС-ГРУППЫ тоже сюда (v0.10): ярлык нужен ОБЩИМ группам помещений, а на объекте
      // они сплошь сквозные (лестницы) — без этого 21 из 29 общих групп Воронежа осталась бы
      // без `ba_area_light`. Список кросс-групп общий (не по шлюзу): у группы нет «владельца»,
      // её копии лежат на каждом участнике.
      let xg = [];
      try {
        const rx = await this._ws({ type: 'arvid_dali_center/cross_groups' });
        xg = (rx.groups || []).map((g) => ({ ...g, cross: true }));
      } catch (e) { /* нет кросс-групп — не беда */ }
      this._s.groups = (r.groups || []).concat(xg);
      // по умолчанию отмечены ТОЛЬКО общие группы помещений — зонным ярлык не нужен
      this._s.grpSel = {};
      for (const g of this._s.groups) {
        if (GENERAL_GROUP_RE.test(g.name || '')) this._s.grpSel[this._grpKey(g)] = true;
      }
    } catch (e) { this._toast('Группы: ' + e.message, true); }
    this._s.loading = false; this._render();
  }

  // ⚠ ключ строки — с uid для кросс-групп: их номер МОЖЕТ совпасть с номером обычной
  // группы другого шлюза, и общий ключ `channel:groupId` склеил бы две разные строки.
  _grpKey(g) { return g.cross ? `x:${g.uid}` : `${g.channel}:${g.groupId}`; }

  async _applyLabels() {
    const label = (this._s.grpLabel || '').trim();
    const rows = this._s.groups.filter((g) => this._s.grpSel[this._grpKey(g)]);
    if (!rows.length) { this._toast('Не выбрано ни одной группы', true); return; }
    let ok = 0; const errs = [];
    for (let i = 0; i < rows.length; i++) {
      const g = rows[i];
      this._s.mapBusy = `Ярлык ${i + 1}/${rows.length}: ${g.name}`; this._render();
      try {
        const res = await this._ws(g.cross
          ? { type: 'arvid_dali_center/set_group_labels', uid: g.uid,
              labels: label ? [label] : [] }
          : { type: 'arvid_dali_center/set_group_labels', gw_sn: this._s.gw,
              channel: g.channel, groupId: g.groupId, labels: label ? [label] : [] });
        if (res && res.ok === false) errs.push(`${g.name}: ${res.error}`);
        else ok++;
      } catch (e) { errs.push(`${g.name}: ${e.message}`); }
    }
    this._s.mapBusy = ''; this._render();
    this._toast(errs.length ? `Ярлык проставлен ${ok}/${rows.length}, отказов ${errs.length}`
      : `Ярлык проставлен: ${ok}`, errs.length > 0);
    if (errs.length) console.warn('[namemap] ярлыки, отказы:', errs);
  }

  _mapList() {
    const f = MAP_FILTERS.find((x) => x.key === this._s.mapFilter) || MAP_FILTERS[0];
    return this._s.mapTable.filter(f.match);
  }

  async _applyMap() {
    // Применяем ПО ОДНОМУ существующим rename: тот же путь, что при ручном переименовании,
    // включая отказ по дублю имени. Прогресс виден по строкам — на тысяче устройств это
    // важнее скорости: любой отказ понятен сразу, с именем конкретной лампы.
    const rows = this._mapList().filter((r) => this._s.mapSel[this._mapKey(r)] && !r.skip
      && r.status === 'matched');
    if (!rows.length) { this._toast('Нечего применять', true); return; }
    let okName = 0, okArea = 0, sameName = 0; const errs = [];
    for (let i = 0; i < rows.length; i++) {
      const r = rows[i];
      const name = (this._s.mapEdit[this._mapKey(r)] ?? r.target_name).trim();
      this._s.mapBusy = `Применяю ${i + 1}/${rows.length}: ${name}`; this._render();
      if (!name) { errs.push(`addr${r.address}: пустое имя`); continue; }
      // ИМЯ УЖЕ ВЕРНОЕ — rename не зовём и строку НЕ бросаем (v0.7). Раньше здесь был выход по
      // отказу, и повторный проход по уже названному объекту не мог доставить область: имя
      // «занято» тем же устройством → continue → set_area не выполнялся (офис 2026-08-11).
      const edited = name !== (r.current_name || '').trim();
      if (r.same && !edited) {
        sameName++;
      } else {
        try {
          const res = await this._ws({
            type: 'arvid_dali_center/rename', gw_sn: this._s.gw,
            devType: r.devType, channel: r.channel, address: r.address,
            devSn: r.devSn || '', name,
          });
          if (res && res.ok === false) {
            errs.push(`${name}: ` + (res.error === 'duplicate' ? 'имя занято ' + (res.conflict || '') : 'отклонено'));
          } else {
            okName++;
          }
        } catch (e) { errs.push(`${name}: ${e.message}`); }
      }
      // область — ОТДЕЛЬНЫМ вызовом: её отказ не должен отменять уже поставленное имя
      // область адресуем по area_id (англ. room_slug): области заводятся до нас, сверять
      // русские имена хрупко. Нет такой области в HA — отказ, молча не создаём.
      const areaId = (this._s.mapSpaceEdit[this._mapKey(r)] ?? r.area_id).trim();
      if (this._s.mapArea && areaId) {
        try {
          const a = await this._ws({
            type: 'arvid_dali_center/set_area', gw_sn: this._s.gw,
            devType: r.devType, channel: r.channel, address: r.address,
            devSn: r.devSn || '', area_id: areaId,
          });
          if (a && a.ok === false) errs.push(`${name}: область «${areaId}» — ${a.error}`);
          else okArea++;
        } catch (e) { errs.push(`${name}: область — ${e.message}`); }
      }
    }
    this._s.mapBusy = ''; this._render();
    await this._loadDevices();
    await this._loadMapTable();
    // СЧЁТЧИКИ РАЗДЕЛЬНЫЕ (v0.7): раньше имя и область падали в один «отказано», и отказ
    // области выглядел как «ничего не применилось» — при том что имена легли (офис 2026-08-11).
    const parts = [`имена ${okName}`];
    if (sameName) parts.push(`уже верных ${sameName}`);
    if (this._s.mapArea) parts.push(`области ${okArea}`);
    if (errs.length) parts.push(`ошибок ${errs.length}`);
    this._toast(parts.join(', '), errs.length > 0);
    if (errs.length) console.warn('[namemap] отказы:', errs);   // подробности — в консоль, не в тост
  }

  async _loadGateways() {
    try {
      const r = await this._ws({ type: 'arvid_dali_center/gateways' });
      this._s.gateways = r.gateways || [];
      // выбранного нет в списке — взять первый
      if (!this._s.gateways.some((g) => g.gwSn === this._s.gw)) {
        this._s.gw = (this._s.gateways[0] || {}).gwSn || '';
      }
    } catch (e) { this._toast('Шлюзы: ' + e.message, true); }
  }

  async _loadDevices() {
    const gw = this._s.gw;
    if (!gw) { this._s.devices = []; this._render(); return; }
    this._s.loading = true; this._render();
    try {
      const r = await this._ws({ type: 'arvid_dali_center/devices', gw_sn: gw });
      if (this._s.gw !== gw) return;   // успели переключить шлюз — ответ устарел
      this._s.devices = r.devices || [];
    } catch (e) { this._toast('Устройства: ' + e.message, true); }
    this._s.loading = false; this._render();
  }

  // ── состояние/доступность устройства ────────────────────────────────────────
  _devOnline(d) {
    return !d.zombie && (String(d.status) === 'online' || d.status === 1 || d.status === true);
  }

  _liveText(d) {
    const t = String(d.devType), E = d.entities || {};
    if (!this._devOnline(d)) return 'не на связи';
    if (t === '0201') { const st = this._st(E.motion); return st ? 'движение: ' + st.state : '—'; }
    if (t === '0202') { const st = this._st(E.lux); return st ? 'освещённость: ' + st.state + ' lx' : '—'; }
    if (t.startsWith('03')) {
      const st = this._st(E.event);
      if (!st) return '—';
      const et = st.attributes && st.attributes.event_type;
      return et ? 'последнее: ' + et + (st.attributes.key_no != null ? ' #' + st.attributes.key_no : '') : 'нажатий не было';
    }
    return '';
  }

  _lampOn(d) {
    const st = this._st((d.entities || {}).light);
    return !!(st && st.state === 'on');
  }

  // ── действия ────────────────────────────────────────────────────────────────
  _toggleLamp(d) {
    const ent = (d.entities || {}).light;
    if (!ent) { this._toast('Нет сущности лампы', true); return; }
    this._hass.callService('light', 'toggle', { entity_id: ent });   // вкл/выкл, не identify
  }

  _prefixFor(devType) {
    const t = String(devType);
    if (t.startsWith('01')) return 'l';
    if (t.startsWith('03')) return 'kp';
    return null;   // датчики (0201/0202) — префикс ставит бэкенд (ms_/il_ + пара)
  }

  async _saveName() {
    const r = this._s.rename;
    if (!r) return;
    const sr = this.shadowRoot;
    const v = (id) => (sr.getElementById(id) || {}).value || '';
    // тело имени из трёх полей: этаж_линия_номер (пустые — опускаем)
    const body = [v('nmFloor'), v('nmLine'), v('nmNum')].map((x) => x.trim()).filter(Boolean).join('_');
    if (!body) { this._toast('Введите хотя бы одно поле', true); return; }
    const pfx = this._prefixFor(r.dev.devType);
    const name = pfx ? `${pfx}_${body}` : body;   // лампа/панель — фронт; датчик — тело
    // Имена уникальны: быстрый локальный отказ (по текущему шлюзу) до отправки.
    // Глобальный/межшлюзовой гейт — на бэкенде (ответ error:duplicate ниже).
    if (this._isDupName(name, r.dev)) {
      this._toast('Имя уже используется: ' + name, true); return;
    }
    try {
      // Лампу включали, чтобы найти её глазами → после сохранения имени гасим.
      // Шлём ДО ренейма (entity_id ещё старый, без гонки со сменой id); только лампы 01xx.
      if (pfx === 'l') {
        const lent = (r.dev.entities || {}).light;
        if (lent) this._hass.callService('light', 'turn_off', { entity_id: lent });
      }
      const res = await this._ws({
        type: 'arvid_dali_center/rename', gw_sn: this._s.gw,
        devType: r.dev.devType, channel: r.dev.channel, address: r.dev.address,
        devSn: r.dev.devSn || '', name,
      });
      if (res && res.ok === false) {   // бэкенд отклонил (дубль имени) — лист оставляем открытым
        this._toast(res.error === 'duplicate'
          ? 'Имя занято: ' + (res.conflict || name) : 'Не переименовано', true);
        return;
      }
      this._s.rename = null;
      await this._loadDevices();   // перечитать имена/флаги (датчик переименует пару)
      this._toast('Названо: ' + name);
    } catch (e) { this._toast('Ошибка: ' + e.message, true); }
  }

  // Дубль имени среди устройств текущего шлюза (без учёта самого переименуемого —
  // по channel/address; пара датчика 0201/0202 делит имя и адрес → не ложно-срабатывает).
  _isDupName(name, self) {
    const nm = name.trim().toLowerCase();
    return (this._s.devices || []).some((d) =>
      d.name && d.name.trim().toLowerCase() === nm
      && !(d.channel === self.channel && d.address === self.address));
  }

  _toast(msg, err) {
    const t = this.shadowRoot.getElementById('toast');
    if (!t) return;
    t.textContent = msg; t.className = 'toast show' + (err ? ' err' : '');
    clearTimeout(this._tt);
    this._tt = setTimeout(() => { t.className = 'toast'; }, 3000);
  }

  // ── рендер ───────────────────────────────────────────────────────────────────
  _renderShell() {
    this.shadowRoot.innerHTML = `<style>${STYLE}</style>
      <div class="root"><div class="card" id="card"></div><div id="toast" class="toast"></div></div>`;
    this.shadowRoot.getElementById('card').addEventListener('click', (e) => this._onClick(e));
    this.shadowRoot.getElementById('card').addEventListener('change', (e) => this._onChange(e));
    this.shadowRoot.getElementById('card').addEventListener('keydown', (e) => this._onKey(e));
    this._rendered = true;
  }

  _list() {
    const tab = TABS.find((x) => x.key === this._s.tab) || TABS[0];
    return this._s.devices
      .map((d, i) => ({ d, i }))
      .filter((x) => tab.match(String(x.d.devType)))
      .filter((x) => !this._s.onlyUnnamed || !x.d.named)
      .sort((a, b) => (a.d.named ? 1 : 0) - (b.d.named ? 1 : 0)   // неназванные — вверх
        || (a.d.address || 0) - (b.d.address || 0));
  }

  _render() {
    const card = this.shadowRoot && this.shadowRoot.getElementById('card');
    if (!card) return;
    const s = this._s;
    // Строка контроллера: ИМЯ (если задано) + хвост серийника + число устройств (v0.14).
    // Раньше показывался только серийник — а на объекте человек ориентируется по НОМЕРУ ЛИНИИ,
    // который живёт в имени шлюза. Хвост серийника оставляем всегда: имена могут быть
    // заводскими и одинаковыми у всех 27 контроллеров, тогда без него список неразличим.
    const gwOpts = s.gateways.map((g) => {
      const named = g.name && g.name !== g.gwSn;
      const nm = named ? `${this._esc(g.name)} · ${this._esc(String(g.gwSn).slice(-5))}`
        : this._esc(g.gwSn);
      const cnt = g.devices != null ? ` · ${g.devices} уст.` : '';
      return `<option value="${this._esc(g.gwSn)}"${g.gwSn === s.gw ? ' selected' : ''}>${nm}${cnt}${g.connected ? '' : ' (offline)'}</option>`;
    }).join('') || '<option value="">шлюзы не найдены</option>';
    // вкладка «Карта» появляется, только если карта лежит на боксе
    const allTabs = s.mapFiles.length ? TABS.concat([{ key: 'map', title: 'Карта' }]) : TABS;
    const tabs = allTabs.map((t) =>
      `<button class="tab${t.key === s.tab ? ' on' : ''}" data-act="tab" data-tab="${t.key}">${t.title}</button>`).join('');
    const isMap = s.tab === 'map';
    const isGrp = isMap && s.mapView === 'grp';
    const isPlan = isMap && s.mapView === 'plan';
    const rows = isPlan ? this._planScreen()
      : s.loading ? `<div class="muted pad">Загрузка…</div>`
      : isGrp
        ? (s.groups.map((g) => this._grpRow(g)).join('') || `<div class="muted pad">Групп нет.</div>`)
        : isMap
          ? (this._mapList().map((r) => this._mapRow(r)).join('') || `<div class="muted pad">Пусто.</div>`)
          : (this._list().map(({ d, i }) => this._row(d, i)).join('') || `<div class="muted pad">Пусто.</div>`);
    card.innerHTML = `
      <header class="hd">
        <div class="hd-title"><b>Пусконаладка DALI</b><span>v${VERSION}</span></div>
      </header>
      <label class="fld"><span>Контроллер</span>
        <select id="gwSel" data-act="gw">${gwOpts}</select></label>
      <div class="tabs">${tabs}</div>
      ${isMap ? this._mapHead2() : `<label class="chk"><input type="checkbox" id="onlyUnnamed" data-act="filter"${s.onlyUnnamed ? ' checked' : ''}> только неназванные</label>`}
      <div class="list">${rows}</div>
      ${isMap && !isPlan ? (isGrp ? this._grpFoot() : this._mapFoot()) : ''}
      ${this._renameSheet()}`;
    if (!s.loading && !isMap) this._syncStates();
  }

  // ── экран «План»: запуск автопусконаладки (группы/области) ──────────────────
  // ⚠ Исполнителя в ядро НЕ тащим (решение пользователя 2026-08-11): пусконаладку делает
  // сгенерированный `apply_<объект>.py` из /config/tools, а карточка лишь запускает его через
  // штатный `shell_command` HA. Токен долгого действия прописан в самом shell_command —
  // карточке он не нужен и через неё не ходит.
  //
  // Настройка (⚠ shell_command выполняется БЕЗ ШЕЛЛА: «HA_TOKEN=… python3 …» не работает,
  // HA примет токен за имя программы). Токен — в файле, команда без него:
  //   configuration.yaml: shell_command: {arvid_import_apply: "python3 /config/tools/{{ script }} {{ args }}"}
  //   /config/tools/arvid_apply.conf: строка «ha_token: eyJ…» (файл ВИДИМЫЙ — его правит человек)
  //
  // Имя сервиса можно переопределить в конфиге карточки: apply_service: shell_command.<имя>.
  _planScreen() {
    const s = this._s;
    const svc = (this._config && this._config.apply_service) || 'shell_command.arvid_import_apply';
    const phases = [['', 'все фазы'], ['groups', 'только группы'], ['areas', 'только области']];
    const opts = phases.map(([v, t]) =>
      `<option value="${v}"${v === s.planPhase ? ' selected' : ''}>${t}</option>`).join('');
    const out = s.planOut
      ? `<pre class="plan-out${s.planCode ? ' bad' : ''}">${this._esc(s.planOut)}</pre>` : '';
    return `
      <div class="pad">
        <label class="fld"><span>Скрипт в /config/tools</span>
          <input type="text" id="planScript" data-act="planScript"
                 placeholder="apply_office_test.py" value="${this._esc(s.planScript)}"></label>
        <label class="fld"><span>Фаза</span>
          <select data-act="planPhase">${opts}</select></label>
        <div class="muted sm">сервис: ${this._esc(svc)}</div>
        <div class="plan-btns">
          <button class="btn ghost" data-act="planDry"${s.planBusy ? ' disabled' : ''}>Проверить (dry-run)</button>
          <button class="btn" data-act="planApply"${s.planBusy ? ' disabled' : ''}>Применить</button>
          <button class="btn ghost" data-act="planLog">Журнал</button>
          ${s.planRunning ? '<button class="btn stop" data-act="planStop">Остановить</button>' : ''}
        </div>
        ${s.planBusy ? `<div class="busy">${this._esc(s.planBusy)}</div>` : ''}
        <div class="muted sm">Прогон идёт в ФОНЕ: 216 групп — это 6–8 минут, а HA обрывает
          команду через 60 с. Кнопка возвращает управление сразу, ход работы смотрите в журнале.</div>
        ${this._planProgressHtml()}
        ${s.planLog ? `<pre class="plan-out">${this._esc(s.planLog)}</pre>` : ''}
        ${out}
      </div>`;
  }

  // ЖУРНАЛ фонового прогона. Пока процесс жив — переспрашиваем раз в 2 с: человек должен
  // видеть, на какой группе идём и что подтвердилось, иначе фоновый запуск превращается в
  // «нажал и гадай».
  async _loadPlanLog(auto) {
    const script = (this._s.planScript || '').trim();
    if (!script) { this._toast('Укажите имя скрипта', true); return; }
    try {
      // lines: 1000 — прогон объекта это ~300 строк; берём его ЦЕЛИКОМ, иначе счётчики
      // «подтверждено/отказано» считались бы по обрезанному хвосту и врали бы в меньшую сторону.
      const r = await this._ws({ type: 'arvid_dali_center/apply_log', script, lines: 1000 });
      this._s.planLog = r.log || '(журнал пуст — прогон ещё не запускался)';
      this._s.planRunning = !!r.running;
      this._s.planProg = this._parsePlanLog(r.log || '');
      this._render();
      clearTimeout(this._logTimer);
      if (r.running) this._logTimer = setTimeout(() => this._loadPlanLog(true), 2000);
      else if (auto) this._toast('Прогон завершён');
    } catch (e) {
      this._s.planRunning = false;
      if (!auto) this._toast('Журнал: ' + e.message, true);
    }
  }

  // Разбор журнала: сколько сделано и с каким исходом. Скрипт печатает «[12/216]» в КАЖДОЙ
  // строке результата — это и человеку понятно, и нам хватает как источника прогресса.
  // Счётчики берём по значкам, которые ставит сам скрипт: ✅ подтв. · ⚠ без сверки ·
  // ❌ не подтв. · = уже в HA. Ничего не додумываем: чего в журнале нет, того не показываем.
  _parsePlanLog(text) {
    if (!text) return null;
    const lines = text.split('\n');
    let done = 0, total = 0, ok = 0, warn = 0, bad = 0, skip = 0;
    for (const ln of lines) {
      const m = ln.match(/\[(\d+)\/(\d+)\]/);
      if (m) { done = +m[1]; total = +m[2]; }
      if (!m) continue;
      if (ln.includes('✅')) ok++;
      else if (ln.includes('⚠')) warn++;
      else if (ln.includes('❌')) bad++;
      else if (/\]\s*=/.test(ln)) skip++;
    }
    return {
      done, total, ok, warn, bad, skip,
      // маркер печатает скрипт нового поколения: без него SIGTERM убьёт процесс СРАЗУ,
      // а обрыв между delGroup и addGroup оставит группу снесённой — предупреждаем человека
      soft: text.includes('мягкая остановка поддерживается'),
      stopped: text.includes('ПРОГОН ОСТАНОВЛЕН'),
      finished: text.includes('ПРОГОН ЗАВЕРШЁН'),
    };
  }

  _planProgressHtml() {
    const s = this._s, p = s.planProg;
    if (!s.planRunning && !p) return '';
    if (!p || !p.total) {
      return s.planRunning ? '<div class="busy">Идёт прогон… журнал обновляется</div>' : '';
    }
    const pct = Math.min(100, Math.round((p.done / p.total) * 100));
    const head = s.planRunning ? `Идёт прогон: ${p.done} из ${p.total}`
      : p.stopped ? `⛔ Остановлено на ${p.done} из ${p.total}`
      : p.finished ? `Завершено: ${p.done} из ${p.total}`
      : `Последний прогон: ${p.done} из ${p.total}`;
    return `<div class="prog">
        <div class="prog-head">${this._esc(head)}</div>
        <div class="prog-bar"><i style="width:${pct}%"></i></div>
        <div class="prog-num">
          <span class="ok">✅ ${p.ok} подтв.</span>
          <span class="warn">⚠ ${p.warn} без сверки</span>
          <span class="bad">❌ ${p.bad} не подтв.</span>
          <span class="muted">= ${p.skip} уже в HA</span>
        </div>
      </div>`;
  }

  // ОСТАНОВКА прогона. Мягкая: бэкенд шлёт SIGTERM, скрипт доканчивает текущую запись и
  // выходит МЕЖДУ группами (обрыв посреди delGroup+addGroup оставил бы группу снесённой).
  async _stopPlan() {
    const script = (this._s.planScript || '').trim();
    if (!script) { this._toast('Укажите имя скрипта', true); return; }
    const p = this._s.planProg;
    const risk = (p && !p.soft)
      ? '\n\n⚠ В журнале нет отметки о мягкой остановке — этот скрипт сгенерирован старой '
        + 'версией. Процесс завершится СРАЗУ, и группа, которая пишется в этот момент, может '
        + 'остаться удалённой. Безопаснее дождаться конца фазы.'
      : '\n\nСкрипт доканчивает текущую запись и останавливается между группами.';
    if (!confirm(`Остановить прогон «${script}»?${risk}`)) return;
    try {
      const r = await this._ws({ type: 'arvid_dali_center/apply_stop', script });
      if (r && r.ok) this._toast('Остановка запрошена — ждём завершения текущей записи');
      else this._toast('Остановить не вышло: ' + ((r && r.error) || 'неизвестно'), true);
    } catch (e) {
      this._toast('Остановить: ' + e.message, true);
    }
    this._loadPlanLog(true);
  }

  async _runPlan(apply) {
    const s = this._s;
    const script = (s.planScript || '').trim();
    if (!script) { this._toast('Укажите имя скрипта', true); return; }
    if (apply && !confirm(`Применить план «${script}»?\n\nБудут созданы DALI-группы на шине `
      + `и назначены области. Действие пишет в контроллеры.`)) return;
    const svc = (this._config && this._config.apply_service) || 'shell_command.arvid_import_apply';
    const [domain, service] = svc.split('.');
    if (!domain || !service) { this._toast(`Неверный apply_service: ${svc}`, true); return; }
    const args = [apply ? '--apply' : '', s.planPhase ? `--only ${s.planPhase}` : '']
      .filter(Boolean).join(' ');
    s.planBusy = apply ? 'Применяю…' : 'Проверяю…'; s.planOut = ''; s.planCode = null;
    this._render();
    try {
      // return_response: shell_command отдаёт {stdout, stderr, returncode} (HA 2023.7+)
      const res = await this._ws({
        type: 'call_service', domain, service,
        service_data: { script, args }, return_response: true,
      });
      const r = (res && res.response) || {};
      s.planCode = r.returncode || 0;
      s.planOut = [r.stdout, r.stderr].filter(Boolean).join('\n').trim()
        || `(пусто, код ${s.planCode})`;
      // обёртка возвращает управление СРАЗУ (процесс ушёл в фон) — значит показываем не
      // «готово», а «запущено», и тут же открываем журнал с автообновлением
      this._toast(s.planCode ? `Не запустилось (код ${s.planCode})` : 'Запущено — смотрите журнал',
        !!s.planCode);
      if (!s.planCode) this._loadPlanLog(false);
    } catch (e) {
      // самая частая причина — сервиса нет: shell_command не заведён в configuration.yaml
      s.planOut = String(e.message || e);
      s.planCode = -1;
      this._toast(`Не удалось запустить ${svc}: ${e.message}`, true);
    }
    s.planBusy = ''; this._render();
  }

  // переключатель «Устройства | Группы» внутри вкладки «Карта»
  _mapHead2() {
    const v = this._s.mapView;
    const sw = `<div class="tabs">
      <button class="tab sm${v === 'dev' ? ' on' : ''}" data-act="mapView" data-v="dev">Устройства</button>
      <button class="tab sm${v === 'grp' ? ' on' : ''}" data-act="mapView" data-v="grp">Группы</button>
      <button class="tab sm${v === 'plan' ? ' on' : ''}" data-act="mapView" data-v="plan">План</button>
    </div>`;
    // У «Плана» своя шапка не нужна: селектор файла карты, фильтры строк и чекбокс области
    // относятся к сшивке имён и на экране запуска скрипта только путают (замечание с экрана
    // 2026-08-11). Показываем один переключатель видов.
    if (v === 'plan') return sw;
    return sw + (v === 'grp' ? this._grpHead() : this._mapHead());
  }

  // ── экран «Группы»: ярлык общим группам помещений ───────────────────────────
  _grpHead() {
    const s = this._s;
    const total = s.groups.length;
    const sel = s.groups.filter((g) => s.grpSel[this._grpKey(g)]).length;
    const gen = s.groups.filter((g) => GENERAL_GROUP_RE.test(g.name || '')).length;
    return `
      <div class="sum">
        <span class="chip">групп ${total}</span>
        <span class="chip ok">общих ${gen}</span>
        <span class="chip">отмечено ${sel}</span>
      </div>
      <label class="fld"><span>Ярлык (создастся, если такого нет)</span>
        <input class="rw-name" type="text" id="grpLabel" data-act="grpLabel"
               value="${this._esc(s.grpLabel)}" placeholder="без ярлыка — снять"></label>
      <div class="warnbox">Отмечены общие группы помещений (в имени <b>obshchii</b>).
        Зонным группам ярлык не ставим — отметьте вручную, если нужно.</div>`;
  }

  _grpRow(g) {
    const key = this._grpKey(g);
    const general = GENERAL_GROUP_RE.test(g.name || '');
    return `
      <div class="rw">
        <label class="rw-chk">
          <input type="checkbox" data-act="grpSel" data-key="${this._esc(key)}"${this._s.grpSel[key] ? ' checked' : ''}>
        </label>
        <div class="rw-body">
          <div class="rw-top"><b>${this._esc(g.name || ('группа ' + g.groupId))}</b>
            ${general ? '<span class="chip ok">общая</span>' : ''}
            ${g.cross ? '<span class="chip">кросс</span>' : ''}</div>
          <div class="rw-cur">канал ${g.channel} · id ${g.groupId} · ламп ${(g.members || []).length}${
            g.cross ? ` · контроллеров ${(g.participants || []).length}` : ''}</div>
        </div>
      </div>`;
  }

  _grpFoot() {
    const n = this._s.groups.filter((g) => this._s.grpSel[this._grpKey(g)]).length;
    return `<div class="foot">
      ${this._s.mapBusy ? `<div class="busy">${this._esc(this._s.mapBusy)}</div>` : ''}
      <button class="btn ghost" data-act="grpGeneral">Только общие</button>
      <button class="btn ghost" data-act="grpNone">Снять</button>
      <button class="btn" data-act="grpApply"${n && !this._s.mapBusy ? '' : ' disabled'}>Проставить (${n})</button>
    </div>`;
  }

  // ── вкладка «Карта»: шапка (файл, сводка, фильтры) ──────────────────────────
  _mapHead() {
    const s = this._s;
    const files = s.mapFiles.map((f) =>
      `<option value="${this._esc(f.name)}"${f.name === s.mapFile ? ' selected' : ''}>${this._esc(f.name)}</option>`).join('');
    const sum = s.mapSummary;
    const chips = sum ? `
      <div class="sum">
        <span class="chip ok">к работе ${sum.ready}</span>
        <span class="chip">уже названо ${sum.already}</span>
        ${sum.verify ? `<span class="chip warn">проверить ${sum.verify}</span>` : ''}
        ${sum.not_on_bus ? `<span class="chip bad">нет на шине ${sum.not_on_bus}</span>` : ''}
        ${sum.not_in_map ? `<span class="chip bad">нет в карте ${sum.not_in_map}</span>` : ''}
        ${sum.paired ? `<span class="chip">освещённость ${sum.paired}</span>` : ''}
        ${sum.danger ? `<span class="chip bad">опасные ${sum.danger}</span>` : ''}
      </div>` : '';
    const filters = MAP_FILTERS.map((f) =>
      `<button class="tab sm${f.key === s.mapFilter ? ' on' : ''}" data-act="mapFilter" data-f="${f.key}">${f.title}</button>`).join('');
    const problems = s.mapProblems.length
      ? `<div class="warnbox">В карте ${s.mapProblems.length} замечаний: ${this._esc(s.mapProblems[0])}${s.mapProblems.length > 1 ? ' …' : ''}</div>`
      : '';
    return `
      <label class="fld"><span>Карта сопоставления</span>
        <select id="mapFile" data-act="mapFile">${files}</select></label>
      ${chips}${problems}
      <div class="tabs">${filters}</div>
      <label class="chk"><input type="checkbox" id="mapArea" data-act="mapArea"${s.mapArea ? ' checked' : ''}> ставить область из карты (по area_id)</label>`;
  }

  // ── вкладка «Карта»: строка таблицы ─────────────────────────────────────────
  _mapRow(r) {
    const key = this._mapKey(r);
    const sel = !!this._s.mapSel[key];
    const name = this._s.mapEdit[key] ?? r.target_name;
    const areaId = this._s.mapSpaceEdit[key] ?? r.area_id;
    const badge = r.danger ? '<span class="chip bad">опасное</span>'
      : r.status === 'paired' ? '<span class="chip">имя придёт с движением</span>'
      : r.status === 'not_on_bus' ? '<span class="chip bad">нет на шине</span>'
      : r.status === 'not_in_map' ? '<span class="chip bad">нет в карте</span>'
      : r.same ? '<span class="chip">уже названо</span>'
      : r.verify ? '<span class="chip warn">проверить</span>' : '';
    // ОБЛАСТЬ: показываем ТЕКУЩУЮ и, если она расходится с картой, — чип «область не стоит».
    // Без этого строка с верным именем выглядела законченной, хотя область не проставлена
    // (офисный прогон 2026-08-11: имена легли, области нет, и повода вернуться не было).
    const areaWant = (r.area_id || '').trim();
    const areaCur = (r.area_current || '').trim();
    const areaBadge = (r.status === 'matched' && areaWant && areaWant !== areaCur)
      ? `<span class="chip warn">область: ${this._esc(areaCur || 'не задана')} → ${this._esc(areaWant)}</span>`
      : (areaCur ? `<span class="chip ok">область ${this._esc(r.area_current_name || areaCur)}</span>` : '');
    const warn = (r.warn || []).length ? `<div class="rw-warn">${this._esc(r.warn.join('; '))}</div>` : '';
    const note = r.note ? `<div class="rw-note">${this._esc(r.note)}</div>` : '';
    return `
      <div class="rw${r.skip ? ' off' : ''}">
        <label class="rw-chk">
          <input type="checkbox" data-act="mapSel" data-key="${this._esc(key)}"${sel ? ' checked' : ''}${r.skip ? ' disabled' : ''}>
        </label>
        <div class="rw-body">
          <div class="rw-top">
            <b>addr ${r.address}</b> <span class="muted">${this._esc(r.devType)}</span> ${badge} ${areaBadge}
          </div>
          <div class="rw-cur">сейчас: ${this._esc(r.current_name || '—')}</div>
          ${r.status === 'not_in_map' ? '' : `
          <input class="rw-name" type="text" value="${this._esc(name)}"
                 data-act="mapName" data-key="${this._esc(key)}" ${r.skip ? 'disabled' : ''}>
          <label class="rw-sub">область${r.space ? ` · ${this._esc(r.space)}` : ''}
            <input class="rw-name" type="text" value="${this._esc(areaId)}" placeholder="без области"
                   data-act="mapSpace" data-key="${this._esc(key)}" ${r.skip ? 'disabled' : ''}>
          </label>`}
          ${note}${warn}
        </div>
      </div>`;
  }

  _mapFoot() {
    const n = this._mapList().filter((r) => this._s.mapSel[this._mapKey(r)] && !r.skip).length;
    return `<div class="foot">
      ${this._s.mapBusy ? `<div class="busy">${this._esc(this._s.mapBusy)}</div>` : ''}
      <button class="btn ghost" data-act="mapAll">Отметить всё</button>
      <button class="btn ghost" data-act="mapNone">Снять</button>
      <button class="btn" data-act="mapApply"${n && !this._s.mapBusy ? '' : ' disabled'}>Применить (${n})</button>
    </div>`;
  }

  _row(d, i) {
    const t = String(d.devType);
    const online = this._devOnline(d);
    const nm = d.name || '— без имени —';
    const addr = `ch${d.channel}/${d.address}`;
    let ctl = '';
    if (t.startsWith('01')) {
      ctl = `<button class="tg" data-act="toggle" data-tg="${i}" data-idx="${i}" title="Вкл/выкл">${this._lampOn(d) ? 'ВКЛ' : 'ВЫКЛ'}</button>`;
    } else {
      ctl = `<span class="live" data-live="${i}">${this._esc(this._liveText(d))}</span>`;
    }
    return `<div class="row${d.named ? '' : ' unnamed'}${online ? '' : ' off'}">
        <div class="rinfo">
          <div class="rname">${this._esc(nm)}</div>
          <div class="rmeta"><span class="dot" data-dot="${i}"></span>${addr}${d.devSn ? ' · ' + this._esc(d.devSn) : ''}</div>
        </div>
        <div class="ract">${ctl}
          <button class="name" data-act="rename" data-idx="${i}">Назвать</button>
        </div>
      </div>`;
  }

  _renameSheet() {
    const r = this._s.rename;
    if (!r) return '';
    const t = String(r.dev.devType);
    const pfx = this._prefixFor(t);
    const hint = pfx ? `Имя: <b>${pfx}_</b>этаж_линия_номер`
      : 'Датчик: префикс ms_/il_ и парная освещённость — автоматически';
    return `<div class="overlay"><div class="sheet">
        <div class="sh-h">Назвать · ${this._esc(r.dev.name || ('ch' + r.dev.channel + '/' + r.dev.address))}</div>
        <div class="sh-hint">${hint}</div>
        <div class="nmgrid">
          <input id="nmFloor" inputmode="numeric" placeholder="этаж" autocomplete="off">
          <input id="nmLine" inputmode="numeric" placeholder="линия" autocomplete="off">
          <input id="nmNum" inputmode="numeric" placeholder="номер" autocomplete="off">
        </div>
        <div class="sh-foot">
          <button class="btn ghost" data-act="closeSheet">Отмена</button>
          <button class="btn primary" data-act="saveName">Сохранить</button>
        </div>
      </div></div>`;
  }

  // точечное обновление состояний без полного ререндера (троттл)
  _scheduleSync() {
    if (this._syncPending) return;
    this._syncPending = true;
    this._syncRaf = requestAnimationFrame(() => { this._syncPending = false; this._syncStates(); });
  }

  _syncStates() {
    const sr = this.shadowRoot;
    if (!sr) return;
    this._list().forEach(({ d, i }) => {
      const online = this._devOnline(d);
      const dot = sr.querySelector(`[data-dot="${i}"]`);
      if (dot) dot.classList.toggle('on', online);
      const live = sr.querySelector(`[data-live="${i}"]`);
      if (live) live.textContent = this._liveText(d);
      const tg = sr.querySelector(`[data-tg="${i}"]`);
      if (tg) { const on = this._lampOn(d); tg.textContent = on ? 'ВКЛ' : 'ВЫКЛ'; tg.classList.toggle('on', on); }
    });
  }

  // ── события ────────────────────────────────────────────────────────────────
  _devByIdx(el) {
    const i = +el.dataset.idx;
    return this._s.devices[i];
  }

  _onClick(e) {
    // закрываем лист ввода ТОЛЬКО по тапу на фон-оверлей (не по его содержимому —
    // иначе тап по input всплывал до оверлея, лист закрывался и клавиатура не появлялась)
    if (e.target.classList && e.target.classList.contains('overlay')) {
      this._s.rename = null; this._render(); return;
    }
    const el = e.target.closest('[data-act]');
    if (!el) return;
    const act = el.dataset.act;
    if (act === 'tab') {
      this._s.tab = el.dataset.tab; this._save();
      // на «Карту» заходим — подтягиваем таблицу (сшивка живая, кеша не держим)
      if (this._s.tab === 'map' && !this._s.mapTable.length) this._loadMapTable();
      else this._render();
    }
    else if (act === 'mapFilter') { this._s.mapFilter = el.dataset.f; this._save(); this._render(); }
    else if (act === 'mapAll' || act === 'mapNone') {
      const on = act === 'mapAll';
      for (const r of this._mapList()) {
        if (!r.skip && r.status === 'matched' && (!r.danger || !on)) {
          this._s.mapSel[this._mapKey(r)] = on;     // опасные массово не включаем, снимаем — да
        }
      }
      this._render();
    }
    else if (act === 'mapApply') this._applyMap();
    else if (act === 'mapView') {
      this._s.mapView = el.dataset.v;
      if (this._s.mapView === 'grp' && !this._s.groups.length) this._loadGroups();
      else this._render();                        // «План» ничего не грузит — он только запускает
    }
    else if (act === 'grpGeneral' || act === 'grpNone') {
      const only = act === 'grpGeneral';
      this._s.grpSel = {};
      if (only) {
        for (const g of this._s.groups) {
          if (GENERAL_GROUP_RE.test(g.name || '')) this._s.grpSel[this._grpKey(g)] = true;
        }
      }
      this._render();
    }
    else if (act === 'grpApply') this._applyLabels();
    else if (act === 'planDry') this._runPlan(false);
    else if (act === 'planApply') this._runPlan(true);
    else if (act === 'planLog') this._loadPlanLog(false);
    else if (act === 'planStop') this._stopPlan();
    else if (act === 'toggle') { const d = this._devByIdx(el); if (d) this._toggleLamp(d); }
    else if (act === 'rename') { const d = this._devByIdx(el); if (d) { this._s.rename = { dev: d }; this._render(); setTimeout(() => { const f = this.shadowRoot.getElementById('nmFloor'); if (f) f.focus(); }, 0); } }
    else if (act === 'saveName') this._saveName();
    else if (act === 'closeSheet') { this._s.rename = null; this._render(); }
  }

  _onChange(e) {
    const el = e.target;
    const act = el.dataset ? el.dataset.act : '';
    if (el.id === 'gwSel') {
      this._s.gw = el.value; this._save();
      this._loadDevices();
      if (this._s.tab === 'map') {
        if (this._s.mapView === 'grp') this._loadGroups(); else this._loadMapTable();
      }
    }
    else if (el.id === 'onlyUnnamed') { this._s.onlyUnnamed = el.checked; this._save(); this._render(); }
    else if (act === 'mapFile') { this._s.mapFile = el.value; this._save(); this._loadMapTable(); }
    else if (act === 'mapArea') { this._s.mapArea = el.checked; this._save(); }
    else if (act === 'mapSel') {
      this._s.mapSel[el.dataset.key] = el.checked;
      this._updateApplyCount();      // без полного _render: иначе теряется фокус/скролл списка
    }
    else if (act === 'mapName') {
      // правка имени руками перебивает карту (человек видит железо, карта — только источник)
      this._s.mapEdit[el.dataset.key] = el.value;
    }
    else if (act === 'grpSel') { this._s.grpSel[el.dataset.key] = el.checked; this._render(); }
    else if (act === 'grpLabel') { this._s.grpLabel = el.value; this._save(); }
    else if (act === 'planScript') { this._s.planScript = el.value; this._save(); }
    else if (act === 'planPhase') { this._s.planPhase = el.value; this._save(); this._render(); }
    else if (act === 'mapSpace') {
      // то же для пространства: предзаполнено из карты, но последнее слово за человеком
      this._s.mapSpaceEdit[el.dataset.key] = el.value;
    }
  }

  _updateApplyCount() {
    const btn = this.shadowRoot && this.shadowRoot.querySelector('[data-act="mapApply"]');
    if (!btn) return;
    const n = this._mapList().filter((r) => this._s.mapSel[this._mapKey(r)] && !r.skip).length;
    btn.textContent = `Применить (${n})`;
    btn.disabled = !n || !!this._s.mapBusy;
  }

  _onKey(e) {
    if (e.key !== 'Enter') return;
    // автопереход между полями имени, на последнем — сохранить
    const order = ['nmFloor', 'nmLine', 'nmNum'];
    const idx = order.indexOf(e.target.id);
    if (idx < 0) return;
    e.preventDefault();
    if (idx < order.length - 1) { const n = this.shadowRoot.getElementById(order[idx + 1]); if (n) n.focus(); }
    else this._saveName();
  }
}

const STYLE = `
:host{display:block}*{box-sizing:border-box}
.root{font-family:var(--ha-card-font-family,system-ui,-apple-system,"Segoe UI",Roboto,sans-serif)}
.card{position:relative;display:flex;flex-direction:column;min-height:calc(100dvh - 16px);border-radius:16px;padding:14px;color:#0F172A;background:linear-gradient(135deg,#cfe3ff 0%,#e7f1ff 42%,#fff 100%);box-shadow:0 6px 24px rgba(2,132,199,.14);overflow:hidden}
.hd{margin-bottom:10px}.hd-title b{font-size:18px}.hd-title span{font-size:11px;color:#64748B;margin-left:6px}
.fld{display:block;margin-bottom:10px}.fld span{display:block;font-size:12px;color:#64748B;margin-bottom:4px}
select{width:100%;min-height:44px;border:1px solid #cfe0f5;border-radius:10px;padding:8px 10px;font:inherit;font-size:16px;color:#0F172A;-webkit-text-fill-color:#0F172A;background:#fff;-webkit-appearance:menulist;appearance:menulist}
.tabs{display:flex;gap:6px;margin-bottom:10px}
.tab{flex:1;min-height:44px;border:1px solid #cfe0f5;background:#fff;color:#0284C7;border-radius:10px;font:inherit;font-weight:600;cursor:pointer}
.tab.on{background:#0284C7;color:#fff;border-color:#0284C7}
.chk{display:flex;align-items:center;gap:8px;font-size:14px;color:#0F172A;margin-bottom:10px;min-height:32px}
.chk input{width:20px;height:20px}
.list{display:flex;flex-direction:column;gap:8px;flex:1;min-height:0;overflow-y:auto;overflow-x:hidden;-webkit-overflow-scrolling:touch}
.row{display:flex;align-items:center;gap:10px;padding:10px;border:1px solid #e3eefb;border-radius:12px;background:#fff}
.row.unnamed{border-color:#f59e0b;background:#fffbeb}
.row.off{opacity:.55}
.rinfo{flex:1;min-width:0}
.rname{font-weight:600;font-size:15px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.rmeta{font-size:12px;color:#64748B;display:flex;align-items:center;gap:6px;margin-top:2px}
.dot{width:9px;height:9px;border-radius:50%;background:#cbd5e1;flex:none}.dot.on{background:#16a34a}
.ract{display:flex;align-items:center;gap:8px;flex:none}
.live{font-size:12px;color:#0F172A;max-width:42vw;text-align:right}
.tg{min-width:64px;min-height:44px;border-radius:10px;border:1px solid #cfe0f5;background:#f1f5f9;color:#64748B;font:inherit;font-weight:700;cursor:pointer}
.tg.on{background:#16a34a;color:#fff;border-color:#16a34a}
.name{min-height:44px;padding:0 12px;border-radius:10px;border:1px solid #0284C7;background:#fff;color:#0284C7;font:inherit;font-weight:600;cursor:pointer}
.overlay{position:fixed;inset:0;background:rgba(15,23,42,.35);display:flex;align-items:flex-end;justify-content:center;z-index:10}
.sheet{width:100%;max-width:520px;background:#fff;border-radius:18px 18px 0 0;padding:16px;box-shadow:0 -8px 30px rgba(2,132,199,.2)}
.sh-h{font-weight:700;font-size:16px;margin-bottom:4px}
.sh-hint{font-size:12px;color:#64748B;margin-bottom:12px}
.nmgrid{display:flex;gap:8px;margin-bottom:14px}
.nmgrid input{flex:1;width:100%;min-height:52px;border:1px solid #cfe0f5;border-radius:10px;padding:8px;font:inherit;font-size:18px;text-align:center}
.sh-foot{display:flex;gap:10px}
.btn{flex:1;min-height:48px;border-radius:12px;font:inherit;font-weight:600;cursor:pointer;border:1px solid #cfe0f5;background:#fff}
.btn.ghost{color:#64748B}.btn.primary{background:#0284C7;color:#fff;border-color:#0284C7}
.muted{color:#64748B}.pad{padding:16px;text-align:center}
.toast{position:fixed;left:50%;bottom:16px;transform:translateX(-50%) translateY(20px);background:#0F172A;color:#fff;padding:10px 16px;border-radius:10px;opacity:0;transition:.2s;pointer-events:none;z-index:20;font-size:14px}
.toast.show{opacity:1;transform:translateX(-50%) translateY(0)}.toast.err{background:#dc2626}
/* ── вкладка «Карта» (переезд объекта) ── */
.tab.sm{min-height:36px;font-size:13px;font-weight:500}
.sum{display:flex;flex-wrap:wrap;gap:6px;margin-bottom:8px}
.chip{font-size:12px;padding:3px 8px;border-radius:999px;background:#eef4fb;color:#475569;white-space:nowrap}
.chip.ok{background:#dcfce7;color:#166534}.chip.warn{background:#fef3c7;color:#92400e}
.chip.bad{background:#fee2e2;color:#991b1b}
.warnbox{font-size:12px;color:#92400e;background:#fef3c7;border-radius:8px;padding:8px;margin-bottom:8px}
.rw{display:flex;gap:8px;background:#fff;border:1px solid #e2ecf7;border-radius:12px;padding:10px}
.rw.off{opacity:.55}
.rw-chk{display:flex;align-items:flex-start;padding-top:2px}.rw-chk input{width:22px;height:22px}
.rw-body{flex:1;min-width:0}
.rw-top{display:flex;align-items:center;gap:6px;flex-wrap:wrap;font-size:14px}
.rw-cur{font-size:12px;color:#64748B;margin:2px 0 6px}
.rw-name{width:100%;min-height:40px;border:1px solid #cfe0f5;border-radius:10px;padding:6px 10px;font:inherit;font-size:15px;color:#0F172A;-webkit-text-fill-color:#0F172A;background:#fff}
.rw-sub{display:block;font-size:11px;color:#64748B;margin-top:6px}
.rw-note{font-size:11px;color:#92400e;margin-top:4px}
.rw-warn{font-size:11px;color:#991b1b;margin-top:4px}
.foot{display:flex;gap:8px;align-items:center;flex-wrap:wrap;padding-top:10px}
.foot .btn{min-height:44px}
.busy{flex:1 0 100%;font-size:12px;color:#0284C7}
/* экран «План»: вывод скрипта — моноширинный, со скроллом, чтобы не ломать мобильную вёрстку */
.plan-btns{display:flex;gap:8px;margin:12px 0}.plan-btns .btn{flex:1}
.plan-out{margin-top:12px;padding:10px;background:#0F172A;color:#E2E8F0;border-radius:8px;
  font:12px/1.45 ui-monospace,SFMono-Regular,Menlo,monospace;white-space:pre-wrap;
  word-break:break-word;max-height:50vh;overflow:auto;text-align:left}
.plan-out.bad{border:1px solid #DC2626}
.plan-btns .btn.stop{background:#DC2626;color:#fff;border-color:#DC2626}
.prog{margin-top:12px}
.prog-head{font-weight:600;margin-bottom:6px}
.prog-bar{height:10px;border-radius:6px;background:#E2E8F0;overflow:hidden}
.prog-bar i{display:block;height:100%;background:linear-gradient(90deg,#2563EB,#60A5FA);
  transition:width .3s ease}
.prog-num{display:flex;flex-wrap:wrap;gap:10px;margin-top:6px;font-size:13px}
.prog-num .ok{color:#059669}.prog-num .warn{color:#B45309}.prog-num .bad{color:#DC2626}
.pad .fld{text-align:left}.muted.sm{font-size:12px;text-align:left}
`;

customElements.define('arvid-dali-commissioning', ArvidDaliCommissioning);
window.customCards = window.customCards || [];
window.customCards.push({
  type: 'arvid-dali-commissioning',
  name: 'ARVID DALI — Пусконаладка',
  description: 'Мобильная карта быстрой детекции и нейминга DALI-устройств',
});
console.info('%c ARVID-DALI-COMMISSIONING %c v' + VERSION + ' ',
  'background:#0284C7;color:#fff;border-radius:4px 0 0 4px;padding:2px 6px',
  'background:#e7f1ff;color:#0284C7;border-radius:0 4px 4px 0;padding:2px 6px');
