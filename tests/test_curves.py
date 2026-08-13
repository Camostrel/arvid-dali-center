"""Тесты кривых «яркость → мощность» — чистый `energy/curves.py`, без Home Assistant.

ЗАЧЕМ. На кривой `lbs` мы уже один раз чуть не переписали ВЕРНУЮ таблицу на неверную: короткий
прогон дал 6–8% «ошибки», а на деле это квантование счётчика реле (тики 0.2–0.4 Вт·ч), а не
ошибка модели (docs/ENERGY_CALC_MODEL.md §5). Точки боевого замера здесь ЗАФИКСИРОВАНЫ: если
кто-то поправит таблицу «на глазок», тест это покажет.

Запуск:  python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]
                       / "custom_components" / "arvid_dali_center" / "energy"))

from curves import CURVES, DEFAULT_CURVE, power_at  # noqa: E402

LAMP_W = 32.2        # боевая лампа, на которой снимали кривую lbs


class TestLbsCurve(unittest.TestCase):
    """РЕГРЕССИЯ Fix P (v1.1.6): точки замера на БОЕВОЙ лампе (реле 230 В, 2026-07-13)."""

    MEASURED = {0.10: 0.075, 0.20: 0.220, 0.30: 0.303, 0.50: 0.503, 1.00: 1.000}

    def test_measured_points_intact(self):
        """Таблица не «поправлена на глазок»: значения ровно из замера."""
        for frac, share in self.MEASURED.items():
            self.assertAlmostEqual(power_at("lbs", LAMP_W, frac), LAMP_W * share, places=4,
                                   msg=f"точка {int(frac * 100)}% разъехалась с замером")

    def test_linear_above_30_percent(self):
        """Выше 30% лампа ЛИНЕЙНА — это и есть находка замера."""
        for frac in (0.30, 0.50, 1.00):
            share = power_at("lbs", LAMP_W, frac) / LAMP_W
            self.assertAlmostEqual(share, frac, delta=0.005,
                                   msg=f"на {int(frac * 100)}% ждали линейность")

    def test_breaks_below_30_percent(self):
        """Ниже 30% ломается — ради этого кривая и нужна (кабинеты на автояркости живут внизу)."""
        share_10 = power_at("lbs", LAMP_W, 0.10) / LAMP_W
        self.assertLess(share_10, 0.10, "на 10% лампа должна ПРОВАЛИВАТЬСЯ ниже прямой")
        share_20 = power_at("lbs", LAMP_W, 0.20) / LAMP_W
        self.assertGreater(share_20, 0.20, "на 20% лампа должна быть ВЫШЕ прямой")

    def test_linear_model_overstates_low_end(self):
        """Цена отказа от кривой: линейная модель на 10% завышает примерно на треть."""
        real = power_at("lbs", LAMP_W, 0.10)
        naive = power_at("linear", LAMP_W, 0.10)
        self.assertAlmostEqual(naive / real, 1.34, delta=0.03)

    def test_interpolation_between_points(self):
        """Между точками — линейная интерполяция (15% лежит между 10% и 20%)."""
        share = power_at("lbs", LAMP_W, 0.15) / LAMP_W
        self.assertAlmostEqual(share, (0.075 + 0.220) / 2, places=4)


class TestPowerAt(unittest.TestCase):
    """Граничные случаи `power_at` — там, где легко тихо начать врать."""

    def test_off_is_standby(self):
        self.assertEqual(power_at("lbs", LAMP_W, 0.0), 0.0)      # standby = 0 (замер)

    def test_no_power_w_means_zero_not_guess(self):
        """power_w не задан → 0 Вт, а НЕ выдуманное число: непокрытые лампы должны быть ВИДНЫ."""
        self.assertEqual(power_at("lbs", None, 0.5), 0.0)
        self.assertEqual(power_at("lbs", 0, 0.5), 0.0)

    def test_unknown_model_falls_back_to_linear(self):
        self.assertEqual(power_at("нет-такой-кривой", LAMP_W, 0.5),
                         power_at(DEFAULT_CURVE, LAMP_W, 0.5))

    def test_none_model_is_linear(self):
        self.assertEqual(power_at(None, LAMP_W, 0.5), LAMP_W * 0.5)

    def test_above_full_brightness_holds_plateau(self):
        """frac > 1 (не должно приходить, но) → полка, а не экстраполяция в небо."""
        self.assertEqual(power_at("lbs", LAMP_W, 1.5), LAMP_W)

    def test_default_curve_exists(self):
        self.assertIn(DEFAULT_CURVE, CURVES)

    def test_curves_are_monotonic_and_bounded(self):
        """Инвариант любой кривой: точки по возрастанию, доли в [0..1]."""
        for cid, c in CURVES.items():
            xs = [x for x, _ in c["points"]]
            ys = [y for _, y in c["points"]]
            self.assertEqual(xs, sorted(xs), f"{cid}: точки не по возрастанию яркости")
            self.assertTrue(all(0.0 <= v <= 1.0 for v in xs + ys), f"{cid}: доли вне [0..1]")


if __name__ == "__main__":
    unittest.main()
