"""WebSocket API для карточки управления ARVID DALI Center.

Команды (type = arvid_dali_center/...):
  gateways                — список шлюзов
  devices {gw_sn}         — устройства шлюза
  scan {gw_sn, flag?}     — скан с ЖИВЫМ логом найденных (event 'found' на каждое)
  get_param/set_param     — параметры драйвера (getDevParam/setDevParam)
  set_address             — смена DALI-адреса (РАЗРУШАЮЩЕЕ)
  restart_gateway         — перезапуск контроллера
  groups / group_write    — DALI-группы
Разрушающие команды — require_admin.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal

import voluptuous as vol

from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.util import dt as dt_util, slugify

from .const import DOMAIN
from .coordinator import dev_state_key
from .eventlog import SIGNAL_EVENTLOG, get_eventlog
from .store import (
    get_group_param_store,
    get_group_store,
    get_name_store,
    get_rotary_store,
    get_store,
    group_name_key,
    name_key,
    param_key,
    purge_identity_everywhere,
    purge_gateway_everywhere,
)
from . import group_ops, namemap, panel_ops
from .naming import sensor_body, sensor_name
from .transport.decode import devtype_name, is_valid_devsn

LIGHT_T = {"0101", "0102", "0103", "0104", "0105", "0106"}

_LOGGER = logging.getLogger(__name__)


def _hubs(hass: HomeAssistant) -> list:
    return list(hass.data.get(DOMAIN, {}).values())


def _find_hub(hass: HomeAssistant, gw_sn: str):
    for hub in _hubs(hass):
        if hub.gw_sn == gw_sn:
            return hub
    return None


def _param_store_key(hub, gw_sn: str, dev_type, channel, address) -> str | None:
    """Ключ ParamStore = `devSn` устройства. Без серийника — `None`, и параметры НЕ храним.

    v1.2.51: адресный фолбэк убран (решение пользователя 2026-08-07). Он казался безобидным,
    но именно такие ключи «Стереть данные» не видело — операция ходит по серийникам, — и
    данные всплывали на другом устройстве, занявшем адрес. Команда на шину уходит в любом
    случае: не сохраняем только НАШУ запись о ней."""
    rec = hub.devices.get(dev_state_key(str(dev_type), channel, address)) if hub else None
    sn = rec.get("devSn") if rec else None
    return sn if is_valid_devsn(sn) else None


async def _force_group_entity_id(hass: HomeAssistant, gw_sn: str, channel, group_id,
                                 name: str) -> None:
    """Дождаться регистрации сущности группы и ПРИНУДИТЕЛЬНО выставить entity_id =
    light.<имя> (как делает HA UI). Надёжно убирает «залипание» старого entity_id."""
    reg = er.async_get(hass)
    uid = f"{gw_sn}_group_{channel}_{group_id}"
    desired = f"light.{slugify(name) or 'group_' + str(group_id)}"
    for _ in range(30):                       # ждём появления сущности (до ~3с)
        eid = reg.async_get_entity_id("light", DOMAIN, uid)
        if eid:
            if eid != desired:
                # освободить желаемый entity_id, если его занял НАШ group-orphan
                holder = reg.async_get(desired)
                if (holder and holder.unique_id != uid
                        and str(holder.unique_id).startswith(f"{gw_sn}_group_")):
                    reg.async_remove(desired)
                with contextlib.suppress(Exception):
                    reg.async_update_entity(eid, new_entity_id=desired, name=None)
            return
        await asyncio.sleep(0.1)


def _entities(hass: HomeAssistant, gw_sn: str, d: dict, hub=None) -> dict:
    """entity_id рабочих сущностей устройства (чтобы карточка читала их состояние
    и управляла через стандартные сервисы HA).

    Резолв по СТАБИЛЬНОМУ ключу devType:channel:address из карты unique_id хаба
    (сущности регистрируют себя при создании) — устойчиво к порче devSn шлюзом.
    Fallback на старую схему по devSn — для оффлайн-устройств без живой сущности.

    `hub` принимаем готовым (в `ws_devices` он уже известен) — иначе на КАЖДОЕ устройство
    шёл линейный `_find_hub` → O(devices×шлюзы). Без hub — fallback на поиск."""
    reg = er.async_get(hass)
    if hub is None:
        hub = _find_hub(hass, gw_sn)
    t = str(d.get("devType"))
    ch, addr, sn = d.get("channel"), d.get("address"), d.get("devSn")
    key = dev_state_key(t, ch, addr)
    base = sn or f"{gw_sn}:{ch}:{addr}"          # для датчиков/панелей (без devType)
    light_uid = sn or f"{gw_sn}:{t}:{ch}:{addr}"  # для ламп (с devType)

    def _eid(role: str, domain: str, fallback_uid: str):
        uid = (hub.entity_uid(role, key) if hub else None) or fallback_uid
        return reg.async_get_entity_id(domain, DOMAIN, uid)

    out = {}
    if t in LIGHT_T:
        out["light"] = _eid("light", "light", light_uid)
    if t == "0201":
        out["motion"] = _eid("motion", "sensor", f"{base}_motion")
    if t == "0202":
        out["lux"] = _eid("lux", "sensor", f"{base}_lux")
    if t.startswith("03"):
        out["event"] = _eid("event", "event", f"{base}_event")
    return out


def _dev(d: dict, hass: HomeAssistant = None, gw_sn: str = "", hub=None) -> dict:
    out = {
        "devType": d.get("devType"),
        "typeName": devtype_name(str(d.get("devType"))),
        "channel": d.get("channel"),
        "address": d.get("address"),
        "name": d.get("name", ""),
        "devSn": d.get("devSn", ""),
        "status": d.get("status", ""),
        "zombie": bool(d.get("zombie")),   # не найден последним сканом (красный, запись цела)
        # ОСИРОТЕВШИЙ (v1.2.2): его адрес занят ДРУГИМ устройством, сам он на шине не найден.
        # Адресовать его тройкой (devType,channel,address) НЕЛЬЗЯ — она указывает на нового
        # жильца, и «Забыть» снесло бы НЕ ТОГО. Поэтому отдаём реальный `key` кеша.
        "orphan": bool(d.get("orphan")),
        "key": d.get("key", ""),
    }
    if hass is not None:
        out["entities"] = _entities(hass, gw_sn, d, hub)
        ns = get_name_store(hass)
        if ns:
            custom = ns.get(name_key(gw_sn, d.get("devType"), d.get("channel"),
                                     d.get("address"), d.get("devSn")))
            out["named"] = bool(custom)   # для фильтра «только неназванные» (пусконаладка)
            if custom:
                # датчики: показываем с префиксом по типу (ms_/il_ + тело), без русского;
                # лампы/панели — имя как есть
                t = str(d.get("devType"))
                out["name"] = sensor_name(t, sensor_body(custom)) if t in ("0201", "0202") else custom
    return out


@callback
def async_register(hass: HomeAssistant) -> None:
    """Зарегистрировать все WS-команды (один раз при старте)."""
    for cmd in (ws_gateways, ws_devices, ws_energy_live, ws_scan,
                ws_get_param, ws_set_param, ws_set_param_bulk, ws_get_group_param,
                ws_forget_device, ws_wipe_gateway_data,
                ws_set_address, ws_restart_gateway, ws_reset_addresses,
                ws_get_gw_net, ws_set_gw_net, ws_set_gw_name, ws_groups,
                ws_group_write, ws_identify, ws_create_group, ws_del_group,
                ws_set_group_members, ws_group_reload,
                ws_panel_bindings, ws_add_panel_obj, ws_del_panel_obj,
                ws_sensor_bindings, ws_add_sensor_obj, ws_del_sensor_obj,
                ws_set_lux_keep, ws_read_lux_keep, ws_clear_lux_keep,
                ws_set_sensor_enabled, ws_set_sensor_schedule, ws_sync_gw_time,
                ws_set_rotary_binding, ws_get_rotary_binding, ws_clear_rotary_binding,
                ws_get_sensor_param, ws_set_sensor_param, ws_rename,
                ws_rename_group, ws_events, ws_events_subscribe,
                # кросс-шлюзовые группы (отдельная модель, docs/CROSS_GATEWAY.md §2)
                ws_cross_groups, ws_group_slots, ws_create_cross_group,
                ws_set_cross_group_members, ws_del_cross_group, ws_cross_group_write,
                # чистка реестра HA от пустых карточек устройств (v1.2.47)
                ws_registry_orphans, ws_registry_cleanup, ws_registry_trash, ws_apply_log,
                ws_apply_stop,
                # карта имён для переезда объекта (v1.2.55): только чтение + область.
                # Имена применяет `ws_rename` — он не переписан ради этой задачи.
                ws_namemap_files, ws_namemap_table, ws_set_area, ws_set_group_labels,
                ):
        websocket_api.async_register_command(hass, cmd)


@websocket_api.websocket_command({vol.Required("type"): "arvid_dali_center/gateways"})
@callback
def ws_gateways(hass, connection, msg):
    out = [{
        "gwSn": h.gw_sn, "ip": (h.gw or {}).get("gwIp"),
        "name": (h.gw or {}).get("name"),         # имя шлюза (для визуального ориентира)
        "sw": h.sw, "fw": h.fw, "connected": h.connected,
        "state": getattr(h, "state", "online"),   # реальное состояние связи
        "devices": len(h.devices),
        # часы шлюза (v1.2.23): расписания датчиков исполняет ШЛЮЗ по СВОИМ часам — сбитые
        # часы = свет не вовремя, и это никак не видно. Показываем расхождение с HA.
        "gwTime": getattr(h, "gw_time", ""),
        "gwTimezone": getattr(h, "gw_timezone", ""),
        "gwTimeSkewS": getattr(h, "gw_time_skew_s", None),
        "busBusy": getattr(h, "bus_busy", False),
        # сущность «все лампы шлюза» (v1.2.47) — карточка жмёт её ШТАТНЫМ сервисом
        # light.turn_on/off, чтобы состояние сущности и кнопка не разъезжались. Резолвим
        # по unique_id: entity_id мог получить суффикс при коллизии имён.
        "allLights": er.async_get(hass).async_get_entity_id(
            "light", DOMAIN, f"{h.gw_sn}_all_lights"),
    } for h in _hubs(hass)]
    connection.send_result(msg["id"], {"gateways": out})


# ── Чистка реестра HA от ПУСТЫХ карточек устройств (v1.2.47) ─────────────────────────
# Откуда мусор: `identifiers` устройства = `devSn`, а при невалидном/пропавшем serial ключ
# падает на адресный (`gw:ch:addr`). Сменилась идентичность — сущности уехали под новый
# ключ, старая карточка осталась без единой сущности. Авто-чистки у нас нет намеренно
# (`_cleanup_foreign_devices` только ГРОМКО ругается — авто-деструктив запрещён), поэтому
# накопленное убирает человек: здесь он видит список и решает сам.


# ── КОРЗИНА РЕЕСТРОВ HA (v1.2.60) ───────────────────────────────────────────────────
# ЗАКОН 1 в самой острой форме: `async_remove` не стирает запись, а кладёт её в
# `deleted_entities` / `deleted_devices` вместе с `entity_id`, именем, областью и ярлыками —
# и возвращает всё это, когда снова появится тот же `unique_id`.
#
# 🔴 Прочитано в исходнике HA 2026.8.0 (`entity_registry.async_remove`):
#     orphaned_timestamp = None if config_entry_id else time.time()
# а штатная уборка (`async_purge_expired_orphaned_entities`) пропускает записи с `None`.
# ⟹ «30 дней» действуют ТОЛЬКО для записей, оставшихся без ConfigEntry. Всё, что мы удаляем
# при живой интеграции, лежит в корзине БЕССРОЧНО. Это и есть корень «вечного всплывания
# бывших»: P0-сага, `light.cross_1_2` вместо `103_dver_obshchii` (v1.2.58), чужие области (T4).
#
# ⚠ Штатной команды «удали из корзины» у HA НЕТ — ни сервиса, ни метода. Работаем с тем же
# объектом реестра, что и при удалении, но с его СЛУЖЕБНЫМ словарём. Публичным контрактом это
# не покрыто, поэтому: (1) всё под `contextlib.suppress` — сломается контракт, не рухнет
# интеграция; (2) сторож в `tests/test_registry_trash.py` падает, если структура изменилась.


def _trash_keys_for_uids(hass, unique_ids: set[str]) -> list[tuple]:
    """Ключи корзины сущностей для наших `unique_id`. Ключ HA — (domain, platform, unique_id)."""
    reg = er.async_get(hass)
    return [key for key in getattr(reg, "deleted_entities", {})
            if len(key) == 3 and key[1] == DOMAIN and key[2] in unique_ids]


@callback
def purge_registry_trash(hass, unique_ids: set[str], identifiers: set[str]) -> dict:
    """Вымести из корзины HA наши записи: сущности по `unique_id`, устройства по `identifiers`.

    Зовётся ТОЛЬКО из ручных операций («Забыть», «Стереть данные», кнопка «Реестр») — человек
    там уже сказал «убрать». Авто-деструктива не появляется: сама по себе чистка не запускается.
    """
    ent_gone: list[str] = []
    dev_gone: list[str] = []
    reg = er.async_get(hass)
    with contextlib.suppress(Exception):
        deleted = reg.deleted_entities
        for key in _trash_keys_for_uids(hass, unique_ids):
            entry = deleted.pop(key, None)
            if entry is not None:
                ent_gone.append(getattr(entry, "entity_id", str(key)))
        if ent_gone:
            reg.async_schedule_save()
    dev_reg = dr.async_get(hass)
    with contextlib.suppress(Exception):
        deleted_dev = dev_reg.deleted_devices
        for did, entry in list(deleted_dev.items()):
            ids = {i[1] for i in (getattr(entry, "identifiers", set()) or set())
                   if i[0] == DOMAIN}
            if ids & identifiers:
                deleted_dev.pop(did, None)
                dev_gone.append(sorted(ids)[0])
        if dev_gone:
            dev_reg.async_schedule_save()
    if ent_gone or dev_gone:
        _LOGGER.info("корзина реестров: вычищено сущностей %s, карточек %s (%s / %s)",
                     len(ent_gone), len(dev_gone), ent_gone[:8], dev_gone[:8])
    return {"entities": ent_gone, "devices": dev_gone}


def _orphan_devices(hass, hub, entry_id: str) -> list[dict]:
    """Карточки устройств записи, у которых нет ни одной ЖИВОЙ сущности.

    ⚠ v1.2.48: критерий был «сущностей НЕТ вовсе» и пропускал главный случай с объекта —
    карточку, где сущности ЕСТЬ, но интеграция их больше не создаёт. Такие HA держит сам из
    реестра: `state=unavailable` + атрибут `restored` («этот объект больше не предоставляется
    интеграцией»). Поэтому живой считаем сущность с состоянием БЕЗ `restored`.

    ⚠ Порядок работ важен: сначала обновиться и ОТСКАНИРОВАТЬ (после фикса reconcile
    физически живые устройства воскрешают свои сущности сами), и только потом чистить —
    иначе снесёшь карточку того, что просто ждало скана.
    """
    dev_reg, ent_reg = dr.async_get(hass), er.async_get(hass)
    live: set[str] = set()
    if hub:
        live = {d.get("devSn") for d in hub.devices_snapshot() if d.get("devSn")}
        live |= {f"{hub.gw_sn}:{d.get('channel')}:{d.get('address')}"
                 for d in hub.devices_snapshot()}
    out: list[dict] = []
    for device in dr.async_entries_for_config_entry(dev_reg, entry_id):
        own = {i[1] for i in device.identifiers if i[0] == DOMAIN}
        if hub and hub.gw_sn in own:                 # сам шлюз — не мусор
            continue
        entries = er.async_entries_for_device(ent_reg, device.id,
                                              include_disabled_entities=True)
        alive = 0
        for e in entries:
            st = hass.states.get(e.entity_id)
            if st is not None and not st.attributes.get("restored"):
                alive += 1
        if alive:
            continue                                 # карточка рабочая — не трогаем
        out.append({
            "device_id": device.id,
            "name": device.name_by_user or device.name or "",
            "identifiers": sorted(own),
            "entities": len(entries),                # сколько осиротевших сущностей уедет с ней
            # живое = идентификатор есть в кеше шлюза. Такое удалять нельзя: вернётся сканом,
            # а имя воскреснет из корзины реестра (закон 1) — чистка будет мнимой.
            "live": bool(own & live),
        })
    return out


# ───────────────────────── карта имён (переезд объекта) ─────────────────────────
# Разбор карты и сшивку со сканом делает `namemap.py` (чистые функции + тесты). Здесь только
# ввод-вывод: прочитать файл из /config/arvid_namemap/ и отдать таблицу карточке.
# ⚠ ИМЕНА ЭТИ КОМАНДЫ НЕ ПРИМЕНЯЮТ: карточка зовёт существующий `rename` по одному
# устройству — тот же путь, что при ручном переименовании (он уже проверен на железе).
# Область — отдельная команда `set_area` ниже, чтобы отказ одного не ломал другое.
NAMEMAP_DIR = "arvid_namemap"


def _namemap_files(hass) -> list[dict]:
    """Список карт в /config/arvid_namemap/ (блокирующий вызов — только в executor)."""
    import os
    path = hass.config.path(NAMEMAP_DIR)
    if not os.path.isdir(path):
        return []
    out = []
    for name in sorted(os.listdir(path)):
        if not name.lower().endswith(".csv"):
            continue
        full = os.path.join(path, name)
        try:
            out.append({"name": name, "size": os.path.getsize(full)})
        except OSError as err:              # файл исчез между listdir и stat — не прячем
            _LOGGER.warning("namemap: %s не прочитан: %s", name, err)
    return out


def _read_namemap(hass, name: str) -> str:
    """Прочитать карту по имени файла (блокирующий вызов — только в executor).

    Гейт на имя: принимаем только базовое имя без разделителей пути — иначе через `..`
    можно было бы прочитать любой файл конфигурации.
    """
    import os
    if not name or "/" in name or "\\" in name or name.startswith("."):
        raise ValueError(f"недопустимое имя файла: {name!r}")
    with open(os.path.join(hass.config.path(NAMEMAP_DIR), name), encoding="utf-8-sig") as fh:
        return fh.read()


@websocket_api.websocket_command({
    vol.Required("type"): "arvid_dali_center/namemap_files",
})
@websocket_api.async_response
async def ws_namemap_files(hass, connection, msg):
    """Какие карты лежат на боксе. Пусто → карточка не показывает режим вовсе."""
    files = await hass.async_add_executor_job(_namemap_files, hass)
    connection.send_result(msg["id"], {"dir": f"/config/{NAMEMAP_DIR}", "files": files})


@websocket_api.websocket_command({
    vol.Required("type"): "arvid_dali_center/namemap_table",
    vol.Required("gw_sn"): str,
    vol.Required("file"): str,
})
@websocket_api.async_response
async def ws_namemap_table(hass, connection, msg):
    """Сшить карту со сканом шлюза → таблица для карточки (ничего не меняет).

    Отдаём ВСЕ категории строк, включая «в карте есть, на шине нет» и «на шине есть, в карте
    нет»: человеку нужны не только совпадения — по первым он ищет лампу, по вторым дописывает
    карту. Проблемы разбора файла тоже уходят наверх, а не в лог: молча пропущенная строка
    оставила бы устройство без имени незаметно.
    """
    hub = _find_hub(hass, msg["gw_sn"])
    if not hub:
        connection.send_error(msg["id"], "not_found", "шлюз не найден")
        return
    try:
        text = await hass.async_add_executor_job(_read_namemap, hass, msg["file"])
    except (OSError, ValueError) as err:
        connection.send_error(msg["id"], "read_failed", str(err))
        return
    rows, problems = namemap.parse_map(text)
    devices = [_dev(d, hass, hub.gw_sn, hub) for d in hub.devices_snapshot()]
    table = namemap.stitch(rows, devices, hub.gw_sn)
    # ТЕКУЩАЯ область устройства (v1.2.57) — подмешиваем здесь: `stitch` чистый и реестра HA
    # не знает. Без неё карточка не отличала «область уже стоит» от «область не проставлена», и
    # строка с верным именем выпадала из работы навсегда (офисный прогон 2026-08-11: имена
    # легли, области нет — применить их вторым заходом было нечем).
    _fill_current_areas(hass, table, hub.gw_sn)
    for row in table:                      # «нужна работа» считает ОДНА реализация (под тестами)
        row["needs_work"] = namemap.needs_work(row)
    summary = namemap.summary(table)
    # Журнал: сводка + ПОИМЁННО всё, что останется без имени (решение пользователя).
    # Без этого «не назвалось» видно только на экране и теряется при уходе со вкладки.
    _LOGGER.info("namemap %s (%s): %s", msg["gw_sn"], msg["file"], summary)
    for problem in problems:
        _LOGGER.warning("namemap %s: карта — %s", msg["gw_sn"], problem)
    left = namemap.unnamed(table)
    if left:
        _LOGGER.warning("namemap %s: БЕЗ ИМЕНИ останется %s устройств:\n  %s",
                        msg["gw_sn"], len(left), "\n  ".join(left))
    connection.send_result(msg["id"], {
        "table": table, "summary": summary, "problems": problems,
        "gateways_in_map": namemap.gateways_in_map(rows),
    })


def _fill_current_areas(hass, table: list[dict], gw_sn: str) -> None:
    """Проставить строкам таблицы `area_current` — область, которая СЕЙЧАС стоит у устройства.

    Идентификатор карточки устройства собираем ТОЧНО так же, как `ws_set_area` и `rename`
    (`devSn`, иначе адресный ключ) — иначе смотрели бы не в ту карточку и показывали чушь.
    """
    from homeassistant.helpers import area_registry as ar

    dev_reg = dr.async_get(hass)
    areas = ar.async_get(hass)
    for row in table:
        if row.get("status") != namemap.ST_MATCHED:
            continue
        t = str(row.get("devType"))
        ident = (row.get("devSn")
                 or (f"{gw_sn}:{t}:{row.get('channel')}:{row.get('address')}" if t in LIGHT_T
                     else f"{gw_sn}:{row.get('channel')}:{row.get('address')}"))
        dev = dev_reg.async_get_device(identifiers={(DOMAIN, ident)})
        area = areas.async_get_area(dev.area_id) if dev and dev.area_id else None
        if area:
            row["area_current"] = area.id
            row["area_current_name"] = area.name


@websocket_api.require_admin
@websocket_api.websocket_command({
    vol.Required("type"): "arvid_dali_center/set_area",
    vol.Required("gw_sn"): str,
    vol.Required("devType"): str,
    vol.Required("channel"): int,
    vol.Required("address"): int,
    vol.Optional("devSn"): vol.Any(str, None),
    vol.Required("area_id"): str,            # ИДЕНТИФИКАТОР области (room_slug); пустой → снять
})
@websocket_api.async_response
async def ws_set_area(hass, connection, msg):
    """Назначить устройству область HA (отдельно от имени — см. блок выше).

    ⚠ Адресуем по `area_id` (`room_slug` проекта: `512_koridor`), а НЕ по видимому русскому
    имени: области заводятся ДО нас, и сверка русских строк хрупка (регистр, пробел, «ё»).
    Отсутствующую область НЕ создаём — расхождение состава областей с проектом должен разбирать
    человек, иначе рядом с настоящей тихо появится дубль.
    """
    from homeassistant.helpers import area_registry as ar

    dev_reg = dr.async_get(hass)
    t = str(msg["devType"])
    devsn = msg.get("devSn") or None
    # тот же идентификатор устройства, что у `rename` — иначе попадём не в ту карточку
    ident = (devsn or (f"{msg['gw_sn']}:{t}:{msg['channel']}:{msg['address']}" if t in LIGHT_T
                       else f"{msg['gw_sn']}:{msg['channel']}:{msg['address']}"))
    dev = dev_reg.async_get_device(identifiers={(DOMAIN, ident)})
    if not dev:
        connection.send_result(msg["id"], {"ok": False, "error": "device_not_found",
                                           "ident": ident})
        return

    area_id = (msg["area_id"] or "").strip()
    if not area_id:                                 # снять привязку
        dev_reg.async_update_device(dev.id, area_id=None)
        connection.send_result(msg["id"], {"ok": True, "area_id": None})
        return

    area = ar.async_get(hass).async_get_area(area_id)
    if area is None:
        connection.send_result(msg["id"], {"ok": False, "error": "area_not_found",
                                           "area_id": area_id})
        return
    dev_reg.async_update_device(dev.id, area_id=area.id)
    connection.send_result(msg["id"], {"ok": True, "area_id": area.id, "area": area.name})


@websocket_api.require_admin
@websocket_api.websocket_command({
    vol.Required("type"): "arvid_dali_center/set_group_labels",
    vol.Optional("gw_sn"): str,              # обычная группа: адрес = gw_sn+channel+groupId
    vol.Optional("channel"): int,
    vol.Optional("groupId"): int,
    vol.Optional("uid"): str,                # КРОСС-группа (v1.2.59): её `uid` из cross_groups
    vol.Required("labels"): [str],           # имена ярлыков; пустой список — снять все
})
@websocket_api.async_response
async def ws_set_group_labels(hass, connection, msg):
    """Навесить ярлыки HA на устройство DALI-группы (общие группы помещений).

    Ярлык — способ адресовать разом «весь общий свет здания» из автоматизаций и дашбордов
    (`target: label_id`). Вешаем на СУЩНОСТЬ группы (v1.2.65, решение пользователя): на
    карточке устройства ярлык работает для `target`, но не виден в списке сущностей, и
    потребители, которые ходят по сущностям, группу не находят.
    Отсутствующий ярлык создаём — иначе пришлось бы заводить его руками до первой раздачи.

    ⚠ v1.2.59: адресоваться можно и по `uid` — иначе КРОСС-шлюзовые группы недостижимы. Их
    карточка устройства ключуется собственным `uid` (`xgrp_…`), а не тройкой gw+ch+id: копии
    группы лежат на каждом участнике, и «шлюза владельца» у неё нет. На объекте это не
    мелочь: общие группы помещений там сплошь сквозные (лестницы), а ярлык `ba_area_light`
    нужен именно им.
    """
    from homeassistant.helpers import label_registry as lr

    dev_reg = dr.async_get(hass)
    uid = msg.get("uid")
    if not uid:
        if not all(k in msg for k in ("gw_sn", "channel", "groupId")):
            connection.send_error(msg["id"], "bad_request",
                                  "нужен либо uid (кросс-группа), либо gw_sn+channel+groupId")
            return
        uid = f"{msg['gw_sn']}_group_{msg['channel']}_{msg['groupId']}"
    dev = dev_reg.async_get_device(identifiers={(DOMAIN, uid)})
    if not dev:
        connection.send_result(msg["id"], {"ok": False, "error": "group_not_found", "uid": uid})
        return
    label_reg = lr.async_get(hass)
    ids = set()
    for name in msg["labels"]:
        name = (name or "").strip()
        if not name:
            continue
        label = label_reg.async_get_label_by_name(name)
        if label is None:
            label = label_reg.async_create(name)
            _LOGGER.info("set_group_labels: создан ярлык «%s» (%s)", name, label.label_id)
        ids.add(label.label_id)

    # 🔴 v1.2.65 — ЯРЛЫК НА СУЩНОСТЬ, а не на устройство (решение пользователя 2026-08-12).
    # Прежде вешали на карточку устройства: HA разворачивает её в сущности при адресации
    # `target: label_id`, но в СПИСКЕ СУЩНОСТЕЙ ярлык не виден, и внешние потребители,
    # которые ходят по сущностям (веб-интерфейс, автоматизации по label), группу не находили.
    ent_reg = er.async_get(hass)
    eid = ent_reg.async_get_entity_id("light", DOMAIN, uid)
    if not eid:
        connection.send_result(msg["id"], {"ok": False, "error": "entity_not_found", "uid": uid})
        return
    ent_reg.async_update_entity(eid, labels=ids)
    # переносим: те же ярлыки снимаем с карточки устройства, чтобы одна и та же метка не
    # висела в двух местах. ЧУЖИЕ ярлыки устройства не трогаем — их ставил человек.
    if dev.labels & ids:
        dev_reg.async_update_device(dev.id, labels=dev.labels - ids)
    _LOGGER.info("set_group_labels %s (%s) → %s", uid, eid, sorted(msg["labels"]) or "снято")
    connection.send_result(msg["id"], {"ok": True, "labels": sorted(ids), "entity_id": eid})


@websocket_api.require_admin
@websocket_api.websocket_command({
    vol.Required("type"): "arvid_dali_center/apply_log",
    vol.Required("script"): str,             # имя скрипта, напр. apply_voronezh.py
    vol.Optional("lines"): int,              # сколько последних строк вернуть (умолч. 200)
})
@websocket_api.async_response
async def ws_apply_log(hass, connection, msg):
    """Хвост журнала автопусконаладки + идёт ли она сейчас.

    Прогон объекта длится минуты (216 групп), а `shell_command` в HA рвёт процесс через 60 с —
    поэтому карточка запускает `run_apply.sh`, та уводит python в фон и пишет сюда. Карточке
    остаётся показывать журнал: без него человек не видит ни прогресса, ни отказов.

    ⚠ Только чтение и только из `/config/tools` — имя файла собираем сами из `script`,
    посторонние пути сюда не проходят.
    """
    base = str(msg["script"]).strip()
    if not base.endswith(".py") or "/" in base or ".." in base:
        connection.send_error(msg["id"], "bad_request", "ожидается имя скрипта вида apply_x.py")
        return
    stem = base[:-3]
    log_path = hass.config.path("tools", f"{stem}.log")
    pid_path = hass.config.path("tools", f"{stem}.pid")
    limit = int(msg.get("lines") or 200)

    def _read():
        text, running = "", False
        try:
            with open(log_path, encoding="utf-8", errors="replace") as f:
                text = "".join(f.readlines()[-limit:])
        except OSError:
            text = ""
        try:
            with open(pid_path, encoding="utf-8") as f:
                pid = int(f.read().strip())
            os.kill(pid, 0)                 # сигнал 0 — только проверка, что процесс жив
            running = True
        except (OSError, ValueError, ProcessLookupError):
            running = False
        return text, running

    text, running = await hass.async_add_executor_job(_read)
    connection.send_result(msg["id"], {"log": text, "running": running, "file": log_path})


@websocket_api.require_admin
@websocket_api.websocket_command({
    vol.Required("type"): "arvid_dali_center/apply_stop",
    vol.Required("script"): str,              # имя скрипта, напр. apply_voronezh.py
})
@websocket_api.async_response
async def ws_apply_stop(hass, connection, msg):
    """Остановить идущий прогон автопусконаладки — МЯГКО.

    Зачем команда, а не второй `shell_command`: тот пришлось бы заводить руками в
    `configuration.yaml` на каждом объекте, а забытая настройка означает прогон, который нечем
    прервать. Здесь всё внутри интеграции.

    ⚠ Останов МЯГКИЙ и это принципиально: создание группы = `delGroup` + `addGroup`, и обрыв
    между ними оставил бы группу СНЕСЁННОЙ. `SIGTERM` в сгенерированном скрипте только поднимает
    флаг, выход происходит МЕЖДУ записями (см. `stopped()` в шаблоне `tools/import_project.py`).
    Скрипты, сгенерированные ДО v1.2.69, обработчика не имеют — там `SIGTERM` завершит процесс
    сразу; такой прогон лучше не прерывать на фазе групп (карточка предупреждает: маркер
    «мягкая остановка поддерживается» печатается в журнал при старте).

    ⛔ `SIGKILL` не шлём НИКОГДА — процесс должен успеть закончить запись.
    """
    base = str(msg["script"]).strip()
    if not base.endswith(".py") or "/" in base or ".." in base:
        connection.send_error(msg["id"], "bad_request", "ожидается имя скрипта вида apply_x.py")
        return
    pid_path = hass.config.path("tools", f"{base[:-3]}.pid")

    def _stop():
        try:
            with open(pid_path, encoding="utf-8") as f:
                pid = int(f.read().strip())
        except (OSError, ValueError):
            return {"ok": False, "error": "прогон не запускался (нет pid-файла)"}
        try:
            os.kill(pid, 0)
        except OSError:
            return {"ok": False, "error": "прогон уже не выполняется"}
        # Сверяем, ЧЕЙ это pid: номера переиспользуются, и без проверки мы могли бы прибить
        # чужой процесс, занявший тот же номер. Адресуем идентичностью, а не номером.
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as f:
                cmdline = f.read().replace(b"\x00", b" ").decode("utf-8", "replace")
        except OSError:
            return {"ok": False, "error": f"не удалось прочитать /proc/{pid}/cmdline — "
                                          f"остановите вручную: kill {pid}"}
        if base not in cmdline:
            return {"ok": False, "error": f"pid {pid} занят другим процессом ({cmdline.strip()[:80]}) "
                                          f"— не трогаю"}
        os.kill(pid, signal.SIGTERM)
        return {"ok": True, "pid": pid}

    res = await hass.async_add_executor_job(_stop)
    if res.get("ok"):
        _LOGGER.warning("apply_stop: послан SIGTERM прогону %s (pid %s) — остановка мягкая",
                        base, res.get("pid"))
    else:
        _LOGGER.info("apply_stop %s: %s", base, res.get("error"))
    connection.send_result(msg["id"], res)


@websocket_api.require_admin
@websocket_api.websocket_command({
    vol.Required("type"): "arvid_dali_center/registry_trash",
    vol.Optional("purge"): bool,             # False/нет — только ПОКАЗАТЬ, ничего не трогая
})
@callback
def ws_registry_trash(hass, connection, msg):
    """Что НАШЕГО лежит в корзине реестров HA (и, по команде, вымести это).

    Зачем отдельная команда, если чистка уже встроена в «Забыть»/«Стереть»: на объекте
    накоплено СТАРОЕ — записи от прежних экспериментов, до того как чистка появилась. Именно
    они и всплывают потом чужими `entity_id`/областями. Показ и уборка разведены намеренно:
    сначала человек смотрит список, потом решает.

    ⚠ Живых записей НЕ трогаем — только корзину: `deleted_entities` / `deleted_devices`.
    """
    reg = er.async_get(hass)
    dev_reg = dr.async_get(hass)
    ents, devs = [], []
    with contextlib.suppress(Exception):
        for key, entry in getattr(reg, "deleted_entities", {}).items():
            if len(key) == 3 and key[1] == DOMAIN:
                ents.append({"entity_id": getattr(entry, "entity_id", ""),
                             "unique_id": key[2],
                             # None = запись НИКОГДА не истечёт сама (закон 1, HA 2026.8):
                             # штатная уборка смотрит только на осиротевшие без ConfigEntry
                             "forever": getattr(entry, "orphaned_timestamp", None) is None})
    with contextlib.suppress(Exception):
        for entry in getattr(dev_reg, "deleted_devices", {}).values():
            ids = sorted(i[1] for i in (getattr(entry, "identifiers", set()) or set())
                         if i[0] == DOMAIN)
            if ids:
                devs.append({"identifiers": ids})
    result = {"entities": sorted(ents, key=lambda x: x["entity_id"]), "devices": devs,
              "forever": sum(1 for e in ents if e["forever"])}
    if msg.get("purge"):
        result["purged"] = purge_registry_trash(
            hass, {e["unique_id"] for e in ents},
            {i for d in devs for i in d["identifiers"]})
    else:
        _LOGGER.info("registry_trash: в корзине наших сущностей %s (вечных %s), карточек %s "
                     "— показ, ничего не удалено", len(ents), result["forever"], len(devs))
    connection.send_result(msg["id"], result)


@websocket_api.require_admin
@websocket_api.websocket_command({
    vol.Required("type"): "arvid_dali_center/registry_orphans",
    vol.Required("gw_sn"): str,
})
@callback
def ws_registry_orphans(hass, connection, msg):
    """Показать пустые карточки устройств этого шлюза (ничего не удаляя)."""
    hub = _find_hub(hass, msg["gw_sn"])
    if not hub or not getattr(hub, "entry_id", None):
        connection.send_error(msg["id"], "not_found", "шлюз не найден")
        return
    items = _orphan_devices(hass, hub, hub.entry_id)
    _LOGGER.info("registry_orphans %s: пустых карточек %s", msg["gw_sn"], len(items))
    connection.send_result(msg["id"], {"orphans": items})


@websocket_api.require_admin
@websocket_api.websocket_command({
    vol.Required("type"): "arvid_dali_center/registry_cleanup",
    vol.Required("gw_sn"): str,
    vol.Required("device_ids"): list,        # что именно сносим — решает человек
})
@websocket_api.async_response
async def ws_registry_cleanup(hass, connection, msg):
    """Удалить ВЫБРАННЫЕ пустые карточки + почистить наши сторы по их ключам.

    Гейты (иначе чистка была бы мнимой или разрушительной):
      • сносим только то, что СЕЙЧАС пусто и не живое — список перечитывается заново, а не
        берётся на веру из карточки (между показом и нажатием устройство могло ожить);
      • чистим `NameStore`/`ParamStore` по ключу: удаление реестра МЯГКОЕ (закон 1), и без
        этого имя вернулось бы вместе с воскресшей записью.
    """
    hub = _find_hub(hass, msg["gw_sn"])
    if not hub or not getattr(hub, "entry_id", None):
        connection.send_error(msg["id"], "not_found", "шлюз не найден")
        return
    allowed = {o["device_id"]: o for o in _orphan_devices(hass, hub, hub.entry_id)
               if not o["live"]}
    dev_reg = dr.async_get(hass)
    ns, ps = get_name_store(hass), get_store(hass)
    removed, skipped = [], []
    for did in msg["device_ids"]:
        item = allowed.get(did)
        if item is None:
            skipped.append(did)
            _LOGGER.warning("registry_cleanup: карточка %s пропущена — она уже не пустая "
                            "или устройство живое", did)
            continue
        for key in item["identifiers"]:
            if ns:
                await ns.async_set(key, "")       # пустое имя = удаление ключа
            if ps:
                await ps.async_remove(key)
        dev_reg.async_remove_device(did)
        removed.append(item["name"] or ",".join(item["identifiers"]))
    _LOGGER.info("registry_cleanup %s: удалено %s, пропущено %s",
                 msg["gw_sn"], len(removed), len(skipped))
    el = get_eventlog(hass)
    if el and removed:
        el.log(msg["gw_sn"], "system",
               f"чистка реестра: снято пустых карточек {len(removed)}")
    connection.send_result(msg["id"], {"removed": removed, "skipped": skipped})


@websocket_api.websocket_command({
    vol.Required("type"): "arvid_dali_center/devices",
    vol.Required("gw_sn"): str,
})
@callback
def ws_devices(hass, connection, msg):
    hub = _find_hub(hass, msg["gw_sn"])
    if not hub:
        connection.send_error(msg["id"], "not_found", "шлюз не найден")
        return
    connection.send_result(msg["id"], {
        "devices": [_dev(d, hass, hub.gw_sn, hub) for d in hub.devices_snapshot()]})


@websocket_api.websocket_command({
    vol.Required("type"): "arvid_dali_center/energy_live",
    vol.Required("gw_sn"): str,
})
@callback
def ws_energy_live(hass, connection, msg):
    """Энергия ламп шлюза для бейджа на карте — ПОЛНОСТЬЮ РАСЧЁТНАЯ (v1.2.6). Ключ — devSn.

    ⚠ ЧТО ИЗМЕНИЛОСЬ. Раньше `power_w` и `today_wh` брались ОТ ШЛЮЗА (`reportEnergy`), а
    `total_wh` — из накопителя `real_wh` (сумма его приростов). Всё это УДАЛЕНО: шлюз энергию
    НЕ ИЗМЕРЯЕТ — он либо ретранслирует энергобанк драйвера, либо выдумывает число, и снаружи
    случаи неразличимы (разброс ×0.2…×1.35, docs/ENERGY_CALC_MODEL.md §1). Показывать такое —
    врать. Теперь бейдж честен и однороден с отчётом: считаем сами по яркости.

      power_w   — мгновенная мощность СЕЙЧАС: `power_w × кривая(яркость)` по состоянию сущности
                  лампы (0, если выключена; None, если не задана полная мощность лампы);
      total_wh  — расчётный накопитель Вт·ч (`EnergyStore.energy_wh`, интегратор, за всё время);
      on_time_h — наработка «вкл» (`EnergyStore.on_time_s`, тот же интегратор);
      alarm     — активные коды аварий (`alarmCodeReport` — это НЕ энергия, он остаётся).

    `today_wh` и `age_s` УБРАНЫ: «сегодня» требует якоря на полночь (отдельная тема — хранилище
    и суточные срезы, долг E4), а `age_s` был про свежесть отчёта ШЛЮЗА, которого больше нет.
    Стоимость: обход ламп ОДНОГО шлюза (≤64 — лимит адресов DALI), без резолвов реестров.
    """
    from .energy.curves import power_at
    from .energy.store import get_energy_store
    hub = _find_hub(hass, msg["gw_sn"])
    if not hub:
        connection.send_error(msg["id"], "not_found", "шлюз не найден")
        return
    es = get_energy_store(hass)
    with hub._lock:                      # reader-поток мутирует alarms
        alarms = {sn: list(a["codes"].keys())
                  for sn, a in getattr(hub, "alarms", {}).items() if a.get("codes")}
    out = {}
    for dev in hub.devices_snapshot():
        devsn = dev.get("devSn")
        if not devsn or not str(dev.get("devType", "")).startswith("01"):
            continue                     # энергия — только по лампам
        rec = es.get(devsn) if es else {}
        # мгновенная мощность — из СОСТОЯНИЯ СУЩНОСТИ (истина состояния, как и в интеграторе)
        power = None
        eid = _entity_id_of(hass, hub, "light", devsn)
        if eid:
            st = hass.states.get(eid)
            if st is not None and st.state == "on":
                bri = st.attributes.get("brightness")
                frac = (bri / 255.0) if bri is not None else 1.0
                power = round(power_at(rec.get("model"), rec.get("power_w"), frac), 2)
            elif st is not None:
                power = 0.0              # выключена: standby = 0 (по замеру)
        out[devsn] = {
            "power_w": power,
            "total_wh": round(rec.get("energy_wh", 0.0), 3),
            "on_time_h": round((rec.get("on_time_s") or 0.0) / 3600.0, 2),
            "alarm": alarms.get(devsn),
        }
    connection.send_result(msg["id"], {"energy": out})


def _entity_id_of(hass, hub, platform: str, uid: str) -> str | None:
    """entity_id сущности по unique_id (для расчёта мощности бейджа)."""
    reg = er.async_get(hass)
    return reg.async_get_entity_id(platform, DOMAIN, uid)


# ⚠ v1.2.6: КАЛИБРОВОЧНЫЙ ЗАМЕР (`measure_start`/`measure_stop`/`measure_state`/`measure_list`/
# `measure_get`/`measure_clear`) УДАЛЁН вместе с `energy/measure.py`. Он питался ИСКЛЮЧИТЕЛЬНО
# числами `reportEnergy` — то есть мерил энергию ОТ ШЛЮЗА, которой верить нельзя (шлюз не измеряет:
# ретранслирует энергобанк драйвера либо выдумывает). Свою задачу — доказать несостоятельность
# шлюза — Замер выполнил; дальше развиваем РАСЧЁТНЫЙ путь (P = power_w × кривая(яркость)).
# Побочно снято: страница «Замер» поллила `measure_state` каждые 5 с, а тот на каждый вызов делал
# полную копию всех сырых сэмплов сессии в петле HA.


@websocket_api.require_admin
@websocket_api.websocket_command({
    vol.Required("type"): "arvid_dali_center/scan",
    vol.Required("gw_sn"): str,
    vol.Optional("flag", default="exited"): vol.In(["exited", "busDevice"]),
    vol.Optional("assign", default="manual"): vol.In(["manual", "auto"]),
})
@websocket_api.async_response
async def ws_scan(hass, connection, msg):
    """Скан с живым логом: event 'found' на каждое устройство, 'conflict' на каждый
    конфликтный адрес. assign=auto — шлюз сам переназначает дубли (разрешение конфликтов)."""
    hub = _find_hub(hass, msg["gw_sn"])
    if not hub:
        connection.send_error(msg["id"], "not_found", "шлюз не найден")
        return

    @callback
    def progress(d):
        connection.send_message(websocket_api.event_message(
            msg["id"], {"event": "found", "device": _dev(d)}))

    @callback
    def conflict(c):
        connection.send_message(websocket_api.event_message(
            msg["id"], {"event": "conflict", "item": c}))

    found = await hub.async_scan(flag=msg["flag"], progress_cb=progress,
                                 conflict_cb=conflict, assign=msg["assign"])
    connection.send_result(msg["id"], {
        "count": len(found), "devices": [_dev(d) for d in found.values()],
        "conflicts": list(getattr(hub, "conflicts", []))})


@websocket_api.websocket_command({
    vol.Required("type"): "arvid_dali_center/get_param",
    vol.Required("gw_sn"): str,
    vol.Required("devType"): str,
    vol.Required("channel"): int,
    vol.Required("address"): int,
})
@callback
def ws_get_param(hass, connection, msg):
    # ТОЛЬКО из нашего хранилища (getDevParam у ламп медленный и часто пуст —
    # по решению пользователя живое чтение драйвера лампы не делаем).
    store = get_store(hass)
    hub = _find_hub(hass, msg["gw_sn"])
    key = _param_store_key(hub, msg["gw_sn"], msg["devType"], msg["channel"], msg["address"])
    saved = store.get(key) if (store and key) else {}   # без devSn параметры не храним
    connection.send_result(msg["id"], {"paramer": saved})


@websocket_api.require_admin
@websocket_api.websocket_command({
    vol.Required("type"): "arvid_dali_center/set_param",
    vol.Required("gw_sn"): str,
    vol.Required("devType"): str,
    vol.Required("channel"): int,
    vol.Required("address"): int,
    vol.Required("paramer"): dict,
})
@websocket_api.async_response
async def ws_set_param(hass, connection, msg):
    hub = _find_hub(hass, msg["gw_sn"])
    if not hub:
        connection.send_error(msg["id"], "not_found", "шлюз не найден")
        return
    res = await hub.async_request("setDevParam", "setDevParamRes", timeout=8.0, data=[{
        "devType": msg["devType"], "channel": msg["channel"],
        "address": msg["address"], "paramer": msg["paramer"]}])
    # запоминаем заданные значения (для показа в карточке, т.к. read часто пуст)
    store = get_store(hass)
    key = _param_store_key(hub, msg["gw_sn"], msg["devType"], msg["channel"], msg["address"])
    if store and key:                     # None = у устройства нет devSn → не запоминаем
        await store.async_update(key, msg["paramer"])
    connection.send_result(msg["id"], {"ok": bool(res and res.get("ack")), "res": res})


# ── БРОАДКАСТ параметров на весь контроллер (v1.2.44) ───────────────────────────────
# Форма взята из ЗАХВАТА DALI Center 2026-08-05 (функция «отправить на контроллер»), три
# шлюза, у каждого ОДИН пакет:
#   {"cmd":"setDevParam","gwSn":"…","data":[{"devType":"FFFF","paramer":{…}}]}  → ack true
# `devType:"FFFF"` без channel/address = «всем на этом контроллере». Адресный путь (наш
# батч массивом) остаётся — он нужен, когда цели выбраны галочками.
BROADCAST_DEVTYPE = "FFFF"


async def _set_param_broadcast(hass, connection, msg, hub) -> None:
    """`setDevParam` ОДНОЙ командой на весь контроллер.

    Зачем: адресный батч пишет `fadeTime`/`fadeRate` в энергонезависимую память КАЖДОГО
    драйвера по очереди — на 8 лампах мы уже упирались в таймаут (v1.2.23, 12→40 с), а на
    полном шлюзе (до 64) это десятки секунд шины. Броадкаст — один кадр: в захвате `ack`
    на 8-ламповом шлюзе пришёл раньше следующего `devStatus` (< ~1 с).

    ⚠ УРОВЕНЬ ДОВЕРИЯ — 🟡 `ack` (docs/CONFIRMATION_MODEL.md). Сверить нечем: `getDevParam`
    у ламп пуст, читать нечего. И мы НЕ знаем, кого именно задевает броадкаст — только gear
    или ещё датчики/панели: спека об этом молчит, в захвате на шлюзах отвечали лампы
    (гейт G42). Поэтому в журнал пишем именно то, что сделали, без обещаний.
    """
    paramer = msg["paramer"]
    # timeout как у адресного батча: цена ложной ошибки на успешной записи выше (v1.2.23)
    res = await hub.async_request("setDevParam", "setDevParamRes", timeout=40.0,
                                  data=[{"devType": BROADCAST_DEVTYPE, "paramer": paramer}])
    ok = bool(res and res.get("ack"))
    # ParamStore: карточка показывает СОХРАНЁННЫЕ нами значения (read у ламп пуст) → после
    # броадкаста надо обновить их у всех ИЗВЕСТНЫХ ламп шлюза, иначе диалог параметров
    # продолжит показывать старое, т.е. соврёт. Решение пользователя 2026-08-06.
    # ⚠ Лампа, которой мы не знаем (не сканировали), параметр физически получит, а в сторе
    # её нет — это ожидаемо и лучше, чем врущий диалог у известных.
    lamps = [d for d in hub.devices_snapshot() if str(d.get("devType")) in LIGHT_T]
    store = get_store(hass)
    if store and ok:
        for d in lamps:
            k = _param_store_key(hub, msg["gw_sn"], str(d.get("devType")),
                                 d.get("channel"), d.get("address"))
            if k:                          # лампы без devSn пропускаем — хранить нечем
                await store.async_update(k, paramer)
    _LOGGER.info("set_param_bulk[БРОАДКАСТ]: шлюз %s, paramer=%s → ack=%s; "
                 "в ParamStore обновлено ламп: %s", msg["gw_sn"], paramer, ok,
                 len(lamps) if ok else 0)
    el = get_eventlog(hass)
    if el:
        el.log(msg["gw_sn"], "param",
               f"параметры {paramer} — броадкастом всему контроллеру"
               f"{'' if ok else ' ⚠ без ack'}")
    connection.send_result(msg["id"], {"ok": ok, "count": len(lamps), "scope": "gateway",
                                       "res": res, "reason": None if ok else _res_reason(res)})


@websocket_api.require_admin
@websocket_api.websocket_command({
    vol.Required("type"): "arvid_dali_center/set_param_bulk",
    vol.Required("gw_sn"): str,
    vol.Required("paramer"): dict,
    vol.Optional("targets", default=list): list,        # [{devType,channel,address}] — явный список
    vol.Optional("group"): dict,                        # {channel,groupId} — резолвится через readGroup
    vol.Optional("scope", default="targets"): vol.In(("targets", "gateway")),
})
@websocket_api.async_response
async def ws_set_param_bulk(hass, connection, msg):
    """Массовая установка параметров ЛАМП за ОДИН setDevParam (мануал: data — массив целей).
    targets — явный список (мульти-выбор), group — резолв состава через readGroup (параметры группе),
    scope="gateway" — БРОАДКАСТ на весь контроллер (см. `_set_param_broadcast`)."""
    hub = _find_hub(hass, msg["gw_sn"])
    if not hub:
        connection.send_error(msg["id"], "not_found", "шлюз не найден")
        return
    if msg["scope"] == "gateway":
        await _set_param_broadcast(hass, connection, msg, hub)
        return
    targets = list(msg["targets"])
    # цель-группа → достаём состав с контроллера (devType членов берём оттуда же)
    grp = msg.get("group")
    if grp:
        rr = await hub.async_request("readGroup", "readGroupRes",
                                     channel=grp["channel"], groupId=grp["groupId"], timeout=8.0)
        members = (rr or {}).get("data", []) or []
        _LOGGER.info("set_param_bulk: группа ch%s id%s → %s членов",
                     grp["channel"], grp["groupId"], len(members))
        targets += [{"devType": m.get("devType"), "channel": m.get("channel"),
                     "address": m.get("address")} for m in members]
    # дедуп по (devType,channel,address)
    seen, uniq = set(), []
    for t in targets:
        k = (str(t.get("devType")), t.get("channel"), t.get("address"))
        if None in k[1:] or k in seen:
            continue
        seen.add(k)
        uniq.append(t)
    if not uniq:
        connection.send_error(msg["id"], "no_targets", "нет целей для настройки")
        return
    data = [{"devType": str(t["devType"]), "channel": t["channel"],
             "address": t["address"], "paramer": msg["paramer"]} for t in uniq]
    # timeout 40с (было 12): `fadeTime`/`fadeRate` пишутся в ЭНЕРГОНЕЗАВИСИМУЮ память КАЖДОГО
    # драйвера (DTR + команда сохранения + выдержка NVM) — на 8 ламп это десятки кадров DALI.
    # На железе 2026-07-28 операция не уложилась в 12с → возвращали None и рисовали ЛОЖНУЮ ошибку
    # на успешной записи (ответ приходил уже после того, как мы перестали ждать).
    res = await hub.async_request("setDevParam", "setDevParamRes", data=data, timeout=40.0)
    _LOGGER.info("set_param_bulk: %s целей, paramer=%s → %s", len(uniq), msg["paramer"], res)
    # запоминаем заданные значения по каждой цели (read часто пуст)
    store = get_store(hass)
    if store:
        for t in uniq:
            k = _param_store_key(hub, msg["gw_sn"], str(t["devType"]), t["channel"], t["address"])
            if k:
                await store.async_update(k, msg["paramer"])
    # если цель — ГРУППА: запомнить параметры и на уровне группы (чтобы диалог «Параметры
    # группы» открывался с ранее заданными, а не пустым — раньше была «загадка»)
    if grp:
        from .store import get_group_param_store
        gps = get_group_param_store(hass)
        if gps:
            await gps.async_update(msg["gw_sn"], grp["channel"], grp["groupId"], msg["paramer"])
    ok = bool(res and res.get("ack"))
    connection.send_result(msg["id"], {"ok": ok, "count": len(uniq), "res": res,
                                       "reason": None if ok else _res_reason(res)})


@websocket_api.websocket_command({
    vol.Required("type"): "arvid_dali_center/get_group_param",
    vol.Required("gw_sn"): str,
    vol.Required("channel"): int,
    vol.Required("groupId"): int,
})
@callback
def ws_get_group_param(hass, connection, msg):
    """Ранее заданные ГРУППЕ параметры (из GroupParamStore) — для предзаполнения диалога."""
    from .store import get_group_param_store
    gps = get_group_param_store(hass)
    saved = gps.get(msg["gw_sn"], msg["channel"], msg["groupId"]) if gps else {}
    connection.send_result(msg["id"], {"paramer": saved})


@websocket_api.require_admin
@websocket_api.websocket_command({
    vol.Required("type"): "arvid_dali_center/forget_device",
    vol.Required("gw_sn"): str,
    vol.Required("devType"): str,
    vol.Required("channel"): int,
    vol.Required("address"): int,
    vol.Optional("key"): str,          # v1.2.2: точный ключ кеша (обязателен для ОСИРОТЕВШИХ)
})
@websocket_api.async_response
async def ws_forget_device(hass, connection, msg):
    """Ручное «Забыть» зомби: снос сущностей + чистка ВСЕХ device-level сторов по devSn
    (параметры + имя). ЕДИНСТВЕННАЯ точка подрезки хранилищ (принцип: авто-удаления нет).

    ⚠ v1.2.2: у ОСИРОТЕВШЕГО (адрес занял другой devSn) тройка (devType,channel,address)
    указывает на НОВОГО ЖИЛЬЦА — «Забыть» по адресу снесло бы живое устройство. Поэтому карточка
    шлёт `key` из выдачи `devices`; адресный путь остался для обратной совместимости.

    ⚠ v1.2.56: забываем ФИЗУСТРОЙСТВО, а не запись. Движение (0201) и освещённость (0202) —
    две записи шлюза с общим devSn на одном адресе, но одна железка: снял с потолка — нет обеих.
    Приходилось снимать их по очереди (замечание с объекта 2026-08-11). Родство считает
    `sensor_ops.unit_keys` (одна реализация, под тестами); осиротевшая запись в пару к ЖИВОЙ
    не идёт — иначе снос осиротевшего утащил бы работающий датчик."""
    hub = _find_hub(hass, msg["gw_sn"])
    if not hub:
        connection.send_error(msg["id"], "not_found", "шлюз не найден")
        return
    key = msg.get("key") or dev_state_key(msg["devType"], msg["channel"], msg["address"])
    dev = hub.devices.get(key)
    if not dev:
        connection.send_error(msg["id"], "not_found", "устройство не найдено")
        return
    devsn = dev.get("devSn")
    # проверяем ДО сноса (async_forget_device уберёт ключ): если тот же devSn ЖИВ ещё где-то,
    # device-level сторы (имя/параметры/энергия, ключ devSn) ОБЩИЕ — чистить их нельзя, иначе
    # живое устройство потеряет имя. Два случая:
    #   • двойник на другом адресе ТОГО ЖЕ шлюза (перераздача адресов) — `devsn_shared…`;
    #   • устройство живёт на ДРУГОМ шлюзе — `devsn_live_on_other_hub` (НАШЕ знание в HA: персист +
    #     скан, НЕ зомби). ⚠ v1.2.18 (F3): было `devsn_bus_confirmed_on_other_hub` (только «видел на
    #     шине В ЭТОЙ СЕССИИ», флаг `bus_seen`) — а он НЕ переживает рестарт HA, и скан у нас ручной.
    #     Рестарт (напр. от отключения света) сбрасывал защиту → «Забыть» зомби-координаты стирало
    #     имя/энергию устройства, ЖИВОГО на соседнем шлюзе. Персист-знание живо сразу после старта.
    #     Возврат к персисту БЕЗОПАСЕН: источник «размазывания» (exited-кеш) убран в v1.2.14 — теперь
    #     `has_devsn(live_only)` видит только реально известные нам не-зомби устройства (не память шлюза).
    shared = hub.devsn_shared_with_other_key(key, devsn)
    elsewhere = hub.devsn_live_on_other_hub(devsn)
    # 🔴 v1.2.66 — ТРЕТИЙ случай, из «перекрёста devSn» (docs/DEVSN_CROSSWIRE.md): тот же
    # серийник носит ЖИВАЯ запись ДРУГОГО типа (шлюз поменял их местами на одном адресе).
    # Сущности у них разные, а карточка устройства и device-level сторы — ОБЩИЕ по `devSn`,
    # поэтому чистить их нельзя: снесём имя и карточку работающего устройства.
    cross_live = hub.devsn_live_under_other_type(key, devsn)
    wipe_ok = is_valid_devsn(devsn) and not shared and not elsewhere and not cross_live
    if cross_live:
        _LOGGER.warning("«Забыть» %s (%s): серийник занят ЖИВОЙ записью %s другого типа — "
                        "сущности сношу, имя/параметры/карточку НЕ трогаю (перекрёст devSn)",
                        devsn, key, cross_live)
    # ⚠ ПОРЯДОК КРИТИЧЕН (v1.2.14). Удаление сущности в HA — МЯГКОЕ: запись уезжает в корзину
    # (`entity_registry.deleted_entities` / `device_registry.deleted_devices`) ВМЕСТЕ с личным
    # именем и entity_id, и при возврате устройства HA достаёт её обратно по devSn/unique_id.
    # Поэтому имя СБРАСЫВАЕМ К ШАБЛОНУ ДО сноса: тогда в корзину уезжает шаблон, и вернувшееся
    # устройство приходит чистым (ровно то поведение, которого ждёт человек: «забыл → вернул →
    # появилось как новое»). Раньше имя чистилось ПОСЛЕ сноса — и «Забыть» не держал.
    # ВСЕ записи ЭТОГО физустройства (датчик = движение + освещённость), сам ключ первым
    from .sensor_ops import unit_keys
    keys = unit_keys(hub.devices, key)
    if len(keys) > 1:
        _LOGGER.info("«Забыть» %s: устройство состоит из %s записей (%s) — снимаем целиком",
                     devsn, len(keys), ", ".join(keys))
    if wipe_ok:
        ns = get_name_store(hass)
        if ns:                                    # 1) наше имя (name_key(...devsn) == devsn)
            await ns.async_set(devsn, "")
        for k in keys:                            # 2) сущности ОБЕИХ ролей → шаблонные id/подпись
            d = hub.devices.get(k)
            if not d:
                continue
            for role, platform, uid in hub._roles_for_dev(d):
                if not er.async_get(hass).async_get_entity_id(platform, DOMAIN, uid):
                    continue
                with contextlib.suppress(Exception):
                    await hub._force_entity_id(role, platform, uid, d, created=True, rename=True)
        gd = dr.async_get(hass).async_get_device(identifiers={(DOMAIN, devsn)})
        if gd and gd.name_by_user is not None:    # 3) имя устройства → дефолт
            with contextlib.suppress(Exception):
                dr.async_get(hass).async_update_device(gd.id, name_by_user=None)
    removed = []                                  # 4) и только теперь снос сущностей + кеша
    for k in keys:
        removed += await hub.async_forget_device(k)
    # чистка остальных device-level сторов по devSn — только если devSn НИГДЕ больше не жив
    if wipe_ok:
        # ЕДИНАЯ чистка (S5, v1.2.51): раньше здесь снимались только имя и параметры, из-за
        # чего «Забыть» оставлял предпочтение активности датчика и его конфигурацию функций —
        # устройство возвращалось на шину со старым наследством (DEBT §S, S3).
        await purge_identity_everywhere(hass, devsn)
        # v1.2.52: снимаем и КАРТОЧКУ УСТРОЙСТВА из реестра HA. Раньше «Забыть» убирал
        # сущности, а пустая карточка оставалась — и её приходилось добивать руками через
        # «Реестр» или настройки (замечание с объекта 2026-08-07). Человек ждёт, что
        # устройство исчезло целиком.
        # ⚠ Удаление МЯГКОЕ (закон 1) — запись уедет в корзину. Это безопасно ИМЕННО ЗДЕСЬ:
        # имя уже сброшено к шаблону выше, поэтому вернуться из корзины может только шаблон.
        # КОРЗИНА (v1.2.60): «забыл → вернул → пришло как новое» иначе неправда — HA вернёт
        # из `deleted_entities` и entity_id, и область, и ярлыки по тому же unique_id.
        # ⚠ v1.2.63 — ПОРЯДОК: чистим В САМОМ КОНЦЕ, после сноса карточки устройства. Иначе
        # карточка уезжает в `deleted_devices` уже ПОСЛЕ уборки и остаётся там (замечание
        # пользователя 2026-08-12: «сначала чистим, а потом туда же добавляем стёртое»).
        uids = {uid for k in keys for _r, _p, uid in hub._roles_for_dev(hub.devices.get(k) or dev)}
        dreg = dr.async_get(hass)
        gdev = dreg.async_get_device(identifiers={(DOMAIN, devsn)})
        if gdev:
            others = set(gdev.config_entries) - {hub.entry_id}
            if others:
                # устройство числится и за другой записью — карточку не сносим, только
                # отвязываем от нашей (иначе снесли бы живое у соседнего шлюза)
                with contextlib.suppress(Exception):
                    dreg.async_update_device(gdev.id, remove_config_entry_id=hub.entry_id)
                _LOGGER.info("«Забыть» %s: карточка оставлена — она есть и у другой записи",
                             devsn)
            else:
                with contextlib.suppress(Exception):
                    dreg.async_remove_device(gdev.id)
                _LOGGER.info("«Забыть» %s: карточка устройства снята из реестра HA", devsn)
        # и только ТЕПЕРЬ выметаем корзину: к этому моменту в неё уехало всё — и сущности,
        # и карточка устройства
        purge_registry_trash(hass, uids, {devsn})
    connection.send_result(msg["id"], {"ok": True, "removed": removed,
                                       "shared": shared or elsewhere or bool(cross_live),
                                       "cross_live": cross_live})


@websocket_api.require_admin
@websocket_api.websocket_command({
    vol.Required("type"): "arvid_dali_center/wipe_gateway_data",
    vol.Required("gw_sn"): str,
})
@websocket_api.async_response
async def ws_wipe_gateway_data(hass, connection, msg):
    """Кнопка «Стереть данные устройств шлюза» (v1.2.12) — СБРОС к шаблону, НЕ удаление.

    ⚠ v1.2.64: сбрасываются ИМЯ, `entity_id`, подпись, **область и ярлыки** — всё, что раздал
    человек. Прежде область и ярлыки чистку переживали и потом тихо расходились с проектом
    (долг T4). Удаления по-прежнему НЕТ: записи остаются живыми, поэтому в корзину HA ничего
    не уезжает — чистка корзины здесь выметает лишь накопленное ПРЕЖНИМИ удалениями.

    ⚠ КОРЕНЬ «вгрызания» (P0), найден по дампу реестров: **удаление из реестра HA — МЯГКОЕ**.
    `dev_reg.async_remove_device()` / `ent_reg.async_remove()` не стирают запись, а кладут её в
    КОРЗИНУ (`device_registry.data.deleted_devices` / `entity_registry.data.deleted_entities`)
    ВМЕСТЕ с `name_by_user` / `name` / `entity_id`. Когда устройство появляется снова с теми же
    `identifiers`/`unique_id` (а они у нас по devSn — стабильны), HA ВОССТАНАВЛИВАЕТ запись из
    корзины вместе с личным именем. Корзина переживает и удаление ConfigEntry, и рестарт HA
    (штатная чистка — только по `orphaned_timestamp`, ~30 дней). Поэтому все прежние версии кнопки
    (v1.2.9 наши сторы → v1.2.10 + снос реестров → v1.2.11 реестро-ориентированный снос) были
    ОБРЕЧЕНЫ: они «удаляли» ровно в ту корзину, из которой HA потом и воскрешал.

    Решение — НЕ УДАЛЯТЬ, а СБРОСИТЬ к дефолту (корзина не задействована → воскрешать нечего):
      • `NameStore` чистим ПЕРВЫМ → `_desired_entity_id` начинает выдавать ШАБЛОННОЕ имя;
      • `_force_entity_id(created=True, rename=True)` → `entity_id` и подпись сущностей → шаблон
        (`created/rename` снимают гейт «ручной id не трогаем» — здесь это явная воля человека);
      • `dev_reg.async_update_device(name_by_user=None)` → имя устройства → дефолт.
    Всё это — публичные API, уже используемые в `ws_rename`/Fix X. Запись устройства остаётся (оно
    физически на шине), но выглядит НОВЫМ, с шаблонным именем — цель достигнута.

    Целевые devSn = (устройства этого ConfigEntry из `device_registry`) ∪ (кеш шлюза). Единственное
    исключение — devSn, живой на ДРУГОМ шлюзе по НАШЕМУ знанию в HA (`devsn_live_on_other_hub`:
    персист + скан, не зомби) = устройство живёт там → его общие по devSn данные нужны там.
    ⚠ v1.2.18 (F3): было `devsn_bus_confirmed_on_other_hub` (флаг `bus_seen`, только «видел в этой
    сессии») — не переживал рестарт HA, а скан ручной → после рестарта защита отваливалась и «Стереть»
    стирало имя/энергию устройства с соседнего шлюза. Персист-знание живо сразу после старта; источник
    «размазывания» (exited-кеш) убран в v1.2.14, поэтому персист больше не выдаёт память шлюза за живое."""
    from .energy.store import get_energy_store
    from .store import (
        get_device_store, get_group_param_store, get_group_store, get_name_store,
        get_panel_act_store, get_rotary_store, get_store,
    )
    hub = _find_hub(hass, msg["gw_sn"])
    if not hub:
        connection.send_error(msg["id"], "not_found", "шлюз не найден")
        return
    gw_sn = msg["gw_sn"]
    entry_id = hub.entry_id
    ps, ns, es, rs = (get_store(hass), get_name_store(hass),
                      get_energy_store(hass), get_rotary_store(hass))
    dev_reg = dr.async_get(hass)

    # целевые devSn: РЕЕСТР этого ConfigEntry (главный — там name_by_user) ∪ КЕШ шлюза.
    targets: set[str] = set()
    if entry_id:
        for gdev in dr.async_entries_for_config_entry(dev_reg, entry_id):
            for dom, ident in gdev.identifiers:
                # только НАШИ устройства: пропускаем сам шлюз (identifier == gwSn) и группы (…_group_…)
                if (dom == DOMAIN and ident != gw_sn and "_group_" not in ident
                        and is_valid_devsn(ident)):
                    targets.add(ident)
                    break
    # 🔴 v1.2.52: devSn -> СПИСОК записей. Движение (0201) и освещённость (0202) — ОДНО
    # физическое устройство с ОБЩИМ серийником, и прежний `dict[str, dict]` затирал первую
    # запись второй. `_roles_for_dev` получал только уцелевший тип → сбрасывалась одна
    # сущность из пары: у датчика освещённость становилась шаблонной, а движение
    # сохраняло прежнее имя (симптом с объекта 2026-08-07).
    cache_devs: dict[str, list[dict]] = {}        # devSn -> снимки кеша (роли/шаблон для сброса)
    for dev in hub.devices_snapshot():
        sn = dev.get("devSn")
        if is_valid_devsn(sn):
            cache_devs.setdefault(sn, []).append(dev)
            targets.add(sn)

    ent_reg = er.async_get(hass)
    wiped, kept = 0, 0
    for devsn in targets:
        # ЕДИНСТВЕННОЕ исключение — устройство живёт на другом шлюзе по НАШЕМУ знанию в HA
        # (персист + скан, не зомби). v1.2.18 (F3): было `devsn_bus_confirmed_on_other_hub`
        # (сессионный `bus_seen`) — рестарт сбрасывал защиту, «Стереть» било по живому соседу.
        if hub.devsn_live_on_other_hub(devsn):
            kept += 1
            continue
        # 1) НАШИ сторы — ЕДИНОЙ чисткой по реестру (S5, v1.2.51). Раньше здесь был ручной
        #    список сторов, и он расходился со списками «Забыть» и удаления шлюза: так
        #    SensorObjStore не чистился нигде, а SensorPrefStore — только тут (DEBT §S).
        #    ⚠ Порядок сохранён: чистка идёт ДО сброса entity_id ниже, потому что пока имя
        #    лежит в NameStore, `_desired_entity_id` считает устройство именованным.
        await purge_identity_everywhere(hass, devsn)
        # 2) СУЩНОСТИ: entity_id и подпись → ШАБЛОН (не сносим — снос уходит в корзину HA).
        #    `created=True, rename=True` снимают гейт «ручной entity_id не трогаем» (Fix R): здесь
        #    это явная воля человека. Проверка на существование записи — чтобы не ждать впустую
        #    3с поллинга в `_force_entity_id` у ролей без сущности.
        devs_of_sn = cache_devs.get(devsn) or []
        for dev in devs_of_sn:                    # обе роли пары 0201/0202, а не одна
            for role, platform, uid in hub._roles_for_dev(dev):
                if not ent_reg.async_get_entity_id(platform, DOMAIN, uid):
                    continue
                with contextlib.suppress(Exception):
                    await hub._force_entity_id(role, platform, uid, dev,
                                               created=True, rename=True)
        # 3) УСТРОЙСТВО: личное имя → дефолт (`name_by_user=None`, как Fix X).
        gd = dev_reg.async_get_device(identifiers={(DOMAIN, devsn)})
        if gd:
            if not devs_of_sn:
                # устройства нет в кеше → ролей/шаблона не знаем; хотя бы снять личные подписи
                for ent in er.async_entries_for_device(ent_reg, gd.id,
                                                       include_disabled_entities=True):
                    if ent.name is not None:
                        with contextlib.suppress(Exception):
                            ent_reg.async_update_entity(ent.entity_id, name=None)
            if gd.name_by_user is not None:
                with contextlib.suppress(Exception):
                    dev_reg.async_update_device(gd.id, name_by_user=None)
            # 4) ОБЛАСТЬ и ЯРЛЫКИ → снимаются (v1.2.64, решение пользователя: «логика работы
            #    должна быть очевидна»). «Стереть данные» означает «устройство как новое», а
            #    область и ярлыки — такая же розданная человеком настройка, как имя. Раньше
            #    они переживали чистку и потом расходились с проектом ТИХО: на офисном прогоне
            #    у ламп всплыли `kab_301`/`kab_302` от давних опытов (долг T4).
            #    Сущности: `area_id=None` = «наследовать от устройства», поэтому явную область
            #    сущности тоже снимаем — иначе она перебьёт пустую область устройства.
            if gd.area_id is not None or gd.labels:
                with contextlib.suppress(Exception):
                    dev_reg.async_update_device(gd.id, area_id=None, labels=set())
            for ent in er.async_entries_for_device(ent_reg, gd.id,
                                                   include_disabled_entities=True):
                if ent.area_id is not None or ent.labels:
                    with contextlib.suppress(Exception):
                        ent_reg.async_update_entity(ent.entity_id, area_id=None, labels=set())
        wiped += 1
    # шлюзовые сторы этого шлюза — полный ноль без удаления записи из config flow
    # ШЛЮЗОВЫЕ сторы — тоже по реестру (S5): сюда попали и те, о которых прежний ручной
    # список не знал — кросс-группы (шлюз выбывает из участников) и легаси адресные ключи
    # имён/параметров. Предпочтения активности датчиков чистит `SensorPrefStore.purge_gateway`.
    gw_report = await purge_gateway_everywhere(hass, gw_sn)
    # КОРЗИНА (v1.2.60): «Стереть» обязана снимать и то, что HA придержал у себя, — иначе
    # после чистки объект приходит со старыми областями и ярлыками (долг T4), а сущности
    # воскресают с прежними entity_id. Целевые devSn у нас уже собраны выше.
    trash_uids: set[str] = set()
    for devsn in targets:
        for devs in (cache_devs.get(devsn) or []):
            trash_uids |= {uid for _r, _p, uid in hub._roles_for_dev(devs)}
        trash_uids.add(devsn)                     # у ламп unique_id = сам devSn
    trash = purge_registry_trash(hass, trash_uids, set(targets))
    # ПАМЯТЬ хаба про «выключен вручную» — рядом с персистом, иначе до рестарта HA стёртый
    # шлюз продолжал бы считать датчики выключенными (v1.2.49).
    for key, dev in list(hub.devices.items()):
        if str(dev.get("devType")).startswith("02"):
            hub.set_sensor_active(hub.sensor_pref_key(dev, key), True)
    connection.send_result(msg["id"], {"ok": True, "wiped": wiped, "kept": kept,
                                       "purged": gw_report, "trash": trash})


@websocket_api.require_admin
@websocket_api.websocket_command({
    vol.Required("type"): "arvid_dali_center/set_rotary_binding",
    vol.Required("gw_sn"): str,
    vol.Required("devType"): str,            # 0300
    vol.Required("channel"): int,
    vol.Required("address"): int,
    vol.Required("target"): dict,            # {devType, channel, address} (лампа 01xx / группа 0401)
    vol.Optional("step", default=20): int,   # шина 0..1000 на 1 «щелчок» (дефолт ~2%)
    vol.Optional("throttle", default=0.8): vol.Coerce(float),   # сек между отправками (пол 0.7 = fade)
})
@websocket_api.async_response
async def ws_set_rotary_binding(hass, connection, msg):
    """Привязка поворота → яркость цели (логика в HA, dpid 4 → дельта). Ключ — devSn панели.
    Доп. снимаем БИТУЮ нативную привязку поворота (она шлёт «яркость 0» и мешает)."""
    hub = _find_hub(hass, msg["gw_sn"])
    if not hub:
        connection.send_error(msg["id"], "not_found", "шлюз не найден")
        return
    key = dev_state_key(msg["devType"], msg["channel"], msg["address"])
    dev = hub.devices.get(key)
    devsn = dev.get("devSn") if dev else None
    if not is_valid_devsn(devsn):
        connection.send_error(msg["id"], "no_devsn", "панель без валидного devSn")
        return
    rs = get_rotary_store(hass)
    throttle = max(0.7, float(msg["throttle"]))   # пол = время fade-разжигания (~0.7с)
    await rs.async_set(devsn, {"target": msg["target"], "step": int(msg["step"]), "throttle": throttle})
    hub._rotary_rt.pop(devsn, None)          # сбросить энкодер-состояние под новую привязку
    # снять нативную привязку поворота (dpid 4), если есть — иначе шлюз гасит цель «в 0»
    cleared = 0
    rr = await hub.async_request("readPanel", "readPanelRes", devType=msg["devType"],
                                 channel=msg["channel"], address=msg["address"],
                                 keyNo=1, dpid=4, timeout=6.0)
    cur = ((rr or {}).get("data", {}) or {}).get("outObj", []) or []
    if cur:
        await hub.async_request(
            "delPanelObj", "delPanelObjRes", devType=msg["devType"],
            channel=msg["channel"], address=msg["address"],
            data={"keyNo": 1, "dpid": 4,
                  "outObj": [{"gwSnObj": o.get("gwSnObj"), "devType": str(o.get("devType")),
                              "channel": o.get("channel"), "address": o.get("address")}
                             for o in cur]}, timeout=8.0)
        cleared = len(cur)
    connection.send_result(msg["id"], {"ok": True, "nativeCleared": cleared})


@websocket_api.websocket_command({
    vol.Required("type"): "arvid_dali_center/get_rotary_binding",
    vol.Required("gw_sn"): str,
    vol.Required("devType"): str,
    vol.Required("channel"): int,
    vol.Required("address"): int,
})
@callback
def ws_get_rotary_binding(hass, connection, msg):
    hub = _find_hub(hass, msg["gw_sn"])
    dev = hub.devices.get(dev_state_key(msg["devType"], msg["channel"], msg["address"])) if hub else None
    devsn = dev.get("devSn") if dev else None
    rs = get_rotary_store(hass)
    connection.send_result(msg["id"], {"binding": rs.get(devsn) if (rs and devsn) else None})


@websocket_api.require_admin
@websocket_api.websocket_command({
    vol.Required("type"): "arvid_dali_center/clear_rotary_binding",
    vol.Required("gw_sn"): str,
    vol.Required("devType"): str,
    vol.Required("channel"): int,
    vol.Required("address"): int,
})
@websocket_api.async_response
async def ws_clear_rotary_binding(hass, connection, msg):
    hub = _find_hub(hass, msg["gw_sn"])
    dev = hub.devices.get(dev_state_key(msg["devType"], msg["channel"], msg["address"])) if hub else None
    devsn = dev.get("devSn") if dev else None
    rs = get_rotary_store(hass)
    if rs and devsn:
        await rs.async_remove(devsn)
        hub._rotary_rt.pop(devsn, None)
    connection.send_result(msg["id"], {"ok": True})


@websocket_api.websocket_command({
    vol.Required("type"): "arvid_dali_center/get_sensor_param",
    vol.Required("gw_sn"): str,
    vol.Required("devType"): str,
    vol.Required("channel"): int,
    vol.Required("address"): int,
})
@websocket_api.async_response
async def ws_get_sensor_param(hass, connection, msg):
    """Параметры датчика (getSensorArgv) — на железе читаются нормально.

    БЕЗ admin: это ЧТЕНИЕ (для просмотра параметров, в т.ч. с телефона под не-админом).
    require_admin (v0.55) ломал просмотр у не-админов; запрос точечный, нагрузка минимальна.
    Мутация параметров (`set_sensor_param`) — по-прежнему admin."""
    hub = _find_hub(hass, msg["gw_sn"])
    if not hub:
        connection.send_error(msg["id"], "not_found", "шлюз не найден")
        return
    res = await hub.async_request(
        "getSensorArgv", "getSensorArgvRes", timeout=6.0,
        devType=msg["devType"], channel=msg["channel"], address=msg["address"])
    connection.send_result(msg["id"], {"data": (res or {}).get("data", {}) or {}})


@websocket_api.require_admin
@websocket_api.websocket_command({
    vol.Required("type"): "arvid_dali_center/set_sensor_param",
    vol.Required("gw_sn"): str,
    vol.Required("devType"): str,
    vol.Required("channel"): int,
    vol.Required("address"): int,
    vol.Required("data"): dict,
})
@websocket_api.async_response
async def ws_set_sensor_param(hass, connection, msg):
    hub = _find_hub(hass, msg["gw_sn"])
    if not hub:
        connection.send_error(msg["id"], "not_found", "шлюз не найден")
        return
    res = await hub.async_request(
        "setSensorArgv", "setSensorArgvRes", data=msg["data"],
        devType=msg["devType"], channel=msg["channel"], address=msg["address"])
    connection.send_result(msg["id"], {"ok": bool(res and res.get("ack")), "res": res})


def _rename_roles(gw_sn: str, d: dict, name: str) -> list[tuple]:
    """Список (domain, role, key, object_id, friendly) сущностей устройства для ренейма.

    role+key — для резолва unique_id из карты хаба `(role, devType:ch:addr)→uid`
    (саморегистрация; устойчиво к дрейфу devSn). object_id → entity_id (slug делает
    вызывающий), friendly → подпись.

    Датчики (0201/0202) — ОДНО физическое устройство (общий адрес/ключ имени): ренейм
    ЛЮБОГО из них переименовывает ОБЕ пары (движение+люкс + их switch активации) с
    префиксом по типу (ms_/il_), без русских слов. Лампы/панели — как есть."""
    t = str(d.get("devType"))
    ch, addr = d.get("channel"), d.get("address")
    if t in LIGHT_T:
        return [("light", "light", dev_state_key(t, ch, addr), name, name)]
    if t in ("0201", "0202"):
        body = sensor_body(name)
        ms, il = sensor_name("0201", body), sensor_name("0202", body)
        return [
            ("sensor", "motion",      dev_state_key("0201", ch, addr), ms, ms),
            ("switch", "active_0201", dev_state_key("0201", ch, addr), ms + "_act", ms + "_act"),
            ("sensor", "lux",         dev_state_key("0202", ch, addr), il, il),
            ("switch", "active_0202", dev_state_key("0202", ch, addr), il + "_act", il + "_act"),
        ]
    if t.startswith("03"):
        return [("event", "event", dev_state_key(t, ch, addr), name, name)]
    return []


def _legacy_uid(gw_sn: str, d: dict, role: str) -> str:
    """Реконструкция unique_id по devSn (fallback для оффлайн-устройств без живой сущности)."""
    t = str(d.get("devType"))
    ch, addr, sn = d.get("channel"), d.get("address"), d.get("devSn")
    if role == "light":
        return sn or f"{gw_sn}:{t}:{ch}:{addr}"
    base = sn or f"{gw_sn}:{ch}:{addr}"
    return f"{base}_{role}"


@websocket_api.require_admin
@websocket_api.websocket_command({
    vol.Required("type"): "arvid_dali_center/rename",
    vol.Required("gw_sn"): str,
    vol.Required("devType"): str,
    vol.Required("channel"): int,
    vol.Required("address"): int,
    vol.Optional("devSn", default=""): str,
    vol.Required("name"): str,
})
@websocket_api.async_response
async def ws_rename(hass, connection, msg):
    """Переименовать устройство: имя в нашей базе + entity_id/подпись сущностей HA.

    entity_id держим АКТУАЛЬНЫМ (важный элемент): не молчаливый фолбэк «только подпись»,
    а форс желаемого id с освобождением НАШЕГО же мёртвого сироты (как для групп). Имена
    ГЛОБАЛЬНО уникальны: если желаемый entity_id занят ЖИВОЙ (или чужой) сущностью — это
    дубль, ренейм отклоняем целиком (имя в NameStore не пишем), чтобы HA не плодил `_2`."""
    name = msg["name"].strip()
    d = {"devType": msg["devType"], "channel": msg["channel"],
         "address": msg["address"], "devSn": msg.get("devSn") or None}
    reg = er.async_get(hass)
    dev_reg = dr.async_get(hass)
    hub = _find_hub(hass, msg["gw_sn"])
    # 1) план ренейма по ролям: (domain, uid, текущий eid, желаемый new_eid, подпись)
    plan = []
    for domain, role, key, object_id, friendly in _rename_roles(msg["gw_sn"], d, name):
        # uid из карты хаба (устойчив к дрейфу devSn у датчиков); fallback — реконструкция
        uid = (hub.entity_uid(role, key) if hub else None) or _legacy_uid(msg["gw_sn"], d, role)
        eid = reg.async_get_entity_id(domain, DOMAIN, uid)
        oid = slugify(object_id) or f"dev_{msg['address']}"
        plan.append((domain, uid, eid, f"{domain}.{oid}", friendly))
    # 2) валидация ГЛОБАЛЬНОЙ уникальности: желаемый id занят кем-то ЖИВЫМ/чужим → дубль.
    #    Наш же МЁРТВЫЙ сирота (наша платформа, нет живого состояния) — не помеха (освободим).
    for domain, uid, eid, new_eid, _friendly in plan:
        holder = reg.async_get(new_eid)
        if holder is None or holder.unique_id == uid:
            continue                              # свободно / уже наш этот же id
        our_dead = (holder.platform == DOMAIN and hass.states.get(new_eid) is None)
        if our_dead:
            continue                              # наш сирота — освободим ниже
        _LOGGER.warning("rename отклонён: имя занято %s (uid=%s)", new_eid, holder.unique_id)
        connection.send_result(msg["id"],
                               {"ok": False, "error": "duplicate", "conflict": new_eid})
        return
    # 3) дублей нет → фиксируем имя в НАШЕМ хранилище (карточка показывает его, переживает рестарт)
    #    ⚠ v1.2.51: ключ — ТОЛЬКО devSn. У устройства без серийника имя живёт лишь в реестре
    #    HA (сущность переименована выше) и наш стор не засоряет: адресный ключ пережил бы
    #    само устройство и всплыл на его преемнике по адресу (так вернулось `l_2_2_2`).
    ns = get_name_store(hass)
    nkey = name_key(msg["gw_sn"], msg["devType"], msg["channel"], msg["address"], d["devSn"])
    if ns and nkey:
        await ns.async_set(nkey, name)
    elif ns:
        _LOGGER.warning("rename %s addr%s: у устройства нет devSn — имя сохранено только в "
                        "реестре HA (в наш стор не пишем, чтобы не осталось за адресом)",
                        msg["devType"], msg["address"])
    # 4) сущности HA: форсируем entity_id + подпись (освобождаем свой мёртвый сирота)
    renamed = []
    for domain, uid, eid, new_eid, friendly in plan:
        if not eid:
            continue
        if new_eid != eid:
            holder = reg.async_get(new_eid)       # наш мёртвый сирота на желаемом id → снять
            if (holder and holder.unique_id != uid
                    and holder.platform == DOMAIN and hass.states.get(new_eid) is None):
                reg.async_remove(new_eid)
        try:
            reg.async_update_entity(eid, new_entity_id=new_eid, name=friendly)
            renamed.append(new_eid)
        except Exception as err:  # noqa: BLE001 — на всякий случай (гонка занятия id)
            _LOGGER.warning("rename %s → %s: %s", eid, new_eid, err)
            reg.async_update_entity(eid, name=friendly)
    # имя HA-device (для дашбордов): идентификатор устройства
    t = str(msg["devType"])
    dev_ident = (d["devSn"] or (f"{msg['gw_sn']}:{t}:{msg['channel']}:{msg['address']}"
                                if t in LIGHT_T else f"{msg['gw_sn']}:{msg['channel']}:{msg['address']}"))
    dev = dev_reg.async_get_device(identifiers={(DOMAIN, dev_ident)})
    if dev:
        dev_reg.async_update_device(dev.id, name_by_user=name)
    # S1 (v1.2.3): группы подписаны на лампы по entity_id — после ренейма id ПРОТУХ,
    # подписку надо пересобрать, иначе группа молча перестанет видеть свою лампу
    if renamed and hub:
        hub.resubscribe_groups()
    connection.send_result(msg["id"], {"ok": True, "renamed": renamed})


@websocket_api.require_admin
@websocket_api.websocket_command({
    vol.Required("type"): "arvid_dali_center/rename_group",
    vol.Required("gw_sn"): str,
    vol.Required("channel"): int,
    vol.Required("groupId"): int,
    vol.Required("name"): str,
})
@websocket_api.async_response
async def ws_rename_group(hass, connection, msg):
    """Переименовать DALI-группу НА КОНТРОЛЛЕРЕ (setGroupName — источник правды) +
    обновить entity_id/подпись и offline-кеш (GroupStore). NameStore для групп НЕ
    используем (он залипал за groupId и затенял имя контроллера)."""
    hub = _find_hub(hass, msg["gw_sn"])
    if not hub:
        connection.send_error(msg["id"], "not_found", "шлюз не найден")
        return
    name = msg["name"].strip()
    # 1) переименовать на контроллере (он хранит имя группы; getGroup его и вернёт)
    res = await hub.async_request("setGroupName", "setGroupNameRes",
                                  channel=msg["channel"], groupId=msg["groupId"], name=name)
    # 2) кеш хаба (его отдаёт ws_groups) + offline-персист (GroupStore)
    gs = get_group_store(hass)
    for g in hub.groups:
        if g.get("channel") == msg["channel"] and g.get("groupId") == msg["groupId"]:
            g["name"] = name
            if gs:
                await gs.async_upsert(hub.gw_sn, g)
            break
    # 3) почистить ЛЕГАСИ NameStore-запись группы (источник залипания старых имён)
    ns = get_name_store(hass)
    if ns:
        await ns.async_set(group_name_key(msg["gw_sn"], msg["channel"], msg["groupId"]), "")
    # 4) сущность HA: entity_id (надёжно) + подпись + имя HA-device
    reg = er.async_get(hass)
    dev_reg = dr.async_get(hass)
    uid = f"{msg['gw_sn']}_group_{msg['channel']}_{msg['groupId']}"
    eid = reg.async_get_entity_id("light", DOMAIN, uid)
    renamed = None
    if eid and name:
        desired = f"light.{slugify(name) or 'group_' + str(msg['groupId'])}"
        # освободить желаемый entity_id, если его занял осиротевший group-orphan
        holder = reg.async_get(desired)
        if (holder and holder.unique_id != uid
                and str(holder.unique_id).startswith(f"{msg['gw_sn']}_group_")):
            reg.async_remove(desired)
        try:
            reg.async_update_entity(eid, new_entity_id=desired, name=name)
            renamed = desired
        except Exception as err:  # noqa: BLE001 — занятый entity_id и т.п.
            _LOGGER.warning("rename group %s → %s: %s", eid, desired, err)
            reg.async_update_entity(eid, name=name)
    elif eid:
        reg.async_update_entity(eid, name=None)
    dev = dev_reg.async_get_device(identifiers={(DOMAIN, uid)})
    if dev:
        dev_reg.async_update_device(dev.id, name_by_user=name or None)
    connection.send_result(msg["id"], {"ok": bool(res and res.get("ack")), "renamed": renamed})


@websocket_api.require_admin
@websocket_api.websocket_command({
    vol.Required("type"): "arvid_dali_center/set_address",
    vol.Required("gw_sn"): str,
    vol.Required("devType"): str,
    vol.Required("channel"): int,
    vol.Required("address"): int,
    vol.Required("new"): int,
})
@websocket_api.async_response
async def ws_set_address(hass, connection, msg):
    hub = _find_hub(hass, msg["gw_sn"])
    if not hub:
        connection.send_error(msg["id"], "not_found", "шлюз не найден")
        return
    res = await hub.async_request("setDevParam", "setDevParamRes", timeout=8.0, data=[{
        "devType": msg["devType"], "channel": msg["channel"],
        "address": msg["address"], "paramer": {"address": msg["new"]}}])
    connection.send_result(msg["id"], {"ok": bool(res and res.get("ack")), "res": res})


@websocket_api.require_admin
@websocket_api.websocket_command({
    vol.Required("type"): "arvid_dali_center/restart_gateway",
    vol.Required("gw_sn"): str,
})
@websocket_api.async_response
async def ws_restart_gateway(hass, connection, msg):
    hub = _find_hub(hass, msg["gw_sn"])
    if not hub:
        connection.send_error(msg["id"], "not_found", "шлюз не найден")
        return
    res = await hub.async_request("restartGateway", "restartGatewayRes", timeout=8.0)
    connection.send_result(msg["id"], {"ok": bool(res and res.get("ack")), "res": res})


@websocket_api.require_admin
@websocket_api.websocket_command({
    vol.Required("type"): "arvid_dali_center/reset_addresses",
    vol.Required("gw_sn"): str,
})
@websocket_api.async_response
async def ws_reset_addresses(hass, connection, msg):
    """Общий сброс адресов: resetGateway {deviceReset:true} — шлюз ПЕРЕНАЗНАЧАЕТ
    короткие адреса всем устройствам шины (механизм «сброс адресов» из DALI Center PC;
    в прошивке заменил удалённый searchDev fullAssign). Сильно разрушающее; подтверждение
    (двойное) — на стороне карточки. ⚠ Семантику resetGateway проверить на железе."""
    hub = _find_hub(hass, msg["gw_sn"])
    if not hub:
        connection.send_error(msg["id"], "not_found", "шлюз не найден")
        return
    el = get_eventlog(hass)
    if el:
        el.log(hub.gw_sn, "scan", "ОБЩИЙ СБРОС адресов: resetGateway deviceReset=true", level="warn")
    res = await hub.async_request("resetGateway", "resetGatewayRes",
                                  deviceReset=True, timeout=15.0)
    connection.send_result(msg["id"], {"ok": bool(res and res.get("ack")), "res": res})


# ── Сеть и имя шлюза (getGwIpInfor/setGwIpInfor/setGatewayName) ───────────────
# Управление сетью шлюза. Чтение — без admin; запись (сеть/имя) — admin + подтверждение
# на карточке. По Wireshark-захвату рабочий setGwIpInfor родного приложения = только
# {mode, ipAddr, mask, defaultGateway} БЕЗ EMQ-полей (MQTT-конфиг шлюз сохраняет сам).
# Сетевые изменения применяются ТОЛЬКО после рестарта шлюза — рестарт предлагает карточка.


@websocket_api.websocket_command({
    vol.Required("type"): "arvid_dali_center/get_gw_net",
    vol.Required("gw_sn"): str,
})
@websocket_api.async_response
async def ws_get_gw_net(hass, connection, msg):
    """Прочитать сетевые настройки шлюза (getGwIpInfor). EMQ-поля карточке НЕ отдаём.
    Ответный cmd шлюза в протоколе опечатан (setGwIpInforRes) — корреляция по msgId, имя
    cmd не важно. Имя/этаж берём из кеша discovery (hub.gw)."""
    hub = _find_hub(hass, msg["gw_sn"])
    if not hub:
        connection.send_error(msg["id"], "not_found", "шлюз не найден")
        return
    res = await hub.async_request("getGwIpInfor", "setGwIpInforRes", timeout=8.0)
    if not res:
        connection.send_error(msg["id"], "no_response", "шлюз не ответил")
        return
    gw = hub.gw or {}
    connection.send_result(msg["id"], {
        "mode": res.get("mode"),
        "ipAddr": res.get("ipAddr"),
        "mask": res.get("mask"),
        "defaultGateway": res.get("defaultGateway"),
        "name": gw.get("name"),
        "floorName": gw.get("floorName"),
    })


@websocket_api.require_admin
@websocket_api.websocket_command({
    vol.Required("type"): "arvid_dali_center/set_gw_net",
    vol.Required("gw_sn"): str,
    vol.Required("mode"): vol.In(["static", "dhcp"]),
    vol.Optional("ipAddr"): str,
    vol.Optional("mask"): str,
    vol.Optional("defaultGateway"): str,
})
@websocket_api.async_response
async def ws_set_gw_net(hass, connection, msg):
    """Записать сетевые настройки шлюза (setGwIpInfor). Шлём только {mode[,ipAddr,mask,
    defaultGateway]} — как родное приложение (без EMQ, MQTT шлюз сохраняет сам). Для dhcp
    поля IP не шлём. ⚠ Применяется ТОЛЬКО после рестарта шлюза (карточка предложит). После
    рестарта хаб найдёт шлюз заново по gwSn (IP не храним). Предупреждение о подсети — на карточке."""
    hub = _find_hub(hass, msg["gw_sn"])
    if not hub:
        connection.send_error(msg["id"], "not_found", "шлюз не найден")
        return
    mode = msg["mode"]
    if mode == "static" and not all(msg.get(f) for f in ("ipAddr", "mask", "defaultGateway")):
        connection.send_error(msg["id"], "bad_request",
                              "для static нужны ipAddr, mask, defaultGateway")
        return
    fields = {"mode": mode}
    if mode == "static":
        for f in ("ipAddr", "mask", "defaultGateway"):
            fields[f] = msg[f]
    el = get_eventlog(hass)
    if el:
        tgt = f"static {msg.get('ipAddr')}/{msg.get('mask')} gw {msg.get('defaultGateway')}" \
            if mode == "static" else "dhcp"
        el.log(hub.gw_sn, "scan", f"СМЕНА СЕТИ ШЛЮЗА → {tgt} (применится после рестарта)", level="warn")
    res = await hub.async_request("setGwIpInfor", "setGwIpInforRes", timeout=10.0, **fields)
    connection.send_result(msg["id"], {"ok": bool(res and res.get("ack")), "res": res})


@websocket_api.require_admin
@websocket_api.websocket_command({
    vol.Required("type"): "arvid_dali_center/set_gw_name",
    vol.Required("gw_sn"): str,
    vol.Required("name"): str,
})
@websocket_api.async_response
async def ws_set_gw_name(hass, connection, msg):
    """Переименовать шлюз (setGatewayName). По Wireshark-захвату рабочий запрос родного
    приложения = {cmd, gwSn, name} БЕЗ floorId/floorName и БЕЗ msgId; ответ
    setGatewayNameRes приходит БЕЗ msgId (поле gwPid) → корреляция через фолбэк по res_cmd
    (см. coordinator._dispatch). Шлём только name, чтобы не трогать привязку этажа."""
    hub = _find_hub(hass, msg["gw_sn"])
    if not hub:
        connection.send_error(msg["id"], "not_found", "шлюз не найден")
        return
    name = (msg["name"] or "").strip()
    if not name:
        connection.send_error(msg["id"], "bad_request", "имя не может быть пустым")
        return
    res = await hub.async_request("setGatewayName", "setGatewayNameRes", timeout=8.0, name=name)
    ok = bool(res and res.get("ack"))
    if ok:
        # обновляем локальный кеш, чтобы карточка сразу показала новое имя
        if hub.gw is not None:
            hub.gw["name"] = name
    connection.send_result(msg["id"], {"ok": ok, "res": res})


@websocket_api.websocket_command({
    vol.Required("type"): "arvid_dali_center/groups",
    vol.Required("gw_sn"): str,
})
@callback
def ws_groups(hass, connection, msg):
    """Список групп с составом и entity_id light-сущности группы (из кеша хаба)."""
    hub = _find_hub(hass, msg["gw_sn"])
    if not hub:
        connection.send_error(msg["id"], "not_found", "шлюз не найден")
        return
    reg = er.async_get(hass)
    out = []
    for g in hub.groups:
        uid = f"{hub.gw_sn}_group_{g['channel']}_{g['groupId']}"
        out.append({
            "channel": g["channel"], "groupId": g["groupId"], "name": g.get("name", ""),
            "members": g.get("members", []),
            "present": g.get("present", True),   # есть ли группа на контроллере
            "entity_id": reg.async_get_entity_id("light", DOMAIN, uid),
        })
    connection.send_result(msg["id"], {"groups": out})


@websocket_api.require_admin
@websocket_api.websocket_command({
    vol.Required("type"): "arvid_dali_center/identify",
    vol.Required("gw_sn"): str,
    vol.Required("devType"): str,
    vol.Required("channel"): int,
    vol.Required("address"): int,
})
@websocket_api.async_response
async def ws_identify(hass, connection, msg):
    """Моргнуть устройством (identifyDev) — для сопоставления физика↔план."""
    hub = _find_hub(hass, msg["gw_sn"])
    if not hub:
        connection.send_error(msg["id"], "not_found", "шлюз не найден")
        return
    res = await hub.async_request("identifyDev", "identifyDevRes", timeout=8.0, data={
        "devType": msg["devType"], "channel": msg["channel"], "address": msg["address"]})
    connection.send_result(msg["id"], {"ok": bool(res and res.get("ack")), "res": res})


def _member_set(members) -> set:
    """Нормализованный набор членов для сверки: (devType, channel, address)."""
    return {(str(m.get("devType")), m.get("channel"), m.get("address"))
            for m in (members or [])}


def _res_reason(res) -> str | None:
    """Человеческая ПРИЧИНА отказа из сырого ответа шлюза (v1.2.23).

    Раньше любой не-ack превращался в глухое «не подтверждено шлюзом», и пользователь гадал.
    Два разбираемых случая:
    - `statusBus` (мануал стр. 64) — шлюз ответил «шина занята» ВМЕСТО ответа на команду. На
      железе 2026-07-29: активная автояркость держит шину, создание группы падает (в РОДНОМ
      DALI Center — тоже, т.е. это не наш дефект);
    - `None` — ответа не было вовсе (таймаут/нет связи).
    Возвращает None, если ответ обычный (причина не распознана)."""
    if res is None:
        return "шлюз не ответил (таймаут) — шина могла быть занята"
    if isinstance(res, dict) and res.get("cmd") == "statusBus":
        return ("DALI-шина ЗАНЯТА — шлюз отклонил команду. Массовая запись при работающей "
                "автояркости кладёт шину: снимите автояркость и повторите")
    return None


async def _apply_group(hass, hub, channel: int, group_id: int, name: str,
                       members: list, crossGateway: str = "no",
                       *, force_clear: bool, op: str) -> dict:
    """Создать/пересоздать DALI-группу детерминированно и СВЕРИТЬ итоговый состав.

    Состав DALI-группы (id 0-15) — это биты принадлежности на самих лампах; отдельных
    команд «убрать одну лампу из группы» в протоколе нет. Поэтому правка состава =
    `delGroup` (целиком) + `addGroup` с нужным набором (как и просил пользователь).

    force_clear — слать ли `delGroup` перед `addGroup`. С v1.2.15 зовущие передают ВСЕГДА
    True: иначе шлюз держит старый состав, `addGroup` поверх него игнорируется/мёржится →
    `writeGroup` бьёт по старым лампам, а имя остаётся прежним. Раньше параметр гейтился по
    «есть ли id в таблице шлюза» — но таблица не знает про БИТЫ НА ЛАМПАХ, и удалённый id
    выглядел свободным, оставаясь физически грязным (см. `ws_create_group`).

    Возвращает {ok, verify, res, raw}. verify — сверка запрошенного и фактического
    (из `readGroup`) состава: лишние/недостающие лампы (диагностика прошивки шлюза).
    Все сырые ответы шлюза логируются (видно в `ha core logs`)."""
    raw = {}
    # 0) при реюзе/правке — сперва ОЧИСТИТЬ слот целиком (сброс битов на лампах)
    if force_clear:
        dres = await hub.async_request("delGroup", "delGroupRes",
                                       channel=channel, groupId=group_id, timeout=8.0)
        raw["delGroup"] = dres
        _LOGGER.info("group %s: delGroup ch%s id%s → %s", op, channel, group_id, dres)
    # 1) создать заново с нужным составом и именем
    ares = await hub.async_request("addGroup", "addGroupRes",
                                   channel=channel, groupId=group_id,
                                   name=name, crossGateway=crossGateway,
                                   data=members, timeout=8.0)
    raw["addGroup"] = ares
    _LOGGER.info("group %s: addGroup ch%s id%s «%s» members=%s → %s",
                 op, channel, group_id, name, members, ares)
    ok = bool(ares and ares.get("ack"))
    if not ok:
        reason = _res_reason(ares)
        if reason:
            _LOGGER.warning("group %s: ch%s id%s НЕ применено — %s", op, channel, group_id, reason)
        return {"ok": False, "verify": None, "res": ares, "raw": raw, "reason": reason}

    # 2) СВЕРКА: перечитать фактический состав с контроллера
    rr = await hub.async_request("readGroup", "readGroupRes",
                                 channel=channel, groupId=group_id, timeout=8.0)
    raw["readGroup"] = rr
    _LOGGER.info("group %s: readGroup ch%s id%s → %s", op, channel, group_id, rr)
    actual = [{"devType": str(m.get("devType")), "channel": m.get("channel"),
               "address": m.get("address")} for m in (rr or {}).get("data", []) or []]
    req_set, act_set = _member_set(members), _member_set(actual)
    verify = {
        "requested": sorted(m[2] for m in req_set if m[2] is not None),
        "actual": sorted(m[2] for m in act_set if m[2] is not None),
        "extra": sorted(m[2] for m in (act_set - req_set) if m[2] is not None),   # лишние
        "missing": sorted(m[2] for m in (req_set - act_set) if m[2] is not None), # не добавились
        "match": act_set == req_set,
    }
    if not verify["match"]:
        _LOGGER.warning("group %s: состав НЕ совпал ch%s id%s: лишние=%s, недобавлены=%s "
                        "(прошивка шлюза не очистила/не добавила биты)",
                        op, channel, group_id, verify["extra"], verify["missing"])

    # 3) обновить кеш хаба + персист (источник правды, переживает рестарт/обрыв)
    group = {"channel": channel, "groupId": group_id, "name": name,
             "members": actual or members, "present": True}
    hub.groups = [g for g in hub.groups
                  if not (g["channel"] == channel and g["groupId"] == group_id)]
    hub.groups.append(group)
    gs = get_group_store(hass)
    if gs:
        await gs.async_upsert(hub.gw_sn, group)
    # 4) пересоздать light-сущность: снести живую + остатки реестра (наш uid и чужой
    #    group-orphan, занявший желаемый light.<имя>), добавить новую, форснуть entity_id
    await hub.async_remove_group_entity(channel, group_id)
    reg = er.async_get(hass)
    uid = f"{hub.gw_sn}_group_{channel}_{group_id}"
    desired = f"light.{slugify(name) or 'group_' + str(group_id)}"
    # точечно (без полного перебора реестра на 4400+ сущностей):
    #  а) снести нашу прошлую запись по uid
    old_eid = reg.async_get_entity_id("light", DOMAIN, uid)
    if old_eid:
        reg.async_remove(old_eid)
    #  б) снести чужой group-orphan, занявший желаемый light.<имя>
    holder = reg.async_get(desired)
    if (holder and holder.unique_id != uid
            and str(holder.unique_id).startswith(f"{hub.gw_sn}_group_")):
        reg.async_remove(desired)
    hub.add_group_entity(group)
    await _force_group_entity_id(hass, hub.gw_sn, channel, group_id, name)
    #  в) Fix X (v1.2.4) + v1.2.18 (F5): сбросить ЗАЛИПШЕЕ `name_by_user` на записи УСТРОЙСТВА
    #     группы. `unique_id` группы зависит только от номера (`{gw}_group_{ch}_{id}`), поэтому
    #     новая группа цепляется к записи `device_registry` от ПРЕЖНЕЙ группы с тем же id — а там
    #     остался `name_by_user` (его ставит ренейм), и он ПЕРЕКРЫВАЕТ имя с контроллера:
    #     наблюдалось «entity_id новый и верный, а имя старое». Имя группы — СТРОГО с контроллера
    #     (getGroup/setGroupName), поэтому пользовательское имя здесь снимаем.
    #     ⚠ v1.2.18 (F5): сброс ПЕРЕНЕСЁН СЮДА — ПОСЛЕ `add_group_entity`. Раньше он стоял ДО, но
    #     `ws_del_group` сносит запись устройства В КОРЗИНУ (Закон 1), и до `add_group_entity`
    #     записи НЕТ (`gdev is None` → сброс пропускался). Именно `add_group_entity` ВОСКРЕШАЕТ
    #     запись из корзины со СТАРЫМ `name_by_user` → его и надо снять уже ПОСЛЕ воскрешения.
    gdev = dr.async_get(hass).async_get_device(identifiers={(DOMAIN, uid)})
    if gdev and gdev.name_by_user:
        dr.async_get(hass).async_update_device(gdev.id, name_by_user=None)
        _LOGGER.info("group %s: снято залипшее имя устройства %r (имя группы — с контроллера)",
                     op, gdev.name_by_user)
    el = get_eventlog(hass)
    if el:
        note = "" if verify["match"] else f" ⚠ лишние={verify['extra']} недобавл={verify['missing']}"
        el.log(hub.gw_sn, "group",
               f"{op}: группа {group_id} «{name}» состав={verify['actual']}{note}")
    return {"ok": True, "verify": verify, "res": ares, "raw": raw}


@websocket_api.require_admin
@websocket_api.websocket_command({
    vol.Required("type"): "arvid_dali_center/create_group",
    vol.Required("gw_sn"): str,
    vol.Required("channel"): int,
    # v1.2.20: DALI-групп физически 16 (0–15). Раньше был просто int → карточка при 16 занятых
    # слотах могла предложить номер 16, и он уходил на шину (`<input max=15>` не мешает — значение
    # проставляется программно). Валидируем на бэкенде — источнике истины.
    vol.Required("groupId"): vol.All(int, vol.Range(min=0, max=15)),
    vol.Optional("name", default=""): str,
    vol.Required("members"): list,
    vol.Optional("crossGateway", default="no"): str,
})
@websocket_api.async_response
async def ws_create_group(hass, connection, msg):
    """Создать DALI-группу. members: [{devType,channel,address}].

    ⚠ v1.2.15: `delGroup` перед `addGroup` шлётся ВСЕГДА (`force_clear=True`).

    Было: `force_clear=reuse`, где `reuse` = «группа с таким id есть в `hub.groups` (present)»,
    то есть В ТАБЛИЦЕ ШЛЮЗА. Для «нового» id delGroup не слался — «без лишних команд».
    Дефект (найден на железе 2026-07-17): состав DALI-группы — это БИТЫ НА ЛАМПАХ, а таблица
    шлюза — его собственная память. `delGroup` убирает запись из таблицы, но биты на лампах
    остаются. Тогда id выглядит СВОБОДНЫМ (`reuse=False`), delGroup не шлётся, и `addGroup`
    ложится на ГРЯЗНЫЙ слот — а он, как описано в `_apply_group`, «игнорируется/мёржится»:
    шлюз пишет в таблицу новый состав и имя, но на шине остаётся СТАРОЕ. Наблюдалось: группа
    №3 пересоздана как «3_etazh» на 9 ламп, а физически управляла двумя лампами прежней
    kab_303 и держала её имя (сверка `readGroup` не ловила — она читает ту же таблицу).

    Вопрос «свободен ли id» таблице шлюза задавать НЕЛЬЗЯ: она не знает про биты на лампах.
    Единственный надёжный ответ — чистить слот всегда. Цена — одна лишняя команда `delGroup`
    на создание; правка состава (`ws_edit_group`) так и работает с v0.18 и проверена на железе."""
    hub = _find_hub(hass, msg["gw_sn"])
    if not hub:
        connection.send_error(msg["id"], "not_found", "шлюз не найден")
        return
    # 🔴 ГЕЙТ v1.2.65: номер, занятый КРОСС-группой на этом шлюзе, брать нельзя.
    # У кросс-группы на каждом участнике лежит СВОЯ копия с тем же `groupId`, но в `hub.groups`
    # её нет (гейт в `async_load_groups`, иначе на один свет появились бы три сущности). Значит
    # для обычного создания слот выглядел СВОБОДНЫМ — и `addGroup` ложился поверх копии,
    # выталкивая кросс-группу с одного из шлюзов (замечание пользователя 2026-08-12). У самих
    # кросс-групп такая проверка есть с v1.2.40 — здесь её просто не было.
    from .store import get_cross_group_store
    xgs = get_cross_group_store(hass)
    for xg in (xgs.for_gateway(hub.gw_sn) if xgs else []):
        if xg.get("channel") == msg["channel"] and xg.get("groupId") == msg["groupId"]:
            connection.send_error(
                msg["id"], "group_id_busy",
                f"номер {msg['groupId']} занят КРОСС-шлюзовой группой «{xg.get('name')}» "
                f"(она живёт копиями на {len(xg.get('participants') or [])} контроллерах). "
                f"Возьмите другой номер, иначе её копия на этом шлюзе будет затёрта.")
            _LOGGER.warning("create_group %s ch%s id%s отклонён: номер держит кросс-группа %s",
                            hub.gw_sn, msg["channel"], msg["groupId"], xg.get("uid"))
            return
    out = await _apply_group(hass, hub, msg["channel"], msg["groupId"], msg["name"],
                             msg["members"], msg["crossGateway"],
                             force_clear=True, op="create")
    connection.send_result(msg["id"], {"ok": out["ok"], "verify": out["verify"],
                                       "res": out["res"], "reason": out.get("reason")})


@websocket_api.require_admin
@websocket_api.websocket_command({
    vol.Required("type"): "arvid_dali_center/set_group_members",
    vol.Required("gw_sn"): str,
    vol.Required("channel"): int,
    vol.Required("groupId"): vol.All(int, vol.Range(min=0, max=15)),   # v1.2.20: DALI 0–15
    vol.Optional("name", default=""): str,
    vol.Required("members"): list,
    vol.Optional("crossGateway", default="no"): str,
})
@websocket_api.async_response
async def ws_set_group_members(hass, connection, msg):
    """Изменить СОСТАВ группы (добавить/убрать лампы). Отдельных команд per-lamp в
    протоколе нет → удаляем группу целиком и пересоздаём с новым набором (force_clear).
    Имя сохраняем (берём из msg или из текущего кеша)."""
    hub = _find_hub(hass, msg["gw_sn"])
    if not hub:
        connection.send_error(msg["id"], "not_found", "шлюз не найден")
        return
    name = msg["name"]
    if not name:   # имя не передали — взять текущее (контроллерное) из кеша
        for g in hub.groups:
            if g.get("channel") == msg["channel"] and g.get("groupId") == msg["groupId"]:
                name = g.get("name") or ""
                break
    out = await _apply_group(hass, hub, msg["channel"], msg["groupId"], name,
                             msg["members"], msg["crossGateway"],
                             force_clear=True, op="edit")
    connection.send_result(msg["id"], {"ok": out["ok"], "verify": out["verify"],
                                       "res": out["res"], "reason": out.get("reason")})


@websocket_api.require_admin
@websocket_api.websocket_command({
    vol.Required("type"): "arvid_dali_center/del_group",
    vol.Required("gw_sn"): str,
    vol.Required("channel"): int,
    vol.Required("groupId"): int,
})
@websocket_api.async_response
async def ws_del_group(hass, connection, msg):
    hub = _find_hub(hass, msg["gw_sn"])
    if not hub:
        connection.send_error(msg["id"], "not_found", "шлюз не найден")
        return
    res = await hub.async_request("delGroup", "delGroupRes",
                                  channel=msg["channel"], groupId=msg["groupId"], timeout=8.0)
    _LOGGER.info("group del: delGroup ch%s id%s → %s",
                 msg["channel"], msg["groupId"], res)
    # ВЕРИФИКАЦИЯ: перечитать список групп с контроллера — реально ли исчезла.
    # (getGroup — кеш контроллера; если группа осталась → delGroup не отработал.)
    gone = None
    gres = await hub.async_request("getGroup", "getGroupRes",
                                   getFlag="exited", timeout=8.0)
    _LOGGER.info("group del: getGroup после удаления → %s", gres)
    if gres is not None:
        still = False
        for blk in (gres or {}).get("group", []) or []:
            if blk.get("channel") != msg["channel"]:
                continue
            for g in blk.get("data", []) or []:
                if g.get("groupId") == msg["groupId"]:
                    still = True
        gone = not still
        if not gone:
            _LOGGER.warning("group del: группа ch%s id%s ОСТАЛАСЬ на контроллере после "
                            "delGroup (прошивка не удалила)", msg["channel"], msg["groupId"])
    # 1) удалить ЖИВУЮ сущность (иначе она пересоздаст запись реестра со старым entity_id)
    await hub.async_remove_group_entity(msg["channel"], msg["groupId"])
    # 2) подчистить остаток записи реестра (на случай, если сущность не была трекнута)
    uid = f"{hub.gw_sn}_group_{msg['channel']}_{msg['groupId']}"
    reg = er.async_get(hass)
    eid = reg.async_get_entity_id("light", DOMAIN, uid)
    if eid:
        reg.async_remove(eid)
    # 3) СНЕСТИ ЗАПИСЬ УСТРОЙСТВА (Fix X, v1.2.4). Без этого имя старой группы ЗАЛИПАЛО:
    # `unique_id` группы = `{gw}_group_{ch}_{groupId}` — он зависит ТОЛЬКО от номера группы,
    # поэтому НОВАЯ группа с тем же id цепляется к ТОЙ ЖЕ записи `device_registry`. А в ней от
    # прежней группы остаётся `name_by_user` (его ставит `ws_rename_group`), и он ПЕРЕКРЫВАЕТ имя
    # с контроллера → наблюдалось: `entity_id` у новой группы верный, а имя — старое.
    # Удаление группы — РУЧНОЕ действие человека, поэтому снос записи здесь легитимен
    # (авто-деструктива не добавляем; частный случай долга D2).
    dev_reg = dr.async_get(hass)
    gdev = dev_reg.async_get_device(identifiers={(DOMAIN, uid)})
    if gdev:
        dev_reg.async_remove_device(gdev.id)
    el = get_eventlog(hass)
    if el:
        gone_note = "" if gone is None else (" (подтверждено)" if gone else " ⚠ ОСТАЛАСЬ на шлюзе")
        el.log(hub.gw_sn, "group", f"del: группа {msg['groupId']} удалена{gone_note}"
               + (f" ({eid})" if eid else ""))
    hub.groups = [g for g in hub.groups
                  if not (g["channel"] == msg["channel"] and g["groupId"] == msg["groupId"])]
    # снять из персиста (иначе восстановится как present=False при следующей загрузке)
    gs = get_group_store(hass)
    if gs:
        await gs.async_remove(hub.gw_sn, msg["channel"], msg["groupId"])
    # почистить легаси NameStore-имя группы (иначе залипнет за этим groupId и всплывёт
    # на следующей группе под тем же id — источник «перепутанных имён»)
    ns = get_name_store(hass)
    if ns:
        await ns.async_set(group_name_key(hub.gw_sn, msg["channel"], msg["groupId"]), "")
    # F11 (v1.2.20): почистить ПАРАМЕТРЫ группы (GroupParamStore, ключ gw:channel:groupId —
    # переиспользуемый слот). Иначе fadeRate/fadeTime удалённой группы достанется НОВОЙ группе с
    # тем же id и попадёт в расчёт удержания-диммирования. Тот же приём, что для имени выше.
    gps = get_group_param_store(hass)
    if gps:
        await gps.async_remove(hub.gw_sn, msg["channel"], msg["groupId"])
    # gone: True — контроллер подтвердил удаление; False — группа осталась (прошивка не
    # удалила); None — getGroup не ответил (связь). Карточка показывает предупреждение.
    connection.send_result(msg["id"], {"ok": bool(res and res.get("ack")),
                                       "gone": gone, "res": res})


@websocket_api.require_admin
@websocket_api.websocket_command({
    vol.Required("type"): "arvid_dali_center/group_reload",
    vol.Required("gw_sn"): str,
})
@websocket_api.async_response
async def ws_group_reload(hass, connection, msg):
    """Подтянуть группы и их состав ЗАНОВО с контроллера (getGroup + readGroup) и
    вернуть свежий список. Сырые ответы шлюза логируются (видно в `ha core logs`) —
    инструмент диагностики «что реально на контроллере»."""
    hub = _find_hub(hass, msg["gw_sn"])
    if not hub:
        connection.send_error(msg["id"], "not_found", "шлюз не найден")
        return
    groups = await hub.async_load_groups()   # перечитывает с контроллера + логирует raw
    reg = er.async_get(hass)
    out = []
    for g in groups:
        uid = f"{hub.gw_sn}_group_{g['channel']}_{g['groupId']}"
        out.append({
            "channel": g["channel"], "groupId": g["groupId"], "name": g.get("name", ""),
            "members": g.get("members", []), "present": g.get("present", True),
            "entity_id": reg.async_get_entity_id("light", DOMAIN, uid),
        })
    connection.send_result(msg["id"], {"groups": out})


@websocket_api.require_admin
@websocket_api.websocket_command({
    vol.Required("type"): "arvid_dali_center/group_write",
    vol.Required("gw_sn"): str,
    vol.Required("channel"): int,
    vol.Required("groupId"): int,
    vol.Required("property"): list,
})
@websocket_api.async_response
async def ws_group_write(hass, connection, msg):
    hub = _find_hub(hass, msg["gw_sn"])
    if not hub:
        connection.send_error(msg["id"], "not_found", "шлюз не найден")
        return
    res = await hub.async_request("writeGroup", "writeGroupRes",
                                  channel=msg["channel"], groupId=msg["groupId"],
                                  data=msg["property"])
    connection.send_result(msg["id"], {"ok": bool(res and res.get("ack")), "res": res})


# ── Привязки панелей (нативные DALI: кнопка → лампа/группа) ───────────────────
# ── КРОСС-ШЛЮЗОВЫЕ группы (docs/CROSS_GATEWAY.md §2) ─────────────────────────
# Кросс-группа = ОДИН И ТОТ ЖЕ `groupId` + имя, заведённые на КАЖДОМ участнике, каждому —
# только ЕГО лампы (захват 2026-08-04). «Главного» шлюза нет, `writeGroup` бьёт только по
# своим лампам → команды шлём веером. Однолшлюзовые группы — ОТДЕЛЬНАЯ модель, не трогаем.

async def _xgroup_used_ids(hass, gw_sns: list) -> dict:
    """Занятые номера групп по каждому шлюзу — для проверки «номер свободен у ВСЕХ».

    Берём из кеша хаба (`getGroup` уже отработал при загрузке) + из кросс-групп: их копии
    в `hub.groups` не попадают (гейт в `async_load_groups`), но слот занимают физически."""
    from .store import get_cross_group_store
    xgs = get_cross_group_store(hass)
    used: dict = {}
    for gw in gw_sns:
        hub = _find_hub(hass, gw)
        ids = {g["groupId"] for g in (hub.groups if hub else []) if g.get("present")}
        if xgs:
            ids |= {g["groupId"] for g in xgs.for_gateway(gw)}
        used[gw] = ids
    return used


async def _xgroup_write(hass, uid, channel, group_id, name, members) -> dict:
    """Записать кросс-группу: `delGroup`+`addGroup`+`readGroup` НА КАЖДОМ участнике.

    `delGroup` перед `addGroup` — ВСЕГДА (та же причина, что у однолшлюзовых, v1.2.15:
    состав группы это БИТЫ НА ЛАМПАХ, и «свободный» по таблице слот может быть физически
    грязным). ⚠ `areaId` НЕ шлём — областей у нас нет (решение 2026-08-04); на исполнение
    он не влияет, это метка проекта DALI Center. Флаг `crossGateway` тоже не шлём — захват
    показал, что DALI Center его не использует."""
    plan = group_ops.split_write_plan(members)
    warns: list[str] = []

    # ПАРАЛЛЕЛЬНО ПО ШЛЮЗАМ (v1.2.53). Шлюзы независимы: у каждого своя шина и своя копия
    # группы, поэтому цепочку `delGroup → addGroup → readGroup` можно вести одновременно.
    # Раньше шли строго по очереди, и создание кросс-группы на трёх контроллерах занимало
    # три полных круга — замечание с объекта «в DALI Center быстрее». В захвате 2026-08-07
    # видно, что DALI Center тоже не ждёт: два шлюза получают `addGroup` в одну миллисекунду
    # (msgId …04.001Z и …04.002Z).
    async def _write_one(gw, gw_members):
        hub = _find_hub(hass, gw)
        if hub is None:
            warns.append(f"контроллер {gw} не подключён к HA — его лампы в группу не вошли")
            _LOGGER.warning("xgroup %s: контроллер %s не найден — его часть НЕ записана", uid, gw)
            return {"gw": gw, "ok": False, "error": "шлюз не найден", "verify": None}
        dres = await hub.async_request("delGroup", "delGroupRes",
                                       channel=channel, groupId=group_id, timeout=8.0)
        ares = await hub.async_request(
            "addGroup", "addGroupRes", channel=channel, groupId=group_id, name=name,
            data=[{"devType": m["devType"], "gwSnObj": gw,
                   "address": m["address"], "channel": m["channel"]} for m in gw_members],
            timeout=8.0)
        ok = bool(ares and ares.get("ack"))
        _LOGGER.info("xgroup %s [%s]: delGroup→%s addGroup ch%s id%s «%s» %s ламп → %s",
                     uid, gw, bool(dres and dres.get("ack")), channel, group_id, name,
                     len(gw_members), ares)
        verify = None
        if ok:                                  # сверка состава ЭТОГО шлюза (его часть)
            rr = await hub.async_request("readGroup", "readGroupRes",
                                         channel=channel, groupId=group_id, timeout=8.0)
            actual = {(str(m.get("devType")), m.get("channel"), m.get("address"))
                      for m in (rr or {}).get("data", []) or []}
            want = {(m["devType"], m["channel"], m["address"]) for m in gw_members}
            verify = {"match": actual == want,
                      "missing": sorted(str(x) for x in (want - actual)),
                      "extra": sorted(str(x) for x in (actual - want))}
            if not verify["match"]:
                _LOGGER.warning("xgroup %s [%s]: состав не совпал — не добавлены %s, лишние %s",
                                uid, gw, verify["missing"], verify["extra"])
                warns.append(f"на {gw} состав не совпал")
        else:
            warns.append(f"{gw}: {_res_reason(ares) or 'не подтвердил создание'}")
        return {"gw": gw, "ok": ok, "verify": verify,
                "reason": None if ok else _res_reason(ares)}

    raw = await asyncio.gather(*[_write_one(gw, mem) for gw, mem in plan],
                               return_exceptions=True)
    results = []
    for (gw, _mem), r in zip(plan, raw):
        if isinstance(r, BaseException):           # упавший шлюз не рушит остальных
            _LOGGER.error("xgroup %s [%s]: запись упала: %s", uid, gw, r)
            warns.append(f"{gw}: ошибка записи ({r})")
            results.append({"gw": gw, "ok": False, "error": str(r), "verify": None})
        else:
            results.append(r)
    return {"ok": all(r["ok"] for r in results) if results else False,
            "results": results, "warnings": warns}


@websocket_api.require_admin
@websocket_api.websocket_command({
    vol.Required("type"): "arvid_dali_center/cross_groups",
})
@websocket_api.async_response
async def ws_cross_groups(hass, connection, msg):
    """Список кросс-шлюзовых групп + `entity_id` их light-сущности (для toggle/яркости)."""
    from .store import get_cross_group_store
    xgs = get_cross_group_store(hass)
    reg = er.async_get(hass)
    out = []
    for g in (xgs.all() if xgs else []):
        # unique_id кросс-группы = её uid (зафиксирован при создании)
        out.append({**g, "entity_id": reg.async_get_entity_id("light", DOMAIN, g["uid"])})
    connection.send_result(msg["id"], {"groups": out})


@websocket_api.require_admin
@websocket_api.websocket_command({
    vol.Required("type"): "arvid_dali_center/group_slots",
    vol.Required("gw_sns"): list,
})
@websocket_api.async_response
async def ws_group_slots(hass, connection, msg):
    """Занятые/свободные номера групп по выбранным шлюзам.

    Карточка спрашивает ДО создания: номер обязан быть свободен у ВСЕХ участников, иначе
    `addGroup` ляжет ПОВЕРХ чужой группы на одном из них — и сверка этого не покажет
    (`readGroup` читает нашу же таблицу, а биты на чужих лампах уже перезаписаны)."""
    used = await _xgroup_used_ids(hass, list(msg["gw_sns"]))
    connection.send_result(msg["id"], {
        "used": {gw: sorted(ids) for gw, ids in used.items()},
        "free": group_ops.free_group_ids(used),
    })


@websocket_api.require_admin
@websocket_api.websocket_command({
    vol.Required("type"): "arvid_dali_center/create_cross_group",
    vol.Required("channel"): int,
    vol.Required("groupId"): vol.All(int, vol.Range(min=group_ops.DALI_GROUP_MIN,
                                                   max=group_ops.DALI_GROUP_MAX)),
    vol.Required("name"): str,
    vol.Required("members"): list,      # [{gwSnObj,devType,channel,address}] — минимум ДВА шлюза
})
@websocket_api.async_response
async def ws_create_cross_group(hass, connection, msg):
    """Создать кросс-шлюзовую группу: одинаковый `groupId`+имя на каждом участнике."""
    members, channel, gid = msg["members"], msg["channel"], msg["groupId"]
    if not group_ops.is_cross_gateway(members):
        connection.send_error(msg["id"], "bad_request",
                              "в составе лампы только одного контроллера — это обычная группа")
        return
    parts = group_ops.participants(members)
    # ГЕЙТ ДО ШИНЫ: номер должен быть свободен у ВСЕХ участников
    used = await _xgroup_used_ids(hass, parts)
    conflicts = group_ops.group_id_conflicts(gid, used)
    if conflicts:
        connection.send_error(
            msg["id"], "group_id_busy",
            f"номер {gid} уже занят на: {', '.join(conflicts)}. "
            f"Свободные у всех: {group_ops.free_group_ids(used) or '— нет'}")
        return
    uid = group_ops.cross_group_uid(parts, channel, gid)   # ФИКСИРУЕТСЯ ЗДЕСЬ, навсегда
    res = await _xgroup_write(hass, uid, channel, gid, msg["name"], members)
    from .store import get_cross_group_store
    xgs = get_cross_group_store(hass)
    if xgs and res["ok"]:
        xg = {"uid": uid, "channel": channel, "groupId": gid,
              "name": msg["name"], "participants": parts, "members": members}
        await xgs.async_upsert(xg)
        # сущность — СРАЗУ, без рестарта HA (v1.2.43). Якорь тот же, что при старте
        # платформы (алфавитно первый участник), иначе после рестарта появился бы дубль.
        anchor = sorted(str(p).upper() for p in parts)[0]
        ahub = _find_hub(hass, anchor)
        if ahub is None or not ahub.add_cross_group_entity(xg):
            res["warnings"].append("сущность появится после перезапуска HA")
            _LOGGER.warning("xgroup %s: якорь %s не готов — сущность будет создана при старте",
                            uid, anchor)
    el = get_eventlog(hass)
    if el:
        el.log(parts[0], "group",
               f"кросс-группа «{msg['name']}» id{gid} на {len(parts)} контроллерах"
               f"{'' if res['ok'] else ' ⚠ не полностью'}")
    connection.send_result(msg["id"], {**res, "uid": uid, "participants": parts})


@websocket_api.require_admin
@websocket_api.websocket_command({
    vol.Required("type"): "arvid_dali_center/set_cross_group_members",
    vol.Required("uid"): str,
    vol.Optional("name", default=""): str,
    vol.Required("members"): list,
})
@websocket_api.async_response
async def ws_set_cross_group_members(hass, connection, msg):
    """Правка состава. ⚠ `uid` НЕ пересчитывается, даже если набор шлюзов изменился —
    иначе HA завёл бы новую сущность и оборвал историю (летучий ключ, закон 2)."""
    from .store import get_cross_group_store
    xgs = get_cross_group_store(hass)
    cur = xgs.get(msg["uid"]) if xgs else None
    if not cur:
        connection.send_error(msg["id"], "not_found", "кросс-группа не найдена")
        return
    members = msg["members"]
    name = msg["name"] or cur.get("name", "")
    new_parts = group_ops.participants(members)
    # шлюз, ВЫБЫВШИЙ из состава: его копию группы надо снести, иначе останется висеть
    gone = [gw for gw in cur.get("participants", [])
            if gw.upper() not in {p.upper() for p in new_parts}]
    for gw in gone:
        hub = _find_hub(hass, gw)
        if hub is None:
            _LOGGER.warning("xgroup %s: выбывший контроллер %s не подключён — его копия "
                            "группы id%s ОСТАЛАСЬ на шине", msg["uid"], gw, cur["groupId"])
            continue
        await hub.async_request("delGroup", "delGroupRes", channel=cur["channel"],
                                groupId=cur["groupId"], timeout=8.0)
        _LOGGER.info("xgroup %s: снял копию с выбывшего %s", msg["uid"], gw)
    res = await _xgroup_write(hass, msg["uid"], cur["channel"], cur["groupId"], name, members)
    if res["ok"]:
        xg = {"uid": msg["uid"], "channel": cur["channel"], "groupId": cur["groupId"],
              "name": name, "participants": new_parts, "members": members}
        await xgs.async_upsert(xg)
        # сущность НЕ пересоздаём (uid зафиксирован) — обновляем на месте и пересобираем
        # подписку: состав другой, значит другой набор entity_id ламп
        for hub in list((hass.data.get(DOMAIN) or {}).values()):
            ent = hub.cross_group_entity(msg["uid"])
            if ent is not None:
                ent.update_from_store(xg)
                break
    if gone:
        res["warnings"].append(f"снята копия у выбывших: {', '.join(gone)}")
    connection.send_result(msg["id"], {**res, "uid": msg["uid"], "participants": new_parts})


@websocket_api.require_admin
@websocket_api.websocket_command({
    vol.Required("type"): "arvid_dali_center/cross_group_write",
    vol.Required("uid"): str,
    vol.Required("property"): list,      # [{dpid,dataType,value}] — как у group_write
})
@websocket_api.async_response
async def ws_cross_group_write(hass, connection, msg):
    """Управление кросс-группой из карточки: `writeGroup` ВЕЕРОМ на всех участников.

    Ретранслятора нет — каждый контроллер бьёт только по своим лампам, поэтому одной
    команды мало. Недоступный участник попадает в `warnings`: половина помещения не
    отработает, и это должно быть видно, а не проглочено."""
    from .store import get_cross_group_store
    xgs = get_cross_group_store(hass)
    cur = xgs.get(msg["uid"]) if xgs else None
    if not cur:
        connection.send_error(msg["id"], "not_found", "кросс-группа не найдена")
        return
    warns, done = [], 0
    for gw in cur.get("participants", []):
        hub = _find_hub(hass, gw)
        if hub is None or not getattr(hub, "connected", False):
            warns.append(f"{gw} не на связи — его лампы не отработали")
            continue
        await hub.async_request("writeGroup", "writeGroupRes", channel=cur["channel"],
                                groupId=cur["groupId"], data=msg["property"])
        done += 1
    connection.send_result(msg["id"], {"ok": done > 0, "sent": done,
                                       "total": len(cur.get("participants", [])),
                                       "warnings": warns})


@websocket_api.require_admin
@websocket_api.websocket_command({
    vol.Required("type"): "arvid_dali_center/del_cross_group",
    vol.Required("uid"): str,
})
@websocket_api.async_response
async def ws_del_cross_group(hass, connection, msg):
    """Удалить кросс-группу: `delGroup` на КАЖДОМ участнике + снять запись."""
    from .store import get_cross_group_store
    xgs = get_cross_group_store(hass)
    cur = xgs.get(msg["uid"]) if xgs else None
    if not cur:
        connection.send_error(msg["id"], "not_found", "кросс-группа не найдена")
        return
    warns, ok_all = [], True
    for gw in cur.get("participants", []):
        hub = _find_hub(hass, gw)
        if hub is None:
            warns.append(f"контроллер {gw} не подключён — его копия группы осталась на шине")
            ok_all = False
            continue
        res = await hub.async_request("delGroup", "delGroupRes", channel=cur["channel"],
                                      groupId=cur["groupId"], timeout=8.0)
        _LOGGER.info("xgroup %s [%s]: delGroup ch%s id%s → %s",
                     msg["uid"], gw, cur["channel"], cur["groupId"], res)
        if not (res and res.get("ack")):
            warns.append(f"{gw} не подтвердил удаление")
            ok_all = False
    await xgs.async_remove(msg["uid"])          # запись снимаем в любом случае — иначе
    # сущность тоже убираем сразу (иначе висит до рестарта и шлёт команды в никуда)
    for hub in list((hass.data.get(DOMAIN) or {}).values()):
        ent = hub.cross_group_entity(msg["uid"])
        if ent is not None:
            await ent.async_remove(force_remove=True)
            break
    connection.send_result(msg["id"], {"ok": ok_all, "warnings": warns})   # висит призрак


# Команды контроллера: addPanelObj / delPanelObj / readPanel (мануал стр. 53-55).
# Привязка ЖИВЁТ НА ШЛЮЗЕ и работает без HA. Это НЕ HA-сущности, а конфигурация.
# Жесты (dpid) и число кнопок — по типу панели (приложение + decode.PRESS).
PANEL_KEY_COUNT = {"0302": 2, "0304": 4, "0306": 6, "0308": 8, "0300": 1}
PANEL_GESTURES = {                       # devType → список dpid-жестов
    "0302": [1, 2, 3, 5], "0304": [1, 2, 3], "0306": [1, 2, 3],
    "0308": [1, 2, 3], "0300": [1, 3, 4],
}
GESTURE_NAME = {1: "клик", 2: "удержание", 3: "двойной", 4: "поворот", 5: "конец удержания"}


# Состав ячейки привязки (ключ цели с gwSnObj, слияние, остаток) — ЧИСТАЯ логика в
# panel_ops.py: она покрыта stdlib-тестами (tests/test_panel_ops.py), здесь только вызовы.
_target_key = panel_ops.target_key
_panel_target_set = panel_ops.target_set
_cell_target = panel_ops.cell_target
_merge_targets = panel_ops.merge_targets


def _norm_out(out_obj) -> list:
    """Нормализовать outObj из readPanel к форме карточки (точечное обновление ячейки)."""
    return [{"gwSnObj": o.get("gwSnObj"), "devType": str(o.get("devType")),
             "channel": o.get("channel"), "address": o.get("address"),
             "property": o.get("property", [])} for o in (out_obj or [])]


def _prop_action(prop) -> str:
    """Тип действия из property (для сохранения в PanelActStore при add_panel_obj).
    Зеркалит `_actionProp` карточки: 25/26 плавно, 31/32 шаг, 20/22 вкл/яркость."""
    ids = {p.get("dpid"): p for p in (prop or [])}
    if 25 in ids:
        return "dimup"
    if 26 in ids:
        return "dimdown"
    if 31 in ids:
        return "stepup"
    if 32 in ids:
        return "stepdown"
    if 20 in ids and ids[20].get("value") is False:
        return "off"
    if 22 in ids:
        return "onbri"
    if 20 in ids:
        return "on"
    return ""


# ── Кросс-шлюз: ячейка живёт на ДВУХ контроллерах ────────────────────────────
# Захват ДВУХ шлюзов одновременно (2026-08-04, панель E22435088727 + цель 762417130914)
# показал: DALI Center пишет привязку и на шлюз ПАНЕЛИ, и на шлюз ЦЕЛИ. В топик шлюза цели
# уходят `delPanelObj`/`addPanelObj`, где поле `gwSn` = шлюз ПАНЕЛИ, а `outObj` = ТОЛЬКО его
# собственные цели; на шлюз панели одновременно идёт ПОЛНЫЙ состав. Шлюз цели идёт ПЕРВЫМ,
# `setPanelArg`/`readPanel` на него не шлются.
# Мы писали только шлюз панели: `readPanel` честно подтверждал запись, а ИСПОЛНЯТЬ было
# некому — команду отрабатывает контроллер, на котором висит лампа (закон 2: память ≠ физика).

async def _panel_targets_del(hass, gw_sn, dt, ch, addr, key_no, dpid, targets) -> list[str]:
    """Снять цели ячейки на КАЖДОМ чужом контроллере. Возвращает предупреждения для UI."""
    warns: list[str] = []
    for tgw, tobjs in panel_ops.foreign_gateway_targets(targets, gw_sn).items():
        thub = _find_hub(hass, tgw)
        if thub is None:
            warns.append(f"контроллер цели {tgw} не подключён к HA — прежняя привязка на нём цела")
            _LOGGER.warning("panel: контроллер цели %s не найден среди шлюзов HA — "
                            "delPanelObj НЕ отправлен, привязка останется неполной", tgw)
            continue
        res = await thub.async_request(
            "delPanelObj", "delPanelObjRes", gwSn=gw_sn, devType=dt, channel=ch, address=addr,
            data={"keyNo": key_no, "dpid": dpid, "outObj": tobjs}, timeout=8.0)
        _LOGGER.info("panel [цель %s] delPanelObj %s цел. key%s g%s → %s",
                     tgw, len(tobjs), key_no, dpid, res)
        if not (res and res.get("ack")):
            warns.append(f"контроллер цели {tgw} не подтвердил снятие")
    return warns


async def _panel_targets_add(hass, gw_sn, dt, ch, addr, key_no, dpid, mode, panel_type,
                             targets) -> list[str]:
    """Записать ячейку на КАЖДОМ чужом контроллере — только ЕГО цели (эталон захвата)."""
    warns: list[str] = []
    for tgw, tobjs in panel_ops.foreign_gateway_targets(targets, gw_sn).items():
        thub = _find_hub(hass, tgw)
        if thub is None:
            warns.append(f"контроллер цели {tgw} не подключён к HA — кнопка его лампами "
                         f"управлять НЕ будет")
            _LOGGER.warning("panel: контроллер цели %s не найден среди шлюзов HA — "
                            "addPanelObj НЕ отправлен, цель работать не будет", tgw)
            continue
        res = await thub.async_request(
            "addPanelObj", "addPanelObjRes", gwSn=gw_sn, devType=dt, channel=ch, address=addr,
            type=panel_type,
            data={"keyNo": key_no, "dpid": dpid, "mode": mode, "outObj": tobjs}, timeout=8.0)
        _LOGGER.info("panel [цель %s] addPanelObj %s цел. key%s g%s mode%s → %s",
                     tgw, len(tobjs), key_no, dpid, mode, res)
        if not (res and res.get("ack")):
            warns.append(f"контроллер цели {tgw} не принял привязку — его лампы не отработают")
    return warns


def _attach_acts(hass, gw_sn, key_no, dpid, out_list) -> list:
    """Подмешать в каждую цель сохранённое действие (`act`) из PanelActStore — контроллер
    при readPanel тип действия не отдаёт (property пуст), поэтому берём из нашего стора."""
    from .store import get_panel_act_store
    pas = get_panel_act_store(hass)
    if pas:
        for o in out_list:
            o["act"] = pas.get(gw_sn, key_no, dpid, o)
    return out_list


@websocket_api.require_admin
@websocket_api.websocket_command({
    vol.Required("type"): "arvid_dali_center/panel_bindings",
    vol.Required("gw_sn"): str,
    vol.Required("devType"): str,
    vol.Required("channel"): int,
    vol.Required("address"): int,
})
@websocket_api.async_response
async def ws_panel_bindings(hass, connection, msg):
    """Прочитать ВСЮ матрицу привязок панели: для каждой кнопки × жеста — readPanel.
    Возвращает список ячеек с целями (контроллер — источник правды)."""
    hub = _find_hub(hass, msg["gw_sn"])
    if not hub:
        connection.send_error(msg["id"], "not_found", "шлюз не найден")
        return
    dt = str(msg["devType"])
    n_keys = PANEL_KEY_COUNT.get(dt, 0)
    gestures = PANEL_GESTURES.get(dt, [1, 2, 3])
    cells = []
    for key_no in range(1, n_keys + 1):
        for dpid in gestures:
            rr = await hub.async_request(
                "readPanel", "readPanelRes", devType=dt, channel=msg["channel"],
                address=msg["address"], keyNo=key_no, dpid=dpid, timeout=6.0)
            # объёмная построчная диагностика матрицы → DEBUG (иначе флудит ha core logs)
            _LOGGER.debug("panel read ch%s addr%s key%s g%s → %s",
                          msg["channel"], msg["address"], key_no, dpid, rr)
            data = (rr or {}).get("data", {}) or {}
            out_obj = data.get("outObj", []) or []
            cells.append({
                "keyNo": key_no, "dpid": dpid, "gesture": GESTURE_NAME.get(dpid, str(dpid)),
                "type": (rr or {}).get("type"), "mode": data.get("mode"),
                "enable": data.get("enable"),
                "outObj": _attach_acts(hass, msg["gw_sn"], key_no, dpid, _norm_out(out_obj)),
            })
    connection.send_result(msg["id"], {"keyCount": n_keys, "gestures": gestures, "cells": cells})


@websocket_api.require_admin
@websocket_api.websocket_command({
    vol.Required("type"): "arvid_dali_center/add_panel_obj",
    vol.Required("gw_sn"): str,
    vol.Required("devType"): str,
    vol.Required("channel"): int,
    vol.Required("address"): int,
    vol.Required("keyNo"): int,
    vol.Required("dpid"): int,
    vol.Optional("panelType", default=2): int,     # 1:scene/2:control/3:mixed
    vol.Optional("mode", default=255): int,        # 0xFF=дефолт (LED выкл); 129=toggle-режим
    vol.Optional("replace", default=False): bool,  # очистить ячейку перед добавлением
    vol.Optional("action", default=""): str,       # тип действия из карты (для PanelActStore)
    vol.Required("outObj"): list,                  # [{gwSnObj?,devType,channel,address,property}]
})
@websocket_api.async_response
async def ws_add_panel_obj(hass, connection, msg):
    """Привязать действие к кнопке + верификация (readPanel).

    ⚠ ЭТАЛОН ЗАПИСИ — захват DALI Center 2026-08-03 (v1.2.38). Ячейка (кнопка×жест)
    ПЕРЕПИСЫВАЕТСЯ ЦЕЛИКОМ, инкрементального добавления не существует даже когда человек
    жмёт «+ цель»: readPanel → delPanelObj(ВСЕ текущие) → addPanelObj(ПОЛНЫЙ новый состав)
    → setPanelArg(mode) → readPanel. Раньше мы слали ОДНУ новую цель и полагались на то,
    что контроллер добавит её к существующим — поведение недетерминированное (у групп
    ровно так и «мёржилось» не туда), а при кросс-шлюзовой цели давало подмену на свою
    лампу с тем же адресом.

    `setPanelArg` шлём ВСЕГДА (в захвате он идёт после каждого addPanelObj), а не только
    для toggle: раньше режим ячейки на контроллере оставался прежним."""
    hub = _find_hub(hass, msg["gw_sn"])
    if not hub:
        connection.send_error(msg["id"], "not_found", "шлюз не найден")
        return
    dt, ch, addr = str(msg["devType"]), msg["channel"], msg["address"]
    key_no, dpid, gw_sn = msg["keyNo"], msg["dpid"], msg["gw_sn"]
    # 0) текущий состав ячейки — нужен и для слияния, и для честной очистки
    rr0 = await hub.async_request("readPanel", "readPanelRes", devType=dt, channel=ch,
                                  address=addr, keyNo=key_no, dpid=dpid, timeout=6.0)
    cur = (rr0 or {}).get("data", {}).get("outObj", []) or []
    cur_mode = (rr0 or {}).get("data", {}).get("mode", msg["mode"])
    # 1) полный НОВЫЙ состав: «заменить» — только присланные цели, «+ цель» — текущие+новые
    desired = ([_cell_target(o) for o in msg["outObj"]] if msg["replace"]
               else _merge_targets(cur, msg["outObj"], gw_sn))
    # 2) ЧУЖИЕ контроллеры идут ПЕРВЫМИ (эталон захвата 2026-08-04): сначала снимаем на них
    #    прежние цели, и только потом трогаем шлюз панели
    warns = await _panel_targets_del(hass, gw_sn, dt, ch, addr, key_no, dpid, cur)
    # 3) снять ВСЕ текущие цели (иначе addPanelObj ведёт себя недетерминированно)
    if cur:
        dres = await hub.async_request(
            "delPanelObj", "delPanelObjRes", devType=dt, channel=ch, address=addr,
            data={"keyNo": key_no, "dpid": dpid, "enable": True, "mode": cur_mode,
                  "outObj": [_cell_target(o) for o in cur]}, timeout=8.0)
        _LOGGER.info("panel add: delPanelObj(все %s цел.) key%s g%s → %s",
                     len(cur), key_no, dpid, dres)
    # 4) состав чужим контроллерам (их цели), затем ПОЛНЫЙ состав — шлюзу панели
    warns += await _panel_targets_add(hass, gw_sn, dt, ch, addr, key_no, dpid,
                                      msg["mode"], msg["panelType"], desired)
    ares = await hub.async_request(
        "addPanelObj", "addPanelObjRes", devType=dt, channel=ch, address=addr,
        type=msg["panelType"],
        data={"keyNo": key_no, "dpid": dpid, "mode": msg["mode"], "outObj": desired},
        timeout=8.0)
    _LOGGER.info("panel addPanelObj ch%s addr%s key%s g%s type%s mode%s out=%s → %s",
                 ch, addr, key_no, dpid, msg["panelType"], msg["mode"], desired, ares)
    ok = bool(ares and ares.get("ack"))
    # 5) setPanelArg — режим ячейки (mode 129 = toggle, иначе обычный). В захвате идёт
    #    ПОСЛЕ КАЖДОГО addPanelObj на шлюзе ПАНЕЛИ (на шлюз цели он не шлётся)
    if ok:
        sres = await hub.async_request(
            "setPanelArg", "setPanelArgRes", devType=dt, channel=ch, address=addr,
            type=msg["panelType"],
            data=[{"keyNo": key_no, "value": {"dpid": dpid, "mode": msg["mode"]},
                   "enable": True}],
            timeout=8.0)
        _LOGGER.info("panel setPanelArg key%s g%s mode%s → %s", key_no, dpid, msg["mode"], sres)
    verify = None
    cell_out = []
    if ok:
        rr2 = await hub.async_request("readPanel", "readPanelRes", devType=dt, channel=ch,
                                      address=addr, keyNo=key_no, dpid=dpid, timeout=6.0)
        # (диагностику readPanel вернули на debug — вопрос «контроллер не эхоит жест в
        # property» закрыт захватом 2026-07-03, см. docs/PLAN_SENSOR_BINDINGS §H1c)
        _LOGGER.debug("panel add verify readPanel key%s g%s → %s", key_no, dpid, rr2)
        actual = (rr2 or {}).get("data", {}).get("outObj", []) or []
        # сохранить ТИП действия по каждой отправленной цели — контроллер его не вернёт
        # (readPanel property пуст), берём из того, что САМИ отправили
        from .store import get_panel_act_store
        pas = get_panel_act_store(hass)
        if pas:
            # действие из карты (msg["action"], различает toggle от on — property одинаков),
            # иначе выводим из property
            for o in msg["outObj"]:
                act = msg.get("action") or _prop_action(o.get("property"))
                await pas.async_set(msg["gw_sn"], key_no, dpid, o, act)
        cell_out = _attach_acts(hass, msg["gw_sn"], key_no, dpid, _norm_out(actual))
        # сверяем ВЕСЬ желаемый состав (мы записали ячейку целиком), ключ — с gwSnObj:
        # подмена цели на свою лампу с тем же адресом теперь видна, а не «совпало»
        req_set = _panel_target_set(desired, gw_sn)
        act_set = _panel_target_set(actual, gw_sn)
        verify = {"match": req_set.issubset(act_set),
                  "missing": sorted(str(t) for t in (req_set - act_set)),
                  "actualCount": len(act_set)}
        if not verify["match"]:
            _LOGGER.warning("panel add: цель НЕ привязалась key%s g%s missing=%s",
                            key_no, dpid, verify["missing"])
        el = get_eventlog(hass)
        if el:
            el.log(hub.gw_sn, "panel",
                   f"привязка: кнопка{key_no} {GESTURE_NAME.get(dpid, dpid)} → "
                   f"{len(actual)} цел.{'' if verify['match'] else ' ⚠ не совпало'}"
                   f"{' ⚠ ' + '; '.join(warns) if warns else ''}")
    connection.send_result(msg["id"], {"ok": ok, "verify": verify, "warnings": warns,
                                       "keyNo": key_no, "dpid": dpid, "outObj": cell_out,
                                       "res": ares, "reason": None if ok else _res_reason(ares)})


@websocket_api.require_admin
@websocket_api.websocket_command({
    vol.Required("type"): "arvid_dali_center/del_panel_obj",
    vol.Required("gw_sn"): str,
    vol.Required("devType"): str,
    vol.Required("channel"): int,
    vol.Required("address"): int,
    vol.Required("keyNo"): int,
    vol.Required("dpid"): int,
    vol.Required("outObj"): list,    # [{gwSnObj?,devType,channel,address}]
})
@websocket_api.async_response
async def ws_del_panel_obj(hass, connection, msg):
    """Снять цель(и) привязки кнопки + верификация (цель ушла, соседние целы).

    ⚠ Тот же ЭТАЛОН, что и у записи (захват DALI Center 2026-08-03): ячейка переписывается
    целиком — readPanel → delPanelObj(ВСЕ текущие) → addPanelObj(остаток) → setPanelArg →
    readPanel. Раньше слали delPanelObj на ОДНУ цель и рассчитывали, что контроллер снимет
    её выборочно; выборочного снятия на железе не наблюдалось ни разу (DALI Center так не
    делает), а при совпадении адресов на разных шлюзах могла уйти чужая цель."""
    hub = _find_hub(hass, msg["gw_sn"])
    if not hub:
        connection.send_error(msg["id"], "not_found", "шлюз не найден")
        return
    dt, ch, addr = str(msg["devType"]), msg["channel"], msg["address"]
    key_no, dpid, gw_sn = msg["keyNo"], msg["dpid"], msg["gw_sn"]
    # 0) текущий состав ячейки + её режим (mode нужен, чтобы вернуть остаток как было)
    rr0 = await hub.async_request("readPanel", "readPanelRes", devType=dt, channel=ch,
                                  address=addr, keyNo=key_no, dpid=dpid, timeout=6.0)
    cur = (rr0 or {}).get("data", {}).get("outObj", []) or []
    mode = (rr0 or {}).get("data", {}).get("mode", 255)
    drop = _panel_target_set(msg["outObj"], gw_sn)
    rest = panel_ops.remaining_targets(cur, msg["outObj"], gw_sn)
    # 1) ЧУЖИЕ контроллеры — ПЕРВЫМИ: снимаем на них весь их прежний состав (v1.2.39).
    #    Без этого снятая цель продолжала бы отрабатывать со своего шлюза, а ячейка панели
    #    показывала бы, что цели нет.
    warns = await _panel_targets_del(hass, gw_sn, dt, ch, addr, key_no, dpid, cur)
    # 2) снять ВСЕ текущие цели на шлюзе панели
    dres = await hub.async_request(
        "delPanelObj", "delPanelObjRes", devType=dt, channel=ch, address=addr,
        data={"keyNo": key_no, "dpid": dpid, "enable": True, "mode": mode,
              "outObj": [_cell_target(o) for o in cur]}, timeout=8.0)
    _LOGGER.info("panel delPanelObj(все %s цел.) key%s g%s снимаем=%s → %s",
                 len(cur), key_no, dpid, sorted(str(t) for t in drop), dres)
    # 3) вернуть остаток (цели, которые снимать не просили) — сперва чужим контроллерам
    if bool(dres and dres.get("ack")) and rest:
        warns += await _panel_targets_add(hass, gw_sn, dt, ch, addr, key_no, dpid, mode, 2, rest)
        ares = await hub.async_request(
            "addPanelObj", "addPanelObjRes", devType=dt, channel=ch, address=addr, type=2,
            data={"keyNo": key_no, "dpid": dpid, "mode": mode, "outObj": rest}, timeout=8.0)
        _LOGGER.info("panel del: вернули остаток %s цел. key%s g%s → %s",
                     len(rest), key_no, dpid, ares)
        await hub.async_request(
            "setPanelArg", "setPanelArgRes", devType=dt, channel=ch, address=addr, type=2,
            data=[{"keyNo": key_no, "value": {"dpid": dpid, "mode": mode}, "enable": True}],
            timeout=8.0)
    rr = await hub.async_request("readPanel", "readPanelRes", devType=dt, channel=ch,
                                 address=addr, keyNo=key_no, dpid=dpid, timeout=6.0)
    _LOGGER.debug("panel del verify readPanel key%s g%s → %s", key_no, dpid, rr)
    actual = (rr or {}).get("data", {}).get("outObj", []) or []
    act_set = _panel_target_set(actual, gw_sn)
    gone = not (drop & act_set)                       # снятые цели действительно ушли
    kept = _panel_target_set(rest, gw_sn).issubset(act_set)   # соседние цели уцелели
    if not kept:
        _LOGGER.warning("panel del: ОСТАТОК ячейки не восстановился key%s g%s (было %s, стало %s)",
                        key_no, dpid, len(rest), len(act_set))
    # снятые цели → убрать их сохранённое действие из PanelActStore (чистота)
    from .store import get_panel_act_store
    pas = get_panel_act_store(hass)
    if pas:
        for o in msg["outObj"]:
            await pas.async_set(msg["gw_sn"], key_no, dpid, o, "")
    el = get_eventlog(hass)
    if el:
        el.log(hub.gw_sn, "panel",
               f"снятие привязки: кнопка{key_no} {GESTURE_NAME.get(dpid, dpid)}"
               f"{'' if gone else ' ⚠ осталась'}{'' if kept else ' ⚠ остаток не вернулся'}"
               f"{' ⚠ ' + '; '.join(warns) if warns else ''}")
    connection.send_result(msg["id"], {"ok": bool(dres and dres.get("ack")),
                                        "gone": gone, "kept": kept, "warnings": warns,
                                        "keyNo": key_no, "dpid": dpid,
                                        "outObj": _attach_acts(hass, msg["gw_sn"], key_no, dpid,
                                                               _norm_out(actual)), "res": dres})


# ── КРОСС-ШЛЮЗ у ДАТЧИКОВ (гейт G38 закрыт захватом 2026-08-05) ──────────────
# Синхронный захват ДВУХ шлюзов при снятии автояркости в DALI Center показал ТУ ЖЕ схему,
# что у панелей: в топик шлюза ЦЕЛИ уходит `delSensorObj`, где поле `gwSn` = шлюз ДАТЧИКА,
# координаты (`devType`/`channel`/`address`) — САМОГО ДАТЧИКА, а `outputObj` — ТОЛЬКО цели
# этого шлюза; на шлюз датчика одновременно уходит ПОЛНЫЙ состав. Цель идёт ПЕРВОЙ
# (msgId …32.001Z против …32.003Z), `readSensor` шлётся только шлюзу датчика.
#
# Это и был симптом с объекта: наша кнопка «Очистить» слала `delSensor` ТОЛЬКО на шлюз
# датчика — копии на шлюзах целей оставались, лампы продолжали регулироваться, и DALI Center
# показывал привязку живой. Классика закона 2: своя память очистилась, физика — нет.
#
# ⚠ УРОВЕНЬ ДОКАЗАТЕЛЬСТВА (docs/CONFIRMATION_MODEL.md): фан-аут `delSensorObj` ПОДТВЕРЖДЁН
# захватом; фан-аут `addSensorObj` — ПО АНАЛОГИИ с панелями (захвата создания кросс-привязки
# датчика по двум шлюзам нет). Проверяется гейтом G40.

async def _sensor_targets_del(hass, sensor_gw, dt, ch, addr, dpid, targets) -> list[str]:
    """Снять цели события датчика на КАЖДОМ чужом контроллере (его цели). → предупреждения."""
    warns: list[str] = []
    for tgw, tobjs in panel_ops.foreign_gateway_targets(targets, sensor_gw).items():
        thub = _find_hub(hass, tgw)
        if thub is None:
            warns.append(f"контроллер цели {tgw} не подключён — его копия привязки ОСТАЛАСЬ, "
                         f"лампы продолжат регулироваться")
            _LOGGER.warning("sensor: контроллер цели %s не найден — delSensorObj НЕ отправлен, "
                            "его копия привязки осталась на шине", tgw)
            continue
        res = await thub.async_request(
            "delSensorObj", "delSensorObjRes", gwSn=sensor_gw, devType=dt, channel=ch,
            address=addr, dpid=dpid, outputObj=tobjs, timeout=8.0)
        _LOGGER.info("sensor [цель %s] delSensorObj dpid%s %s цел. → %s",
                     tgw, dpid, len(tobjs), res)
        if not (res and res.get("ack")):
            warns.append(f"контроллер цели {tgw} не подтвердил снятие")
    return warns


async def _sensor_targets_add(hass, sensor_gw, dt, ch, addr, dpid, run_cond, lux_range,
                              targets, mode=None) -> list[str]:
    """Записать конфигурацию события датчика на КАЖДОМ чужом контроллере — только ЕГО цели."""
    warns: list[str] = []
    for tgw, tobjs in panel_ops.foreign_gateway_targets(targets, sensor_gw).items():
        thub = _find_hub(hass, tgw)
        if thub is None:
            warns.append(f"контроллер цели {tgw} не подключён — его лампы регулироваться НЕ будут")
            _LOGGER.warning("sensor: контроллер цели %s не найден — addSensorObj НЕ отправлен", tgw)
            continue
        data = {"dpid": dpid, "runCondition": run_cond or [], "outputObj": tobjs}
        if lux_range is not None:
            data["luxRange"] = lux_range
        res = await thub.async_request(
            "addSensorObj", "addSensorObjRes", gwSn=sensor_gw, devType=dt, channel=ch,
            address=addr, linkSensor=[], mode=mode or {}, data=data, timeout=10.0)
        _LOGGER.info("sensor [цель %s] addSensorObj dpid%s %s цел. → %s",
                     tgw, dpid, len(tobjs), res)
        if not (res and res.get("ack")):
            warns.append(f"контроллер цели {tgw} не принял привязку — его лампы не отработают")
    return warns


# ── Привязки датчиков (нативные DALI: движение/освещённость → лампа/группа) ────
# Команды: addSensorObj / delSensorObj / readSensor (мануал стр. 45-49). Привязка
# ЖИВЁТ НА ШЛЮЗЕ и работает без HA. Событие датчика (data.dpid) → цели (outObj).
# Реальные события датчика движения (0201) — железо отдаёт ровно 3 (dpid 1,2,3):
# 1 no_motion (переходное, быстро → vacant), 2 motion, 3 vacant.
# Привязывать по делу — к `motion` и `vacant`. ВНИМАНИЕ: цели датчика — поле
# `outputObj` (не `outObj`, как у панелей) — иначе шлюз молча игнорирует (ack:True, пусто).
# ⚠ Имена событий держим В ТОЙ ЖЕ форме, что состояния сущности (v1.2.45) — иначе в карточке
# привязка называлась бы «движение», а состояние показывало `motion`, и это снова две формы.
SENSOR_EVENTS = {                        # devType → {dpid: имя события}
    "0201": {1: "no_motion", 2: "motion", 3: "vacant"},
    "0202": {1: "меньше", 2: "больше", 3: "между", 4: "точное"},
}


@websocket_api.require_admin
@websocket_api.websocket_command({
    vol.Required("type"): "arvid_dali_center/sensor_bindings",
    vol.Required("gw_sn"): str,
    vol.Required("devType"): str,
    vol.Required("channel"): int,
    vol.Required("address"): int,
})
@websocket_api.async_response
async def ws_sensor_bindings(hass, connection, msg):
    """Прочитать привязки датчика (readSensor — одна команда возвращает весь конфиг) и
    разложить по событиям (как страница панели). Источник правды — контроллер."""
    hub = _find_hub(hass, msg["gw_sn"])
    if not hub:
        connection.send_error(msg["id"], "not_found", "шлюз не найден")
        return
    dt = str(msg["devType"])
    rr = await hub.async_request("readSensor", "readSensorRes", devType=dt,
                                 channel=msg["channel"], address=msg["address"], timeout=8.0)
    _LOGGER.info("sensor read ch%s addr%s → %s", msg["channel"], msg["address"], rr)
    ev_map = SENSOR_EVENTS.get(dt, {})
    # цели по dpid из ответа железа (что УЖЕ привязано); поле outputObj (не outObj!)
    bound = {d.get("dpid"): _norm_out(d.get("outputObj", []))
             for d in (rr or {}).get("data", []) or []}
    # ПОЛНЫЙ список событий строим из СТАТИЧЕСКОЙ карты SENSOR_EVENTS, а не из ответа
    # железа: readSensor у нового датчика без привязок отдаёт пустой data → раньше
    # список событий выходил пустым и привязывать было нечего (замкнутый круг).
    events = [{"dpid": dpid, "name": name, "outObj": bound.get(dpid, [])}
              for dpid, name in ev_map.items()]
    # подстраховка: dpid, вернувшийся с железа, но отсутствующий в карте — не терять
    for dpid, out in bound.items():
        if dpid not in ev_map:
            events.append({"dpid": dpid, "name": f"событие {dpid}", "outObj": out})
    mode = (rr or {}).get("mode", {}) or {}
    connection.send_result(msg["id"], {
        "enable": (rr or {}).get("enable"),
        "modeType": mode.get("type", "manual"), "timeValue": mode.get("timeValue", 0),
        "events": events})


@websocket_api.require_admin
@websocket_api.websocket_command({
    vol.Required("type"): "arvid_dali_center/add_sensor_obj",
    vol.Required("gw_sn"): str,
    vol.Required("devType"): str,
    vol.Required("channel"): int,
    vol.Required("address"): int,
    vol.Required("dpid"): int,
    vol.Optional("modeType", default="manual"): str,   # ordinary/auto/manual
    vol.Optional("timeValue", default=0): int,
    vol.Optional("replace", default=False): bool,
    vol.Required("outObj"): list,
})
@websocket_api.async_response
async def ws_add_sensor_obj(hass, connection, msg):
    """Привязать действие к событию датчика (addSensorObj) + верификация (readSensor).
    replace=True: сперва снять текущие цели события (delSensorObj) — детерминированная правка."""
    hub = _find_hub(hass, msg["gw_sn"])
    if not hub:
        connection.send_error(msg["id"], "not_found", "шлюз не найден")
        return
    dt, ch, addr, dpid = str(msg["devType"]), msg["channel"], msg["address"], msg["dpid"]
    # 0) при замене — снять текущие цели этого события (поле outputObj!)
    if msg["replace"]:
        rr = await hub.async_request("readSensor", "readSensorRes", devType=dt, channel=ch,
                                     address=addr, timeout=8.0)
        cur = []
        for d in (rr or {}).get("data", []) or []:
            if d.get("dpid") == dpid:
                cur = d.get("outputObj", []) or []
        if cur:
            dres = await hub.async_request(
                "delSensorObj", "delSensorObjRes", devType=dt, channel=ch, address=addr,
                dpid=dpid, outputObj=[{"gwSnObj": o.get("gwSnObj"), "devType": str(o.get("devType")),
                                       "channel": o.get("channel"), "address": o.get("address")}
                                      for o in cur], timeout=8.0)
            _LOGGER.info("sensor add(replace): delSensorObj dpid%s → %s", dpid, dres)
    # 1) добавить привязку (цели — outputObj, иначе шлюз игнорирует молча)
    ares = await hub.async_request(
        "addSensorObj", "addSensorObjRes", devType=dt, channel=ch, address=addr,
        mode={"timeValue": msg["timeValue"], "type": msg["modeType"]},
        data={"dpid": dpid, "outputObj": msg["outObj"]}, timeout=8.0)
    _LOGGER.info("sensor addSensorObj ch%s addr%s dpid%s mode%s out=%s → %s",
                 ch, addr, dpid, msg["modeType"], msg["outObj"], ares)
    ok = bool(ares and ares.get("ack"))
    verify, cell_out = None, []
    if ok:
        rr2 = await hub.async_request("readSensor", "readSensorRes", devType=dt, channel=ch,
                                      address=addr, timeout=8.0)
        _LOGGER.debug("sensor add verify readSensor dpid%s → %s", dpid, rr2)
        actual = []
        for d in (rr2 or {}).get("data", []) or []:
            if d.get("dpid") == dpid:
                actual = d.get("outputObj", []) or []
        cell_out = _norm_out(actual)
        # ключ сверки — с gwSnObj (v1.2.38): у датчика цель обычно своя, но одинаковые
        # адреса на разных шлюзах без шлюза в ключе давали ложное «совпало»
        req_set = _panel_target_set(msg["outObj"], msg["gw_sn"])
        act_set = _panel_target_set(actual, msg["gw_sn"])
        verify = {"match": req_set.issubset(act_set),
                  "missing": sorted(str(t) for t in (req_set - act_set))}
        if not verify["match"]:
            _LOGGER.warning("sensor add: цель НЕ привязалась dpid%s missing=%s",
                            dpid, verify["missing"])
        el = get_eventlog(hass)
        if el:
            ev = SENSOR_EVENTS.get(dt, {}).get(dpid, dpid)
            el.log(hub.gw_sn, "sensor", f"привязка датчика: «{ev}» → {len(actual)} цел."
                   f"{'' if verify['match'] else ' ⚠ не совпало'}")
    connection.send_result(msg["id"], {"ok": ok, "verify": verify,
                                        "dpid": dpid, "outObj": cell_out, "res": ares})


@websocket_api.require_admin
@websocket_api.websocket_command({
    vol.Required("type"): "arvid_dali_center/del_sensor_obj",
    vol.Required("gw_sn"): str,
    vol.Required("devType"): str,
    vol.Required("channel"): int,
    vol.Required("address"): int,
    vol.Required("dpid"): int,
    vol.Required("outObj"): list,
})
@websocket_api.async_response
async def ws_del_sensor_obj(hass, connection, msg):
    """Снять цель привязки события датчика (delSensorObj) + верификация (readSensor)."""
    hub = _find_hub(hass, msg["gw_sn"])
    if not hub:
        connection.send_error(msg["id"], "not_found", "шлюз не найден")
        return
    dt, ch, addr, dpid = str(msg["devType"]), msg["channel"], msg["address"], msg["dpid"]
    # цель на ЧУЖОМ шлюзе держит СВОЮ копию — снимаем и там, иначе лампы продолжат
    # регулироваться при пустом диалоге (симптом с объекта 2026-08-05)
    warns = await _sensor_targets_del(hass, hub.gw_sn, dt, ch, addr, msg["dpid"], msg["outObj"])
    dres = await hub.async_request("delSensorObj", "delSensorObjRes", devType=dt, channel=ch,
                                   address=addr, dpid=dpid, outputObj=msg["outObj"], timeout=8.0)
    _LOGGER.info("sensor delSensorObj dpid%s out=%s → %s", dpid, msg["outObj"], dres)
    rr = await hub.async_request("readSensor", "readSensorRes", devType=dt, channel=ch,
                                 address=addr, timeout=8.0)
    _LOGGER.debug("sensor del verify readSensor dpid%s → %s", dpid, rr)
    actual = []
    for d in (rr or {}).get("data", []) or []:
        if d.get("dpid") == dpid:
            actual = d.get("outputObj", []) or []
    drop = _panel_target_set(msg["outObj"], msg["gw_sn"])
    gone = not (drop & _panel_target_set(actual, msg["gw_sn"]))
    el = get_eventlog(hass)
    if el:
        ev = SENSOR_EVENTS.get(dt, {}).get(dpid, dpid)
        el.log(hub.gw_sn, "sensor", f"снятие привязки датчика: «{ev}»"
               f"{'' if gone else ' ⚠ осталась'}")
    connection.send_result(msg["id"], {"warnings": warns, "ok": bool(dres and dres.get("ack")),
                                        "gone": gone, "dpid": dpid,
                                        "outObj": _norm_out(actual), "res": dres})


# ── Автояркость / 恒照 (Путь A — нативный замкнутый контур шлюза) ───────────────
# КАРКАС для hardware-теста (как H1c): addSensorObj на датчике 0202 с luxRange →
# группа; шлюз сам держит освещённость. Структура — best-guess, тест уточнит:
#   data = { dpid, luxRange:[target,tol], outputObj:[{devType:"0401"=группа, property:[вкл]}] }
# ⚠ Открытые вопросы (см. docs/PLAN_SENSOR_BINDINGS §H3b-B3): держит ли контур замкнуто;
#   семантика luxRange ([цель,допуск] vs [min,max]); нужен ли dpid (0202: 1<,2>,3между,4точно).

def _lux_read(rr):
    """Вытащить luxRange/цели/РАСПИСАНИЕ из ответа readSensor (для показа/сверки).

    v1.2.25: добавлены `windows` (условие времени `runCondition` devType 0701 — карточка
    показывает и правит окна работы) и `mode` (шлюз отдаёт {"timeValue":-1,"type":"ordinary"},
    подтверждено захватом) — карточке нужно предзаполнять селектор режима."""
    out = []
    for d in (rr or {}).get("data", []) or []:
        wins = [c.get("value") for c in (d.get("runCondition") or [])
                if str(c.get("devType")) == "0701"]
        out.append({"dpid": d.get("dpid"), "luxRange": d.get("luxRange"),
                    "outputObj": _norm_out(d.get("outputObj", []) or []),
                    "windows": wins[0] if wins else []})
    return out


@websocket_api.require_admin
@websocket_api.websocket_command({
    vol.Required("type"): "arvid_dali_center/set_lux_keep",
    vol.Required("gw_sn"): str,
    vol.Required("devType"): str,            # датчик освещённости (0202)
    vol.Required("channel"): int,
    vol.Required("address"): int,
    vol.Required("group"): dict,             # {channel, groupId} — целевая группа
    # КРОСС-ГРУППА как цель (v1.2.53): её копии живут на каждом участнике, поэтому цель —
    # НЕ одна, а по одной на шлюз. Форма подтверждена захватом трёх шлюзов 2026-08-07.
    vol.Optional("xgroup_uid", default=""): str,
    vol.Required("target"): int,             # целевая освещённость (lux)
    vol.Optional("tol", default=10): int,    # порог (Threshold) → luxRange = target ± tol
    vol.Optional("dpid", default=3): int,    # 恒照 = dpid 3 (диапазон), подтверждено captured DALI Center
    # v1.2.23 (ТЕСТОВОЕ): режим сосуществования с ручным управлением (мануал стр. 46).
    # "" = как раньше (шлём mode={} — шлюз применит свой дефолт `ordinary`, подтверждено
    # захватом readSensorRes: {"timeValue":-1,"type":"ordinary"}).
    vol.Optional("modeType", default=""): vol.In(["", "ordinary", "auto", "manual"]),
    vol.Optional("timeValue", default=-1): int,   # для auto: через сколько секунд датчик вернёт себе контроль
})
@websocket_api.async_response
async def ws_set_lux_keep(hass, connection, msg):
    """Нативная автояркость (恒照) — структура подтверждена захватом DALI Center (Wireshark):
    addSensorObj на датчике 0202, data.dpid=3, luxRange=[target-tol, target+tol] (min,max),
    outputObj = ОТДЕЛЬНЫЕ ЛАМПЫ группы (devType 0101) с ПУСТЫМ property (=«Auto brightness»),
    mode={}. Группа разворачивается в лампы через readGroup. Перед записью — delSensor."""
    hub = _find_hub(hass, msg["gw_sn"])
    if not hub:
        connection.send_error(msg["id"], "not_found", "шлюз не найден")
        return
    dt, ch, addr = str(msg["devType"]), msg["channel"], msg["address"]
    grp = msg["group"]
    # ЦЕЛЬ — САМА ГРУППА (devType 0401, address = groupId), property пустой = «Auto brightness».
    # Подтверждено захватом DALI Center 2026-07-31. Разворот в лампы (как было до v1.2.37) убран:
    # состав разворачивает сам контроллер в момент работы, поэтому привязка НЕ протухает при
    # изменении состава группы, не требует `readGroup` и не расходится с обычным управлением
    # группой (оба идут групповой адресацией).
    out_obj = [{"gwSnObj": hub.gw_sn, "devType": "0401",
                "channel": grp["channel"], "address": grp["groupId"], "property": []}]
    # ── ЦЕЛЬ-КРОСС-ГРУППА (v1.2.53) ──────────────────────────────────────────────
    # Эталон — синхронный захват ТРЁХ шлюзов 2026-08-07: DALI Center кладёт в outputObj по
    # ОДНОЙ цели `0401` на КАЖДОГО участника (`gwSnObj` = участник, address = groupId), а
    # затем рассылает конфигурацию на все шлюзы: чужим — только их цель, шлюзу датчика —
    # полный состав. Второе делает `_sensor_targets_add` (он уже так умеет), первое — здесь.
    if msg["xgroup_uid"]:
        from .store import get_cross_group_store
        xgs = get_cross_group_store(hass)
        xg = xgs.get(msg["xgroup_uid"]) if xgs else None
        if not xg:
            connection.send_error(msg["id"], "not_found", "кросс-группа не найдена")
            return
        out_obj = [{"gwSnObj": part, "devType": "0401", "channel": xg["channel"],
                    "address": xg["groupId"], "property": []}
                   for part in xg.get("participants") or []]
        if not out_obj:
            connection.send_error(msg["id"], "bad_request", "у кросс-группы нет участников")
            return
        _LOGGER.info("автояркость на КРОСС-ГРУППУ %s: целей %s (по одной на участника)",
                     msg["xgroup_uid"], len(out_obj))
    target, tol = msg["target"], msg["tol"]
    lux_range = [max(0, target - tol), target + tol]   # [min, max], как у DALI Center
    # ⚠ ПРЕЖНЮЮ конфигурацию чистим НА ОБЕИХ сторонах (v1.2.41): если раньше цель была на
    # чужом шлюзе, его копия осталась бы жить и продолжала регулировать лампы. Читаем
    # текущий состав — иначе не знаем, кому слать снятие.
    rr0 = await hub.async_request("readSensor", "readSensorRes", devType=dt, channel=ch,
                                  address=addr, timeout=8.0)
    prev = []
    for entry in ((rr0 or {}).get("data") or []):
        if entry.get("dpid") == msg["dpid"]:
            prev = entry.get("outputObj") or []
    warns = await _sensor_targets_del(hass, hub.gw_sn, dt, ch, addr, msg["dpid"], prev)
    # чистим прежнюю конфигурацию датчика (без накопления)
    await hub.async_request("delSensor", "delSensorRes", devType=dt, channel=ch,
                            address=addr, timeout=8.0)
    # mode: пусто = прежнее поведение (дефолт шлюза `ordinary` = «не подвержен стороннему
    # управлению» → ручная команда лампой мгновенно перебивается контуром). `auto` + timeValue
    # отдаёт человеку приоритет на N секунд, `manual` — до следующего выключения света.
    # ⚠ ТЕСТОВОЕ: DALI Center это поле НЕ выставляет (шлёт mode={}), поведение проверяем сами.
    mode = ({"type": msg["modeType"], "timeValue": msg["timeValue"]}
            if msg["modeType"] else {})
    # цели на ЧУЖИХ шлюзах — им нужна СВОЯ копия (иначе их лампы не отработают, как было
    # у панелей до v1.2.39). Цель идёт ПЕРВОЙ, как в захвате.
    warns += await _sensor_targets_add(hass, hub.gw_sn, dt, ch, addr, msg["dpid"],
                                       [], lux_range, out_obj, mode)
    ares = await hub.async_request(
        "addSensorObj", "addSensorObjRes", devType=dt, channel=ch, address=addr,
        linkSensor=[], mode=mode,
        data={"dpid": msg["dpid"], "runCondition": [], "luxRange": lux_range,
              "outputObj": out_obj}, timeout=10.0)
    _LOGGER.info("luxKeep set ch%s addr%s lux%s → ГРУППА ch%s id%s mode%s → %s",
                 ch, addr, lux_range, grp.get("channel"), grp.get("groupId"),
                 mode or "(дефолт)", ares)
    ok = bool(ares and ares.get("ack"))
    verify = None
    if ok:
        rr = await hub.async_request("readSensor", "readSensorRes", devType=dt, channel=ch,
                                     address=addr, timeout=8.0)
        _LOGGER.info("luxKeep verify readSensor → %s", rr)
        verify = _lux_read(rr)
        el = get_eventlog(hass)
        if el:
            el.log(hub.gw_sn, "sensor",
                   f"автояркость: 0202 ch{ch}/{addr} → группа {grp.get('groupId')} "
                   f"lux {lux_range}")
    connection.send_result(msg["id"], {"ok": ok, "verify": verify, "res": ares,
                                       "warnings": warns,
                                       "reason": None if ok else _res_reason(ares)})


@websocket_api.require_admin
@websocket_api.websocket_command({
    vol.Required("type"): "arvid_dali_center/read_lux_keep",
    vol.Required("gw_sn"): str,
    vol.Required("devType"): str,
    vol.Required("channel"): int,
    vol.Required("address"): int,
})
@websocket_api.async_response
async def ws_read_lux_keep(hass, connection, msg):
    """Прочитать текущую конфигурацию автояркости датчика (readSensor → luxRange/цели)."""
    hub = _find_hub(hass, msg["gw_sn"])
    if not hub:
        connection.send_error(msg["id"], "not_found", "шлюз не найден")
        return
    rr = await hub.async_request("readSensor", "readSensorRes", devType=str(msg["devType"]),
                                 channel=msg["channel"], address=msg["address"], timeout=8.0)
    connection.send_result(msg["id"], {
        "entries": _lux_read(rr),
        # enable — мягкое вкл/выкл функции (setSensorOnOff), НЕ наличие привязки;
        # mode — режим сосуществования с ручным управлением (ordinary/auto/manual)
        "enable": (rr or {}).get("enable"),
        "mode": (rr or {}).get("mode") or {},
    })


@websocket_api.require_admin
@websocket_api.websocket_command({
    vol.Required("type"): "arvid_dali_center/sync_gw_time",
    vol.Required("gw_sn"): str,
})
@websocket_api.async_response
async def ws_sync_gw_time(hass, connection, msg):
    """Синхронизировать часы шлюза с временем HA (`updateTimeZone`, мануал стр. 8) — v1.2.26.

    ЗАЧЕМ. Окна расписания датчиков (`runCondition` 0701) исполняет САМ ШЛЮЗ по СВОИМ часам:
    сбитые часы = свет не вовремя, и снаружи это невидимо. Читать их мы начали в v1.2.23, теперь
    даём ЧЕЛОВЕКУ кнопку поправить.

    ⚠ ПОЧЕМУ ТОЛЬКО ПО КНОПКЕ, а не автоматом при коннекте: часами шлюза пользуется и настольный
    DALI Center, это запись в чужое общее устройство — принцип «без авто-деструктива».

    ⚠ ПОЯС НЕ МЕНЯЕМ — отправляем тот, что шлюз сам вернул (`getTimeZone`). Нотация пояса у
    Sunricher POSIX-инвертированная ("CST-8"/"UTC-08:00" = UTC+8, мануал стр. 8), ошибка в ней
    увела бы расписание на часы. Для расписаний важно ЛОКАЛЬНОЕ время — его и выставляем."""
    from datetime import datetime
    hub = _find_hub(hass, msg["gw_sn"])
    if not hub:
        connection.send_error(msg["id"], "not_found", "шлюз не найден")
        return
    tz = getattr(hub, "gw_timezone", "") or ""
    if not tz:                       # пояс неизвестен → сперва прочитать (не выдумываем)
        rr = await hub.async_request("getTimeZone", "getTimeZoneRes", timeout=5.0)
        tz = str((rr or {}).get("timezone") or "")
    if not tz:
        connection.send_error(msg["id"], "no_timezone",
                              "шлюз не отдал часовой пояс — синхронизация отменена "
                              "(свой пояс не выдумываем: ошибка увела бы расписание на часы)")
        return
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    res = await hub.async_request("updateTimeZone", "updateTimeZoneRes",
                                  time=now, timezone=tz, timeout=8.0)
    ok = bool(res and (res.get("ack") is not False))
    _LOGGER.info("шлюз %s: синхронизация часов → %s (пояс %s, оставлен как был) → %s",
                 hub.gw_sn, now, tz, res)
    skew = None
    if ok:
        await hass.async_add_executor_job(hub._read_gw_time)   # noqa: SLF001 — перечитать факт
        skew = getattr(hub, "gw_time_skew_s", None)
    el = get_eventlog(hass)
    if el:
        el.log(hub.gw_sn, "conn", f"часы шлюза синхронизированы с HA ({now})"
               if ok else "синхронизация часов НЕ подтверждена")
    connection.send_result(msg["id"], {"ok": ok, "gwTime": getattr(hub, "gw_time", ""),
                                       "gwTimezone": tz, "gwTimeSkewS": skew,
                                       "reason": None if ok else _res_reason(res)})


@websocket_api.require_admin
@websocket_api.websocket_command({
    vol.Required("type"): "arvid_dali_center/set_sensor_enabled",
    vol.Required("gw_sn"): str,
    vol.Required("devType"): str,
    vol.Required("channel"): int,
    vol.Required("address"): int,
    vol.Required("value"): bool,
})
@websocket_api.async_response
async def ws_set_sensor_enabled(hass, connection, msg):
    """Тумблер функции датчика для КАРТОЧКИ (v1.2.25) — мягкое вкл/выкл (`setSensorOnOff`).

    ⚠ НЕ путать с `clear_lux_keep` (`delSensor`): тот СНОСИТ настройку («Очистить»), а этот
    только приостанавливает — привязка цела, возврат мгновенный, шину не грузим. Одна
    реализация с сервисом `set_autobrightness` (sensor_ops) — чтобы пути не разъехались."""
    hub = _find_hub(hass, msg["gw_sn"])
    if not hub:
        connection.send_error(msg["id"], "not_found", "шлюз не найден")
        return
    from .coordinator import dev_state_key
    from .sensor_ops import async_set_sensor_enabled
    key = dev_state_key(str(msg["devType"]), msg["channel"], msg["address"])
    dev = hub.devices.get(key) or {"devType": str(msg["devType"]), "channel": msg["channel"],
                                   "address": msg["address"]}
    r = await async_set_sensor_enabled(hub, dev, msg["value"])
    el = get_eventlog(hass)
    if el:
        el.log(hub.gw_sn, "sensor",
               f"датчик {msg['devType']} addr{msg['address']}: "
               f"{'включён' if msg['value'] else 'выключен (настройка сохранена)'}")
    connection.send_result(msg["id"], {"ok": r["ok"], "value": msg["value"],
                                       "reason": None if r["ok"] else _res_reason(r.get("res"))})


@websocket_api.require_admin
@websocket_api.websocket_command({
    vol.Required("type"): "arvid_dali_center/set_sensor_schedule",
    vol.Required("gw_sn"): str,
    vol.Required("devType"): str,
    vol.Required("channel"): int,
    vol.Required("address"): int,
    vol.Required("dpid"): int,              # 3 = автояркость (0202), 2 = движение (0201)
    vol.Required("windows"): [str],         # ["08:00-17:30", ...]; [] = снять расписание
})
@websocket_api.async_response
async def ws_set_sensor_schedule(hass, connection, msg):
    """Окна работы датчика для КАРТОЧКИ (v1.2.25). Валидация — общая с сервисом
    (`schedule_util`): формат, только ВНУТРИ ДНЯ (через полночь нельзя), без вырожденных."""
    hub = _find_hub(hass, msg["gw_sn"])
    if not hub:
        connection.send_error(msg["id"], "not_found", "шлюз не найден")
        return
    from .coordinator import dev_state_key
    from .schedule_util import WindowError, normalize_windows, windows_overlap
    from .sensor_ops import async_set_schedule
    try:
        windows = normalize_windows(msg["windows"])
    except WindowError as err:
        connection.send_error(msg["id"], "bad_window", str(err))
        return
    key = dev_state_key(str(msg["devType"]), msg["channel"], msg["address"])
    dev = hub.devices.get(key)
    if not dev:
        connection.send_error(msg["id"], "not_found", "устройство не найдено")
        return
    r = await async_set_schedule(hub, dev, msg["dpid"], windows)
    warnings = [f"окна пересекаются: {a} и {b}" for a, b in windows_overlap(windows)]
    skew = getattr(hub, "gw_time_skew_s", None)
    if skew is not None and abs(skew) > 60:
        warnings.append(f"часы шлюза расходятся с HA на {skew:+.0f} с — "
                        f"расписание сработает не вовремя")
    el = get_eventlog(hass)
    if el and r.get("ok"):
        el.log(hub.gw_sn, "sensor",
               f"расписание {msg['devType']} addr{msg['address']}: "
               f"{', '.join(windows) if windows else 'снято (круглосуточно)'}")
    connection.send_result(msg["id"], {"ok": r["ok"], "verify": r.get("verify"),
                                       "error": r.get("error"), "warnings": warnings})


@websocket_api.require_admin
@websocket_api.websocket_command({
    vol.Required("type"): "arvid_dali_center/clear_lux_keep",
    vol.Required("gw_sn"): str,
    vol.Required("devType"): str,
    vol.Required("channel"): int,
    vol.Required("address"): int,
})
@websocket_api.async_response
async def ws_clear_lux_keep(hass, connection, msg):
    """Выключить автояркость: снять конфигурацию на ВСЕХ задействованных контроллерах.

    🔴 СИМПТОМ С ОБЪЕКТА (2026-08-05), который это чинит: «Очистить» отрабатывало, диалог
    пустел — а лампы продолжали регулироваться, и DALI Center показывал привязку живой.
    Причина: мы слали `delSensor` ТОЛЬКО на шлюз датчика, а у кросс-шлюзовой автояркости
    КАЖДЫЙ шлюз цели держит СВОЮ копию (захват двух шлюзов 2026-08-05, docs/CROSS_GATEWAY §3).
    Своя память очищалась, физика — нет (закон 2).

    Порядок как в захвате DALI Center: сперва цели (`delSensorObj` с их долей состава),
    затем свой шлюз. Состав берём `readSensor` ДО снятия — иначе не знаем, кому слать."""
    hub = _find_hub(hass, msg["gw_sn"])
    if not hub:
        connection.send_error(msg["id"], "not_found", "шлюз не найден")
        return
    dt, ch, addr = str(msg["devType"]), msg["channel"], msg["address"]
    rr0 = await hub.async_request("readSensor", "readSensorRes", devType=dt, channel=ch,
                                  address=addr, timeout=8.0)
    warns: list[str] = []
    for entry in ((rr0 or {}).get("data") or []):          # снимаем по КАЖДОЙ функции датчика
        targets = entry.get("outputObj") or []
        if targets:
            warns += await _sensor_targets_del(hass, hub.gw_sn, dt, ch, addr,
                                               entry.get("dpid"), targets)
    dres = await hub.async_request("delSensor", "delSensorRes", devType=dt, channel=ch,
                                   address=addr, timeout=8.0)
    _LOGGER.info("luxKeep clear ch%s addr%s → %s%s", ch, addr, dres,
                 f" ⚠ {warns}" if warns else "")
    el = get_eventlog(hass)
    if el:
        el.log(hub.gw_sn, "sensor", f"автояркость снята: 0202 ch{ch}/{addr}"
               f"{'' if not warns else ' ⚠ ' + '; '.join(warns)}")
    connection.send_result(msg["id"], {"ok": bool(dres and dres.get("ack")),
                                       "warnings": warns, "res": dres})


@websocket_api.websocket_command({
    vol.Required("type"): "arvid_dali_center/events",
    vol.Optional("gw_sn"): str,
    vol.Optional("limit", default=1000): int,
})
@callback
def ws_events(hass, connection, msg):
    """Снимок журнала (последние N событий, опц. по шлюзу)."""
    el = get_eventlog(hass)
    items = el.recent(msg.get("gw_sn"), msg.get("limit", 1000)) if el else []
    connection.send_result(msg["id"], {"events": items})


@websocket_api.websocket_command({
    vol.Required("type"): "arvid_dali_center/events_subscribe",
    vol.Optional("gw_sn"): str,
})
@callback
def ws_events_subscribe(hass, connection, msg):
    """Живая подписка на журнал: каждое новое событие → клиенту (для панели «Журнал»)."""
    gw = msg.get("gw_sn")

    @callback
    def _forward(rec):
        # системные записи (gw == "") показываем всем
        if not gw or rec.get("gw") == gw or rec.get("gw") == "":
            connection.send_message(websocket_api.event_message(msg["id"], {"event": rec}))

    connection.subscriptions[msg["id"]] = async_dispatcher_connect(
        hass, SIGNAL_EVENTLOG, _forward)
    connection.send_result(msg["id"])
