"""Триггеры УСТРОЙСТВА для панелей ARVID DALI Center (v1.2.46).

Что это даёт человеку: в автоматизации выбираешь устройство «Панель …» и из списка —
«Клавиша 3 · нажатие». Ни имени сущности, ни типов событий знать не нужно.

⚠ ГЛАВНОЕ РЕШЕНИЕ: триггер цепляется к СОБЫТИЮ ШИНЫ (`arvid_dali_center_event`), а НЕ к
смене состояния event-сущности. У `event`-сущности состояние — метка времени последнего
нажатия, и оно меняется ещё и при перезагрузке HA / восстановлении сущности: триггер по
состоянию сработал бы БЕЗ нажатия (лампы включились бы сами при рестарте). Событие шины
бывает только от реального нажатия. Тот же приём у upstream-интеграции.

Список типов берётся из `event_types` самой сущности (их формирует
`panel_ops.key_event_types` по devType), поэтому 8-клавишная панель предложит key1..key8,
а 4-клавишная — только свои.
"""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant.components.device_automation import DEVICE_TRIGGER_BASE_SCHEMA
from homeassistant.components.homeassistant.triggers import event as event_trigger
from homeassistant.const import (
    CONF_DEVICE_ID,
    CONF_DOMAIN,
    CONF_ENTITY_ID,
    CONF_EVENT_DATA,
    CONF_PLATFORM,
    CONF_TYPE,
)
from homeassistant.core import CALLBACK_TYPE, HomeAssistant
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity import get_capability
from homeassistant.helpers.trigger import TriggerActionType, TriggerInfo
from homeassistant.helpers.typing import ConfigType

from .const import DOMAIN, EVENT_PANEL

_LOGGER = logging.getLogger(__name__)

TRIGGER_SCHEMA = DEVICE_TRIGGER_BASE_SCHEMA.extend({
    vol.Required(CONF_ENTITY_ID): cv.entity_id_or_uuid,
    vol.Required(CONF_TYPE): str,
})


async def async_get_triggers(hass: HomeAssistant, device_id: str) -> list[dict[str, Any]]:
    """Что можно ждать от этого устройства — по одному триггеру на тип события."""
    reg = er.async_get(hass)
    triggers: list[dict[str, Any]] = []
    for entry in er.async_entries_for_device(reg, device_id):
        if entry.domain != "event" or entry.platform != DOMAIN:
            continue
        # event_types объявляет сама сущность (зависит от devType панели)
        for event_type in get_capability(hass, entry.entity_id, "event_types") or []:
            triggers.append({
                CONF_PLATFORM: "device",
                CONF_DEVICE_ID: device_id,
                CONF_DOMAIN: DOMAIN,
                CONF_ENTITY_ID: entry.entity_id,
                CONF_TYPE: event_type,
            })
    return triggers


async def async_attach_trigger(
    hass: HomeAssistant,
    config: ConfigType,
    action: TriggerActionType,
    trigger_info: TriggerInfo,
) -> CALLBACK_TYPE:
    """Подписать автоматизацию на событие шины (не на состояние — см. шапку модуля)."""
    event_config = event_trigger.TRIGGER_SCHEMA({
        CONF_PLATFORM: "event",
        event_trigger.CONF_EVENT_TYPE: EVENT_PANEL,
        CONF_EVENT_DATA: {
            "entity_id": config[CONF_ENTITY_ID],
            "event_type": config[CONF_TYPE],
        },
    })
    return await event_trigger.async_attach_trigger(
        hass, event_config, action, trigger_info, platform_type="device")


async def async_validate_trigger_config(
    hass: HomeAssistant, config: ConfigType
) -> ConfigType:
    """Проверка конфигурации триггера."""
    return TRIGGER_SCHEMA(config)
