"""WS-команды мониторинга здоровья (сателлit). Только чтение/правка HealthStore."""

from __future__ import annotations

import voluptuous as vol
from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.util import dt as dt_util

from .evaluator import KIND_LABEL, SIGNAL_HEALTH_UPDATE
from .store import DEFAULT_THRESHOLDS, get_health_store

_EVAL_KEY = "arvid_dali_center_health_evaluator"


def _enrich(rec: dict) -> dict:
    return {**rec, "kindLabel": KIND_LABEL.get(rec.get("kind"), rec.get("kind"))}


def _active_list(store) -> list[dict]:
    """Активные ошибки списком, старые сверху (общее для health_data и подписки)."""
    active = [_enrich({"key": k, **v}) for k, v in store.active.items()]
    active.sort(key=lambda r: r.get("since") or "")
    return active


@callback
def async_register_health(hass: HomeAssistant) -> None:
    for cmd in (ws_health_data, ws_health_subscribe,
                ws_health_set_thresholds, ws_health_clear_window):
        websocket_api.async_register_command(hass, cmd)


@websocket_api.require_admin
@websocket_api.websocket_command({vol.Required("type"): "arvid_dali_center/health_clear_window"})
@callback
def ws_health_clear_window(hass, connection, msg):
    """Очистить ОКНО «Восстановлено» (сдвиг метки на «сейчас»). Хранилище (30 дней) остаётся —
    его видно через CSV. Активные не трогаем (вычисляются живьём)."""
    store = get_health_store(hass)
    if not store:
        connection.send_error(msg["id"], "not_found", "стор не найден")
        return
    store.set_window_since(dt_util.utcnow().isoformat())
    connection.send_result(msg["id"], {"ok": True, "window_since": store.window_since})


@websocket_api.websocket_command({
    vol.Required("type"): "arvid_dali_center/health_data",
    vol.Optional("refresh"): bool,       # v1.2.5: полный пересчёт — ТОЛЬКО по явной просьбе
})
@callback
def ws_health_data(hass, connection, msg):
    """Снимок: пороги + активные ошибки + лог «Восстановлено» (новые сверху) + `generated_at`.

    ⚠ v1.2.5 — ЧИТАТЕЛЬ БОЛЬШЕ НЕ СЧИТАЕТ (долг HD1 закрыт). Раньше КАЖДЫЙ запрос безусловно
    запускал `evaluator.refresh()` — полный синхронный проход по всем устройствам всех шлюзов
    (4400 итераций + 4400 `states.get` + копии словарей под локами хабов + перестройка лога +
    запись на диск), в петле HA. Внешний потребитель, поллящий раз в 30 с, гонял этот обход
    2 раза в минуту круглосуточно — то есть СВОИМ поллингом управлял нагрузкой на HA.

    Теперь ответ — готовый снимок из стора (оценщик поддерживает его инкрементально: событие о
    конкретной сущности + будильник на дедлайн). Поллить можно как угодно часто.
    `refresh: true` — принудительный полный пересчёт (кнопка «Обновить» в нашей карточке).
    `generated_at` — когда снимок последний раз менялся (свежесть данных для внешних систем).
    """
    store = get_health_store(hass)
    if not store:
        connection.send_error(msg["id"], "not_found", "стор не найден")
        return
    evaluator = hass.data.get(_EVAL_KEY)
    if evaluator and msg.get("refresh"):
        evaluator.refresh()
    recovered = [_enrich(r) for r in reversed(store.recovered)]   # хранилище (30 дней), новые сверху
    connection.send_result(msg["id"], {
        "thresholds": store.thresholds, "active": _active_list(store), "recovered": recovered,
        "window_since": store.window_since,
        "generated_at": getattr(evaluator, "generated_at", None) if evaluator else None})


@websocket_api.websocket_command({vol.Required("type"): "arvid_dali_center/health_subscribe"})
@callback
def ws_health_subscribe(hass, connection, msg):
    """ЖИВАЯ подписка на активные ошибки (push, без поллинга).

    Отдаёт снимок сразу и затем шлёт его заново на каждый пересчёт оценщика
    (`SIGNAL_HEALTH_UPDATE`). Ошибок мало → шлём весь список, дифф не нужен.
    Оффлайн шлюза оценщик видит за ~1.5с (дебаунс сигналов связи), а не за `interval_min` —
    ради этого подписка и нужна. Лог «Восстановлено» сюда НЕ входит (тяжёлый) — он в `health_data`.
    Пересчёт не вызываем: подписчик получает результат ЧУЖОГО пересчёта, лишних проходов нет.
    """
    store = get_health_store(hass)
    if not store:
        connection.send_error(msg["id"], "not_found", "стор не найден")
        return

    @callback
    def _forward() -> None:
        connection.send_message(websocket_api.event_message(
            msg["id"], {"active": _active_list(store), "window_since": store.window_since}))

    connection.subscriptions[msg["id"]] = async_dispatcher_connect(
        hass, SIGNAL_HEALTH_UPDATE, _forward)
    connection.send_result(msg["id"], {
        "active": _active_list(store), "window_since": store.window_since})


@websocket_api.require_admin
@websocket_api.websocket_command({
    vol.Required("type"): "arvid_dali_center/health_set_thresholds",
    vol.Optional("motion_stuck_h"): vol.Coerce(float),
    vol.Optional("clear_h"): vol.Coerce(float),
    vol.Optional("lux_stale_h"): vol.Coerce(float),
    vol.Optional("grace_min"): vol.Coerce(float),     # v1.1.2: терпение (семантика аварии)
    vol.Optional("interval_min"): vol.Coerce(float),  # период обхода (нагрузка)
})
@callback
def ws_health_set_thresholds(hass, connection, msg):
    """Задать пороги; перевзвести оценщик (новый интервал) + сразу пересчитать."""
    store = get_health_store(hass)
    if not store:
        connection.send_error(msg["id"], "not_found", "стор не найден")
        return
    store.set_thresholds({k: msg[k] for k in DEFAULT_THRESHOLDS if k in msg})
    evaluator = hass.data.get(_EVAL_KEY)
    if evaluator:
        evaluator.rearm()
    connection.send_result(msg["id"], {"ok": True, "thresholds": store.thresholds})
