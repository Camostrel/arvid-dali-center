#!/usr/bin/env python3
"""Анализатор проектной таблицы объекта (ДО импорта в DALI).

Считает бюджет DALI-групп по линиям и ловит проблемы, из-за которых импорт упадёт.
Ничего не пишет и никуда не ходит — только читает CSV. Запускать до `import_project.py`.

    python3 tools/analyze_project_table.py project.csv
    python3 tools/analyze_project_table.py project.csv --encoding utf-8

Правила (согласованы с docs/NAMING_AND_PROJECT_TABLE.md):
- DALI-группа = МИНИМУМ 2 лампы. Однолам́повая «группа» — ошибка проекта (лампой управляем напрямую).
- На DALI-линию доступно ТОЛЬКО 16 групп (id 0..15).
- У помещения свои подгруппы (каждая со своим датчиком) + «комнатная» группа со всеми лампами.
  Если в помещении РОВНО ОДНА подгруппа и она покрывает все лампы помещения — комнатная = она же
  (отдельный group_id не тратится).
"""
from __future__ import annotations

import argparse
import csv
import sys
from collections import defaultdict

DALI_GROUPS_PER_LINE = 16          # id 0..15 — жёсткий предел DALI

# Excel превращает «1.4» в дату «01.апр» → декодируем русские сокращения месяцев.
_RU_MONTH = {"янв": 1, "фев": 2, "мар": 3, "апр": 4, "май": 5, "июн": 6,
             "июл": 7, "авг": 8, "сен": 9, "окт": 10, "ноя": 11, "дек": 12}


def parse_addr(raw: str):
    """`1.20.15` → (1, 20, 15). Чинит Excel-порчу `01.07.2013` → (1, 7, 13).

    Excel читает «1.7.13» как дату 01.07.2013, а «1.12.30» как 01.12.1930
    (двузначный год: 00-29 → 2000-е, 30-99 → 1900-е). Возвращает None если не адрес.
    """
    s = (raw or "").strip()
    if not s or s.lower() in ("нет", "-", "—"):
        return None
    parts = s.split(".")
    if len(parts) != 3:
        return None
    try:
        a, b, c = (int(p) for p in parts)
    except ValueError:
        return None
    if c >= 1930:                                  # год → это Excel-порча, восстанавливаем адрес
        c = c - 2000 if c >= 2000 else c - 1900
    return (a, b, c)


def parse_line(raw: str):
    """Номер линии. `22` → [22]; `22/21` → [22, 21]; Excel `01.апр` → [1, 4]."""
    s = (raw or "").strip()
    if not s:
        return []
    if "/" in s:
        return [int(p) for p in s.split("/") if p.strip().isdigit()]
    if s.isdigit():
        return [int(s)]
    parts = s.split(".")                           # Excel-дата «01.апр» = 1.4
    if len(parts) == 2:
        d, mon = parts[0].strip(), parts[1].strip().lower()[:3]
        if d.isdigit() and mon in _RU_MONTH:
            return [int(d), _RU_MONTH[mon]]
    return []


def read_rows(path: str, encodings: list[str]):
    """Читает CSV, пробуя кодировки (по умолчанию cp866 — типичная выгрузка из Excel RU)."""
    for enc in encodings:
        try:
            with open(path, encoding=enc, newline="") as f:
                return list(csv.DictReader(f, delimiter=";")), enc
        except (UnicodeDecodeError, LookupError):
            continue
    sys.exit(f"не удалось прочитать {path} ни в одной из кодировок: {encodings}")


def main() -> int:
    ap = argparse.ArgumentParser(description="Анализ проектной таблицы перед импортом в DALI")
    ap.add_argument("csv", help="путь к проектной таблице (;-разделитель)")
    ap.add_argument("--encoding", action="append", dest="encodings",
                    help="кодировка (можно повторять); по умолчанию cp866, utf-8-sig, cp1251")
    args = ap.parse_args()

    rows, enc = read_rows(args.csv, args.encodings or ["cp866", "utf-8-sig", "cp1251"])
    print(f"# файл: {args.csv} (кодировка {enc}), строк: {len(rows)}\n")

    # ── разбор: Excel оставляет пустыми повторяющиеся ячейки → протягиваем вниз ──
    cur = {"floor": "", "room": "", "line": "", "group": ""}
    groups: dict[tuple, dict] = {}     # (line, group) → {lamps, sensor, panel, room, floor}
    rooms: dict[tuple, set] = defaultdict(set)   # (line, room) → {group}
    warns: list[str] = []

    for i, r in enumerate(rows, 2):                # 2 = номер строки в файле (с шапкой)
        for src, key in (("floor", "floor"), ("name", "room"), ("dali_line", "line"), ("group", "group")):
            v = (r.get(src) or "").strip()
            if v:
                cur[key] = v
        lamp_raw = (r.get("Lamp") or "").strip()
        if not lamp_raw or not cur["group"]:
            continue
        lamp = parse_addr(lamp_raw)
        if not lamp:
            warns.append(f"стр.{i}: не разобрал адрес лампы {lamp_raw!r} (группа {cur['group']})")
            continue
        lines = parse_line(cur["line"])
        line = lamp[1]                             # линия берём ИЗ АДРЕСА — он надёжнее колонки
        if lines and line not in lines:
            warns.append(f"стр.{i}: адрес {lamp_raw} на линии {line}, а колонка dali_line={cur['line']!r}")

        key = (line, cur["group"])
        g = groups.setdefault(key, {"lamps": [], "sensor": None, "panel": None,
                                    "room": cur["room"], "floor": cur["floor"]})
        g["lamps"].append(lamp)
        for col, fld in (("sensor", "sensor"), ("key_panel", "panel")):
            a = parse_addr(r.get(col) or "")
            if a and not g[fld]:
                g[fld] = a
        rooms[(line, cur["room"])].add(cur["group"])

    # ── бюджет групп по линиям ──
    print(f"{'линия':>5} {'подгрупп':>9} {'1-лампов':>9} {'помещ.':>7} {'комнатных':>10} {'ИТОГО id':>9}  статус")
    print("-" * 72)
    over, singles_total = [], 0
    for line in sorted({k[0] for k in groups}):
        gs = {k[1]: v for k, v in groups.items() if k[0] == line}
        valid = {n: v for n, v in gs.items() if len(v["lamps"]) >= 2}
        singles = {n: v for n, v in gs.items() if len(v["lamps"]) < 2}
        singles_total += len(singles)
        rms = {r: subs for (l, r), subs in rooms.items() if l == line}
        # комнатная группа нужна, если в помещении >1 ВАЛИДНОЙ подгруппы, либо есть лампы вне подгрупп
        room_ids = 0
        for r, subs in rms.items():
            v = [s for s in subs if s in valid]
            if len(v) > 1 or (len(v) == 1 and len(subs) > 1) or len(v) == 0:
                room_ids += 1
        total = len(valid) + room_ids
        bad = total > DALI_GROUPS_PER_LINE
        if bad:
            over.append((line, total))
        mark = f"❌ ПЕРЕПОЛНЕНИЕ (+{total - DALI_GROUPS_PER_LINE})" if bad else "ok"
        print(f"{line:>5} {len(valid):>9} {len(singles):>9} {len(rms):>7} {room_ids:>10} "
              f"{total:>4}/{DALI_GROUPS_PER_LINE}  {mark}")

    # ── нарушения правила «группа ≥ 2 ламп» ──
    if singles_total:
        print(f"\n## Однолам́повые группы ({singles_total}) — по правилу проекта их быть не должно:")
        for (line, name), g in sorted(groups.items()):
            if len(g["lamps"]) < 2:
                print(f"  линия {line}: {name} ({g['room']}) — 1 лампа {g['lamps'][0]}")

    if warns:
        print(f"\n## Предупреждения ({len(warns)}):")
        for w in warns[:40]:
            print(f"  {w}")
        if len(warns) > 40:
            print(f"  … ещё {len(warns) - 40}")

    print(f"\nИТОГ: групп(валидных) {sum(1 for g in groups.values() if len(g['lamps']) >= 2)}, "
          f"однолам́повых {singles_total}, линий {len({k[0] for k in groups})}")
    if over:
        print(f"❌ Переполнены линии: {', '.join(f'{l} ({t}/16)' for l, t in over)}")
        return 1
    print("✅ Все линии влезают в 16 DALI-групп")
    return 0


if __name__ == "__main__":
    sys.exit(main())
