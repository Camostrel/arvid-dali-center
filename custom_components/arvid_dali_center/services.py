"""Сервисы HA для МАССОВОГО управления датчиками (v1.2.24).

ЗАЧЕМ. Карточка правит по одному устройству — это годится для наладки, но не для эксплуатации:
нужно уметь «выключить автояркость во всём кабинете с клавиши на панели» и «задать окна работы
датчиков по этажу/объекту». Сервисы адресуются штатным `target` (entity/device/area), поэтому
кладутся и на карточку помещения, и в автоматизацию от кнопки, и вызываются извне.

ДВА СЕРВИСА:
  • `arvid_dali_center.set_autobrightness` — включить/выключить функцию датчика (`setSensorOnOff`,
    мануал стр. 50). Привязка на контроллере СОХРАНЯЕТСЯ, просто перестаёт работать → возврат
    мгновенный, шину не грузим.
  • `arvid_dali_center.set_effective_time` — окна работы (`runCondition` devType `0701`, мануал
    стр. 45, расшифровка — docs/PLAN_SENSOR_BINDINGS §H4).

⚠ ГЛАВНАЯ ЛОВУШКА `set_effective_time` (из захвата DALI Center 2026-07-29): `addSensorObj`
перезаписывает блок `data` ЦЕЛИКОМ. Поэтому сначала читаем текущую конфигурацию (`readSensor`),
забираем `luxRange` и `outputObj` (сами лампы!) и отправляем их обратно вместе с новым
расписанием — иначе назначение времени СНЕСЛО БЫ автояркость.

⚠ ЧАСЫ ШЛЮЗА. Окна исполняет САМ ШЛЮЗ по СВОИМ часам (v1.2.23 их читает при коннекте). Если
расхождение с HA велико — сервис предупреждает в отчёте: расписание сработает не вовремя, и
снаружи это невидимо.

⚠ ШИНА. Массовая запись по этажу — это ровно тот случай, когда шина ложится (см. CLAUDE.md:
автояркость + создание групп). Поэтому `pace` (пауза между устройствами) и ЧЕСТНЫЙ отчёт по
каждому датчику: сервис возвращает результат (`response_variable`), а не молчит.
"""

from __future__ import annotations

import asyncio
import logging

import voluptuous as vol
from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.service import async_extract_entity_ids

from .const import DOMAIN
from .schedule_util import WindowError, normalize_windows, validate_window, windows_overlap
from .sensor_ops import (
    DEVTYPE_LUX, FUNC_DPID, async_set_schedule, async_set_sensor_enabled,
)

_LOGGER = logging.getLogger(__name__)

SERVICE_SET_AUTOBRIGHTNESS = "set_autobrightness"
SERVICE_SET_EFFECTIVE_TIME = "set_effective_time"


# Пауза между устройствами по умолчанию: массовая запись кладёт шину (см. модульный docstring)
_DEFAULT_PACE = 0.3


def _valid_window(w: str) -> str:
    """Валидация окна для схемы сервиса (правила — в schedule_util, они же под тестами)."""
    try:
        return validate_window(w)
    except WindowError as err:
        raise vol.Invalid(str(err)) from err


SCHEMA_SET_AUTOBRIGHTNESS = cv.make_entity_service_schema({
    vol.Optional("enabled"): cv.boolean,
    # toggle — для КЛАВИШИ ПАНЕЛИ: одна кнопка и включает, и выключает (v1.2.31). Состояние
    # берём из СВОЕГО знания (`sensor_active`, персистится), а не опросом шлюза: опрос грузит
    # шину, а нажатие кнопки должно отрабатывать мгновенно.
    vol.Optional("toggle", default=False): cv.boolean,
})

SCHEMA_SET_EFFECTIVE_TIME = cv.make_entity_service_schema({
    vol.Optional("windows", default=list): vol.All(cv.ensure_list, [_valid_window]),
    vol.Optional("function", default="autobrightness"): vol.In(list(FUNC_DPID) + ["both"]),
    vol.Optional("clear", default=False): cv.boolean,
    vol.Optional("pace", default=_DEFAULT_PACE): vol.All(vol.Coerce(float), vol.Range(0, 10)),
})


def _hubs(hass: HomeAssistant) -> list:
    return list(hass.data.get(DOMAIN, {}).values())


def _targets(hass: HomeAssistant, entity_ids: set[str], devtypes: tuple[str, ...]) -> list[tuple]:
    """Целевые ДАТЧИКИ по выбранным сущностям: [(hub, key, dev)].

    `target` в HA раскрывается до entity_id (area/device → сущности). У одного датчика их
    несколько (`ms_`, `il_`, тумблер активности) — поэтому идём от УСТРОЙСТВА: для каждого
    известного хабу датчика смотрим, попала ли ХОТЬ ОДНА его сущность в выбор. Так «выделил
    область» и «выделил конкретный il_» работают одинаково, а лампы/группы из выбора
    отфильтровываются сами (у них не тот devType)."""
    out: list[tuple] = []
    for hub in _hubs(hass):
        for key, dev in hub.devices_snapshot_map().items():
            dt = str(dev.get("devType") or "")
            if dt not in devtypes or dev.get("zombie") or dev.get("orphan"):
                continue
            uids = hub.entity_uids_for_key(key)
            ents = [hub.bus_entity(uid) for uid in uids]
            eids = {getattr(e, "entity_id", None) for e in ents if e is not None}
            if eids & entity_ids:
                out.append((hub, key, dev))
    return out




async def async_setup_services(hass: HomeAssistant) -> None:
    """Зарегистрировать сервисы (однократно, из async_setup)."""

    async def _svc_autobrightness(call: ServiceCall) -> dict:
        toggle = call.data["toggle"]
        enabled = call.data.get("enabled")
        if enabled is None and not toggle:
            raise ServiceValidationError("укажите enabled: true/false или toggle: true")
        eids = await async_extract_entity_ids(hass, call)
        targets = _targets(hass, eids, (DEVTYPE_LUX,))
        if not targets:
            _LOGGER.warning("set_autobrightness: в цели нет датчиков освещённости (0202)")
            return {"changed": 0, "results": [],
                    "warning": "в цели нет датчиков освещённости (0202)"}
        if toggle:
            # Групповое переключение ПРЕДСКАЗУЕМО: если хоть один датчик цели сейчас включён —
            # выключаем ВСЮ цель, иначе включаем всю. Иначе одно нажатие оставляло бы помещение
            # в разнобое (часть включена, часть нет), и следующее нажатие ничего бы не меняло.
            from .coordinator import dev_state_key
            any_on = any(
                hub.sensor_active.get(
                    hub.sensor_pref_key(dev, dev_state_key(str(dev["devType"]), dev["channel"],
                                                           dev["address"])), True)
                for hub, _k, dev in targets)
            enabled = not any_on
            _LOGGER.info("set_autobrightness toggle: сейчас %s → ставлю %s (%d датчиков)",
                         "есть включённые" if any_on else "все выключены",
                         "ВКЛ" if enabled else "ВЫКЛ", len(targets))
        results = []
        for hub, _key, dev in targets:
            try:
                r = await async_set_sensor_enabled(hub, dev, enabled)
            except Exception as err:  # noqa: BLE001 — отчёт честнее падения посреди объекта
                _LOGGER.error("set_autobrightness %s addr%s: %s", hub.gw_sn, dev.get("address"), err)
                r = {"ok": False, "error": str(err)}
            results.append({"gw": hub.gw_sn, "address": dev.get("address"),
                            "devSn": dev.get("devSn"), "ok": r["ok"],
                            "error": r.get("error")})
        done = sum(1 for r in results if r["ok"])
        _LOGGER.info("set_autobrightness(%s): %d/%d датчиков", enabled, done, len(results))
        return {"changed": done, "total": len(results), "results": results}

    async def _svc_effective_time(call: ServiceCall) -> dict:
        windows = [] if call.data["clear"] else normalize_windows(call.data["windows"])
        if not windows and not call.data["clear"]:
            raise ServiceValidationError(
                "укажите windows (например ['08:00-17:30']) или clear: true")
        func = call.data["function"]
        pace = call.data["pace"]
        funcs = list(FUNC_DPID) if func == "both" else [func]
        eids = await async_extract_entity_ids(hass, call)
        results, warnings = [], []
        # пересечение окон: НЕ ошибка (как шлюз трактует перекрытие — не проверено), но человек
        # должен видеть, что задал двусмысленное расписание
        for w1, w2 in windows_overlap(windows):
            warnings.append(f"окна пересекаются: {w1} и {w2} — поведение шлюза не проверено")
        # часы шлюза: окна исполняет ШЛЮЗ по СВОИМ часам — предупредить, если они врут
        for hub in _hubs(hass):
            skew = getattr(hub, "gw_time_skew_s", None)
            if skew is not None and abs(skew) > 60:
                warnings.append(f"шлюз {hub.gw_sn}: часы расходятся с HA на {skew:+.0f} с — "
                                f"расписание сработает не вовремя")
        for fname in funcs:
            devtype, dpid = FUNC_DPID[fname]
            for hub, _key, dev in _targets(hass, eids, (devtype,)):
                try:
                    r = await async_set_schedule(hub, dev, dpid, windows)
                except Exception as err:  # noqa: BLE001
                    _LOGGER.error("set_effective_time %s addr%s: %s",
                                  hub.gw_sn, dev.get("address"), err)
                    r = {"ok": False, "error": str(err)}
                results.append({"gw": hub.gw_sn, "function": fname, "address": dev.get("address"),
                                "devSn": dev.get("devSn"), "ok": r["ok"],
                                "error": r.get("error"), "verify": r.get("verify")})
                await asyncio.sleep(pace)   # массовая запись кладёт шину — идём с паузой
        done = sum(1 for r in results if r["ok"])
        if not results:
            warnings.append("в цели нет подходящих датчиков")
        _LOGGER.info("set_effective_time(%s, окна=%s): %d/%d датчиков%s",
                     func, windows or "(снято)", done, len(results),
                     f"; предупреждения: {warnings}" if warnings else "")
        return {"changed": done, "total": len(results), "windows": windows,
                "warnings": warnings, "results": results}

    hass.services.async_register(DOMAIN, SERVICE_SET_AUTOBRIGHTNESS, _svc_autobrightness,
                                 schema=SCHEMA_SET_AUTOBRIGHTNESS,
                                 supports_response=SupportsResponse.OPTIONAL)
    hass.services.async_register(DOMAIN, SERVICE_SET_EFFECTIVE_TIME, _svc_effective_time,
                                 schema=SCHEMA_SET_EFFECTIVE_TIME,
                                 supports_response=SupportsResponse.OPTIONAL)
