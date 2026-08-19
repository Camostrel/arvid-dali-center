/*
 * arvid-dali-panel — Lovelace custom card для интеграции ARVID DALI Center.
 * Vanilla JS custom element (без сборки), Shadow DOM. Управление — через HA
 * WebSocket API (arvid_dali_center/*) и стандартные сервисы HA (свет).
 * Тема — сине-белый градиент (airy).
 *
 * Установка: /config/www/arvid-dali-panel.js → ресурс /local/arvid-dali-panel.js
 * (JavaScript Module) → карточка type: custom:arvid-dali-panel.
 */

const ICONS = {
  scan: '<path d="M3 7V5a2 2 0 0 1 2-2h2M17 3h2a2 2 0 0 1 2 2v2M21 17v2a2 2 0 0 1-2 2h-2M7 21H5a2 2 0 0 1-2-2v-2M3 12h18"/>',
  restart: '<path d="M3 12a9 9 0 1 0 3-6.7L3 8M3 3v5h5"/>',
  param: '<circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.6 1.6 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.6 1.6 0 0 0-2.7 1.1V21a2 2 0 1 1-4 0v-.1A1.6 1.6 0 0 0 7 19.4a1.6 1.6 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1A1.6 1.6 0 0 0 2.6 15H2a2 2 0 1 1 0-4h.1A1.6 1.6 0 0 0 4.6 7L4.5 7a2 2 0 1 1 2.8-2.8l.1.1A1.6 1.6 0 0 0 9 4.6V4a2 2 0 1 1 4 0v.1A1.6 1.6 0 0 0 17 4.6l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1A1.6 1.6 0 0 0 21.4 11H21"/>',
  addr: '<path d="M4 7h16M4 12h16M4 17h10"/>',
  bulb: '<path d="M9 18h6M10 22h4M12 2a7 7 0 0 0-4 12.7c.6.5 1 1.3 1 2.1V18h6v-1.2c0-.8.4-1.6 1-2.1A7 7 0 0 0 12 2Z"/>',
  bulbOff: '<path d="M3 3l18 18M9 18h6M10 22h4M8.6 8.6A7 7 0 0 0 8 14.7c.6.5 1 1.3 1 2.1V18h6"/>',
  brightness: '<circle cx="12" cy="7.5" r="3"/><path d="M12 1.6v1.3M12 12.1v1.3M4.8 7.5H3.5M20.5 7.5h-1.3M6.9 2.4 6 1.5M18 2.4l.9-.9M6.9 12.6 6 13.5M18 12.6l.9.9M3 20h6.4M14.6 20H21"/><circle cx="12" cy="20" r="2.1"/>',
  flash: '<path d="M13 2 4 14h7l-1 8 9-12h-7l1-8z"/>',
  pencil: '<path d="M16.5 3.5 20.5 7.5 8 20 3 21 4 16Z"/><path d="M14 6 18 10"/>',
  list: '<path d="M8 6h13M8 12h13M8 18h13M3 6h.01M3 12h.01M3 18h.01"/>',
  warn: '<path d="M10.3 3.2 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.2a2 2 0 0 0-3.4 0Z"/><path d="M12 9v4M12 17h.01"/>',
  chev: '<path d="M9 6l6 6-6 6"/>',   // шеврон сворачивания секции (вправо=свёрнуто, поворот→вниз=раскрыто)
  chart: '<path d="M3 3v18h18M7 15l3-4 3 3 4-6"/>',   // энергомониторинг
  health: '<path d="M3 12h5l2-6 4 12 2-6h5"/>',   // здоровье устройств (пульс)
};
const svg = (p, cls = '') =>
  `<svg class="ic ${cls}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${p}</svg>`;

const LIGHT_T = ['0101', '0102', '0103', '0104', '0105', '0106'];
const GESTURE_LABEL = { 1: 'клик', 2: 'удержание', 3: 'двойной', 4: 'поворот', 5: 'конец удержания' };
const SENSOR_T = ['0201', '0202'];
const PANEL_T = ['0300', '0302', '0304', '0306', '0308'];
const GROUPS = [
  { key: 'light', title: 'Лампы', types: LIGHT_T },
  { key: 'sensor', title: 'Датчики', types: SENSOR_T },
  { key: 'panel', title: 'Панели', types: ['0300', '0302', '0304', '0306', '0308'] },
];
// fade-индекс → человекочитаемая подпись. ВАЖНО: это РАЗНЫЕ механизмы (см.
// docs/PLAN_SENSOR_BINDINGS.md §Fade): fadeTime — разгон при вкл/выкл/задать-яркость
// (команда set-level), fadeRate — скорость рампы при удержании «плавно ярче/темнее»
// (команды UP/DOWN). Выше индекс = медленнее/мягче у ОБОИХ. Выбор из списка, не ручной ввод.
const FADE_TIME_OPTS = [[0, '0 — резко'], [1, '1 — 0.7 с'], [2, '2 — 1.0 с'], [3, '3 — 1.4 с'], [4, '4 — 2.0 с'], [5, '5 — 2.8 с'], [6, '6 — 4.0 с'], [7, '7 — 5.7 с'], [8, '8 — 8.0 с'], [9, '9 — 11 с'], [10, '10 — 16 с'], [11, '11 — 23 с'], [12, '12 — 32 с'], [13, '13 — 45 с'], [14, '14 — 64 с'], [15, '15 — 90 с']];
const FADE_RATE_OPTS = [[1, '1 — 358 шаг/с (быстро, ступенчато)'], [2, '2 — 253 шаг/с'], [3, '3 — 179 шаг/с'], [4, '4 — 127 шаг/с'], [5, '5 — 89 шаг/с'], [6, '6 — 63 шаг/с'], [7, '7 — 45 шаг/с'], [8, '8 — 32 шаг/с (комфортно)'], [9, '9 — 22 шаг/с'], [10, '10 — 16 шаг/с'], [11, '11 — 11 шаг/с'], [12, '12 — 8 шаг/с'], [13, '13 — 5.6 шаг/с'], [14, '14 — 4 шаг/с'], [15, '15 — 2.8 шаг/с (плавно, медленно)']];
const LAMP_FIELDS = [
  ['fadeTime', 'Fade time · разгон вкл/выкл', FADE_TIME_OPTS],
  ['fadeRate', 'Fade rate · «плавно ярче/темнее»', FADE_RATE_OPTS],
  ['minBrightness', 'Мин. яркость'], ['maxBrightness', 'Макс. яркость'],
  ['powerStatus', 'Уровень при включении'],
];
// Параметры датчиков. ⚠ Зона/чувствительность/время присутствия — свойства ДАТЧИКА ДВИЖЕНИЯ
// (0201): у датчика освещённости (0202) их физически нет, показывать их там — врать (v1.2.26).
const SENSOR_FIELDS_COMMON = [
  ['reportTime', 'Интервал отчёта'], ['downTime', 'Мин. интервал сообщений'],
];
const SENSOR_FIELDS_MOTION = [
  ['coverage', 'Зона (0-100)'], ['sensitivity', 'Чувствительность (0-100)'],
  ['occpyTime', 'Время присутствия'],
];
const SENSOR_FIELDS = [...SENSOR_FIELDS_COMMON, ...SENSOR_FIELDS_MOTION];   // для мульти-настройки
// Набор полей под КОНКРЕТНЫЙ тип датчика
const sensorFields = (devType) => (String(devType) === '0202'
  ? SENSOR_FIELDS_COMMON : SENSOR_FIELDS);

class ArvidDaliPanel extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: 'open' });
    this._state = {
      gateways: [], activeGw: null, devices: [], groups: [], groupState: {},
      loading: true, scanning: false, scanLog: [], scanConflicts: [], modal: null,
      lastBright: {},   // последняя заданная яркость (ключ briKey) — для модалки, когда сущность выключена
      events: [], eventsOpen: false,   // журнал (последние события) + флаг панели
      energy: {},   // КАРТА БЕЙДЖЕЙ (ключ devSn): {power_w (РАСЧЁТНАЯ), total_wh, on_time_h, alarm}. Пишет _pollEnergy, читает _energyBadge. НИКОГДА не null (иначе _syncStates падает).
      energyPage: null,   // состояние СТРАНИЦЫ энергомониторинга ({loading,tariff,lamps,fFloor,fArea}) — отдельно от карты бейджей
    };
    this._evUnsub = null;
    this._rendered = false;
  }

  setConfig(config) { this._config = config || {}; }
  getCardSize() { return 12; }

  connectedCallback() {
    // повторное добавление карты в DOM — перезапустить опрос энергии (init был раньше)
    if (this._hass && !this._enT) this._startEnergyPoll();
  }

  disconnectedCallback() {
    if (this._evUnsub) { try { this._evUnsub(); } catch (e) { /* ignore */ } this._evUnsub = null; }
    // снять подписку скана, если карту удалили во время активного скана (иначе утечка)
    if (this._scanUnsub) { try { this._scanUnsub(); } catch (e) { /* ignore */ } this._scanUnsub = null; }
    clearTimeout(this._tt);   // висящий таймер тоста — на всякий случай
    clearTimeout(this._gwT);  // debounce обновления шлюзов
    if (this._enT) { clearInterval(this._enT); this._enT = null; }   // опрос энергии
    // F10 (v1.2.20): отменяя запланированный синк, СБРОСИТЬ и флаг — иначе его колбэк, который
    // сбрасывал `_syncPending=false`, не выполнится, и после повторного добавления карты в DOM
    // `_scheduleSync` навсегда выходит на первой строке (`if (_syncPending) return`) → точки
    // связи/яркости/бейджи застывают до случайного полного `_render`.
    if (this._syncRaf) { cancelAnimationFrame(this._syncRaf); this._syncRaf = null; }
    this._syncPending = false;
  }

  set hass(hass) {
    const first = !this._hass;
    this._hass = hass;
    if (first) this._init();
    else if (this._rendered && !this._state.modal) this._scheduleSync();
  }

  async _ws(msg) { return this._hass.connection.sendMessagePromise(msg); }
  _st(ent) { return ent && this._hass && this._hass.states ? this._hass.states[ent] : null; }

  async _init() { this._renderShell(); await this._loadGateways(); this._subscribeEvents(); this._startEnergyPoll(); }

  // ── живая энергия ламп: опрос energy_live по таймеру (НЕ HA-сенсоры) ────────
  // Мощность меняется ~раз в 24с (частота reportEnergy) → опрашиваем реже полной
  // перерисовки, точечно патчим бейджи через _scheduleSync. Не долбим, если вкладка
  // скрыта или карта без активного шлюза.
  _startEnergyPoll() {
    if (this._enT) return;
    this._pollEnergy();
    this._enT = setInterval(() => this._pollEnergy(), 15000);   // 15с
  }

  async _pollEnergy() {
    const gw = this._state.activeGw;
    if (!gw || !this._hass || (document.hidden)) return;
    // C4 (v1.2.17): `try` сужен до САМОГО ЗАПРОСА. Раньше он накрывал и мердж, и `_scheduleSync()`,
    // а `catch` глушил всё молча под предлогом «энергия не критична» — из-за этого ошибка
    // отрисовки (`suspect is not defined`, v1.2.6→v1.2.13) не оставляла НИ ОДНОГО следа.
    let r;
    try {
      r = await this._ws({ type: 'arvid_dali_center/energy_live', gw_sn: gw });
    } catch (e) {
      console.error('arvid-dali-panel: опрос энергии не удался', e);   // не критично, но ВИДНО
      return;
    }
    {
      if (gw !== this._state.activeGw) return;
      // МЕРДЖ, не замена: бейджи по devSn (глобально уникален) КЕШИРУЮТСЯ между шлюзами —
      // при переключении чипов не мигаем пустотой.
      // (v1.2.6: метка приёма `_rx` убрана — она проверяла СВЕЖЕСТЬ отчёта ШЛЮЗА, а его больше
      //  нет: мощность считается по текущему состоянию сущности и устареть не может.)
      const inc = (r && r.energy) || {};
      for (const sn in inc) this._state.energy[sn] = { ...inc[sn] };
      if (this._rendered && !this._state.modal) this._scheduleSync();
    }
  }

  // Живой журнал: копим последние события; при событиях связи (conn) — освежаем
  // состояние шлюзов (баннер/чип), при доступности (avail) — пере-синк статусов.
  async _subscribeEvents() {
    if (this._evUnsub) return;
    try {
      this._evUnsub = await this._hass.connection.subscribeMessage((m) => {
        const rec = m && m.event;
        if (!rec) return;
        this._state.events.push(rec);
        if (this._state.events.length > 1000) this._state.events.shift();
        if (rec.kind === 'conn') this._refreshGatewaysState();
        if (this._state.eventsOpen) this._renderEvents();
        if (rec.kind === 'avail' || rec.kind === 'conn') this._scheduleSync();
      }, { type: 'arvid_dali_center/events_subscribe' });
    } catch (e) { /* журнал не критичен */ }
  }

  // debounce: пачка conn-событий не должна бить полным _render на каждый тик
  _refreshGatewaysState() {
    clearTimeout(this._gwT);
    this._gwT = setTimeout(() => this._doRefreshGateways(), 200);
  }

  async _doRefreshGateways() {
    // C4 (v1.2.17): `try` — только вокруг запроса; `_render()` вынесен наружу (раньше его отказ
    // молча глотался под «пропускаем»).
    let r;
    try {
      r = await this._ws({ type: 'arvid_dali_center/gateways' });
    } catch (e) {
      console.error('arvid-dali-panel: обновление списка шлюзов не удалось', e);
      return;
    }
    const byId = {};
    (r.gateways || []).forEach((g) => { byId[g.gwSn] = g; });
    this._state.gateways = this._state.gateways.map((g) => byId[g.gwSn] || g);
    // F9 (v1.2.20): НЕ перерисовывать поверх ОТКРЫТОЙ модалки. `_render()` пересобирает весь
    // innerHTML вместе с модалкой → введённый в диалоге текст/отметки (напр. сотни галок в
    // «Массовой настройке») слетают. Триггер — `conn`-событие ЛЮБОГО шлюза (связь флапает).
    // Гейт `!modal` уже стоит в `set hass` и `_pollEnergy` — здесь был забыт. Состояние шлюзов
    // обновлено в state; отрисуется при следующем рендере (в т.ч. при закрытии модалки).
    if (!this._state.modal) this._render();
  }

  _activeGw() { return this._state.gateways.find((g) => g.gwSn === this._state.activeGw) || null; }

  // ── журнал (панель в карточке) ───────────────────────────────────────────
  async _toggleEvents() {
    this._state.eventsOpen = !this._state.eventsOpen;
    if (this._state.eventsOpen) {
      try {
        const r = await this._ws({ type: 'arvid_dali_center/events', limit: 1000 });
        this._state.events = r.events || [];
      } catch (e) { /* журнал не критичен */ }
    }
    this._render();
  }

  _esc(s) { return String(s == null ? '' : s).replace(/[&<>]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;' }[c])); }
  _evTime(ts) { try { return new Date((ts || 0) * 1000).toLocaleTimeString(); } catch (e) { return ''; } }

  _eventRows() {
    const evs = this._state.events.slice(-300).reverse();   // новые сверху
    if (!evs.length) return '<div class="muted" style="padding:8px">Событий нет.</div>';
    return evs.map((e) => `<div class="logrow ev-${e.level || 'info'}"><span class="evt">${this._evTime(e.ts)}</span><span class="evk">${this._esc(e.kind)}</span><span>${this._esc(e.msg)}</span></div>`).join('');
  }
  _renderEvents() {
    const box = this.shadowRoot.getElementById('evbox');
    if (box) box.innerHTML = this._eventRows();
    const c = this.shadowRoot.getElementById('evcount');
    if (c) c.textContent = this._state.events.length;
  }
  _eventsPanel() {
    if (!this._state.eventsOpen) return '';
    return `<section class="panel log"><div class="panel-h">${svg(ICONS.list, 'sm')} Журнал <span class="muted" id="evcount">${this._state.events.length}</span><button class="mini" data-act="closeEvents">Закрыть</button></div><div class="logbox events" id="evbox">${this._eventRows()}</div></section>`;
  }

  // ── баннер состояния связи активного шлюза ───────────────────────────────
  _connBanner() {
    const g = this._activeGw();
    if (!g || !g.state || g.state === 'online') return '';
    const txt = {
      offline: 'Нет связи с контроллером — идёт автоматическое восстановление…',
      reauth: 'Переподключение к контроллеру (re-discovery, без скана шины)…',
      init: 'Подключение к контроллеру…',
    }[g.state] || ('Состояние связи: ' + g.state);
    return `<section class="banner warn">${svg(ICONS.warn)}<span>${txt}</span></section>`;
  }

  // online устройства: по доступности рабочей сущности (unavailable=offline), иначе из снимка
  _devOnline(dev) {
    const E = dev.entities || {};
    const ent = E.light || E.motion || E.lux || E.event;
    if (ent) { const st = this._st(ent); if (st) return st.state !== 'unavailable'; }
    const s = dev.status; return String(s) === 'online' || s === 1 || s === true;
  }

  async _loadGateways() {
    try {
      const r = await this._ws({ type: 'arvid_dali_center/gateways' });
      this._state.gateways = r.gateways || [];
      if (!this._state.activeGw && this._state.gateways.length) {
        // восстановить последний выбранный шлюз из localStorage (если он ещё найден),
        // иначе — первый. Чтобы выбор не сбрасывался на первый чип при каждой перезагрузке.
        let saved = null;
        try { saved = localStorage.getItem('arvid-dali:gw'); } catch (e) { /* ignore */ }
        const ok = saved && this._state.gateways.some((g) => g.gwSn === saved);
        this._state.activeGw = ok ? saved : this._state.gateways[0].gwSn;
      }
    } catch (e) { this._toast('Ошибка загрузки шлюзов: ' + e.message, true); }
    this._state.loading = false;
    await this._loadDevices();
  }

  async _loadDevices() {
    const gw = this._state.activeGw;
    if (!gw) { this._state.loading = false; this._render(); return; }
    try {
      const d = await this._ws({ type: 'arvid_dali_center/devices', gw_sn: gw });
      if (gw !== this._state.activeGw) return;
      this._state.devices = d.devices || [];
    } catch (e) { this._toast('Ошибка устройств: ' + e.message, true); }
    this._state.loading = false;
    this._render();
    // C4 (v1.2.17): `try` — только вокруг запроса. Раньше в него попадал и `_render()`, и его
    // отказ глушился под «группы не критичны» — а падал там ОБЩИЙ рендер, а не группы.
    let g;
    try {
      g = await this._ws({ type: 'arvid_dali_center/groups', gw_sn: gw });
    } catch (e) {
      console.error('arvid-dali-panel: загрузка групп не удалась', e);
      return;
    }
    if (gw !== this._state.activeGw) return;
    this._state.groups = g.groups || [];
    this._render();
    // кросс-шлюзовые группы — ОТДЕЛЬНАЯ модель: шлюзу не принадлежат, грузим общим списком
    // (в нём участвуют лампы разных контроллеров, поэтому фильтровать по activeGw нечем)
    try {
      const x = await this._ws({ type: 'arvid_dali_center/cross_groups' });
      this._state.xgroups = x.groups || [];
      this._render();
    } catch (e) { console.error('arvid-dali-panel: загрузка кросс-групп не удалась', e); }
  }

  // последние 5 знаков серийника шлюза — так они подписаны в составе кросс-группы
  _gwTail(sn) { return String(sn || '').slice(-5); }

  // ── действия ──────────────────────────────────────────────────────────────
  // flag='exited' — опрос КЕША шлюза (быстро, безопасно); 'busDevice' — физический
  // опрос DALI-линии (медленно, МОЖЕТ переназначить короткие адреса → подтверждение).
  // выбор режима опроса шины (manual — показать конфликты / auto — развести дубли)
  _openScanMode() {
    if (this._state.scanning) return;
    this._state.modal = { kind: 'scanMode' };
    this._render();
  }

  // flag='exited' — опрос КЕША шлюза (быстро, инлайн-лог); 'busDevice' — физический
  // опрос DALI-линии (модальное окно: живой список + конфликтные адреса).
  // assign='manual' — конфликты показываем; 'auto' — шлюз САМ переназначает дубли.
  // opts.skipConfirm — не спрашивать подтверждение (цепочка после «Разрешить конфликты»:
  // пользователь уже согласился). opts.note — заголовок лога. Возврат: запустился ли скан.
  async _scan(flag = 'exited', assign = 'manual', opts = {}) {
    if (this._state.scanning) return false;
    const gw = this._state.activeGw;
    const isBus = flag === 'busDevice';
    if (!opts.skipConfirm && isBus && assign === 'manual' &&
        !confirm('Опрос ШИНЫ — физический опрос DALI-линии. Идёт медленно и МОЖЕТ ПЕРЕНАЗНАЧИТЬ короткие адреса устройств. Продолжить?')) return false;
    if (!opts.skipConfirm && isBus && assign === 'auto' &&
        !confirm('Разрешение конфликтов: шлюз САМ переназначит дублирующиеся короткие адреса, затем шина будет перечитана. Разрушающая операция. Продолжить?')) return false;
    this._state.scanning = true;
    this._state.scanLog = [{ t: (opts.note || (isBus ? (assign === 'auto' ? 'Развожу конфликтные адреса…' : 'Опрос шины запущен…') : 'Обновление из кеша…')), kind: 'info' }];
    if (assign !== 'auto') this._state.scanConflicts = [];   // у разведения конфликтов список не сбрасываем
    // F2: у разведения конфликтов СВОЙ экран (оно не перечисляет устройства — см. модалку)
    if (isBus) this._state.modal = { kind: assign === 'auto' ? 'resolve' : 'scan' };
    this._render();
    try {
      this._scanUnsub = await this._hass.connection.subscribeMessage((m) => {
        if (!m || !m.event) return;
        if (m.event === 'found' && m.device) {
          const dv = m.device;
          this._state.scanLog.push({ t: `${dv.typeName} ch${dv.channel}/${dv.address}` + (dv.devSn ? ` · ${dv.devSn}` : ''), kind: 'found' });
          this._renderLog(); this._renderScan();
        } else if (m.event === 'conflict' && m.item) {
          this._state.scanConflicts.push(m.item);
          this._renderScan();
        }
      }, { type: 'arvid_dali_center/scan', gw_sn: gw, flag, assign });
      this._state.scanLog.push({ t: (assign === 'auto' ? 'Конфликты разведены.' : 'Скан завершён.'), kind: 'info' });
    } catch (e) { this._state.scanLog.push({ t: 'Ошибка скана: ' + e.message, kind: 'err' }); }
    this._state.scanning = false;
    await this._loadDevices();   // перечитать устройства; _render обновит и модалку скана
    return true;
  }

  // «Разрешить конфликты» = ОПЕРАЦИЯ, а не скан (v1.1.4): шлюз САМ переназначает дублирующиеся
  // короткие адреса и НИЧЕГО НЕ ПЕРЕЧИСЛЯЕТ (список устройств приходит пустой — проверено на
  // железе). Поэтому следом СРАЗУ перечитываем шину обычным сканом — иначе пользователь
  // остаётся с исправленной шиной и устаревшим составом.
  async _resolveConflicts() {
    const ran = await this._scan('busDevice', 'auto');
    if (!ran) return;                            // пользователь отменил подтверждение
    await this._scan('busDevice', 'manual',
                     { skipConfirm: true, note: 'Конфликты разведены — перечитываю шину…' });
  }

  // общий сброс адресов (resetGateway deviceReset) — двойное подтверждение
  async _resetAddrs() {
    if (this._state.scanning) return;
    if (!confirm('ОБЩИЙ СБРОС АДРЕСОВ контроллера ' + this._state.activeGw + '.\n\nШлюз ПЕРЕНАЗНАЧИТ короткие адреса ВСЕМ устройствам шины (как «сброс адресов» в DALI Center PC). Привязка адрес↔устройство изменится, возможен ребут шлюза.\n\nПродолжить?')) return;
    if (!confirm('Точно сбросить адреса всего контроллера? Действие разрушающее.')) return;
    try {
      const r = await this._ws({ type: 'arvid_dali_center/reset_addresses', gw_sn: this._state.activeGw });
      this._toast(r.ok ? 'Сброс адресов отправлен — переадресация на шине идёт, затем «Сканировать заново»' : 'Не подтверждено', !r.ok);
    } catch (e) { this._toast('Ошибка: ' + e.message, true); }
  }

  async _restart() {
    if (!confirm('Перезапустить контроллер ' + this._state.activeGw + '?')) return;
    try {
      const r = await this._ws({ type: 'arvid_dali_center/restart_gateway', gw_sn: this._state.activeGw });
      this._toast(r.ok ? 'Команда рестарта отправлена' : 'Не подтверждено', !r.ok);
    } catch (e) { this._toast('Ошибка: ' + e.message, true); }
  }

  // Стереть ДАННЫЕ устройств шлюза (имена/параметры/энергия по devSn + устройства/группы шлюза).
  // Явное необратимое действие, двойное подтверждение. Переехавшие на другой шлюз — не трогает (M1).
  async _wipeData() {
    const gw = this._state.activeGw;
    if (!confirm(`Стереть ВСЕ данные устройств шлюза ${gw}?\n\nБудут удалены ПОЛНОСТЬЮ: имена (в т.ч. личные — из реестра HA), параметры (мощность/кривая), энергия, привязки поворота; сами устройства и сущности из реестра HA; список устройств и группы шлюза.\n\nУстройства, переехавшие на ДРУГОЙ шлюз, не затрагиваются.\n\nЭто НЕОБРАТИМО. Устройства появятся заново ЧИСТЫМИ (без имён) при следующем скане.`)) return;
    if (!confirm(`Точно стереть? Личные имена по ${gw} восстановить будет нельзя.`)) return;
    try {
      const r = await this._ws({ type: 'arvid_dali_center/wipe_gateway_data', gw_sn: gw });
      this._toast(`Стёрто устройств: ${r.wiped}${r.kept ? `, пропущено переехавших: ${r.kept}` : ''}. Сущности обновятся после рескана.`);
      await this._loadDevices();
    } catch (e) { this._toast('Ошибка: ' + e.message, true); }
  }

  // ── Сеть и имя шлюза ───────────────────────────────────────────────────────
  // Открыть модалку настроек: грузим текущие сетевые параметры (getGwIpInfor),
  // EMQ-поля бэкенд карточке НЕ отдаёт (держит у себя для безопасного echo при записи).
  async _openGwSettings() {
    const gw = this._state.activeGw;
    if (!gw) { this._toast('Шлюз не выбран', true); return; }
    this._state.modal = { kind: 'gwSettings', gwSn: gw, loading: true };
    this._render();
    try {
      const r = await this._ws({ type: 'arvid_dali_center/get_gw_net', gw_sn: gw });
      this._state.modal = { kind: 'gwSettings', gwSn: gw, loading: false, cur: r || {}, mode: (r && r.mode) || 'dhcp' };
    } catch (e) {
      this._state.modal = { kind: 'gwSettings', gwSn: gw, loading: false, cur: {}, mode: 'dhcp', error: e.message };
    }
    this._render();
  }

  _isIp4(s) { return /^(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})$/.test((s || '').trim()) && s.split('.').every((o) => +o >= 0 && +o <= 255); }

  // одна ли подсеть у двух IP при данной маске (для предупреждения о потере шлюза)
  _sameSubnet(ip1, ip2, mask) {
    const toInt = (s) => s.trim().split('.').reduce((a, o) => ((a << 8) + (+o & 255)) >>> 0, 0);
    try { const m = toInt(mask); return ((toInt(ip1) & m) >>> 0) === ((toInt(ip2) & m) >>> 0); }
    catch (e) { return true; }   // не смогли посчитать → не пугаем
  }

  // Сохранить имя шлюза (setGatewayName). floorId/floorName сохраняет бэкенд.
  async _saveGwName() {
    const name = ((this.shadowRoot.getElementById('gwName') || {}).value || '').trim();
    if (!name) { this._toast('Имя не может быть пустым', true); return; }
    try {
      const r = await this._ws({ type: 'arvid_dali_center/set_gw_name', gw_sn: this._state.modal.gwSn, name });
      if (r && r.ok) {
        this._toast('Имя шлюза сохранено');
        if (this._state.modal && this._state.modal.cur) this._state.modal.cur.name = name;  // отразить в открытой модалке
        await this._loadGateways();   // обновить чип (заголовок = имя)
      } else this._toast('Не подтверждено шлюзом', true);
    } catch (e) { this._toast('Ошибка: ' + e.message, true); }
  }

  // Часы контроллера ← время HA (updateTimeZone). Пояс НЕ меняем — шлём тот, что вернул шлюз
  // (нотация у Sunricher POSIX-инвертированная, ошибка увела бы расписание на часы).
  async _syncGwTime() {
    const m = this._state.modal;
    if (!confirm('Синхронизировать часы контроллера с временем Home Assistant?\n\nЧасами пользуется и настольный DALI Center. Часовой пояс останется прежним — меняется только время.')) return;
    try {
      const r = await this._ws({ type: 'arvid_dali_center/sync_gw_time', gw_sn: m.gwSn });
      if (!r || !r.ok) { this._toast((r && r.reason) || 'Не подтверждено шлюзом', true); return; }
      this._toast('Часы синхронизированы' + (r.gwTimeSkewS != null ? ` (расхождение ${Math.round(r.gwTimeSkewS)} с)` : ''));
      await this._loadGateways();   // освежить показанное время/расхождение
      this._render();
    } catch (e) { this._toast('Ошибка: ' + e.message, true); }
  }

  // Применить сетевые настройки (setGwIpInfor). При static вне текущей подсети — предупреждаем,
  // но разрешаем (по решению пользователя). Бэкенд echo'ит EMQ-поля, чтобы не сбросить MQTT.
  async _saveGwNet() {
    const sr = this.shadowRoot, m = this._state.modal;
    const mode = (sr.getElementById('gwMode') || {}).value || 'dhcp';
    const payload = { type: 'arvid_dali_center/set_gw_net', gw_sn: m.gwSn, mode };
    if (mode === 'static') {
      const ip = ((sr.getElementById('gwIp') || {}).value || '').trim();
      const mask = ((sr.getElementById('gwMask') || {}).value || '').trim();
      const gw = ((sr.getElementById('gwGw') || {}).value || '').trim();
      if (!this._isIp4(ip) || !this._isIp4(mask) || !this._isIp4(gw)) {
        this._toast('IP, маска и шлюз должны быть корректными IPv4', true); return;
      }
      // предупреждение о смене подсети: новый IP сравниваем с текущим IP шлюза по новой маске
      const curIp = (m.cur && m.cur.ipAddr) || (this._state.gateways.find((g) => g.gwSn === m.gwSn) || {}).ip;
      let warn = `Применить СТАТИЧЕСКИЙ IP шлюза ${m.gwSn}:\n  ${ip} / ${mask}, шлюз ${gw}\n\nШлюз перенастроит сеть; связь восстановится автоматически по серийнику.`;
      if (curIp && !this._sameSubnet(ip, curIp, mask)) {
        warn += `\n\n⚠ ВНИМАНИЕ: ${ip} вне текущей подсети (${curIp}). Шлюз может ПРОПАСТЬ из обнаружения, если уйдёт в другую подсеть. Продолжать?`;
      }
      if (!confirm(warn)) return;
      payload.ipAddr = ip; payload.mask = mask; payload.defaultGateway = gw;
    } else {
      if (!confirm(`Переключить шлюз ${m.gwSn} на DHCP (авто-IP)?\n\nШлюз получит адрес от роутера; связь восстановится по серийнику.`)) return;
    }
    try {
      const r = await this._ws(payload);
      if (!(r && r.ok)) { this._toast('Не подтверждено шлюзом', true); return; }
      // Сетевые изменения применяются ТОЛЬКО после рестарта шлюза (как в DALI Center) — предлагаем
      if (confirm('Настройки приняты шлюзом. Они применятся ТОЛЬКО после перезапуска шлюза.\n\nПерезапустить шлюз сейчас? (связь восстановится автоматически по серийнику)')) {
        const rr = await this._ws({ type: 'arvid_dali_center/restart_gateway', gw_sn: m.gwSn });
        this._toast((rr && rr.ok) ? 'Перезапуск отправлен — сеть применяется' : 'Рестарт не подтверждён', !(rr && rr.ok));
      } else {
        this._toast('Настройки сохранены — примените позже рестартом шлюза');
      }
    } catch (e) { this._toast('Ошибка: ' + e.message, true); }
  }

  async _identify(dev) {
    try {
      const r = await this._ws({ type: 'arvid_dali_center/identify', gw_sn: this._state.activeGw, devType: dev.devType, channel: dev.channel, address: dev.address });
      this._toast(r.ok ? 'Identify отправлен' : 'Не подтверждено', !r.ok);
    } catch (e) { this._toast('Ошибка: ' + e.message, true); }
  }

  async _changeAddr(dev) {
    const v = prompt(`Новый DALI-адрес для ${dev.typeName} (текущий ${dev.address}):`, dev.address);
    if (v === null) return;
    const nv = parseInt(v, 10);
    if (Number.isNaN(nv)) { this._toast('Адрес должен быть числом', true); return; }
    if (!confirm(`Сменить адрес ${dev.address} → ${nv}? Это разрушающая операция.`)) return;
    try {
      const r = await this._ws({ type: 'arvid_dali_center/set_address', gw_sn: this._state.activeGw, devType: dev.devType, channel: dev.channel, address: dev.address, new: nv });
      this._toast(r.ok ? 'Адрес изменён' : 'Не подтверждено', !r.ok);
      if (r.ok) await this._loadDevices();
    } catch (e) { this._toast('Ошибка: ' + e.message, true); }
  }

  // переименование устройства — через модалку (как параметры), а не prompt()
  _rename(dev) {
    this._state.modal = { kind: 'rename', dev, name: dev.name || '' };
    this._render();
  }

  async _doRename(name) {
    const dev = (this._state.modal || {}).dev;
    if (!dev) return;
    try {
      const res = await this._ws({ type: 'arvid_dali_center/rename', gw_sn: this._state.activeGw,
        devType: dev.devType, channel: dev.channel, address: dev.address,
        devSn: dev.devSn || '', name: (name || '').trim() });
      // F8 (v1.2.20): бэкенд отклоняет ДУБЛЬ имени РЕЗУЛЬТАТОМ (ok:false), не исключением —
      // раньше карточка это глотала и говорила «Переименовано», хотя имя НЕ сохранено (пусконаладчик
      // шёл дальше, считая устройство названным). Теперь показываем отказ, модалку не закрываем.
      if (res && res.ok === false) {
        this._toast(res.error === 'duplicate' ? 'Имя занято: ' + (res.conflict || name) : 'Не переименовано', true);
        return;
      }
      this._state.modal = null;
      await this._loadDevices();   // перечитать (новые entity_id/имя), убирает «пропажу»
      this._toast('Переименовано');
    } catch (e) { this._toast('Ошибка: ' + e.message, true); }  // модал оставляем открытым
  }

  async _forget(dev) {
    const nm = dev.name || (dev.typeName + ' ' + dev.address);
    // ОСИРОТЕВШИЙ: его адрес занят ДРУГИМ устройством → предупреждаем отдельно (человек должен
    // понимать, что сносит запись прежнего жильца, а не то, что сейчас стоит на этом адресе)
    const extra = dev.orphan
      ? `\n\n⚠ Это ОСИРОТЕВШАЯ запись: её DALI-адрес ${dev.address} уже занят другим устройством. Снос затронет только прежнего жильца (devSn ${dev.devSn}), нынешнее устройство на этом адресе не пострадает.`
      : '';
    // ДАТЧИК = ОДНА ЖЕЛЕЗКА, ДВЕ РОЛИ (v1.2.56): движение (0201) и освещённость (0202) — записи
    // с общим devSn на одном адресе. Забываем их вместе, поэтому и предупреждаем вместе: раньше
    // человек снимал одну роль и не понимал, почему вторая осталась висеть.
    const pair = (String(dev.devType) === '0201' || String(dev.devType) === '0202') && !dev.orphan
      ? '\n\nℹ Это ДАТЧИК: будут забыты обе его роли — движение и освещённость (одно устройство).'
      : '';
    if (!confirm(`Забыть «${nm}»?${extra}${pair}\n\nЗапись устройства и его данные (параметры, имя) будут удалены из хранилищ, сущности снесены из реестра HA.\n\nЭто ручное необратимое действие. Если устройство вернётся на шину — появится заново (без старого имени).`)) return;
    try {
      // key — точный ключ кеша: у осиротевшего адрес совпадает с адресом НОВОГО жильца,
      // и снос по тройке (devType,channel,address) попал бы не в того (v1.2.2)
      const res = await this._ws({ type: 'arvid_dali_center/forget_device', gw_sn: this._state.activeGw,
        devType: String(dev.devType), channel: dev.channel, address: dev.address,
        ...(dev.key ? { key: dev.key } : {}) });
      await this._loadDevices();
      // v1.2.66: если серийник занят ЖИВОЙ записью другого типа (перекрёст devSn), бэкенд
      // сносит только сущности этой записи, а имя/параметры/карточку не трогает — говорим
      // об этом прямо, иначе «забыл, а имя осталось» выглядит как невыполненное действие
      this._toast(res && res.cross_live
        ? `Запись снята. Имя и данные СОХРАНЕНЫ: серийник занят живым устройством (${res.cross_live})`
        : 'Устройство забыто');
    } catch (e) { this._toast('Ошибка: ' + e.message, true); }
  }

  // переименование группы — модалка + WS rename_group (имя/entity_id light-сущности)
  _renameGroup(g) {
    if (!g) { this._toast('Группа не найдена (список обновился) — повторите', true); return; }
    this._state.modal = { kind: 'renameGroup', g, name: g.name || '' };
    this._render();
  }

  async _doRenameGroup(name) {
    const g = (this._state.modal || {}).g;
    if (!g) return;
    try {
      const res = await this._ws({ type: 'arvid_dali_center/rename_group', gw_sn: this._state.activeGw,
        channel: g.channel, groupId: g.groupId, name: (name || '').trim() });
      // F8 (v1.2.20): rename_group отдаёт ok=false, если контроллер НЕ подтвердил setGroupName
      // (ack). Имя группы — строго с контроллера → без ack следующий _loadDevices вернёт старое
      // имя, и тост «переименована» вводил в заблуждение. Показываем отказ, модалку не закрываем.
      if (res && res.ok === false) {
        this._toast('Контроллер не подтвердил переименование группы', true);
        return;
      }
      this._state.modal = null;
      await this._loadDevices();
      this._toast('Группа переименована');
    } catch (e) { this._toast('Ошибка: ' + e.message, true); }
  }

  _lampToggle(dev) {
    const ent = dev.entities && dev.entities.light;
    if (!ent) { this._toast('Нет сущности лампы', true); return; }
    this._hass.callService('light', 'toggle', { entity_id: ent });
  }

  // Группа по ИДЕНТИЧНОСТИ (`channel:groupId`). Индексы в `_state.groups` протухают при каждой
  // перерисовке (см. отрисовку групп) — адресовать ими действия нельзя.
  _groupByKey(key) {
    if (!key) return null;
    const [ch, gid] = String(key).split(':');
    return this._state.groups.find((g) => String(g.channel) === ch && String(g.groupId) === gid) || null;
  }

  _groupToggle(g) {
    if (!g) return;
    if (g.entity_id) { this._hass.callService('light', 'toggle', { entity_id: g.entity_id }); return; }
    // запасной путь (новая группа до появления сущности): writeGroup + оптимистично.
    // ключ — с каналом и шлюзом: groupId 0-15 повторяется на КАЖДОМ шлюзе (долг C2).
    const on = !this._state.groupState[g.groupId];
    this._state.groupState[g.groupId] = on; this._syncStates();
    this._ws({ type: 'arvid_dali_center/group_write', gw_sn: this._state.activeGw, channel: g.channel, groupId: g.groupId, property: [{ dpid: 20, dataType: 'bool', value: on }] }).catch((e) => this._toast('Ошибка: ' + e.message, true));
  }

  // ключ для запоминания последней яркости (лампа — по entity, группа без сущности — по id)
  _briKey(target) {
    return target.type === 'light' ? 'e:' + target.entity : 'g:' + target.g.groupId;
  }

  _openBright(target) {
    // 1) если сущность ВКЛЮЧЕНА — берём её реальную яркость;
    // 2) иначе — последнюю заданную из памяти (не сбрасываем на 100% у выключенной);
    // 3) иначе — 100% по умолчанию.
    let value = null;
    if (target.type === 'light') {
      const st = this._st(target.entity);
      if (st && st.state === 'on' && st.attributes.brightness)
        value = Math.round(st.attributes.brightness / 255 * 100);
    }
    if (value == null) {
      const remembered = this._state.lastBright[this._briKey(target)];
      if (remembered != null) value = remembered;
    }
    if (value == null) value = 100;
    this._state.modal = { kind: 'bright', target, value };
    this._render();
  }

  async _applyBright(close = true) {
    const inp = this.shadowRoot.getElementById('briRange');
    const v = inp ? parseInt(inp.value, 10) : 100;
    const t = (this._state.modal || {}).target;
    if (!t) return;
    this._state.lastBright[this._briKey(t)] = v;   // запоминаем заданное (для выключенной сущности)
    try {
      if (t.type === 'light') {
        await this._hass.callService('light', 'turn_on', { entity_id: t.entity, brightness_pct: v });
      } else {
        const dev = Math.max(1, Math.round(v / 100 * 1000));
        await this._ws({ type: 'arvid_dali_center/group_write', gw_sn: this._state.activeGw, channel: t.g.channel, groupId: t.g.groupId, property: [{ dpid: 20, dataType: 'bool', value: true }, { dpid: 22, dataType: 'uint16', value: dev }] });
        this._state.groupState[t.g.groupId] = true;
      }
      if (close) { this._state.modal = null; this._render(); }
    } catch (e) { this._toast('Ошибка: ' + e.message, true); }
  }

  // общий рендер сетки параметров: поля с опциями (fade) → выпадающий список, иначе число
  _paramGrid(fields, p) {
    return `<div class="grid">${fields.map(([key, label, opts]) => {
      const cur = p[key] ?? '';
      const ctrl = opts
        ? `<select data-param="${key}"><option value="">— не менять —</option>${opts.map(([v, t]) => `<option value="${v}"${String(cur) === String(v) ? ' selected' : ''}>${t}</option>`).join('')}</select>`
        : `<input type="number" data-param="${key}" value="${cur}" placeholder="${p[key] ?? '—'}">`;
      return `<label class="fld"><span>${label}</span>${ctrl}</label>`;
    }).join('')}</div>`;
  }

  // собрать заполненные параметры из модалки (пустые/«не менять» — пропускаем)
  _collectParams() {
    const p = {};
    this.shadowRoot.querySelectorAll('[data-param]').forEach((inp) => {
      if (inp.value !== '') p[inp.dataset.param] = parseInt(inp.value, 10);
    });
    return p;
  }

  // Автояркость (恒照, Путь A): нативный контур шлюза на датчике 0202 → группа.
  async _openLuxKeep(dev) {
    let entries = [], enable = null, mode = {};
    try {
      const r = await this._ws({ type: 'arvid_dali_center/read_lux_keep', gw_sn: this._state.activeGw, devType: dev.devType, channel: dev.channel, address: dev.address });
      entries = r.entries || []; enable = r.enable; mode = r.mode || {};
    } catch (e) { /* покажем «—» */ }
    // окна работы (v1.2.25) — из записи АВТОЯРКОСТИ (dpid 3); правятся строками в модалке
    const e3 = entries.find((e) => e.dpid === 3) || {};
    // ЦЕЛИ автояркости = обычные группы шлюза + КРОСС-ГРУППЫ (v1.2.53). Раньше кросс-группу
    // нельзя было выбрать вовсе — список строился только из групп активного контроллера.
    // Форма записи подтверждена захватом трёх шлюзов 2026-08-07: по одной цели 0401 на
    // каждого участника, дальше фан-аут раскладывает конфигурацию по шлюзам.
    const xg = (this._state.xgroups || []).map((x) => ({
      name: x.name, channel: x.channel, groupId: x.groupId, uid: x.uid, cross: true,
      parts: (x.participants || []).length }));
    this._state.modal = { kind: 'luxKeep', dev, groups: [...(this._state.groups || []), ...xg],
      entries, enable: enable !== false, mode, windows: (e3.windows || []).slice() };
    this._render();
  }

  // Окна работы: «+» добавляет строку, «×» убирает. Хранится в модалке, пишется при сохранении.
  _lkAddWindow() {
    const m = this._state.modal; if (!m || m.kind !== 'luxKeep') return;
    this._lkCollectWindows();
    m.windows.push('08:00-17:30');
    this._render();
  }

  _lkDelWindow(i) {
    const m = this._state.modal; if (!m || m.kind !== 'luxKeep') return;
    this._lkCollectWindows();
    m.windows.splice(i, 1);
    this._render();
  }

  // Считать введённые окна из полей в состояние (перед перерисовкой — иначе ввод потеряется)
  _lkCollectWindows() {
    const m = this._state.modal, sr = this.shadowRoot;
    m.windows = (m.windows || []).map((_w, i) => {
      const a = (sr.getElementById('lkWinA' + i) || {}).value || '';
      const b = (sr.getElementById('lkWinB' + i) || {}).value || '';
      return (a && b) ? (a + '-' + b) : '';
    }).filter(Boolean);
    return m.windows;
  }

  // Тумблер «Активна» — МЯГКОЕ вкл/выкл (setSensorOnOff): привязка на контроллере ЦЕЛА.
  // Это НЕ «Очистить» (delSensor) — там настройка сносится совсем.
  async _lkToggleEnabled(on) {
    const m = this._state.modal;
    try {
      const r = await this._ws({ type: 'arvid_dali_center/set_sensor_enabled', gw_sn: this._state.activeGw, devType: m.dev.devType, channel: m.dev.channel, address: m.dev.address, value: on });
      if (!r || !r.ok) { this._toast((r && r.reason) || 'Не подтверждено шлюзом', true); return; }
      m.enable = on;
      this._toast(on ? 'Автояркость активна' : 'Автояркость приостановлена (настройка сохранена)');
      this._render();
    } catch (e) { this._toast('Ошибка: ' + e.message, true); }
  }

  async _saveLuxKeep() {
    const m = this._state.modal, sr = this.shadowRoot;
    const gi = (sr.getElementById('lkGroup') || {}).value;
    if (gi === '' || gi == null) { this._toast('Нет группы — создай группу-цель', true); return; }
    const g = m.groups[+gi];
    const target = parseInt((sr.getElementById('lkTarget') || {}).value, 10);
    const tol = parseInt((sr.getElementById('lkTol') || {}).value, 10);
    if (!(target >= 0)) { this._toast('Задай целевой lux', true); return; }
    // режим сосуществования с ручным управлением (v1.2.23, ТЕСТОВОЕ — DALI Center его не шлёт)
    const modeType = ((sr.getElementById('lkMode') || {}).value) || '';
    const timeValue = parseInt((sr.getElementById('lkModeTime') || {}).value, 10);
    const windows = this._lkCollectWindows();
    try {
      const payload = { type: 'arvid_dali_center/set_lux_keep', gw_sn: this._state.activeGw, devType: m.dev.devType, channel: m.dev.channel, address: m.dev.address, group: { channel: g.channel, groupId: g.groupId }, target, tol: tol >= 0 ? tol : 10 };
      if (g.cross && g.uid) payload.xgroup_uid = g.uid;   // цель на КАЖДОГО участника (v1.2.53)
      if (modeType) { payload.modeType = modeType; payload.timeValue = (modeType === 'auto' && timeValue >= 0) ? timeValue : -1; }
      const r = await this._ws(payload);
      if (!r || !r.ok) { this._toast((r && r.reason) || 'Не подтверждено — см. журнал', true); return; }
      // ОКНА пишем ОТДЕЛЬНОЙ командой и ПОСЛЕ привязки: addSensorObj перезаписывает конфигурацию
      // функции целиком, поэтому расписание кладётся поверх уже записанных целей (иначе снесёт).
      let extra = '';
      if (windows.length) {
        const rs = await this._ws({ type: 'arvid_dali_center/set_sensor_schedule', gw_sn: this._state.activeGw, devType: m.dev.devType, channel: m.dev.channel, address: m.dev.address, dpid: 3, windows });
        if (!rs || !rs.ok) { this._toast('Автояркость записана, но РАСПИСАНИЕ не легло: ' + ((rs && rs.error) || 'нет подтверждения'), true); this._state.modal = null; this._render(); return; }
        extra = ' + окна (' + windows.join(', ') + ')';
        if (rs.warnings && rs.warnings.length) extra += ' ⚠ ' + rs.warnings.join('; ');
      }
      this._toast('Автояркость включена' + extra + ' — проверь на железе');
      this._state.modal = null; this._render();
    } catch (e) { this._toast('Ошибка: ' + e.message, true); }
  }

  async _clearLuxKeep() {
    const m = this._state.modal;
    // ⚠ Это СНОС настройки (delSensor), а не пауза. Приостановить — тумблером «Активна».
    if (!confirm('ОЧИСТИТЬ настройку автояркости?\n\nС датчика будет снята вся конфигурация (цель, коридор lux, окна работы). Это НЕ пауза — чтобы просто приостановить, выключите тумблер «Активна».')) return;
    try {
      const r = await this._ws({ type: 'arvid_dali_center/clear_lux_keep', gw_sn: this._state.activeGw, devType: m.dev.devType, channel: m.dev.channel, address: m.dev.address });
      this._toast(r.ok ? 'Автояркость выключена' : 'Не подтверждено — см. журнал', !r.ok);
      this._state.modal = null; this._render();
    } catch (e) { this._toast('Ошибка: ' + e.message, true); }
  }

  // Поворот → яркость (логика в HA). Цель — лампа/группа; шаг — % яркости на «щелчок».
  async _openRotaryBind(dev) {
    let binding = null;
    try {
      const r = await this._ws({ type: 'arvid_dali_center/get_rotary_binding', gw_sn: this._state.activeGw, devType: dev.devType, channel: dev.channel, address: dev.address });
      binding = r.binding || null;
    } catch (e) { /* покажем «нет» */ }
    const lamps = this._state.devices.filter((d) => LIGHT_T.includes(String(d.devType)));
    this._state.modal = { kind: 'rotaryBind', dev, lamps, groups: this._state.groups || [], binding };
    this._render();
  }

  async _saveRotaryBind() {
    const m = this._state.modal, sr = this.shadowRoot;
    const sel = (sr.getElementById('roTarget') || {}).value || '';
    const stepPct = parseInt((sr.getElementById('roStep') || {}).value, 10);
    const throttle = Math.max(0.7, parseFloat((sr.getElementById('roThrottle') || {}).value) || 0.8);   // сек, пол 0.7 (fade)
    if (!sel) { this._toast('Выбери цель', true); return; }
    const [kind, ix] = sel.split(':');
    let target;
    if (kind === 'lamp') { const d = m.lamps[+ix]; target = { devType: d.devType, channel: d.channel, address: d.address }; }
    else { const g = m.groups[+ix]; target = { devType: '0401', channel: g.channel, address: g.groupId }; }
    const step = Math.max(1, Math.round((stepPct > 0 ? stepPct : 2) / 100 * 1000));   // % → шина 0..1000
    try {
      const r = await this._ws({ type: 'arvid_dali_center/set_rotary_binding', gw_sn: this._state.activeGw, devType: m.dev.devType, channel: m.dev.channel, address: m.dev.address, target, step, throttle });
      this._toast(r.ok ? ('Привязка поворота сохранена' + (r.nativeCleared ? ' (битая нативная снята)' : '')) : 'Не подтверждено', !r.ok);
      this._state.modal = null;
      if (this._state.view === 'panelBind') this._loadPanelBindings(); else this._render();
    } catch (e) { this._toast('Ошибка: ' + e.message, true); }
  }

  async _clearRotaryBind() {
    const dev = (this._state.panelBind || {}).dev;
    if (!dev) return;
    if (!confirm('Снять привязку поворота → яркость?')) return;
    try {
      await this._ws({ type: 'arvid_dali_center/clear_rotary_binding', gw_sn: this._state.activeGw, devType: dev.devType, channel: dev.channel, address: dev.address });
      this._toast('Привязка снята');
      this._loadPanelBindings();
    } catch (e) { this._toast('Ошибка: ' + e.message, true); }
  }

  // Массовые действия над датчиками — ЧЕРЕЗ СЕРВИСЫ HA (v1.2.27), а не своей копией логики:
  // `arvid_dali_center.set_autobrightness` / `set_effective_time`. Одна реализация на карточку,
  // автоматизации и внешние вызовы (профилактика «фикса, доехавшего до одного вызывающего»).
  // Цель — либо ОТМЕЧЕННЫЕ датчики (по их entity_id), либо ОБЛАСТЬ HA (там сервис сам раскроет
  // область в устройства — так работает и «этаж/объект целиком»).
  _mpSensorActions(m) {
    const areas = m.areas || Object.values((this._hass && this._hass.areas) || {});
    const aopts = areas.map((a) => `<option value="${this._esc(a.area_id)}">${this._esc(a.name || a.area_id)}</option>`).join('');
    const areaNote = m.areas === null ? '<div class="muted">Загружаю список областей…</div>'
      : (!areas.length ? `<div class="muted">⚠ Областей в HA нет${m.areasError ? ' (' + this._esc(m.areasError) + ')' : ''} — адресуйтесь галочками выше.</div>` : '');
    const wins = (m.mpWindows || []).map((w, i) => {
      const [a, b] = String(w).split('-');
      return `<div class="lk-win"><input id="mpWinA${i}" type="time" value="${this._esc(a || '')}"><span>—</span><input id="mpWinB${i}" type="time" value="${this._esc(b || '')}"><button class="mini danger" data-act="mpDelWin" data-idx="${i}" title="Убрать окно">×</button></div>`;
    }).join('') || '<div class="muted">окна не заданы</div>';
    return `<div class="chk-h">Массовые действия<button class="mini" data-act="mpAddWin" title="Добавить окно" style="float:right">+</button></div>
      <div class="grid">
        <label class="fld fld-wide"><span>Цель</span><select id="mpTarget">
          <option value="">отмеченные датчики выше</option>${aopts}</select></label>
        ${areaNote}
      </div>
      <div class="mp-acts">
        <button class="btn ghost" data-act="mpAutoOn">Автояркость ВКЛ</button>
        <button class="btn ghost" data-act="mpAutoOff">Автояркость ВЫКЛ</button>
      </div>
      <div class="chk-h" style="margin-top:10px">Окна работы</div>
      <div class="grid">
        <label class="fld fld-wide"><span>Функция (для окон)</span><select id="mpFunc">
          <option value="autobrightness">автояркость</option>
          <option value="motion">движение</option>
          <option value="both">обе</option></select></label>
      </div>
      <div class="lk-wins" style="margin-top:6px">${wins}</div>
      <div class="mp-acts">
        <button class="btn ghost" data-act="mpSetWins">Задать окна</button>
        <button class="btn ghost" data-act="mpClearWins">Снять окна (круглосуточно)</button>
      </div>
      <div class="muted" style="margin-top:4px">Окна — только внутри суток (ночь = двумя окнами). Массовая запись грузит шину — идёт с паузой, дождитесь отчёта.<br>⚠ Выбор ОБЛАСТИ действует на её датчики (в т.ч. на других контроллерах), но только если область НАЗНАЧЕНА устройству в HA. Автоматически мы её не проставляем — если область у датчиков не задана, отчёт покажет 0 из 0; тогда выбирайте датчики галочками.</div>`;
  }

  // цель для сервиса: область (если выбрана) либо entity_id отмеченных датчиков
  _mpServiceTarget() {
    const sr = this.shadowRoot;
    const area = (sr.getElementById('mpTarget') || {}).value || '';
    if (area) return { area_id: area };
    const sel = this._mpChecked(this._mpDevs('sensor'));
    const eids = sel.flatMap((d) => Object.values((d && d.entities) || {})).filter(Boolean);
    return eids.length ? { entity_id: eids } : null;
  }

  _mpCollectWindows() {
    const m = this._state.modal, sr = this.shadowRoot;
    m.mpWindows = (m.mpWindows || []).map((_w, i) => {
      const a = (sr.getElementById('mpWinA' + i) || {}).value || '';
      const b = (sr.getElementById('mpWinB' + i) || {}).value || '';
      return (a && b) ? (a + '-' + b) : '';
    }).filter(Boolean);
    return m.mpWindows;
  }

  _mpAddWindow() { const m = this._state.modal; this._mpCollectWindows(); (m.mpWindows = m.mpWindows || []).push('08:00-17:30'); this._render(); }
  _mpDelWindow(i) { const m = this._state.modal; this._mpCollectWindows(); m.mpWindows.splice(i, 1); this._render(); }

  // единый вызов сервиса + разбор отчёта (сервисы возвращают response: changed/total/warnings)
  async _mpCallService(service, data) {
    const target = this._mpServiceTarget();
    if (!target) { this._toast('Отметь датчики или выбери область', true); return; }
    try {
      // return_response — ОДНОЙ попыткой, без ретрая: повтор при неудаче означал бы ВТОРУЮ
      // массовую запись на шину (сервисы идемпотентны, но шину это кладёт). Сервисы объявлены
      // SupportsResponse.OPTIONAL, поэтому отчёт приходит на актуальных версиях HA.
      const resp = await this._hass.callService('arvid_dali_center', service, data, target, false, true);
      const r = resp && (resp.response || resp);
      if (r && r.total != null) {
        const warn = (r.warnings || []).join('; ');
        this._toast(`Применено: ${r.changed}/${r.total} датчиков${warn ? ' ⚠ ' + warn : ''}`,
          r.changed < r.total || !!warn);
      } else this._toast('Отправлено (отчёт недоступен) — проверьте журнал');
    } catch (e) { this._toast('Ошибка: ' + e.message, true); }
  }

  async _mpAutoBright(enabled) { await this._mpCallService('set_autobrightness', { enabled }); }

  async _mpSetWindows(clear) {
    const sr = this.shadowRoot;
    const func = (sr.getElementById('mpFunc') || {}).value || 'autobrightness';
    const windows = clear ? [] : this._mpCollectWindows();
    if (!clear && !windows.length) { this._toast('Добавь хотя бы одно окно («+»)', true); return; }
    if (clear && !confirm('Снять расписание — датчики будут работать КРУГЛОСУТОЧНО. Продолжить?')) return;
    await this._mpCallService('set_effective_time', { function: func, windows, clear, pace: 0.3 });
  }

  // массовая настройка: мульти-выбор по классу (кнопка в шапке). cls: lamp/sensor/panel
  _openMultiParam() {
    this._state.modal = { kind: 'multiParam', cls: 'lamp', areas: null };
    this._render();
    this._loadAreas();
  }

  // Список областей HA — из РЕЕСТРА по WS (v1.2.29). Раньше брали `hass.areas`, но это поле есть
  // не во всех версиях фронтенда → выбор области в карточке был пуст. `config/area_registry/list`
  // — тот же путь, которым пользуется скрипт автопусконаладки, работает везде.
  async _loadAreas() {
    const m = this._state.modal;
    try {
      const list = await this._ws({ type: 'config/area_registry/list' });
      const areas = (list || []).map((a) => ({ area_id: a.area_id, name: a.name || a.area_id }));
      areas.sort((x, y) => String(x.name).localeCompare(String(y.name), 'ru'));
      if (this._state.modal === m) { m.areas = areas; this._render(); }
    } catch (e) {
      if (this._state.modal === m) { m.areas = []; m.areasError = e.message; this._render(); }
    }
  }

  // устройства активного шлюза по классу мульти-настройки (тот же порядок в рендере и сейве)
  _mpDevs(cls) {
    const types = cls === 'sensor' ? SENSOR_T : cls === 'panel' ? PANEL_T : LIGHT_T;
    return this._state.devices.filter((d) => types.includes(String(d.devType)));
  }

  // отмеченные устройства мульти-модалки
  _mpChecked(devs) {
    return [...this.shadowRoot.querySelectorAll('[data-mp]')].filter((c) => c.checked).map((c) => devs[+c.dataset.mp]);
  }

  async _saveMultiParam() {
    const cls = this._state.modal.cls || 'lamp';
    // ЛАМПЫ, область «весь контроллер» — броадкаст, список галочек не участвует (v1.2.44)
    if (cls === 'lamp' && (this._state.modal.scope || 'targets') === 'gateway') {
      return this._saveMultiParamBroadcast();
    }
    const devs = this._mpDevs(cls);
    const sel = this._mpChecked(devs);
    if (!sel.length) { this._toast('Отметь хотя бы одно устройство', true); return; }
    if (cls === 'sensor') return this._saveMultiSensor(sel);
    if (cls === 'panel') return this._saveMultiPanel(sel);
    // лампы — один батч setDevParam
    const p = this._collectParams();
    if (!Object.keys(p).length) { this._toast('Задай хотя бы один параметр', true); return; }
    const targets = sel.map((d) => ({ devType: d.devType, channel: d.channel, address: d.address }));
    try {
      const r = await this._ws({ type: 'arvid_dali_center/set_param_bulk', gw_sn: this._state.activeGw, targets, paramer: p });
      this._toast(r.ok ? `Применено к ${r.count} лампам` : `Отправлено (${r.count}) — без ack`, !r.ok);
      this._state.modal = null; this._render();
    } catch (e) { this._toast('Ошибка: ' + e.message, true); }
  }

  // ── весь свет контроллера (броадкаст) ────────────────────────────────────────
  // Сущность создаёт платформа light (DaliAllLights); entity_id приходит в списке шлюзов
  // (резолв по unique_id — имя могло получить суффикс при коллизии).
  async _allLights(on) {
    const gw = (this._state.gateways || []).find((g) => g.gwSn === this._state.activeGw);
    const eid = gw && gw.allLights;
    if (!eid) { this._toast('Сущность «все лампы» не найдена — перезапустите HA', true); return; }
    try {
      await this._hass.callService('light', on ? 'turn_on' : 'turn_off', { entity_id: eid });
      this._toast(on ? 'Весь свет контроллера включён' : 'Весь свет контроллера выключен');
    } catch (e) { this._toast('Ошибка: ' + e.message, true); }
  }

  // ── реестр HA: пустые карточки устройств ─────────────────────────────────────
  // Показать → выбрать → снять. Авто-чистки нет намеренно (проблемы должны быть ВИДНЫ),
  // поэтому решение принимает человек, а живые устройства бэкенд снести не даст.
  async _openRegistry() {
    this._state.modal = { kind: 'registry', loading: true, orphans: [] };
    this._render();
    try {
      const r = await this._ws({ type: 'arvid_dali_center/registry_orphans', gw_sn: this._state.activeGw });
      const m = this._state.modal;
      if (m && m.kind === 'registry') { m.loading = false; m.orphans = r.orphans || []; this._render(); }
    } catch (e) {
      const m = this._state.modal;
      if (m && m.kind === 'registry') { m.loading = false; m.error = e.message; this._render(); }
    }
  }

  async _cleanRegistry() {
    const m = this._state.modal || {};
    const ids = [...this.shadowRoot.querySelectorAll('[data-orph]')]
      .filter((c) => c.checked).map((c) => c.dataset.orph);
    if (!ids.length) { this._toast('Отметь, что снимать', true); return; }
    try {
      const r = await this._ws({ type: 'arvid_dali_center/registry_cleanup', gw_sn: this._state.activeGw, device_ids: ids });
      const skipped = (r.skipped || []).length;
      this._toast(`Снято карточек: ${(r.removed || []).length}` + (skipped ? ` · пропущено ${skipped} (ожили)` : ''), !!skipped);
      this._openRegistry();     // перечитать список
    } catch (e) { this._toast('Ошибка: ' + e.message, true); }
  }

  // Перечитать кривые мощности из /config/arvid_curves/curves.yaml (v1.2.67). Таблицу
  // «яркость → ватты» пусконаладчик снимает на объекте и правит файл в File Editor —
  // ждать рестарт ядра ради этого незачем. Проблемы разбора показываем прямо, а не в лог:
  // кривая с опечаткой = неверный энергоучёт по целому типу светильников.
  async _reloadCurves() {
    try {
      const r = await this._ws({ type: 'arvid_dali_center/curves_reload' });
      if (this._state.energyPage) this._state.energyPage.curves = r.curves || [];
      this._render();
      const probs = r.problems || [];
      this._toast(probs.length
        ? `Загружено ${r.loaded}, замечаний ${probs.length}: ${probs[0]}`
        : `Кривых из файла: ${r.loaded}`, probs.length > 0);
      if (probs.length) console.warn('[curves] замечания:', probs);
    } catch (e) { this._toast('Ошибка: ' + e.message, true); }
  }

  // ── КОРЗИНА реестров HA (v1.2.60) ────────────────────────────────────────────
  // Показать → вымести. HA при удалении не стирает запись, а прячет её и возвращает вместе с
  // entity_id, областью и ярлыками, когда снова появится тот же unique_id. Причём БЕССРОЧНО:
  // штатная уборка (30 дней) касается лишь записей, оставшихся без записи интеграции.
  // Отсюда «вечно всплывающие бывшие»: чужой entity_id у новой группы, старые области.
  async _openTrash() {
    this._state.modal = { kind: 'trash', loading: true, items: [] };
    this._render();
    try {
      const r = await this._ws({ type: 'arvid_dali_center/registry_trash' });   // БЕЗ purge — только показ
      const m = this._state.modal;
      if (m && m.kind === 'trash') {
        m.loading = false; m.items = r.entities || []; m.devices = r.devices || [];
        m.forever = r.forever || 0; this._render();
      }
    } catch (e) {
      const m = this._state.modal;
      if (m && m.kind === 'trash') { m.loading = false; m.error = e.message; this._render(); }
    }
  }

  async _purgeTrash() {
    const m = this._state.modal || {};
    const n = (m.items || []).length;
    const nd = (m.devices || []).length;
    if (!confirm(`Вымести из корзины HA ${n} сущност(ей) и ${nd} карточек устройств нашей интеграции?\n\n`
      + `Это записи УЖЕ УДАЛЁННЫХ сущностей и карточек — живое не затрагивается.\n`
      + `После этого вернувшееся устройство придёт как новое: без старого entity_id, `
      + `области и ярлыков.`)) return;
    try {
      const r = await this._ws({ type: 'arvid_dali_center/registry_trash', purge: true });
      const p = r.purged || {};
      this._toast(`Вымыто: сущностей ${(p.entities || []).length}, карточек ${(p.devices || []).length}`);
      this._openTrash();       // перечитать — должно остаться пусто
    } catch (e) { this._toast('Ошибка: ' + e.message, true); }
  }

  // Броадкаст параметров всему контроллеру (setDevParam devType FFFF). Одна команда вместо
  // перебора ламп: адресный батч пишет NVM каждого драйвера по очереди и на полном шлюзе
  // упирается в таймаут. count в ответе — сколько ИЗВЕСТНЫХ ламп обновлено в нашем сторе
  // (физически команду получают все на шине, в т.ч. не сканированные).
  async _saveMultiParamBroadcast() {
    const p = this._collectParams();
    if (!Object.keys(p).length) { this._toast('Задай хотя бы один параметр', true); return; }
    try {
      const r = await this._ws({ type: 'arvid_dali_center/set_param_bulk', gw_sn: this._state.activeGw, scope: 'gateway', paramer: p });
      this._toast(r.ok
        ? `Отправлено всему контроллеру (в карточке обновлено ламп: ${r.count})`
        : `Не подтверждено${r.reason ? ' — ' + r.reason : ''}`, !r.ok);
      this._state.modal = null; this._render();
    } catch (e) { this._toast('Ошибка: ' + e.message, true); }
  }

  // датчики: setSensorArgv по одному (массива в протоколе нет); читаем текущее и мержим,
  // чтобы не обнулить незаданные поля.
  async _saveMultiSensor(sel) {
    const p = this._collectParams();
    if (!Object.keys(p).length) { this._toast('Задай хотя бы один параметр', true); return; }
    let ok = 0;
    const motionOnly = SENSOR_FIELDS_MOTION.map(([k]) => k);
    for (const d of sel) {
      try {
        // 0202 (освещённость) не имеет зоны/чувствительности/времени присутствия — не шлём их туда
        const pd = String(d.devType) === '0202'
          ? Object.fromEntries(Object.entries(p).filter(([k]) => !motionOnly.includes(k))) : p;
        if (!Object.keys(pd).length) continue;
        const cur = await this._ws({ type: 'arvid_dali_center/get_sensor_param', gw_sn: this._state.activeGw, devType: d.devType, channel: d.channel, address: d.address });
        const data = { ...(cur.data || {}), ...pd };
        const r = await this._ws({ type: 'arvid_dali_center/set_sensor_param', gw_sn: this._state.activeGw, devType: d.devType, channel: d.channel, address: d.address, data });
        if (r.ok) ok++;
      } catch (e) { /* следующий датчик */ }
    }
    this._toast(`Применено к ${ok}/${sel.length} датчикам`, ok < sel.length);
    this._state.modal = null; this._render();
  }

  // панели: одна привязка (кнопка×жест → цель+действие) на ВСЕ выбранные панели (addPanelObj по каждой)
  async _saveMultiPanel(sel) {
    const sr = this.shadowRoot;
    const keyNo = parseInt((sr.getElementById('mpKey') || {}).value, 10);
    const dpid = parseInt((sr.getElementById('mpDpid') || {}).value, 10);
    const tgt = (sr.getElementById('bindTarget') || {}).value || '';
    if (!tgt) { this._toast('Выбери цель', true); return; }
    const action = (sr.getElementById('bindAction') || {}).value || 'on';
    const bri = parseInt((sr.getElementById('bindBri') || {}).value, 10);
    const replace = (sr.getElementById('mpReplace') || {}).checked;
    const [kind, ix] = tgt.split(':');
    const prop = this._actionProp(action, bri);
    // цель берём из списка ВЫБРАННОГО контроллера (m.lamps/m.groups), шлюз цели — в gwSnObj
    const m = this._state.modal || {};
    const tgw = m.targetGw || this._state.activeGw;
    const lamps = m.lamps || [];
    const groups = m.groups || [];
    // кросс-группа → по одной цели на КАЖДОГО участника (v1.2.54), как в одиночной привязке
    let outObjs;
    if (kind === 'lamp') { const d = lamps[+ix]; if (!d) { this._toast('Цель не найдена — перевыбери', true); return; } outObjs = [{ gwSnObj: tgw, devType: d.devType, channel: d.channel, address: d.address, property: prop }]; }
    else if (kind === 'xgroup') {
      const x = (m.xgroups || [])[+ix];
      if (!x || !(x.participants || []).length) { this._toast('Кросс-группа не найдена или без участников', true); return; }
      outObjs = x.participants.map((part) => ({ gwSnObj: part, devType: '0401', channel: x.channel, address: x.groupId, property: prop }));
    }
    else { const g = groups[+ix]; if (!g) { this._toast('Цель не найдена — перевыбери', true); return; } outObjs = [{ gwSnObj: tgw, devType: '0401', channel: g.channel, address: g.groupId, property: prop }]; }
    let ok = 0;
    const warnSet = new Set();   // v1.2.39: недоступный контроллер ЦЕЛИ должен быть виден
    for (const pn of sel) {
      try {
        const r = await this._ws({ type: 'arvid_dali_center/add_panel_obj', gw_sn: this._state.activeGw, devType: pn.devType, channel: pn.channel, address: pn.address, keyNo, dpid, panelType: 2, mode: 255, replace, outObj: outObjs });
        if (r.ok && (!r.verify || r.verify.match)) ok++;
        (r.warnings || []).forEach((w) => warnSet.add(w));
      } catch (e) { /* следующая панель */ }
    }
    const warn = [...warnSet].join('; ');
    this._toast(`Привязка задана ${ok}/${sel.length} панелям` + (warn ? ' ⚠ ' + warn : ''), ok < sel.length || !!warn);
    this._state.modal = null; this._render();
  }

  // параметры всем лампам группы (кнопка в строке группы; состав резолвит бэкенд через readGroup)
  async _openGroupParam(g) {
    if (!g) return;
    // предзаполнить ранее заданными группе параметрами (GroupParamStore) — раньше диалог
    // открывался пустым, значения «терялись» (загадка)
    let paramer = {};
    try {
      const r = await this._ws({ type: 'arvid_dali_center/get_group_param', gw_sn: this._state.activeGw, channel: g.channel, groupId: g.groupId });
      paramer = r.paramer || {};
    } catch (e) { /* нет сохранённого — покажем пусто */ }
    this._state.modal = { kind: 'groupParam', g, paramer };
    this._render();
  }

  async _saveGroupParam() {
    const g = this._state.modal.g;
    const p = this._collectParams();
    if (!Object.keys(p).length) { this._toast('Задай хотя бы один параметр', true); return; }
    try {
      const r = await this._ws({ type: 'arvid_dali_center/set_param_bulk', gw_sn: this._state.activeGw, group: { channel: g.channel, groupId: g.groupId }, paramer: p });
      this._toast(r.ok ? `Применено к ${r.count} лампам группы` : `Отправлено (${r.count}) — без ack`, !r.ok);
      this._state.modal = null; this._render();
    } catch (e) { this._toast('Ошибка: ' + e.message, true); }
  }

  async _openParams(dev) {
    const isLight = LIGHT_T.includes(String(dev.devType));
    let paramer = {};
    try {
      if (isLight) {
        const r = await this._ws({ type: 'arvid_dali_center/get_param', gw_sn: this._state.activeGw, devType: dev.devType, channel: dev.channel, address: dev.address });
        paramer = r.paramer || {};
      } else {
        const r = await this._ws({ type: 'arvid_dali_center/get_sensor_param', gw_sn: this._state.activeGw, devType: dev.devType, channel: dev.channel, address: dev.address });
        paramer = r.data || {};
      }
    } catch (e) { this._toast('Ошибка чтения параметров: ' + e.message, true); }
    this._state.modal = { kind: isLight ? 'lampParam' : 'sensorParam', dev, paramer };
    this._render();
  }

  async _saveModal() {
    const m = this._state.modal;
    if (!m) return;
    if (m.kind === 'createGroup') return this._saveCreateGroup();
    if (m.kind === 'editGroup') return this._saveEditGroup();
    if (m.kind === 'panelBind') return this._saveBinding();
    if (m.kind === 'sensorBind') return this._saveSensorBinding();
    if (m.kind === 'energyParams') return this._saveEnergyParams();
    if (m.kind === 'healthThresholds') return this._saveHealthThresholds();
    if (m.kind === 'multiParam') return this._saveMultiParam();
    if (m.kind === 'groupParam') return this._saveGroupParam();
    if (m.kind === 'xgroup') {
      // собрать состояние формы ДО отправки: имя, номер и отметки ламп по шлюзам
      m.name = (this.shadowRoot.getElementById('xgrpName') || {}).value || m.name;
      const sel = this.shadowRoot.getElementById('xgrpId');
      if (sel && sel.value !== '') m.groupId = +sel.value;
      m.checked = new Set([...this.shadowRoot.querySelectorAll('[data-xmember]:checked')]
        .map((cb) => cb.dataset.xmember));
      return this._saveXGroup();
    }
    if (m.kind === 'luxKeep') return this._saveLuxKeep();
    if (m.kind === 'rotaryBind') return this._saveRotaryBind();
    if (m.kind === 'bright') return this._applyBright();
    if (m.kind === 'rename') return this._doRename((this.shadowRoot.getElementById('renameInput') || {}).value);
    if (m.kind === 'renameGroup') return this._doRenameGroup((this.shadowRoot.getElementById('renameInput') || {}).value);
    const p = {};
    this.shadowRoot.querySelectorAll('[data-param]').forEach((inp) => {
      if (inp.value !== '') p[inp.dataset.param] = parseInt(inp.value, 10);
    });
    if (!Object.keys(p).length) { this._toast('Заполни хотя бы один параметр', true); return; }
    try {
      if (m.kind === 'lampParam') {
        const r = await this._ws({ type: 'arvid_dali_center/set_param', gw_sn: this._state.activeGw, devType: m.dev.devType, channel: m.dev.channel, address: m.dev.address, paramer: p });
        this._toast(r.ok ? 'Параметры применены' : 'Сохранено', false);
      } else {
        const data = { ...m.paramer, ...p };
        const r = await this._ws({ type: 'arvid_dali_center/set_sensor_param', gw_sn: this._state.activeGw, devType: m.dev.devType, channel: m.dev.channel, address: m.dev.address, data });
        this._toast(r.ok ? 'Параметры применены' : 'Отправлено (без ack)', false);
      }
      this._state.modal = null; this._render();
    } catch (e) { this._toast('Ошибка: ' + e.message, true); }  // модал оставляем открытым
  }

  // сравнение ламп по отображаемому имени с ЧИСЛОВОЙ сортировкой (l_1_1_2 < l_1_1_3 <
  // l_1_1_10, а не лексикографически) — состав групп на объекте это 20+ ламп подряд.
  _cmpByName(a, b) {
    const na = a.name || (a.typeName + ' ' + a.address);
    const nb = b.name || (b.typeName + ' ' + b.address);
    return na.localeCompare(nb, undefined, { numeric: true, sensitivity: 'base' });
  }

  _openCreateGroup() {
    // сортируем сам массив lights (а не только рендер): чекбоксы адресуют m.lights[i] по
    // индексу при сохранении, поэтому порядок в state и в UI должен совпадать
    const lights = this._state.devices.filter((d) => LIGHT_T.includes(String(d.devType)))
      .sort((a, b) => this._cmpByName(a, b));
    const used = new Set(this._state.groups.map((g) => g.groupId));
    // DALI-групп физически 16 (0–15). Ищем первый свободный; все заняты → предупреждаем и не
    // открываем диалог (раньше gid доходил до 16 и уходил на шину — бэкенд теперь это отвергает).
    let gid = 0; while (gid < 16 && used.has(gid)) gid++;
    if (gid > 15) { this._toast('Все 16 DALI-групп заняты (0–15). Удалите ненужную.', true); return; }
    this._state.modal = { kind: 'createGroup', name: '', groupId: gid, lights };
    this._render();
  }

  async _saveCreateGroup() {
    const m = this._state.modal;
    const name = (this.shadowRoot.getElementById('grpName') || {}).value || '';
    const gid = parseInt((this.shadowRoot.getElementById('grpId') || {}).value, 10);
    if (Number.isNaN(gid)) { this._toast('Укажи номер группы', true); return; }
    if (gid < 0 || gid > 15) { this._toast('Номер DALI-группы — 0…15', true); return; }
    const members = [];
    this.shadowRoot.querySelectorAll('[data-member]:checked').forEach((cb) => {
      const d = m.lights[+cb.dataset.member];
      members.push({ devType: d.devType, channel: d.channel, address: d.address });
    });
    if (!members.length) { this._toast('Выбери хотя бы одну лампу', true); return; }
    try {
      const r = await this._ws({ type: 'arvid_dali_center/create_group', gw_sn: this._state.activeGw, channel: members[0].channel, groupId: gid, name, members });
      this._groupVerifyToast(r, 'Группа создана');
      this._state.modal = null; await this._loadDevices();
    } catch (e) { this._toast('Ошибка: ' + e.message, true); }
  }

  // открыть редактор состава: чекбоксы ламп, текущие члены — отмечены
  _openEditGroup(g) {
    if (!g) { this._toast('Группа не найдена (список обновился) — повторите', true); return; }
    const lights = this._state.devices.filter((d) => LIGHT_T.includes(String(d.devType)))
      .sort((a, b) => this._cmpByName(a, b));
    const checked = new Set((g.members || []).map((m) => `${m.channel}/${m.address}`));
    this._state.modal = { kind: 'editGroup', name: g.name || '', groupId: g.groupId, channel: g.channel, lights, checked };
    this._render();
  }

  // сохранить состав: пересоздать группу с отмеченными лампами (delGroup+addGroup на бэке)
  async _saveEditGroup() {
    const m = this._state.modal;
    const name = (this.shadowRoot.getElementById('grpName') || {}).value || m.name || '';
    const members = [];
    this.shadowRoot.querySelectorAll('[data-member]:checked').forEach((cb) => {
      const d = m.lights[+cb.dataset.member];
      members.push({ devType: d.devType, channel: d.channel, address: d.address });
    });
    if (!members.length) { this._toast('Выбери хотя бы одну лампу (пустую группу нельзя — удали её)', true); return; }
    try {
      const r = await this._ws({ type: 'arvid_dali_center/set_group_members', gw_sn: this._state.activeGw, channel: m.channel, groupId: m.groupId, name, members });
      this._groupVerifyToast(r, 'Состав обновлён');
      this._state.modal = null; await this._loadDevices();
    } catch (e) { this._toast('Ошибка: ' + e.message, true); }
  }

  // единый тост по результату create/set_members: показать расхождение состава
  _groupVerifyToast(r, okMsg) {
    // v1.2.23: бэкенд распознаёт ПРИЧИНУ отказа (statusBus = «шина занята», таймаут) — показываем
    // её вместо глухого «не подтверждено», иначе пользователь гадает (случай с железа 2026-07-29:
    // активная автояркость держит шину, создание группы падает — в родном DALI Center тоже).
    if (!r || !r.ok) { this._toast(r && r.reason ? r.reason : 'Не подтверждено шлюзом', true); return; }
    const v = r.verify;
    if (v && !v.match) {
      const parts = [];
      if (v.extra && v.extra.length) parts.push('лишние: ' + v.extra.join(', '));
      if (v.missing && v.missing.length) parts.push('не добавились: ' + v.missing.join(', '));
      this._toast('Состав НЕ совпал (' + parts.join('; ') + ') — см. журнал', true);
    } else {
      this._toast(okMsg);
    }
  }

  // перечитать группы и состав ЗАНОВО с контроллера (диагностика «что реально на шлюзе»)
  async _groupReload() {
    try {
      const r = await this._ws({ type: 'arvid_dali_center/group_reload', gw_sn: this._state.activeGw });
      if (r && r.groups) { this._state.groups = r.groups; this._syncStates(); this._render(); }
      this._toast('Состав перечитан с контроллера');
    } catch (e) { this._toast('Ошибка: ' + e.message, true); }
  }

  async _delGroup(g) {
    // ⚠ гейт: раньше `g.name` читался ДО try — на протухшем индексе (см. отрисовку групп) это
    // давало TypeError ВНЕ обработки ошибок, клик молча гибнул, и группа оставалась на контроллере.
    if (!g) { this._toast('Группа не найдена (список обновился) — повторите', true); return; }
    if (!confirm(`Удалить группу ${g.name || g.groupId}?`)) return;
    try {
      const r = await this._ws({ type: 'arvid_dali_center/del_group', gw_sn: this._state.activeGw, channel: g.channel, groupId: g.groupId });
      if (r && r.gone === false) this._toast('⚠ Группа осталась на контроллере (delGroup не отработал) — см. журнал', true);
      else this._toast('Группа удалена');
      await this._loadDevices();
    } catch (e) { this._toast('Ошибка: ' + e.message, true); }
  }

  // ── привязки панели (нативные DALI: кнопка → лампа/группа) ─────────────────
  // Отдельная СТРАНИЦА: матрица кнопки×жесты → цели. Источник правды — контроллер
  // (readPanel). Правка состава — добавить/убрать цель или «заменить» (del+add).
  _saveScroll() { this._savedScroll = window.scrollY || document.documentElement.scrollTop || 0; }

  // вернуться на главный экран, ВОССТАНОВИВ позицию прокрутки (чтобы не мотать заново)
  _backToMain() {
    this._state.view = null; this._state.panelBind = null; this._state.sensorBind = null;
    this._state.energyPage = null; this._state.health = null;   // energy (бейджи) НЕ трогаем
    this._render();
    const y = this._savedScroll || 0;
    requestAnimationFrame(() => { try { window.scrollTo(0, y); } catch (e) { /* ignore */ } });
  }

  // ───────────────────── Энергомониторинг (сателлит) ──────────────────────────
  // Вкладка-страница: параметры ламп (мульти-задание) + табличный отчёт + тариф.
  // Истина — в нашем сторе (бэкенд energy/); периодные срезы — из LTS HA. Кода управления
  // не касается: только читает energy_data / пишет параметры/тариф через WS.
  _openEnergy() {
    this._saveScroll();
    this._state.view = 'energy';
    this._state.energyPage = { loading: true, tariff: null, lamps: [], fFloor: '', fArea: '' };
    this._render();
    this._loadEnergy();
  }

  async _loadEnergy() {
    const e = this._state.energyPage;
    try {
      const r = await this._ws({ type: 'arvid_dali_center/energy_data', gw_sn: this._state.activeGw });
      if (this._state.energyPage !== e) return;   // ушли с вкладки/переоткрыли → ответ устарел
      // имя берём из списка устройств карточки (join по devSn)
      const nameBySn = {};
      this._state.devices.forEach((d) => { if (d.devSn) nameBySn[d.devSn] = d.name || (d.typeName + ' ' + d.address); });
      e.tariff = r.tariff;
      e.curves = r.curves || [];         // кривые драйверов (v1.1.3) — для выбора в параметрах
      e.coverage = r.coverage || null;   // покрытие power_w (E3, v1.2.19): N/M ламп с мощностью
      e.lamps = (r.lamps || []).map((l) => ({ ...l, name: nameBySn[l.devSn] || l.devSn }));
      e.loading = false;
      this._render();
    } catch (err) { e.loading = false; this._toast('Энергия: ' + err.message, true); this._render(); }
  }

  async _saveEnergyParams() {
    const root = this.shadowRoot;
    const devsns = [...root.querySelectorAll('[data-en]')].filter((c) => c.checked).map((c) => c.dataset.en);
    if (!devsns.length) { this._toast('Отметьте лампы', true); return; }
    const pwRaw = root.getElementById('enPower').value.trim();
    const model = root.getElementById('enModel').value.trim();
    if (pwRaw === '' && model === '') { this._toast('Задайте мощность или кривую', true); return; }
    const msg = { type: 'arvid_dali_center/energy_set_params', devsns };
    if (pwRaw !== '') { const v = parseFloat(pwRaw.replace(',', '.')); if (Number.isNaN(v) || v < 0) { this._toast('Мощность — число ≥ 0', true); return; } msg.power_w = v; }
    if (model !== '') msg.model = model;
    try {
      const r = await this._ws(msg);
      this._toast(`Задано ${r.count} лампам`);
      this._state.modal = null;          // закрыть модалку параметров
      await this._loadEnergy();          // перечитать + перерисовать
    } catch (err) { this._toast('Ошибка: ' + err.message, true); }
  }

  async _saveTariff() {
    const raw = this.shadowRoot.getElementById('enTariff').value.trim();
    const tariff = raw === '' ? null : parseFloat(raw.replace(',', '.'));
    if (tariff !== null && (Number.isNaN(tariff) || tariff < 0)) { this._toast('Тариф — число ≥ 0', true); return; }
    try {
      const r = await this._ws({ type: 'arvid_dali_center/energy_set_tariff', tariff });
      this._state.energyPage.tariff = r.tariff;
      this._toast(tariff === null ? 'Тариф снят' : 'Тариф сохранён');
      this._render();
    } catch (err) { this._toast('Ошибка: ' + err.message, true); }
  }

  _energyKwh(l) {
    return (l.energy_wh || 0) / 1000;   // «Всё время» из нашего стора (сенсоров/LTS больше нет)
  }

  // выгрузка CSV того, что в отчёте (видимые колонки; полную иерархию даёт REST-вью)
  _downloadEnergyCsv() {
    const e = this._state.energyPage;
    if (!e || !e.lamps.length) { this._toast('Нет данных', true); return; }
    // выгружаем то, что показано (с учётом фильтров этаж/пространство)
    const shown = e.lamps.filter((l) => (!e.fFloor || l.floor === e.fFloor) && (!e.fArea || l.area === e.fArea));
    const esc = (c) => `"${String(c == null ? '' : c).replace(/"/g, '""')}"`;
    const rows = [['Лампа', 'Этаж', 'Пространство', 'Мощность_Вт', 'Наработка_ч', 'кВтч', 'Стоимость']];
    shown.forEach((l) => {
      const kwh = this._energyKwh(l);
      rows.push([l.name, l.floor || '', l.area || '', l.power_w != null ? l.power_w : '', ((l.on_time_s || 0) / 3600).toFixed(2),
        kwh != null ? kwh.toFixed(3) : '', (kwh != null && e.tariff != null) ? (kwh * e.tariff).toFixed(2) : '']);
    });
    const csv = '﻿' + rows.map((r) => r.map(esc).join(';')).join('\r\n');   // BOM + разделитель ; (локаль Excel)
    const a = document.createElement('a');
    a.href = URL.createObjectURL(new Blob([csv], { type: 'text/csv;charset=utf-8' }));
    a.download = 'arvid_energy.csv';
    a.click();
    URL.revokeObjectURL(a.href);
  }

  // ───────────────────── Здоровье устройств (сателлит) ────────────────────────
  _openHealth() {
    this._saveScroll();
    this._state.view = 'health';
    this._state.health = { loading: true, active: [], recovered: [], thresholds: {}, hFloor: '', hArea: '' };
    this._render();
    this._loadHealth();
  }

  async _loadHealth() {
    const h = this._state.health;
    try {
      // refresh:true — принудительный полный пересчёт. Это НАШ админский экран: открывается
      // человеком и редко, поэтому свежесть важнее цены обхода. Внешние потребители
      // (веб-интерфейс) зовут health_data БЕЗ refresh и получают готовый снимок — оценщик
      // поддерживает его инкрементально, их поллинг больше не гоняет обход объекта (v1.2.5).
      const r = await this._ws({ type: 'arvid_dali_center/health_data', refresh: true });
      if (this._state.health !== h) return;   // ушли с вкладки/переоткрыли → ответ устарел
      h.active = r.active || []; h.recovered = r.recovered || []; h.thresholds = r.thresholds || {};
      h.windowSince = r.window_since || '';
      h.loading = false;
      this._render();
    } catch (err) { h.loading = false; this._toast('Здоровье: ' + err.message, true); this._render(); }
  }

  _fmtTs(iso) {
    if (!iso) return '—';
    try { return new Date(iso).toLocaleString('ru-RU', { day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit' }); } catch (e) { return iso; }
  }

  _healthPage() {
    const h = this._state.health || { loading: true, active: [], recovered: [] };
    const head = `<header class="hd"><div class="hd-title">${svg(ICONS.health)}<div><b>Здоровье устройств</b><span>лог ошибок</span></div></div>
      <div class="hd-actions">
        <button class="btn ghost" data-act="healthThresholdsOpen" title="Пороги">${svg(ICONS.param)}</button>
        <button class="btn ghost" data-act="healthRefresh" title="Обновить">${svg(ICONS.restart)}</button>
        <button class="btn primary" data-act="healthBack">← Назад</button></div></header>`;
    if (h.loading) return head + '<section class="panel empty">Загрузка…</section>';
    // фильтры этаж/пространство (значения из всех записей — активных + лога)
    const all = [...(h.active || []), ...(h.recovered || [])];
    const uniq = (key) => [...new Set(all.map((r) => r[key]).filter(Boolean))].sort();
    const optList = (vals, cur) => `<option value="">Все</option>` + vals.map((v) => `<option value="${v}"${v === cur ? ' selected' : ''}>${v}</option>`).join('');
    const floors = uniq('floor'); const areas = uniq('area');
    const pass = (e) => (!h.hFloor || e.floor === h.hFloor) && (!h.hArea || e.area === h.hArea);
    const filters = (floors.length || areas.length) ? `<section class="panel"><div class="enfilters">
      ${floors.length ? `<label class="fld"><span>Этаж</span><select id="hFloor">${optList(floors, h.hFloor || '')}</select></label>` : ''}
      ${areas.length ? `<label class="fld"><span>Пространство</span><select id="hArea">${optList(areas, h.hArea || '')}</select></label>` : ''}
    </div></section>` : '';

    const act = (h.active || []).filter(pass);
    const errRows = act.length ? act.map((e) => `<div class="row"><div class="row-main"><span class="dot off"></span><div class="row-txt">
      <div class="name-row"><b>${this._esc(e.name)}</b> <span class="zchip">${e.kindLabel}</span></div>
      <span class="muted">с ${this._fmtTs(e.since)}${e.gw_sn ? ' · шлюз ' + e.gw_sn : ''}</span></div></div></div>`).join('')
      : '<div class="muted" style="padding:10px 6px">Активных ошибок нет.</div>';
    // окно «Восстановлено»: записи новее метки window_since (хранилище шире — 30 дней) + фильтр
    const since = h.windowSince ? new Date(h.windowSince).getTime() : 0;
    const win = (h.recovered || []).filter((e) => (!since || (e.resolved && new Date(e.resolved).getTime() >= since)) && pass(e));
    const recRows = win.length ? win.map((e) => `<div class="row"><div class="row-main"><span class="dot on"></span><div class="row-txt">
      <div class="name-row"><b>${this._esc(e.name)}</b> <span class="muted">${e.kindLabel}</span></div>
      <span class="muted">было ${this._fmtTs(e.since)} → восстановлено ${this._fmtTs(e.resolved)}</span></div></div></div>`).join('')
      : '<div class="muted" style="padding:10px 6px">В окне пусто (с последней очистки/начала суток).</div>';
    const errors = `<section class="panel"><div class="panel-h">Ошибки <span class="muted">${act.length}</span></div><div class="hlist">${errRows}</div></section>`;
    const recovered = `<section class="panel"><div class="panel-h">Восстановлено <span class="muted">окно ${win.length} · в логе ${h.recovered.length}</span><button class="mini" data-act="healthClearWindow" style="margin-left:auto">Очистить окно</button><button class="mini" data-act="healthCsv">Скачать CSV (30 дн)</button></div><div class="hlist">${recRows}</div></section>`;
    return head + filters + errors + recovered;
  }

  async _saveHealthThresholds() {
    const sr = this.shadowRoot;
    const msg = { type: 'arvid_dali_center/health_set_thresholds' };
    [['htMotion', 'motion_stuck_h'], ['htClear', 'clear_h'], ['htLux', 'lux_stale_h'], ['htGrace', 'grace_min'], ['htInterval', 'interval_min']]
      .forEach(([id, k]) => { const v = parseFloat((sr.getElementById(id) || {}).value); if (!Number.isNaN(v) && v > 0) msg[k] = v; });
    try {
      const r = await this._ws(msg);
      if (this._state.health) this._state.health.thresholds = r.thresholds;
      this._toast('Пороги сохранены');
      this._state.modal = null;
      await this._loadHealth();
    } catch (err) { this._toast('Ошибка: ' + err.message, true); }
  }

  async _clearHealthWindow() {
    // чистим только ОКНО (метка), хранилище 30 дней остаётся (видно в CSV)
    try {
      await this._ws({ type: 'arvid_dali_center/health_clear_window' });
      this._toast('Окно очищено (лог сохранён)');
      await this._loadHealth();
    } catch (err) { this._toast('Ошибка: ' + err.message, true); }
  }

  _downloadHealthCsv() {
    const h = this._state.health;
    if (!h) return;
    const esc = (c) => `"${String(c == null ? '' : c).replace(/"/g, '""')}"`;
    const rows = [['Статус', 'Тип', 'Имя', 'devType', 'Шлюз', 'Начало', 'Восстановлено']];
    h.active.forEach((e) => rows.push(['Ошибка', e.kindLabel, e.name, e.devType, e.gw_sn, this._fmtTs(e.since), '']));
    h.recovered.forEach((e) => rows.push(['Восстановлено', e.kindLabel, e.name, e.devType, e.gw_sn, this._fmtTs(e.since), this._fmtTs(e.resolved)]));
    const csv = '﻿' + rows.map((r) => r.map(esc).join(';')).join('\r\n');
    const a = document.createElement('a');
    a.href = URL.createObjectURL(new Blob([csv], { type: 'text/csv;charset=utf-8' }));
    a.download = 'arvid_health.csv'; a.click(); URL.revokeObjectURL(a.href);
  }

  _energyPage() {
    const e = this._state.energyPage || { loading: true, lamps: [], tariff: null };
    const head = `<header class="hd"><div class="hd-title">${svg(ICONS.chart)}<div><b>Энергомониторинг</b><span>расчётный · шлюз ${this._state.activeGw || ''}</span></div></div>
      <div class="hd-actions">
        <button class="btn ghost" data-act="energyParamsOpen" title="Параметры ламп (мощность/модель)">${svg(ICONS.param)}</button>
        <button class="btn primary" data-act="energyBack">← Назад</button></div></header>`;
    if (e.loading) return head + '<section class="panel empty">Загрузка…</section>';
    if (!e.lamps.length) return head + '<section class="panel empty">Нет ламп на этом шлюзе.</section>';

    // — фильтры: этаж / пространство (для масштаба в 1000+ ламп) —
    const uniq = (key) => [...new Set(e.lamps.map((l) => l[key]).filter(Boolean))].sort();
    const optList = (vals, cur) => `<option value="">Все</option>` + vals.map((v) => `<option value="${v}"${v === cur ? ' selected' : ''}>${v}</option>`).join('');
    const floors = uniq('floor'); const areas = uniq('area');
    const filters = (floors.length || areas.length) ? `<div class="enfilters">
      ${floors.length ? `<label class="fld"><span>Этаж</span><select id="enFloor">${optList(floors, e.fFloor || '')}</select></label>` : ''}
      ${areas.length ? `<label class="fld"><span>Пространство</span><select id="enArea">${optList(areas, e.fArea || '')}</select></label>` : ''}
    </div>` : '';

    // — отчёт (на отфильтрованном списке), «Всё время» из нашего стора —
    const shown = e.lamps.filter((l) => (!e.fFloor || l.floor === e.fFloor) && (!e.fArea || l.area === e.fArea));
    let totalKwh = 0;
    const rows = shown.map((l) => {
      const kwh = this._energyKwh(l);
      totalKwh += kwh;
      const onH = (l.on_time_s || 0) / 3600;
      const cost = (e.tariff != null) ? (kwh * e.tariff).toFixed(2) + ' ₽' : '';
      return `<tr><td>${this._esc(l.name)}</td><td class="num">${l.power_w != null ? l.power_w : '—'}</td>
        <td class="num">${onH >= 0.1 ? onH.toFixed(1) + ' ч' : '—'}</td>
        <td class="num">${kwh.toFixed(3)}</td>
        <td class="num">${cost || '—'}</td></tr>`;
    }).join('');
    const totalCost = (e.tariff != null) ? (totalKwh * e.tariff).toFixed(2) + ' ₽' : '';
    const cntLabel = shown.length === e.lamps.length ? `${e.lamps.length}` : `${shown.length} из ${e.lamps.length}`;
    // Покрытие (E3, v1.2.19): сколько ламп с заданной мощностью. Непокрытые дают 0 — их
    // потребление в отчёт НЕ входит; показываем честно, чтобы числам можно было доверять.
    const cov = e.coverage;
    const covLine = cov && cov.total ? `<div class="muted" style="margin:2px 0 8px">Покрытие мощностью:
      <b>${cov.covered}/${cov.total}</b> ламп (${cov.pct}%).${cov.uncovered
        ? ` <b style="color:#c0392b">${cov.uncovered}</b> без <code>power_w</code> — их потребление в отчёт НЕ входит (задайте мощность в «Параметрах ламп»).`
        : ' Все лампы покрыты.'}</div>` : '';
    const report = `<section class="panel"><div class="panel-h">Отчёт · всё время <span class="muted">${cntLabel}</span></div>
      ${covLine}
      ${filters}
      <table class="entbl"><thead><tr><th>Лампа</th><th class="num">Мощн.</th><th class="num">Наработка</th><th class="num">кВт·ч</th><th class="num">Стоимость</th></tr></thead>
        <tbody>${rows || '<tr><td colspan="5" class="muted" style="padding:10px">Под фильтр ничего не попало.</td></tr>'}</tbody>
        <tfoot><tr><td>ИТОГО</td><td></td><td></td><td class="num">${totalKwh.toFixed(3)}</td><td class="num">${totalCost || '—'}</td></tr></tfoot></table>
      <div class="muted" style="margin-top:6px">Энергия — за всё время из нашего стора. Выгрузка: «Скачать CSV» или REST <code>/api/arvid_dali_center/energy</code> (<code>?format=csv</code>, токен HA).</div>
      <div class="enfoot">
        <label class="fld"><span>Тариф, ₽/кВт·ч</span><input id="enTariff" type="number" min="0" step="0.01" value="${e.tariff != null ? e.tariff : ''}" placeholder="не задан"></label>
        <button class="mini" data-act="energyTariff">Сохранить тариф</button>
        <button class="mini" data-act="energyCsv" style="margin-left:auto">Скачать CSV</button>
      </div></section>`;
    return head + report;
  }

  _openPanelBindings(dev) {
    if (!dev) return;
    this._saveScroll();
    this._state.view = 'panelBind';
    this._state.panelBind = { dev, loading: true, keyCount: 0, gestures: [], cells: [] };
    this._render();
    this._loadPanelBindings();
  }

  async _loadPanelBindings() {
    const pb = this._state.panelBind;
    if (!pb) return;
    const d = pb.dev;
    try {
      const r = await this._ws({ type: 'arvid_dali_center/panel_bindings', gw_sn: this._state.activeGw, devType: d.devType, channel: d.channel, address: d.address });
      if (this._state.panelBind !== pb) return;   // сменили устройство/вкладку → ответ устарел
      pb.keyCount = r.keyCount; pb.gestures = r.gestures; pb.cells = r.cells || [];
      if (String(d.devType) === '0300') {   // поворот регулирует яркость логикой в HA
        const rr = await this._ws({ type: 'arvid_dali_center/get_rotary_binding', gw_sn: this._state.activeGw, devType: d.devType, channel: d.channel, address: d.address });
        pb.rotary = rr.binding || null;
      }
    } catch (e) { this._toast('Ошибка: ' + e.message, true); }
    pb.loading = false;
    this._render();
  }

  // имя цели привязки: группа (devType 0401) или лампа — резолвим по кешу.
  // ⚠ v1.2.38: имя ищем ТОЛЬКО среди устройств СВОЕГО контроллера. Адреса 0..63 живут на
  // КАЖДОМ шлюзе, поэтому для цели с чужим gwSnObj местное имя — подмена: раньше чип
  // рисовал одноимённую местную лампу, и кросс-шлюзовая привязка выглядела как «привязалось
  // к своей». Чужую цель показываем адресом + пометкой контроллера.
  _targetName(o) {
    const own = String(this._state.activeGw || '').toUpperCase();
    const gw = String(o.gwSnObj || this._state.activeGw || '').toUpperCase();
    const foreign = !!gw && gw !== own;
    const mark = foreign ? ` <em class="muted">· ${this._esc(gw)}</em>` : '';
    if (String(o.devType) === '0401') {
      const g = foreign ? null : (this._state.groups || []).find((x) => x.channel === o.channel && x.groupId === o.address);
      return this._esc('группа ' + (g ? (g.name || g.groupId) : o.address)) + mark;
    }
    const d = foreign ? null : (this._state.devices || []).find((x) => String(x.devType) === String(o.devType) && x.channel === o.channel && x.address === o.address);
    return this._esc((d && d.name) || ('лампа ' + o.address)) + mark;
  }

  // действие привязки из property: вкл / выкл / вкл N% (шина 1..1000 → %).
  // isSensor — контекст: пустой property у ДАТЧИКА = авто-яркость (恒照, штатно), у ПАНЕЛИ
  // авто НЕВОЗМОЖНА (恒照 = addSensorObj+luxRange на 0202, не панельное действие). Контроллер
  // не эхоит команды-жесты (STEP 31/32, UP/DOWN 25/26) в property при readPanel → у панели
  // пустой property НЕ называем «авто-яркость» (был баг: «шаг ярче» показывался как авто).
  _actionLabel(prop, isSensor, act) {
    // act — сохранённое у нас действие (PanelActStore): контроллер тип жеста при readPanel
    // не отдаёт (property пуст), поэтому для панели берём наш act. Приоритетнее property.
    const ACT = { dimup: 'ярче', dimdown: 'темнее', stepup: 'шаг ярче', stepdown: 'шаг темнее',
                  on: 'вкл', off: 'выкл', onbri: 'вкл+ярк.', toggle: 'переключить' };
    if (act && ACT[act]) return ACT[act];
    // зеркалит _actionProp: 20 вкл/выкл, 22 яркость, 25/26 плавно ярче/темнее (удержание),
    // 31/32 шаг ярче/темнее
    const p = prop || [];
    const has = (id) => p.find((x) => x.dpid === id);
    if (has(25)) return 'ярче';
    if (has(26)) return 'темнее';
    if (has(31)) return 'шаг ярче';
    if (has(32)) return 'шаг темнее';
    const on = has(20), bri = has(22);
    if (on && on.value === false) return 'выкл';
    if (bri && bri.value != null) return 'вкл ' + Math.round(bri.value / 1000 * 100) + '%';
    if (on && on.value === true) return 'вкл';
    if (p.length) return 'dpid ' + p.map((x) => x.dpid).join(',');   // непустое неизвестное — честно
    // пустой property: датчик → авто-яркость (恒照); панель → команда-жест (контроллер не
    // вернул её в property, тип не восстановить из чтения — показываем нейтрально)
    return isSensor ? 'авто-яркость' : 'жест (на контроллере)';
  }

  _panelBindPage() {
    const pb = this._state.panelBind;
    if (!pb) return '';
    const d = pb.dev;
    const head = `<header class="hd"><div class="hd-title">${svg(ICONS.bulb)}<div><b>Привязки кнопок</b><span>${d.name || d.typeName + ' ' + d.address} · ch${d.channel}/${d.address}</span></div></div>
      <div class="hd-actions"><button class="btn ghost" data-act="panelReload" title="Перечитать с контроллера">${svg(ICONS.restart)}</button><button class="btn primary" data-act="panelBack">← Назад</button></div></header>
      <div class="muted" style="padding:4px 6px">Привязки живут на контроллере и работают без Home Assistant.</div>`;
    if (pb.loading) return head + this._skeleton();
    const byKey = {};
    pb.cells.forEach((c) => { (byKey[c.keyNo] = byKey[c.keyNo] || []).push(c); });
    const keys = Object.keys(byKey).map(Number).sort((a, b) => a - b);
    const sections = keys.map((k) => {
      const rows = byKey[k].map((c) => {
        if (c.dpid === 4) {   // ПОВОРОТ → регулировка яркости логикой в HA (натив не умеет)
          const b = pb.rotary;
          const info = b && b.target
            ? `${this._targetName(b.target)} · <em>яркость, шаг ${Math.round((b.step || 20) / 1000 * 100)}%</em>`
            : '<span class="muted">не настроено</span>';
          return `<div class="row"><div class="row-main"><div class="row-txt"><b>${c.gesture} → яркость <span class="muted">(в HA)</span></b><div class="bchips">${info}</div></div></div>
            <div class="row-act"><button class="mini" data-act="rotaryEdit" title="Поворот регулирует яркость цели (логика в HA)">${b ? 'Изменить' : 'Настроить'}</button>${b ? '<button class="mini danger" data-act="rotaryClear" title="Снять">снять</button>' : ''}</div></div>`;
        }
        const targets = (c.outObj || []).map((o, oi) => `<span class="tchip">${this._targetName(o)} · <em>${this._actionLabel(o.property, false, o.act)}</em><button class="tx" data-act="rmBinding" data-key="${k}" data-dpid="${c.dpid}" data-oidx="${oi}" title="Убрать">×</button></span>`).join(' ') || '<span class="muted">не привязано</span>';
        return `<div class="row"><div class="row-main"><div class="row-txt"><b>${c.gesture}</b><div class="bchips">${targets}</div></div></div>
          <div class="row-act"><button class="mini" data-act="addBinding" data-key="${k}" data-dpid="${c.dpid}" data-replace="0" title="Добавить цель">+ цель</button>
          <button class="mini" data-act="addBinding" data-key="${k}" data-dpid="${c.dpid}" data-replace="1" title="Заменить все цели кнопки">заменить</button></div></div>`;
      }).join('');
      return `<section class="panel"><div class="panel-h">Кнопка ${k}</div>${rows}</section>`;
    }).join('');
    return head + (sections || '<div class="muted" style="padding:8px">Нет кнопок/жестов для этого типа панели.</div>');
  }

  // точечно обновить ОДНУ ячейку (кнопка×жест) без полной перечитки матрицы (24 readPanel)
  _patchCell(keyNo, dpid, outObj) {
    const pb = this._state.panelBind;
    if (!pb) return;
    const cell = pb.cells.find((c) => c.keyNo === keyNo && c.dpid === dpid);
    if (cell) { cell.outObj = outObj || []; this._render(); }
    else this._loadPanelBindings();   // fallback — полная перечитка
  }

  _openAddBinding(keyNo, dpid, replace) {
    // текущая цель ячейки (для предвыбора при «заменить/+цель» — не сбрасывать на первую)
    const cell = (this._state.panelBind.cells || []).find((c) => c.keyNo === keyNo && c.dpid === dpid);
    const cur = (cell && cell.outObj && cell.outObj[0]) || null;
    const gw = (cur && cur.gwSnObj) || this._state.activeGw;   // цель может быть на другом шлюзе
    this._state.modal = { kind: 'panelBind', keyNo, dpid, replace, targetGw: gw, lamps: [], groups: [], cur, loadingTargets: true };
    this._render();
    this._loadBindTargets(gw);   // подгрузить устройства/группы ВЫБРАННОГО контроллера
  }

  // лампы+группы выбранного контроллера для модалки привязки (под масштаб — не все шлюзы сразу)
  async _loadBindTargets(gw) {
    const m = this._state.modal;
    if (!m) return;
    try {
      const [dr, gr] = await Promise.all([
        this._ws({ type: 'arvid_dali_center/devices', gw_sn: gw }),
        this._ws({ type: 'arvid_dali_center/groups', gw_sn: gw }),
      ]);
      if (this._state.modal !== m) return;   // модалку закрыли/сменили — ответ устарел
      m.lamps = (dr.devices || []).filter((d) => LIGHT_T.includes(String(d.devType)));
      m.groups = gr.groups || [];
      // КРОСС-ГРУППЫ как цель привязки (v1.2.54). Они НЕ принадлежат выбранному контроллеру —
      // их копии живут на каждом участнике, поэтому список от `targetGw` не зависит.
      m.xgroups = (this._state.xgroups || []).slice();
    } catch (e) { this._toast('Цели: ' + e.message, true); }
    m.loadingTargets = false;
    this._render();
  }

  // property текущей цели → действие (предвыбор select действия); зеркалит _actionProp
  _propToAction(prop) {
    const p = prop || [];
    const has = (id) => p.find((x) => x.dpid === id);
    if (has(25)) return 'dimup';
    if (has(26)) return 'dimdown';
    if (has(31)) return 'stepup';
    if (has(32)) return 'stepdown';
    const on = has(20), bri = has(22);
    if (on && on.value === false) return 'off';
    if (bri && bri.value != null) return 'onbri';
    return 'on';
  }

  // действие привязки → property (dpid). Общий для привязок панели и датчика.
  // 20 вкл/выкл, 22 яркость (% → шина 10..1000). H1c — ДИММИРОВАНИЕ удержанием:
  // 25 «плавно ярче» / 26 «плавно темнее» (рампу ведёт контроллер, ПОКА кнопка
  // удерживается), 31/32 «шаг ярче/темнее». ⚠ Кодировка 25/26/31/32 —
  // ПРЕДПОЛОЖЕНИЕ (bool/true, по аналогии с dpid20); уточняется hardware-тестом
  // H1c через readPanel-сверку (см. docs/PLAN_SENSOR_BINDINGS.md §H1c).
  _actionProp(action, bri) {
    switch (action) {
      case 'off': return [{ dpid: 20, dataType: 'bool', value: false }];
      case 'onbri': return [{ dpid: 20, dataType: 'bool', value: true }, { dpid: 22, dataType: 'uint16', value: Math.max(10, Math.round((bri || 100) / 100 * 1000)) }];
      case 'dimup': return [{ dpid: 25, dataType: 'bool', value: true }];
      case 'dimdown': return [{ dpid: 26, dataType: 'bool', value: true }];
      case 'stepup': return [{ dpid: 31, dataType: 'bool', value: true }];
      case 'stepdown': return [{ dpid: 32, dataType: 'bool', value: true }];
      // toggle (переключить вкл/выкл): property как «вкл» (dpid20:true), отличие — mode 129
      // (+ setPanelArg), см. _saveBinding. Захват DALI Center 2026-07-03.
      case 'toggle': return [{ dpid: 20, dataType: 'bool', value: true }];
      default: return [{ dpid: 20, dataType: 'bool', value: true }];
    }
  }

  async _saveBinding() {
    const m = this._state.modal;
    const sel = (this.shadowRoot.getElementById('bindTarget') || {}).value || '';
    const action = (this.shadowRoot.getElementById('bindAction') || {}).value || 'on';
    const bri = parseInt((this.shadowRoot.getElementById('bindBri') || {}).value, 10);
    if (!sel) { this._toast('Выбери цель', true); return; }
    const [kind, ix] = sel.split(':');
    const prop = this._actionProp(action, bri);
    // toggle → mode 129 (контроллеру нужен режим-переключатель + setPanelArg, см. бэкенд);
    // остальные действия — обычный mode 255
    const mode = action === 'toggle' ? 129 : 255;
    let outObj;
    const tgw = m.targetGw || this._state.activeGw;   // контроллер цели (cross-gateway; датчик — свой)
    // ЦЕЛИ: одна запись, кроме кросс-группы — у неё по одной цели на КАЖДОГО участника
    // (v1.2.54). Форма та же, что доказана захватом для автояркости: `0401`, address =
    // общий groupId, gwSnObj = участник. Дальше панельный фан-аут (`_panel_targets_add`)
    // сам разложит цели по контроллерам — он уже так умеет.
    let outObjs;
    if (kind === 'lamp') { const d = m.lamps[+ix]; outObjs = [{ gwSnObj: tgw, devType: d.devType, channel: d.channel, address: d.address, property: prop }]; }
    else if (kind === 'xgroup') {
      const x = (m.xgroups || [])[+ix];
      if (!x) { this._toast('Кросс-группа не найдена — перевыбери', true); return; }
      outObjs = (x.participants || []).map((part) => ({ gwSnObj: part, devType: '0401', channel: x.channel, address: x.groupId, property: prop }));
      if (!outObjs.length) { this._toast('У кросс-группы нет участников', true); return; }
    }
    else { const g = m.groups[+ix]; outObjs = [{ gwSnObj: tgw, devType: '0401', channel: g.channel, address: g.groupId, property: prop }]; }
    const d = this._state.panelBind.dev;
    try {
      const r = await this._ws({ type: 'arvid_dali_center/add_panel_obj', gw_sn: this._state.activeGw, devType: d.devType, channel: d.channel, address: d.address, keyNo: m.keyNo, dpid: m.dpid, panelType: 2, mode, action, replace: m.replace, outObj: outObjs });
      // v1.2.39: ячейка пишется на ДВА контроллера (панель + шлюз цели). Недоступный шлюз
      // цели = привязка неполная, и это обязано быть видно, а не «сохранено»
      const warn = (r.warnings || []).join('; ');
      if (r.ok && (!r.verify || r.verify.match)) this._toast(warn ? 'Сохранено, но ⚠ ' + warn : 'Привязка сохранена', !!warn);
      else this._toast('Не подтверждено / цель не привязалась — см. журнал', true);
      this._state.modal = null;
      this._patchCell(m.keyNo, m.dpid, r.outObj);   // обновляем только эту ячейку
    } catch (e) { this._toast('Ошибка: ' + e.message, true); }
  }

  async _rmBinding(keyNo, dpid, oidx) {
    const pb = this._state.panelBind;
    if (!pb) return;
    const cell = pb.cells.find((c) => c.keyNo === keyNo && c.dpid === dpid);
    const o = cell && cell.outObj[oidx];
    if (!o) return;
    if (!confirm('Убрать цель привязки?')) return;
    const d = pb.dev;
    try {
      const r = await this._ws({ type: 'arvid_dali_center/del_panel_obj', gw_sn: this._state.activeGw, devType: d.devType, channel: d.channel, address: d.address, keyNo, dpid, outObj: [{ gwSnObj: o.gwSnObj, devType: o.devType, channel: o.channel, address: o.address }] });
      const warn = ((r && r.warnings) || []).join('; ');
      if (r && r.gone === false) this._toast('⚠ Цель осталась на контроллере — см. журнал', true);
      else this._toast(warn ? 'Цель убрана, но ⚠ ' + warn : 'Цель убрана', !!warn);
      this._patchCell(keyNo, dpid, r && r.outObj);   // обновляем только эту ячейку
    } catch (e) { this._toast('Ошибка: ' + e.message, true); }
  }

  // ── привязки датчика (нативные DALI: движение → лампа/группа) ──────────────
  // Зеркало страницы панелей: события датчика (движение/нет движения/…) → цели.
  // Источник правды — контроллер (readSensor). Правка — del+add, точечное обновление.
  _openSensorBindings(dev) {
    if (!dev) return;
    this._saveScroll();
    this._state.view = 'sensorBind';
    this._state.sensorBind = { dev, loading: true, events: [], modeType: 'manual', timeValue: 0 };
    this._render();
    this._loadSensorBindings();
  }

  async _loadSensorBindings() {
    const sb = this._state.sensorBind;
    if (!sb) return;
    const d = sb.dev;
    try {
      const r = await this._ws({ type: 'arvid_dali_center/sensor_bindings', gw_sn: this._state.activeGw, devType: d.devType, channel: d.channel, address: d.address });
      if (this._state.sensorBind !== sb) return;   // сменили устройство/вкладку → ответ устарел
      sb.events = r.events || []; sb.modeType = r.modeType || 'manual'; sb.timeValue = r.timeValue || 0;
    } catch (e) { this._toast('Ошибка: ' + e.message, true); }
    sb.loading = false;
    this._render();
  }

  _patchSensorCell(dpid, outObj) {
    const sb = this._state.sensorBind;
    if (!sb) return;
    const ev = sb.events.find((c) => c.dpid === dpid);
    if (ev) { ev.outObj = outObj || []; this._render(); }
    else this._loadSensorBindings();
  }

  _sensorBindPage() {
    const sb = this._state.sensorBind;
    if (!sb) return '';
    const d = sb.dev;
    const MODES = { ordinary: 'обычный (не перехватывать сторонние)', auto: 'авто (перехват через timeValue)', manual: 'ручной (перехват по on→off)' };
    const modeOpts = Object.keys(MODES).map((k) => `<option value="${k}"${sb.modeType === k ? ' selected' : ''}>${MODES[k]}</option>`).join('');
    const head = `<header class="hd"><div class="hd-title">${svg(ICONS.bulb)}<div><b>Привязки датчика</b><span>${d.name || d.typeName + ' ' + d.address} · ch${d.channel}/${d.address}</span></div></div>
      <div class="hd-actions"><button class="btn ghost" data-act="sensorReload" title="Перечитать с контроллера">${svg(ICONS.restart)}</button><button class="btn primary" data-act="sensorBack">← Назад</button></div></header>
      <div class="muted" style="padding:4px 6px">Привязки живут на контроллере и работают без Home Assistant. Тайминги «движение→свободно» — в «Параметры» датчика (downTime/occpyTime).</div>
      <div class="grid" style="padding:0 6px 6px"><label class="fld"><span>Режим перехвата (для новых)</span><select id="sbMode">${modeOpts}</select></label><label class="fld"><span>timeValue, сек (для auto)</span><input id="sbTime" type="number" min="0" value="${sb.timeValue > 0 ? sb.timeValue : 0}"></label></div>`;
    if (sb.loading) return head + this._skeleton();
    const rows = (sb.events || []).map((c) => {
      const targets = (c.outObj || []).map((o, oi) => `<span class="tchip">${this._targetName(o)} · <em>${this._actionLabel(o.property, true, o.act)}</em><button class="tx" data-act="rmSBind" data-dpid="${c.dpid}" data-oidx="${oi}" title="Убрать">×</button></span>`).join(' ') || '<span class="muted">не привязано</span>';
      return `<div class="row"><div class="row-main"><div class="row-txt"><b>${c.name}</b><div class="bchips">${targets}</div></div></div>
        <div class="row-act"><button class="mini" data-act="addSBind" data-dpid="${c.dpid}" data-replace="0" title="Добавить цель">+ цель</button>
        <button class="mini" data-act="addSBind" data-dpid="${c.dpid}" data-replace="1" title="Заменить все цели события">заменить</button></div></div>`;
    }).join('');
    return head + `<section class="panel"><div class="panel-h">События датчика</div>${rows || '<div class="muted" style="padding:8px">Нет событий.</div>'}</section>`;
  }

  _openAddSensorBinding(dpid, replace) {
    const lamps = this._state.devices.filter((d) => LIGHT_T.includes(String(d.devType)));
    const groups = this._state.groups || [];
    // подхватываем выбранный режим/timeValue из шапки (если успели поменять)
    const modeSel = this.shadowRoot.getElementById('sbMode');
    const timeInp = this.shadowRoot.getElementById('sbTime');
    if (modeSel && this._state.sensorBind) this._state.sensorBind.modeType = modeSel.value;
    if (timeInp && this._state.sensorBind) this._state.sensorBind.timeValue = parseInt(timeInp.value, 10) || 0;
    const ev = (this._state.sensorBind.events.find((c) => c.dpid === dpid) || {}).name || ('событие ' + dpid);
    this._state.modal = { kind: 'sensorBind', dpid, replace, lamps, groups, ev };
    this._render();
  }

  async _saveSensorBinding() {
    const m = this._state.modal;
    const sb = this._state.sensorBind;
    const sel = (this.shadowRoot.getElementById('bindTarget') || {}).value || '';
    const action = (this.shadowRoot.getElementById('bindAction') || {}).value || 'on';
    const bri = parseInt((this.shadowRoot.getElementById('bindBri') || {}).value, 10);
    if (!sel) { this._toast('Выбери цель', true); return; }
    const [kind, ix] = sel.split(':');
    const prop = this._actionProp(action, bri);
    let outObj;
    const tgw = m.targetGw || this._state.activeGw;   // контроллер цели (cross-gateway; датчик — свой)
    if (kind === 'lamp') { const d = m.lamps[+ix]; outObj = { gwSnObj: tgw, devType: d.devType, channel: d.channel, address: d.address, property: prop }; }
    else { const g = m.groups[+ix]; outObj = { gwSnObj: tgw, devType: '0401', channel: g.channel, address: g.groupId, property: prop }; }
    const d = sb.dev;
    try {
      const r = await this._ws({ type: 'arvid_dali_center/add_sensor_obj', gw_sn: this._state.activeGw, devType: d.devType, channel: d.channel, address: d.address, dpid: m.dpid, modeType: sb.modeType, timeValue: sb.timeValue || 0, replace: m.replace, outObj: [outObj] });
      if (r.ok && (!r.verify || r.verify.match)) this._toast('Привязка сохранена');
      else this._toast('Не подтверждено / цель не привязалась — см. журнал', true);
      this._state.modal = null;
      this._patchSensorCell(m.dpid, r.outObj);
    } catch (e) { this._toast('Ошибка: ' + e.message, true); }
  }

  async _rmSensorBinding(dpid, oidx) {
    const sb = this._state.sensorBind;
    if (!sb) return;
    const ev = sb.events.find((c) => c.dpid === dpid);
    const o = ev && ev.outObj[oidx];
    if (!o) return;
    if (!confirm('Убрать цель привязки?')) return;
    const d = sb.dev;
    try {
      const r = await this._ws({ type: 'arvid_dali_center/del_sensor_obj', gw_sn: this._state.activeGw, devType: d.devType, channel: d.channel, address: d.address, dpid, outObj: [{ gwSnObj: o.gwSnObj, devType: o.devType, channel: o.channel, address: o.address }] });
      if (r && r.gone === false) this._toast('⚠ Цель осталась на контроллере — см. журнал', true);
      else this._toast('Цель убрана');
      this._patchSensorCell(dpid, r && r.outObj);
    } catch (e) { this._toast('Ошибка: ' + e.message, true); }
  }

  // ── рендер ──────────────────────────────────────────────────────────────
  _renderShell() {
    this.shadowRoot.innerHTML = `<style>${STYLE}</style><div class="root"><div class="card" id="card"></div><div id="toast" class="toast"></div></div>`;
    this._rendered = true;
    this._render();
    const card = this.shadowRoot.getElementById('card');
    card.addEventListener('click', (e) => this._onClick(e));
    // яркость применяется при ОТПУСКАНИИ слайдера (change), без кнопки «Применить»
    card.addEventListener('change', (e) => {
      if (e.target.id === 'briRange') { this._applyBright(false); return; }
      // выбор шлюза через компактный select (когда контроллеров много)
      if (e.target.id === 'gwSelect') { this._state.activeGw = e.target.value; this._persistGw(); this._state.loading = true; this._render(); this._loadDevices(); this._pollEnergy(); return; }
      // фильтры энергоотчёта (этаж/пространство) → перерисовать список
      if (e.target.id === 'enFloor' && this._state.energyPage) { this._state.energyPage.fFloor = e.target.value; this._render(); }
      else if (e.target.id === 'enArea' && this._state.energyPage) { this._state.energyPage.fArea = e.target.value; this._render(); }
      // фильтры здоровья
      else if (e.target.id === 'hFloor' && this._state.health) { this._state.health.hFloor = e.target.value; this._render(); }
      else if (e.target.id === 'hArea' && this._state.health) { this._state.health.hArea = e.target.value; this._render(); }
      // замер: длительность/имя (без ре-рендера — не терять фокус) + чекбоксы ламп
      // смена контроллера цели в модалке привязки → подгрузить его устройства/группы
      // «Вернуть через» имеет смысл ТОЛЬКО для режима auto (v1.2.26) — прячем в остальных.
      // Через style, а не ре-рендер: перерисовка потеряла бы введённые окна и поля.
      else if (e.target.id === 'lkMode') {
        const f = this.shadowRoot.getElementById('lkModeTimeFld');
        if (f) f.style.display = e.target.value === 'auto' ? '' : 'none';
      }
      else if (e.target.id === 'bindCtrl' && this._state.modal) {
        const m = this._state.modal;
        m.targetGw = e.target.value; m.cur = null; m.lamps = []; m.groups = []; m.loadingTargets = true;
        this._render(); this._loadBindTargets(e.target.value);
      }
    });
    // Enter в поле имени → применить переименование
    card.addEventListener('keydown', (e) => {
      if (e.target.id === 'renameInput' && e.key === 'Enter') { e.preventDefault(); this._saveModal(); }
      // Enter/Space на шапке секции = свернуть/развернуть (шапка — role=button)
      const sec = (e.key === 'Enter' || e.key === ' ') && e.target.closest && e.target.closest('[data-act="toggleSection"]');
      if (sec) { e.preventDefault(); this._toggleSection(sec.dataset.sec); }
    });
  }

  _toast(msg, err = false) {
    const t = this.shadowRoot.getElementById('toast');
    if (!t) return;
    t.textContent = msg; t.className = 'toast show' + (err ? ' err' : '');
    clearTimeout(this._tt); this._tt = setTimeout(() => { t.className = 'toast'; }, 3500);
  }

  _onClick(e) {
    if (e.target.classList && e.target.classList.contains('overlay')) { this._state.modal = null; this._render(); return; }
    const el = e.target.closest('[data-act]');
    if (!el) return;
    const act = el.dataset.act;
    const idx = el.dataset.idx;
    const dev = idx !== undefined ? this._state.devices[+idx] : null;
    // группа — ТОЛЬКО по идентичности (`channel:groupId`), см. комментарий в отрисовке групп:
    // индекс протухает от перерисовки после удаления, и клик уходил в чужую группу/undefined.
    const grp = this._groupByKey(el.dataset.gkey);
    if (act === 'gw') { this._state.activeGw = el.dataset.gw; this._persistGw(); this._state.loading = true; this._render(); this._loadDevices(); this._pollEnergy(); }
    else if (act === 'scan') this._openScanMode();           // «Сканировать» → выбор режима manual/auto
    // «auto» — это не режим скана, а разрешение конфликтов + автоматическое перечитывание шины
    else if (act === 'scanRun') { if (el.dataset.mode === 'auto') this._resolveConflicts(); else this._scan('busDevice', 'manual'); }
    // v1.2.21 (F1): «Обновить» (опрос кеша шлюза, flag=exited) УБРАНА — кеш шлюза «кого я знал»
    // поднимал давно снятые «древние лампы» с их именами из корзины HA. Список держится на
    // реконнектах + начальном физическом скане; истина существования — busDevice, не память шлюза.
    else if (act === 'resolveConflicts') this._resolveConflicts();
    else if (act === 'resetAddrs') this._resetAddrs();
    else if (act === 'closeLog') { this._state.scanLog = []; this._render(); }
    else if (act === 'restart') this._restart();
    else if (act === 'wipeData') this._wipeData();
    else if (act === 'gwSettings') this._openGwSettings();
    else if (act === 'saveGwName') this._busy(el, () => this._saveGwName(), 'Сохранение…');
    else if (act === 'syncGwTime') this._busy(el, () => this._syncGwTime(), 'Синхронизация…');
    else if (act === 'saveGwNet') this._busy(el, () => this._saveGwNet(), 'Применение…');
    else if (act === 'refresh') this._loadDevices();
    else if (act === 'toggleEvents') this._toggleEvents();
    else if (act === 'closeEvents') { this._state.eventsOpen = false; this._render(); }
    else if (act === 'params') this._openParams(dev);
    else if (act === 'multiParam') this._openMultiParam();
    else if (act === 'mpCls') {
      const mm = this._state.modal;
      mm.cls = el.dataset.cls;
      // вкладка «Панели» — цели привязки берём с ВЫБРАННОГО контроллера (cross-gateway,
      // v1.2.38): раньше массовая привязка молча брала лампы своего шлюза
      if (mm.cls === 'panel' && !mm.targetGw) {
        mm.targetGw = this._state.activeGw; mm.lamps = []; mm.groups = []; mm.loadingTargets = true;
        this._loadBindTargets(mm.targetGw);
      }
      this._render();
    }
    // область действия массовой настройки ЛАМП: отмеченным или БРОАДКАСТОМ всему
    // контроллеру (v1.2.44, devType FFFF — форма из захвата DALI Center)
    else if (act === 'mpScope') { this._state.modal.scope = el.dataset.scope; this._render(); }
    // весь свет контроллера одной командой — жмём ШТАТНЫЙ сервис на сущности all_lights,
    // чтобы её состояние и кнопка не разъезжались (сущность оптимистичная)
    else if (act === 'allOn') this._allLights(true);
    else if (act === 'allOff') this._allLights(false);
    else if (act === 'registry') this._openRegistry();
    else if (act === 'registryClean') this._cleanRegistry();
    else if (act === 'curvesReload') this._reloadCurves();
    else if (act === 'trash') this._openTrash();
    else if (act === 'trashPurge') this._purgeTrash();
    else if (act === 'groupParam') this._openGroupParam(grp);
    else if (act === 'addr') this._changeAddr(dev);
    else if (act === 'identify') this._identify(dev);
    else if (act === 'rename') this._rename(dev);
    else if (act === 'forget') this._forget(dev);
    else if (act === 'lampToggle') this._lampToggle(dev);
    else if (act === 'bright') this._openBright({ type: 'light', entity: dev.entities && dev.entities.light });
    else if (act === 'groupToggle') this._groupToggle(grp);
    else if (act === 'groupBright') { if (grp) this._openBright(grp.entity_id ? { type: 'light', entity: grp.entity_id } : { type: 'group', g: grp }); }
    else if (act === 'groupRename') this._renameGroup(grp);
    else if (act === 'createGroup') this._openCreateGroup();
    else if (act === 'editGroup') this._openEditGroup(grp);
    else if (act === 'groupReload') this._groupReload();
    else if (act === 'delgroup') this._delGroup(grp);
    // кросс-шлюзовые группы адресуются uid (channel:groupId живёт на нескольких контроллерах)
    else if (act === 'createXGroup') this._openCreateXGroup();
    else if (act === 'editXGroup') this._openEditXGroup(el.dataset.uid);
    else if (act === 'delXGroup') this._delXGroup(el.dataset.uid);
    else if (act === 'xgroupToggle') this._xgroupToggle(el.dataset.uid);
    else if (act === 'xgroupBright') this._xgroupBright(el.dataset.uid);
    else if (act === 'xgwPick') this._xgwPick(el.dataset.gw, el.checked);
    else if (act === 'panelBind') this._openPanelBindings(dev);
    else if (act === 'panelBack') this._backToMain();
    else if (act === 'sensorBind') this._openSensorBindings(dev);
    else if (act === 'luxKeep') this._openLuxKeep(dev);
    else if (act === 'rotaryEdit') this._openRotaryBind((this._state.panelBind || {}).dev);
    else if (act === 'rotaryClear') this._clearRotaryBind();
    else if (act === 'clearLuxKeep') this._busy(el, () => this._clearLuxKeep(), 'Очистка…');
    // окна работы (v1.2.25): «+» добавить, «×» убрать, тумблер — мягкое вкл/выкл датчика
    else if (act === 'lkAddWin') this._lkAddWindow();
    else if (act === 'lkDelWin') this._lkDelWindow(+el.dataset.idx);
    else if (act === 'lkToggle') this._lkToggleEnabled(el.checked);
    // массовые действия над датчиками — через сервисы HA (v1.2.27)
    else if (act === 'mpAddWin') this._mpAddWindow();
    else if (act === 'mpDelWin') this._mpDelWindow(+el.dataset.idx);
    else if (act === 'mpAutoOn') this._busy(el, () => this._mpAutoBright(true), 'Включаю…');
    else if (act === 'mpAutoOff') this._busy(el, () => this._mpAutoBright(false), 'Выключаю…');
    else if (act === 'mpSetWins') this._busy(el, () => this._mpSetWindows(false), 'Пишу окна…');
    else if (act === 'mpClearWins') this._busy(el, () => this._mpSetWindows(true), 'Снимаю…');
    else if (act === 'sensorBack') this._backToMain();
    else if (act === 'sensorReload') this._loadSensorBindings();
    else if (act === 'addSBind') this._openAddSensorBinding(+el.dataset.dpid, el.dataset.replace === '1');
    else if (act === 'rmSBind') this._rmSensorBinding(+el.dataset.dpid, +el.dataset.oidx);
    else if (act === 'panelReload') this._loadPanelBindings();
    else if (act === 'addBinding') this._openAddBinding(+el.dataset.key, +el.dataset.dpid, el.dataset.replace === '1');
    else if (act === 'rmBinding') this._rmBinding(+el.dataset.key, +el.dataset.dpid, +el.dataset.oidx);
    else if (act === 'closeModal') { this._state.modal = null; this._render(); }
    else if (act === 'saveModal') this._busy(el, () => this._saveModal());
    else if (act === 'toggleSection') this._toggleSection(el.dataset.sec);   // свернуть/развернуть секцию
    else if (act === 'openEnergy') this._openEnergy();
    else if (act === 'energyBack') this._backToMain();
    else if (act === 'energyTariff') this._saveTariff();
    else if (act === 'energyCsv') this._downloadEnergyCsv();
    else if (act === 'energyParamsOpen') { this._state.modal = { kind: 'energyParams' }; this._render(); }
    else if (act === 'openHealth') this._openHealth();
    else if (act === 'healthBack') this._backToMain();
    else if (act === 'healthRefresh') this._loadHealth();
    else if (act === 'healthThresholdsOpen') { this._state.modal = { kind: 'healthThresholds' }; this._render(); }
    else if (act === 'healthCsv') this._downloadHealthCsv();
    else if (act === 'healthClearWindow') this._clearHealthWindow();
    // ── калибровочный замер ──
  }

  // Запомнить выбранный шлюз (localStorage, на браузер) — чтобы не сбрасывался на первый
  // при перезагрузке. Восстановление — в _loadGateways (только если шлюз ещё найден).
  _persistGw() {
    try { localStorage.setItem('arvid-dali:gw', this._state.activeGw || ''); } catch (e) { /* ignore */ }
  }

  // ── Сворачиваемые секции (C-Q1: localStorage, дефолт-раскрыто, счётчик в шапке) ──
  // Состояние держим в localStorage (косметика вида, не данные устройств): 4 ключа
  // по классу секции, на браузер. Дефолт (нет ключа) = раскрыто → ничего не прячется молча.
  _secCollapsed(key) {
    try { return localStorage.getItem('arvid-dali:collapse:' + key) === '1'; } catch (e) { return false; }
  }

  _toggleSection(key) {
    const next = !this._secCollapsed(key);
    try { localStorage.setItem('arvid-dali:collapse:' + key, next ? '1' : '0'); } catch (e) { /* ignore */ }
    this._render();
  }

  // Обёртка секции с кликабельной шапкой. headerInner — содержимое шапки (заголовок+счётчик+
  // опц. кнопки, у них свой data-act → closest() ловит их, а не тоггл). body прячем при свёрнутости.
  _collapsibleSection(key, headerInner, body) {
    const c = this._secCollapsed(key);
    return `<section class="panel${c ? ' collapsed' : ''}">
      <div class="panel-h collapsible" data-act="toggleSection" data-sec="${key}" role="button" tabindex="0" aria-expanded="${c ? 'false' : 'true'}">
        <span class="chev">${svg(ICONS.chev, 'sm')}</span>${headerInner}</div>
      ${c ? '' : body}</section>`;
  }

  // Компактный select для выбора шлюза (когда их много). Заголовок опции = имя (или серийник),
  // под select — сводка активного шлюза (серийник/ip/sw/кол-во устройств), как в чипе.
  _gwSelect(s) {
    const opts = s.gateways.map((g) => {
      const nm = (g.name && g.name !== g.gwSn) ? g.name : g.gwSn;
      const st = (g.state && g.state !== 'online') ? ' — нет связи' : '';
      return `<option value="${this._esc(g.gwSn)}"${g.gwSn === s.activeGw ? ' selected' : ''}>${this._esc(nm)}${st}</option>`;
    }).join('');
    const ag = s.gateways.find((g) => g.gwSn === s.activeGw) || {};
    const sub = ag.gwSn ? `${this._esc(ag.gwSn)}${ag.ip ? ' · ' + ag.ip : ''} · sw ${ag.sw || '?'} · ${ag.devices != null ? ag.devices : 0} уст.` : '';
    return `<div class="gw-sel">
      <select id="gwSelect" class="gw-select" aria-label="Выбор контроллера">${opts}</select>
      <div class="gw-sel-sub">${sub}</div></div>`;
  }

  // Busy-обёртка для async-действий кнопок модалок: блокирует кнопку и пишет на ней «label»,
  // пока идёт операция. Если модалка после неё перерисовалась/закрылась (кнопки уже нет в DOM) —
  // восстанавливать нечего; если осталась (ошибка/отмена) — вернуть исходный вид.
  async _busy(el, fn, label = 'Применение…') {
    if (!el || el.dataset.busy) return;
    const prev = el.innerHTML, wasDis = el.disabled;
    el.dataset.busy = '1'; el.disabled = true; el.innerHTML = label;
    try { return await fn(); }
    finally { if (el.isConnected) { el.disabled = wasDis; el.innerHTML = prev; delete el.dataset.busy; } }
  }

  // ───────────────────── Калибровочный замер энергии (сателлит) ────────────────
  // Отдельное окно: выбираю лампы шлюза, пишу N минут реального времени, получаю по каждой
  // + суммарно Вт·ч и РАСЧЁТНУЮ мощность (Δ кумулятива ÷ время; кумулятив НЕ с нуля →
  // считаем по дельте). Сырые сэмплы шлюза → CSV для сверки с ваттметром. Обычный
  // энергоучёт/бейджи НЕ трогаем (свой стор `measure` на бэкенде).
  // ⚠ v1.2.6: страница «Замер» УДАЛЕНА вместе с бэкендом (energy/measure.py + WS measure_*).
  // Замер питался ИСКЛЮЧИТЕЛЬНО числами reportEnergy — то есть мерил энергию ОТ ШЛЮЗА, которой
  // верить нельзя (шлюз не измеряет: ретранслирует энергобанк драйвера либо выдумывает). Свою
  // задачу — доказать несостоятельность шлюза — он выполнил. Живёт расчётный путь.
  // Побочно снят поллинг measure_state каждые 5 с (сервер делал полную копию сырых сэмплов).


  _render() {
    if (!this._rendered) return;
    const s = this._state;
    const card = this.shadowRoot.getElementById('card');
    if (!card) return;
    // отдельная СТРАНИЦА привязок панели (вместо главного экрана)
    if (s.view === 'panelBind') { card.innerHTML = this._panelBindPage() + this._modal(); return; }
    if (s.view === 'sensorBind') { card.innerHTML = this._sensorBindPage() + this._modal(); return; }
    if (s.view === 'energy') { card.innerHTML = this._energyPage() + this._modal(); return; }
    if (s.view === 'health') { card.innerHTML = this._healthPage() + this._modal(); return; }
    const gwChips = s.gateways.map((g) => {
      const bad = g.state && g.state !== 'online';
      const badge = bad ? `<span class="cst warn">${{ offline: 'нет связи', reauth: 'reauth…', init: '…' }[g.state] || g.state}</span>` : '';
      // Заголовок чипа — ИМЯ шлюза (если задано, для визуального ориентира: пользователь
      // вписывает в имя номер линии), иначе серийник. Серийник тогда уходит в подпись.
      const hasName = g.name && g.name !== g.gwSn;
      return `
      <button class="chip ${g.gwSn === s.activeGw ? 'on' : ''}" data-act="gw" data-gw="${g.gwSn}" title="${this._esc(g.gwSn)}">
        <span class="chip-t">${this._esc(hasName ? g.name : g.gwSn)}${badge}</span>
        <span class="chip-s">${hasName ? this._esc(g.gwSn) + ' · ' : ''}${g.ip || ''} · sw ${g.sw || '?'} · ${g.devices} уст.</span></button>`;
    }).join('');
    // Навигация по шлюзам: чипы при небольшом числе (наглядно), компактный select при многих
    // (масштаб 20-40 контроллеров — чипы переполнят экран).
    const gwNav = !s.gateways.length
      ? '<div class="chips"><span class="muted">Шлюзы не найдены</span></div>'
      : s.gateways.length <= 8 ? `<div class="chips">${gwChips}</div>` : this._gwSelect(s);
    card.innerHTML = `
      <header class="hd">
        <div class="hd-title">${svg(ICONS.bulb)}<div><b>ARVID DALI</b><span>панель управления</span></div></div>
        <div class="hd-actions">
          <button class="btn ghost" data-act="multiParam" title="Массовая настройка параметров ламп">${svg(ICONS.param)}</button>
          <button class="btn ghost" data-act="openEnergy" title="Энергомониторинг">${svg(ICONS.chart)}</button>          <button class="btn ghost" data-act="openHealth" title="Здоровье устройств (лог ошибок)">${svg(ICONS.health)}</button>
          <button class="btn ghost ${s.eventsOpen ? 'on' : ''}" data-act="toggleEvents" title="Журнал">${svg(ICONS.list)}</button>
          <button class="btn danger" data-act="resetAddrs" title="Сброс адресов контроллера (resetGateway) — разрушающее">${svg(ICONS.warn)}</button>
        </div>
      </header>
      ${gwNav}
      ${this._connBanner()}
      ${this._eventsPanel()}
      <div class="toolbar">
        <button class="btn primary" data-act="scan" ${s.scanning ? 'disabled' : ''} title="Физический опрос шины (может менять адреса)">${svg(ICONS.scan)}${s.scanning ? 'Сканирую…' : 'Сканировать'}</button>
        <button class="btn ghost" data-act="allOn" title="Включить ВЕСЬ свет контроллера одной командой (броадкаст)">${svg(ICONS.bulb)}Всё вкл</button>
        <button class="btn ghost" data-act="allOff" title="Выключить ВЕСЬ свет контроллера одной командой (броадкаст)">${svg(ICONS.bulb)}Всё выкл</button>
        <button class="btn ghost" data-act="gwSettings" title="Сеть и имя шлюза (DHCP/статический IP)">${svg(ICONS.param)}Сеть</button>
        <button class="btn ghost" data-act="registry" title="Пустые карточки устройств в реестре HA (наследие смены идентичности) — показать и снять">${svg(ICONS.trash || ICONS.param)}Реестр</button>
        <button class="btn ghost" data-act="trash" title="Корзина реестров HA: удалённые записи, которые HA вернёт вместе с entity_id, областью и ярлыками при появлении того же устройства">${svg(ICONS.trash || ICONS.param)}Корзина</button>
        <button class="btn danger" data-act="restart">${svg(ICONS.restart)}Рестарт</button>
        <button class="btn danger" data-act="wipeData" title="Стереть ДАННЫЕ устройств этого шлюза: имена, параметры, энергию, привязки поворота (по devSn). Устройства/группы шлюза тоже. Переехавшие на другой шлюз — не трогает.">${svg(ICONS.trash || ICONS.restart)}Стереть данные</button>
      </div>
      ${this._logPanel()}
      ${s.loading ? this._skeleton() : this._deviceTree()}
      ${this._groupsPanel()}
      ${this._modal()}`;
    if (!s.loading) this._syncStates();
  }

  _renderLog() {
    const box = this.shadowRoot.getElementById('logbox');
    if (box) { box.innerHTML = this._logRows(); box.scrollTop = box.scrollHeight; }
  }
  _logRows() {
    return this._state.scanLog.map((l) => `<div class="logrow ${l.kind}">${l.kind === 'found' ? svg(ICONS.flash, 'sm') : ''}<span>${l.t}</span></div>`).join('');
  }
  // строки конфликтных адресов (модалка скана): ch/addr/класс + ручное назначение
  // Fix H (v1.1.4): кнопка «адрес» убрана. Сменить адрес конфликтному устройству НЕЛЬЗЯ в
  // принципе: команда адресуется ПО короткому адресу, а его занимают двое → её принимают оба
  // (конфликт в лучшем случае переезжает). Единственное лекарство — «Разрешить конфликты»
  // (шлюз переназначает дубли на уровне комиссии шины). set_address для обычных, однозначно
  // адресованных устройств остался (страница устройства).
  _conflictRows() {
    if (!this._state.scanConflicts.length) return '<div class="muted" style="padding:6px">Конфликтов не обнаружено.</div>';
    const rows = this._state.scanConflicts.map((c) =>
      `<div class="logrow err"><span>${svg(ICONS.warn, 'sm')} ch${c.channel} · addr ${c.address} · ${this._esc(c.devClass)}</span></div>`).join('');
    return rows + '<div class="muted" style="padding:6px">Устройства с конфликтных адресов НЕ добавляются (данные шлюза о них недостоверны). Нажмите «Разрешить конфликты» — шлюз разведёт адреса, и шина будет перечитана автоматически.</div>';
  }
  // инкрементальное обновление модалки скана / разведения конфликтов (без полной перерисовки)
  _renderScan() {
    const sr = this.shadowRoot;
    const kind = this._state.modal && this._state.modal.kind;
    if (kind !== 'scan' && kind !== 'resolve') return;   // 'resolve' — только лог, счётчиков нет
    const box = sr.getElementById('scanFound');
    if (box) { box.innerHTML = this._logRows(); box.scrollTop = box.scrollHeight; }
    const fc = sr.getElementById('scanFoundCount');
    if (fc) fc.textContent = this._state.scanLog.filter((x) => x.kind === 'found').length;
    const cc = sr.getElementById('scanConfCount');
    if (cc) cc.textContent = this._state.scanConflicts.length;
    const cbox = sr.getElementById('scanConf');
    if (cbox) cbox.innerHTML = this._conflictRows();
    const sect = sr.getElementById('scanConfSect');
    if (sect) sect.style.display = this._state.scanConflicts.length ? '' : 'none';
    const st = sr.getElementById('scanStatus');
    if (st) st.textContent = this._state.scanning ? 'Идёт сканирование…' : 'Скан завершён';
    const foot = sr.getElementById('scanClose');
    if (foot) foot.disabled = false;
  }
  _logPanel() {
    const s = this._state;
    if (s.modal && s.modal.kind === 'scan') return '';   // во время модалки скана инлайн не дублируем
    if (!s.scanLog.length && !s.scanning) return '';
    // кнопка «Закрыть» доступна, когда скан не идёт (можно убрать лог с экрана)
    const close = s.scanning ? '' : `<button class="mini" data-act="closeLog" title="Закрыть лог">Закрыть</button>`;
    return `<section class="panel log" aria-live="polite"><div class="panel-h">Лог скана <span class="muted">${s.scanLog.filter(x => x.kind === 'found').length} найдено</span>${close}</div><div class="logbox" id="logbox">${this._logRows()}</div></section>`;
  }
  _skeleton() { return `<div class="panel">${'<div class="sk"></div>'.repeat(5)}</div>`; }

  _deviceTree() {
    const s = this._state;
    if (!s.devices.length) return `<section class="panel empty">Нет устройств. Запустите скан.</section>`;
    const byType = (types) => s.devices.map((d, i) => ({ d, i })).filter((x) => types.includes(String(x.d.devType)))
      .sort((a, b) => (a.d.zombie ? 1 : 0) - (b.d.zombie ? 1 : 0));   // зомби — вниз своего окна
    return GROUPS.map((g) => {
      const items = byType(g.types);
      if (!items.length) return '';
      const head = `${g.title} <span class="muted">${items.length}</span>`;
      return this._collapsibleSection(g.key, head, items.map(({ d, i }) => this._deviceRow(d, i, g.key)).join(''));
    }).join('');
  }

  _deviceRow(d, i, kind) {
    // C3 (v1.2.17): кружок = ТОЛЬКО связь, и считает его ОДИН источник — `_devOnline` (им же
    // `_syncStates` перекрашивает этот кружок сразу после отрисовки). Раньше здесь была своя
    // формула: `!d.zombie && (d.status === 'online' || …)` — она (а) тащила zombie в кружок и
    // (б) смотрела снимок `d.status`, а не состояние сущности → две оценки расходились, и точка
    // МИГАЛА красный↔зелёный при каждой перерисовке.
    // Зомби — ОТДЕЛЬНАЯ ось, у него своя визуализация (класс `.row.zombie` + чип «зомби»), и в
    // кружок связи он лезть не должен: устройство не становится зомби от оффлайна — только от
    // скана, не нашедшего его на шине.
    const online = this._devOnline(d);
    const acts = [];
    if (kind === 'light') {
      acts.push(`<button class="ibtn tg" data-act="lampToggle" data-tg="${i}" data-idx="${i}" title="Вкл/выкл">${svg(ICONS.bulbOff)}</button>`);
      acts.push(`<button class="ibtn" data-act="bright" data-idx="${i}" title="Яркость">${svg(ICONS.brightness)}</button>`);
    }
    if (kind === 'light' || kind === 'sensor') acts.push(`<button class="ibtn" data-act="params" data-idx="${i}" title="Параметры">${svg(ICONS.param)}</button>`);
    if (kind === 'panel') acts.push(`<button class="mini" data-act="panelBind" data-idx="${i}" title="Привязки кнопок (на контроллере)">Привязки</button>`);
    if (kind === 'sensor' && String(d.devType) === '0201') acts.push(`<button class="mini" data-act="sensorBind" data-idx="${i}" title="Привязки датчика (на контроллере)">Привязки</button>`);
    if (kind === 'sensor' && String(d.devType) === '0202') acts.push(`<button class="mini" data-act="luxKeep" data-idx="${i}" title="Автояркость 恒照 (на контроллере)">Автояркость</button>`);
    acts.push(`<button class="ibtn" data-act="identify" data-idx="${i}" title="Identify">${svg(ICONS.flash)}</button>`);
    acts.push(`<button class="ibtn" data-act="addr" data-idx="${i}" title="Сменить адрес">${svg(ICONS.addr)}</button>`);
    if (d.zombie) acts.push(`<button class="mini danger" data-act="forget" data-idx="${i}" title="Забыть: снести запись и очистить хранилища (необратимо)">Забыть</button>`);
    return `<div class="row${d.zombie ? ' zombie' : ''}">
      <div class="row-main">
        <span class="dot ${online ? 'on' : 'off'}" data-dot="${i}" title="${online ? 'online' : 'offline'}"></span>
        <div class="row-txt">
          <div class="name-row"><b>${this._esc(d.name || d.typeName + ' ' + d.address)}</b>
            ${d.orphan
              ? '<span class="zchip" title="ОСИРОТЕВШИЙ: его DALI-адрес занят другим устройством, а сам он на шине не найден. Сущности сохранены и недоступны. Если это был прогрев шины или мис-энумерация — вернётся на следующем скане и запись уйдёт сама. Иначе снести вручную кнопкой «Забыть».">осиротевший</span>'
              : (d.zombie ? '<span class="zchip" title="Не найден последним сканом. Запись сохранена — вернётся при ре-скане или удали вручную.">зомби</span>' : '')}
            <button class="pen" data-act="rename" data-idx="${i}" title="Переименовать">${svg(ICONS.pencil, 'sm')}</button></div>
          <span class="muted">${d.typeName} · ch${d.channel}/${d.address}${d.devSn ? ' · ' + this._esc(d.devSn) : ''}</span>
          <span class="live" data-live="${i}"></span>
          ${kind === 'light' ? `<span class="enb" data-enb="${i}"></span>` : ''}</div>
      </div>
      <div class="row-act">${acts.join('')}</div></div>`;
  }

  // ── КРОСС-ШЛЮЗОВЫЕ ГРУППЫ ──────────────────────────────────────────────────
  // Отдельная модель (docs/CROSS_GATEWAY.md §2): один и тот же groupId+имя заводится на
  // КАЖДОМ контроллере, каждому — только его лампы. Поэтому в диалоге выбираются шлюзы,
  // а лампы грузятся по каждому отдельно; номер берём из ПЕРЕСЕЧЕНИЯ свободных.
  _openCreateXGroup() {
    this._state.modal = { kind: 'xgroup', uid: null, name: '', channel: 0, groupId: null,
                          gws: [], lamps: {}, checked: new Set(), free: [], loading: false };
    this._render();
  }

  async _openEditXGroup(uid) {
    const g = (this._state.xgroups || []).find((x) => x.uid === uid);
    if (!g) { this._toast('Кросс-группа не найдена — обнови список', true); return; }
    const checked = new Set((g.members || []).map((m) => `${m.gwSnObj}|${m.channel}|${m.address}`));
    this._state.modal = { kind: 'xgroup', uid, name: g.name || '', channel: g.channel,
                          groupId: g.groupId, gws: [...(g.participants || [])], lamps: {},
                          checked, free: [], loading: true };
    this._render();
    for (const gw of this._state.modal.gws) await this._xgwLoadLamps(gw);
    this._state.modal.loading = false;
    this._render();
  }

  async _xgwLoadLamps(gw) {
    const m = this._state.modal;
    if (!m || m.lamps[gw]) return;
    try {
      const d = await this._ws({ type: 'arvid_dali_center/devices', gw_sn: gw });
      m.lamps[gw] = (d.devices || []).filter((x) => String(x.devType || '').startsWith('01'));
    } catch (e) {
      m.lamps[gw] = [];
      this._toast(`Лампы ${this._gwTail(gw)} не загрузились: ${e.message}`, true);
    }
  }

  async _xgwPick(gw, on) {
    const m = this._state.modal;
    if (!m) return;
    if (on) {
      if (!m.gws.includes(gw)) m.gws.push(gw);
      m.loading = true; this._render();
      await this._xgwLoadLamps(gw);
      m.loading = false;
    } else {
      m.gws = m.gws.filter((x) => x !== gw);
      // снять отметки ламп этого шлюза — иначе уедут в состав уже невыбранного контроллера
      [...m.checked].forEach((k) => { if (k.startsWith(gw + '|')) m.checked.delete(k); });
    }
    await this._xgwLoadFree();
    this._render();
  }

  async _xgwLoadFree() {
    const m = this._state.modal;
    if (!m || m.gws.length < 2) { m.free = []; return; }
    try {
      const r = await this._ws({ type: 'arvid_dali_center/group_slots', gw_sns: m.gws });
      m.free = r.free || [];
      // при создании подставляем первый свободный У ВСЕХ; при правке номер не трогаем
      if (m.uid == null && (m.groupId == null || !m.free.includes(m.groupId))) {
        m.groupId = m.free.length ? m.free[0] : null;
      }
    } catch (e) { console.error('arvid-dali-panel: group_slots', e); }
  }

  _xgroupMembers() {
    const m = this._state.modal;
    return [...m.checked].map((k) => {
      const [gw, ch, addr] = k.split('|');
      return { gwSnObj: gw, devType: '0101', channel: +ch, address: +addr };
    });
  }

  async _saveXGroup() {
    const m = this._state.modal;
    const members = this._xgroupMembers();
    const gws = new Set(members.map((x) => x.gwSnObj));
    if (gws.size < 2) { this._toast('Нужны лампы минимум ДВУХ контроллеров — иначе это обычная группа', true); return; }
    if (!m.name.trim()) { this._toast('Задай имя группы', true); return; }
    if (m.groupId == null) { this._toast('Нет номера, свободного на всех контроллерах', true); return; }
    try {
      const r = m.uid
        ? await this._ws({ type: 'arvid_dali_center/set_cross_group_members', uid: m.uid, name: m.name.trim(), members })
        : await this._ws({ type: 'arvid_dali_center/create_cross_group', channel: m.channel, groupId: m.groupId, name: m.name.trim(), members });
      const warn = (r.warnings || []).join('; ');
      this._toast(r.ok ? (warn ? 'Сохранено, но ⚠ ' + warn : 'Кросс-группа сохранена')
                       : 'Не применено: ' + (warn || 'см. журнал'), !r.ok || !!warn);
      this._state.modal = null;
      this._loadDevices();
    } catch (e) { this._toast('Ошибка: ' + e.message, true); }
  }

  async _delXGroup(uid) {
    const g = (this._state.xgroups || []).find((x) => x.uid === uid);
    if (!g) return;
    if (!confirm(`Удалить кросс-группу «${g.name || g.groupId}» со ВСЕХ контроллеров?`)) return;
    try {
      const r = await this._ws({ type: 'arvid_dali_center/del_cross_group', uid });
      const warn = (r.warnings || []).join('; ');
      this._toast(warn ? 'Удалена, но ⚠ ' + warn : 'Кросс-группа удалена', !!warn);
      this._loadDevices();
    } catch (e) { this._toast('Ошибка: ' + e.message, true); }
  }

  _xgroup(uid) { return (this._state.xgroups || []).find((x) => x.uid === uid) || null; }

  _xgroupToggle(uid) {
    const g = this._xgroup(uid);
    if (!g) return;
    // сущность есть → штатный light.toggle (состояние агрегируется из ламп-членов);
    // до её появления — запасной путь веером через WS, оптимистично
    if (g.entity_id) { this._hass.callService('light', 'toggle', { entity_id: g.entity_id }); return; }
    const on = !this._state.groupState['x:' + uid];
    this._state.groupState['x:' + uid] = on;
    this._syncStates();
    this._xgroupWrite(uid, on);
  }

  _xgroupBright(uid) {
    const g = this._xgroup(uid);
    if (!g) return;
    if (g.entity_id) { this._openBright({ type: 'light', entity: g.entity_id }); return; }
    this._toast('Сущность группы ещё не создана — перезагрузите HA', true);
  }

  async _xgroupWrite(uid, on) {
    const prop = [{ dpid: 20, dataType: 'bool', value: on }];
    try {
      const r = await this._ws({ type: 'arvid_dali_center/cross_group_write', uid, property: prop });
      const warn = (r.warnings || []).join('; ');
      if (warn) this._toast(`Отправлено ${r.sent}/${r.total} ⚠ ${warn}`, true);
    } catch (e) { this._toast('Ошибка: ' + e.message, true); }
  }

  _groupsPanel() {
    const s = this._state;
    if (s.loading) return '';
    // ⚠ v1.2.16: действия адресуются ИДЕНТИЧНОСТЬЮ (`data-gkey` = channel:groupId), а НЕ индексом.
    // Индекс — летучий ключ: после каждого удаления `_loadDevices()` перестраивает `_state.groups`
    // и перерисовывает список, индексы съезжают. Быстрые клики по крестикам попадали уже в ДРУГУЮ
    // группу или в `undefined` (и `_delGroup` падал на `g.name` ДО try) → строки исчезали от
    // перерисовки, а команда не уходила: три клика — одно удаление, остальные группы оставались
    // на контроллере. `data-gtg`/`data-glive` — индексные СПЕЦИАЛЬНО: это DOM-якоря для
    // `_syncStates` внутри ОДНОЙ отрисовки, они не адресуют сущность.
    const rows = s.groups.map((g, i) => {
      const addrs = (g.members || []).map((m) => m.address).filter((a) => a != null);
      const comp = addrs.length ? `лампы: ${addrs.join(', ')}` : 'состав пуст';
      const gk = `${g.channel}:${g.groupId}`;
      return `<div class="row">
      <div class="row-main"><div class="row-txt">
        <div class="name-row"><b>${this._esc(g.name || 'Группа ' + g.groupId)}</b>
          <button class="pen" data-act="groupRename" data-gkey="${gk}" title="Переименовать">${svg(ICONS.pencil, 'sm')}</button></div>
        <span class="muted">ch${g.channel} · id ${g.groupId} · ${comp}</span>
        <span class="live" data-glive="${i}"></span></div></div>
      <div class="row-act">
        <button class="ibtn tg" data-act="groupToggle" data-gtg="${i}" data-gkey="${gk}" title="Вкл/выкл">${svg(ICONS.bulbOff)}</button>
        <button class="ibtn" data-act="groupBright" data-gkey="${gk}" title="Яркость">${svg(ICONS.brightness)}</button>
        <button class="mini" data-act="editGroup" data-gkey="${gk}" title="Состав группы">Состав</button>
        <button class="ibtn" data-act="groupParam" data-gkey="${gk}" title="Параметры ламп группы">${svg(ICONS.param)}</button>
        <button class="ibtn danger" data-act="delgroup" data-gkey="${gk}" title="Удалить">×</button>
      </div></div>`;
    }).join('');
    // ── КРОСС-ШЛЮЗОВЫЕ группы: тот же список, отличаются бейджем и составом по шлюзам ──
    // Адресуются `uid` (зафиксирован при создании) — не индексом и не channel:groupId:
    // один и тот же номер живёт на нескольких контроллерах, идентичность даёт только uid.
    const xrows = (s.xgroups || []).map((g) => {
      const byGw = {};
      (g.members || []).forEach((m) => {
        const k = m.gwSnObj || '';
        (byGw[k] = byGw[k] || []).push(m.address);
      });
      const comp = (g.participants || []).map((gw) => {
        const addrs = (byGw[gw] || byGw[String(gw).toUpperCase()] || []).filter((a) => a != null);
        return `<div class="muted">${this._esc(this._gwTail(gw))}: ${addrs.length ? addrs.join(', ') : '—'}</div>`;
      }).join('');
      return `<div class="row">
      <div class="row-main"><div class="row-txt">
        <div class="name-row"><b>${this._esc(g.name || 'Группа ' + g.groupId)}</b>
          <span class="xbadge" title="Группа живёт на нескольких контроллерах">кросс</span></div>
        <span class="muted">ch${g.channel} · id ${g.groupId} · ${(g.participants || []).length} контроллера</span>
        ${comp}</div></div>
      <div class="row-act">
        <button class="ibtn tg" data-act="xgroupToggle" data-uid="${this._esc(g.uid)}" data-xtg="${this._esc(g.uid)}" title="Вкл/выкл">${svg(ICONS.bulbOff)}</button>
        <button class="ibtn" data-act="xgroupBright" data-uid="${this._esc(g.uid)}" title="Яркость">${svg(ICONS.brightness)}</button>
        <button class="mini" data-act="editXGroup" data-uid="${this._esc(g.uid)}" title="Состав группы">Состав</button>
        <button class="ibtn danger" data-act="delXGroup" data-uid="${this._esc(g.uid)}" title="Удалить">×</button>
      </div></div>`;
    }).join('');
    const total = s.groups.length + (s.xgroups || []).length;
    const head = `DALI-группы <span class="muted">${total}</span><button class="mini" data-act="groupReload" title="Перечитать состав с контроллера">обновить</button><button class="mini" data-act="createGroup">+ группа</button><button class="mini" data-act="createXGroup" title="Группа из ламп нескольких контроллеров">+ кросс</button>`;
    return this._collapsibleSection('group', head, (rows + xrows) || '<div class="muted" style="padding:8px 6px">Групп нет — создайте из ламп.</div>');
  }

  _modal() {
    const m = this._state.modal;
    if (!m) return '';
    let title = '', body = '';
    if (m.kind === 'createGroup' || m.kind === 'editGroup') {
      const isEdit = m.kind === 'editGroup';
      title = isEdit ? 'Состав группы' : 'Новая DALI-группа';
      const chk = m.checked || new Set();   // ключи выбранных членов: ch/addr
      const rows = m.lights.map((d, i) => {
        const on = chk.has(`${d.channel}/${d.address}`) ? ' checked' : '';
        return `<label class="chk"><input type="checkbox" data-member="${i}"${on}><span>${d.name || d.typeName + ' ' + d.address} <em>ch${d.channel}/${d.address}</em></span></label>`;
      }).join('') || '<div class="muted">Нет ламп на шлюзе</div>';
      const head = isEdit
        ? `<div class="grid"><label class="fld"><span>Имя</span><input id="grpName" type="text" value="${(m.name || '').replace(/"/g, '&quot;')}" placeholder="Группа"></label><label class="fld"><span>Номер</span><input id="grpId" type="number" value="${m.groupId}" disabled></label></div><div class="muted" style="margin:2px 0 4px">Снятые лампы убираются, отмеченные добавляются: группа пересоздаётся целиком.</div>`
        : `<div class="grid"><label class="fld"><span>Имя</span><input id="grpName" type="text" value="${m.name}" placeholder="Группа"></label><label class="fld"><span>Номер (0-15)</span><input id="grpId" type="number" min="0" max="15" value="${m.groupId}"></label></div>`;
      // список выше базовых 240px — состав групп это 20+ ламп, скролл под большой набор
      body = `${head}<div class="chk-h">Лампы группы</div><div class="chk-list" style="max-height:60vh;overflow:auto">${rows}</div>`;
    } else if (m.kind === 'xgroup') {
      // КРОСС-ШЛЮЗОВАЯ группа: сперва выбираем контроллеры, потом их лампы. Номер — только
      // из СВОБОДНЫХ У ВСЕХ (пересечение): занятый хоть у одного положит addGroup поверх
      // чужой группы, и сверка этого не покажет.
      title = m.uid ? 'Состав кросс-группы' : 'Новая кросс-шлюзовая группа';
      const gws = this._state.gateways || [];
      const gwRows = gws.map((g) => {
        const on = m.gws.includes(g.gwSn) ? ' checked' : '';
        return `<label class="chk"><input type="checkbox" data-act="xgwPick" data-gw="${this._esc(g.gwSn)}"${on}><span>${this._esc(g.name || g.gwSn)} <em>${this._esc(this._gwTail(g.gwSn))}</em></span></label>`;
      }).join('') || '<div class="muted">Шлюзы не найдены</div>';
      const lampBlocks = m.gws.map((gw) => {
        const list = (m.lamps[gw] || []).map((d) => {
          const k = `${gw}|${d.channel}|${d.address}`;
          const on = m.checked.has(k) ? ' checked' : '';
          return `<label class="chk"><input type="checkbox" data-xmember="${this._esc(k)}"${on}><span>${this._esc(d.name || d.typeName + ' ' + d.address)} <em>ch${d.channel}/${d.address}</em></span></label>`;
        }).join('') || '<div class="muted">Ламп нет</div>';
        return `<div class="chk-h">${this._esc(this._gwTail(gw))} · ${this._esc(gw)}</div><div class="chk-list">${list}</div>`;
      }).join('');
      const idFld = m.uid
        ? `<label class="fld"><span>Номер</span><input type="number" value="${m.groupId}" disabled></label>`
        : `<label class="fld"><span>Номер (свободен у всех)</span><select id="xgrpId">${(m.free || []).map((n) => `<option value="${n}"${n === m.groupId ? ' selected' : ''}>${n}</option>`).join('') || '<option value="">нет свободных</option>'}</select></label>`;
      const hint = m.gws.length < 2
        ? '<div class="muted" style="margin:4px 0">Отметь минимум ДВА контроллера — иначе это обычная группа.</div>'
        : (m.free && m.free.length ? '' : '<div class="muted" style="margin:4px 0">⚠ Свободных номеров, общих для всех выбранных контроллеров, нет.</div>');
      body = `<div class="grid"><label class="fld"><span>Имя</span><input id="xgrpName" type="text" value="${(m.name || '').replace(/"/g, '&quot;')}" placeholder="Например: зал_общий"></label>${idFld}</div>
        ${hint}<div class="chk-h">Контроллеры</div><div class="chk-list">${gwRows}</div>
        ${m.loading ? '<div class="muted" style="padding:6px">загрузка ламп…</div>' : lampBlocks}`;
    } else if (m.kind === 'panelBind' || m.kind === 'sensorBind') {
      const isSensor = m.kind === 'sensorBind';
      title = (isSensor ? `Событие · ${m.ev}` : `Кнопка ${m.keyNo} · ${GESTURE_LABEL[m.dpid] || ('жест ' + m.dpid)}`) + (m.replace ? ' · заменить' : '');
      // выбор КОНТРОЛЛЕРА цели (cross-gateway) — только для привязок кнопок (m.targetGw задан)
      const gws = this._state.gateways || [];
      const ctrlSel = m.targetGw ? `<label class="fld fld-wide"><span>Контроллер</span><select id="bindCtrl">${gws.map((g) => `<option value="${this._esc(g.gwSn)}"${g.gwSn === m.targetGw ? ' selected' : ''}>${this._esc(g.gwSn)}</option>`).join('')}</select></label>` : '';
      // предвыбор текущей цели (группа devType 0401 / лампа)
      const cur = m.cur, isCurGrp = cur && String(cur.devType) === '0401';
      const grpSel = (gr) => isCurGrp && gr.channel === cur.channel && gr.groupId === cur.address ? ' selected' : '';
      const lampSel = (d) => cur && !isCurGrp && String(d.devType) === String(cur.devType) && d.channel === cur.channel && d.address === cur.address ? ' selected' : '';
      const opts = m.loadingTargets ? '<option>загрузка…</option>'
        : ([   // ГРУППЫ сверху, затем лампы
          ...(m.groups || []).map((gr, i) => `<option value="group:${i}"${grpSel(gr)}>группа: ${this._esc(gr.name || gr.groupId)}</option>`),
          ...(m.xgroups || []).map((x, i) => `<option value="xgroup:${i}">⇄ кросс-группа: ${this._esc(x.name || x.groupId)} (${(x.participants || []).length} контроллера)</option>`),
          ...(m.lamps || []).map((d, i) => `<option value="lamp:${i}"${lampSel(d)}>лампа: ${this._esc(d.name || d.typeName + ' ' + d.address)}</option>`),
        ].join('') || '<option value="">нет ламп/групп</option>');
      // предвыбор ДЕЙСТВИЯ и яркости из текущей цели
      const curAct = cur ? this._propToAction(cur.property) : 'on';
      const aopt = (v, label) => `<option value="${v}"${curAct === v ? ' selected' : ''}>${label}</option>`;
      // диммирование (H1c) — только для панелей: «удержание→плавно ярче/темнее» рулит
      // контроллер, не забивая шину; на датчике (движение) такие действия не нужны.
      const dimOpts = isSensor ? '' : aopt('dimup', 'Плавно ярче (удерж.)') + aopt('dimdown', 'Плавно темнее (удерж.)');
      const curBri = cur && (cur.property || []).find((x) => x.dpid === 22);
      const briVal = curBri && curBri.value != null ? Math.round(curBri.value / 1000 * 100) : 100;
      body = `<div class="grid">
        ${ctrlSel}
        <label class="fld fld-wide"><span>Цель</span><select id="bindTarget">${opts}</select></label>
        <label class="fld"><span>Действие</span><select id="bindAction" onchange="this.closest('.dialog').querySelector('#briFld').style.display=this.value==='onbri'?'':'none'">${aopt('on', 'Включить')}${aopt('off', 'Выключить')}${isSensor ? '' : aopt('toggle', 'Переключить (вкл/выкл)')}${aopt('onbri', 'Вкл + яркость')}${dimOpts}</select></label>
        <label class="fld" id="briFld" style="display:${curAct === 'onbri' ? '' : 'none'}"><span>Яркость %</span><input id="bindBri" type="number" min="1" max="100" value="${briVal}"></label>
      </div><div class="muted" style="margin-top:8px">${m.replace ? 'Заменит ВСЕ цели ' + (isSensor ? 'этого события' : 'этой кнопки/жеста') : 'Добавит цель к ' + (isSensor ? 'этому событию' : 'кнопке/жесту')}. Работает на контроллере без HA.</div>`;
    } else if (m.kind === 'scanMode') {
      title = 'Опрос шины';
      body = `<div class="grid">
        <button class="btn primary scan-mode" data-act="scanRun" data-mode="manual">${svg(ICONS.scan)} Скан — перечитать шину</button>
        <button class="btn ghost scan-mode" data-act="scanRun" data-mode="auto">${svg(ICONS.flash)} Разрешить конфликты — развести дубли адресов</button></div>
        <div class="muted" style="margin-top:10px"><b>Скан</b> — физический опрос шины: перечитывает состав, конфликтные адреса выводит отдельной пачкой (адреса не трогает). Устройства с конфликтных адресов НЕ добавляются: пока адрес занят двумя, данные шлюза о них недостоверны.<br><b>Разрешить конфликты</b> — это не скан, а операция: шлюз сам переназначает дублирующиеся адреса (разрушающе), после чего шина перечитывается автоматически.</div>`;
    } else if (m.kind === 'resolve') {
      // F2 (v1.1.6): у РАЗВЕДЕНИЯ конфликтов — свой экран. Раньше оно показывалось модалкой
      // скана со счётчиком «Найдено: 0» и пустым списком → выглядело как сломавшийся скан.
      // Это не скан: шлюз физически перекомиссирует шину (переназначает дубли адресов) и
      // устройств не перечисляет ВОВСЕ. Список наполнит обычный скан, который идёт следом.
      title = 'Разрешение конфликтов адресов';
      body = `
        <div class="muted">Шлюз переназначает дублирующиеся короткие адреса — это физическая
        перекомиссия DALI-шины, она идёт <b>десятки секунд</b>. Устройств эта операция
        <b>не перечисляет</b>: сразу после неё шина будет перечитана обычным сканом.</div>
        <div class="logbox dlgbox" style="margin-top:8px" id="scanFound">${this._logRows()}</div>`;
    } else if (m.kind === 'scan') {
      title = 'Сканирование шины';
      const nf = this._state.scanLog.filter((x) => x.kind === 'found').length;
      const nc = this._state.scanConflicts.length;
      const dis = this._state.scanning ? 'disabled' : '';
      body = `
        <div class="muted" id="scanStatus">${this._state.scanning ? 'Идёт сканирование…' : 'Скан завершён'}</div>
        <div class="panel-h" style="padding:8px 2px 4px">Найдено <span class="muted" id="scanFoundCount">${nf}</span></div>
        <div class="logbox dlgbox" id="scanFound">${this._logRows()}</div>
        <div id="scanConfSect" style="display:${nc ? '' : 'none'}">
          <div class="panel-h" style="padding:10px 2px 4px;color:#c2410c">Конфликтные адреса <span class="muted" id="scanConfCount">${nc}</span>
            <button class="mini" data-act="resolveConflicts" ${dis} title="Шлюз сам переназначит дублирующиеся адреса, затем шина будет перечитана">Разрешить конфликты</button></div>
          <div class="logbox dlgbox" id="scanConf" style="border-color:#fed7aa;background:#fff7ed">${this._conflictRows()}</div>
        </div>
        <div class="muted" style="margin-top:8px">Шина прогревается: если найдено меньше ожидаемого — «Сканировать заново».</div>`;
    } else if (m.kind === 'bright') {
      title = 'Яркость';
      body = `<div class="bri"><input id="briRange" type="range" min="1" max="100" value="${m.value}" oninput="this.nextElementSibling.textContent=this.value+'%'"><span class="bri-val">${m.value}%</span></div>`;
    } else if (m.kind === 'rename' || m.kind === 'renameGroup') {
      const isGrp = m.kind === 'renameGroup';
      const subj = isGrp ? ('группы ' + (m.g.name || m.g.groupId)) : ((m.dev.typeName || '') + ' ' + (m.dev.address ?? ''));
      title = 'Имя · ' + subj;
      body = `<div class="grid"><label class="fld fld-wide"><span>Имя</span><input id="renameInput" type="text" value="${(m.name || '').replace(/"/g, '&quot;')}" placeholder="Введите имя" autofocus></label></div><div class="muted" style="margin-top:8px">Имя задаёт подпись и entity_id ${isGrp ? 'light-сущности группы' : 'сущностей устройства'}.</div>`;
    } else if (m.kind === 'multiParam') {
      // массовая настройка: вкладки класса (лампы/датчики/панели) + список устройств + форма по типу
      const cls = m.cls || 'lamp';
      title = 'Массовая настройка';
      const tabs = [['lamp', 'Лампы'], ['sensor', 'Датчики'], ['panel', 'Панели']]
        .map(([c, t]) => `<button class="btn ${c === cls ? 'primary' : 'ghost'} mini" data-act="mpCls" data-cls="${c}">${t}</button>`).join('');
      const devs = this._mpDevs(cls);
      const rows = devs.map((d, i) => `<label class="chk"><input type="checkbox" data-mp="${i}"><span>${d.name || d.typeName + ' ' + d.address} <em>ch${d.channel}/${d.address}</em></span></label>`).join('') || '<span class="muted">Нет устройств этого типа</span>';
      const selAll = `<label class="chk"><input type="checkbox" onclick="this.closest('.dialog').querySelectorAll('[data-mp]').forEach(c=>c.checked=this.checked)"><span><b>Выбрать все (${devs.length})</b></span></label>`;
      let form;
      if (cls === 'panel') {
        // привязка кнопки → одинаковая цель на все выбранные панели.
        // Цели — с ВЫБРАННОГО контроллера (m.lamps/m.groups грузит _loadBindTargets),
        // а не с активного: цель может жить на другом шлюзе (gwSnObj)
        const gwsMp = this._state.gateways || [];
        const ctrlSelMp = `<label class="fld fld-wide"><span>Контроллер цели</span><select id="bindCtrl">${gwsMp.map((g) => `<option value="${this._esc(g.gwSn)}"${g.gwSn === m.targetGw ? ' selected' : ''}>${this._esc(g.gwSn)}</option>`).join('')}</select></label>`;
        const opts = m.loadingTargets ? '<option>загрузка…</option>'
          : ([...(m.lamps || []).map((d, i) => `<option value="lamp:${i}">лампа: ${this._esc(d.name || d.typeName + ' ' + d.address)}</option>`),
            ...(m.groups || []).map((gr, i) => `<option value="group:${i}">группа: ${this._esc(gr.name || gr.groupId)}</option>`),
            ...(m.xgroups || []).map((x, i) => `<option value="xgroup:${i}">⇄ кросс-группа: ${this._esc(x.name || x.groupId)} (${(x.participants || []).length} контроллера)</option>`)].join('') || '<option value="">нет ламп/групп</option>');
        form = `<div class="chk-h">Привязка кнопки (одинаковая на все выбранные панели)</div>
          <div class="grid">
            ${ctrlSelMp}
            <label class="fld"><span>Кнопка</span><select id="mpKey">${[1, 2, 3, 4, 5, 6, 7, 8].map((n) => `<option>${n}</option>`).join('')}</select></label>
            <label class="fld"><span>Жест</span><select id="mpDpid"><option value="1">клик</option><option value="2">удержание</option><option value="3">двойной</option></select></label>
            <label class="fld fld-wide"><span>Цель</span><select id="bindTarget">${opts}</select></label>
            <label class="fld"><span>Действие</span><select id="bindAction" onchange="this.closest('.dialog').querySelector('#briFld').style.display=this.value==='onbri'?'':'none'"><option value="on">Включить</option><option value="off">Выключить</option><option value="onbri">Вкл + яркость</option><option value="dimup">Плавно ярче (удерж.)</option><option value="dimdown">Плавно темнее (удерж.)</option></select></label>
            <label class="fld" id="briFld" style="display:none"><span>Яркость %</span><input id="bindBri" type="number" min="1" max="100" value="100"></label>
          </div>
          <label class="chk"><input type="checkbox" id="mpReplace"><span>Заменить существующие цели ячейки</span></label>
          <div class="muted" style="margin-top:4px">Кнопка/жест должны существовать на панели (иначе пропустится). Работает на контроллере без HA.</div>`;
      } else {
        const fields = cls === 'sensor' ? SENSOR_FIELDS : LAMP_FIELDS;
        form = `<div class="chk-h">Параметры ${cls === 'sensor' ? 'датчиков' : 'ламп'}</div>${this._paramGrid(fields, {})}`
          + (cls === 'sensor' ? '<div class="muted" style="margin-top:4px">⚠ Зона, чувствительность и время присутствия есть только у датчиков ДВИЖЕНИЯ — датчикам освещённости они не отправляются.</div>' : '')
          + (cls === 'sensor' ? this._mpSensorActions(m) : '');
      }
      // ЛАМПЫ: область действия — отмеченным (адресный батч) или ВСЕМУ контроллеру одной
      // командой (броадкаст devType FFFF, v1.2.44). Датчики/панели броадкаста не имеют —
      // у них свои команды (setSensorArgv/addPanelObj), там переключателя нет.
      const scope = cls === 'lamp' ? (m.scope || 'targets') : 'targets';
      const scopeSel = cls !== 'lamp' ? '' : `<div class="mp-tabs" style="margin-bottom:4px">${
        [['targets', `Отмеченным (${devs.length} в списке)`], ['gateway', 'Всем лампам контроллера']]
          .map(([s, t]) => `<button class="btn ${s === scope ? 'primary' : 'ghost'} mini" data-act="mpScope" data-scope="${s}">${t}</button>`).join('')}</div>`;
      const listPart = scope === 'gateway'
        ? `<div class="muted" style="margin:2px 0 6px">Параметры уйдут ОДНОЙ командой всем устройствам контроллера <b>${this._esc(this._state.activeGw)}</b> — отмечать ничего не нужно. Так делает и DALI Center («на контроллер»): это быстрее и не грузит шину перебором ламп.<br>⚠ Подтверждение — только <b>ack</b> контроллера: что именно приняла каждая лампа, прочитать нечем.</div>`
        : `<div class="muted" style="margin:2px 0 6px">Отметь устройства и задай ${cls === 'panel' ? 'привязку' : 'параметры'} — применится всем выбранным.</div>
        <div class="chk-list">${selAll}${rows}</div>`;
      body = `<div class="mp-tabs">${tabs}</div>${scopeSel}${listPart}${form}`;
    } else if (m.kind === 'registry') {
      // пустые карточки устройств: наследие смены идентичности (devSn → адресный ключ и наоборот)
      title = 'Реестр HA · пустые карточки устройств';
      const list = m.orphans || [];
      const dead = list.filter((o) => !o.live);
      const alive = list.filter((o) => o.live);
      const rows = dead.map((o) => `<label class="chk"><input type="checkbox" data-orph="${this._esc(o.device_id)}" checked><span>${this._esc(o.name || o.identifiers.join(', '))} <em>${this._esc(o.identifiers.join(', '))}${o.entities ? ` · осиротевших сущностей: ${o.entities}` : ''}</em></span></label>`).join('');
      body = m.loading ? '<div class="muted">Читаю реестр…</div>'
        : m.error ? `<div class="muted">Ошибка: ${this._esc(m.error)}</div>`
        : !list.length ? '<div class="muted">Пустых карточек нет — реестр чист.</div>'
        : `<div class="muted" style="margin-bottom:6px">Карточки устройств без единой ЖИВОЙ сущности — пустые, либо с сущностями, которые интеграция больше не создаёт («этот объект больше не предоставляется…»).<br>⚠ <b>Сначала «Сканировать»</b>: физически живое устройство после скана поднимет свои сущности само, и в этот список не попадёт. Чистить — то, что осталось после скана.<br>⚠ Снятие в реестре HA <b>мягкое</b>: имя уезжает в корзину и вернулось бы вместе с записью — поэтому мы заодно чистим наши хранилища имён и параметров по этому ключу.</div>
          <div class="chk-list">${rows}</div>
          ${alive.length ? `<div class="muted" style="margin-top:6px">⚠ Ещё ${alive.length} карточк(и) пусты, но их устройства ЖИВЫ в кеше шлюза — такие не снимаем: вернутся сканом, а имя воскреснет. Для живых — «Забыть» в строке устройства.</div>` : ''}
          <div style="margin-top:8px"><button class="btn danger" data-act="registryClean">Снять отмеченные (${dead.length})</button></div>`;
    } else if (m.kind === 'trash') {
      title = 'Корзина реестров HA';
      const items = m.items || [];
      const forever = m.forever || 0;
      const rows = items.map((e) => `<div class="chk"><span>${this._esc(e.entity_id)}
        <em>${this._esc(e.unique_id)}${e.forever ? ' · не истечёт сама' : ''}</em></span></div>`).join('');
      body = m.loading ? '<div class="muted">Читаю корзину…</div>'
        : m.error ? `<div class="muted">Ошибка: ${this._esc(m.error)}</div>`
        : !items.length ? '<div class="muted">Корзина чиста — наших записей в ней нет.</div>'
        : `<div class="muted" style="margin-bottom:6px">Удалённые записи, которые HA придержал у себя: при появлении того же устройства он вернёт из них <b>entity_id, имя, область и ярлыки</b>.<br>
          • <b>${items.length}</b> сущностей нашей интеграции (список ниже), из них <b>${forever}</b> не истекут сами никогда — штатная уборка (30 дней) считает только записи, оставшиеся без записи интеграции; остальные ${items.length - forever} HA уберёт сам.<br>
          • <b>${(m.devices || []).length}</b> карточек устройств — это ДРУГОЙ реестр; на одно устройство приходится 2–4 сущности, поэтому чисел два.<br>
          ⚠ Живые сущности не затрагиваются: чистится ТОЛЬКО корзина.</div>
          <div class="chk-list">${rows}</div>
          <div style="margin-top:8px"><button class="btn danger" data-act="trashPurge">Вымести всё: ${items.length} сущн. + ${(m.devices || []).length} карточек</button></div>`;
    } else if (m.kind === 'luxKeep') {
      title = 'Автояркость · ' + (m.dev.name || m.dev.typeName + ' ' + m.dev.address);
      // ПРЕДЗАПОЛНЕНИЕ из текущей конфигурации (контроллер её хранит — readSensor отдаёт
      // luxRange+цель). Раньше поля сбрасывались на дефолт при перезаходе («загадка»), хотя
      // автояркость настроена. luxRange=[min,max] → цель=(min+max)/2, допуск=(max-min)/2.
      const _lkEntries = (m.entries || []).filter((e) => e.luxRange && e.luxRange.length);
      const _e0 = _lkEntries[0];
      const _lr = _e0 && _e0.luxRange;
      const curTarget = _lr ? Math.round((_lr[0] + _lr[1]) / 2) : 100;
      const curTol = _lr ? Math.round((_lr[1] - _lr[0]) / 2) : 10;
      // Цель автояркости на контроллере — САМА ГРУППА (devType 0401, address = groupId),
      // поэтому читаем её прямо из readSensor (v1.2.37).
      const _tgt = (_e0 && (_e0.outputObj || [])[0]) || null;
      const curGid = (_tgt && String(_tgt.devType) === '0401') ? _tgt.address : null;
      const gopts = m.groups.map((g, i) => `<option value="${i}"${curGid != null && g.groupId === curGid ? ' selected' : ''}>${g.cross ? '⇄ ' : ''}${this._esc(g.name || ('Группа ' + g.groupId))} (id ${g.groupId}${g.cross ? `, кросс: ${g.parts} контроллера` : ''})</option>`).join('') || '<option value="">нет групп</option>';
      const cur = _lkEntries.map((e) => {
        const t = (e.outputObj || [])[0];
        const who = !t ? 'нет цели' : (String(t.devType) === '0401' ? ('группа id ' + t.address) : (e.outputObj.length + ' ламп'));
        return `dpid ${e.dpid}: luxRange ${JSON.stringify(e.luxRange)} → ${who}`;
      }).join('<br>') || '— не настроено';
      const _gName = (gid) => { const g = m.groups.find((x) => x.groupId === gid); return g ? (g.name || ('Группа ' + gid)) : ('группа id ' + gid); };
      const target0 = curGid != null
        ? `<div class="lk-target">Управляет: <b>${this._esc(_gName(curGid))}</b></div>`
        : '<div class="lk-target muted">Автояркость не настроена.</div>';
      // v1.2.25: тумблер «Активна» (мягко), режим сосуществования и окна работы (в столбик).
      const _mt = (m.mode && m.mode.type) || '';
      const _mtv = (m.mode && m.mode.timeValue != null && m.mode.timeValue >= 0) ? m.mode.timeValue : 600;
      const mopts = [['', 'по умолчанию (ordinary)'], ['ordinary', 'ordinary — датчик главнее'],
                     ['auto', 'auto — ручное на N секунд'], ['manual', 'manual — до выключения света']]
        .map(([v, t]) => `<option value="${v}"${_mt === v || (!_mt && !v) ? ' selected' : ''}>${t}</option>`).join('');
      const wrows = (m.windows || []).map((w, i) => {
        const [a, b] = String(w).split('-');
        return `<div class="lk-win"><input id="lkWinA${i}" type="time" value="${this._esc(a || '')}"><span>—</span><input id="lkWinB${i}" type="time" value="${this._esc(b || '')}"><button class="mini danger" data-act="lkDelWin" data-idx="${i}" title="Убрать окно">×</button></div>`;
      }).join('') || '<div class="muted">круглосуточно (окна не заданы)</div>';
      body = `<div class="muted" style="margin-bottom:6px">Нативный контур шлюза (恒照): датчик держит освещённость, сам подстраивая яркость группы. Без HA, без забивания шины.</div>
        ${target0}
        <label class="lk-toggle"><input type="checkbox" id="lkEnabled" data-act="lkToggle" ${m.enable ? 'checked' : ''}><span><b>Активна</b> — датчик управляет светом. Выключение ПРИОСТАНАВЛИВАЕТ (настройка на контроллере сохраняется).</span></label>
        <div class="grid">
          <label class="fld fld-wide"><span>Группа</span><select id="lkGroup">${gopts}</select></label>
          <label class="fld"><span>Целевой lux</span><input id="lkTarget" type="number" min="0" max="2000" value="${curTarget}"></label>
          <label class="fld"><span>Допуск (±lux)</span><input id="lkTol" type="number" min="0" max="2000" value="${curTol}"></label>
          <label class="fld fld-wide"><span>Режим при ручном управлении</span><select id="lkMode">${mopts}</select></label>
          <label class="fld" id="lkModeTimeFld" style="display:${_mt === 'auto' ? '' : 'none'}"><span>Вернуть через, с</span><input id="lkModeTime" type="number" min="0" max="86400" value="${_mtv}"></label>
        </div>
        <div class="chk-h">Окна работы<button class="mini" data-act="lkAddWin" title="Добавить окно" style="float:right">+</button></div>
        <div class="lk-wins">${wrows}</div>
        <div class="muted" style="margin-top:6px">Вне окон датчик светом не управляет. Окна — ТОЛЬКО внутри суток: ночь задаётся двумя окнами (22:00–23:59 и 00:00–06:00). Исполняет их сам шлюз по СВОИМ часам.</div>
        <div class="chk-h">Сейчас на датчике</div><div class="muted">${cur}</div>
        <div class="muted" style="margin-top:6px">Режим: <b>ordinary</b> — контур перебивает ручные команды сразу; <b>auto</b> — отдаёт управление человеку на N секунд; <b>manual</b> — до выключения света. ⚠ Режим — ТЕСТОВЫЙ (DALI Center его не выставляет, поведение уточняется на железе).</div>`;
    } else if (m.kind === 'rotaryBind') {
      title = 'Поворот → яркость · ' + (m.dev.name || m.dev.typeName + ' ' + m.dev.address);
      const b = m.binding;
      let curSel = '', curStep = 2, curThrottle = 0.8;
      if (b && b.target) {
        if (String(b.target.devType) === '0401') { const gi = m.groups.findIndex((g) => g.channel === b.target.channel && g.groupId === b.target.address); if (gi >= 0) curSel = 'group:' + gi; }
        else { const li = m.lamps.findIndex((d) => String(d.devType) === String(b.target.devType) && d.channel === b.target.channel && d.address === b.target.address); if (li >= 0) curSel = 'lamp:' + li; }
        curStep = Math.max(1, Math.round((b.step || 20) / 1000 * 100));
        if (b.throttle) curThrottle = b.throttle;
      }
      const opts = [...m.lamps.map((d, i) => `<option value="lamp:${i}"${('lamp:' + i) === curSel ? ' selected' : ''}>Лампа ${this._esc(d.name || d.address)}</option>`), ...m.groups.map((g, i) => `<option value="group:${i}"${('group:' + i) === curSel ? ' selected' : ''}>Группа ${this._esc(g.name || g.groupId)}</option>`)].join('') || '<option value="">нет целей</option>';
      body = `<div class="muted" style="margin-bottom:6px">Регулировка яркости поворотом — логика в HA (нативная привязка «следовать за ручкой» не умеет). Низ хода = ВЫКЛ, выше = ВКЛ + яркость. Группа — одной командой (не разворачиваем). Сохранение снимет битую нативную привязку поворота.</div>
        <div class="grid">
          <label class="fld fld-wide"><span>Цель</span><select id="roTarget">${opts}</select></label>
          <label class="fld"><span>Шаг (% / щелчок)</span><input id="roStep" type="number" min="1" max="20" value="${curStep}"></label>
          <label class="fld"><span>Таймаут отправки, с</span><input id="roThrottle" type="number" min="0.7" max="5" step="0.1" value="${curThrottle}"></label>
        </div>
        <div class="muted" style="margin-top:6px">Таймаут = пауза между командами при быстром кручении (бережёт шину; каждая команда запускает fade-разжигание ~0.7с, поэтому пол 0.7с). ${b ? 'Привязка активна.' : 'Привязки нет.'}</div>`;
    } else if (m.kind === 'groupParam') {
      title = 'Параметры группы · ' + (m.g.name || m.g.groupId);
      body = `<div class="muted" style="margin-bottom:6px">Параметры применятся всем лампам группы (состав читается с контроллера).</div>${this._paramGrid(LAMP_FIELDS, m.paramer || {})}`;
    } else if (m.kind === 'healthThresholds') {
      title = 'Пороги мониторинга';
      const t = (this._state.health && this._state.health.thresholds) || {};
      body = `<div class="grid">
        <label class="fld"><span>Залипшее движение, ч</span><input id="htMotion" type="number" min="0.1" step="0.5" value="${t.motion_stuck_h != null ? t.motion_stuck_h : 1}"></label>
        <label class="fld"><span>«Свободно» дольше, ч</span><input id="htClear" type="number" min="0.1" step="0.5" value="${t.clear_h != null ? t.clear_h : 7}"></label>
        <label class="fld"><span>Освещённость без изменений, ч</span><input id="htLux" type="number" min="0.1" step="0.5" value="${t.lux_stale_h != null ? t.lux_stale_h : 7}"></label>
        <label class="fld"><span>Терпение (грейс), мин</span><input id="htGrace" type="number" min="1" step="1" value="${t.grace_min != null ? t.grace_min : 5}"></label>
        <label class="fld"><span>Период обхода, мин</span><input id="htInterval" type="number" min="1" step="1" value="${t.interval_min != null ? t.interval_min : 20}"></label>
      </div><div class="muted" style="margin-top:6px"><b>Терпение</b> — сколько устройство должно провисеть в «оффлайн/неизвестно», прежде чем это назовут ошибкой (защита от транзиентов при рестарте). Оффлайн всплывает точно через него — обхода не ждёт.<br><b>Период обхода</b> — как часто пересматриваем ДОЛГИЕ пороги («залипло на N часов»). Обход читает только память, к шине не ходит, — на большом объекте его можно смело ставить редким.</div>`;
    } else if (m.kind === 'energyParams') {
      title = 'Параметры ламп';
      const e = this._state.energyPage || { lamps: [] };
      const selAll = `<label class="chk"><input type="checkbox" onclick="this.closest('.dialog').querySelectorAll('[data-en]').forEach(c=>c.checked=this.checked)"><span><b>Выбрать все (${e.lamps.length})</b></span></label>`;
      const lampRows = e.lamps.map((l) => `<label class="chk"><input type="checkbox" data-en="${l.devSn}"><span>${this._esc(l.name)} <em>${l.power_w != null ? l.power_w + ' Вт' : 'мощн. не задана'}${l.model ? ' · ' + this._esc(l.model) : ''}</em></span></label>`).join('');
      // Кривая драйвера (v1.1.3): форма «яркость→мощность». Список приходит из energy_data.
      const curveOpts = (e.curves || []).map((c) => `<option value="${this._esc(c.id)}">${this._esc(c.label)}</option>`).join('');
      body = `<div class="muted" style="margin-bottom:6px">Отметьте лампы и задайте мощность/кривую — применится всем выбранным.</div>
        <div class="chk-list" style="max-height:40vh;overflow:auto">${selAll}${lampRows}</div>
        <div class="grid" style="margin-top:8px">
          <label class="fld"><span>Полная мощность (при 100%), Вт</span><input id="enPower" type="number" min="0" step="0.1" placeholder="напр. 31.7"></label>
          <label class="fld"><span>Кривая драйвера</span><select id="enModel"><option value="">— не менять —</option>${curveOpts}</select></label>
        </div>
        <div class="muted" style="margin-top:6px"><b>Мощность</b> — своя у каждой лампы (зависит от длины ленты/светильника). <b>Кривая</b> — форма «яркость→мощность», общая на ТИП светильника (на объекте их обычно один-два). Без кривой считается линейно.</div>
        <div style="margin-top:8px"><button class="btn ghost" data-act="curvesReload">Перечитать кривые из файла</button></div>
        <div class="muted" style="margin-top:4px">Свои кривые — <code>/config/arvid_curves/curves.yaml</code>: таблица «яркость % → ватты», снятая ваттметром. Образец с методикой — <code>tools/curves.example.yaml</code>. Рестарт HA не нужен.</div>`;
    } else if (m.kind === 'gwSettings') {
      title = 'Сеть и имя · ' + (m.gwSn || '');
      if (m.loading) {
        body = '<div class="muted" style="padding:8px 2px">Чтение настроек шлюза…</div>';
      } else {
        const c = m.cur || {}, isStatic = (m.mode || 'dhcp') === 'static';
        const stDisp = isStatic ? '' : 'none';
        const curLine = `Текущее: ${this._esc(c.mode || '?')}${c.ipAddr ? ' · ' + this._esc(c.ipAddr) : ''}${c.mask ? ' / ' + this._esc(c.mask) : ''}${c.defaultGateway ? ' · шлюз ' + this._esc(c.defaultGateway) : ''}`;
        // Часы шлюза (v1.2.26): расписания датчиков исполняет ШЛЮЗ по СВОИМ часам — сбитые
        // часы = свет не вовремя, и снаружи это невидимо. Показываем факт + кнопка правки.
        const gwo = (this._state.gateways || []).find((g) => g.gwSn === m.gwSn) || {};
        const skew = gwo.gwTimeSkewS;
        const skewBad = skew != null && Math.abs(skew) > 60;
        const timeLine = gwo.gwTime
          ? `${this._esc(gwo.gwTime)}${gwo.gwTimezone ? ' · пояс ' + this._esc(gwo.gwTimezone) : ''}`
            + (skew != null ? ` · расхождение с HA <b style="color:${skewBad ? '#b91c1c' : '#166534'}">${skew > 0 ? '+' : ''}${Math.round(skew)} с</b>` : '')
          : 'не прочитаны (шлюз не ответил)';
        body = `<div class="grid">
          <label class="fld fld-wide"><span>Имя шлюза</span><input id="gwName" type="text" value="${this._esc(c.name || '')}" placeholder="Имя контроллера"></label>
        </div>
        <div class="chk-h">Часы контроллера</div>
        <div class="muted">${timeLine}</div>
        <div style="margin-top:6px"><button class="btn ghost" data-act="syncGwTime">Синхронизировать с HA</button></div>
        <div class="muted" style="margin-top:6px">Окна работы датчиков (расписание) исполняет САМ контроллер по своим часам${skewBad ? ' — <b style="color:#b91c1c">сейчас они врут, расписание сработает не вовремя</b>' : ''}. Часовой пояс не меняем — отправляем время в том поясе, который контроллер сообщил. Читаются при подключении.</div>
        <div class="chk-h">Сеть (IP)</div>
        <div class="grid">
          <label class="fld fld-wide"><span>Режим</span>
            <select id="gwMode" onchange="this.closest('.dialog').querySelectorAll('.gw-static').forEach(e=>e.style.display=this.value==='static'?'':'none')">
              <option value="dhcp"${isStatic ? '' : ' selected'}>DHCP (авто)</option>
              <option value="static"${isStatic ? ' selected' : ''}>Статический</option>
            </select></label>
          <label class="fld gw-static" style="display:${stDisp}"><span>IP-адрес</span><input id="gwIp" type="text" value="${this._esc(c.ipAddr || '')}" placeholder="192.168.8.40"></label>
          <label class="fld gw-static" style="display:${stDisp}"><span>Маска</span><input id="gwMask" type="text" value="${this._esc(c.mask || '')}" placeholder="255.255.255.0"></label>
          <label class="fld gw-static" style="display:${stDisp}"><span>Шлюз сети</span><input id="gwGw" type="text" value="${this._esc(c.defaultGateway || '')}" placeholder="192.168.8.1"></label>
        </div>
        <div class="muted" style="margin-top:8px">${curLine}.<br>«Имя» и «Сеть» сохраняются раздельно. После смены сети шлюз перенастроится — связь восстановится автоматически по серийнику (IP мы не храним). MQTT-настройки не трогаются.</div>
        ${m.error ? `<div class="muted" style="color:#c2410c;margin-top:6px">Не удалось прочитать текущие настройки: ${this._esc(m.error)}. Можно задать заново.</div>` : ''}`;
      }
    } else {
      const p = m.paramer || {};
      const fields = m.kind === 'lampParam' ? LAMP_FIELDS : sensorFields(m.dev && m.dev.devType);
      title = 'Параметры · ' + (m.dev.name || m.dev.typeName + ' ' + m.dev.address);
      body = this._paramGrid(fields, p);
    }
    // яркость применяется на лету (при отпускании слайдера) → только «Выход»
    const foot = m.kind === 'bright'
      ? `<button class="btn primary" data-act="closeModal">Выход</button>`
      : m.kind === 'scanMode'
      ? `<button class="btn ghost" data-act="closeModal">Отмена</button>`
      : m.kind === 'resolve'
      ? `<button class="btn primary" id="scanClose" data-act="closeModal" ${this._state.scanning ? 'disabled' : ''}>${this._state.scanning ? 'Развожу адреса…' : 'Закрыть'}</button>`
      : m.kind === 'scan'
      ? `<button class="btn ghost" data-act="scan" ${this._state.scanning ? 'disabled' : ''}>Сканировать заново</button><button class="btn primary" id="scanClose" data-act="closeModal">${this._state.scanning ? 'Сканирование…' : 'Закрыть'}</button>`
      : m.kind === 'registry'
      ? `<button class="btn primary" data-act="closeModal">Закрыть</button>`
      : m.kind === 'luxKeep'
      ? `<button class="btn ghost" data-act="closeModal">Отмена</button><button class="btn danger" data-act="clearLuxKeep" title="Снять всю конфигурацию с датчика (не пауза!)">Очистить</button><button class="btn primary" data-act="saveModal">Сохранить</button>`
      : m.kind === 'rotaryBind'
      ? `<button class="btn ghost" data-act="closeModal">Отмена</button><button class="btn primary" data-act="saveModal">Сохранить</button>`
      : m.kind === 'gwSettings'
      ? (m.loading
        ? `<button class="btn ghost" data-act="closeModal">Закрыть</button>`
        : `<button class="btn ghost" data-act="closeModal">Отмена</button><button class="btn primary" data-act="saveGwName">Сохранить имя</button><button class="btn danger" data-act="saveGwNet">Применить сеть</button>`)
      : `<button class="btn ghost" data-act="closeModal">Отмена</button><button class="btn primary" data-act="saveModal">${m.kind === 'createGroup' ? 'Создать' : (m.kind === 'xgroup' && !m.uid) ? 'Создать' : (m.kind === 'editGroup' || m.kind === 'xgroup' || m.kind === 'panelBind' || m.kind === 'sensorBind') ? 'Сохранить' : 'Применить'}</button>`;
    return `<div class="overlay"><div class="dialog"><div class="dlg-h">${this._esc(title)}</div>${body}<div class="dlg-foot">${foot}</div></div></div>`;
  }

  // троттл: схлопываем пачку hass/avail/conn-обновлений в один проход за кадр
  _scheduleSync() {
    if (this._syncPending) return;
    this._syncPending = true;
    this._syncRaf = requestAnimationFrame(() => { this._syncPending = false; this._syncStates(); });
  }

  // живые состояния — обновляем точечно, без полной перерисовки (без мельканий)
  _syncStates() {
    const sr = this.shadowRoot;
    if (!sr) return;
    this._state.devices.forEach((dev, i) => {
      const live = sr.querySelector(`[data-live="${i}"]`);
      if (live) live.textContent = this._liveText(dev);
      // ИЗОЛЯЦИЯ (v1.2.13): бейдж энергии — КОСМЕТИКА, а ниже идёт ФУНКЦИОНАЛ (точка online,
      // иконка вкл/выкл) и весь дальнейший цикл, включая группы. Пока изоляции не было, одна
      // ошибка в бейдже (`suspect is not defined`, жила с v1.2.6) обрывала forEach на первой же
      // лампе → у ВСЕХ ламп навсегда пропадал статус вкл/выкл. Ошибку не глушим — пишем в
      // консоль (принцип «проблемы должны быть ВИДНЫ»), но синк не роняем.
      const enb = sr.querySelector(`[data-enb="${i}"]`);
      if (enb) {
        try {
          enb.innerHTML = this._energyBadge(dev);
        } catch (err) {
          enb.innerHTML = `<span class="enbadge stale" title="Ошибка бейджа">—</span>`;
          console.error('arvid-dali-panel: _energyBadge упал', err, dev);
        }
      }
      const dot = sr.querySelector(`[data-dot="${i}"]`);
      if (dot) { const av = this._devOnline(dev); dot.classList.toggle('on', av); dot.classList.toggle('off', !av); dot.title = av ? 'online' : 'offline'; }
      const tg = sr.querySelector(`[data-tg="${i}"]`);
      if (tg) {
        const ent = dev.entities && dev.entities.light;
        const st = this._st(ent);
        const on = st && st.state === 'on';
        tg.classList.toggle('on', !!on);
        tg.innerHTML = svg(on ? ICONS.bulb : ICONS.bulbOff);
      }
    });
    this._state.groups.forEach((g, i) => {
      let on = false, txt = '';
      if (g.entity_id) {
        const st = this._st(g.entity_id);
        if (st && st.state === 'unavailable') { txt = g.present === false ? 'нет на контроллере' : 'не на связи'; }
        else if (st) { on = st.state === 'on'; const b = st.attributes.brightness; txt = (on ? 'включена' : 'выключена') + (b ? ` · ${Math.round(b / 255 * 100)}%` : ''); }
      } else { on = !!this._state.groupState[g.groupId]; }
      const tg = sr.querySelector(`[data-gtg="${i}"]`);
      if (tg) { tg.classList.toggle('on', on); tg.innerHTML = svg(on ? ICONS.bulb : ICONS.bulbOff); }
      const live = sr.querySelector(`[data-glive="${i}"]`);
      if (live) live.textContent = txt;
    });
    // кросс-группы: якорь по uid (индекс не годится — список другой), состояние из сущности
    (this._state.xgroups || []).forEach((g) => {
      let on = false;
      if (g.entity_id) {
        const st = this._st(g.entity_id);
        if (st && st.state !== 'unavailable') on = st.state === 'on';
      } else { on = !!this._state.groupState['x:' + g.uid]; }
      const tg = sr.querySelector(`[data-xtg="${(window.CSS && CSS.escape) ? CSS.escape(g.uid) : g.uid}"]`);
      if (tg) { tg.classList.toggle('on', on); tg.innerHTML = svg(on ? ICONS.bulb : ICONS.bulbOff); }
    });
  }

  // бейдж энергии лампы: «сейчас · всего · наработка» (+ аларм перегорания).
  // Данные — из снапшота WS energy_live (НЕ HA-сенсоры), ключ devSn. Честная заглушка
  // «—» для ламп без метрологии/данных (не пугаем нулём). Обновляется троттлингом.
  // ⚠ v1.2.6: бейдж ПОЛНОСТЬЮ РАСЧЁТНЫЙ. Раньше «сейчас» и «сегодня» приходили ОТ ШЛЮЗА
  // (reportEnergy) — а шлюз энергию не измеряет (ретранслирует энергобанк драйвера либо
  // выдумывает; разброс ×0.2…×1.35), то есть бейдж честно показывал недостоверное число.
  // Теперь: мощность = power_w × кривая(яркость) по состоянию сущности, «всего»/«наработка» —
  // из расчётного накопителя. «Сегодня» убрано: нужен якорь на полночь (отдельная тема — E4).
  _energyBadge(dev) {
    const e = (dev.devSn && this._state.energy) ? this._state.energy[dev.devSn] : null;
    if (!e) return `<span class="enbadge stale" title="Нет данных по лампе">—</span>`;
    // аларм перегорания/обрыва — приоритетнее чисел (Правило B)
    const al = e.alarm || [];
    if (al.includes('openCircuit'))
      return `<span class="enbadge crit" title="alarmCodeReport: обрыв источника (光源开路)">⚠ перегорела</span>`;
    if (al.length)
      return `<span class="enbadge warn" title="Алармы: ${this._esc(al.join(', '))}">⚠ ${this._esc(al[0])}</span>`;
    // мощность (Вт): РАСЧЁТНАЯ, по текущей яркости. null — у лампы не задана полная мощность
    // (power_w) → энергию по ней НЕ копим и число не выдумываем (принцип «проблемы видимы»).
    const havePower = e.power_w != null;
    const now = havePower ? `${Math.round(e.power_w)} Вт` : '—';
    // «всего»: Вт·ч до 1000, дальше кВт·ч (лайфтайм-накопитель)
    const wh = e.total_wh || 0;
    const total = wh >= 1000 ? `${(wh / 1000).toFixed(2)} кВт·ч` : `${wh.toFixed(wh < 10 ? 2 : 1)} Вт·ч`;
    // «наработка»: часы «вкл» (из расчётного накопителя on_time)
    const ot = e.on_time_h || 0;
    const work = ot >= 1 ? `${ot < 10 ? ot.toFixed(1) : Math.round(ot)} ч` : `${Math.round(ot * 60)} мин`;
    // подозрение «вкл, но ~0 Вт» (Правило A): ТОЛЬКО по свежему валидному усреднённому замеру
    // (мощность известна, отчёт не устарел, лампа-сущность ON, P≈0). Накопление = дебаунс,
    // так что переходные fade/розжиг не флагают.
    const ent = dev.entities && dev.entities.light;
    const st = this._st(ent);
    // «вкл, но power_w не задан» — не авария, а пробел в настройке: энергия по лампе НЕ копится
    const noParams = !havePower && st && st.state === 'on';
    const cls = noParams ? 'enbadge susp' : 'enbadge';
    const tip = noParams
      ? 'Лампа включена, но не задана полная мощность (power_w) — энергия по ней НЕ копится. Задайте в «Энергия» → «Параметры ламп».'
      : 'сейчас · всего · наработка. Всё РАСЧЁТНОЕ: мощность = полная мощность × кривая драйвера '
        + 'по текущей яркости; энергия и наработка — накопители интегратора. Шлюзовым числам не верим '
        + '(он энергию не измеряет).';
    return `<span class="${cls}" title="${tip}">${now} · ${total} · ${work}${noParams ? ' ⚠' : ''}</span>`;
  }

  _liveText(dev) {
    const t = String(dev.devType), E = dev.entities || {};
    if (!this._devOnline(dev)) return 'не на связи';
    if (LIGHT_T.includes(t)) { const st = this._st(E.light); if (!st) return ''; const b = st.attributes.brightness; return (st.state === 'on' ? 'включена' : 'выключена') + (b ? ` · ${Math.round(b / 255 * 100)}%` : ''); }
    if (t === '0201') { const st = this._st(E.motion); return st ? 'движение: ' + st.state : ''; }
    if (t === '0202') { const st = this._st(E.lux); return st ? 'освещённость: ' + st.state + ' lx' : ''; }
    if (t.startsWith('03')) { const st = this._st(E.event); if (!st) return ''; const et = st.attributes && st.attributes.event_type; return et ? 'последнее: ' + et + (st.attributes.key_no != null ? ' #' + st.attributes.key_no : '') : 'нажатий не было'; }
    return '';
  }
}

const STYLE = `
:host{display:block}*{box-sizing:border-box}
.root{font-family:var(--ha-card-font-family,system-ui,-apple-system,"Segoe UI",Roboto,sans-serif)}
.card{position:relative;border-radius:20px;padding:16px;color:#0F172A;background:linear-gradient(135deg,#cfe3ff 0%,#e7f1ff 42%,#ffffff 100%);box-shadow:0 8px 30px rgba(2,132,199,.14);overflow:hidden}
.hd{display:flex;align-items:center;justify-content:space-between;margin-bottom:12px}
.hd-title{display:flex;align-items:center;gap:10px;color:#0284C7}
.hd-title div{display:flex;flex-direction:column;line-height:1.1}
.hd-title b{font-size:18px;color:#0F172A}.hd-title span{font-size:12px;color:#64748B}
.ic{width:20px;height:20px}.ic.sm{width:14px;height:14px}
.muted{color:#64748B;font-size:12px;font-weight:500}
.chips{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:12px}
.chip{display:flex;flex-direction:column;align-items:flex-start;gap:2px;padding:8px 12px;min-height:44px;border:1px solid #cfe0f5;background:rgba(255,255,255,.7);border-radius:12px;cursor:pointer;transition:all .18s;font:inherit;text-align:left}
.bchips{display:flex;flex-wrap:wrap;gap:6px;margin-top:4px}
.tchip{display:inline-flex;align-items:center;gap:6px;padding:4px 4px 4px 10px;border:1px solid #cfe0f5;background:rgba(255,255,255,.8);border-radius:10px;font-size:13px}
.tchip .tx{border:none;background:#fef2f2;color:#DC2626;border-radius:7px;width:24px;height:24px;cursor:pointer;font-size:15px;line-height:1}
.tchip .tx:hover{background:#fde2e2}
.tchip em{font-style:normal;color:#0284C7;font-size:12px;white-space:nowrap}
.chip:hover{border-color:#7cc1ec;background:#fff}
.chip.on{border-color:#0284C7;background:#fff;box-shadow:0 2px 10px rgba(2,132,199,.18)}
.chip-t{font-weight:600;font-size:13px;color:#0F172A}.chip-s{font-size:11px;color:#64748B}
.toolbar{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-bottom:12px}
.btn{display:inline-flex;align-items:center;gap:8px;padding:0 16px;min-height:44px;border-radius:12px;border:1px solid transparent;cursor:pointer;font:inherit;font-size:14px;font-weight:600;transition:.18s}
.btn:focus-visible{outline:none;box-shadow:0 0 0 3px rgba(2,132,199,.45)}
.btn.primary{background:#0284C7;color:#fff}.btn.primary:hover{background:#0369a1}.btn.primary:disabled{opacity:.6;cursor:default}
.btn.danger{background:#fff;color:#DC2626;border-color:#f3c4c4}.btn.danger:hover{background:#fef2f2}
.btn.ghost{background:rgba(255,255,255,.7);color:#0284C7;border-color:#cfe0f5}.btn.ghost:hover{background:#fff}
.panel{background:rgba(255,255,255,.82);backdrop-filter:blur(6px);border:1px solid #e0eefb;border-radius:16px;padding:10px 12px;margin-bottom:12px}
.panel-h{display:flex;align-items:center;gap:8px;font-weight:600;font-size:13px;color:#0F172A;padding:4px 2px 8px;text-transform:uppercase;letter-spacing:.03em}
.panel-h.collapsible{cursor:pointer;user-select:none;border-radius:8px;min-height:32px}
.panel-h.collapsible:hover{color:#0284C7}
.panel-h.collapsible:focus-visible{outline:none;box-shadow:0 0 0 3px rgba(2,132,199,.35)}
.panel.collapsed .panel-h{padding-bottom:4px}
.chev{display:inline-flex;color:#64748B;transition:transform .15s ease;transform:rotate(90deg)}
.panel.collapsed .chev{transform:rotate(0deg)}
.enfilters{display:flex;gap:10px;flex-wrap:wrap;margin-bottom:10px}
.hlist{max-height:55vh;overflow:auto}
.entbl{width:100%;border-collapse:collapse;font-size:13px}
.entbl th,.entbl td{padding:6px 8px;border-top:1px solid #eef5fd;text-align:left}
.entbl th{color:#64748B;font-weight:600;font-size:12px;text-transform:uppercase;letter-spacing:.02em}
.entbl .num{text-align:right;font-variant-numeric:tabular-nums}
.entbl tfoot td{border-top:2px solid #cfe0f5;font-weight:700}
.enfoot{display:flex;align-items:flex-end;gap:10px;flex-wrap:wrap;margin-top:10px}
.panel.empty{color:#64748B;text-align:center;padding:24px}
.mini{margin-left:auto;border:1px solid #cfe0f5;background:#fff;color:#0284C7;border-radius:9px;padding:6px 10px;font:inherit;font-size:12px;font-weight:600;cursor:pointer;min-height:32px}
.mini:hover{border-color:#7cc1ec;background:#f0f9ff}
.mini.danger{border-color:#f3c4c4;color:#dc2626}
.mini.danger:hover{border-color:#ef9a9a;background:#fef2f2}
.row{display:flex;align-items:center;justify-content:space-between;gap:10px;padding:8px 6px;border-top:1px solid #eef5fd}
.row.zombie{background:#fef2f2;border-left:3px solid #dc2626;padding-left:3px}
.row.zombie .row-txt b{color:#b91c1c}
.zchip{font-size:10px;font-weight:700;color:#dc2626;background:#fde2e2;border-radius:6px;padding:1px 6px;flex:0 0 auto;text-transform:uppercase;letter-spacing:.3px}
.row:first-of-type{border-top:0}
.row-main{display:flex;align-items:center;gap:10px;min-width:0}
.row-txt{display:flex;flex-direction:column;min-width:0}
.row-txt b{font-size:14px;font-weight:600}
.name-row{display:flex;align-items:center;gap:6px;min-width:0}
/* бейдж кросс-шлюзовой группы: она живёт на нескольких контроллерах */
.xbadge{display:inline-flex;align-items:center;background:#7C3AED;color:#fff;border-radius:6px;padding:1px 6px;font-size:10px;font-weight:700;letter-spacing:.3px;flex:0 0 auto}
.pen{display:inline-flex;align-items:center;justify-content:center;width:26px;height:26px;border:0;background:transparent;color:#0284C7;cursor:pointer;border-radius:7px;flex:0 0 auto;transition:.15s}
.pen:hover{color:#0369a1;background:#eaf3ff}
.pen .ic{width:16px;height:16px}
.row-txt .muted{white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:46vw}
.live{font-size:11px;color:#0284C7;font-weight:600;margin-top:1px;min-height:13px}
.enb{display:block;margin-top:1px;min-height:0}
.enbadge{display:inline-block;font-size:11px;font-weight:600;color:#0f766e;background:#ecfdf5;border:1px solid #bbf7e6;border-radius:7px;padding:1px 7px}
.enbadge.stale{color:#64748B;background:#f1f5f9;border-color:#e2e8f0}
.enbadge.susp{color:#b45309;background:#fffbeb;border-color:#fde68a}
.enbadge.warn{color:#b45309;background:#fffbeb;border-color:#fcd34d}
.enbadge.crit{color:#DC2626;background:#fef2f2;border-color:#fca5a5}
.dot{width:9px;height:9px;border-radius:50%;flex:0 0 auto}
.dot.on{background:#16a34a;box-shadow:0 0 0 3px rgba(22,163,74,.15)}.dot.off{background:#cbd5e1}
.row-act{display:flex;gap:4px;flex:0 0 auto}
.ibtn{display:inline-flex;align-items:center;justify-content:center;width:38px;height:38px;border-radius:10px;border:1px solid #e0eefb;background:#fff;color:#0284C7;cursor:pointer;transition:.15s}
.ibtn:hover{border-color:#7cc1ec;background:#f0f9ff}
.ibtn:focus-visible{outline:none;box-shadow:0 0 0 3px rgba(2,132,199,.4)}
.ibtn.danger{color:#DC2626;font-size:20px;line-height:1}
.ibtn.tg{color:#94a3b8}.ibtn.tg.on{color:#f59e0b;border-color:#fcd9a0;background:#fffaf0}
.log .logbox{max-height:160px;overflow:auto;font-size:12px;border-radius:10px;background:#f6fbff;border:1px solid #e0eefb;padding:6px}
.logrow{display:flex;align-items:center;gap:6px;padding:3px 4px;color:#334155}
.logrow.found{color:#0369a1}.logrow.info{color:#64748B}.logrow.err{color:#DC2626}
.sk{height:34px;border-radius:10px;margin:6px 0;background:linear-gradient(90deg,#eef5fd,#dbeafe,#eef5fd);background-size:200% 100%;animation:sh 1.2s infinite}
@keyframes sh{0%{background-position:200% 0}100%{background-position:-200% 0}}
.overlay{position:fixed;inset:0;background:rgba(15,23,42,.45);display:flex;align-items:center;justify-content:center;z-index:1000;padding:16px;animation:fade .15s}
.dialog{width:100%;max-width:440px;max-height:85vh;overflow:auto;background:linear-gradient(135deg,#eaf3ff,#ffffff);border-radius:18px;padding:16px;box-shadow:0 20px 60px rgba(2,132,199,.3)}
.dlgbox{max-height:34vh;overflow:auto}
.scan-mode{justify-content:flex-start;width:100%;min-height:48px;font-size:14px}
.dlg-h{font-weight:600;font-size:15px;margin-bottom:12px;color:#0F172A}
.grid{display:flex;flex-direction:column;gap:10px}
.fld{display:flex;align-items:center;justify-content:space-between;gap:10px;font-size:13px;color:#334155}
input,select,textarea{color:#0F172A;-webkit-text-fill-color:#0F172A;font-size:16px}
select{background:#eaf3ff;border:1px solid #cfe0f5;border-radius:10px;min-height:42px;padding:8px 10px}
.gw-sel{margin-bottom:12px}.gw-select{width:100%;font-weight:600;color:#0F172A}.gw-sel-sub{font-size:11px;color:#64748B;margin-top:4px;padding-left:4px}
.btn[data-busy]{opacity:.75;cursor:progress}
option{background:#eaf3ff;color:#0F172A}
.fld input{width:120px;height:40px;border:1px solid #cfe0f5;border-radius:10px;padding:0 10px;font-family:inherit;background:#fff}
.fld input:focus{outline:none;border-color:#0284C7;box-shadow:0 0 0 3px rgba(2,132,199,.2)}
.fld.fld-wide{flex-direction:column;align-items:stretch;gap:6px}
.fld.fld-wide input{width:100%}
.chk-h{font-size:12px;font-weight:600;color:#334155;margin:12px 2px 6px;text-transform:uppercase;letter-spacing:.03em}
/* Автояркость v1.2.25: тумблер «Активна» + окна работы (в СТОЛБИК, «+» в шапке раздела) */
.lk-target{margin:6px 0;font-size:13px}
.lk-toggle{display:flex;gap:8px;align-items:flex-start;margin:8px 0;padding:8px 10px;border:1px solid #cfe0f5;border-radius:10px;background:#f8fbff;font-size:12px;color:#334155;cursor:pointer}
.lk-toggle input{margin-top:2px;width:18px;height:18px;flex:none;cursor:pointer}
.lk-wins{display:flex;flex-direction:column;gap:6px}
.mp-acts{display:flex;gap:6px;flex-wrap:wrap;margin-top:6px}
.lk-win{display:flex;align-items:center;gap:6px}
.lk-win input[type=time]{flex:1;min-width:0;padding:6px 8px;border:1px solid #cfe0f5;border-radius:8px;font:inherit;min-height:32px}
.lk-win span{color:#64748b}
.mini.danger{color:#b91c1c;border-color:#fecaca;margin-left:0;flex:none;min-width:32px;text-align:center}
.chk-list{max-height:240px;overflow:auto;display:flex;flex-direction:column;gap:2px;border:1px solid #e0eefb;border-radius:10px;padding:6px;background:#f6fbff}
.mp-tabs{display:flex;gap:6px;margin-bottom:6px}
.chk{display:flex;align-items:center;gap:10px;padding:8px;border-radius:8px;cursor:pointer;font-size:13px}
.chk:hover{background:#eaf3ff}.chk input{width:18px;height:18px;accent-color:#0284C7}
.chk em{color:#64748B;font-style:normal;font-size:11px}
.mlamps{display:flex;flex-direction:column;gap:2px;max-height:260px;overflow-y:auto;margin-bottom:8px}
.mreport{border-top:1px dashed #cfe0f5;margin-top:12px;padding-top:4px}
.bri{display:flex;align-items:center;gap:14px;padding:6px 2px}
.bri input[type=range]{flex:1;accent-color:#0284C7;height:6px}
.bri-val{font-weight:600;color:#0284C7;min-width:48px;text-align:right}
.dlg-foot{display:flex;justify-content:flex-end;gap:8px;margin-top:16px}
.hd-actions{display:flex;gap:8px}
.btn.ghost.on{background:#0284C7;color:#fff;border-color:#0284C7}
.cst{margin-left:8px;font-size:10px;font-weight:700;padding:1px 6px;border-radius:6px;vertical-align:middle}
.cst.warn{background:#fff7ed;color:#c2410c;border:1px solid #fed7aa}
.banner{display:flex;align-items:center;gap:10px;padding:10px 12px;border-radius:14px;margin-bottom:12px;font-size:13px;font-weight:500}
.banner .ic{width:20px;height:20px;flex:0 0 auto}
.banner.warn{background:#fff7ed;color:#9a3412;border:1px solid #fed7aa}
.logbox.events{max-height:300px}
.logrow.ev-warn{color:#c2410c}.logrow.ev-error{color:#dc2626}
.evt{color:#94a3b8;font-variant-numeric:tabular-nums;flex:0 0 auto}
.evk{color:#0284C7;font-weight:600;flex:0 0 auto;min-width:52px}
.events .logrow{align-items:flex-start}
.toast{position:fixed;left:50%;bottom:24px;transform:translateX(-50%) translateY(20px);opacity:0;background:#0F172A;color:#fff;padding:10px 16px;border-radius:12px;font-size:13px;pointer-events:none;transition:.25s;z-index:1100;max-width:90vw}
.toast.show{opacity:1;transform:translateX(-50%) translateY(0)}.toast.err{background:#DC2626}
@keyframes fade{from{opacity:0}to{opacity:1}}
@media (prefers-reduced-motion:reduce){*{animation:none!important;transition:none!important}}
`;

customElements.define('arvid-dali-panel', ArvidDaliPanel);
window.customCards = window.customCards || [];
window.customCards.push({ type: 'arvid-dali-panel', name: 'ARVID DALI Panel', description: 'Управление интеграцией ARVID DALI Center.' });
console.info('%c ARVID-DALI-PANEL %c v1.2.74 ', 'background:#0284C7;color:#fff;border-radius:4px 0 0 4px;padding:2px 6px', 'background:#e7f1ff;color:#0284C7;border-radius:0 4px 4px 0;padding:2px 6px');
