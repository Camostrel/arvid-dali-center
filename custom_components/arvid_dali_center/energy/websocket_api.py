"""WS-команды энергомониторинга (сателлит).

Регистрируются отдельно от основных команд (`async_register_energy`), читают/пишут только
EnergyStore — управляющих путей не касаются. Имена `arvid_dali_center/energy_*`.
"""

from __future__ import annotations

import logging

import voluptuous as vol
from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import (
    area_registry as ar_reg,
    device_registry as dr_reg,
    floor_registry as fr_reg,
)

from ..const import DOMAIN
from .curves import CURVES, USER_CURVES_FILE, apply_user_curves, curve_list, parse_user_curves
from .rest import _resolve_device   # резолв имя/зона/этаж по devSn (общий с REST)
from .store import get_energy_store

_LOGGER = logging.getLogger(__name__)


def _hub(hass: HomeAssistant, gw_sn: str):
    for h in hass.data.get(DOMAIN, {}).values():
        if getattr(h, "gw_sn", None) == gw_sn:
            return h
    return None


@callback
def async_register_energy(hass: HomeAssistant) -> None:
    for cmd in (ws_energy_data, ws_energy_set_params, ws_energy_set_tariff,
                ws_curves_reload):
        websocket_api.async_register_command(hass, cmd)


def load_user_curves_blocking(hass) -> tuple:
    """Прочитать файл кривых (блокирующе — звать через executor). Возврат: (сколько, проблемы).

    Файла нет — это НОРМА (объект без замеров работает на встроенных `linear`/`lbs`), поэтому
    молча возвращаем ноль. А вот битый файл — не норма: проблемы уходят наверх и в лог.
    """
    import yaml

    path = hass.config.path(USER_CURVES_FILE)
    try:
        with open(path, encoding="utf-8") as f:
            raw = yaml.safe_load(f)
    except FileNotFoundError:
        return 0, []
    except (OSError, yaml.YAMLError) as err:
        return 0, [f"{USER_CURVES_FILE}: {err}"]
    curves, problems = parse_user_curves(raw)
    problems += apply_user_curves(curves)
    for problem in problems:
        _LOGGER.warning("кривые мощности: %s", problem)
    if curves:
        _LOGGER.info("кривые мощности: загружено %s из %s (%s)",
                     len(curves), USER_CURVES_FILE, ", ".join(sorted(curves)))
    return len(curves), problems


@websocket_api.require_admin
@websocket_api.websocket_command({
    vol.Required("type"): "arvid_dali_center/curves_reload",
})
@websocket_api.async_response
async def ws_curves_reload(hass, connection, msg):
    """Перечитать файл кривых мощности БЕЗ рестарта HA.

    Пусконаладчик снимает таблицу «яркость → ватты» прямо на объекте и правит файл в File
    Editor; ждать перезапуск ядра ради этого незачем. Отдаём и проблемы разбора — человек
    должен видеть, что именно не легло, а не гадать, почему кривая не появилась в списке.
    """
    count, problems = await hass.async_add_executor_job(load_user_curves_blocking, hass)
    connection.send_result(msg["id"], {"ok": not problems, "loaded": count,
                                       "problems": problems, "curves": curve_list()})


@websocket_api.websocket_command({
    vol.Required("type"): "arvid_dali_center/energy_data",
    vol.Required("gw_sn"): str,
})
@callback
def ws_energy_data(hass, connection, msg):
    """Данные отчёта по лампам шлюза: параметры (power_w/model) + накопитель (energy_wh/
    on_time_s) + area/floor. (HA-сенсоры энергии и период-срезы через recorder убраны
    в v0.46 — отчёт «за всё время» из нашего стора, не из statistics.)"""
    store = get_energy_store(hass)
    hub = _hub(hass, msg["gw_sn"])
    if not store or not hub:
        connection.send_error(msg["id"], "not_found", "шлюз/стор не найден")
        return
    dreg, areg, freg = dr_reg.async_get(hass), ar_reg.async_get(hass), fr_reg.async_get(hass)
    lamps = []
    for dev in hub.devices_snapshot():
        if not str(dev.get("devType", "")).startswith("01"):
            continue
        devsn = dev.get("devSn")
        if not devsn:
            continue
        rec = store.get(devsn)
        # зона/этаж из реестра HA — для фильтров отчёта в карточке
        _name, area, floor = _resolve_device(dreg, areg, freg, devsn)
        lamps.append({
            "devSn": devsn,
            "power_w": rec.get("power_w"),
            "model": rec.get("model"),
            "energy_wh": rec.get("energy_wh", 0.0),
            "on_time_s": rec.get("on_time_s", 0.0),
            "area": area,
            "floor": floor,
        })
    # ПОКРЫТИЕ (E3, v1.2.19): сколько ламп имеют заданный `power_w` — без него лампа даёт 0 Вт
    # (её потребление в отчёт НЕ попадает). Это сигнал доверия к числам: карточка показывает
    # «N/M ламп покрыто», непокрытые — на виду (принцип «проблемы видимы»). Энергия непокрытых
    # НЕИЗВЕСТНА и в сумме просто отсутствует — поэтому метрика по КОЛИЧЕСТВУ ламп, не по Вт·ч.
    total = len(lamps)
    covered = sum(1 for l in lamps if l["power_w"] is not None)
    coverage = {"total": total, "covered": covered, "uncovered": total - covered,
                "pct": round(100.0 * covered / total, 1) if total else None}
    # v1.1.3: список кривых драйверов — карточка предлагает их выбором в параметрах лампы
    # (отдаём здесь, а не отдельной командой: карточка и так зовёт energy_data перед отчётом)
    connection.send_result(msg["id"], {
        "tariff": store.tariff, "lamps": lamps, "curves": curve_list(),
        "coverage": coverage})


@websocket_api.require_admin
@websocket_api.websocket_command({
    vol.Required("type"): "arvid_dali_center/energy_set_params",
    vol.Required("devsns"): [str],
    vol.Optional("power_w"): vol.Any(None, vol.Coerce(float)),
    vol.Optional("model"): vol.Any(None, str),
})
@callback
def ws_energy_set_params(hass, connection, msg):
    """Массовое задание параметров: power_w/model набору ламп (по devSn)."""
    store = get_energy_store(hass)
    if not store:
        connection.send_error(msg["id"], "not_found", "стор не найден")
        return
    power_w = msg.get("power_w")
    model = msg.get("model")
    # v1.1.3: `model` теперь НЕ вольная метка, а имя КРИВОЙ драйвера (energy/curves.py) —
    # неизвестное имя молча считалось бы по линейной. Отказываем явно (проблемы видимы).
    if model not in (None, "") and model not in CURVES:
        connection.send_error(msg["id"], "invalid_format",
                              f"неизвестная кривая {model!r}; доступны: {', '.join(CURVES)}")
        return
    count = 0
    for devsn in msg["devsns"]:
        if not devsn:
            continue
        store.set_params(devsn, power_w=power_w, model=model)
        count += 1
    connection.send_result(msg["id"], {"ok": True, "count": count})


@websocket_api.require_admin
@websocket_api.websocket_command({
    vol.Required("type"): "arvid_dali_center/energy_set_tariff",
    vol.Required("tariff"): vol.Any(None, vol.Coerce(float)),
})
@callback
def ws_energy_set_tariff(hass, connection, msg):
    """Тариф ₽/кВт·ч (персистится). None/пусто — стоимость не считаем."""
    store = get_energy_store(hass)
    if not store:
        connection.send_error(msg["id"], "not_found", "стор не найден")
        return
    store.set_tariff(msg.get("tariff"))
    connection.send_result(msg["id"], {"ok": True, "tariff": store.tariff})
