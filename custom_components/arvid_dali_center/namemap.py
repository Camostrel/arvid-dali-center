"""Карта сопоставления «DALI-адрес → проектное имя»: разбор файла и сшивка со сканом.

ЗАЧЕМ. При переезде действующего объекта на нашу интеграцию имена уже розданы, но живут они
в старом HAOS и в памяти контроллеров. Переустановку переживает только DALI-адрес, поэтому
имена восстанавливаем по нему: пусконаладчик кладёт CSV-карту в `/config/arvid_namemap/`,
карточка показывает таблицу «что на шине ↔ что предлагает карта» и применяет отмеченное.

⚠ ЭТОТ МОДУЛЬ НИЧЕГО НЕ ПИШЕТ. Он только разбирает файл и СШИВАЕТ его со сканом, отдавая
строки со статусом. Имена применяет существующий `rename` (его карточка зовёт по одному
устройству — тот же путь, что при ручном переименовании), область — отдельный `set_area`.
Так работающий путь именования не переписывается ради этой задачи.

Формат карты (`;`, заголовок обязателен, BOM допускается):

    gw_sn;devtype;address;role;project_key;target_name;group_id;space;source;verify;note

Ключ сшивки — **(devtype, address)** в пределах шлюза. Это ЕДИНСТВЕННОЕ место, где мы
адресуемся адресом: карта для того и существует, что до присвоения имён другого моста нет.
После применения имя живёт на `devSn` (закон 2), а карта становится историей.

Генератор карты для объекта «школа №45» — `tools/voronezh/build_name_map.py`,
разбор источников — docs/VORONEZH_MIGRATION.md.
"""

from __future__ import annotations

import csv
import io
import logging

from .naming import sensor_body

_LOGGER = logging.getLogger(__name__)

# Колонки файла. Обязательны только те, без которых сшивка невозможна; остальные —
# справочные (показываем в таблице, чтобы человек понимал, откуда взялось предложение).
REQUIRED = ("gw_sn", "devtype", "address", "target_name")
OPTIONAL = ("role", "project_key", "group_id", "space", "area_id",
            "source", "verify", "danger", "note")

SENSOR_TYPES = ("0201", "0202")

# Статусы строки таблицы
ST_MATCHED = "matched"          # адрес есть и в карте, и на шине — можно применять
ST_NOT_ON_BUS = "not_on_bus"    # карта знает, скан не нашёл (лампа снята / не отвечает)
ST_NOT_IN_MAP = "not_in_map"    # на шине есть, в карте нет (не роздано / лишнее)
ST_PAIRED = "paired"            # освещённость: имя придёт вместе с движением (см. ниже)

# ⚠ ОСВЕЩЁННОСТЬ (0202) ОТДЕЛЬНО НЕ ИМЕНУЕТСЯ. Движение и люкс — ОДНО устройство с общим
# `devSn`, и `rename` переименовывает пару целиком: назвали движение — освещённость получила
# имя сама. Карта строк `0202` не содержит вовсе, а найденные на шине люкс-сущности мы
# помечаем `paired`, чтобы они не выглядели «нет в карте» и не считались проблемой.
# (Сопоставлять их по адресу НЕЛЬЗЯ: 0201 и 0202 — разные адресные пространства, адрес 6
#  у движения и адрес 6 у освещённости принадлежат разным приборам.)


def _norm_devtype(raw) -> str:
    """`101` / `0101` / `'0101'` → `0101`. Excel любит съедать ведущий ноль."""
    s = str(raw or "").strip()
    return s.zfill(4) if s.isdigit() else s.upper()


def _norm_addr(raw):
    """Адрес → int. `12`, `12.0`, ` 12 ` → 12; мусор → None (строка уйдёт в проблемы)."""
    s = str(raw or "").strip().replace(",", ".")
    if not s:
        return None
    try:
        return int(float(s))
    except ValueError:
        return None


def parse_map(text: str) -> tuple[list[dict], list[str]]:
    """Разобрать CSV карты → (строки, проблемы).

    Проблемы НЕ глушим и не выбрасываем молча: они возвращаются списком и показываются в
    карточке. Тихо пропущенная строка карты = устройство, которое останется без имени, а
    человек об этом не узнает.
    """
    rows: list[dict] = []
    problems: list[str] = []
    if not (text or "").strip():
        return rows, ["файл пуст"]

    reader = csv.DictReader(io.StringIO(text.lstrip("﻿")), delimiter=";")
    have = {(c or "").strip().lstrip("﻿") for c in (reader.fieldnames or [])}
    missing = [c for c in REQUIRED if c not in have]
    if missing:
        return rows, [f"в заголовке нет обязательных колонок: {', '.join(missing)}"]

    seen: dict[tuple[str, str, int], int] = {}
    for lineno, raw in enumerate(reader, start=2):
        raw = { (k or "").strip().lstrip("﻿"): (v or "").strip()
                for k, v in raw.items() if k is not None }
        gw = raw.get("gw_sn", "").upper()
        devtype = _norm_devtype(raw.get("devtype"))
        addr = _norm_addr(raw.get("address"))
        name = raw.get("target_name", "")
        if not gw or not devtype or addr is None:
            problems.append(f"строка {lineno}: не разобрать ключ "
                            f"(gw_sn={raw.get('gw_sn')!r} devtype={raw.get('devtype')!r} "
                            f"address={raw.get('address')!r})")
            continue
        if not name:
            problems.append(f"строка {lineno}: пустое target_name для {gw} {devtype} addr{addr}")
            continue
        key = (gw, devtype, addr)
        if key in seen:
            # Два имени на один адрес — карта противоречива. Берём ПЕРВОЕ и говорим об этом:
            # молча взять последнее значило бы тихо переименовать не то устройство.
            problems.append(f"строка {lineno}: адрес уже занят строкой {seen[key]} "
                            f"({gw} {devtype} addr{addr}) — взята первая")
            continue
        seen[key] = lineno
        rows.append({
            "gw_sn": gw, "devtype": devtype, "address": addr, "target_name": name,
            "role": raw.get("role", ""), "project_key": raw.get("project_key", ""),
            "group_id": raw.get("group_id", ""), "space": raw.get("space", ""),
            # ⚠ область адресуем ПО `area_id` (англ. `room_slug` из проекта): области заводятся
            # до нас, а сверка русских имён хрупка. `space` остаётся только для показа человеку.
            "area_id": raw.get("area_id", ""),
            "source": raw.get("source", ""), "note": raw.get("note", ""),
            "verify": raw.get("verify", "").strip().upper() in ("YES", "1", "TRUE", "ДА"),
            # 🔴 «опасное» — устройство отвалившееся либо с проблемой адреса (список объекта).
            # Имя предложить можно, но отмечать автоматически нельзя: адрес мог уехать.
            "danger": raw.get("danger", "").strip().upper() in ("YES", "1", "TRUE", "ДА"),
            "line": lineno,
        })
    return rows, problems


def gateways_in_map(rows: list[dict]) -> list[str]:
    """Серийники шлюзов, которые встречаются в карте (для подсказки в UI)."""
    return sorted({r["gw_sn"] for r in rows})


def _same_name(devtype: str, current: str, target: str) -> bool:
    """Уже названо так же? У датчиков сравниваем ТЕЛО имени.

    `rename` принимает и `ms_4_3_9`, и `4_3_9` (`sensor_body` режет префикс), а показываем мы
    всегда с префиксом. Без нормализации `ms_4_3_9` и `4_3_9` выглядели бы разными именами, и
    карточка предлагала бы «переименовать» уже названное.
    """
    cur, tgt = (current or "").strip(), (target or "").strip()
    if str(devtype) in SENSOR_TYPES:
        cur, tgt = sensor_body(cur), sensor_body(tgt)
    return cur.casefold() == tgt.casefold() and bool(cur)


def stitch(map_rows: list[dict], devices: list[dict], gw_sn: str) -> list[dict]:
    """Сшить карту со сканом ОДНОГО шлюза → строки таблицы для карточки.

    `devices` — снимок устройств шлюза в формате WS `devices` (devType/channel/address/
    name/devSn/zombie/orphan/key). Возвращаются ВСЕ три категории строк, потому что человеку
    важны не только совпадения: «в карте есть, на шине нет» — повод искать лампу, «на шине
    есть, в карте нет» — повод дописать карту.
    """
    gw = (gw_sn or "").upper()
    by_key = {(r["devtype"], r["address"]): r for r in map_rows if r["gw_sn"] == gw}
    used: set[tuple[str, int]] = set()
    out: list[dict] = []
    # Координаты, где на шине есть ДВИЖЕНИЕ: их люкс-половина переименуется вместе с ним и
    # отдельного имени не требует.
    # ⚠ v1.2.77: критерий — КООРДИНАТА, а не общий серийник. Факт железа (подтверждён
    # пользователем 2026-08-19): `0201` и `0202` всегда на одном адресе и под одним серийником,
    # без исключений. Координата опознаёт пару не хуже, но работает и там, где серийника нет
    # (DALI-1, адресный режим) или где он перекошен — иначе объект показал бы ~190 строк
    # мнимых проблем «люкс не в карте».
    motion_addr = {(d.get("channel"), d.get("address")) for d in devices
                   if _norm_devtype(d.get("devType")) == "0201"}

    # 1) идём от ЖЕЛЕЗА: что реально на шине
    for d in devices:
        devtype = _norm_devtype(d.get("devType"))
        addr = d.get("address")
        row = by_key.get((devtype, addr))
        if row:
            used.add((devtype, addr))
        current = d.get("name", "") or ""
        warn = []
        if devtype == "0202" and (d.get("channel"), d.get("address")) in motion_addr:
            out.append({
                "status": ST_PAIRED,
                "devType": devtype, "channel": d.get("channel"), "address": addr,
                "devSn": d.get("devSn", ""), "key": d.get("key", ""),
                "current_name": current, "target_name": "", "project_key": "",
                "space": "", "area_id": "", "area_current": "", "group_id": "",
                "source": "", "note": "",
                "verify": False, "danger": False, "same": False, "skip": True,
                "warn": ["имя придёт вместе с движением (одно устройство)"],
            })
            continue
        # осиротевший/зомби — НЕ предлагаем к применению: адрес указывает на нового жильца,
        # переименование ушло бы не туда (v1.2.2, тот же довод, что у «Забыть»)
        skip = bool(d.get("orphan") or d.get("zombie"))
        if d.get("orphan"):
            warn.append("осиротевший — его адрес занят другим устройством")
        elif d.get("zombie"):
            warn.append("не найден последним сканом")
        if not d.get("devSn"):
            # без серийника имя ляжет только в реестр HA и не переживёт сброс (v1.2.51)
            warn.append("нет devSn — имя не сохранится в нашем хранилище")
        out.append({
            "status": ST_MATCHED if row else ST_NOT_IN_MAP,
            "devType": devtype, "channel": d.get("channel"), "address": addr,
            "devSn": d.get("devSn", ""), "key": d.get("key", ""),
            "current_name": current,
            "target_name": (row or {}).get("target_name", ""),
            "project_key": (row or {}).get("project_key", ""),
            "space": (row or {}).get("space", ""),
            "area_id": (row or {}).get("area_id", ""),
            "area_current": "",          # текущая область устройства — подставит websocket_api
            "group_id": (row or {}).get("group_id", ""),
            "source": (row or {}).get("source", ""),
            "note": (row or {}).get("note", ""),
            "verify": bool((row or {}).get("verify")),
            "danger": bool((row or {}).get("danger")),
            "same": _same_name(devtype, current, (row or {}).get("target_name", "")),
            "skip": skip,
            "warn": warn,
        })

    # 2) остаток карты: адрес известен, а на шине не найден
    for (devtype, addr), row in sorted(by_key.items()):
        if (devtype, addr) in used:
            continue
        out.append({
            "status": ST_NOT_ON_BUS,
            "devType": devtype, "channel": None, "address": addr,
            "devSn": "", "key": "",
            "current_name": "",
            "target_name": row.get("target_name", ""),
            "project_key": row.get("project_key", ""),
            "space": row.get("space", ""),
            "area_id": row.get("area_id", ""),
            "area_current": "",
            "group_id": row.get("group_id", ""),
            "source": row.get("source", ""),
            "note": row.get("note", ""),
            "verify": bool(row.get("verify")),
            "danger": bool(row.get("danger")),
            "same": False,
            "skip": True,                      # применять нечему
            "warn": ["в карте есть, скан не нашёл"],
        })

    out.sort(key=lambda r: (str(r["devType"]), r["address"] if r["address"] is not None else -1))
    return out


def needs_work(row: dict) -> bool:
    """Нужна ли по этой строке РАБОТА: переименование ИЛИ простановка области.

    ⚠ v1.2.57. Раньше «к работе» = «имя не совпало», и строка с уже верным именем выпадала из
    отметок совсем. На офисном прогоне 2026-08-11 это стоило дорого: имена легли, а области —
    нет (старый фронт слал `area` вместо `area_id`), и второй заход применить их уже не мог:
    «там всё названо». На объекте вышло бы то же самое при любом повторном проходе.

    Область считаем нуждающейся в работе, только если карта её ЗАДАЁТ и она отличается от
    текущей (`area_current` подмешивает бэкенд из реестра HA; пусто = не знаем/нет).
    «Опасные» сюда не попадают — их отмечает человек (адрес мог уехать).
    """
    if row.get("status") != ST_MATCHED or row.get("skip") or row.get("danger"):
        return False
    if not row.get("same"):
        return True                                    # имя не совпало — обычная работа
    want = (row.get("area_id") or "").strip()
    return bool(want) and want != (row.get("area_current") or "").strip()


def unnamed(table: list[dict]) -> list[str]:
    """Устройства, которые ПОСЛЕ применения карты останутся без имени — строками для лога.

    Решение пользователя: всё неназванное должно попадать в журнал, а не только на экран.
    Сюда идут: то, чего нет в карте; то, что карта знает, а скан не нашёл; осиротевшие и
    зомби (их адрес принадлежит другому устройству); «опасные» — они не отмечаются сами.
    """
    out = []
    for r in table:
        if r["status"] == ST_PAIRED or r["same"]:
            continue                       # имя есть или придёт вместе с движением
        if r["status"] == ST_MATCHED and not r["skip"] and not r.get("danger"):
            continue                       # штатно применится
        why = ("нет в карте" if r["status"] == ST_NOT_IN_MAP else
               "в карте есть, скан не нашёл" if r["status"] == ST_NOT_ON_BUS else
               "опасное — отмечать вручную" if r.get("danger") else
               "; ".join(r.get("warn") or []) or "не применяется")
        out.append(f"{r['devType']} addr{r['address']}"
                   + (f" «{r['target_name']}»" if r["target_name"] else "")
                   + (f" (сейчас «{r['current_name']}»)" if r["current_name"] else "")
                   + f" — {why}")
    return out


def summary(table: list[dict]) -> dict:
    """Счётчики для шапки экрана (сколько применимо, сколько требует внимания)."""
    ready = [r for r in table
             if r["status"] == ST_MATCHED and not r["skip"] and not r["same"]
             and not r.get("danger")]
    return {
        "total": len(table),
        "matched": sum(1 for r in table if r["status"] == ST_MATCHED),
        "not_on_bus": sum(1 for r in table if r["status"] == ST_NOT_ON_BUS),
        "not_in_map": sum(1 for r in table if r["status"] == ST_NOT_IN_MAP),
        "paired": sum(1 for r in table if r["status"] == ST_PAIRED),
        "already": sum(1 for r in table if r["same"]),
        "verify": sum(1 for r in table if r["verify"] and r["status"] == ST_MATCHED),
        "danger": sum(1 for r in table if r.get("danger")),
        "ready": len(ready),
    }
