"""Сборщик срезов сверки: наши числа vs числа реле. Только ЧТЕНИЕ, на шину не ходит."""

from __future__ import annotations

import logging
import time
from datetime import timedelta

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.util import dt as dt_util

from ..energy.curves import power_at
from ..energy.store import get_energy_store
from .store import VerifyStore

# Ключ интегратора в hass.data (energy/__init__.py). Нужен, чтобы видеть ОТКРЫТЫЙ отрезок —
# см. `_open_tail_wh`. Читаем, не мутируем.
_INTEG_KEY = "arvid_dali_center_energy_integrator"

_LOGGER = logging.getLogger(__name__)

_COLLECTOR_KEY = "arvid_dali_center_verify_collector"
DEFAULT_INTERVAL_S = 60


def _num(state) -> float | None:
    """Состояние HA → число. `unknown`/`unavailable`/мусор → None (в срез пойдёт пусто —
    подделывать нулём нельзя: ноль неотличим от «реально 0 Вт»)."""
    if state is None or state.state in ("unknown", "unavailable", "", None):
        return None
    try:
        return float(state.state)
    except (TypeError, ValueError):
        return None


# Приведение единиц реле к НАШИМ (Вт·ч и Вт). Реле Shelly отдаёт энергию в кВт·ч, а наш
# накопитель — в Вт·ч: без нормировки `ratio` уехал бы РОВНО в 1000 раз. Единицу берём из
# `unit_of_measurement` самой сущности — гадать и заставлять человека выбирать не надо.
_ENERGY_TO_WH = {"wh": 1.0, "вт·ч": 1.0, "вт⋅ч": 1.0, "втч": 1.0,
                 "kwh": 1000.0, "квт·ч": 1000.0, "квт⋅ч": 1000.0, "квтч": 1000.0,
                 "mwh": 1_000_000.0, "мвт·ч": 1_000_000.0}
_POWER_TO_W = {"w": 1.0, "вт": 1.0, "kw": 1000.0, "квт": 1000.0,
               "mw": 0.001, "мвт": 0.001}     # mW — милливатты (редко, но встречается)


def _scaled(state, table: dict) -> tuple[float | None, str | None, bool]:
    """(значение в НАШИХ единицах, исходная единица, распознана ли единица).

    Единица не распознана → значение отдаём КАК ЕСТЬ и поднимаем флаг: карточка предупредит,
    а мы не подставим молча неверный коэффициент (принцип «проблемы должны быть видны»)."""
    v = _num(state)
    if v is None:
        return None, None, True
    unit = (state.attributes.get("unit_of_measurement") or "").strip() if state else ""
    mult = table.get(unit.lower())
    if mult is None:
        return v, unit or None, False
    return v * mult, unit or None, True


class VerifyCollector:
    """Снимает срез раз в интервал и кладёт в стор."""

    def __init__(self, hass: HomeAssistant, store: VerifyStore) -> None:
        self.hass = hass
        self.store = store
        self._unsub = None

    # ── жизненный цикл ────────────────────────────────────────────────────────
    def start(self) -> None:
        """Запустить таймер, если в сторе есть ЗАПУЩЕННАЯ сессия (переживает рестарт HA)."""
        s = self.store.session
        if s and s.get("running"):
            self._arm(int(s.get("interval_s") or DEFAULT_INTERVAL_S))

    def _arm(self, interval_s: int) -> None:
        self._disarm()
        self._unsub = async_track_time_interval(
            self.hass, self._tick, timedelta(seconds=max(10, interval_s)))

    def _disarm(self) -> None:
        if self._unsub:
            self._unsub()
            self._unsub = None

    def stop(self) -> None:
        self._disarm()

    def _open_tail_wh(self, devsn: str, power_w, model) -> float:
        """Энергия ОТКРЫТОГО (ещё не закрытого) отрезка, Вт·ч.

        ⚠ ЗАЧЕМ (иначе сверка врёт). Накопитель `energy_wh` пополняется только когда отрезок
        ЗАКРЫВАЕТСЯ: при изменении состояния лампы (вкл/выкл/яркость) либо на ЧАСОВОЙ подбивке.
        Пока лампа горит ровно, накопитель СТОИТ НА МЕСТЕ — а счётчик реле продолжает расти.
        На коротком окне это дало бы `ratio` около нуля и вердикт «модель врёт», хотя модель
        права: энергия просто ещё не записана.

        Поэтому к накопителю добавляем «хвост» — то, что натикало с момента последнего события:
        мощность на текущей яркости × прошедшее время. Ядро при этом НЕ трогаем (подбивку не
        зовём, чужие отрезки не дробим) — только читаем открытый отрезок интегратора."""
        integ = self.hass.data.get(_INTEG_KEY)
        seg = getattr(integ, "_seg", None) if integ else None   # noqa: SLF001 — читаем, не пишем
        if not seg or devsn not in seg or power_w is None:
            return 0.0
        last_t, frac = seg[devsn]
        dt_s = max(0.0, time.monotonic() - last_t)
        return power_at(model, power_w, frac) * dt_s / 3600.0

    # ── съём ──────────────────────────────────────────────────────────────────
    def read_now(self, cfg: dict) -> dict:
        """Мгновенный срез по конфигурации сессии. Используется и таймером, и при СТАРТЕ
        (чтобы зафиксировать базу дельт), и WS-командой «показать сейчас»."""
        devsn = cfg.get("devsn") or ""
        rec = {}
        es = get_energy_store(self.hass)
        if es:
            rec = es.get(devsn) or {}
        power_w, model = rec.get("power_w"), rec.get("model")

        # НАША мгновенная мощность — той же формулой, что бейдж карты: доля яркости × кривая.
        # Берём из состояния сущности лампы (единственный источник, который знает яркость).
        lamp_st = self.hass.states.get(cfg.get("lamp_entity") or "")
        our_w = None
        bri = None
        if lamp_st is not None and power_w is not None:
            if lamp_st.state == "on":
                bri = lamp_st.attributes.get("brightness")
                frac = (bri / 255.0) if bri is not None else 1.0
                our_w = round(power_at(model, power_w, frac), 2)
            elif lamp_st.state == "off":
                our_w = 0.0            # standby = 0 (по замеру, см. ENERGY_CALC_MODEL)

        relay_p = self.hass.states.get(cfg.get("relay_power") or "")
        relay_e = self.hass.states.get(cfg.get("relay_energy") or "")
        relay_sw = self.hass.states.get(cfg.get("relay_switch") or "")
        # ⚠ приводим к Вт·ч / Вт: Shelly отдаёт энергию в кВт·ч (иначе ratio ушёл бы в 1000 раз)
        relay_wh, unit_e, known_e = _scaled(relay_e, _ENERGY_TO_WH)
        relay_w, unit_p, known_p = _scaled(relay_p, _POWER_TO_W)
        # НАША энергия = записанный накопитель + незакрытый хвост (см. `_open_tail_wh`)
        our_wh = None
        if rec:
            tail = self._open_tail_wh(devsn, power_w, model)
            our_wh = round(rec.get("energy_wh", 0.0) + tail, 3)
        return {
            "ts": dt_util.now().isoformat(timespec="seconds"),
            "our_w": our_w,
            "our_wh": our_wh,
            "relay_w": None if relay_w is None else round(relay_w, 2),
            "relay_wh": None if relay_wh is None else round(relay_wh, 4),
            "relay_unit_e": unit_e, "relay_unit_p": unit_p,
            "unit_unknown": (not known_e) or (not known_p),
            "relay_on": relay_sw.state if relay_sw is not None else None,
            "lamp_state": lamp_st.state if lamp_st is not None else None,
            "lamp_bri": bri,
            "power_w": power_w,
            "model": model,
        }

    @callback
    def _tick(self, _now) -> None:
        s = self.store.session
        if not s or not s.get("running"):
            self._disarm()
            return
        try:
            self.store.add_sample(self.read_now(s))
        except Exception as err:  # noqa: BLE001 — сателлит не должен ронять HA
            _LOGGER.error("сверка энергии: срез не снят: %s", err)

    # ── управление сессией (зовёт WS) ─────────────────────────────────────────
    async def async_start_session(self, cfg: dict) -> dict:
        base = self.read_now(cfg)
        cfg = {
            **cfg,
            "started_at": base["ts"],
            "running": True,
            "base_our_wh": base["our_wh"],
            "base_relay_wh": base["relay_wh"],
            "power_w": base["power_w"],
            "model": base["model"],
        }
        await self.store.async_start(cfg)
        self.store.add_sample(base)
        self._arm(int(cfg.get("interval_s") or DEFAULT_INTERVAL_S))
        _LOGGER.info("сверка энергии НАЧАТА: %s (devSn %s) · база наш=%s Вт·ч реле=%s Вт·ч · "
                     "power_w=%s кривая=%s", cfg.get("lamp_entity"), cfg.get("devsn"),
                     base["our_wh"], base["relay_wh"], base["power_w"], base["model"])
        return cfg

    async def async_set_running(self, running: bool) -> None:
        if running:
            await self.store.async_resume()
            s = self.store.session or {}
            self._arm(int(s.get("interval_s") or DEFAULT_INTERVAL_S))
        else:
            self._disarm()
            await self.store.async_stop()
        _LOGGER.info("сверка энергии: %s", "продолжена" if running else "приостановлена")

    async def async_rebase(self) -> dict:
        s = self.store.session
        if not s:
            return {}
        cur = self.read_now(s)
        await self.store.async_rebase({
            "base_our_wh": cur["our_wh"], "base_relay_wh": cur["relay_wh"],
            "started_at": cur["ts"], "power_w": cur["power_w"], "model": cur["model"]})
        _LOGGER.info("сверка энергии: база сброшена на наш=%s реле=%s",
                     cur["our_wh"], cur["relay_wh"])
        return cur


def get_collector(hass: HomeAssistant) -> VerifyCollector | None:
    return hass.data.get(_COLLECTOR_KEY)
