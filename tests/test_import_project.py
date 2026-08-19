# -*- coding: utf-8 -*-
"""Тесты чистой логики import_project (кодировка действий панели). Stdlib, без HA/pandas.

Кодировка property стережёт совместимость с картой www/arvid-dali-panel.js `_actionProp`:
если она разъедется — панели, привязанные скриптом, будут делать не то, что кнопки в UI.
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

import import_project as ip  # noqa: E402


class TestActionProperty(unittest.TestCase):
    def test_on(self):
        prop, mode = ip.action_property("on")
        self.assertEqual(prop, [{"dpid": 20, "dataType": "bool", "value": True}])
        self.assertEqual(mode, 255)

    def test_off(self):
        prop, mode = ip.action_property("off")
        self.assertEqual(prop, [{"dpid": 20, "dataType": "bool", "value": False}])
        self.assertEqual(mode, 255)

    def test_toggle_uses_mode_129(self):
        # toggle: property как «вкл», отличие — mode 129 (setPanelArg на бэкенде)
        prop, mode = ip.action_property("toggle")
        self.assertEqual(prop, [{"dpid": 20, "dataType": "bool", "value": True}])
        self.assertEqual(mode, 129)

    def test_hold_up_down(self):
        self.assertEqual(ip.action_property("up")[0], [{"dpid": 25, "dataType": "bool", "value": True}])
        self.assertEqual(ip.action_property("down")[0], [{"dpid": 26, "dataType": "bool", "value": True}])

    def test_empty_action_no_property(self):
        prop, mode = ip.action_property(None)
        self.assertIsNone(prop)


class TestBusResolve(unittest.TestCase):
    def test_bus_of_single(self):
        amap = {"light.a": {"floor": 1, "bus": 1, "num": 5},
                "light.b": {"floor": 1, "bus": 1, "num": 6}}
        bus, missing, buses = ip.bus_of(["light.a", "light.b"], amap)
        self.assertEqual(bus, (1, 1))
        self.assertEqual(missing, [])

    def test_bus_of_split(self):
        amap = {"light.a": {"floor": 1, "bus": 1, "num": 5},
                "light.b": {"floor": 1, "bus": 2, "num": 6}}
        bus, missing, buses = ip.bus_of(["light.a", "light.b"], amap)
        self.assertIsNone(bus)                       # разные шины → одна DALI-группа невозможна
        self.assertEqual(len(buses), 2)

    def test_bus_of_missing(self):
        amap = {"light.a": {"floor": 1, "bus": 1, "num": 5}}
        bus, missing, buses = ip.bus_of(["light.a", "light.x"], amap)
        self.assertEqual(missing, ["light.x"])


class TestCrossGroupNumbers(unittest.TestCase):
    """Нумерация групп: КРОСС первыми и СВЕРХУ, обычные — снизу (2026-08-12).

    Почему такой порядок (и почему прежний был неверен): у обычной группы одно ограничение —
    свободный номер на СВОЕЙ шине, какой именно, ей всё равно. У кросс-группы номер обязан
    совпасть у ВСЕХ участников. Раздав номера сперва обычным, мы оставляли кросс-группе разные
    огрызки на разных шинах — и общего номера не находилось даже там, где слоты были. На
    Воронеже так «терялись» четыре сквозные лестницы: `1.10` свободна на 7–11, `5.1` — на
    14–15, пересечения нет. Кросс сверху + обычные снизу разводят их естественным образом.
    """

    def _plan(self, zones, spaces, amap, cfg=None):
        layer = {"groups": _DF(zones), "spaces": _DF(spaces), "meta": {}}
        base = {"general_groups": {"create": False}}
        base.update(cfg or {})
        return ip.plan_groups(layer, base, amap)

    def test_cross_takes_top_regular_takes_bottom(self):
        amap = {"light.a": {"floor": 1, "bus": 1, "num": 1},
                "light.b": {"floor": 1, "bus": 2, "num": 1},
                "light.c": {"floor": 1, "bus": 3, "num": 1}}
        zones = [
            {"group_id": "z_one_bus", "lamps": ["light.a"], "space": "S1", "room_slug": "s1",
             "sensors_il": [], "sensors_ms": [], "panels": [], "space_type": "class"},
            {"group_id": "z_cross", "lamps": ["light.b", "light.c"], "space": "S2",
             "room_slug": "s2", "sensors_il": [], "sensors_ms": [], "panels": [],
             "space_type": "class"},
        ]
        plan = self._plan(zones, [], amap)
        self.assertEqual([g["dali_num"] for grps in plan["buses"].values() for g in grps], [0])
        self.assertEqual([g["dali_num"] for g in plan["cross"]], [15])
        self.assertFalse(plan["warnings"])

    def test_cross_fits_when_buses_filled_unevenly(self):
        """🔴 Регрессия Воронежа: шины забиты ПО-РАЗНОМУ, общий номер обязан найтись."""
        amap = {}
        zones = []
        for i in range(10):                      # шина 1.2 — 10 обычных групп
            amap[f"light.p{i}"] = {"floor": 1, "bus": 2, "num": i}
            zones.append({"group_id": f"p{i}", "lamps": [f"light.p{i}"], "space": "S",
                          "room_slug": "s", "sensors_il": [], "sensors_ms": [], "panels": [],
                          "space_type": "class"})
        for i in range(14):                      # шина 1.3 — 14 обычных групп
            amap[f"light.q{i}"] = {"floor": 1, "bus": 3, "num": i}
            zones.append({"group_id": f"q{i}", "lamps": [f"light.q{i}"], "space": "S",
                          "room_slug": "s", "sensors_il": [], "sensors_ms": [], "panels": [],
                          "space_type": "class"})
        amap["light.x1"] = {"floor": 1, "bus": 2, "num": 90}
        amap["light.x2"] = {"floor": 1, "bus": 3, "num": 90}
        zones.append({"group_id": "x_cross", "lamps": ["light.x1", "light.x2"], "space": "SX",
                      "room_slug": "sx", "sensors_il": [], "sensors_ms": [], "panels": [],
                      "space_type": "class"})
        plan = self._plan(zones, [], amap)
        self.assertEqual(plan["cross"][0]["dali_num"], 15, "сквозная группа осталась без номера")
        self.assertFalse([w for w in plan["warnings"] if "НЕ СОЗДАЁТСЯ" in w])

    def test_regular_overflow_is_loud(self):
        """Обычным не хватило слотов (кросс держит верхние) — это должно быть ГРОМКО."""
        amap = {f"light.r{i}": {"floor": 1, "bus": 1, "num": i} for i in range(16)}
        amap["light.x1"] = {"floor": 1, "bus": 1, "num": 90}
        amap["light.x2"] = {"floor": 1, "bus": 2, "num": 90}
        zones = [{"group_id": f"z{i}", "lamps": [f"light.r{i}"], "space": "S", "room_slug": "s",
                  "sensors_il": [], "sensors_ms": [], "panels": [], "space_type": "class"}
                 for i in range(16)]
        zones.append({"group_id": "z_cross", "lamps": ["light.x1", "light.x2"], "space": "S2",
                      "room_slug": "s2", "sensors_il": [], "sensors_ms": [], "panels": [],
                      "space_type": "class"})
        plan = self._plan(zones, [], amap)
        self.assertTrue(any("НЕ ВЛЕЗАЮТ" in w for w in plan["warnings"]))
        without = [g["name"] for grps in plan["buses"].values() for g in grps
                   if g["dali_num"] is None]
        self.assertEqual(len(without), 1, "ровно одна группа осталась без номера")

    def test_skip_list_frees_slot(self):
        """`groups.skip` убирает группу из плана — так человек разгружает контроллер."""
        amap = {"light.a": {"floor": 1, "bus": 1, "num": 1},
                "light.b": {"floor": 1, "bus": 1, "num": 2}}
        zones = [{"group_id": "keep", "lamps": ["light.a"], "space": "S", "room_slug": "s",
                  "sensors_il": [], "sensors_ms": [], "panels": [], "space_type": "class"},
                 {"group_id": "drop", "lamps": ["light.b"], "space": "S", "room_slug": "s",
                  "sensors_il": [], "sensors_ms": [], "panels": [], "space_type": "class"}]
        plan = self._plan(zones, [], amap, {"groups": {"skip": ["drop"]}})
        names = [g["name"] for grps in plan["buses"].values() for g in grps]
        self.assertEqual(names, ["keep"])
        self.assertTrue(any("ИСКЛЮЧЕНА" in w for w in plan["warnings"]))

    def test_cross_groups_do_not_collide_with_each_other(self):
        amap = {"light.b": {"floor": 1, "bus": 2, "num": 1},
                "light.c": {"floor": 1, "bus": 3, "num": 1},
                "light.d": {"floor": 1, "bus": 2, "num": 2},
                "light.e": {"floor": 1, "bus": 3, "num": 2}}
        zones = [
            {"group_id": "x1", "lamps": ["light.b", "light.c"], "space": "S", "room_slug": "s",
             "sensors_il": [], "sensors_ms": [], "panels": [], "space_type": "class"},
            {"group_id": "x2", "lamps": ["light.d", "light.e"], "space": "S", "room_slug": "s",
             "sensors_il": [], "sensors_ms": [], "panels": [], "space_type": "class"},
        ]
        nums = [g["dali_num"] for g in self._plan(zones, [], amap)["cross"]]
        self.assertEqual(sorted(nums), [14, 15])


class _DF:
    """Минимальная замена pandas.DataFrame: только то, что использует `plan_groups`."""

    def __init__(self, rows):
        self._rows = rows

    def iterrows(self):
        return enumerate(self._rows)

    @property
    def empty(self):
        return not self._rows

    def __getitem__(self, col):
        return _Col(self._rows, col)


class _Col:
    def __init__(self, rows, col):
        self._rows, self._col = rows, col

    def __eq__(self, other):
        return _DF([r for r in self._rows if r.get(self._col) == other])


class TestEmitTemplate(unittest.TestCase):
    """Шаблон `apply_*.py` живёт СТРОКОЙ — опечатку в нём не поймает ни один импорт.

    Ошибка вылезет уже на боксе, посреди пусконаладки, когда назад дороги нет. Поэтому
    компилируем подставленный шаблон здесь.
    """

    def _render(self, cross="[]"):
        return (ip._EMIT_TEMPLATE
                .replace("@@NAME@@", "apply_test.py")
                .replace("@@URL@@", "ws://localhost:8123/api/websocket")
                .replace("@@GROUPS@@", '[{"name": "z1", "dali_num": 0, "members": ["light.a"]}]')
                .replace("@@CROSS@@", cross)
                .replace("@@AUTOBRIGHT@@", "[]")
                .replace("@@PANELS@@", "[]")
                .replace("@@AREAS@@", "[]"))

    def test_template_compiles(self):
        compile(self._render(), "apply_test.py", "exec")

    def test_template_compiles_with_cross(self):
        cross = ('[{"name": "103_2", "dali_num": 0, '
                 '"members": ["light.l_1_2_1", "light.l_1_3_1"]}]')
        compile(self._render(cross), "apply_test.py", "exec")

    def test_cross_phase_wired_into_groups(self):
        """Кросс-группы должны исполняться в фазе `groups` — иначе `--only groups` их потеряет."""
        body = self._render()
        self.assertIn("def do_cross_groups(", body)
        # условие проверяем по СМЫСЛУ, а не буквой: с v1.2.69 к нему добавился гейт остановки
        # (`and not STOP["on"]`), и сторож на точную строку падал бы на каждой такой правке
        self.assertRegex(body, r'if a\.only in \(None, "groups"\) and CROSS[ :]')

    def test_soft_stop_wired(self):
        """v1.2.69: мягкая остановка. Прогон объекта идёт минутами, и прервать его надо уметь —
        но ТОЛЬКО между записями: обрыв посреди `delGroup`+`addGroup` оставит группу СНЕСЁННОЙ.
        Сторож следит, что обработчик сигнала есть, что он лишь поднимает флаг, и что каждая
        фаза этот флаг проверяет."""
        body = self._render()
        self.assertIn("signal.signal(signal.SIGTERM", body)
        self.assertIn("def stopped(", body)
        for phase in ("группы", "кросс-группы", "пространства", "автояркость", "панели"):
            self.assertIn(f'if stopped("{phase}"', body)
        # между фазами флаг тоже проверяется: остановили на группах — в автояркость не идём
        self.assertIn('and not STOP["on"]', body)

    def test_progress_counter_in_every_phase(self):
        """v1.2.69: `[k/M]` в строках результата — источник прогресса для карточки и опора для
        человека («идёт ли оно вообще»). Без счётчика фоновый прогон = «нажал и гадай»."""
        body = self._render()
        for coll in ("GROUPS", "CROSS", "AREAS", "AUTOBRIGHT", "PANELS"):
            self.assertIn(f"for i, ", body)
            self.assertIn(f"total = len({coll})", body)
        self.assertIn("[{i}/{total}]", body)

    def test_output_is_unbuffered(self):
        """Журнал читают ВО ВРЕМЯ прогона: без flush python копит вывод блоками 4–8 КБ, и
        карточка показывала бы пустоту минутами (прогон выглядит зависшим)."""
        self.assertIn("print = functools.partial(print, flush=True)", self._render())

    def test_cross_requires_two_gateways(self):
        """Гейт «участников ≥2»: одношлюзовый состав — это обычная группа, не кросс."""
        self.assertIn("len(gws) < 2", self._render())


if __name__ == "__main__":
    unittest.main()
