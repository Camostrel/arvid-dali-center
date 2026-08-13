"""ARVID DALI Center — интеграция HA на собственном транспорте (см. transport/).

Модель: ОДНА запись ConfigEntry на шлюз → отдельная карточка-контроллер в HA.
Каждая запись поднимает свой DaliGatewayHub и регистрирует устройство-шлюз
(родитель для устройств шины через via_device). Сущности — Ф2 light, далее Ф3.
"""

from __future__ import annotations

import contextlib
import logging

from homeassistant.config_entries import SOURCE_IMPORT, ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr

from . import websocket_api
from .const import CONF_BIND_IP, CONF_GW_SN, DOMAIN
from .coordinator import DaliGatewayHub
from .discovery import get_connect_semaphore, get_discovery
from .energy import async_setup_energy
from .eventlog import async_setup_eventlog, get_eventlog
from .health import async_setup_health
from .services import async_setup_services
from .store import async_setup_store, get_name_store, get_store
from .transport.core import auto_iface, discover_all

_LOGGER = logging.getLogger(__name__)

# Платформы сущностей: свет + датчики + панели + переключатели активации датчиков.
PLATFORMS: list[str] = ["light", "sensor", "event", "switch"]


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    """Однократная инициализация: журнал + хранилище параметров + WebSocket API."""
    await async_setup_eventlog(hass)
    await async_setup_store(hass)
    websocket_api.async_register(hass)
    # Сателлит энергомониторинга (стор+интегратор+WS). Развязан: только подписки, в
    # управляющие пути не пишет (см. energy/__init__.py, docs/PLAN_ENERGY.md).
    await async_setup_energy(hass)
    # Сателлит мониторинга здоровья устройств (лог ошибок). Тоже развязан (см. health/).
    await async_setup_health(hass)
    # Сервисы массового управления датчиками (v1.2.24): автояркость вкл/выкл и расписание —
    # адресуются штатным target (area/device/entity), см. docs/SERVICES.md.
    await async_setup_services(hass)
    # ВРЕМЕННЫЙ сателлит СВЕРКИ энергоучёта с реле (v1.2.32, docs/ENERGY_VERIFY.md). Развязан:
    # только читает (EnergyStore + состояния сущностей), на шину не ходит, в управление не лезет.
    # Падение сателлита НЕ должно ронять интеграцию — поэтому под try (исследовательский код).
    try:
        from .verify import async_setup_verify
        await async_setup_verify(hass)
    except Exception as err:  # noqa: BLE001
        _LOGGER.error("сателлит сверки энергии не поднялся (интеграция работает дальше): %s", err)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Поднять запись одного шлюза."""
    # Миграция со старой hub-записи (без gw_sn): разворачиваем в записи на каждый шлюз.
    if CONF_GW_SN not in entry.data:
        await _migrate_hub_entry(hass, entry)
        return False

    bind_ip = entry.data[CONF_BIND_IP]
    gw_sn = entry.data[CONF_GW_SN]
    hub = DaliGatewayHub(hass, bind_ip, gw_sn)
    hub.entry_id = entry.entry_id          # для перепривязки «переехавших» сущностей (reconcile)
    # ВСЕГДА регистрируем хаб — чтобы шлюз был виден даже когда контроллер выключен
    # (раньше при недоступности setup падал ConfigEntryNotReady и шлюз пропадал с карточки).
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = hub
    hub.load_persisted()                   # устройства+группы из персиста (даже без связи)

    connected = False
    try:
        # ОБЩИЙ залп discovery (один на все шлюзы — как «Add gateway»): на старте объекта с
        # 20-60 шлюзами 40 записей берут результат ОДНОГО залпа, а не плодят 40 поисков.
        # gw может быть None (шлюз offline / не в залпе) → async_connect сам сделает точечный
        # fallback. Подключение — через общий семафор (по N за раз, бережём пул потоков HA).
        gw = await get_discovery(hass).get(bind_ip, gw_sn)
        async with get_connect_semaphore(hass):
            await hub.async_connect(gw)    # gw из общего discovery-залпа; None (не в залпе/оффлайн)
                                           # → connect бросит, except ниже пометит оффлайн, watchdog поднимет
        await hub.async_load_devices()     # кеш устройств шлюза (+персист) для платформ
        await hub.async_load_groups()      # DALI-группы с составом (контроллер ∪ персист)
        connected = True
    except Exception as err:  # noqa: BLE001 — оффлайн не критичен, watchdog восстановит
        hub.mark_offline()
        el = get_eventlog(hass)
        if el:
            el.log(gw_sn, "conn", f"setup: шлюз оффлайн ({err}) — показываю из персиста, "
                   "watchdog восстановит", level="warn")

    hub.ensure_watchdog()                  # ВСЕГДА — авто-реконнект каждые 20с

    dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, gw_sn)},
        manufacturer="Sunricher",
        model="DALI Gateway",
        name=f"DALI Gateway {gw_sn}",
        sw_version=hub.sw or None,
    )

    # Активация датчиков 02xx и очистка чужих — только при живой связи (иначе нет
    # смысла; после реконнекта датчики активирует watchdog через _async_on_reconnected).
    if connected:
        await _set_sensors_active(hub)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    # S1 (v1.2.3): группы слушают лампы АДРЕСНО, по entity_id. На старте группа могла быть
    # добавлена раньше, чем лампы получили entity_id → её подписка оказалась бы пустой, и до
    # первого скана группа не видела бы своих ламп. Здесь все платформы уже подняты — пересобираем.
    hub.resubscribe_groups()
    if connected:
        _cleanup_foreign_devices(hass, entry, gw_sn, hub)
    return True


async def async_remove_config_entry_device(
    hass: HomeAssistant, entry: ConfigEntry, device: dr.DeviceEntry
) -> bool:
    """Разрешить УДАЛЕНИЕ устройства из UI Home Assistant (v1.2.47).

    Зачем: авто-чистки у нас нет намеренно (`_cleanup_foreign_devices` громко ругается, но
    ничего не сносит — авто-деструктив запрещён), а без этого хука HA не показывает у наших
    устройств кнопку «Удалить». В итоге в записи копились ПУСТЫЕ карточки устройств —
    наследие смены идентичности: `identifiers` = `devSn`, а при невалидном/пропавшем serial
    ключ падает на адресный (`gw:ch:addr`). Сменился serial (или шлюз перестал отдавать
    мусорный) — сущности уехали под новый идентификатор, старая запись осталась ни с чем.
    Теперь решение принимает человек кнопкой, как и требует принцип «проблемы видимы».

    ⛔ ЖИВОЕ устройство удалить НЕ даём. Оно всё равно вернётся при следующем скане, а
    удаление МЯГКОЕ (закон 1): запись ляжет в корзину вместе с личным именем и воскреснет
    вместе с ним — человек получит ровно то, от чего избавлялся. Живое сносят «Забыть» /
    «Стереть данные», где чистятся и наши сторы.
    """
    hub: DaliGatewayHub | None = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    own = {i[1] for i in device.identifiers if i[0] == DOMAIN}
    if not own:
        return True
    # сам шлюз — не удаляем (его убирают удалением записи интеграции)
    if hub and hub.gw_sn in own:
        _LOGGER.warning("удаление устройства ШЛЮЗА %s отклонено — удаляйте саму интеграцию",
                        hub.gw_sn)
        return False
    if hub:
        live = {d.get("devSn") for d in hub.devices_snapshot() if d.get("devSn")}
        live |= {f"{hub.gw_sn}:{d.get('channel')}:{d.get('address')}"
                 for d in hub.devices_snapshot()}
        if own & live:
            _LOGGER.warning("удаление устройства %s отклонено: оно ЖИВОЕ в кеше шлюза %s — "
                            "вернётся при следующем скане, а имя воскреснет из корзины "
                            "(закон 1). Для живого — «Забыть» / «Стереть данные»",
                            own, hub.gw_sn)
            return False
    # мусорная запись: чистим и НАШИ сторы по этому ключу, иначе имя вернётся из NameStore
    # при повторном появлении такого же идентификатора
    ns = get_name_store(hass)
    ps = get_store(hass)
    for key in own:
        if ns:
            await ns.async_set(key, "")     # пустое имя = удаление ключа (NameStore)
        if ps:
            await ps.async_remove(key)
    _LOGGER.info("устройство %s удалено из реестра вручную (сущностей нет, сторы почищены)",
                 own)
    return True


async def _set_sensors_active(hub: DaliGatewayHub) -> None:
    """Активировать датчики 02xx при старте — ТОЙ ЖЕ реализацией, что скан и реконнект.

    🔴 Fix (v1.2.49). Здесь жила СВОЯ копия активации: она слала `setSensorOnOff=true` ВСЕМ
    датчикам подряд, тогда как `_rearm_sensors` (скан/реконнект) уважает «выключен вручную»
    (`SensorPrefStore`) и не расталкивает зомби. Решение v1.2.23 «не будить выключённое»
    соблюдалось на одном пути из двух: после рестарта HA осознанно выключенный датчик
    физически включался, а тумблер `*_act` показывал «выключен» (он читает персист и на шину
    ничего не шлёт) — UI и железо расходились до первого клика.
    Предпочтения к этому моменту уже в памяти: `load_sensor_prefs()` зовётся из
    `load_persisted()`, то есть раньше этого вызова.
    """
    await hub.async_rearm_sensors()


def _cleanup_foreign_devices(
    hass: HomeAssistant, entry: ConfigEntry, gw_sn: str, hub: DaliGatewayHub
) -> None:
    """Отвязать от этой записи устройства чужих шлюзов (наследие мультишлюзовой записи).

    Валидные идентификаторы записи = сам шлюз (gw_sn) + devSn его устройств. Всё, что
    привязано к записи, но не входит в этот набор, — чужое: убираем у него ссылку на
    нашу запись (само устройство останется под своей записью)."""
    valid = {gw_sn} | {d.get("devSn") for d in hub.devices_snapshot() if d.get("devSn")}
    # ⚠ v1.2.14 (A3): ПУСТОЕ ЗНАНИЕ ≠ «устройств нет». Если про устройства шлюза мы не знаем
    # НИЧЕГО (персист пуст и физического скана ещё не было — напр. сразу после «Стереть данные»
    # или у только что добавленного шлюза), то судить, что «чужое», НЕ НА ЧЕМ: любая запись
    # реестра выглядела бы чужой, и мы бы отвязали (а с гейтом ниже — и снесли) ВСЕ устройства
    # записи. Пропускаем чистку целиком — она не срочная и повторится на следующем setup.
    if len(valid) <= 1:
        _LOGGER.debug("шлюз %s: кеш устройств пуст — чистку чужих записей пропускаю "
                      "(нет основания судить, что чужое)", gw_sn)
        return
    # devSn, принадлежащие ДРУГИМ ЖИВЫМ шлюзам — чтобы отличить «реально чужое» (пересечение
    # шлюзов, аномалия) от «осиротевшее наследие» (нестабильный/пропавший serial, ничьё).
    others: set[str] = set()
    for other in hass.data.get(DOMAIN, {}).values():
        if other is hub:
            continue
        others |= {d.get("devSn") for d in other.devices_snapshot() if d.get("devSn")}
    dev_reg = dr.async_get(hass)
    for device in dr.async_entries_for_config_entry(dev_reg, entry.entry_id):
        own = {i[1] for i in device.identifiers if i[0] == DOMAIN}
        # ГРУППЫ этого шлюза ({gw}_group_*) — валидны: их идентификатор не devSn, но
        # отвязывать их НЕЛЬЗЯ (иначе сущности групп исчезают после каждого рестарта).
        if any(str(o).startswith(f"{gw_sn}_group_") for o in own):
            continue
        if own and not (own & valid):
            # ⚠ v1.2.14 (A2): прежний комментарий здесь обещал «отвязка безопасна в любом
            # случае — устройство останется под своей записью». ЭТО НЕВЕРНО. Если снимаемая
            # запись у устройства ПОСЛЕДНЯЯ, HA не просто отвязывает, а СНОСИТ устройство
            # целиком, каскадом с его сущностями — и укладывает их в корзину реестра
            # (`deleted_*`), откуда они потом воскресают по devSn (см. docs/DEBT §P0).
            # То есть рутинная «чистка» тихо уничтожала устройства. Авто-деструктива быть не
            # должно, а проблема должна быть ВИДНА → оставляем запись и говорим ГРОМКО;
            # решение (снести) принимает человек кнопкой «Забыть»/«Стереть данные».
            if not (set(device.config_entries) - {entry.entry_id}):
                _LOGGER.warning("устройство %s числится за записью шлюза %s, но в его кеше "
                                "отсутствует, а эта запись у устройства ПОСЛЕДНЯЯ — НЕ трогаю "
                                "(отвязка означала бы снос устройства вместе с сущностями). "
                                "Если оно лишнее — снести вручную: «Забыть» / «Стереть данные»",
                                own, gw_sn)
                continue
            # запись НЕ последняя → отвязка действительно безопасна (устройство останется под
            # другой своей записью). WARNING — только если оно реально принадлежит ДРУГОМУ
            # ЖИВОМУ шлюзу (пересечение = стоит знать); осиротевшее наследие → debug.
            dev_reg.async_update_device(device.id, remove_config_entry_id=entry.entry_id)
            if own & others:
                _LOGGER.warning("отвязано устройство %s от записи шлюза %s — принадлежит "
                                "ДРУГОМУ активному шлюзу (пересечение)", own, gw_sn)
            else:
                _LOGGER.debug("отвязано осиротевшее устройство %s от записи шлюза %s "
                              "(наследие/нестабильный serial, ничьё)", own, gw_sn)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Выгрузить запись: остановить сессию шлюза."""
    ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    hub: DaliGatewayHub | None = hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
    if hub:
        await hub.async_disconnect()
    return ok


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Шлюз УДАЛЯЮТ из config flow → почистить ШЛЮЗОВЫЕ сторы (v1.2.9, гибрид).

    Сущности/устройства в реестрах HA сносит сам HA. А наши персист-сторы он не знает.
    Чистим ТОЛЬКО данные, ПРОИЗВОДНЫЕ от шлюза (ключ gwSn) — иначе они всплывут при повторном
    добавлении того же шлюза (`load_persisted` поднимет устройства из `DeviceStore`):
      • DeviceStore — устройства шины (главный источник «призраков»);
      • GroupStore / GroupParamStore / PanelActStore — группы и привязки кнопок.
    ⚠ ДАННЫЕ ЧЕЛОВЕКА (имя/параметры/энергия, ключ devSn) НЕ трогаем: их ключ — devSn, а не gwSn,
    устройство могло ПЕРЕЕХАТЬ на другой шлюз (M1). Их чистит явная кнопка «Стереть данные»
    (`ws_wipe_gateway_data`), не автоматика — иначе удаление шлюза ради ПЕРЕСОЗДАНИЯ записи сожгло
    бы все имена без спроса (принцип «без авто-деструктива»).

    ⚠ v1.2.12 — ОТКАТ v1.2.11: попытка сносить здесь ещё и данные+реестры пользы не дала (личное имя
    воскресало не отсюда, а из КОРЗИНЫ реестров HA — `deleted_devices`/`deleted_entities`, см.
    `ws_wipe_gateway_data`), а риск потери имён при любом удалении шлюза добавила. Возвращено как было."""
    from .store import (
        get_device_store, get_group_param_store, get_group_store, get_panel_act_store,
    )
    gw_sn = entry.data.get(CONF_GW_SN) or entry.unique_id
    if not gw_sn:
        return
    total = 0
    # ЕДИНАЯ чистка по реестру (S5, v1.2.51): прежний ручной список знал четыре стора и не
    # знал про кросс-группы — они переживали удаление шлюза и ссылались на несуществующего
    # участника (DEBT §S, S4).
    from .store import purge_gateway_everywhere
    report = await purge_gateway_everywhere(hass, gw_sn)
    total = sum(report.values())
    _LOGGER.info("шлюз %s удалён из config flow → почищены шлюзовые сторы (%d записей: %s). "
                 "Имена/параметры/энергия (devSn) сохранены — стереть их можно кнопкой.",
                 gw_sn, total, report or "пусто")


async def _migrate_hub_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Старая запись-хаб (все шлюзы) → отдельные записи на каждый шлюз, затем удалить."""
    bind_ip = entry.data.get(CONF_BIND_IP) or await hass.async_add_executor_job(auto_iface)
    gws = await hass.async_add_executor_job(
        lambda: discover_all(bind_ip=bind_ip, timeout=8.0)
    )
    for gw in gws:
        if gw.get("gwSn"):
            hass.async_create_task(
                hass.config_entries.flow.async_init(
                    DOMAIN, context={"source": SOURCE_IMPORT},
                    data={CONF_GW_SN: gw["gwSn"], CONF_BIND_IP: bind_ip})
            )
    _LOGGER.warning("миграция: hub-запись развёрнута в %d записей на шлюзы, удаляю старую",
                    len(gws))
    hass.async_create_task(hass.config_entries.async_remove(entry.entry_id))
