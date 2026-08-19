"""Хранилище последних заданных параметров ламп (HA Store).

getDevParam для ламп на железе часто возвращает пусто, поэтому запоминаем то, что
сами записали (setDevParam), и показываем эти значения в карточке. Ключ —
gwSn:devType:channel:address.
"""

from __future__ import annotations

import logging

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .identity import DEFAULT_MODE, is_addr_key, normalize_mode

from .transport.decode import is_valid_devsn

_LOGGER = logging.getLogger(__name__)

_STORE_KEY = "arvid_dali_center_params"
_HASS_KEY = "arvid_dali_center_param_store"
_NAME_STORE_KEY = "arvid_dali_center_names"
_NAME_HASS_KEY = "arvid_dali_center_name_store"
_GROUP_STORE_KEY = "arvid_dali_center_groups"
# кросс-шлюзовые группы — ОТДЕЛЬНЫЙ файл: модель другая (не принадлежит шлюзу), смешивать
# с однолшлюзовыми в одном сторе значило бы мигрировать рабочие записи ради стройности
_XGROUP_STORE_KEY = "arvid_dali_center_cross_groups"
_GROUP_HASS_KEY = "arvid_dali_center_group_store"
_XGROUP_HASS_KEY = "arvid_dali_center_cross_group_store"
_DEVICE_STORE_KEY = "arvid_dali_center_devices"
_DEVICE_HASS_KEY = "arvid_dali_center_device_store"
_ROTARY_STORE_KEY = "arvid_dali_center_rotary"
_ROTARY_HASS_KEY = "arvid_dali_center_rotary_store"
_PANELACT_STORE_KEY = "arvid_dali_center_panel_acts"
_PANELACT_HASS_KEY = "arvid_dali_center_panel_act_store"
_GROUPPARAM_STORE_KEY = "arvid_dali_center_group_params"
_GROUPPARAM_HASS_KEY = "arvid_dali_center_group_param_store"
_SENSORPREF_STORE_KEY = "arvid_dali_center_sensor_prefs"
_SENSORPREF_HASS_KEY = "arvid_dali_center_sensor_pref_store"
_SENSOROBJ_STORE_KEY = "arvid_dali_center_sensor_objs"
_SENSOROBJ_HASS_KEY = "arvid_dali_center_sensor_obj_store"
_ALL_STORES_HASS_KEY = "arvid_dali_center_all_stores"   # реестр для чисток (S5)
# Режим идентичности — НАСТРОЙКА ОБЪЕКТА, а не данные устройства: один режим на всю установку
# (решение 2026-08-19). Поэтому отдельный файл и НЕ `PurgeableStore` — чистки его не трогают,
# иначе «Стереть данные» молча возвращала бы объект в штатный режим.
_MODE_STORE_KEY = "arvid_dali_center_identity_mode"
_MODE_HASS_KEY = "arvid_dali_center_identity_mode_store"

# Отложенная запись config-сторов: частые правки (особенно bulk — параметры/имена сотням
# ламп) коалесятся в ОДНУ запись через задержку (был full-file rewrite на каждое изменение
# → write-amplification на масштабе 4400). Данные в памяти обновляются сразу (чтение
# консистентно), на диск — отложенно; HA флашит delay_save на остановке. Удаления/санитары
# (async_remove/sanitize/claim) НЕ через delay — пишутся немедленно (редкие, важна сразу-
# персистентность). Накопитель энергии — свой delay 30с (см. energy/store.py).
_SAVE_DELAY = 2.0


class PurgeableStore:
    """Базовый класс хранилища: КАЖДОЕ умеет убирать за собой (S5, v1.2.51).

    Зачем. Раньше три операции чистки («Забыть», «Стереть данные», удаление шлюза) были
    РУЧНЫМИ СПИСКАМИ вызовов, и списки не совпадали. Завели новый стор — забыли дописать его
    в списки; ошибка не видна (код работает, тесты зелёные), а мусор копится молча. Так
    появились S1–S4 в docs/DEBT.md §S: `SensorObjStore` не чистился НИГДЕ, `SensorPrefStore`
    не чистился в «Забыть», кросс-группы переживали удаление шлюза.

    Теперь операции чистки — ОБХОД реестра: «скажи каждому хранилищу убрать про это
    устройство / этот шлюз». Забыть подключить новый стор больше нельзя: без этих двух
    методов он не пройдёт тест-сторож (tests/test_stores.py).

    ⚠ Если чистка бессмысленна (например, стор шлюзовой и про устройства ничего не знает) —
    метод всё равно ОБЯЗАН быть и вернуть 0, а в докстроке написано ПОЧЕМУ. «Забыли
    подключить» и «решили не чистить» снаружи выглядят одинаково, а последствия разные.
    """

    #: человекочитаемое имя для отчётов чистки (заполняется наследником)
    purge_name: str = "?"

    async def purge_identity(self, identity: str) -> int:
        """Убрать всё, что хранится про УСТРОЙСТВО, по его КЛЮЧУ ИДЕНТИЧНОСТИ.

        ⚠ Имя параметра — `identity`, а не `devsn`, и это не косметика: в штатном режиме ключ
        и правда серийник, но в адресном (docs/ADDRESS_IDENTITY.md) это координата на шине.
        Название, которое врёт о природе ключа, — та самая ловушка, из-за которой чистки уже
        расходились со сторами (DEBT §S)."""
        raise NotImplementedError

    async def purge_gateway(self, gw_sn: str) -> int:
        """Убрать всё, что хранится про ШЛЮЗ."""
        raise NotImplementedError

    async def _purge_keys(self, keys: list[str]) -> int:
        """Хелпер: снять список ключей верхнего уровня и записать немедленно."""
        for k in keys:
            self._data.pop(k, None)
        if keys:
            await self._store.async_save(self._data)
        return len(keys)


def param_key(gw_sn: str, dev_type: str, channel, address) -> str:
    """⚠ ЛЕГАСИ (v1.2.51): адресный ключ параметров. Новые записи так НЕ ключуются —
    идентичность устройства это `devSn` и только он (решение пользователя 2026-08-07:
    фолбэки под устройства без серийника не городим — они копят мусор). Функция оставлена
    ради миграции старых записей (`async_migrate_to_devsn`) и будет удалена, когда легаси
    выветрится."""
    return f"{gw_sn}:{dev_type}:{channel}:{address}"


def group_name_key(gw_sn: str, channel, group_id) -> str:
    """Ключ пользовательского имени DALI-группы (у группы нет devSn)."""
    return f"{gw_sn}:group:{channel}:{group_id}"


def name_key(gw_sn: str, dev_type: str, channel, address, dev_sn=None) -> str | None:
    """Ключ имени устройства — ТОЛЬКО `devSn`. Без серийника имя не храним (v1.2.51).

    Раньше здесь был адресный фолбэк, и он вышел боком: пока скан терял `devSn` (v1.2.50),
    имена ламп оседали под адресными ключами, «Стереть данные» их не видело (оно ходит по
    серийникам), и старое имя воскресало на новом светильнике, занявшем тот же адрес —
    ровно `l_2_2_2` с объекта. Решение пользователя 2026-08-07: **фолбэки под устройства без
    серийника не городим**. Устройство без `devSn` остаётся видимым и управляемым, но
    персистентных данных за ним не хранится — тогда и мусору неоткуда взяться.

    `None` = «ключа нет» → вызывающий НЕ пишет и НЕ читает имя (см. `has_custom_name`).
    Датчики/панели (02xx/03xx) делят один ключ (движение+люкс = одно устройство) — это не
    фолбэк, а их физическая природа: у пары общий серийник.
    """
    return str(dev_sn) if is_valid_devsn(dev_sn) else None


def legacy_name_key(gw_sn: str, dev_type: str, channel, address) -> str:
    """АДРЕСНЫЙ ключ имени — только для работы со СТАРЫМИ записями (v1.2.51).

    Новые имена так не ключуются (см. `name_key`). Нужен двум местам: миграции имён на
    серийник при скане и чистке легаси в `NameStore.purge_gateway`. Отдельная функция —
    чтобы адресный ключ нельзя было применить случайно: у него теперь одно назначение."""
    t = str(dev_type)
    if t.startswith("02") or t.startswith("03"):
        return f"{gw_sn}:{channel}:{address}"
    return f"{gw_sn}:{t}:{channel}:{address}"


class ParamStore(PurgeableStore):
    """Персист заданных параметров ламп."""

    purge_name = "параметры устройств"

    async def purge_identity(self, identity: str) -> int:
        """Ключ — devSn; попутно снимаем ЛЕГАСИ адресные ключи этого же устройства нельзя
        (адрес тут не знаем) — их снимает `purge_gateway`."""
        return await self._purge_keys([identity] if identity in self._data else [])

    async def purge_gateway(self, gw_sn: str) -> int:
        """Легаси адресные ключи вида `<gw>:<devType>:<ch>:<addr>` — их не покрывает
        `purge_identity` (devSn там неизвестен). Записи на devSn шлюзу не принадлежат."""
        return await self._purge_keys([k for k in self._data
                                       if str(k).startswith(f"{gw_sn}:")])

    def __init__(self, hass: HomeAssistant) -> None:
        self._store = Store(hass, 1, _STORE_KEY)
        self._data: dict[str, dict] = {}

    async def async_load(self) -> None:
        self._data = await self._store.async_load() or {}

    def get(self, key: str) -> dict:
        return dict(self._data.get(key, {}))

    def has_legacy_keys(self) -> bool:
        """Есть ли ещё АДРЕСНЫЕ ключи (`gwSn:devType:ch:addr`) под миграцию на devSn?
        devSn-ключ идёт без ':'. Дёшево (без прохода-мутации) — гейт, чтобы не гонять
        миграцию на каждом старте, когда всё уже переехало на devSn."""
        return any(":" in k for k in self._data)

    async def async_update(self, key: str, paramer: dict) -> None:
        merged = {**self._data.get(key, {}), **paramer}
        self._data[key] = merged
        # delay_save: серия bulk-обновлений (set_param_bulk в цикле) → одна запись
        self._store.async_delay_save(lambda: self._data, _SAVE_DELAY)

    async def async_remove(self, key: str) -> None:
        """Удалить параметры устройства (ручное «Забыть» — единственная точка подрезки)."""
        if self._data.pop(key, None) is not None:
            await self._store.async_save(self._data)

    async def async_migrate_to_devsn(self, addr_map: dict[str, str]) -> tuple[int, list[str]]:
        """Миграция ключей параметров со старого адресного формата `gwSn:devType:ch:addr`
        на стабильный `devSn` (device-level). `addr_map`: старый_ключ → devSn (строится из
        DeviceStore). Идемпотентно: ключ без ':' уже на devSn — пропускаем. Неразрешённые
        (устройство не в кеше — напр. оффлайн-шлюз) ОСТАВЛЯЕМ как есть + лог (принцип: не
        выкидывать молча; следующий старт добьёт, когда устройство появится)."""
        resolved, orphans, changed = 0, [], False
        for key in list(self._data.keys()):
            if ":" not in key:                       # уже devSn-ключ
                continue
            sn = addr_map.get(key)
            if sn:                                   # слить в devSn-ключ, старый убрать
                self._data[sn] = {**self._data.get(sn, {}), **self._data.pop(key)}
                resolved += 1
                changed = True
            else:
                orphans.append(key)
        if changed:
            # стартовая запись: сбой диска (read-only/полон через годы) НЕ должен ронять
            # setup всей интеграции — данные в памяти консистентны, запись подхватится при
            # следующем изменении/отложенном сохранении
            try:
                await self._store.async_save(self._data)
            except Exception as err:  # noqa: BLE001
                _LOGGER.warning("ParamStore: миграция в памяти ок, запись не удалась: %s", err)
        if resolved or orphans:
            _LOGGER.info("ParamStore: мигрировано %d ключей на devSn; сирот %d %s",
                         resolved, len(orphans), orphans or "")
        return resolved, orphans


class NameStore(PurgeableStore):
    """Персист пользовательских имён устройств (ключ — name_key = devSn)."""

    purge_name = "имена"

    async def purge_identity(self, identity: str) -> int:
        return await self._purge_keys([identity] if identity in self._data else [])

    async def purge_gateway(self, gw_sn: str) -> int:
        """Имена ГРУПП (`<gw>:group:<ch>:<id>`) и ЛЕГАСИ адресные ключи устройств
        (`<gw>:…`, писались до v1.2.51, пока был фолбэк). Именно из-за них «Стереть данные»
        не убирало имя `l_2_2_2` — оно ходило только по серийникам (DEBT §S, S1)."""
        return await self._purge_keys([k for k in self._data
                                       if str(k).startswith(f"{gw_sn}:")])

    def __init__(self, hass: HomeAssistant) -> None:
        self._store = Store(hass, 1, _NAME_STORE_KEY)
        self._data: dict[str, str] = {}

    async def async_load(self) -> None:
        self._data = await self._store.async_load() or {}

    def get(self, key: str) -> str:
        return self._data.get(key, "")

    async def async_set(self, key: str, name: str) -> None:
        if name:
            self._data[key] = name
        else:
            self._data.pop(key, None)
        # delay_save: bulk-переименование коалесится в одну запись
        self._store.async_delay_save(lambda: self._data, _SAVE_DELAY)


class GroupStore(PurgeableStore):
    """Персист DALI-групп (channel/groupId/name/members) — источник правды в HA.

    Чтобы сущности групп не ИСЧЕЗАЛИ при потере связи/перезагрузке, а становились
    недоступными и восстанавливались. Ключ — gwSn:channel:groupId."""

    purge_name = "DALI-группы"

    async def purge_identity(self, identity: str) -> int:
        """Группа не привязана к устройству: её состав — адреса, а не серийники.
        Убирается вместе со шлюзом или явным удалением группы."""
        return 0

    async def purge_gateway(self, gw_sn: str) -> int:
        return await self.async_remove_gateway(gw_sn)

    def __init__(self, hass: HomeAssistant) -> None:
        self._store = Store(hass, 1, _GROUP_STORE_KEY)
        self._data: dict[str, dict] = {}

    async def async_load(self) -> None:
        self._data = await self._store.async_load() or {}

    @staticmethod
    def _k(gw_sn: str, channel, group_id) -> str:
        return f"{gw_sn}:{channel}:{group_id}"

    def all(self, gw_sn: str) -> list[dict]:
        return [dict(v) for v in self._data.values() if v.get("gw") == gw_sn]

    async def async_upsert(self, gw_sn: str, group: dict) -> None:
        self._data[self._k(gw_sn, group["channel"], group["groupId"])] = {
            "gw": gw_sn, "channel": group["channel"], "groupId": group["groupId"],
            "name": group.get("name", ""), "members": group.get("members", []),
        }
        self._store.async_delay_save(lambda: self._data, _SAVE_DELAY)

    async def async_remove(self, gw_sn: str, channel, group_id) -> None:
        if self._data.pop(self._k(gw_sn, channel, group_id), None) is not None:
            await self._store.async_save(self._data)

    async def async_remove_gateway(self, gw_sn: str) -> int:
        """Снести ВСЕ группы шлюза (v1.2.9: чистка при удалении ConfigEntry). Ключ — `{gw}:…`."""
        pref = f"{gw_sn}:"
        gone = [k for k in self._data if k.startswith(pref)]
        for k in gone:
            self._data.pop(k, None)
        if gone:
            await self._store.async_save(self._data)
        return len(gone)


class CrossGroupStore(PurgeableStore):
    """Персист КРОСС-ШЛЮЗОВЫХ DALI-групп — ОТДЕЛЬНАЯ модель от `GroupStore`.

    Кросс-группа не принадлежит шлюзу: это один и тот же `groupId` + имя, заведённые на
    КАЖДОМ участнике, каждому — только его лампы (захват 2026-08-04, docs/CROSS_GATEWAY.md §2).
    Однолшлюзовые группы остаются в `GroupStore` без изменений — решение 2026-08-04: подходы
    разные, смешивать их в одной модели дороже, чем держать две.

    ⚠ **Ключ — `uid`, вычисленный ОДИН РАЗ при создании** (`group_ops.cross_group_uid`) и с
    тех пор НЕИЗМЕННЫЙ. Пересчитывать его от живого состава запрещено: убрали последнюю
    лампу одного из шлюзов — набор участников поменялся, id поехал бы, HA завёл новую
    сущность, история оборвалась (летучий ключ, закон 2).
    `participants` тоже храним — это состояние НА МОМЕНТ создания плюс правки состава; для
    `unique_id` он больше не используется."""

    purge_name = "кросс-шлюзовые группы"

    async def purge_identity(self, identity: str) -> int:
        """Состав кросс-группы — адреса ламп, серийников там нет."""
        return 0

    async def purge_gateway(self, gw_sn: str) -> int:
        """СОСТАВ НЕ ТРОГАЕМ (решение пользователя 2026-08-07, вариант A).

        Прогон на объекте показал цену автоматики: удалили шлюз → он выбыл из участников →
        вернули шлюз → его лампы кросс-группой уже не управлялись, а рядом всплыла его
        «личная» группа-двойник с тем же именем. Удаление записи в HA чаще означает
        переустановку, а не демонтаж контроллера, поэтому состав сохраняем — вернувшийся
        шлюз просто снова начинает работать.

        ⚠ Осознанный размен: если контроллер убран НАВСЕГДА, запись продолжает ссылаться на
        отсутствующего участника. Это видно оператору (в карточке участник помечен) и
        разбирается вручную — как и договаривались: программа не решает за человека, а
        показывает состояние (см. CLAUDE.md, принцип «проблемы должны быть ВИДНЫ»)."""
        return 0

    def __init__(self, hass: HomeAssistant) -> None:
        self._store = Store(hass, 1, _XGROUP_STORE_KEY)
        self._data: dict[str, dict] = {}

    async def async_load(self) -> None:
        self._data = await self._store.async_load() or {}

    def all(self) -> list[dict]:
        """Все кросс-группы (они не привязаны к шлюзу — фильтровать по нему нечем)."""
        return [dict(v) for v in self._data.values()]

    def get(self, uid: str) -> dict | None:
        v = self._data.get(uid)
        return dict(v) if v else None

    def for_gateway(self, gw_sn: str) -> list[dict]:
        """Кросс-группы, в которых участвует этот шлюз (для чистки при удалении записи)."""
        gw = str(gw_sn or "").upper()
        return [dict(v) for v in self._data.values()
                if gw in {str(p).upper() for p in (v.get("participants") or [])}]

    async def async_upsert(self, group: dict) -> None:
        """Записать/обновить. `uid` обязателен и НЕ пересчитывается здесь — приходит готовым."""
        uid = group["uid"]
        prev = self._data.get(uid) or {}
        self._data[uid] = {
            "uid": uid,
            "channel": group["channel"], "groupId": group["groupId"],
            "name": group.get("name", prev.get("name", "")),
            "participants": group.get("participants", prev.get("participants", [])),
            "members": group.get("members", prev.get("members", [])),
        }
        self._store.async_delay_save(lambda: self._data, _SAVE_DELAY)

    async def async_remove(self, uid: str) -> bool:
        if self._data.pop(uid, None) is None:
            return False
        await self._store.async_save(self._data)
        return True


class DeviceStore(PurgeableStore):
    """Персист устройств шины (devType/channel/address/name/devSn) по шлюзам.

    Чтобы шлюзы и их сущности не ПРОПАДАЛИ при оффлайне/рестарте, а становились
    недоступными и оживали при возврате связи. Данные на устройство — минимум для
    создания сущности; живое состояние/яркость не храним. Ключ верхнего уровня — gwSn."""

    purge_name = "устройства шины"

    async def purge_identity(self, identity: str) -> int:
        """Снять записи с этим серийником на ВСЕХ шлюзах (устройство могло переехать)."""
        gone = 0
        for gw, devs in list(self._data.items()):
            keep = {k: v for k, v in (devs or {}).items() if v.get("devSn") != identity}
            if len(keep) != len(devs or {}):
                gone += len(devs) - len(keep)
                self._data[gw] = keep
        if gone:
            await self._store.async_save(self._data)
        return gone

    async def purge_gateway(self, gw_sn: str) -> int:
        return await self.async_remove_gateway(gw_sn)

    def __init__(self, hass: HomeAssistant) -> None:
        self._store = Store(hass, 1, _DEVICE_STORE_KEY)
        self._data: dict[str, dict] = {}

    async def async_load(self) -> None:
        self._data = await self._store.async_load() or {}

    def all(self, gw_sn: str) -> list[dict]:
        return [dict(v) for v in (self._data.get(gw_sn) or {}).values()]

    async def async_replace(self, gw_sn: str, devices: dict[str, dict]) -> None:
        """Полностью заменить набор устройств шлюза (источник — успешная загрузка/скан).

        `zombie` СОХРАНЯЕМ (v0.99): скан метит исчезнувшее устройство, но раньше белый список
        полей его резал → флаг не доезжал до диска, и после рестарта зомби «оживал» онлайн
        (сущность применяет флаг в `_bus_register`, но применять было нечего). См. docs/ENTITIES."""
        # `orphan` СОХРАНЯЕМ (v1.2.2) по той же причине, что и `zombie`: осиротевшего (его адрес
        # занял другой devSn) убирает ЧЕЛОВЕК кнопкой «Забыть». Не доехав до диска, флаг терялся бы
        # на рестарте — запись всплыла бы как обычная, а её сущности так и висели бы недоступными.
        # `status` СОХРАНЯЕМ (v1.2.23) — исключение из «живое состояние не храним». Это не
        # яркость, а ПОСЛЕДНЕЕ СКАЗАННОЕ ШЛЮЗОМ про присутствие устройства (`onlineStatus`).
        # Без него `online_map` после рестарта пуст, а дефолт `get(k, True)` = «доступна» →
        # снятая с шины лампа выглядела управляемой, пока шлюз не пришлёт очередной
        # `onlineStatus` (минуты). Восстанавливаем знание, а не выдумываем его.
        self._data[gw_sn] = {
            k: {"devType": v.get("devType"), "channel": v.get("channel"),
                "address": v.get("address"), "name": v.get("name", ""),
                "devSn": v.get("devSn", ""), "zombie": bool(v.get("zombie")),
                "orphan": bool(v.get("orphan")), "status": v.get("status")}
            for k, v in devices.items()
        }
        self._store.async_delay_save(lambda: self._data, _SAVE_DELAY)

    async def async_remove_gateway(self, gw_sn: str) -> int:
        """Снести устройства шлюза (v1.2.9: чистка при удалении ConfigEntry). Иначе они всплывут
        из персиста при повторном добавлении того же шлюза (`load_persisted`)."""
        n = len(self._data.get(gw_sn, {}) or {})
        if self._data.pop(gw_sn, None) is not None:
            await self._store.async_save(self._data)
        return n

    async def async_sanitize(self) -> int:
        """Z1: выкинуть из персиста МУСОРНЫЕ записи (пустой/вырожденный devSn — фантомы
        вроде поворотных панелей `0300` с devSn=''). Вызывается при старте, чтобы такие
        записи не «всплывали из памяти» на рестарте, особенно когда шлюз оффлайн."""
        removed = 0
        for gw, devs in list(self._data.items()):
            keep = {k: v for k, v in (devs or {}).items() if is_valid_devsn(v.get("devSn"))}
            removed += len(devs or {}) - len(keep)
            self._data[gw] = keep
        if removed:
            try:                                  # стартовая запись — сбой не валит setup
                await self._store.async_save(self._data)
            except Exception as err:  # noqa: BLE001
                _LOGGER.warning("DeviceStore: санитар в памяти ок, запись не удалась: %s", err)
            _LOGGER.warning("DeviceStore: удалено %d фантомных записей (пустой/вырожденный devSn)",
                            removed)
        return removed

    async def async_claim(self, gw_sn: str, devsns: set[str]) -> dict[str, list[str]]:
        """Z2: devSn УНИКАЛЕН на один шлюз. Шлюз `gw_sn` нашёл `devsns` (валидные) при скане
        → отобрать их у ДРУГИХ шлюзов (переехавшие устройства оставляют хвост в персисте
        старого шлюза → коллизия unique_id → лампы «не управляются»). Возврат: {gw: [keys]}."""
        stolen: dict[str, list[str]] = {}
        for gw, devs in list(self._data.items()):
            if gw == gw_sn or not devs:
                continue
            drop = [k for k, v in devs.items() if v.get("devSn") in devsns]
            if drop:
                for k in drop:
                    devs.pop(k, None)
                stolen[gw] = drop
        if stolen:
            try:                                  # стартовая запись — сбой не валит setup
                await self._store.async_save(self._data)
            except Exception as err:  # noqa: BLE001
                _LOGGER.warning("DeviceStore: claim в памяти ок, запись не удалась: %s", err)
            # штатная консолидация уникальности devSn (Z2), НЕ аномалия → INFO, чтобы не
            # сорить WARNING-каналом (по нему судят о здоровье интеграции). Заглушечные/
            # нестабильные serial дают ложное «переехало» на каждом старте; реальный перенос
            # между шлюзами и так виден по смене шлюза устройства в карточке.
            _LOGGER.info("DeviceStore: %s консолидировал %d devSn (убраны из персиста %s)",
                         gw_sn, sum(len(v) for v in stolen.values()), list(stolen))
        return stolen

    def devsn_addr_map(self) -> dict[str, str]:
        """Карта `gwSn:devType:ch:addr` → devSn для миграции ParamStore с адресного ключа
        на стабильный devSn. Инкапсулирует обход персиста (раньше `async_setup_store` лез
        в приватный `_data`). Ключ записи устройства = `devType:ch:addr`; только валидные devSn."""
        return {
            f"{gw}:{k}": rec.get("devSn")
            for gw, recs in self._data.items()
            for k, rec in (recs or {}).items()
            if is_valid_devsn(rec.get("devSn"))
        }


class RotaryStore(PurgeableStore):
    """Персист привязок поворотной панели (0300) → регулировка яркости цели в HA.

    Натив шлюза «следовать за ручкой» не умеет (см. docs), поэтому крутим яркость
    логикой в HA по событиям поворота (dpid 4, абсолютная позиция 0..255). Ключ —
    devSn поворотной панели (стабильная идентичность). Значение:
    {target:{devType,channel,address}, step}."""

    purge_name = "привязки поворотной панели"

    async def purge_identity(self, identity: str) -> int:
        return await self._purge_keys([identity] if identity in self._data else [])

    async def purge_gateway(self, gw_sn: str) -> int:
        """Ключ — только devSn панели, шлюза в нём нет → чистить по шлюзу нечего.
        Записи снимаются вместе с устройствами (`purge_identity` на каждое)."""
        return 0

    def __init__(self, hass: HomeAssistant) -> None:
        self._store = Store(hass, 1, _ROTARY_STORE_KEY)
        self._data: dict[str, dict] = {}

    async def async_load(self) -> None:
        self._data = await self._store.async_load() or {}

    def get(self, devsn: str) -> dict | None:
        return self._data.get(str(devsn))

    async def async_set(self, devsn: str, binding: dict) -> None:
        self._data[str(devsn)] = binding
        self._store.async_delay_save(lambda: self._data, _SAVE_DELAY)

    async def async_remove(self, devsn: str) -> None:
        if self._data.pop(str(devsn), None) is not None:
            await self._store.async_save(self._data)


class SensorPrefStore(PurgeableStore):
    """Персист ЖЕЛАЕМОЙ активности датчика (`setSensorOnOff`), v1.2.23.

    Раньше `hub.sensor_active` жил ТОЛЬКО в памяти → после рестарта HA `_rearm_sensors`
    включал обратно датчик, который пусконаладчик выключил осознанно (напр. снял автояркость
    в кабинете). Решение пользователя: **не будить выключенное** — состояние остаётся за
    человеком, а не за нашим перевзводом.

    Ключ — ИДЕНТИЧНОСТЬ (`devSn:devType`, тот же `sensor_pref_key`, Fix L), поэтому
    перенумерация адресов предпочтение не протухает. Значение — bool (активен)."""

    purge_name = "предпочтения активности датчиков"

    async def purge_identity(self, identity: str) -> int:
        """Ключ — `devSn:devType` (движение и люкс — две записи одного устройства)."""
        return await self._purge_keys([k for k in self._data
                                       if str(k).startswith(f"{identity}:")])

    async def purge_gateway(self, gw_sn: str) -> int:
        """Легаси адресный фолбэк ключа (до v1.2.51) начинался с серийника ШЛЮЗА."""
        return await self._purge_keys([k for k in self._data
                                       if str(k).startswith(f"{gw_sn}:")])

    def __init__(self, hass: HomeAssistant) -> None:
        self._store = Store(hass, 1, _SENSORPREF_STORE_KEY)
        self._data: dict[str, bool] = {}

    async def async_load(self) -> None:
        self._data = await self._store.async_load() or {}

    def all(self) -> dict[str, bool]:
        return dict(self._data)

    def get(self, key: str) -> bool | None:
        return self._data.get(str(key))

    async def async_set(self, key: str, active: bool) -> None:
        self._data[str(key)] = bool(active)
        self._store.async_delay_save(lambda: self._data, _SAVE_DELAY)

    async def async_remove(self, key: str) -> None:
        if self._data.pop(str(key), None) is not None:
            await self._store.async_save(self._data)


class SensorObjStore(PurgeableStore):
    """Персист ПОСЛЕДНЕЙ ЗАПИСАННОЙ НАМИ конфигурации функции датчика (v1.2.24).

    ЗАЧЕМ («план Б» пользователя). Чтобы дописать расписание, не стерев привязку, нужно знать
    текущие `outputObj`/`luxRange`. Сейчас берём их у железа (`readSensor`), но на объекте
    массовый опрос может оказаться слишком долгим. Тогда источником станет ЭТОТ стор — что мы
    сами отправили. Надёжно ровно при условии, которое назвал пользователь: на рабочем объекте
    не правят конфигурацию с нескольких панелей (настольный DALI Center + наша карта) постоянно.

    Пишем ВСЕГДА (даже пока читаем с железа) — чтобы переключение на «план Б» не требовало
    переделки и не начиналось с пустого стора. Ключ — ИДЕНТИЧНОСТЬ + функция:
    `devSn:devType:dpid` (адрес волатилен, закон проекта), фолбэк на `gwSn:devType:dpid`."""

    purge_name = "конфигурации функций датчиков"

    async def purge_identity(self, identity: str) -> int:
        """Ключ — `devSn:devType:dpid` → снимаем все функции этого устройства."""
        return await self._purge_keys([k for k in self._data
                                       if str(k).startswith(f"{identity}:")])

    async def purge_gateway(self, gw_sn: str) -> int:
        """Легаси-фолбэк писался как `gwSn:devType:dpid` (до v1.2.51)."""
        return await self._purge_keys([k for k in self._data
                                       if str(k).startswith(f"{gw_sn}:")])

    def __init__(self, hass: HomeAssistant) -> None:
        self._store = Store(hass, 1, _SENSOROBJ_STORE_KEY)
        self._data: dict[str, dict] = {}

    async def async_load(self) -> None:
        self._data = await self._store.async_load() or {}

    @staticmethod
    def _k(gw_sn: str, identity: str, dev_type: str, dpid) -> str | None:
        """Ключ — ИДЕНТИЧНОСТЬ + ФУНКЦИЯ + dpid (v1.2.51, идентичность обобщена в v1.2.73).

        Был фолбэк на серийник ШЛЮЗА: конфигурации разных датчиков одного контроллера
        схлопывались в одну запись, а чистка по устройству их не находила. Без ключа
        идентичности просто не храним — «план Б» для такого датчика недоступен, и это честнее
        подмены.

        ⚠ `dev_type` в ключе обязателен и в адресном режиме: движение `0201` и освещённость
        `0202` делят координату, но конфигурации функций у них РАЗНЫЕ (Н2 плана)."""
        if not identity:
            return None
        if not is_addr_key(identity) and not is_valid_devsn(identity):
            return None
        return f"{identity}:{dev_type}:{dpid}"

    def get(self, gw_sn: str, devsn: str, dev_type: str, dpid) -> dict | None:
        k = self._k(gw_sn, devsn, dev_type, dpid)
        return self._data.get(k) if k else None

    async def async_set(self, gw_sn: str, devsn: str, dev_type: str, dpid, cfg: dict) -> None:
        k = self._k(gw_sn, devsn, dev_type, dpid)
        if not k:                      # нет devSn → хранить нечем (см. _k)
            return
        self._data[k] = dict(cfg)
        self._store.async_delay_save(lambda: self._data, _SAVE_DELAY)


class PanelActStore(PurgeableStore):
    """Персист ДЕЙСТВИЯ привязки кнопки (stepup/dimdown/on…). Контроллер при `readPanel`
    возвращает ЦЕЛЬ привязки, но НЕ тип действия (property пуст — команды-жесты STEP/UP/DOWN
    не эхоятся, см. docs/PLAN_SENSOR_BINDINGS §H1c). Храним у себя, чтобы карточка показывала
    «шаг ярче» и т.п., а не «жест на контроллере». Ключ — gw:keyNo:жест-dpid:цель(dt:ch:addr)."""

    purge_name = "действия привязок кнопок"

    async def purge_identity(self, identity: str) -> int:
        """Ключ — `gw:keyNo:dpid:цель(dt:ch:addr)`: ни панели, ни цели по серийнику там нет
        (обе адресуются адресом). Снимается вместе со шлюзом."""
        return 0

    async def purge_gateway(self, gw_sn: str) -> int:
        return await self.async_remove_gateway(gw_sn)

    def __init__(self, hass: HomeAssistant) -> None:
        self._store = Store(hass, 1, _PANELACT_STORE_KEY)
        self._data: dict[str, str] = {}

    async def async_load(self) -> None:
        self._data = await self._store.async_load() or {}

    @staticmethod
    def _k(gw_sn: str, key_no, dpid, tgt: dict) -> str:
        return (f"{gw_sn}:{key_no}:{dpid}:{tgt.get('devType')}:"
                f"{tgt.get('channel')}:{tgt.get('address')}")

    def get(self, gw_sn: str, key_no, dpid, tgt: dict) -> str:
        return self._data.get(self._k(gw_sn, key_no, dpid, tgt), "")

    def targets_for(self, gw_sn: str, key_no, dpid) -> list[tuple[str, str, int, int]]:
        """Все цели привязки кнопки×жеста → [(act, devType, channel, address)]. Нужно
        оценщику удержания (баг2): по событию hold найти, что кнопка драйвит. gw_sn — hex
        без ':', devType/ch/addr — хвост ключа `gw:keyNo:dpid:devType:ch:addr`."""
        prefix = f"{gw_sn}:{key_no}:{dpid}:"
        out: list[tuple[str, str, int, int]] = []
        for k, act in self._data.items():
            if not k.startswith(prefix):
                continue
            rest = k[len(prefix):].split(":")
            if len(rest) == 3:
                try:
                    out.append((act, rest[0], int(rest[1]), int(rest[2])))
                except (TypeError, ValueError):
                    continue
        return out

    async def async_set(self, gw_sn: str, key_no, dpid, tgt: dict, action: str) -> None:
        k = self._k(gw_sn, key_no, dpid, tgt)
        if action:
            self._data[k] = action
        else:
            self._data.pop(k, None)
        self._store.async_delay_save(lambda: self._data, _SAVE_DELAY)

    async def async_remove_gateway(self, gw_sn: str) -> int:
        """Снести все привязки кнопок шлюза (v1.2.9). Ключ — `{gw}:…`."""
        pref = f"{gw_sn}:"
        gone = [k for k in self._data if k.startswith(pref)]
        for k in gone:
            self._data.pop(k, None)
        if gone:
            await self._store.async_save(self._data)
        return len(gone)


class GroupParamStore(PurgeableStore):
    """Персист параметров, заданных ГРУППЕ ламп (fadeRate/fadeTime/powerOn… через
    `set_param_bulk` с group). Лампы хранят свои параметры в ParamStore по devSn, но «памяти
    группы» не было → диалог «Параметры группы» открывался ПУСТЫМ (загадка). Ключ —
    gw:channel:groupId. Показывает, что задавали группе последний раз (не физика ламп)."""

    purge_name = "параметры групп"

    async def purge_identity(self, identity: str) -> int:
        """Ключ — `gw:ch:groupId`, устройства в нём нет."""
        return 0

    async def purge_gateway(self, gw_sn: str) -> int:
        return await self.async_remove_gateway(gw_sn)

    def __init__(self, hass: HomeAssistant) -> None:
        self._store = Store(hass, 1, _GROUPPARAM_STORE_KEY)
        self._data: dict[str, dict] = {}

    async def async_load(self) -> None:
        self._data = await self._store.async_load() or {}

    @staticmethod
    def _k(gw_sn: str, channel, group_id) -> str:
        return f"{gw_sn}:{channel}:{group_id}"

    def get(self, gw_sn: str, channel, group_id) -> dict:
        return dict(self._data.get(self._k(gw_sn, channel, group_id), {}))

    async def async_update(self, gw_sn: str, channel, group_id, paramer: dict) -> None:
        k = self._k(gw_sn, channel, group_id)
        self._data[k] = {**self._data.get(k, {}), **paramer}
        self._store.async_delay_save(lambda: self._data, _SAVE_DELAY)

    async def async_remove(self, gw_sn: str, channel, group_id) -> bool:
        """Снести параметры ОДНОЙ группы (v1.2.20, F11): ключ `gw:channel:groupId` — это НОМЕР
        СЛОТА в таблице шлюза, он переиспользуется. Без чистки при удалении группы `fadeRate` и т.п.
        от УДАЛЁННОЙ группы достаётся НОВОЙ группе с тем же id (и идёт в расчёт удержания-диммирования
        `_apply_hold_dim`). Тот же приём, что для легаси-имени группы в `ws_del_group`."""
        k = self._k(gw_sn, channel, group_id)
        if self._data.pop(k, None) is not None:
            await self._store.async_save(self._data)
            return True
        return False

    async def async_remove_gateway(self, gw_sn: str) -> int:
        """Снести параметры групп шлюза (v1.2.9). Ключ — `{gw}:…`."""
        pref = f"{gw_sn}:"
        gone = [k for k in self._data if k.startswith(pref)]
        for k in gone:
            self._data.pop(k, None)
        if gone:
            await self._store.async_save(self._data)
        return len(gone)


class IdentityModeStore(PurgeableStore):
    """Чем ключуется идентичность на ЭТОЙ установке: `devsn` (штатно) или `addr`.

    Почему отдельный файл, а не опция в ConfigEntry: записей у нас по одной НА ШЛЮЗ, а режим —
    один на объект (docs/ADDRESS_IDENTITY.md §11.1). Флаг в каждой записи пришлось бы
    синхронизировать, и рано или поздно добавленный позже шлюз получил бы не тот режим — это
    источник расхождения, а не удобство.

    ⚠ Хранилище НЕ участвует в чистках (не `PurgeableStore`): «Стереть данные» и удаление шлюза
    обязаны оставлять режим как есть. Режим меняет только человек явным действием.
    """

    def __init__(self, hass: HomeAssistant) -> None:
        self._store = Store(hass, 1, _MODE_STORE_KEY)
        self._mode: str = DEFAULT_MODE

    async def async_load(self) -> None:
        data = await self._store.async_load() or {}
        raw = data.get("mode")
        self._mode = normalize_mode(raw)
        if raw is not None and self._mode != raw:
            # молча подменять режим нельзя: человек должен узнать, что в файле мусор
            _LOGGER.error("режим идентичности «%s» неизвестен — работаю в «%s»", raw, self._mode)
        if self._mode != DEFAULT_MODE:
            _LOGGER.warning("РЕЖИМ ИДЕНТИЧНОСТИ: %s (ключ — координата на шине, не серийник)",
                            self._mode)

    purge_name = "режим идентичности"

    async def purge_identity(self, identity: str) -> int:
        """НЕ чистим — и это решение, а не забывчивость (см. `PurgeableStore`).

        Режим — настройка ВСЕЙ установки, а не данные устройства. Если бы «Забыть» его трогала,
        одно снятое устройство молча вернуло бы объект в штатный режим, и половина сущностей
        переехала бы на другие ключи. Режим меняет только человек явным действием."""
        return 0

    async def purge_gateway(self, gw_sn: str) -> int:
        """НЕ чистим по той же причине: «Стереть данные» и удаление шлюза обязаны оставлять
        режим как есть. Иначе откат «стёр данные → переключил» превращался бы в рулетку."""
        return 0

    @property
    def mode(self) -> str:
        return self._mode

    async def async_set(self, mode: str) -> str:
        """Сменить режим. Возвращает установленный. Гейты (пусто ли на объекте) — НЕ здесь:
        это хранилище, оно не решает, можно ли; решает вызывающий, и он же спрашивает человека."""
        self._mode = normalize_mode(mode)
        await self._store.async_save({"mode": self._mode})
        _LOGGER.warning("режим идентичности переключён на «%s»", self._mode)
        return self._mode


def get_store(hass: HomeAssistant) -> ParamStore | None:
    return hass.data.get(_HASS_KEY)


def get_identity_mode_store(hass: HomeAssistant) -> "IdentityModeStore | None":
    return hass.data.get(_MODE_HASS_KEY)


def get_identity_mode(hass: HomeAssistant) -> str:
    """Режим идентичности установки. До загрузки хранилища — штатный (ничего не меняем)."""
    st = hass.data.get(_MODE_HASS_KEY)
    return st.mode if st else DEFAULT_MODE


def get_panel_act_store(hass: HomeAssistant) -> "PanelActStore | None":
    return hass.data.get(_PANELACT_HASS_KEY)


def get_group_param_store(hass: HomeAssistant) -> "GroupParamStore | None":
    return hass.data.get(_GROUPPARAM_HASS_KEY)


def get_rotary_store(hass: HomeAssistant) -> "RotaryStore | None":
    return hass.data.get(_ROTARY_HASS_KEY)


def get_sensor_pref_store(hass: HomeAssistant) -> "SensorPrefStore | None":
    return hass.data.get(_SENSORPREF_HASS_KEY)


def get_sensor_obj_store(hass: HomeAssistant) -> "SensorObjStore | None":
    return hass.data.get(_SENSOROBJ_HASS_KEY)


def get_name_store(hass: HomeAssistant) -> NameStore | None:
    return hass.data.get(_NAME_HASS_KEY)


def get_group_store(hass: HomeAssistant) -> GroupStore | None:
    return hass.data.get(_GROUP_HASS_KEY)


def get_cross_group_store(hass: HomeAssistant) -> "CrossGroupStore | None":
    return hass.data.get(_XGROUP_HASS_KEY)


def get_device_store(hass: HomeAssistant) -> DeviceStore | None:
    return hass.data.get(_DEVICE_HASS_KEY)


async def async_setup_store(hass: HomeAssistant) -> None:
    pstore = ParamStore(hass)
    await pstore.async_load()
    hass.data[_HASS_KEY] = pstore
    # режим идентичности читаем ПЕРВЫМ: от него зависит, чем ключуется всё остальное
    mstore = IdentityModeStore(hass)
    await mstore.async_load()
    hass.data[_MODE_HASS_KEY] = mstore
    nstore = NameStore(hass)
    await nstore.async_load()
    hass.data[_NAME_HASS_KEY] = nstore
    gstore = GroupStore(hass)
    await gstore.async_load()
    hass.data[_GROUP_HASS_KEY] = gstore
    xgstore = CrossGroupStore(hass)          # кросс-шлюзовые группы (отдельная модель)
    await xgstore.async_load()
    hass.data[_XGROUP_HASS_KEY] = xgstore
    dstore = DeviceStore(hass)
    await dstore.async_load()
    await dstore.async_sanitize()   # Z1: чистим фантомы (пустой/вырожденный devSn) при старте
    hass.data[_DEVICE_HASS_KEY] = dstore
    rstore = RotaryStore(hass)
    await rstore.async_load()
    hass.data[_ROTARY_HASS_KEY] = rstore
    pastore = PanelActStore(hass)
    await pastore.async_load()
    hass.data[_PANELACT_HASS_KEY] = pastore
    gpstore = GroupParamStore(hass)
    await gpstore.async_load()
    hass.data[_GROUPPARAM_HASS_KEY] = gpstore
    spstore = SensorPrefStore(hass)          # v1.2.23: «не будить выключённое» переживает рестарт
    await spstore.async_load()
    hass.data[_SENSORPREF_HASS_KEY] = spstore
    sostore = SensorObjStore(hass)           # v1.2.24: что МЫ записали датчику («план Б» вместо readSensor)
    await sostore.async_load()
    hass.data[_SENSOROBJ_HASS_KEY] = sostore
    # Миграция ParamStore: адресные ключи → devSn (по DeviceStore). Идемпотентно — каждый
    # старт ДОБИВАЕТ сирот, если их устройства появились в кеше позже. Проход гоняем ТОЛЬКО
    # пока остались адресные ключи (в устоявшемся состоянии всё на devSn → ноль работы).
    # Карту строит сам DeviceStore (инкапсуляция — не лезем в его _data).
    if pstore.has_legacy_keys():
        await pstore.async_migrate_to_devsn(dstore.devsn_addr_map())
    # РЕЕСТР для единой чистки (S5): все сторы, кроме сателлитных (энергия/здоровье — свои)
    hass.data[_ALL_STORES_HASS_KEY] = [mstore, pstore, nstore, gstore, xgstore, dstore, rstore,
                                       pastore, gpstore, spstore, sostore]


# ── ЕДИНАЯ ЧИСТКА (S5, v1.2.51) ──────────────────────────────────────────────────────
# Три операции («Забыть», «Стереть данные», удаление шлюза) больше НЕ перечисляют сторы
# руками — они обходят реестр. Забыть подключить новый стор нельзя: он и в реестре, и обязан
# реализовать оба метода (иначе падает tests/test_stores.py).


def get_all_stores(hass: HomeAssistant) -> list[PurgeableStore]:
    """Все хранилища, участвующие в чистке (порядок не важен — операции независимы).

    Основной реестр собирается в `async_setup_store`; САТЕЛЛИТЫ (энергия) добавляются здесь,
    потому что поднимаются отдельно и могут отсутствовать — но забывать их нельзя, ровно на
    этом и погорели (DEBT §S)."""
    stores = list(hass.data.get(_ALL_STORES_HASS_KEY) or [])
    from .energy.store import get_energy_store
    es = get_energy_store(hass)
    if es is not None:
        stores.append(es)
    return stores


async def purge_identity_everywhere(hass: HomeAssistant, identity: str) -> dict[str, int]:
    """Убрать ВСЁ, что хранится про устройство, во всех сторах. Отчёт: имя стора → сколько.

    ⚠ Реестры HA (сущности/устройства) этим не затрагиваются — они живут по своим законам
    (мягкое удаление, закон 1) и чистятся вызывающим кодом отдельно."""
    report: dict[str, int] = {}
    for store in get_all_stores(hass):
        try:
            n = await store.purge_identity(devsn)
        except Exception as err:  # noqa: BLE001 — один стор не должен рушить всю чистку
            _LOGGER.error("purge_identity(%s) в %s: %s", devsn, store.purge_name, err)
            continue
        if n:
            report[store.purge_name] = n
    if report:
        _LOGGER.info("чистка устройства %s: %s", devsn, report)
    return report


async def purge_gateway_everywhere(hass: HomeAssistant, gw_sn: str) -> dict[str, int]:
    """Убрать ВСЁ, что хранится про шлюз, во всех сторах. Отчёт: имя стора → сколько."""
    report: dict[str, int] = {}
    for store in get_all_stores(hass):
        try:
            n = await store.purge_gateway(gw_sn)
        except Exception as err:  # noqa: BLE001
            _LOGGER.error("purge_gateway(%s) в %s: %s", gw_sn, store.purge_name, err)
            continue
        if n:
            report[store.purge_name] = n
    if report:
        _LOGGER.info("чистка шлюза %s: %s", gw_sn, report)
    return report
