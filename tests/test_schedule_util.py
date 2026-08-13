# -*- coding: utf-8 -*-
"""Тесты расписания датчиков (`runCondition` devType 0701). Stdlib, без HA.

Формат окна подтверждён захватом DALI Center 2026-07-29 (docs/PLAN_SENSOR_BINDINGS §H4).
Ошибка здесь = свет включается не тогда, поэтому валидация строгая, а «умных» правок
заданного человеком расписания мы не делаем.
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "custom_components"
                       / "arvid_dali_center"))

import schedule_util as su  # noqa: E402


class TestValidateWindow(unittest.TestCase):
    def test_ok(self):
        self.assertEqual(su.validate_window("08:00-17:30"), "08:00-17:30")
        self.assertEqual(su.validate_window(" 00:00-23:59 "), "00:00-23:59")

    def test_midnight_cross_rejected(self):
        # через полночь ЗАПРЕЩЕНО (v1.2.25): DALI Center так не умеет, поведение шлюза неизвестно.
        # Ночь задаётся двумя окнами — и подсказка об этом должна быть в тексте ошибки.
        with self.assertRaises(su.WindowError) as ctx:
            su.validate_window("22:00-06:00")
        self.assertIn("двумя окнами", str(ctx.exception))

    def test_night_as_two_windows(self):
        self.assertEqual(su.normalize_windows(["22:00-23:59", "00:00-06:00"]),
                         ["22:00-23:59", "00:00-06:00"])

    def test_bad_format(self):
        for bad in ("8:00-17:30", "08:00–17:30", "08:00", "08:00-17:60", "24:00-01:00", ""):
            with self.assertRaises(su.WindowError):
                su.validate_window(bad)

    def test_degenerate(self):
        # начало == конец: «никогда» или «всегда»? двусмысленность в расписании света недопустима
        with self.assertRaises(su.WindowError):
            su.validate_window("09:00-09:00")


class TestNormalizeWindows(unittest.TestCase):
    def test_dedup_keeps_order(self):
        self.assertEqual(su.normalize_windows(["08:00-12:00", "13:00-17:00", "08:00-12:00"]),
                         ["08:00-12:00", "13:00-17:00"])

    def test_empty(self):
        self.assertEqual(su.normalize_windows([]), [])
        self.assertEqual(su.normalize_windows(None), [])

    def test_propagates_error(self):
        with self.assertRaises(su.WindowError):
            su.normalize_windows(["08:00-12:00", "мусор"])


class TestOverlap(unittest.TestCase):
    def test_overlap_detected(self):
        self.assertEqual(su.windows_overlap(["08:00-13:00", "12:00-17:00"]),
                         [("08:00-13:00", "12:00-17:00")])

    def test_touching_is_not_overlap(self):
        # «до 13:00» и «с 13:00» — стык, а не пересечение
        self.assertEqual(su.windows_overlap(["08:00-13:00", "13:00-17:00"]), [])

    def test_no_false_overlap(self):
        self.assertEqual(su.windows_overlap(["08:00-12:00", "13:00-17:00"]), [])


if __name__ == "__main__":
    unittest.main()
