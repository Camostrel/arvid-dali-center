"""Тесты состава ячейки привязки — ЧИСТЫЕ функции `panel_ops.py`, без запуска Home Assistant.

ЗАЧЕМ ЭТИ ТЕСТЫ. Кросс-шлюзовая привязка (панель на одном контроллере, лампа на другом)
ломалась ТИХО: ключ сверки не содержал `gwSnObj`, поэтому подмена цели на СВОЮ лампу с тем
же адресом проходила как «совпало», и карточка рапортовала успех. Здесь зафиксировано, что
шлюз — часть идентичности цели, и что состав ячейки считается целиком (эталон DALI Center,
захват 2026-08-03: del ВСЕХ текущих → add ПОЛНОГО нового состава).

Запуск (из корня проекта, HA не нужен):
    python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

# panel_ops.py не тянет ни HA, ни другие модули проекта → грузим напрямую, без пакета
_PATH = (Path(__file__).resolve().parent.parent
         / "custom_components" / "arvid_dali_center" / "panel_ops.py")
_spec = importlib.util.spec_from_file_location("panel_ops", _PATH)
panel_ops = importlib.util.module_from_spec(_spec)
sys.modules["panel_ops"] = panel_ops
_spec.loader.exec_module(panel_ops)

GW_A = "E22435088727"          # шлюз панели (свой)
GW_B = "762417130914"          # шлюз цели (чужой) — оба из захвата 2026-08-03


def lamp(addr, gw=None, prop=None):
    o = {"devType": "0101", "channel": 0, "address": addr, "property": prop or []}
    if gw is not None:
        o["gwSnObj"] = gw
    return o


class TestTargetKey(unittest.TestCase):
    """Ключ цели = (шлюз, тип, канал, адрес). Шлюз в ключе — суть фикса v1.2.38."""

    def test_same_address_different_gateways_are_different_targets(self):
        # ГЛАВНОЕ: адрес 0 на своём и на чужом шлюзе — РАЗНЫЕ лампы
        self.assertNotEqual(panel_ops.target_key(lamp(0, GW_A)),
                            panel_ops.target_key(lamp(0, GW_B)))

    def test_empty_gateway_normalized_to_own(self):
        # цель без gwSnObj = цель своего шлюза: две формы записи → один ключ
        self.assertEqual(panel_ops.target_key(lamp(3), GW_A),
                         panel_ops.target_key(lamp(3, GW_A), GW_A))

    def test_case_insensitive_serial(self):
        self.assertEqual(panel_ops.target_key(lamp(1, GW_A.lower())),
                         panel_ops.target_key(lamp(1, GW_A.upper())))

    def test_group_and_lamp_with_same_address_differ(self):
        grp = {"devType": "0401", "channel": 0, "address": 2, "gwSnObj": GW_A}
        self.assertNotEqual(panel_ops.target_key(grp), panel_ops.target_key(lamp(2, GW_A)))


class TestTargetSet(unittest.TestCase):
    def test_verify_catches_substitution(self):
        """Запрошена лампа чужого шлюза, контроллер записал свою с тем же адресом —
        сверка обязана увидеть расхождение (раньше говорила «совпало»)."""
        requested = panel_ops.target_set([lamp(0, GW_B)], GW_A)
        actual = panel_ops.target_set([lamp(0, GW_A)], GW_A)
        self.assertFalse(requested.issubset(actual))

    def test_verify_match_when_written_as_asked(self):
        requested = panel_ops.target_set([lamp(0, GW_B)], GW_A)
        actual = panel_ops.target_set([lamp(0, GW_A), lamp(1, GW_A), lamp(0, GW_B)], GW_A)
        self.assertTrue(requested.issubset(actual))


class TestCellTarget(unittest.TestCase):
    def test_form_matches_capture(self):
        o = panel_ops.cell_target(lamp(5, GW_B, [{"dpid": 20, "dataType": "bool", "value": True}]))
        self.assertEqual(set(o), {"devType", "address", "channel", "gwSnObj", "property"})
        self.assertEqual(o["gwSnObj"], GW_B)

    def test_property_never_none(self):
        self.assertEqual(panel_ops.cell_target({"devType": "0101", "channel": 0,
                                                "address": 1, "property": None})["property"], [])

    def test_drops_foreign_fields(self):
        # `act` — наша служебная пометка из PanelActStore, на шину её слать нельзя
        o = panel_ops.cell_target({**lamp(1, GW_A), "act": "toggle"})
        self.assertNotIn("act", o)


class TestMergeTargets(unittest.TestCase):
    """«+ цель» = ПОЛНЫЙ новый состав (текущие + новая), а не одна дельта."""

    def test_adds_to_existing(self):
        cur = [lamp(0, GW_A), lamp(1, GW_A)]
        out = panel_ops.merge_targets(cur, [lamp(0, GW_B)], GW_A)
        self.assertEqual(len(out), 3)
        self.assertEqual(panel_ops.target_key(out[-1]), panel_ops.target_key(lamp(0, GW_B)))

    def test_same_target_replaced_not_duplicated(self):
        # правка действия у уже привязанной цели: состав не растёт, property обновился
        cur = [lamp(0, GW_A, [{"dpid": 20, "value": True}])]
        out = panel_ops.merge_targets(cur, [lamp(0, GW_A, [{"dpid": 20, "value": False}])], GW_A)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["property"], [{"dpid": 20, "value": False}])

    def test_order_of_existing_preserved(self):
        cur = [lamp(7, GW_A), lamp(2, GW_A)]
        out = panel_ops.merge_targets(cur, [lamp(9, GW_A)], GW_A)
        self.assertEqual([o["address"] for o in out], [7, 2, 9])

    def test_empty_cell(self):
        self.assertEqual(len(panel_ops.merge_targets([], [lamp(0, GW_B)], GW_A)), 1)


class TestRemainingTargets(unittest.TestCase):
    """Снятие цели = del ВСЕХ + возврат остатка (выборочного удаления у контроллера нет)."""

    def test_drops_only_asked(self):
        cur = [lamp(0, GW_A), lamp(1, GW_A), lamp(0, GW_B)]
        rest = panel_ops.remaining_targets(cur, [lamp(1, GW_A)], GW_A)
        self.assertEqual([panel_ops.target_key(o) for o in rest],
                         [panel_ops.target_key(lamp(0, GW_A)), panel_ops.target_key(lamp(0, GW_B))])

    def test_foreign_target_removal_keeps_local_twin(self):
        # снимаем ЧУЖУЮ лампу адреса 0 — местная лампа адреса 0 обязана остаться
        cur = [lamp(0, GW_A), lamp(0, GW_B)]
        rest = panel_ops.remaining_targets(cur, [lamp(0, GW_B)], GW_A)
        self.assertEqual(len(rest), 1)
        self.assertEqual(rest[0]["gwSnObj"], GW_A)

    def test_drop_all_gives_empty(self):
        cur = [lamp(0, GW_A), lamp(0, GW_B)]
        self.assertEqual(panel_ops.remaining_targets(cur, cur, GW_A), [])


class TestTargetsByGateway(unittest.TestCase):
    """Ячейка живёт на ДВУХ шлюзах (захват 2026-08-04): полный состав — шлюзу панели,
    его собственные цели — шлюзу цели. Без этой разбивки чужая лампа не отрабатывала."""

    def test_splits_own_and_foreign(self):
        cur = [lamp(0, GW_A), lamp(1, GW_A), lamp(0, GW_B)]
        by_gw = panel_ops.targets_by_gateway(cur, GW_A)
        self.assertEqual(sorted(by_gw), sorted([GW_A, GW_B]))
        self.assertEqual([o["address"] for o in by_gw[GW_A]], [0, 1])
        self.assertEqual([o["address"] for o in by_gw[GW_B]], [0])

    def test_empty_gwsnobj_counts_as_own(self):
        cur = [{"devType": "0101", "address": 5, "channel": 0, "property": []}]
        by_gw = panel_ops.targets_by_gateway(cur, GW_A)
        self.assertEqual(list(by_gw), [GW_A])

    def test_case_insensitive_one_bucket(self):
        cur = [lamp(0, GW_B.lower()), lamp(1, GW_B.upper())]
        by_gw = panel_ops.targets_by_gateway(cur, GW_A)
        self.assertEqual(len(by_gw), 1)
        self.assertEqual(len(next(iter(by_gw.values()))), 2)

    def test_foreign_only_excludes_own(self):
        cur = [lamp(0, GW_A), lamp(0, GW_B)]
        foreign = panel_ops.foreign_gateway_targets(cur, GW_A)
        self.assertEqual(list(foreign), [GW_B])
        self.assertEqual(foreign[GW_B][0]["address"], 0)

    def test_foreign_empty_when_all_local(self):
        self.assertEqual(panel_ops.foreign_gateway_targets([lamp(0, GW_A)], GW_A), {})

    def test_foreign_target_carries_property(self):
        # на шлюз цели уходит цель С property (в захвате add несёт вкл+яркость)
        cur = [{"devType": "0101", "address": 0, "channel": 0, "gwSnObj": GW_B,
                "property": [{"dpid": 20, "dataType": "bool", "value": True}]}]
        foreign = panel_ops.foreign_gateway_targets(cur, GW_A)
        self.assertEqual(foreign[GW_B][0]["property"][0]["dpid"], 20)


class TestKeyEventTypes(unittest.TestCase):
    """Типы событий панели: номер клавиши входит в ТИП (v1.2.45), иначе триггер
    автоматизации ловит нажатие любой клавиши."""

    def test_key_count_from_devtype(self):
        self.assertEqual(panel_ops.panel_key_count("0308"), 8)
        self.assertEqual(panel_ops.panel_key_count("0302"), 2)
        self.assertEqual(panel_ops.panel_key_count("0300"), 0)   # поворотная — клавиш нет
        self.assertEqual(panel_ops.panel_key_count(None), 0)

    def test_event_types_cover_every_key_and_gesture(self):
        types = panel_ops.key_event_types("0308")
        self.assertIn("key1_click", types)
        self.assertIn("key8_hold_end", types)
        self.assertNotIn("key9_click", types)                    # клавиши сверх devType нет
        # голые жесты обязаны остаться: HA принимает только тип из списка, а событие без
        # keyNo (поворотная панель) иначе потерялось бы молча
        for g in panel_ops.GESTURES:
            self.assertIn(g, types)

    def test_event_type_for_key(self):
        self.assertEqual(panel_ops.key_event_type("0308", 3, "click"), "key3_click")
        self.assertEqual(panel_ops.key_event_type("0304", "2", "hold"), "key2_hold")
        # клавиша вне диапазона devType → None (вызывающий выпустит голый жест + WARNING)
        self.assertIsNone(panel_ops.key_event_type("0304", 7, "click"))
        self.assertIsNone(panel_ops.key_event_type("0308", None, "click"))
        self.assertIsNone(panel_ops.key_event_type("0308", 0, "click"))


if __name__ == "__main__":
    unittest.main()
