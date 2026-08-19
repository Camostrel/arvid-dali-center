"""Смена РЕЖИМА ИДЕНТИЧНОСТИ — операция, а не переключатель (Н10 плана).

━━ ПОЧЕМУ ОПЕРАЦИЯ ━━
Флаг сам по себе ничего не решает: после смены режима у всех устройств меняется `unique_id`,
платформы заводят НОВЫЕ сущности, а прежние остаются сиротами — недоступными, с занятыми
`entity_id`, — и вдобавок уезжают в корзину HA, откуда воскресают при возврате режима (закон 1,
docs/DEBT.md §T5).

Разбор кода показал и вторую половину проблемы: «Стереть данные» шлюз ПУСТЫМ не оставляет —
она чистит сторы и сбрасывает имена к шаблону, но не сносит сущности (снос ушёл бы в ту же
корзину) и не очищает кеш устройств. То есть последовательность «стёр данные → переключил»
без этой операции оставляла бы сирот, причём на 27 контроллерах — незаметно.

Поэтому смена режима — ОДНО действие: снести старое поколение → почистить хранилища → вымести
корзину → записать новый режим → попросить пересканировать.

━━ ЧТО ЭТО НЕ ДЕЛАЕТ ━━
* не переключает режим САМА и ни при каких условиях (годность серийников оценивает человек —
  решение пользователя 2026-08-19);
* не трогает DALI-шину: ни одной команды на железо. Группы, привязки кнопок и автояркость
  живут В КОНТРОЛЛЕРАХ и переживают смену режима — мы меняем только своё представление;
* не переносит данные между режимами. Миграции нет и не планируется: объект при необходимости
  разворачивают заново (решение пользователя).
"""

from __future__ import annotations

import logging

from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

from .const import DOMAIN
from .identity import MODES, normalize_mode

_LOGGER = logging.getLogger(__name__)


def current_mode(hass: HomeAssistant) -> str:
    from .store import get_identity_mode
    return get_identity_mode(hass)


def scope(hass: HomeAssistant) -> dict:
    """Что затронет смена режима — БЕЗ изменений. Это текст для подтверждения человеку.

    Показываем именно масштаб, а не «всё будет хорошо»: сколько устройств и сущностей потеряют
    свои записи. Человек должен видеть цену до нажатия, а не после.
    """
    ent_reg = er.async_get(hass)
    dev_reg = dr.async_get(hass)
    gateways, devices, entities = [], 0, 0
    for hub in hass.data.get(DOMAIN, {}).values():
        gw_sn = getattr(hub, "gw_sn", None)
        if not gw_sn:
            continue
        devs = hub.devices_snapshot()
        gateways.append({"gw_sn": gw_sn, "devices": len(devs)})
        devices += len(devs)
        for dev in devs:
            for _role, platform, uid in hub._roles_for_dev(dev):
                if ent_reg.async_get_entity_id(platform, DOMAIN, uid):
                    entities += 1
    # карточки устройств этой интеграции (кроме самих шлюзов и групп)
    cards = 0
    for entry in dev_reg.devices.values():
        idents = {i[1] for i in (entry.identifiers or set()) if i[0] == DOMAIN}
        if idents and not any("_group_" in i for i in idents):
            cards += 1
    return {"mode": current_mode(hass), "modes": list(MODES), "gateways": gateways,
            "devices": devices, "entities": entities, "device_cards": cards}


async def switch_mode(hass: HomeAssistant, new_mode: str) -> dict:
    """Переключить режим идентичности, снеся поколение старых ключей. ДЕСТРУКТИВНО.

    Порядок важен: сущности → карточки → хранилища → корзина → флаг. Сначала снимаем то, что
    ключуется старым способом, и только потом меняем правило — иначе сбор ключей пошёл бы уже
    по новому режиму и не нашёл бы ничего (а мусор остался бы навсегда).
    """
    from .store import (get_device_store, get_identity_mode, get_identity_mode_store,
                        purge_gateway_everywhere)
    from .websocket_api import purge_registry_trash

    mode = normalize_mode(new_mode)
    was = get_identity_mode(hass)
    if mode == was:
        return {"ok": True, "changed": False, "mode": mode}

    ent_reg = er.async_get(hass)
    dev_reg = dr.async_get(hass)
    removed_entities = removed_cards = 0
    uids: set[str] = set()
    idents: set[str] = set()

    for hub in list(hass.data.get(DOMAIN, {}).values()):
        gw_sn = getattr(hub, "gw_sn", None)
        if not gw_sn:
            continue
        for dev in hub.devices_snapshot():
            for _role, platform, uid in hub._roles_for_dev(dev):
                uids.add(uid)
                eid = ent_reg.async_get_entity_id(platform, DOMAIN, uid)
                if eid:
                    ent_reg.async_remove(eid)
                    removed_entities += 1
            # карточка устройства ключуется тем же способом, что и сущности
            ident = hub.identity(dev)
            if ident:
                idents.add(ident)
                card = dev_reg.async_get_device(identifiers={(DOMAIN, ident)})
                if card:
                    dev_reg.async_remove_device(card.id)
                    removed_cards += 1
        # хранилища этого шлюза (имена, параметры, предпочтения, энергия, устройства шины)
        await purge_gateway_everywhere(hass, gw_sn)
        # кеш в памяти: устройства придут заново физическим сканом, и это ЕДИНСТВЕННЫЙ
        # достоверный источник (закон 2). Оставить кеш — значит показывать записи со старыми
        # ключами до первого скана.
        with hub._lock:
            hub.devices.clear()
        hub.online_map.clear()
        hub.sensor_active.clear()
        ds = get_device_store(hass)
        if ds:
            await ds.purge_gateway(gw_sn)

    # КОРЗИНА: без этого возврат режима поднимет старые записи вместе с именами, областями и
    # ярлыками — ровно то, из-за чего «бывшие» всплывали годами (T5).
    trash = purge_registry_trash(hass, uids, idents)

    store = get_identity_mode_store(hass)
    if store:
        await store.async_set(mode)
    _LOGGER.warning("РЕЖИМ ИДЕНТИЧНОСТИ %s → %s: снято сущностей %d, карточек %d, корзина %s",
                    was, mode, removed_entities, removed_cards, trash)
    return {"ok": True, "changed": True, "mode": mode, "was": was,
            "removed_entities": removed_entities, "removed_cards": removed_cards, "trash": trash}
