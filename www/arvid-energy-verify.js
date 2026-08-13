/*
 * arvid-energy-verify — карточка СВЕРКИ расчётного энергоучёта с реле (ВРЕМЕННАЯ, v1.2.32).
 *
 * Наш учёт расчётный (P = power_w × кривая(яркость)); единственная проверка — прибор на входе
 * 230 В. Карточка показывает обе стороны рядом и накопленное отношение Δнаш/Δреле (цель 1.0),
 * чтобы не сидеть в терминале с tools/energy_compare.py.
 *
 * ⚠ Отдельный файл и отдельный ресурс: основную панель (arvid-dali-panel.js) не трогаем —
 * сверка исследовательская и снимется вместе с сателлитом (docs/ENERGY_VERIFY.md).
 *
 * Подключение: Настройки → Панели → Ресурсы → /local/arvid-energy-verify.js (module),
 * затем карточка `type: custom:arvid-energy-verify`.
 */

const HOUR_S = 3600;

class ArvidEnergyVerify extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: 'open' });
    this._state = { loading: true, data: null, err: null, lamps: [] };
    this._timer = null;
  }

  setConfig(config) { this._config = config || {}; }
  getCardSize() { return 8; }

  set hass(hass) {
    const first = !this._hass;
    this._hass = hass;
    if (first) { this._render(); this._load(); this._loadLamps(); this._arm(); }
  }

  disconnectedCallback() { if (this._timer) clearInterval(this._timer); this._timer = null; }

  _arm() {
    if (this._timer) clearInterval(this._timer);
    // 15 с достаточно: сателлит снимает срезы раз в минуту, чаще дёргать нечего
    this._timer = setInterval(() => this._load(), 15000);
  }

  async _ws(msg) { return this._hass.connection.sendMessagePromise(msg); }

  async _load() {
    try {
      this._state.data = await this._ws({ type: 'arvid_dali_center/verify_state' });
      this._state.err = null;
    } catch (e) { this._state.err = e.message; }
    this._state.loading = false;
    this._render();
  }

  // Список НАШИХ ламп (для выбора в форме): devSn + entity_id берём из ядра одним запросом
  async _loadLamps() {
    try {
      const gws = (await this._ws({ type: 'arvid_dali_center/gateways' })).gateways || [];
      const out = [];
      for (const g of gws) {
        const devs = (await this._ws({ type: 'arvid_dali_center/devices', gw_sn: g.gwSn })).devices || [];
        for (const d of devs) {
          if (!String(d.devType || '').startsWith('01') || !d.devSn) continue;
          const eid = (d.entities || {}).light;
          if (eid) out.push({ devsn: d.devSn, entity: eid, name: d.name || eid, gw: g.gwSn });
        }
      }
      out.sort((a, b) => String(a.name).localeCompare(String(b.name), 'ru'));
      this._state.lamps = out;
      this._render();
    } catch (e) { /* форма переживёт: entity_id можно вписать руками */ }
  }

  // ── действия ──────────────────────────────────────────────────────────────
  async _start() {
    const sr = this.shadowRoot, v = (id) => (sr.getElementById(id) || {}).value || '';
    const lampIdx = v('vLamp');
    const lamp = this._state.lamps[+lampIdx];
    if (!lamp) { this._toast('Выбери лампу', true); return; }
    if (!v('vRelayE')) { this._toast('Укажи сущность энергии реле', true); return; }
    try {
      const r = await this._ws({
        type: 'arvid_dali_center/verify_start',
        devsn: lamp.devsn, lamp_entity: lamp.entity,
        relay_energy: v('vRelayE'), relay_power: v('vRelayP'), relay_switch: v('vRelayS'),
        name: v('vName') || lamp.name, interval_s: parseInt(v('vInt'), 10) || 60,
      });
      if (r && r.warning) this._toast(r.warning, true);
      else this._toast('Сверка начата');
      await this._load();
    } catch (e) { this._toast('Ошибка: ' + e.message, true); }
  }

  async _control(action) {
    const texts = { pause: 'Пауза', resume: 'Продолжено', rebase: 'База сброшена',
                    clear: 'Сессия закрыта' };
    if (action === 'clear' && !confirm('Закрыть сессию сверки?\n\nСводка уйдёт в архив, срезы будут удалены.')) return;
    if (action === 'rebase' && !confirm('Сбросить базу дельт на текущие показания?\n\nНужно после смены полной мощности или кривой: прошлое НЕ пересчитывается, поэтому сверять дальше надо от новой точки.')) return;
    try {
      await this._ws({ type: 'arvid_dali_center/verify_control', action });
      this._toast(texts[action] || 'Готово');
      await this._load();
    } catch (e) { this._toast('Ошибка: ' + e.message, true); }
  }

  async _csv() {
    try {
      const r = await this._ws({ type: 'arvid_dali_center/verify_csv' });
      const blob = new Blob([r.csv], { type: 'text/csv;charset=utf-8' });
      const a = document.createElement('a');
      a.href = URL.createObjectURL(blob);
      a.download = 'energy_verify.csv';
      a.click();
      URL.revokeObjectURL(a.href);
      this._toast(`Выгружено строк: ${r.rows}`);
    } catch (e) { this._toast('Ошибка: ' + e.message, true); }
  }

  _toast(text, bad) {
    const el = this.shadowRoot.getElementById('toast');
    if (!el) return;
    el.textContent = text;
    el.className = 'toast show' + (bad ? ' bad' : '');
    clearTimeout(this._tt);
    this._tt = setTimeout(() => { el.className = 'toast'; }, 5000);
  }

  // ── отрисовка ─────────────────────────────────────────────────────────────
  _esc(s) { return String(s == null ? '' : s).replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c])); }

  _fmt(v, unit, digits = 1) {
    return (v == null || v === '') ? '—' : `${Number(v).toFixed(digits)}${unit ? ' ' + unit : ''}`;
  }

  _form() {
    const opts = this._state.lamps.map((l, i) => `<option value="${i}">${this._esc(l.name)} · ${this._esc(l.entity)}</option>`).join('')
      || '<option value="">лампы не найдены</option>';
    return `<div class="muted">Сверяем НАШ расчёт (мощность × кривая × время) с реальным реле на входе 230 В.</div>
      <div class="grid">
        <label class="fld wide"><span>Лампа</span><select id="vLamp">${opts}</select></label>
        <label class="fld wide"><span>Реле: энергия (Вт·ч), обязательно</span><input id="vRelayE" type="text" placeholder="sensor.…_energy"></label>
        <label class="fld wide"><span>Реле: мощность (Вт)</span><input id="vRelayP" type="text" placeholder="sensor.…_power"></label>
        <label class="fld wide"><span>Реле: сам выключатель</span><input id="vRelayS" type="text" placeholder="switch.…"></label>
        <label class="fld"><span>Интервал, с</span><input id="vInt" type="number" min="10" max="3600" value="60"></label>
        <label class="fld wide"><span>Название (необязательно)</span><input id="vName" type="text" placeholder="l_1_1_3 через Shelly"></label>
      </div>
      <div class="acts"><button class="btn primary" id="bStart">Начать сверку</button></div>
      <div class="muted note">⚠ Счётчик Вт·ч у реле квантован (тики 0.2–0.4 Вт·ч): на коротком окне это даёт до 6–8 % ложной ошибки. Энергию сверяем прогоном от часа, форму кривой — мгновенной мощностью.</div>`;
  }

  _running(d) {
    const s = d.session, live = d.live || {}, del = d.delta || {};
    const age = s.started_at ? (Date.now() - new Date(s.started_at).getTime()) / 1000 : 0;
    const young = age < HOUR_S;
    const ratio = del.ratio;
    const verdict = ratio == null ? { t: 'нет данных', c: '' }
      : Math.abs(ratio - 1) <= 0.05 ? { t: 'совпадает (±5 %)', c: 'ok' }
      : Math.abs(ratio - 1) <= 0.15 ? { t: 'расхождение 5–15 %', c: 'warn' }
      : { t: 'расхождение > 15 %', c: 'bad' };
    const mism = live.lamp_state === 'on' && live.relay_on === 'off';
    const rows = (d.samples || []).slice(-120).reverse().map((x) => `<tr>
        <td>${this._esc((x.ts || '').slice(11, 16))}</td>
        <td>${this._fmt(x.our_w, '', 1)}</td><td>${this._fmt(x.relay_w, '', 1)}</td>
        <td>${this._fmt(x.our_wh, '', 2)}</td><td>${this._fmt(x.relay_wh, '', 2)}</td>
        <td>${this._esc(x.lamp_state || '—')}${x.lamp_bri != null ? ' · ' + Math.round(x.lamp_bri / 2.55) + '%' : ''}</td>
        <td>${this._esc(x.relay_on || '—')}</td></tr>`).join('');
    return `
      <div class="head">
        <div><b>${this._esc(s.name || s.lamp_entity)}</b>
          <div class="muted">${this._esc(s.lamp_entity)} · devSn ${this._esc(s.devsn)} · с ${this._esc((s.started_at || '').replace('T', ' ').slice(0, 16))}
          ${s.running ? '' : ' · <b>на паузе</b>'}</div></div>
        <div class="verdict ${verdict.c}">${this._esc(verdict.t)}</div>
      </div>
      ${s.power_w == null ? '<div class="alert">⚠ У лампы не задана полная мощность (power_w) — наш расчёт даёт 0 Вт·ч. Задайте её в «Энергия → Параметры ламп», иначе сверять нечего.</div>' : ''}
      ${mism ? '<div class="alert">⚠ Лампа числится включённой при ВЫКЛЮЧЕННОМ реле — эти интервалы из сверки надо исключить (иначе выглядит как «модель завышает»).</div>' : ''}
      ${live.unit_unknown ? `<div class="alert">⚠ Не распознана единица измерения реле (энергия: ${this._esc(live.relay_unit_e || '—')}, мощность: ${this._esc(live.relay_unit_p || '—')}) — значения взяты КАК ЕСТЬ, без пересчёта. Отношение будет неверным, если реле отдаёт не Вт·ч/Вт.</div>` : ''}
      ${young ? `<div class="alert soft">Прогон идёт ${Math.round(age / 60)} мин. Счётчик реле квантован — отношению можно верить примерно через час.</div>` : ''}
      <div class="tiles">
        <div class="tile"><span>Мощность сейчас</span><b>${this._fmt(live.our_w, 'Вт')}</b><i>наш расчёт</i></div>
        <div class="tile"><span>&nbsp;</span><b>${this._fmt(live.relay_w, 'Вт')}</b><i>реле</i></div>
        <div class="tile"><span>Накоплено за прогон</span><b>${this._fmt(del.our_wh, 'Вт·ч', 2)}</b><i>наш расчёт (с текущим отрезком)</i></div>
        <div class="tile"><span>&nbsp;</span><b>${this._fmt(del.relay_wh, 'Вт·ч', 2)}</b><i>реле</i></div>
        <div class="tile big"><span>Отношение Δнаш / Δреле</span><b class="${verdict.c}">${ratio == null ? '—' : ratio.toFixed(3)}</b><i>цель 1.000</i></div>
      </div>
      <div class="muted">Реле отдаёт ${this._esc(live.relay_unit_e || '?')} / ${this._esc(live.relay_unit_p || '?')} → пересчитано в Вт·ч / Вт · полная мощность ${this._fmt(s.power_w, 'Вт')} · кривая ${this._esc(s.model || 'linear')} · лампа сейчас ${this._esc(live.lamp_state || '—')}${live.lamp_bri != null ? ' (' + Math.round(live.lamp_bri / 2.55) + ' %)' : ''} · реле ${this._esc(live.relay_on || '—')}</div>
      <div class="acts">
        <button class="btn" id="bPause">${s.running ? 'Пауза' : 'Продолжить'}</button>
        <button class="btn" id="bRebase">Сбросить базу</button>
        <button class="btn" id="bCsv">Выгрузить CSV</button>
        <button class="btn danger" id="bClear">Закрыть сессию</button>
      </div>
      <div class="tblwrap"><table class="tbl"><thead><tr><th>время</th><th>наш Вт</th><th>реле Вт</th><th>наш Вт·ч</th><th>реле Вт·ч</th><th>лампа</th><th>реле</th></tr></thead><tbody>${rows || '<tr><td colspan="7">срезов пока нет</td></tr>'}</tbody></table></div>
      <div class="muted">Показаны последние срезы (прокрутка). Всего в сессии: ${(d.samples || []).length}${(d.samples || []).length >= 120 ? '+' : ''} — полный список в CSV.</div>`;
  }

  _render() {
    const d = this._state.data;
    // карточка перерисовывается раз в 15 с — без этого прокрутка таблицы срезов
    // прыгала бы наверх прямо под рукой у читающего (v1.2.34)
    const prevWrap = this.shadowRoot.querySelector('.tblwrap');
    const prevScroll = prevWrap ? prevWrap.scrollTop : null;
    const body = this._state.loading ? '<div class="muted">Загрузка…</div>'
      : this._state.err ? `<div class="alert">Ошибка: ${this._esc(this._state.err)}</div>`
      : (d && d.session) ? this._running(d) : this._form();
    this.shadowRoot.innerHTML = `<style>
      :host{display:block}
      .card{background:var(--card-background-color,#fff);border-radius:14px;padding:14px;box-shadow:var(--ha-card-box-shadow,0 2px 8px rgba(0,0,0,.08));font:14px/1.45 system-ui,-apple-system,Segoe UI,Roboto,sans-serif;color:var(--primary-text-color,#0f172a)}
      h2{margin:0 0 10px;font-size:16px;display:flex;align-items:center;gap:8px}
      .tag{font-size:11px;font-weight:600;color:#0284C7;background:#e7f1ff;border-radius:6px;padding:2px 6px}
      .muted{color:var(--secondary-text-color,#64748b);font-size:12px;margin:6px 0}
      .note{margin-top:10px}
      .grid{display:flex;flex-wrap:wrap;gap:8px;margin:10px 0}
      .fld{display:flex;flex-direction:column;gap:4px;font-size:12px;color:#334155;flex:1 1 150px}
      .fld.wide{flex:1 1 100%}
      .fld input,.fld select{padding:8px;border:1px solid #cfe0f5;border-radius:8px;font:inherit;min-height:34px;background:#fff;color:#0f172a}
      .acts{display:flex;gap:8px;flex-wrap:wrap;margin:10px 0}
      .btn{border:1px solid #cfe0f5;background:#fff;color:#0284C7;border-radius:9px;padding:8px 12px;font:inherit;font-size:13px;font-weight:600;cursor:pointer}
      .btn.primary{background:#0284C7;color:#fff;border-color:#0284C7}
      .btn.danger{color:#b91c1c;border-color:#fecaca}
      .head{display:flex;justify-content:space-between;align-items:flex-start;gap:10px}
      .verdict{font-size:12px;font-weight:700;padding:4px 10px;border-radius:999px;background:#f1f5f9;color:#334155;white-space:nowrap}
      .verdict.ok{background:#dcfce7;color:#166534}.verdict.warn{background:#fef9c3;color:#854d0e}.verdict.bad{background:#fee2e2;color:#b91c1c}
      .tiles{display:flex;flex-wrap:wrap;gap:8px;margin:10px 0}
      .tile{flex:1 1 110px;background:#f8fbff;border:1px solid #e2e8f0;border-radius:10px;padding:8px 10px}
      .tile span{display:block;font-size:11px;color:#64748b;min-height:14px}
      .tile b{display:block;font-size:18px;margin:2px 0}
      .tile i{font-size:11px;color:#94a3b8;font-style:normal}
      .tile.big{flex:1 1 100%;background:#eff6ff}
      .tile b.ok{color:#166534}.tile b.warn{color:#854d0e}.tile b.bad{color:#b91c1c}
      .alert{background:#fff7ed;border:1px solid #fed7aa;color:#9a3412;border-radius:10px;padding:8px 10px;font-size:12px;margin:8px 0}
      .alert.soft{background:#f8fafc;border-color:#e2e8f0;color:#475569}
      /* Прокрутка вместо бесконечного роста карточки: высота = шапка + 7 строк (v1.2.34) */
      .tblwrap{max-height:calc(28px + 7 * 25px);overflow-y:auto;margin-top:10px;border:1px solid #eef2f7;border-radius:8px}
      .tbl{width:100%;border-collapse:collapse;font-size:12px}
      .tbl thead th{position:sticky;top:0;background:var(--card-background-color,#fff);z-index:1}
      .tbl th,.tbl td{padding:5px 6px;border-bottom:1px solid #eef2f7;text-align:right;height:25px;box-sizing:border-box}
      .tbl th:first-child,.tbl td:first-child,.tbl th:nth-child(6),.tbl td:nth-child(6),.tbl th:last-child,.tbl td:last-child{text-align:left}
      .tbl th{color:#64748b;font-weight:600}
      .toast{position:fixed;left:50%;bottom:24px;transform:translateX(-50%) translateY(20px);background:#0f172a;color:#fff;padding:10px 14px;border-radius:10px;font-size:13px;opacity:0;pointer-events:none;transition:.2s;max-width:80vw;z-index:99}
      .toast.show{opacity:1;transform:translateX(-50%)}
      .toast.bad{background:#b91c1c}
    </style>
    <div class="card"><h2>Сверка энергоучёта<span class="tag">временная</span></h2>${body}</div>
    <div class="toast" id="toast"></div>`;

    if (prevScroll) {
      const wrap = this.shadowRoot.querySelector('.tblwrap');
      if (wrap) wrap.scrollTop = prevScroll;
    }
    const on = (id, fn) => { const el = this.shadowRoot.getElementById(id); if (el) el.onclick = fn; };
    on('bStart', () => this._start());
    on('bPause', () => this._control((d && d.session && d.session.running) ? 'pause' : 'resume'));
    on('bRebase', () => this._control('rebase'));
    on('bClear', () => this._control('clear'));
    on('bCsv', () => this._csv());
  }
}

customElements.define('arvid-energy-verify', ArvidEnergyVerify);
window.customCards = window.customCards || [];
window.customCards.push({
  type: 'arvid-energy-verify',
  name: 'ARVID — сверка энергоучёта',
  description: 'Сравнение расчётной энергии с реле (временная, исследовательская)',
});
console.info('%c ARVID-ENERGY-VERIFY %c v1.2.54 ', 'background:#0284C7;color:#fff;border-radius:4px 0 0 4px;padding:2px 6px', 'background:#e7f1ff;color:#0284C7;border-radius:0 4px 4px 0;padding:2px 6px');
