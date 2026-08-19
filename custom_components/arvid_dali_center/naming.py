"""Именование: имя УСТРОЙСТВА по devSn, имя СУЩНОСТИ по адресу (модель v1.2.7).

Три поля, три роли, НИ ОДНО не догоняет другое (это и снимает класс дефектов M2/наложений):

  имя УСТРОЙСТВА HA  = <тип-слово>_<полный devSn>   (sensor_E038…, light_E0…, keypanel_8_E0…)
    → СТАБИЛЬНО НАВСЕГДА: devSn не меняется ни при смене адреса, ни при переезде на другой шлюз.
      Значит имя устройства НИКОГДА не переименовывается (Fix V удалён).
  entity_id БЕЗЫМЯННОЙ сущности = <тип>_<адрес>_<sn5>   (motion_14_0ffd6)
    → адрес нужен для опознания глазами (по нему и ориентируются на пусконаладке); sn5 (5 знаков
      devSn) разводит одинаковые адреса и снимает наложения при перераздаче адресов.
    → ⚠ ШЛЮЗА В ИМЕНИ НЕТ (был `_<gw4>`, убран в v1.2.7): sn5 уже уникален, а gw4 только вредил —
      при переезде устройства на другой шлюз (тот же адрес) entity_id менялся без нужды, разрывая
      историю recorder. Без gw4 переезд между шлюзами entity_id НЕ трогает → история непрерывна.
  подпись (friendly_name) БЕЗЫМЯННОЙ = НЕ ЗАДАЁМ
    → HA выводит её из entity_id сам (motion_14_0ffd6 → «motion 14 0ffd6»), и она следует за
      entity_id автоматически. Догонять нечего (ветка подписи Fix N удалена).

ИМЕНОВАННЫЕ (продакшен, через панель, NameStore, ключ devSn) — данные ЧЕЛОВЕКА, от адреса не
зависят (на них висят автоматизации): entity_id = slug(имя), подпись = имя (задаём явно).
"""

from __future__ import annotations

import re

LIGHT_TYPES = {"0101", "0102", "0103", "0104", "0105", "0106"}
PANEL_KEYS = {"0302": 2, "0304": 4, "0306": 6, "0308": 8}

# Префикс пользовательского имени датчика ПО ТИПУ: движение → ms_, освещённость → il_.
# Движение и люкс — одно физическое устройство (общий devSn/адрес/ключ имени), поэтому
# имя у них общее «тело», а тип кодируется префиксом (entity_id == friendly == ms_/il_ + тело).
SENSOR_PREFIX = {"0201": "ms", "0202": "il"}

SN_TAIL = 5          # сколько знаков devSn берём в хвост entity_id сущности
GW_TAIL = 4          # знаков серийника ШЛЮЗА в хвосте entity_id (адресный режим)


def sensor_body(name: str) -> str:
    """«Тело» имени датчика без типового префикса/хвоста активации. Нормализует ввод:
    пользователь может ввести как полное `ms_5_1_3`, так и тело `5_1_3` — режем ведущий
    известный префикс и хвост `_act`. Обратно совместимо со старыми записями NameStore."""
    s = str(name or "").strip()
    low = s.lower()
    for p in ("illuminance_", "motion_", "ms_", "il_"):   # длинные раньше коротких
        if low.startswith(p):
            s = s[len(p):]
            break
    if s.lower().endswith("_act"):
        s = s[:-4]
    return s.strip("_") or str(name or "").strip()


def sensor_name(devtype: str, body: str) -> str:
    """Имя сущности датчика: <префикс по типу>_<тело> (ms_5_1_3 / il_5_1_3)."""
    return f"{SENSOR_PREFIX.get(str(devtype), 'sensor')}_{body}"


def type_word(devtype: str) -> str:
    """Базовое английское слово типа устройства."""
    d = str(devtype)
    if d in LIGHT_TYPES:
        return "light"
    if d == "0201":
        return "motion"
    if d == "0202":
        return "illuminance"
    if d == "0300":
        return "rotary"
    if d in PANEL_KEYS:
        return f"keypanel_{PANEL_KEYS[d]}"
    return f"dev_{d}"


def device_word(devtype: str) -> str:
    """Слово типа для имени УСТРОЙСТВА. У датчиков (02xx) движение и люкс — ОДНО устройство,
    поэтому общее слово `sensor` (а тип кодируется в именах сущностей: motion/illuminance)."""
    return "sensor" if str(devtype).startswith("02") else type_word(devtype)


def sn_suffix(devsn: str = "") -> str:
    """Разводящий хвост entity_id сущности: последние SN_TAIL знаков devSn.

    Пусто, если серийника нет (устройство ещё без devSn) — тогда entity_id строится без хвоста:
    наложение в этом случае возможно, но и опереться нам не на что (лечится fallback `_2`)."""
    s = str(devsn or "").strip().lower()
    return s[-SN_TAIL:] if s else ""


def gw_suffix(gw_sn: str = "") -> str:
    """Разводящий хвост entity_id в АДРЕСНОМ режиме: последние GW_TAIL знаков серийника ШЛЮЗА.

    Зачем он там нужен, хотя в штатном режиме шлюз из имени убран (v1.2.7): в адресном режиме
    имя производно от АДРЕСА, а адреса 0..63 повторяются на каждом контроллере — без хвоста
    `light_5` на двух шлюзах столкнулись бы. В штатном режиме эту роль играет `sn5`, и он же
    делает переезд между шлюзами незаметным для истории; в адресном режиме такой переезд и так
    меняет идентичность, поэтому `gw4` честен (Н4 плана docs/ADDRESS_IDENTITY.md).
    """
    s = str(gw_sn or "").strip().lower()
    return s[-GW_TAIL:] if s else ""


def device_name_addr(devtype: str, gw_sn: str = "", address=None) -> str:
    """Имя УСТРОЙСТВА HA в адресном режиме: `<тип-слово>_<gw4>_<адрес>`.

    Серийник в него не входит — он в этом режиме справочный и может быть пустым или
    перекошенным. Имя устройства человек видит редко (он смотрит на `entity_id`), но оно должно
    быть стабильным и однозначным: координата даёт и то, и другое."""
    word = device_word(devtype)
    tail = gw_suffix(gw_sn)
    parts = [word] + ([tail] if tail else []) + ([str(address)] if address is not None else [])
    return "_".join(parts)


def entity_name(devtype: str, address, devsn: str = "", *, tail: str | None = None) -> str:
    """entity_id/имя БЕЗЫМЯННОЙ сущности: <тип>_<адрес>_<sn5> (напр. motion_14_0ffd6).

    ⚠ v1.2.7: шлюза в имени НЕТ (был `_<gw4>`). sn5 уже уникален, а gw4 ломал entity_id при
    переезде между шлюзами (см. докстринг модуля). При пустом devSn — без хвоста."""
    base = f"{type_word(devtype)}_{address}"
    t = sn_suffix(devsn) if tail is None else tail   # tail задаёт вызывающий (режим — его знание)
    return f"{base}_{t}" if t else base


def device_name(devtype: str, devsn: str = "", address=None) -> str:
    """Имя УСТРОЙСТВА HA: <тип-слово>_<полный devSn> (sensor_E0387029A088D0B9, light_E0…).

    ⚠ v1.2.7: производно ТОЛЬКО от devSn → стабильно навсегда, не догоняет ни адрес, ни шлюз.
    Полный devSn (не sn5): имя устройства уникальности HA не требует, но полный серийник
    исключает совпадение двух разных устройств в карточке (важно на исследованиях с переездами).
    Fallback при пустом/битом devSn — по адресу, чтобы устройство добавилось, а не пропало."""
    word = device_word(devtype)
    sn = str(devsn or "").strip()
    if sn:
        return f"{word}_{sn}"
    return f"{word}_addr{address}" if address is not None else word


def is_auto_suffix(eid: str, desired: str) -> bool:
    """`eid` — это HA-автосуффикс ЖЕЛАЕМОГО id (`<desired>_2`, `_3`…)?

    ⚠ v1.1.7 (баг v1.1.4–1.1.6): раньше признаком считалось «кончается на `_<цифры>`»
    (`re.compile(r"_\\d+$")`). Но на это правило попадают продакшен-имена (`l_2_5_13`) и
    entity_id с числовым хвостом. Правильный признак — «это РОВНО желаемый id плюс `_N`».
    Чистая функция про имена → живёт здесь, тестируется без HA.
    """
    return bool(desired) and bool(re.fullmatch(rf"{re.escape(desired)}_\d+", eid or ""))
