"""Платформа sensor — датчики DALI.

Ф3: движение 0201 — текстовый sensor (`no_motion`/`motion`/`vacant`/`occupied`/
`occupied_hold` — латиницей с v1.2.45, чтобы автоматизации сравнивали машинные
значения); освещённость 0202 — числовой sensor (люкс). Датчики сами шлют
devStatus — обновляемся по диспетчеру SIGNAL_DEV_UPDATE. Движение и люкс могут
делить devSn (одно физическое устройство) → одна карточка, две сущности.
"""

from __future__ import annotations

import logging

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import LIGHT_LUX
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import (
    SIGNAL_DEV_UPDATE,
    DaliBusEntity,
    DaliGatewayHub,
    dev_state_key,
)
from .naming import device_name, sensor_body, sensor_name
from .transport.decode import MOTION

_LOGGER = logging.getLogger(__name__)

MOTION_TYPE = "0201"
LUX_TYPE = "0202"


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    hub: DaliGatewayHub = hass.data[DOMAIN][entry.entry_id]
    entities: list[SensorEntity] = []
    for dev in hub.devices_snapshot():
        t = str(dev.get("devType"))
        if t not in (MOTION_TYPE, LUX_TYPE):
            continue
        custom = hub.custom_name(dev)
        if t == MOTION_TYPE:
            entities.append(DaliMotionSensor(hub, dev, custom))
        else:
            entities.append(DaliLuxSensor(hub, dev, custom))
    _LOGGER.info("%s [%s]: создано датчиков %d", DOMAIN, hub.gw_sn, len(entities))
    async_add_entities(entities)

    # динамика датчиков: factory + adder в хабе → reconcile создаёт новые без reload
    def _factory(dev):
        t = str(dev.get("devType"))
        custom = hub.custom_name(dev)
        if t == MOTION_TYPE:
            return DaliMotionSensor(hub, dev, custom)
        if t == LUX_TYPE:
            return DaliLuxSensor(hub, dev, custom)
        return None
    hub.register_platform("sensor", async_add_entities, _factory)


class _DaliSensorBase(DaliBusEntity, SensorEntity):
    """Общая база датчиков: привязка к устройству и подписка на события."""

    _attr_has_entity_name = False
    _role: str | None = None   # роль для карты unique_id хаба (motion/lux)

    def __init__(self, hub: DaliGatewayHub, dev: dict, custom: str = "") -> None:
        self._hub = hub
        self._gw_sn = hub.gw_sn
        self._devtype = str(dev.get("devType"))
        self._channel = dev.get("channel")
        self._address = dev.get("address")
        self._custom = custom
        self._key = dev_state_key(self._devtype, self._channel, self._address)
        self._avail_key = self._key   # реальный online/offline из onlineStatus
        # devSn общий у движения/люкса → одна карточка-устройство
        self._devsn = dev.get("devSn") or ""
        self._uid_base = hub.identity(dev)   # единый ключ идентичности (см. хаб)
        # имя УСТРОЙСТВА — по devSn (стабильно навсегда); custom (продакшен) — как есть
        dev_name = custom or device_name(self._devtype, self._devsn, self._address)
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, self._uid_base)},
            via_device=(DOMAIN, hub.gw_sn),
            manufacturer="Sunricher",
            model="DALI Sensor",
            name=dev_name,
        )

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(
            async_dispatcher_connect(self.hass, SIGNAL_DEV_UPDATE, self._on_update)
        )
        self._wire_avail()   # доступность: связь шлюза + online устройства
        self._bus_register()  # трекинг в хабе (reconcile/resync/gone + резолв в карточке)

    @callback
    def _on_update(self, gw_sn: str, key: str, data: dict) -> None:
        if gw_sn != self._gw_sn or key != self._key:
            return
        self._handle(data.get("property", []) or [])
        self.async_write_ha_state()

    def _handle(self, props: list[dict]) -> None:  # переопределяется
        raise NotImplementedError


class DaliMotionSensor(_DaliSensorBase):
    """Движение 0201 — состояние из ЗАКРЫТОГО списка (v1.2.46).

    `device_class=ENUM` + `options` говорят HA, что значений конечное число: их подставляет
    UI автоматизаций (не надо помнить и печатать руками) и корректно ведёт статистика.

    ⚠ В `options` — ВСЕ пять значений декодера, хотя рабочий датчик пользователя шлёт три
    (`no_motion`/`motion`/`vacant`). Объявить только три нельзя: если какой-нибудь датчик
    пришлёт код 4/5, состояние окажется вне списка — HA ругается, а значение теряется.
    Список обязан покрывать всё, что умеет отдать `decode.MOTION`."""

    _attr_icon = "mdi:motion-sensor"
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = list(MOTION.values())
    _role = "motion"

    def __init__(self, hub: DaliGatewayHub, dev: dict, custom: str = "") -> None:
        super().__init__(hub, dev, custom)
        self._attr_unique_id = f"{self._uid_base}_motion"
        # ИМЕНОВАННЫЙ → подпись = ms_<тело>; БЕЗЫМЯННЫЙ → подпись НЕ задаём (None), HA выведет её
        # из entity_id сам (v1.2.7). entity_id безымянного форсит coordinator: motion_<addr>_<sn5>.
        self._attr_name = sensor_name(self._devtype, sensor_body(custom)) if custom else None

    def _handle(self, props: list[dict]) -> None:
        for p in props:
            val, dpid = p.get("value"), p.get("dpid")
            # у 0201 код состояния в value либо в dpid (см. EVENTS.md)
            code = val if val is not None else dpid
            text = MOTION.get(code)
            if text:
                self._attr_native_value = text


class DaliLuxSensor(_DaliSensorBase):
    """Освещённость 0202 — люкс."""

    _attr_device_class = SensorDeviceClass.ILLUMINANCE
    _attr_native_unit_of_measurement = LIGHT_LUX
    _attr_state_class = SensorStateClass.MEASUREMENT
    _role = "lux"

    def __init__(self, hub: DaliGatewayHub, dev: dict, custom: str = "") -> None:
        super().__init__(hub, dev, custom)
        self._attr_unique_id = f"{self._uid_base}_lux"
        # ИМЕНОВАННЫЙ → подпись = il_<тело>; БЕЗЫМЯННЫЙ → подпись НЕ задаём (см. движение выше)
        self._attr_name = sensor_name(self._devtype, sensor_body(custom)) if custom else None

    def _handle(self, props: list[dict]) -> None:
        for p in props:
            if p.get("dpid") == 4 and p.get("value") is not None:
                self._attr_native_value = int(p["value"])
