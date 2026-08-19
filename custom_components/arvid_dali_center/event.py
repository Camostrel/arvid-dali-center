"""Платформа event — панели/кнопки DALI (devType 03xx).

Ф3: панель шлёт devStatus при нажатии — кидаем HA-событие с типом нажатия и номером
кнопки (keyNo). Для поворотных (dpid 4) в атрибуты кладём value (0..255).
"""

from __future__ import annotations

import logging

from homeassistant.components.event import EventEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import panel_ops
from .const import DOMAIN, EVENT_PANEL
from .coordinator import (
    SIGNAL_DEV_UPDATE,
    DaliBusEntity,
    DaliGatewayHub,
    dev_state_key,
)
from .naming import device_name
from .transport.decode import devtype_name

_LOGGER = logging.getLogger(__name__)

# dpid → ЖЕСТ (см. EVENTS.md / decode.PRESS). Сам список типов событий (`key3_click` и т.д.)
# живёт в panel_ops — чистой логикой под тестами, как и состав ячейки привязки.
PRESS_EVENT = {1: "click", 2: "hold", 3: "double", 4: "rotate", 5: "hold_end"}


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    hub: DaliGatewayHub = hass.data[DOMAIN][entry.entry_id]
    entities: list[DaliPanelEvent] = []
    for dev in hub.devices_snapshot():
        if str(dev.get("devType")).startswith("03"):
            custom = hub.custom_name(dev)
            entities.append(DaliPanelEvent(hub, dev, custom))
    _LOGGER.info("%s [%s]: создано панелей %d", DOMAIN, hub.gw_sn, len(entities))
    async_add_entities(entities)

    # динамика панелей: factory + adder в хабе → reconcile создаёт новые без reload
    def _factory(dev):
        if not str(dev.get("devType")).startswith("03"):
            return None
        custom = hub.custom_name(dev)
        return DaliPanelEvent(hub, dev, custom)
    hub.register_platform("event", async_add_entities, _factory)


class DaliPanelEvent(DaliBusEntity, EventEntity):
    """Панель DALI 03xx — события кнопок."""

    _attr_has_entity_name = False
    _attr_icon = "mdi:gesture-tap-button"
    _role = "event"

    def __init__(self, hub: DaliGatewayHub, dev: dict, custom: str = "") -> None:
        self._hub = hub
        self._gw_sn = hub.gw_sn
        self._devtype = str(dev.get("devType"))
        self._channel = dev.get("channel")
        self._address = dev.get("address")
        self._key = dev_state_key(self._devtype, self._channel, self._address)
        self._avail_key = self._key   # реальный online/offline из onlineStatus
        # типы событий зависят от числа клавиш конкретной панели (0302 → 2, 0308 → 8)
        self._attr_event_types = panel_ops.key_event_types(self._devtype)
        devsn = dev.get("devSn") or ""
        uid_base = hub.identity(dev)         # единый ключ идентичности (см. хаб)
        self._attr_unique_id = f"{uid_base}_event"
        # ИМЕНОВАННАЯ → подпись = custom; БЕЗЫМЯННАЯ → подпись НЕ задаём (v1.2.7), HA выведет её
        # из entity_id (форс coordinator: keypanel_<кнопок>_<addr>_<sn5> / rotary_…).
        # Имя УСТРОЙСТВА — по devSn (стабильно навсегда).
        self._attr_name = custom or None
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, uid_base)},
            via_device=(DOMAIN, hub.gw_sn),
            manufacturer="Sunricher",
            model=devtype_name(self._devtype),
            name=custom or device_name(self._devtype, devsn, self._address),
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
        for p in data.get("property", []) or []:
            gesture = PRESS_EVENT.get(p.get("dpid"))
            if not gesture:
                continue
            # ТИП события = key<N>_<жест>, чтобы клавиша выбиралась прямо в триггере
            # автоматизации. Жест и номер остаются и в атрибутах — по ним фильтруют те,
            # кому нужна «любая клавиша» (так делает blueprint автояркости).
            key_no = p.get("keyNo")
            cand = panel_ops.key_event_type(self._devtype, key_no, gesture)
            etype = gesture
            if cand:
                etype = cand
            elif key_no is not None and gesture != "rotate":
                # клавиша есть, а типа для неё нет — панель отдала номер вне своего devType.
                # Не глушим: событие выпускаем голым жестом, но след в журнале оставляем.
                _LOGGER.warning("панель %s [%s]: keyNo=%s вне диапазона devType %s — "
                                "событие выпущено как «%s»", self._key, self._gw_sn,
                                key_no, self._devtype, gesture)
            attrs = {"key_no": key_no, "gesture": gesture}
            if gesture == "rotate":
                attrs["value"] = p.get("value")
            self._trigger_event(etype, attrs)
            self.async_write_ha_state()
            # СОБЫТИЕ НА ШИНУ HA (v1.2.46) — на нём стоят триггеры устройства
            # (`device_trigger.py`). Почему не смена состояния сущности: состояние у event
            # меняется и при перезагрузке/восстановлении сущности, то есть автоматизация на
            # state-триггере может выстрелить БЕЗ нажатия. Событие шины бывает только от
            # реального нажатия.
            self.hass.bus.async_fire(EVENT_PANEL, {
                "entity_id": self.entity_id,
                "device_id": self.registry_entry.device_id if self.registry_entry else None,
                "event_type": etype,
                "key_no": key_no,
                "gesture": gesture,
                **({"value": p.get("value")} if gesture == "rotate" else {}),
            })
