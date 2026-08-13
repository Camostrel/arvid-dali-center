"""Тесты пользовательских кривых мощности (файл `/config/arvid_curves/curves.yaml`).

ЗАЧЕМ. Таблицу «яркость → ватты» снимает пусконаладчик на объекте и правит файл руками.
Ошибка в файле — это неверный энергоучёт по ЦЕЛОМУ типу светильников, причём тихий: число
в отчёте будет, просто неправильное. Поэтому здесь зафиксировано, что разбор нормирует
единицы как обещано и НЕ глотает проблемы.

Без HA: `parse_user_curves`/`apply_user_curves` — чистые функции.
"""

import importlib.util
import sys
import types
import unittest
from pathlib import Path

# energy/curves.py — чистый модуль (logging + stdlib), но лежит в пакете: собираем минимальный
_DIR = Path(__file__).resolve().parent.parent / "custom_components" / "arvid_dali_center"
for _pkg, _path in (("arvid_dali_center", _DIR), ("arvid_dali_center.energy", _DIR / "energy")):
    if _pkg not in sys.modules:
        _m = types.ModuleType(_pkg)
        _m.__path__ = [str(_path)]
        sys.modules[_pkg] = _m
_spec = importlib.util.spec_from_file_location("arvid_dali_center.energy.curves",
                                               _DIR / "energy" / "curves.py")
curves = importlib.util.module_from_spec(_spec)
sys.modules["arvid_dali_center.energy.curves"] = curves
_spec.loader.exec_module(curves)


class TestParseUserCurves(unittest.TestCase):
    def test_watts_normalised_to_fractions(self):
        """Человек пишет ватты и проценты — внутри должны получиться доли 0..1."""
        got, problems = curves.parse_user_curves({"curves": {
            "big": {"label": "Большие", "full_w": 60.0,
                    "points": {10: 6.0, 50: 30.0, 100: 60.0}}}})
        self.assertEqual(problems, [])
        pts = got["big"]["points"]
        self.assertEqual(pts[0], (0.0, 0.0))            # ноль дорисован
        self.assertIn((0.1, 0.1), pts)                  # 6 Вт из 60 = 0.1
        self.assertIn((0.5, 0.5), pts)
        self.assertEqual(pts[-1], (1.0, 1.0))

    def test_full_w_optional(self):
        """`full_w` не указан → берём максимум точек, чтобы человек не считал сам."""
        got, problems = curves.parse_user_curves({"curves": {
            "small": {"points": {50: 9.0, 100: 18.0}}}})
        self.assertEqual(problems, [])
        self.assertEqual(got["small"]["full_w"], 18.0)
        self.assertIn((0.5, 0.5), got["small"]["points"])

    def test_nonlinear_shape_preserved(self):
        """Смысл кривой — НИЗЫ: 10 % даёт не 10 % мощности, и это должно сохраниться."""
        got, _ = curves.parse_user_curves({"curves": {
            "big": {"full_w": 100.0, "points": {10: 7.5, 100: 100.0}}}})
        self.assertIn((0.1, 0.075), got["big"]["points"])

    def test_missing_100_percent_is_reported(self):
        """Нет точки на 100 % — достраиваем, но ГОВОРИМ об этом (иначе тихая экстраполяция)."""
        got, problems = curves.parse_user_curves({"curves": {
            "big": {"full_w": 60.0, "points": {10: 6.0, 50: 30.0}}}})
        self.assertIn("big", got)
        self.assertTrue(any("100%" in p for p in problems))

    def test_bad_input_is_not_swallowed(self):
        for raw, why in (
            ({}, "пустой файл"),
            ({"curves": {}}, "пустая секция"),
            ({"curves": {"x": {"points": {}}}}, "нет точек"),
            ({"curves": {"x": {"points": {10: "много"}}}}, "точка не число"),
            ({"curves": {"x": {"points": {150: 5}}}}, "яркость вне 0..100"),
            ({"curves": {"x": {"points": {10: -5}}}}, "отрицательная мощность"),
            ({"curves": {"x": {"points": {10: 0}, "full_w": 0}}}, "нулевая полная мощность"),
        ):
            got, problems = curves.parse_user_curves(raw)
            self.assertTrue(problems, f"проблема проглочена: {why}")
            self.assertEqual(got, {}, f"кривая принята при ошибке: {why}")

    def test_standby_kept(self):
        got, _ = curves.parse_user_curves({"curves": {
            "big": {"full_w": 60.0, "standby_w": 0.4, "points": {100: 60.0}}}})
        self.assertEqual(got["big"]["standby_w"], 0.4)


class TestApplyUserCurves(unittest.TestCase):
    def setUp(self):
        self._orig = dict(curves.CURVES)

    def tearDown(self):
        curves.CURVES.clear()
        curves.CURVES.update(self._orig)

    def test_added_and_usable_in_power_at(self):
        user, _ = curves.parse_user_curves({"curves": {
            "big": {"full_w": 60.0, "points": {10: 6.0, 100: 60.0}}}})
        curves.apply_user_curves(user)
        self.assertIn("big", dict(x["id"] for x in [] ) if False else curves.CURVES)
        # 60 Вт лампа на 10 % яркости → 6 Вт (а не 6.0 по линейной? тут совпало — проверяем связь)
        self.assertAlmostEqual(curves.power_at("big", 60.0, 0.1), 6.0, places=3)

    def test_overriding_builtin_warns(self):
        user, _ = curves.parse_user_curves({"curves": {
            "linear": {"full_w": 10.0, "points": {100: 10.0}}}})
        warns = curves.apply_user_curves(user)
        self.assertTrue(any("linear" in w for w in warns))

    def test_reload_drops_previous_user_curves(self):
        """Файл — истина: исчезнувшая из него кривая не должна оставаться в памяти."""
        first, _ = curves.parse_user_curves({"curves": {
            "big": {"full_w": 60.0, "points": {100: 60.0}}}})
        curves.apply_user_curves(first)
        second, _ = curves.parse_user_curves({"curves": {
            "small": {"full_w": 18.0, "points": {100: 18.0}}}})
        curves.apply_user_curves(second)
        self.assertNotIn("big", curves.CURVES)
        self.assertIn("small", curves.CURVES)
        self.assertIn("linear", curves.CURVES)          # встроенные не трогаем


if __name__ == "__main__":
    unittest.main()
