"""Платформа switch — точечная активация датчиков 02xx.

Для каждого датчика (движение 0201, освещённость 0202) — свой переключатель
активности (setSensorOnOff). Позволяет отключить, напр., освещённость в одном
датчике, оставив движение. Состояние оптимистичное (по умолчанию вкл — датчики
активируются при старте интеграции).
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import DaliBusEntity, DaliGatewayHub, dev_state_key
from .naming import device_name, sensor_body, sensor_name

_LOGGER = logging.getLogger(__name__)

SENSOR_TYPES = {"0201", "0202"}


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    hub: DaliGatewayHub = hass.data[DOMAIN][entry.entry_id]
    entities = []
    for dev in hub.devices_snapshot():
        if str(dev.get("devType")) not in SENSOR_TYPES:
            continue
        custom = hub.custom_name(dev)
        entities.append(DaliSensorActiveSwitch(hub, dev, custom))
    _LOGGER.info("%s [%s]: создано переключателей активации %d",
                 DOMAIN, hub.gw_sn, len(entities))
    async_add_entities(entities)

    # динамика: factory + adder в хабе → reconcile создаёт переключатель новых датчиков
    def _factory(dev):
        if str(dev.get("devType")) not in SENSOR_TYPES:
            return None
        custom = hub.custom_name(dev)
        return DaliSensorActiveSwitch(hub, dev, custom)
    hub.register_platform("switch", async_add_entities, _factory)


class DaliSensorActiveSwitch(DaliBusEntity, SwitchEntity):
    """Активность одного датчика (movement/illuminance) через setSensorOnOff."""

    _attr_has_entity_name = False
    _attr_entity_category = EntityCategory.CONFIG
    _attr_icon = "mdi:toggle-switch"

    def __init__(self, hub: DaliGatewayHub, dev: dict, custom: str = "") -> None:
        self._hub = hub
        self._devtype = str(dev.get("devType"))
        self._channel = dev.get("channel")
        self._address = dev.get("address")
        self._key = dev_state_key(self._devtype, self._channel, self._address)
        self._avail_key = self._key   # реальный online/offline из onlineStatus
        self._role = f"active_{self._devtype}"   # роль для трекинга/reconcile в хабе
        devsn = dev.get("devSn") or ""
        uid_base = hub.identity(dev)         # единый ключ идентичности (см. хаб)
        self._attr_unique_id = f"{uid_base}_active_{self._devtype}"
        # ИМЕНОВАННЫЙ → подпись = ms_/il_<тело>_act; БЕЗЫМЯННЫЙ → подпись НЕ задаём (v1.2.7),
        # HA выведет из entity_id (форс coordinator: <тип>_<addr>_<sn5>_active).
        self._attr_name = (sensor_name(self._devtype, sensor_body(custom)) + "_act") if custom \
            else None
        dev_name = custom or device_name(self._devtype, devsn, self._address)
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, uid_base)}, name=dev_name)
        self._attr_is_on = True  # датчики активируются при старте
        # Ключ предпочтения активности — по ИДЕНТИЧНОСТИ (devSn:devType), а не по адресу (Fix L,
        # v1.1.6): адресный ключ протухал при перенумерации — выключенный вручную датчик тихо
        # включался обратно, а «выключено» наследовало чужое устройство с этим адресом.
        # Ключ стабилен, обновлять при смене адреса не нужно.
        self._pref_key = hub.sensor_pref_key(dev, self._key)
        # дефолт активности (для перевзвода) — ТОЛЬКО если человек ничего не выбирал: иначе
        # создание сущности затирало бы поднятое из персиста «выключено» (v1.2.23)
        if self._pref_key not in hub.sensor_active:
            hub.set_sensor_active(self._pref_key, True)
        self._attr_is_on = hub.sensor_active.get(self._pref_key, True)

    async def async_added_to_hass(self) -> None:
        self._wire_avail()   # доступность: связь шлюза + online устройства
        self._bus_register()  # трекинг в хабе (reconcile/resync/gone)

    async def _set(self, value: bool) -> None:
        await self._hub.async_request(
            "setSensorOnOff", "setSensorOnOffRes", value=value,
            devType=self._devtype, channel=self._channel, address=self._address)
        self._attr_is_on = value
        # запомнить желаемую активность: после реконнекта/скана НЕ включаем выключенный вручную.
        # persist=True (v1.2.23): решение ЧЕЛОВЕКА переживает рестарт HA — раньше жило только в
        # памяти, и после перезапуска `_rearm_sensors` будил осознанно выключенный датчик.
        self._hub.set_sensor_active(self._pref_key, value, persist=True)
        self.async_write_ha_state()

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._set(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._set(False)
