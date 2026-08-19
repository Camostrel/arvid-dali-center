#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""import_project.py — автоматическая пусконаладка объекта из нормализованного слоя (parquet).

ЧТО ДЕЛАЕТ (по фазам):
  1) PLAN   — offline: читает parquet + конфиг, раскладывает DALI-группы по шинам (частные зоны +
              общие группы помещений), считает бюджет 16/шину, планирует автояркость и панели,
              печатает отчёт и ВСЕ несостыковки. Ничего не пишет, HA не нужен.
  2..4) APPLY — (позже) через WS API HA: резолвит entity_id → gw_sn:channel:address у ЖИВОГО шлюза,
              создаёт группы / привязывает автояркость / привязывает панели.

МОДЕЛЬ (handoff_v2): entity_id уже посчитаны коллегой — берём готовые из parquet, не собираем.
Адрес из таблицы (X.Y.Z = этаж.шина.номер) используем ТОЛЬКО для offline-бюджета «сколько групп
на шину». Истину адреса для КОМАНД берём у живого шлюза по entity_id (закон проекта: адрес волатилен).

Шина = (этаж, шина) = один DALI-контроллер (у нас канал всегда 0, адрес = номер Z, 0..63).
DALI-групп на шину — 16 (0..15); частные и общие делят этот бюджет.

Запуск:
  python3 tools/import_project.py plan --normalized handoff_v2/handoff/sample
  python3 tools/import_project.py plan --normalized data/normalized --config tools/config_project.yaml
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from collections import defaultdict
from pathlib import Path

SCHEMA_VERSION_EXPECTED = 3
DALI_GROUPS_PER_BUS = 16          # 0..15
DALI_MAX_ADDRESS = 63             # короткие адреса DALI 0..63 (одна линия)

# Типы устройств DALI (для команд APPLY; offline — номинально по kind).
DEVTYPE_LAMP = "0101"             # реальный подтип резолвится у живого шлюза
DEVTYPE_LUX = "0202"             # датчик освещённости (恒照)

DALI_CHANNEL = 0                  # у наших шлюзов канал всегда 0 (одна DALI-линия)

# ── кодировка действий панели (1:1 с карточкой www/arvid-dali-panel.js _actionProp) ──
# Жест ЯЧЕЙКИ (dpid в addPanelObj): 1=клик (нажатие), 2=удержание. Действие — в property.
GESTURE_PRESS = 1
GESTURE_HOLD = 2


def action_property(action: str):
    """Действие → property (list) + mode. mode 129 = toggle (нужен setPanelArg на бэкенде)."""
    if action == "on":
        return [{"dpid": 20, "dataType": "bool", "value": True}], 255
    if action == "off":
        return [{"dpid": 20, "dataType": "bool", "value": False}], 255
    if action == "toggle":
        return [{"dpid": 20, "dataType": "bool", "value": True}], 129
    if action == "up":       # плавно ярче (удержание, dpid25)
        return [{"dpid": 25, "dataType": "bool", "value": True}], 255
    if action == "down":     # плавно темнее (dpid26)
        return [{"dpid": 26, "dataType": "bool", "value": True}], 255
    return None, 255         # нет действия → ячейку не трогаем


# ── загрузка нормализованного слоя ───────────────────────────────────────────

def load_layer(normalized: Path) -> dict:
    """Читаем parquet + проверяем schema_version. Возвращаем {devices, groups, spaces, meta}."""
    try:
        import pandas as pd
    except ImportError:
        sys.exit("Нужен pandas: pip install pandas pyarrow")

    meta_path = normalized / "normalized_meta.json"
    if meta_path.exists():
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        sv = meta.get("schema_version")
        if sv != SCHEMA_VERSION_EXPECTED:
            sys.exit(f"schema_version={sv}, ждём {SCHEMA_VERSION_EXPECTED} — проверь свой скрипт "
                     f"(набор колонок мог смениться).")
    else:
        meta = {}
        print(f"⚠ нет {meta_path.name} — версию схемы не проверить, продолжаю осторожно.")

    def rd(name):
        p = normalized / f"{name}.parquet"
        if not p.exists():
            sys.exit(f"нет файла {p}")
        return pd.read_parquet(p)

    return {"devices": rd("devices"), "groups": rd("groups"),
            "spaces": rd("spaces"), "meta": meta}


# ── мост entity_id → адрес (из devices, для offline-бюджета) ──────────────────

def build_addr_map(devices) -> dict:
    """entity_id → {floor, bus, num, kind}. Из devices.parquet. Учитывает и entity_id_2 (il)."""
    amap: dict[str, dict] = {}
    for _, r in devices.iterrows():
        rec = {"floor": int(r["addr_floor"]), "bus": int(r["addr_bus"]),
               "num": int(r["addr_num"]), "kind": r["kind"], "addr": r["addr"]}
        eid = r.get("entity_id")
        if isinstance(eid, str) and eid:
            amap[eid] = rec
        eid2 = r.get("entity_id_2")
        if isinstance(eid2, str) and eid2:
            amap[eid2] = rec
    return amap


def bus_of(entities: list[str], amap: dict) -> tuple:
    """Определить шину (floor, bus) по набору entity_id. Возврат (bus_key|None, missing, buses_set).
    bus_key = None, если сущности на РАЗНЫХ шинах или ничего не резолвится."""
    missing = [e for e in entities if e not in amap]
    buses = {(amap[e]["floor"], amap[e]["bus"]) for e in entities if e in amap}
    if len(buses) == 1:
        return next(iter(buses)), missing, buses
    return None, missing, buses


# ── планирование групп ───────────────────────────────────────────────────────

def plan_groups(layer: dict, cfg: dict, amap: dict) -> dict:
    """Разложить DALI-группы по шинам. Возврат:
       {buses: {bus_key: [group,...]}, warnings: [...], general_names: {space: entity}}.
    group = {name, kind, space, members(entity_id), bus_key, sensors_il, panels, dali_num}."""
    groups_df, spaces_df = layer["groups"], layer["spaces"]
    warnings: list[str] = []
    # СПИСОК ИСКЛЮЧЕНИЙ (v2026-08-12): `groups.skip: [имя, ...]` в конфиге. Нужен, когда на
    # контроллере 16 слотов кончились: человек решает, чем пожертвовать, а не программа
    # (принцип «без авто-деструктива»). Обычно это лестничная зона, целиком входящая в
    # кросс-общую того же помещения — её слот дублирует управление.
    skip_names = {str(n).strip() for n in (cfg.get("groups", {}) or {}).get("skip", []) if n}
    by_bus: dict[tuple, list[dict]] = defaultdict(list)
    cross: list[dict] = []            # группы, чьи лампы живут на РАЗНЫХ шлюзах

    # 1) ЧАСТНЫЕ ЗОНЫ (groups.parquet). Имя DALI-группы = group_id (→ light.<group_id>).
    zone_bus: dict[str, tuple] = {}
    for _, g in groups_df.iterrows():
        if str(g["group_id"]) in skip_names:
            warnings.append(f"зона {g['group_id']}: ИСКЛЮЧЕНА списком groups.skip — "
                            f"группа не создаётся, слот освобождён")
            continue
        lamps = list(g["lamps"])
        bus_key, missing, buses = bus_of(lamps, amap)
        if missing:
            warnings.append(f"зона {g['group_id']}: НЕ резолвятся лампы {missing} "
                            f"(нет в devices) — группа неполна")
        if bus_key is None:
            # КРОСС-ШЛЮЗОВАЯ зона (v1.2.40+): единого объекта в контроллере нет, но группа
            # делается одинаковым groupId+именем на КАЖДОМ участнике (docs/CROSS_GATEWAY.md).
            # Раньше такие просто выбрасывались — на объекте это 4 зоны и 21 общая.
            cross.append({
                "name": g["group_id"], "kind": "zone", "space": g["space"],
                "room_slug": g["room_slug"], "members": lamps, "buses": sorted(buses),
                "sensors_il": list(g["sensors_il"]), "sensors_ms": list(g["sensors_ms"]),
                "panels": list(g["panels"]), "space_type": g["space_type"], "dali_num": None,
            })
            continue
        zone_bus[g["group_id"]] = bus_key
        by_bus[bus_key].append({
            "name": g["group_id"], "kind": "zone", "space": g["space"],
            "room_slug": g["room_slug"],
            "members": lamps, "bus_key": bus_key,
            "sensors_il": list(g["sensors_il"]), "sensors_ms": list(g["sensors_ms"]),
            "panels": list(g["panels"]), "space_type": g["space_type"], "dali_num": None,
        })

    # 2) ОБЩИЕ ГРУППЫ помещений (spaces.parquet). Члены = объединение ламп всех зон помещения.
    general_names: dict[str, str] = {}
    if cfg.get("general_groups", {}).get("create", True):
        # ⚠ ДЕФОЛТ ИЗМЕНЁН 2026-08-12 (решение пользователя): общая группа нужна КАЖДОМУ
        # пространству, даже если зона в нём одна. Причина внешняя: общими группами управляет
        # СТОРОННЯЯ интеграция (ярлык `ba_area_light`), и «пространство без общей группы» для
        # неё дыра — свет помещения нечем адресовать. Экономия слота тут дешевле, чем
        # разнобой: где-то общая есть, где-то нет.
        # ⚠ Цена в слотах реальна: на Воронеже это +15 групп и второе переполнение (4.1).
        # Поэтому бюджет считается ниже жёстко, а лишнее видно в отчёте.
        skip_single = cfg["general_groups"].get("skip_if_single_zone", False)
        # ⚠ РЕШЕНИЕ ПОЛЬЗОВАТЕЛЯ 2026-08-13: общая группа нужна НЕ ВЕЗДЕ, а только там, где
        # ею реально пользуются — в рекреациях (на них вешается автояркость). Коридоры,
        # лестницы и залы управляются зонами. Пустой список = общие для ВСЕХ типов (прежнее
        # поведение), поэтому другие объекты не задеты.
        only_types = {str(t) for t in (cfg["general_groups"].get("only_space_types") or [])}
        for _, s in spaces_df.iterrows():
            zones = list(s["groups"])
            general_names[s["space"]] = s["general_light_entity"]
            if s["general_light_entity"].split(".", 1)[-1] in skip_names:
                warnings.append(f"общая группа {s['space']}: ИСКЛЮЧЕНА списком groups.skip")
                continue
            if only_types and str(s["space_type"]) not in only_types:
                continue  # тип помещения не в списке → общую не заводим (управление зонами)
            if skip_single and len(zones) <= 1:
                continue  # общая == единственная зона → отдельный слот не тратим
            # члены общей = все лампы зон помещения (из groups.parquet)
            lamps: list[str] = []
            for z in zones:
                row = groups_df[groups_df["group_id"] == z]
                if not row.empty:
                    lamps.extend(list(row.iloc[0]["lamps"]))
            bus_key, missing, buses = bus_of(lamps, amap)
            if bus_key is None:
                cross.append({
                    "name": s["general_light_entity"].split(".", 1)[-1], "kind": "general",
                    "space": s["space"], "room_slug": s["room_slug"], "members": lamps,
                    "buses": sorted(buses), "sensors_il": [], "sensors_ms": [], "panels": [],
                    "space_type": s["space_type"], "dali_num": None,
                })
                continue
            # имя DALI-группы = slug из general_light_entity (light.<slug> → просто <slug>)
            gname = s["general_light_entity"].split(".", 1)[-1]
            by_bus[bus_key].append({
                "name": gname, "kind": "general", "space": s["space"],
                "room_slug": s["room_slug"],
                "members": lamps, "bus_key": bus_key, "sensors_il": [], "sensors_ms": [],
                "panels": [], "space_type": s["space_type"], "dali_num": None,
            })

    # 3) НУМЕРАЦИЯ. 🔑 ПОРЯДОК: СНАЧАЛА КРОСС-ГРУППЫ, потом обычные (2026-08-12).
    #
    # Почему так, а не наоборот (как было): у обычной группы ограничение ОДНО — её номер
    # свободен на СВОЁЙ шине, и какой именно, ей безразлично. У кросс-группы номер обязан
    # совпасть у ВСЕХ участников, то есть она связана жёстче. Если сперва раздать номера
    # обычным, кросс-группе остаются РАЗНЫЕ огрызки на разных шинах, и общего номера может
    # не найтись даже при свободных слотах на каждой шине. Ровно это и вышло на Воронеже:
    # `1.10` свободна на 7–11, `5.1` — на 14–15, пересечения нет → четыре сквозные лестницы
    # оставались без номера при формально не переполненных шинах.
    #
    # Кросс берут номера СВЕРХУ (15, 14, …), обычные — снизу (0, 1, …): так они расходятся и
    # встречаются только когда контроллер реально забит под завязку — а это уже видно как
    # переполнение. Внутри кросс-групп первыми идут самые ЗАЖАТЫЕ (меньше всего общих
    # свободных номеров) — классическая жадность по наименьшей степени свободы.
    used: dict[tuple, set] = defaultdict(set)

    def _free_common(xg) -> list[int]:
        busy: set = set()
        for bk in xg["buses"]:
            busy |= used[tuple(bk)]
        return [n for n in range(DALI_GROUPS_PER_BUS) if n not in busy]

    # сортировка детерминированная: зажатость → больше участников → имя
    for xg in sorted(cross, key=lambda x: (len(_free_common(x)), -len(x["buses"]), x["name"])):
        free = _free_common(xg)
        if not free:
            warnings.append(f"кросс-группа {xg['name']}: на шинах {xg['buses']} нет НИ ОДНОГО "
                            f"общего свободного номера — НЕ СОЗДАЁТСЯ. Освободите слот "
                            f"(groups.skip) на одном из участников")
            continue
        xg["dali_num"] = free[-1]                 # сверху вниз
        for bk in xg["buses"]:
            used[tuple(bk)].add(xg["dali_num"])

    # 4) ОБЫЧНЫЕ группы добирают оставшиеся слоты своей шины.
    #
    # ПОРЯДОК ВАЖЕН — он решает, ЧТО отвалится при нехватке слотов (2026-08-12):
    #   1. ОБЩИЕ группы помещений — первыми. Требование пользователя: у пространства общая
    #      группа должна быть ВСЕГДА (ими управляет сторонняя интеграция по `ba_area_light`).
    #      Раньше первыми шли зоны, и на переполненных 4.1/5.1 отваливались именно общие —
    #      худший из возможных исходов.
    #   2. Зоны, ЦЕЛИКОМ входящие в кросс-общую своего помещения, — последними: их слот
    #      дублирует управление (лестница из двух ламп, которой и так рулит сквозная группа),
    #      поэтому если чем-то жертвовать, то ими.
    #   3. Остальные зоны — по имени (детерминизм).
    # ЧТО СЧИТАЕМ ДУБЛЕМ (уточнено 2026-08-13). Не «лампы зоны входят в общую» — под это
    # определение попадает ЛЮБАЯ зона, ведь общая по смыслу объединяет все зоны помещения.
    # Дубль — это когда общая группа НА ЭТОМ ЖЕ ШЛЮЗЕ не даёт ничего сверх зоны: её копия
    # здесь состоит ровно из тех же ламп. Тогда зонный слот тратится впустую.
    #   • лестница `501_0` (2 лампы на 5.1) — копия кросс-общей на 5.1 те же 2 лампы → ДУБЛЬ;
    #   • коридор `417_0` (6 ламп на 4.1) — копия кросс-общей на 4.1 это 18 ламп (все четыре
    #     зоны 417) → НЕ дубль, зона даёт дробное управление и снимать её нельзя.
    xg_here: dict[tuple, dict[str, set]] = {}
    for xg in cross:
        if xg["kind"] != "general" or xg["dali_num"] is None:
            continue
        for bk in xg["buses"]:
            here = {m for m in xg["members"] if amap.get(m)
                    and (amap[m]["floor"], amap[m]["bus"]) == tuple(bk)}
            xg_here.setdefault(tuple(bk), {}).setdefault(xg["space"], set()).update(here)

    def _is_dup(g, bus_key) -> bool:
        here = (xg_here.get(tuple(bus_key)) or {}).get(g["space"])
        return bool(here) and set(g["members"]) == here

    def _order(g) -> tuple:
        return (g["kind"] != "general", _is_dup(g, g["bus_key"]), g["name"])

    soft_collisions: list[str] = []               # (осталось для совместимости отчёта)
    for bus_key, grps in by_bus.items():
        for g in grps:
            g["dup_here"] = g["kind"] == "zone" and _is_dup(g, bus_key)
        grps.sort(key=_order)
        slots = [n for n in range(DALI_GROUPS_PER_BUS) if n not in used[bus_key]]
        if len(grps) > len(slots):
            names = [g["name"] for g in grps[len(slots):]]
            warnings.append(f"🔴 шина {bus_key[0]}.{bus_key[1]}: обычных групп {len(grps)}, "
                            f"свободных слотов {len(slots)} (остальные держат кросс-группы) — "
                            f"НЕ ВЛЕЗАЮТ: {names}. Сократите состав (groups.skip)")
        for g, num in zip(grps, slots):
            g["dali_num"] = num
            used[bus_key].add(num)

    if soft_collisions:
        warnings.append(f"ℹ {len(soft_collisions)} кросс-групп(ы) получили номер, совпадающий с "
                        f"номером обычной группы на ДРУГИХ шлюзах (у своих участников номер "
                        f"свободен — физически безопасно; риск только при будущей правке "
                        f"состава): {', '.join(soft_collisions)}")
    load: dict[tuple, dict] = {}
    for bk, grps in by_bus.items():
        load[bk] = {"own": len(grps), "cross": 0}
    for xg in cross:
        for bk in xg["buses"]:
            load.setdefault(tuple(bk), {"own": 0, "cross": 0})["cross"] += 1
    for bk, l in load.items():
        l["total"] = l["own"] + l["cross"]
        if l["total"] > DALI_GROUPS_PER_BUS:
            warnings.append(f"🔴 шина {bk[0]}.{bk[1]}: {l['total']} групп на "
                            f"{DALI_GROUPS_PER_BUS} слотов ({l['own']} своих + {l['cross']} "
                            f"кросс) — ЛИШНИЕ НЕ СОЗДАДУТСЯ. Сократите состав "
                            f"(groups.skip в конфиге)")
    return {"buses": dict(by_bus), "cross": cross, "warnings": warnings,
            "general_names": general_names, "load": load}


# ── планирование автояркости ─────────────────────────────────────────────────

def plan_autobright(plan: dict, cfg: dict, amap: dict) -> list[dict]:
    """Для зон с типом из конфига — привязка датчика il → группа зоны. Возврат списка привязок.
    ИСКЛЮЧЕНИЯ: exclude_spaces (подстроки имени) / exclude_floors (этажи целиком)."""
    ac = cfg.get("autobrightness", {})
    by_type = ac.get("by_space_type", {})
    excl_spaces = ac.get("exclude_spaces", []) or []
    excl_floors = set(ac.get("exclude_floors", []) or [])
    out: list[dict] = []
    for bus_key, grps in plan["buses"].items():
        floor = bus_key[0]
        for g in grps:
            if g["kind"] != "zone":
                continue
            st = g.get("space_type")
            params = by_type.get(st) if st else None
            if not params:
                continue                    # тип не в списке автояркости → пропуск
            if floor in excl_floors:
                continue                    # этаж исключён
            if any(x and x in g["space"] for x in excl_spaces):
                continue                    # помещение исключено
            if not g["sensors_il"]:
                continue                    # у zal и т.п. датчиков нет
            for il in g["sensors_il"]:
                out.append({
                    "sensor_il": il, "group_name": g["name"], "dali_num": g["dali_num"],
                    "bus_key": bus_key, "target": params["target_lux"],
                    "tol": params.get("tol", 50), "space_type": st,
                    "space": g["space"], "resolvable": il in amap,
                })
    return out


# ── планирование панелей ─────────────────────────────────────────────────────

def plan_panels(layer: dict, plan: dict, cfg: dict, amap: dict) -> list[dict]:
    """Разложить клавиши панелей по конфигу. Панель принадлежит зоне (parquet); мастер-клавиши →
    общая группа помещения, зонные клавиши → зоны помещения по порядку. Число клавиш известно
    только у ЖИВОГО шлюза → тут планируем НАМЕРЕНИЕ и требуемое число клавиш."""
    pcfg = cfg.get("panels", {})
    layout = pcfg.get("layout", {})
    spaces_df = layer["spaces"]
    # индекс: зона → её помещение и общая группа; помещение → его зоны
    zone_to_space: dict[str, str] = {}
    for bus_key, grps in plan["buses"].items():
        for g in grps:
            if g["kind"] == "zone":
                zone_to_space[g["name"]] = g["space"]

    def general_group_name(space: str) -> str | None:
        for bus_key, grps in plan["buses"].items():
            for g in grps:
                if g["kind"] == "general" and g["space"] == space:
                    return g["name"]
        # общей нет (single-zone или пропущена) → мастер целит саму единственную зону
        row = spaces_df[spaces_df["space"] == space]
        if not row.empty and len(list(row.iloc[0]["groups"])) == 1:
            return list(row.iloc[0]["groups"])[0]
        return None

    # раскладка: явные клавиши (числа) + необязательный 'rest' для незаданных выше последней явной
    explicit = {int(k): v for k, v in layout.items() if str(k).isdigit()}
    rest = layout.get("rest")
    max_explicit = max(explicit) if explicit else 0

    def _act(v):
        # YAML 1.1 читает on/off/yes/no как БУЛЕВЫ (press: on → True). Возвращаем к строкам.
        if v is True: return "on"
        if v is False: return "off"
        return v

    def resolve_target(tgt, own_zone, zones):
        tgt = tgt or "room"
        if tgt == "room":
            return general_group_name(zone_to_space.get(own_zone, ""))
        if tgt == "zone":
            return own_zone
        if tgt.startswith("zone") and tgt[4:].isdigit():   # zone1..zoneN
            i = int(tgt[4:]) - 1
            return zones[i] if 0 <= i < len(zones) else None
        return None

    out: list[dict] = []
    for _, g in layer["groups"].iterrows():
        for panel in list(g["panels"]):
            space = g["space"]
            zrow = spaces_df[spaces_df["space"] == space]
            zones = list(zrow.iloc[0]["groups"]) if not zrow.empty else []
            general = general_group_name(space)
            keys: list[dict] = []
            warns: list[str] = []
            for kno in sorted(explicit):                    # ЛЮБАЯ явно заданная клавиша
                spec = explicit[kno]
                tgt = resolve_target(spec.get("target", "room"), g["group_id"], zones)
                if tgt is None:
                    warns.append(f"кл{kno}: цель '{spec.get('target')}' не разрешилась"); continue
                keys.append({"key": kno, "press": _act(spec.get("press")), "hold": _act(spec.get("hold")),
                             "target_group": tgt})
            if rest:                                        # незаданные клавиши → зоны по порядку
                for i, z in enumerate(zones):
                    keys.append({"key": max_explicit + 1 + i, "press": _act(rest.get("press")),
                                 "hold": _act(rest.get("hold")), "target_group": z})
            out.append({
                "panel": panel, "space": space, "zone": g["group_id"], "general": general,
                "required_keys": max((k["key"] for k in keys), default=0),
                "keys": keys, "warns": warns, "resolvable": panel in amap,
            })
    return out


def plan_areas(plan: dict, cfg: dict) -> list[dict]:
    """HA-пространства (area) группам: entity группы → имя area = имя помещения.

    ⚠ Решение пользователя 2026-08-10: область нужна **всем** группам, а не только общим
    (раньше раздавали только общим). Зонные группы тоже живут в помещении, и без области их
    не найти ни фильтром, ни адресацией сервисов «по области» (docs/SERVICES.md).
    ⚠ Адресуем область по `area_id` = `room_slug` из паркета (`512_koridor`), а НЕ по видимому
    русскому имени: области заводятся до нас, а совпадение русских строк — вещь хрупкая
    (регистр, пробел, «ё»). `room_slug` есть у всех 65 помещений и уникален.
    """
    ar = cfg.get("areas", {})
    want_general = ar.get("assign_general", True)
    want_zone = ar.get("assign_zone", True)
    out = []
    for grps in list(plan["buses"].values()) + [plan.get("cross") or []]:
        for g in grps:
            if g["dali_num"] is None:
                continue                                   # не влезла в 16 — нечего назначать
            if g["kind"] == "general" and not want_general:
                continue
            if g["kind"] == "zone" and not want_zone:
                continue
            out.append({"entity": "light." + g["name"], "area_id": g["room_slug"],
                        "area_name": g["space"], "kind": g["kind"]})
    return out


# ── отчёт ────────────────────────────────────────────────────────────────────

PANEL_ACTIONS = {
    "on": "включить", "off": "выключить", "toggle": "переключить",
    "up": "плавно ярче (удержание)", "down": "плавно темнее (удержание)",
}
PANEL_TARGETS = {
    "room": "общая группа помещения панели",
    "zone": "своя зона панели (из таблицы)",
    "zone1..zoneN": "N-я зона помещения по порядку",
    "zone_by_order": "зоны помещения по порядку (только для 'rest')",
}


def report_caps(layer):
    """Список ДОСТУПНОГО (действия/цели панелей) + что ЕСТЬ в этом объекте (типы/этажи/помещения).
    JSON — чтобы коллега строил UI заполнения конфига по нему, а не по памяти."""
    sp = layer["spaces"]
    caps = {
        "panel_actions": PANEL_ACTIONS,
        "panel_targets": PANEL_TARGETS,
        "panel_max_keys": 8,
        "autobright_params": {"target_lux": "int, целевые люксы", "tol": "int, ±коридор",
                              "exclude_spaces": "list подстрок имени", "exclude_floors": "list этажей"},
        "space_types_available": sorted({str(x) for x in sp["space_type"] if x}),
        "floors_in_object": sorted({int(x) for x in sp["floor"]}),
        "spaces_in_object": sorted(sp["space"].tolist()),
    }
    print(json.dumps(caps, ensure_ascii=False, indent=2))


def report(layer, plan, autobright, panels, areas):
    m = layer["meta"].get("stats", {})
    print("=" * 78)
    print(f"ПЛАН ПУСКОНАЛАДКИ  (parquet: {m.get('devices','?')} устройств, "
          f"{m.get('groups','?')} зон, {m.get('spaces','?')} помещений)")
    print("=" * 78)

    print("\n── DALI-ГРУППЫ ПО ШИНАМ (бюджет 16/шину) ──")
    for bus_key in sorted(plan["buses"]):
        grps = plan["buses"][bus_key]
        used = sum(1 for g in grps if g["dali_num"] is not None)
        flag = "  ⚠ ПЕРЕПОЛНЕНИЕ" if len(grps) > DALI_GROUPS_PER_BUS else ""
        print(f"\n  шина {bus_key[0]}.{bus_key[1]}:  {used}/{DALI_GROUPS_PER_BUS} слотов{flag}")
        for g in grps:
            num = "‑‑" if g["dali_num"] is None else f"{g['dali_num']:2}"
            kind = "зона " if g["kind"] == "zone" else "ОБЩАЯ"
            print(f"    [{num}] {kind} {g['name']:32} ламп={len(g['members'])}  ({g['space']})")

    print("\n── АВТОЯРКОСТЬ (恒照) ──")
    if not autobright:
        print("  (нет привязок: нет помещений с настроенным типом или без датчиков il)")
    for b in autobright:
        r = "" if b["resolvable"] else "  ⚠ датчик не резолвится"
        print(f"  {b['sensor_il']:20} → группа {b['group_name']:14} "
              f"target={b['target']}±{b['tol']} lux  [{b['space_type']}]{r}")

    print("\n── ПАНЕЛИ ──")
    if not panels:
        print("  (нет панелей)")
    for p in panels:
        r = "" if p["resolvable"] else "  ⚠ панель не резолвится"
        print(f"  {p['panel']:18} помещение {p['space']} — нужно клавиш: {p['required_keys']}{r}")
        for k in sorted(p["keys"], key=lambda x: x["key"]):
            print(f"      кл{k['key']}: нажатие={str(k['press']):6} удерж={str(k['hold']):5} "
                  f"→ {k['target_group']}")
        for w in p.get("warns", []):
            print(f"      ⚠ {w}")

    xs = plan.get("cross") or []
    if xs:
        print(f"\n── КРОСС-ШЛЮЗОВЫЕ ГРУППЫ ({len(xs)}) — одинаковый id+имя на КАЖДОМ участнике ──")
        for g in xs:
            buses = ", ".join(f"{b[0]}.{b[1]}" for b in g["buses"])
            num = g["dali_num"] if g["dali_num"] is not None else "—"
            print(f"  [{num:>2}] {'ОБЩАЯ' if g['kind']=='general' else 'зона '} {g['name']:34} "
                  f"ламп={len(g['members'])}  шины: {buses}")
    print("\n── ПРОСТРАНСТВА HA (area) ──")
    if not areas:
        print("  (нет — общих групп нет или assign_general выключен)")
    for a in areas:
        print(f"  {a['entity']:34} → {a['area_id']}  ({a['area_name']})")

    load = plan.get("load") or {}
    if load:
        print("\n── БЮДЖЕТ ГРУПП (16 на контроллер: свои + участия в кросс-группах) ──")
        for bk in sorted(load):
            l = load[bk]
            mark = ("  🔴 ПЕРЕПОЛНЕНИЕ" if l["total"] > DALI_GROUPS_PER_BUS
                    else "  ⚠ впритык" if l["total"] == DALI_GROUPS_PER_BUS else "")
            print(f"  {bk[0]}.{bk[1]:<4} {l['total']:2}/{DALI_GROUPS_PER_BUS} "
                  f"({l['own']} своих + {l['cross']} кросс){mark}")
        # КАНДИДАТЫ НА СОКРАЩЕНИЕ для перегруженных: зона, ВСЕ лампы которой уже входят в
        # кросс-общую того же помещения — её слот дублирует управление. Решает человек:
        # мы только показываем (принцип «без авто-деструктива»).
        over = [bk for bk, l in load.items() if l["total"] > DALI_GROUPS_PER_BUS]
        # шины, из-за которых кросс-группа осталась БЕЗ НОМЕРА, — такие же кандидаты на
        # разгрузку: там слотов не хватило именно для сквозной группы
        for xg in plan.get("cross") or []:
            if xg["dali_num"] is None:
                over += [tuple(b) for b in xg["buses"]]
        over = sorted(set(over))
        if over:
            print("\n  КАНДИДАТЫ НА СОКРАЩЕНИЕ (общая группа НА ЭТОМ ЖЕ шлюзе состоит из тех"
                  " же ламп — зонный слот ничего не добавляет):")
            found = False
            for bk in sorted(over):
                for g in plan["buses"].get(bk, []):
                    if g["kind"] == "zone" and g.get("dup_here"):
                        found = True
                        print(f"    {bk[0]}.{bk[1]}: {g['name']:28} "
                              f"{len(g['members'])} ламп — «{g['space']}»")
            if not found:
                print("    (нет: все зоны дают управление, которого нет у общей — "
                      "сокращать придётся осмысленно)")
    print("\n── ЗАМЕЧАНИЯ ──")
    if not plan["warnings"]:
        print("  ✅ нет — всё сходится")
    for w in plan["warnings"]:
        print(f"  ⚠ {w}")
    print()


# ── WS-клиент HA (aiohttp на боксе, иначе websockets) ─────────────────────────

class HAClient:
    """Тонкий клиент Home Assistant WebSocket API: auth + вызов команд по id."""

    def __init__(self, url: str, token: str):
        self._url = url
        self._token = token
        self._id = 0
        self._backend = None
        self._ws = None
        self._session = None

    async def __aenter__(self):
        try:
            import aiohttp
            self._backend = "aiohttp"
            self._session = aiohttp.ClientSession()
            self._ws = await self._session.ws_connect(self._url, heartbeat=30)
        except ImportError:
            import websockets
            self._backend = "websockets"
            self._ws = await websockets.connect(self._url, max_size=8 * 1024 * 1024)
        await self._recv()                       # auth_required
        await self._send({"type": "auth", "access_token": self._token})
        auth = await self._recv()
        if auth.get("type") != "auth_ok":
            raise RuntimeError(f"HA auth не прошёл: {auth}")
        return self

    async def __aexit__(self, *exc):
        if self._backend == "aiohttp":
            await self._ws.close()
            await self._session.close()
        else:
            await self._ws.close()

    async def _send(self, obj):
        if self._backend == "aiohttp":
            await self._ws.send_json(obj)
        else:
            await self._ws.send(json.dumps(obj))

    async def _recv(self):
        if self._backend == "aiohttp":
            msg = await self._ws.receive()
            return json.loads(msg.data)
        return json.loads(await self._ws.recv())

    async def cmd(self, type_: str, **kwargs) -> dict:
        """Отправить команду, дождаться result по своему id. Возврат result или бросок."""
        self._id += 1
        mid = self._id
        await self._send({"id": mid, "type": type_, **kwargs})
        while True:
            m = await self._recv()
            if m.get("id") != mid:
                continue                          # чужие события (подписки) — пропускаем
            if m.get("type") != "result":
                continue
            if not m.get("success"):
                raise RuntimeError(f"{type_}: {m.get('error')}")
            return m.get("result", {})


async def resolve_entities(client: HAClient) -> dict:
    """entity_id → {gw_sn, devType, channel, address, key_count?}. Из живого WS `devices`."""
    gws = await client.cmd("arvid_dali_center/gateways")
    emap: dict[str, dict] = {}
    for g in gws.get("gateways", []) or []:
        gw = g.get("gwSn")
        if not gw:
            continue
        dv = await client.cmd("arvid_dali_center/devices", gw_sn=gw)
        for d in dv.get("devices", []) or []:
            rec = {"gw_sn": gw, "devType": str(d.get("devType")),
                   "channel": d.get("channel"), "address": d.get("address")}
            for role, eid in (d.get("entities") or {}).items():
                if eid:
                    emap[eid] = rec
    return emap


PANEL_KEY_COUNT = {"0302": 2, "0304": 4, "0306": 6, "0308": 8}


# ── фазы записи (dry-run по умолчанию) ────────────────────────────────────────

async def apply_groups(client, plan, emap, apply):
    """create_group на каждую группу плана. Члены резолвим по entity_id у живого шлюза."""
    done = fail = 0
    for bus_key in sorted(plan["buses"]):
        for g in plan["buses"][bus_key]:
            if g["dali_num"] is None:
                print(f"  ПРОПУСК {g['name']}: нет слота (переполнение шины)"); fail += 1; continue
            members, gw_set, miss = [], set(), []
            for eid in g["members"]:
                r = emap.get(eid)
                if not r:
                    miss.append(eid); continue
                members.append({"devType": r["devType"], "channel": r["channel"],
                                "address": r["address"]})
                gw_set.add(r["gw_sn"])
            if miss:
                print(f"  ⚠ {g['name']}: не резолвятся {miss} — ПРОПУСК"); fail += 1; continue
            if len(gw_set) != 1:
                print(f"  ⚠ {g['name']}: лампы на разных шлюзах {gw_set} — ПРОПУСК"); fail += 1; continue
            gw = next(iter(gw_set))
            args = dict(gw_sn=gw, channel=DALI_CHANNEL, groupId=g["dali_num"],
                        name=g["name"], members=members)
            if not apply:
                print(f"  [dry] create_group {g['name']} id={g['dali_num']} "
                      f"gw={gw} ламп={len(members)}"); done += 1; continue
            try:
                await client.cmd("arvid_dali_center/create_group", **args)
                print(f"  ✅ {g['name']} id={g['dali_num']} ({len(members)} ламп)"); done += 1
            except Exception as e:
                print(f"  ❌ {g['name']}: {e}"); fail += 1
    print(f"группы: {done} готово, {fail} пропущено/ошибок")


async def apply_autobright(client, autobright, plan, emap, apply):
    """set_lux_keep: датчик il → группа зоны. Группа уже создана (фаза групп раньше)."""
    done = fail = 0
    for b in autobright:
        r = emap.get(b["sensor_il"])
        if not r:
            print(f"  ⚠ {b['sensor_il']}: не резолвится — ПРОПУСК"); fail += 1; continue
        if b["dali_num"] is None:
            print(f"  ⚠ {b['group_name']}: группа без слота — ПРОПУСК"); fail += 1; continue
        args = dict(gw_sn=r["gw_sn"], devType=r["devType"], channel=r["channel"],
                    address=r["address"],
                    group={"channel": DALI_CHANNEL, "groupId": b["dali_num"]},
                    target=b["target"], tol=b["tol"])
        if not apply:
            print(f"  [dry] set_lux_keep {b['sensor_il']} → {b['group_name']} "
                  f"({b['target']}±{b['tol']})"); done += 1; continue
        try:
            await client.cmd("arvid_dali_center/set_lux_keep", **args)
            print(f"  ✅ {b['sensor_il']} → {b['group_name']}"); done += 1
        except Exception as e:
            print(f"  ❌ {b['sensor_il']}: {e}"); fail += 1
    print(f"автояркость: {done} готово, {fail} пропущено/ошибок")


def _group_num_map(plan) -> dict:
    """имя группы → dali_num (для целей панелей)."""
    return {g["name"]: g["dali_num"] for grps in plan["buses"].values() for g in grps}


async def apply_panels(client, panels, plan, emap, apply):
    """add_panel_obj по клавишам. Каждая клавиша = до 2 вызовов (нажатие + удержание)."""
    gnum = _group_num_map(plan)
    done = fail = 0
    for p in panels:
        r = emap.get(p["panel"])
        if not r:
            print(f"  ⚠ {p['panel']}: не резолвится — ПРОПУСК"); fail += 1; continue
        kc = PANEL_KEY_COUNT.get(r["devType"], 0)
        if p["required_keys"] > kc:
            print(f"  ⚠ {p['panel']}: нужно {p['required_keys']} клавиш, у панели {kc} — "
                  f"лишние клавиши плана не привязываю")
        for k in p["keys"]:
            if k["key"] > kc:
                continue
            gid = gnum.get(k["target_group"])
            if gid is None:
                print(f"  ⚠ {p['panel']} кл{k['key']}: цель {k['target_group']} без слота — ПРОПУСК")
                fail += 1; continue
            out_base = {"gwSnObj": r["gw_sn"], "devType": "0401",
                        "channel": DALI_CHANNEL, "address": gid}
            for action, gesture in ((k["press"], GESTURE_PRESS), (k["hold"], GESTURE_HOLD)):
                if not action:
                    continue
                prop, mode = action_property(action)
                if prop is None:
                    continue
                out = dict(out_base, property=prop)
                args = dict(gw_sn=r["gw_sn"], devType=r["devType"], channel=r["channel"],
                            address=r["address"], keyNo=k["key"], dpid=gesture,
                            panelType=2, mode=mode, replace=True, outObj=[out])
                if not apply:
                    print(f"  [dry] {p['panel']} кл{k['key']} "
                          f"{'нажатие' if gesture == 1 else 'удерж'}={action} → {k['target_group']}")
                    done += 1; continue
                try:
                    await client.cmd("arvid_dali_center/add_panel_obj", **args)
                    done += 1
                except Exception as e:
                    print(f"  ❌ {p['panel']} кл{k['key']} {action}: {e}"); fail += 1
        if apply:
            print(f"  ✅ {p['panel']}")
    print(f"панели: {done} действий, {fail} пропущено/ошибок")


async def run_apply(args, plan, autobright, panels, emap_from):
    """Онлайн-фазы. emap_from — уже готовый резолвер (или None → резолвим сами)."""
    token = args.ha_token or os.environ.get("HA_TOKEN")
    if not token:
        sys.exit("Нужен токен HA: --ha-token или переменная окружения HA_TOKEN")
    async with HAClient(args.ha_url, token) as client:
        emap = await resolve_entities(client)
        print(f"резолвер: {len(emap)} сущностей с боевого шлюза\n")
        mode = "ЗАПИСЬ" if args.apply else "dry-run (ничего не пишу)"
        print(f"режим: {mode}\n")
        if args.phase in ("groups", "all"):
            print("── ГРУППЫ ──"); await apply_groups(client, plan, emap, args.apply)
        if args.phase in ("autobright", "all"):
            print("\n── АВТОЯРКОСТЬ ──"); await apply_autobright(client, autobright, plan, emap, args.apply)
        if args.phase in ("panels", "all"):
            print("\n── ПАНЕЛИ ──"); await apply_panels(client, panels, plan, emap, args.apply)


_EMIT_TEMPLATE = r'''#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""АВТО-СГЕНЕРИРОВАН `import_project.py emit` — самодостаточный скрипт пусконаладки объекта.

НАЗНАЧЕНИЕ: по зашитому ниже плану (GROUPS/AUTOBRIGHT/PANELS) создаёт DALI-группы, привязывает
автояркость и кнопочные панели ЧЕРЕЗ WS API Home Assistant. План читается глазами — это ровно то,
что будет применено. Зависимостей НЕТ (WebSocket-клиент на stdlib) — запускается в терминале HA.

━━ КАК НАСТРОИТЬ ━━
  1) HA_TOKEN — токен долгого действия HA (профиль → низ страницы). Из терминала:
        export HA_TOKEN="ey..."
     Из КАРТОЧКИ окружение не задать (shell_command идёт без шелла) — заполни ВИДИМЫЙ файл
     /config/tools/arvid_apply.conf одной строкой:  ha_token: eyJhbGciOi...
  2) HA_URL (ниже / флаг --ha-url) — адрес HA. В аддоне «Terminal & SSH» localhost часто НЕ ядро,
     тогда: ws://homeassistant:8123/api/websocket  или  ws://<IP бокса>:8123/api/websocket

━━ КАК ЗАПУСТИТЬ ━━
    python3 @@NAME@@                     # DRY-RUN: показать, что будет (НИЧЕГО не пишет)
    python3 @@NAME@@ --apply             # применить всё
    python3 @@NAME@@ --apply --only groups     # по фазе: groups | autobright | panels
    python3 @@NAME@@ --apply --force           # пересоздать группы, даже если HA их уже знает

━━ ПОВТОРНЫЙ ЗАПУСК (идемпотентно) ━━
  ГРУППЫ пропускаются, если HA УЖЕ ЗНАЕТ такую группу тем же составом (проверка по состоянию HA,
  не по контроллеру). Удалил группу в карточке → пересоздастся. АВТОЯРКОСТЬ/ПАНЕЛИ применяются
  каждый раз (у них нет HA-сущности, del+add идемпотентен). Транзиентный сбой прогрева — с ретраем.
"""
import argparse, base64, json, os, signal, socket, ssl, struct, sys, time

# Журнал читают ВО ВРЕМЯ прогона (кнопка «Журнал» в карточке), а python при выводе в файл
# буферизует блоками по 4–8 КБ — без этого строки долетали бы пачками, с минутным лагом, и
# прогресс выглядел бы «зависшим». Обёртка дешевле, чем помнить про flush в каждой печати.
import functools
print = functools.partial(print, flush=True)


# ═══════════════ МЯГКАЯ ОСТАНОВКА ═══════════════
# Прогон объекта идёт минутами, и человеку нужно уметь его прервать. Убивать процесс жёстко
# НЕЛЬЗЯ: создание группы — это `delGroup` + `addGroup`, и обрыв между ними оставит группу
# СНЕСЁННОЙ (закон «без авто-деструктива»: нельзя оставлять объект в состоянии, которого никто
# не выбирал). Поэтому сигнал только ПОДНИМАЕТ ФЛАГ, а выход происходит МЕЖДУ записями.
STOP = {"on": False}


def _on_stop(signum, frame):
    if not STOP["on"]:
        STOP["on"] = True
        print("\n⛔ ПОЛУЧЕН СИГНАЛ ОСТАНОВКИ — доканчиваю текущую запись и выхожу…", flush=True)


signal.signal(signal.SIGTERM, _on_stop)
signal.signal(signal.SIGINT, _on_stop)


def stopped(phase: str, done: int, total: int) -> bool:
    """Проверка в начале каждой итерации: пора ли выйти. Печатает, на чём остановились."""
    if STOP["on"]:
        print(f"⛔ ОСТАНОВЛЕНО ОПЕРАТОРОМ: {phase} — сделано {done} из {total}, "
              f"остальное НЕ применялось", flush=True)
        return True
    return False

# ═══════════════ НАСТРОЙКА ═══════════════
# Адрес HA. В терминале аддона «Terminal & SSH» localhost обычно НЕ ядро HA — тогда укажи
# ws://homeassistant:8123/... или ws://<IP бокса>:8123/... (можно и флагом --ha-url).
HA_URL = "@@URL@@"
# Токен долгого действия HA. Три источника, по убыванию приоритета:
#   1) флаг --ha-token
#   2) переменная окружения HA_TOKEN (export HA_TOKEN="...") — для запуска из терминала
#   3) ФАЙЛ КОНСТАНТ рядом с этим скриптом (см. ниже) — так запускает КАРТОЧКА
#
# ⚠ Почему файл, а не окружение: `shell_command` в HA выполняется БЕЗ ШЕЛЛА — строка
# разбирается на «программу + аргументы», поэтому «HA_TOKEN=... python3 ...» падает
# ([Errno 2] No such file or directory: 'eyJ...'), как и `$(cat ...)`. Токен аргументом
# (`--ha-token`) виден в `ps` любому на боксе.
# ⚠ Почему рядом со скриптом и БЕЗ ТОЧКИ в имени: файл заполняет ЧЕЛОВЕК, значит он обязан
# быть виден в File Editor / Studio Code Server (правило пользователя 2026-08-11). Скрытые
# файлы эти аддоны не показывают вовсе.
# ⚠ Почему не внутри скрипта: скрипт СОБИРАЕТСЯ `import_project.py emit` и при следующей
# сборке плана перезаписывается — вписанный внутрь токен потерялся бы.
CONF_FILE = "arvid_apply.conf"          # ищется рядом со скриптом, затем в /config/tools/
# ═════════════════════════════════════════

# ── ПЛАН (зашит; прочитай глазами — это ровно то, что будет создано) ──
# План (JSON, читается глазами — это ровно то, что будет применено). Парсится json.loads в рантайме,
# поэтому это валидный JSON (null/true/false), а не Python-литерал.
GROUPS = json.loads(r"""@@GROUPS@@""")
AUTOBRIGHT = json.loads(r"""@@AUTOBRIGHT@@""")
PANELS = json.loads(r"""@@PANELS@@""")
AREAS = json.loads(r"""@@AREAS@@""")   # [{entity, area_id (room_slug), area_name, kind}]
# КРОСС-ШЛЮЗОВЫЕ группы: лампы на РАЗНЫХ контроллерах. Пишутся отдельной командой
# (create_cross_group) — обычная create_group адресует ОДИН шлюз и собрала бы половину.
CROSS = json.loads(r"""@@CROSS@@""")   # [{name, dali_num, members:[entity_id]}]

DALI_CHANNEL = 0
GESTURE_PRESS, GESTURE_HOLD = 1, 2


def read_conf() -> dict:
    """Прочитать файл констант (`ha_token`, опц. `ha_url`). Формат — простые `ключ: значение`,
    `#` — комментарий. Без зависимостей: скрипт запускается в терминале HA как есть.

    Ищем рядом со скриптом, затем в /config/tools — чтобы работало и при запуске из другого
    каталога (карточка зовёт `python3 /config/tools/apply_*.py` из рабочего каталога HA)."""
    here = os.path.dirname(os.path.abspath(__file__))
    out = {}
    for path in (os.path.join(here, CONF_FILE), os.path.join("/config/tools", CONF_FILE)):
        try:
            with open(path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#") or ":" not in line:
                        continue
                    k, _, v = line.partition(":")
                    out.setdefault(k.strip().lower(), v.strip().strip('"').strip("'"))
        except OSError:
            continue
        if out:
            return out
    return out


def action_property(action):
    if action == "on":     return [{"dpid": 20, "dataType": "bool", "value": True}], 255
    if action == "off":    return [{"dpid": 20, "dataType": "bool", "value": False}], 255
    if action == "toggle": return [{"dpid": 20, "dataType": "bool", "value": True}], 129
    if action == "up":     return [{"dpid": 25, "dataType": "bool", "value": True}], 255
    if action == "down":   return [{"dpid": 26, "dataType": "bool", "value": True}], 255
    return None, 255


class WS:
    """Минимальный синхронный WebSocket-клиент (stdlib), под HA WS API."""
    def __init__(self, url):
        sec = url.startswith("wss://")
        host_path = url.split("://", 1)[1]
        hostport, _, path = host_path.partition("/")
        host, _, port = hostport.partition(":")
        self.host, self.port = host, int(port or (443 if sec else 80))
        self.path, self.sec, self.buf, self._id = "/" + path, sec, b"", 0

    def connect(self):
        s = socket.create_connection((self.host, self.port), timeout=15)
        if self.sec:
            s = ssl.create_default_context().wrap_socket(s, server_hostname=self.host)
        self.sock = s
        key = base64.b64encode(os.urandom(16)).decode()
        s.sendall((f"GET {self.path} HTTP/1.1\r\nHost: {self.host}:{self.port}\r\n"
                   "Upgrade: websocket\r\nConnection: Upgrade\r\n"
                   f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode())
        resp = b""
        while b"\r\n\r\n" not in resp:
            resp += s.recv(4096)
        if b" 101 " not in resp.split(b"\r\n", 1)[0]:
            raise RuntimeError("WS handshake отклонён: " + resp[:80].decode("latin1"))
        self.buf = resp.split(b"\r\n\r\n", 1)[1]

    def _send(self, obj):
        pl = json.dumps(obj).encode()
        b = bytearray([0x81]); m = os.urandom(4); n = len(pl)
        if n < 126: b.append(0x80 | n)
        elif n < 65536: b.append(0x80 | 126); b += struct.pack("!H", n)
        else: b.append(0x80 | 127); b += struct.pack("!Q", n)
        b += m; b += bytes(c ^ m[i % 4] for i, c in enumerate(pl))
        self.sock.sendall(bytes(b))

    def _frame(self):
        while True:
            d = self.buf
            if len(d) >= 2:
                op = d[0] & 0x0f; ln = d[1] & 0x7f; off = 2
                if ln == 126 and len(d) >= 4: ln = struct.unpack("!H", d[2:4])[0]; off = 4
                elif ln == 127 and len(d) >= 10: ln = struct.unpack("!Q", d[2:10])[0]; off = 10
                elif ln >= 126: off = None
                if off is not None and len(d) >= off + ln:
                    pl = d[off:off + ln]; self.buf = d[off + ln:]
                    if op == 0x8: raise RuntimeError("WS закрыт сервером")
                    if op == 0x9: self._pong(pl); continue    # ping → pong
                    if op == 0xA: continue                    # pong → игнор
                    return pl
            ch = self.sock.recv(65536)
            if not ch: raise RuntimeError("WS соединение оборвано")
            self.buf += ch

    def _pong(self, payload):
        b = bytearray([0x8A]); m = os.urandom(4); n = len(payload)
        b.append(0x80 | n); b += m; b += bytes(c ^ m[i % 4] for i, c in enumerate(payload))
        self.sock.sendall(bytes(b))

    def _recv(self):
        return json.loads(self._frame())

    def auth(self, token):
        self._recv()                              # auth_required
        self._send({"type": "auth", "access_token": token})
        if self._recv().get("type") != "auth_ok":
            raise RuntimeError("HA не принял токен")

    def cmd(self, type_, **kw):
        self._id += 1; mid = self._id
        self._send({"id": mid, "type": type_, **kw})
        while True:
            m = self._recv()
            if m.get("id") != mid or m.get("type") != "result":
                continue
            if not m.get("success"):
                raise RuntimeError(f"{type_}: {m.get('error')}")
            return m.get("result", {})


def resolve(ws):
    emap = {}
    for g in ws.cmd("arvid_dali_center/gateways").get("gateways", []) or []:
        gw = g.get("gwSn")
        if not gw: continue
        for d in ws.cmd("arvid_dali_center/devices", gw_sn=gw).get("devices", []) or []:
            rec = {"gw_sn": gw, "devType": str(d.get("devType")),
                   "channel": d.get("channel"), "address": d.get("address")}
            for _role, eid in (d.get("entities") or {}).items():
                if eid: emap[eid] = rec
    return emap


PANEL_KEYS = {"0302": 2, "0304": 4, "0306": 6, "0308": 8}
_TRANSIENT = ("empty_group", "timeout", "no_response", "busy")
PACE = 0.3        # пауза между командами ЗАПИСИ (сек) — даёт шине осесть; меняется флагом --pace


def apply_cmd(ws, type_, tries=4, delay=2.0, **kw):
    """Команда ЗАПИСИ + ретрай на ТРАНЗИЕНТ: исключение (timeout/…) ИЛИ ok=false (шлюз не
    подтвердил — прогрев/забитая шина). Возврат result ПОСЛЕДНЕЙ попытки (может быть {ok:false}).
    Скрипт сам решает по res['ok']/res['verify'], что печатать — НЕ выдаёт «OK» вслепую."""
    last = {"ok": False, "error": "нет ответа"}
    for i in range(tries):
        try:
            res = ws.cmd(type_, **kw)
        except RuntimeError as e:
            last = {"ok": False, "error": str(e)}
            if i < tries - 1 and any(t in str(e).lower() for t in _TRANSIENT):
                time.sleep(delay); continue
            return last
        last = res
        if res.get("ok") is not False:    # ok=true ИЛИ команда без поля ok (HA-core) → готово
            return res
        if i < tries - 1:                 # ok=false → шлюз не ack, транзиент → повтор с паузой
            time.sleep(delay)
    return last


def _ha_groups(ws, gw, cache):
    """Группы, которые ВИДИТ HA (ws_groups читает hub.groups — КЕШ HA, не контроллер): скольким
    (channel, groupId) какой состав (channel, address). Дёшево, отражает удаление в карточке."""
    if gw not in cache:
        m = {}
        for eg in ws.cmd("arvid_dali_center/groups", gw_sn=gw).get("groups", []) or []:
            m[(eg["channel"], eg["groupId"])] = {(x.get("channel"), x.get("address"))
                                                 for x in eg.get("members", []) or []}
        cache[gw] = m
    return cache[gw]


def do_groups(ws, emap, apply, force, pace):
    """Группы: пропуск, если HA УЖЕ ЗНАЕТ группу тем же составом (кеш HA, не контроллер). Иначе
    create_group + ПРОВЕРКА: ok (ack) и verify.match (перечитка состава с контроллера)."""
    ok = warn = bad = skip = 0; cache = {}
    total = len(GROUPS)
    for i, g in enumerate(GROUPS, 1):
        if stopped("группы", i - 1, total): break
        members, gws, miss = [], set(), []
        for eid in g["members"]:
            r = emap.get(eid)
            if not r: miss.append(eid); continue
            members.append({"devType": r["devType"], "channel": r["channel"], "address": r["address"]})
            gws.add(r["gw_sn"])
        if miss: print(f"  [{i}/{total}] ПРОПУСК {g['name']}: не найдены {miss}"); bad += 1; continue
        if len(gws) != 1: print(f"  [{i}/{total}] ПРОПУСК {g['name']}: лампы на разных шлюзах {gws}"); bad += 1; continue
        gw = next(iter(gws))
        want = {(m["channel"], m["address"]) for m in members}
        if not force and _ha_groups(ws, gw, cache).get((DALI_CHANNEL, g["dali_num"])) == want:
            print(f"  [{i}/{total}] = {g['name']} id={g['dali_num']}: HA уже знает тем же составом, пропуск"); skip += 1; continue
        if not apply:
            print(f"  [{i}/{total}] [dry] create_group {g['name']} id={g['dali_num']} gw={gw} ламп={len(members)}"); ok += 1; continue
        res = apply_cmd(ws, "arvid_dali_center/create_group", gw_sn=gw, channel=DALI_CHANNEL,
                        groupId=g["dali_num"], name=g["name"], members=members)
        v = res.get("verify") or {}
        if not res.get("ok"):
            print(f"  [{i}/{total}] ❌ {g['name']} id={g['dali_num']}: НЕ подтверждено ({res.get('error') or 'шлюз не ack'})"); bad += 1
        elif v.get("match") is False:
            print(f"  [{i}/{total}] ⚠ {g['name']} id={g['dali_num']}: записано, но сверка состава НЕ совпала "
                  f"(недобавл={v.get('missing')}, лишние={v.get('extra')})"); warn += 1
        else:
            print(f"  [{i}/{total}] ✅ {g['name']} id={g['dali_num']} ({len(members)} ламп) подтверждено"); ok += 1
        time.sleep(pace)
    if apply:
        print(f"группы: ✅{ok} подтв., ⚠{warn} без сверки, ❌{bad} НЕ подтв., ={skip} уже в HA")
    else:
        print(f"группы (dry-run): {ok} будет создано, ={skip} уже в HA, {bad} пропущено "
              f"(проверка ok/verify — при --apply)")


def _ha_cross_groups(ws, cache):
    """Кросс-группы, которые ВИДИТ HA (`cross_groups` читает CrossGroupStore — наш персист).

    Тот же принцип, что у обычных групп: проверяем по состоянию HA, а не опросом контроллера
    (память шлюза недостоверна и прогревается — закон 2). Ключ — (channel, groupId, имя),
    значение — состав как множество (шлюз, канал, адрес)."""
    if "x" not in cache:
        m = {}
        for g in ws.cmd("arvid_dali_center/cross_groups").get("groups", []) or []:
            key = (g.get("channel"), g.get("groupId"), g.get("name") or "")
            m[key] = {(str(x.get("gwSnObj") or "").upper(), x.get("channel"), x.get("address"))
                      for x in g.get("members", []) or []}
        cache["x"] = m
    return cache["x"]


def do_cross_groups(ws, emap, apply, force, pace):
    """КРОСС-ШЛЮЗОВЫЕ группы: один и тот же `groupId`+имя на КАЖДОМ участнике, у каждого свои
    лампы (docs/CROSS_GATEWAY.md §2). Обычной `create_group` такую не собрать — она пишет один
    контроллер, и вторая половина света осталась бы вне группы.

    Бэкенд (`create_cross_group`) сам проверяет, что номер свободен У ВСЕХ участников, и сам
    ведёт `unique_id` — здесь мы только резолвим состав и честно печатаем, что легло."""
    if not CROSS:
        return
    ok = warn = bad = skip = 0
    cache = {}
    total = len(CROSS)
    for i, g in enumerate(CROSS, 1):
        if stopped("кросс-группы", i - 1, total): break
        members, gws, miss = [], set(), []
        for eid in g["members"]:
            r = emap.get(eid)
            if not r:
                miss.append(eid); continue
            members.append({"gwSnObj": r["gw_sn"], "devType": r["devType"],
                            "channel": r["channel"], "address": r["address"]})
            gws.add(r["gw_sn"])
        if miss:
            print(f"  [{i}/{total}] ПРОПУСК {g['name']}: не найдены {miss}"); bad += 1; continue
        # ГЕЙТ: участников меньше двух — это обычная группа, и создавать её надо create_group.
        # Такое бывает, когда лампы «разных шин» по проекту физически сидят на одном шлюзе.
        if len(gws) < 2:
            print(f"  [{i}/{total}] ПРОПУСК {g['name']}: лампы на ОДНОМ контроллере ({gws}) — это не "
                  f"кросс-группа, проверьте таблицу линия→шлюз"); bad += 1; continue
        want = {(str(m["gwSnObj"]).upper(), m["channel"], m["address"]) for m in members}
        have = _ha_cross_groups(ws, cache).get((DALI_CHANNEL, g["dali_num"], g["name"]))
        if not force and have == want:
            print(f"  [{i}/{total}] = {g['name']} id={g['dali_num']}: HA уже знает тем же составом, пропуск")
            skip += 1; continue
        if not apply:
            print(f"  [{i}/{total}] [dry] create_cross_group {g['name']} id={g['dali_num']} "
                  f"шлюзов={len(gws)} ламп={len(members)}"); ok += 1; continue
        res = apply_cmd(ws, "arvid_dali_center/create_cross_group", channel=DALI_CHANNEL,
                        groupId=g["dali_num"], name=g["name"], members=members)
        # ответ: {ok, results:[{gw, ok, verify}], warnings, uid, participants}
        results = res.get("results") or []
        bad_gw = [r.get("gw") for r in results if not r.get("ok")]
        unverified = [r.get("gw") for r in results
                      if r.get("ok") and (r.get("verify") or {}).get("match") is False]
        if not res.get("ok"):
            print(f"  [{i}/{total}] ❌ {g['name']} id={g['dali_num']}: НЕ подтверждено "
                  f"({res.get('error') or 'не ack: ' + ', '.join(map(str, bad_gw))})"); bad += 1
        elif unverified or res.get("warnings"):
            print(f"  [{i}/{total}] ⚠ {g['name']} id={g['dali_num']}: записано, но "
                  + (f"состав не сверился на {unverified}" if unverified else "")
                  + ("; ".join(res.get("warnings") or []))); warn += 1
        else:
            print(f"  [{i}/{total}] ✅ {g['name']} id={g['dali_num']} ({len(members)} ламп на "
                  f"{len(res.get('participants') or gws)} контроллерах) подтверждено"); ok += 1
        time.sleep(pace)
    if apply:
        print(f"кросс-группы: ✅{ok} подтв., ⚠{warn} без сверки, ❌{bad} НЕ подтв., ={skip} уже в HA")
    else:
        print(f"кросс-группы (dry-run): {ok} будет создано, ={skip} уже в HA, {bad} пропущено")


def do_autobright(ws, emap, apply, pace):
    """Автояркость (нет HA-сущности → применяем каждый раз). ПРОВЕРКА: ok (ack) + verify (readSensor
    показал наш luxRange)."""
    ok = warn = bad = 0
    total = len(AUTOBRIGHT)
    for i, b in enumerate(AUTOBRIGHT, 1):
        if stopped("автояркость", i - 1, total): break
        r = emap.get(b["sensor_il"])
        if not r: print(f"  [{i}/{total}] ПРОПУСК {b['sensor_il']}: не найден"); bad += 1; continue
        if not apply:
            print(f"  [{i}/{total}] [dry] set_lux_keep {b['sensor_il']} → группа id={b['dali_num']} ({b['target']}±{b['tol']})"); ok += 1; continue
        want = [max(0, b["target"] - b["tol"]), b["target"] + b["tol"]]
        res = apply_cmd(ws, "arvid_dali_center/set_lux_keep", gw_sn=r["gw_sn"], devType=r["devType"],
                        channel=r["channel"], address=r["address"],
                        group={"channel": DALI_CHANNEL, "groupId": b["dali_num"]}, target=b["target"], tol=b["tol"])
        entries = res.get("verify") or []
        seen = any(e.get("dpid") == 3 and e.get("luxRange") == want and e.get("outputObj") for e in entries)
        if not res.get("ok"):
            print(f"  [{i}/{total}] ❌ {b['sensor_il']}: НЕ подтверждено ({res.get('error') or 'шлюз не ack'})"); bad += 1
        elif not seen:
            print(f"  [{i}/{total}] ⚠ {b['sensor_il']} → id={b['dali_num']}: записано, но перечитка не показала luxRange {want}"); warn += 1
        else:
            print(f"  [{i}/{total}] ✅ {b['sensor_il']} → id={b['dali_num']} подтверждено"); ok += 1
        time.sleep(pace)
    if apply:
        print(f"автояркость: ✅{ok} подтв., ⚠{warn} без сверки, ❌{bad} НЕ подтв.")
    else:
        print(f"автояркость (dry-run): {ok} будет применено, {bad} пропущено — dry-run НЕ читает "
              f"текущее состояние (у автояркости нет HA-сущности); проверка ok/verify — при --apply")


def do_panels(ws, emap, apply, pace):
    """Панели (нет HA-сущности → применяем каждый раз). ПРОВЕРКА по каждому действию: ok (ack) +
    verify.match (readPanel показал нашу цель). ✅ считаем тихо, печатаем только ⚠/❌ и итог."""
    ok = warn = bad = 0
    total = len(PANELS)
    for i, p in enumerate(PANELS, 1):
        if stopped("панели", i - 1, total): break
        r = emap.get(p["panel"])
        if not r: print(f"  [{i}/{total}] ПРОПУСК {p['panel']}: не найдена"); bad += 1; continue
        kc = PANEL_KEYS.get(r["devType"], 0)
        for k in p["keys"]:
            if k["key"] > kc:
                print(f"  [{i}/{total}] ⚠ {p['panel']} кл{k['key']}: у панели {kc} клавиш — пропуск"); warn += 1; continue
            out = {"gwSnObj": r["gw_sn"], "devType": "0401", "channel": DALI_CHANNEL, "address": k["dali_num"]}
            for action, gesture in ((k["press"], GESTURE_PRESS), (k["hold"], GESTURE_HOLD)):
                if not action: continue
                prop, mode = action_property(action)
                if prop is None: continue
                lbl = f"{p['panel']} кл{k['key']} {'нажатие' if gesture == 1 else 'удерж'}={action}"
                if not apply:
                    print(f"  [{i}/{total}] [dry] {lbl} → группа id={k['dali_num']}"); ok += 1; continue
                res = apply_cmd(ws, "arvid_dali_center/add_panel_obj", gw_sn=r["gw_sn"], devType=r["devType"],
                                channel=r["channel"], address=r["address"], keyNo=k["key"], dpid=gesture,
                                panelType=2, mode=mode, replace=True, outObj=[dict(out, property=prop)])
                v = res.get("verify") or {}
                if not res.get("ok"):
                    print(f"  [{i}/{total}] ❌ {lbl}: НЕ подтверждено ({res.get('error') or 'шлюз не ack'})"); bad += 1
                elif v.get("match") is False:
                    print(f"  [{i}/{total}] ⚠ {lbl}: записано, но цель НЕ привязалась (missing={v.get('missing')})"); warn += 1
                else:
                    ok += 1
                time.sleep(pace)
    if apply:
        print(f"панели: ✅{ok} подтв. действий, ⚠{warn} без сверки, ❌{bad} НЕ подтв.")
    else:
        print(f"панели (dry-run): {ok} действий будет применено, {warn} предупр. — dry-run НЕ читает "
              f"текущее состояние; проверка ok/verify — при --apply")


def do_areas(ws, apply):
    """HA-area группам (v2: ВСЕМ, не только общим; адресация по `area_id`).

    ⚠ Область ищем по `area_id` = `room_slug` из проекта (`512_koridor`), а не по видимому
    русскому имени: области создаются ДО нас, и сверять русские строки хрупко. Отсутствующую
    НЕ создаём — это сигнал, что состав областей разошёлся с проектом, и решать это должен
    человек, а не скрипт (иначе рядом с «512_koridor» тихо появится дубль).
    Назначение — HA-side (`entity_registry/update`), шину не трогает и прогрева не требует.
    """
    if not AREAS: return
    have = {a.get("area_id") for a in (ws.cmd("config/area_registry/list") or [])}
    ok = bad = 0
    total = len(AREAS)
    for i, it in enumerate(AREAS, 1):
        if stopped("пространства", i - 1, total): break
        aid = it["area_id"]
        if aid not in have:
            print(f"  [{i}/{total}] ⚠ {it['entity']}: области «{aid}» ({it['area_name']}) в HA НЕТ — пропуск")
            bad += 1
            continue
        if not apply:
            print(f"  [{i}/{total}] [dry] {it['entity']} → {aid}"); ok += 1; continue
        res = apply_cmd(ws, "config/entity_registry/update", entity_id=it["entity"], area_id=aid)
        assigned = (res.get("entity_entry") or {}).get("area_id") == aid
        print(f"  [{i}/{total}] {'OK' if assigned else '!'} {it['entity']} → {aid}"
              + ("" if assigned else " (назначение не подтвердилось)")); ok += 1
    print(f"пространства: {ok} обработано, {bad} без области")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="реально писать (иначе dry-run)")
    ap.add_argument("--force", action="store_true", help="группы пересоздавать, даже если HA их знает")
    ap.add_argument("--only", choices=["groups", "areas", "autobright", "panels"], help="одна фаза")
    ap.add_argument("--pace", type=float, default=PACE, help=f"пауза между записями, сек (умолч. {PACE})")
    ap.add_argument("--ha-url", default=HA_URL)
    ap.add_argument("--ha-token", default=os.environ.get("HA_TOKEN", ""))
    a = ap.parse_args()
    conf = read_conf()
    if not a.ha_token:                       # фолбэк: файл констант (так зовёт карточка)
        a.ha_token = conf.get("ha_token", "")
    if conf.get("ha_url") and a.ha_url == HA_URL:
        a.ha_url = conf["ha_url"]            # адрес ядра тоже настраиваемый, но не обязателен
    if not a.ha_token:
        sys.exit(f"Нужен токен HA. Варианты: --ha-token, env HA_TOKEN или файл {CONF_FILE} "
                 f"рядом со скриптом / в /config/tools со строкой:\n  ha_token: eyJhbGciOi...")
    ws = WS(a.ha_url); ws.connect(); ws.auth(a.ha_token)
    emap = resolve(ws)
    print("прогон: мягкая остановка поддерживается (SIGTERM между записями)")
    print(f"резолвер: {len(emap)} сущностей; режим: "
          + ("ЗАПИСЬ" if a.apply else "DRY-RUN (ничего не пишу)")
          + (" · FORCE" if a.force else "") + f" · пауза {a.pace}с\n")
    # ⛔ между фазами тоже проверяем флаг: остановили на группах — в автояркость не лезем
    if a.only in (None, "groups") and not STOP["on"]:
        print("── ГРУППЫ ──");     do_groups(ws, emap, a.apply, a.force, a.pace)
    if a.only in (None, "groups") and CROSS and not STOP["on"]:
        print("\n── КРОСС-ШЛЮЗОВЫЕ ГРУППЫ ──"); do_cross_groups(ws, emap, a.apply, a.force, a.pace)
    if a.only in (None, "areas") and not STOP["on"]:
        print("\n── ПРОСТРАНСТВА (area) ──"); do_areas(ws, a.apply)
    if a.only in (None, "autobright") and not STOP["on"]:
        print("\n── АВТОЯРКОСТЬ ──"); do_autobright(ws, emap, a.apply, a.pace)
    if a.only in (None, "panels") and not STOP["on"]:
        print("\n── ПАНЕЛИ ──");     do_panels(ws, emap, a.apply, a.pace)
    # Последняя строка журнала — итог прогона. Карточка ищет именно её, чтобы отличить
    # «остановлено человеком» от «упало само» (во втором случае строки просто нет).
    if STOP["on"]:
        print("\n⛔ ПРОГОН ОСТАНОВЛЕН ОПЕРАТОРОМ — незаписанное осталось незаписанным, "
              "повторный запуск продолжит с того же места (готовые группы пропускаются)")
        sys.exit(3)
    print("\n✅ ПРОГОН ЗАВЕРШЁН")


if __name__ == "__main__":
    main()
'''


def emit_script(plan, autobright, panels, areas, out_path: Path, url: str):
    """Сгенерировать самодостаточный скрипт с зашитым планом."""
    gnum = _group_num_map(plan)
    groups = [{"name": g["name"], "dali_num": g["dali_num"], "members": g["members"]}
              for grps in plan["buses"].values() for g in grps if g["dali_num"] is not None]
    ab = [{"sensor_il": b["sensor_il"], "dali_num": b["dali_num"],
           "target": b["target"], "tol": b["tol"]}
          for b in autobright if b["dali_num"] is not None]
    pn = []
    for p in panels:
        keys = [{"key": k["key"], "press": k["press"], "hold": k["hold"],
                 "dali_num": gnum.get(k["target_group"])}
                for k in p["keys"] if gnum.get(k["target_group"]) is not None]
        pn.append({"panel": p["panel"], "keys": keys})
    # кросс-группы: только те, кому планировщик нашёл номер, свободный у ВСЕХ участников
    cross = [{"name": g["name"], "dali_num": g["dali_num"], "members": g["members"]}
             for g in (plan.get("cross") or []) if g["dali_num"] is not None]
    skipped_cross = [g["name"] for g in (plan.get("cross") or []) if g["dali_num"] is None]
    body = (_EMIT_TEMPLATE
            .replace("@@NAME@@", out_path.name)
            .replace("@@CROSS@@", json.dumps(cross, ensure_ascii=False, indent=2))
            .replace("@@URL@@", url)
            .replace("@@GROUPS@@", json.dumps(groups, ensure_ascii=False, indent=2))
            .replace("@@AUTOBRIGHT@@", json.dumps(ab, ensure_ascii=False, indent=2))
            .replace("@@PANELS@@", json.dumps(pn, ensure_ascii=False, indent=2))
            .replace("@@AREAS@@", json.dumps(areas, ensure_ascii=False, indent=2)))
    out_path.write_text(body, encoding="utf-8")
    print(f"✅ скрипт создан: {out_path}")
    print(f"   групп {len(groups)}, кросс-групп {len(cross)}, автояркость {len(ab)}, "
          f"панелей {len(pn)}, пространств {len(areas)}")
    # обычные группы, которым не хватило слота, — тоже НЕ молчим: без номера они не создаются
    skipped_regular = [g["name"] for grps in plan["buses"].values() for g in grps
                       if g["dali_num"] is None]
    if skipped_regular:
        print(f"   ⚠ БЕЗ НОМЕРА и потому НЕ вошли (обычные): {skipped_regular} — на контроллере "
              f"кончились слоты, см. раздел «БЮДЖЕТ ГРУПП» в отчёте plan")
    if skipped_cross:
        # НЕ молчим о срезанном: без номера группа не создастся, и это надо видеть
        print(f"   ⚠ БЕЗ НОМЕРА и потому НЕ вошли: {skipped_cross} — бюджет 16 групп "
              f"исчерпан у участников, см. отчёт plan")
    print(f"   запуск: export HA_TOKEN=... ; python3 {out_path.name}  (dry-run) → --apply (запись)")


def report_names(layer):
    """Чек-лист ОЖИДАЕМЫХ имён (entity_id) по шинам — под что называть устройства пусконаладкой.
    Имя лампы l_<этаж>_<шина>_<номер>; датчик — ms_/il_<…>; панель — kp_<…>. Адрес = что в таблице."""
    dev = layer["devices"]
    by_bus = defaultdict(lambda: {"lamp": [], "sensor": [], "panel": []})
    for _, r in dev.iterrows():
        bus = (int(r["addr_floor"]), int(r["addr_bus"]))
        eid = r.get("entity_id")
        eid2 = r.get("entity_id_2")
        label = f"{r['addr']:8} → {eid}" + (f"  +  {eid2}" if isinstance(eid2, str) and eid2 else "")
        by_bus[bus][r["kind"]].append(label)
    print("=" * 70)
    print("ОЖИДАЕМЫЕ ИМЕНА (называй устройства пусконаладкой ровно так)")
    print("=" * 70)
    for bus in sorted(by_bus):
        b = by_bus[bus]
        print(f"\n── шина {bus[0]}.{bus[1]} "
              f"(ламп {len(b['lamp'])}, датчиков {len(b['sensor'])}, панелей {len(b['panel'])}) ──")
        for kind, title in (("lamp", "ЛАМПЫ"), ("sensor", "ДАТЧИКИ (движение + освещённость)"),
                            ("panel", "ПАНЕЛИ")):
            if b[kind]:
                print(f"  {title}:")
                for s in b[kind]:
                    print(f"    {s}")
    print()


def main():
    ap = argparse.ArgumentParser(description="Автопусконаладка объекта из parquet (handoff_v2).")
    ap.add_argument("phase", choices=["names", "caps", "plan", "emit", "groups", "autobright", "panels", "all"],
                    help="names — имена; caps — доступные действия/цели (JSON для UI); plan — раскладка; "
                    "emit — сгенерировать скрипт; groups/autobright/panels/all — запись напрямую через WS")
    ap.add_argument("--out", type=Path, help="emit: куда писать скрипт (по умолч. apply_project.py)")
    ap.add_argument("--normalized", type=Path, required=True,
                    help="каталог с *.parquet (или sample из handoff)")
    ap.add_argument("--config", type=Path, default=Path(__file__).parent / "config_project.yaml")
    ap.add_argument("--apply", action="store_true", help="реально писать (иначе dry-run)")
    ap.add_argument("--space", default="", help="тест на ОДНОМ помещении: подстрока space "
                    "(напр. '104' или '103_Вестибюль'). Нумерация групп — как у полного объекта.")
    ap.add_argument("--ha-url", default="ws://localhost:8123/api/websocket")
    ap.add_argument("--ha-token", default="", help="long-lived token HA (или env HA_TOKEN)")
    args = ap.parse_args()

    import yaml
    cfg = yaml.safe_load(args.config.read_text(encoding="utf-8")) if args.config.exists() else {}

    layer = load_layer(args.normalized)
    if args.phase == "names":
        report_names(layer)
        return
    if args.phase == "caps":
        report_caps(layer)
        return
    amap = build_addr_map(layer["devices"])
    plan = plan_groups(layer, cfg, amap)              # нумерация — по ВСЕМУ объекту (id стабильны)
    autobright = plan_autobright(plan, cfg, amap)
    panels = plan_panels(layer, plan, cfg, amap)
    areas = plan_areas(plan, cfg)

    if args.space:                                    # ФИЛЬТР для теста на одной комнате
        s = args.space
        for bus in list(plan["buses"]):
            plan["buses"][bus] = [g for g in plan["buses"][bus] if s in g["space"]]
            if not plan["buses"][bus]:
                del plan["buses"][bus]
        autobright = [b for b in autobright if s in b.get("space", "")]
        panels = [p for p in panels if s in p["space"]]
        areas = [a for a in areas if s in a["area"]]
        print(f"⚠ ФИЛЬТР: только помещение(я) со «{s}» (нумерация групп — как у полного объекта)\n")

    if args.phase == "plan":
        report(layer, plan, autobright, panels, areas)
        return
    if args.phase == "emit":
        out = args.out or (Path(__file__).parent / "apply_project.py")
        # адрес HA для скрипта: из конфига (ha.url), иначе флаг --ha-url
        url = (cfg.get("ha") or {}).get("url") or args.ha_url
        emit_script(plan, autobright, panels, areas, out, url)
        return
    asyncio.run(run_apply(args, plan, autobright, panels, None))


if __name__ == "__main__":
    main()
