"""WS API сателлита сверки энергии (для карточки `arvid-energy-verify`)."""

from __future__ import annotations

import logging

import voluptuous as vol
from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant, callback

from .collector import DEFAULT_INTERVAL_S, get_collector
from .store import get_verify_store

_LOGGER = logging.getLogger(__name__)


def _summary(store, collector) -> dict:
    """Сводка для карточки: сессия + дельты + последние срезы.

    `ratio` = Δнаш/Δреле (цель 1.0). ⚠ Считать его можно только на ДЛИННОМ окне: счётчик Вт·ч
    реле квантован тиками 0.2–0.4 Вт·ч (docs/ENERGY_CALC_MODEL.md §5), поэтому отдаём ещё и
    возраст сессии — карточка предупреждает, пока он меньше часа."""
    s = store.session
    if not s:
        return {"session": None, "archive": store.archive}
    live = collector.read_now(s) if collector else {}
    d_our = d_relay = ratio = None
    if live.get("our_wh") is not None and s.get("base_our_wh") is not None:
        d_our = round(live["our_wh"] - s["base_our_wh"], 3)
    if live.get("relay_wh") is not None and s.get("base_relay_wh") is not None:
        d_relay = round(live["relay_wh"] - s["base_relay_wh"], 3)
    if d_our is not None and d_relay:
        ratio = round(d_our / d_relay, 3)
    return {
        "session": s,
        "live": live,
        "delta": {"our_wh": d_our, "relay_wh": d_relay, "ratio": ratio},
        # хвост для графика/таблицы — не весь массив (карточке столько не нужно)
        "samples": store.samples[-180:],
        "archive": store.archive,
    }


@callback
def async_register_verify(hass: HomeAssistant) -> None:
    """Зарегистрировать WS-команды сателлита (зовётся из async_setup_verify)."""

    @websocket_api.websocket_command({vol.Required("type"): "arvid_dali_center/verify_state"})
    @callback
    def ws_state(hass, connection, msg):
        store = get_verify_store(hass)
        if not store:
            connection.send_error(msg["id"], "not_ready", "сателлит сверки не поднят")
            return
        connection.send_result(msg["id"], _summary(store, get_collector(hass)))

    @websocket_api.require_admin
    @websocket_api.websocket_command({
        vol.Required("type"): "arvid_dali_center/verify_start",
        vol.Required("devsn"): str,
        vol.Required("lamp_entity"): str,
        vol.Required("relay_energy"): str,
        vol.Optional("relay_power", default=""): str,
        vol.Optional("relay_switch", default=""): str,
        vol.Optional("name", default=""): str,
        vol.Optional("interval_s", default=DEFAULT_INTERVAL_S): vol.All(int, vol.Range(10, 3600)),
    })
    @websocket_api.async_response
    async def ws_start(hass, connection, msg):
        store, coll = get_verify_store(hass), get_collector(hass)
        if not store or not coll:
            connection.send_error(msg["id"], "not_ready", "сателлит сверки не поднят")
            return
        cfg = {k: msg[k] for k in ("devsn", "lamp_entity", "relay_energy", "relay_power",
                                   "relay_switch", "name", "interval_s")}
        started = await coll.async_start_session(cfg)
        warn = None
        if started.get("power_w") is None:
            warn = ("у лампы НЕ задана полная мощность (power_w) — наш расчёт даст 0 Вт·ч. "
                    "Задайте её в «Энергия → Параметры ламп», иначе сверять нечего")
        connection.send_result(msg["id"], {"ok": True, "session": started, "warning": warn})

    @websocket_api.require_admin
    @websocket_api.websocket_command({
        vol.Required("type"): "arvid_dali_center/verify_control",
        vol.Required("action"): vol.In(["pause", "resume", "rebase", "clear"]),
    })
    @websocket_api.async_response
    async def ws_control(hass, connection, msg):
        store, coll = get_verify_store(hass), get_collector(hass)
        if not store or not coll:
            connection.send_error(msg["id"], "not_ready", "сателлит сверки не поднят")
            return
        act = msg["action"]
        if act in ("pause", "resume"):
            await coll.async_set_running(act == "resume")
        elif act == "rebase":
            await coll.async_rebase()
        elif act == "clear":
            coll.stop()
            await store.async_archive()      # итог сессии не теряем — уходит в архив
            await store.async_clear()
        connection.send_result(msg["id"], {"ok": True, **_summary(store, coll)})

    @websocket_api.websocket_command({vol.Required("type"): "arvid_dali_center/verify_csv"})
    @callback
    def ws_csv(hass, connection, msg):
        """Все срезы одной строкой CSV — карточка отдаёт их файлом (анализ в Excel)."""
        store = get_verify_store(hass)
        if not store:
            connection.send_error(msg["id"], "not_ready", "сателлит сверки не поднят")
            return
        # relay_* уже НОРМИРОВАНЫ в Вт/Вт·ч; исходные единицы пишем рядом (Shelly даёт кВт·ч)
        cols = ["ts", "our_w", "our_wh", "relay_w", "relay_wh", "relay_unit_p", "relay_unit_e",
                "relay_on", "lamp_state", "lamp_bri", "power_w", "model"]
        lines = [";".join(cols)]
        for smp in store.samples:
            lines.append(";".join("" if smp.get(c) is None else str(smp.get(c)) for c in cols))
        connection.send_result(msg["id"], {"csv": "\n".join(lines), "rows": len(store.samples)})

    for cmd in (ws_state, ws_start, ws_control, ws_csv):
        websocket_api.async_register_command(hass, cmd)
