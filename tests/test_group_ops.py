"""Тесты кросс-шлюзовых DALI-групп — ЧИСТЫЕ функции `group_ops.py`, без Home Assistant.

ЗАЧЕМ ЭТИ ТЕСТЫ. Кросс-шлюзовая группа заводится на КАЖДОМ контроллере отдельно, одним и
тем же `groupId` (захват 2026-08-04). Два места, где это ломается тихо:
  1) состав разложен по шлюзам неверно → чужому контроллеру уедет не его лампа;
  2) `groupId` свободен у одного шлюза и занят у другого → `addGroup` ляжет ПОВЕРХ чужой
     группы, а сверка `readGroup` этого не покажет (она читает нашу же таблицу).

Запуск (из корня проекта, HA не нужен):
    python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import pathlib

import importlib.util
import sys
import unittest
from pathlib import Path

_PATH = (Path(__file__).resolve().parent.parent
         / "custom_components" / "arvid_dali_center" / "group_ops.py")
_spec = importlib.util.spec_from_file_location("group_ops", _PATH)
group_ops = importlib.util.module_from_spec(_spec)
sys.modules["group_ops"] = group_ops
_spec.loader.exec_module(group_ops)

GW_A = "E22435088727"          # шлюзы из захвата 2026-08-04
GW_B = "762417130914"
GW_C = "4225020288D0"


def lamp(addr, gw=None):
    m = {"devType": "0101", "channel": 0, "address": addr}
    if gw:
        m["gwSnObj"] = gw
    return m


class TestMembersByGateway(unittest.TestCase):

    def test_splits_by_owner(self):
        # ровно состав из захвата: 3 лампы на A + 1 на B
        members = [lamp(2, GW_A), lamp(5, GW_A), lamp(7, GW_A), lamp(0, GW_B)]
        by_gw = group_ops.members_by_gateway(members, GW_A)
        self.assertEqual([m["address"] for m in by_gw[GW_A]], [2, 5, 7])
        self.assertEqual([m["address"] for m in by_gw[GW_B]], [0])

    def test_empty_gwsnobj_goes_to_default(self):
        by_gw = group_ops.members_by_gateway([lamp(3)], GW_A)
        self.assertEqual(list(by_gw), [GW_A])
        # gwSnObj проставляется в payload — DALI Center шлёт его даже для своего шлюза
        self.assertEqual(by_gw[GW_A][0]["gwSnObj"], GW_A)

    def test_case_insensitive_one_bucket(self):
        by_gw = group_ops.members_by_gateway([lamp(1, GW_B.lower()), lamp(2, GW_B.upper())], GW_A)
        self.assertEqual(len(by_gw), 1)
        self.assertEqual(len(next(iter(by_gw.values()))), 2)

    def test_participants_order_stable(self):
        members = [lamp(0, GW_B), lamp(2, GW_A), lamp(1, GW_B)]
        self.assertEqual(group_ops.participants(members, GW_A), [GW_B, GW_A])

    def test_write_plan_matches_participants(self):
        members = [lamp(2, GW_A), lamp(0, GW_B)]
        plan = group_ops.split_write_plan(members, GW_A)
        self.assertEqual([gw for gw, _ in plan], [GW_A, GW_B])
        self.assertEqual([len(lst) for _, lst in plan], [1, 1])


class TestIsCrossGateway(unittest.TestCase):
    """Однолшлюзовая группа — ОТДЕЛЬНАЯ модель и в кросс-группу не превращается."""

    def test_single_gateway_is_not_cross(self):
        self.assertFalse(group_ops.is_cross_gateway([lamp(1, GW_A), lamp(2, GW_A)], GW_A))

    def test_two_gateways_is_cross(self):
        self.assertTrue(group_ops.is_cross_gateway([lamp(1, GW_A), lamp(0, GW_B)], GW_A))

    def test_three_gateways_is_cross(self):
        members = [lamp(1, GW_A), lamp(0, GW_B), lamp(4, GW_C)]
        self.assertTrue(group_ops.is_cross_gateway(members, GW_A))
        self.assertEqual(len(group_ops.participants(members, GW_A)), 3)

    def test_empty_members_not_cross(self):
        self.assertFalse(group_ops.is_cross_gateway([], GW_A))


class TestFreeGroupIds(unittest.TestCase):
    """Номер должен быть свободен у ВСЕХ участников — иначе ляжет поверх чужой группы."""

    def test_intersection_not_union(self):
        used = {GW_A: {0, 1, 2}, GW_B: {2, 3}}
        free = group_ops.free_group_ids(used)
        self.assertNotIn(1, free)      # свободен у B, но занят у A → нельзя
        self.assertNotIn(3, free)      # свободен у A, но занят у B → нельзя
        self.assertIn(4, free)

    def test_all_free_when_empty(self):
        self.assertEqual(group_ops.free_group_ids({}), list(range(0, 16)))

    def test_gateway_without_record_counts_as_empty(self):
        self.assertEqual(group_ops.free_group_ids({GW_A: set(), GW_B: None}), list(range(0, 16)))

    def test_no_free_ids(self):
        used = {GW_A: set(range(0, 16))}
        self.assertEqual(group_ops.free_group_ids(used), [])

    def test_limit_is_16_groups(self):
        self.assertEqual(len(group_ops.free_group_ids({})), 16)
        self.assertEqual(max(group_ops.free_group_ids({})), 15)

    def test_conflicts_named(self):
        used = {GW_A: {3}, GW_B: {3, 4}, GW_C: {5}}
        self.assertEqual(group_ops.group_id_conflicts(3, used), sorted([GW_A, GW_B]))
        self.assertEqual(group_ops.group_id_conflicts(9, used), [])


class TestCrossGroupUid(unittest.TestCase):
    """`unique_id` фиксируется ПРИ СОЗДАНИИ. Пересчёт от живого состава = летучий ключ."""

    def test_format_uses_serial_tails(self):
        uid = group_ops.cross_group_uid([GW_A, GW_B], 0, 2)
        self.assertEqual(uid, "xgrp_30914_88727_0_2")   # хвосты, отсортированы

    def test_order_of_participants_irrelevant(self):
        self.assertEqual(group_ops.cross_group_uid([GW_A, GW_B], 0, 2),
                         group_ops.cross_group_uid([GW_B, GW_A], 0, 2))

    def test_case_irrelevant(self):
        self.assertEqual(group_ops.cross_group_uid([GW_A.lower(), GW_B], 0, 2),
                         group_ops.cross_group_uid([GW_A.upper(), GW_B], 0, 2))

    def test_three_gateways(self):
        uid = group_ops.cross_group_uid([GW_A, GW_B, GW_C], 1, 7)
        self.assertTrue(uid.startswith("xgrp_"))
        self.assertTrue(uid.endswith("_1_7"))
        self.assertEqual(len(uid.split("_")), 6)        # xgrp + 3 хвоста + ch + id

    def test_different_group_ids_differ(self):
        self.assertNotEqual(group_ops.cross_group_uid([GW_A, GW_B], 0, 2),
                            group_ops.cross_group_uid([GW_A, GW_B], 0, 3))

    def test_tail_not_head(self):
        # у MAC-серийников из одной партии совпадает НАЧАЛО — хвост обязан различать
        a, b = "AABBCC000001", "AABBCC000002"
        self.assertNotEqual(group_ops.cross_group_uid([a], 0, 1),
                            group_ops.cross_group_uid([b], 0, 1))

    def test_blank_serials_skipped(self):
        self.assertEqual(group_ops.cross_group_uid([GW_A, "", None], 0, 1), "xgrp_88727_0_1")


class TestGroupIdBusyIsVisible(unittest.TestCase):
    """v1.2.79: номер, занятый КРОСС-группой, не должен предлагаться при создании обычной.

    Корень дефекта: копии кросс-групп намеренно не попадают в `hub.groups` (иначе на один свет
    было бы три сущности), поэтому карточка, считавшая занятость по своему списку групп,
    показывала слот свободным. Гейт на бэкенде был — человек узнавал об отказе уже после
    нажатия «Создать».
    """

    def _src(self, rel):
        base = pathlib.Path(__file__).resolve().parents[1]
        return (base / rel).read_text(encoding="utf-8")

    def test_backend_reports_what_occupies_the_slot(self):
        src = self._src("custom_components/arvid_dali_center/websocket_api.py")
        self.assertIn("def _slots_detail(", src)
        self.assertIn('"busy": _slots_detail(hass, gws)', src)
        # кросс-группы обязаны попасть в отчёт — ради них всё и делалось
        i = src.index("def _slots_detail(")
        body = src[i:src.index("\n\n\n", i)]
        self.assertIn('"kind": "cross"', body)
        self.assertIn('"kind": "group"', body)

    def test_card_does_not_compute_busy_from_its_own_list(self):
        """Карточка обязана СПРАШИВАТЬ занятость, а не выводить её из `_state.groups`."""
        src = self._src("www/arvid-dali-panel.js")
        i = src.index("_openCreateGroup(")
        body = src[i:src.index("\n  async _saveCreateGroup", i)]
        self.assertIn("arvid_dali_center/group_slots", body)
        self.assertNotIn("this._state.groups.map((g) => g.groupId)", body,
                         "занятость снова считается по локальному списку — кросс-группы туда "
                         "не попадают, и занятый номер опять будет предложен")


if __name__ == "__main__":
    unittest.main()
