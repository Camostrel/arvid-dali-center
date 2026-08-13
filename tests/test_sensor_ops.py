"""Тесты чистой логики `sensor_ops` — родство записей ОДНОГО физустройства (v1.2.56).

Движение (0201) и освещённость (0202) — две записи кеша с общим devSn на одном адресе, но
одна железка. «Забыть» должно снимать обе; при этом ОСИРОТЕВШАЯ запись не должна утащить за
собой ЖИВУЮ с тем же серийником (на объекте 2026-08-11 так пропали работавшие устройства).
Без HA: модуль чистый (только logging).
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "custom_components"
                       / "arvid_dali_center"))

import sensor_ops as so  # noqa: E402


def dev(devtype, addr, sn, *, channel=0, orphan=False):
    return {"devType": devtype, "channel": channel, "address": addr,
            "devSn": sn, "orphan": orphan}


class TestUnitDevtypes(unittest.TestCase):
    def test_sensor_types_are_paired(self):
        self.assertEqual(so.unit_devtypes("0201"), ("0201", "0202"))
        self.assertEqual(so.unit_devtypes("0202"), ("0201", "0202"))

    def test_other_types_stay_alone(self):
        self.assertEqual(so.unit_devtypes("0101"), ("0101",))
        self.assertEqual(so.unit_devtypes("0308"), ("0308",))


class TestUnitKeys(unittest.TestCase):
    def test_pair_found_both_directions(self):
        devs = {"0201:0:8": dev("0201", 8, "AA11"), "0202:0:8": dev("0202", 8, "AA11")}
        self.assertEqual(sorted(so.unit_keys(devs, "0201:0:8")), ["0201:0:8", "0202:0:8"])
        self.assertEqual(sorted(so.unit_keys(devs, "0202:0:8")), ["0201:0:8", "0202:0:8"])

    def test_requested_key_goes_first(self):
        devs = {"0202:0:8": dev("0202", 8, "AA11"), "0201:0:8": dev("0201", 8, "AA11")}
        self.assertEqual(so.unit_keys(devs, "0202:0:8")[0], "0202:0:8")

    def test_lamp_is_alone(self):
        devs = {"0101:0:8": dev("0101", 8, "BB22"), "0201:0:8": dev("0201", 8, "AA11")}
        self.assertEqual(so.unit_keys(devs, "0101:0:8"), ["0101:0:8"])

    def test_other_devsn_not_paired(self):
        """Тот же адрес, но ДРУГОЙ серийник — разные приборы (перенумерация/мис-энумерация)."""
        devs = {"0201:0:8": dev("0201", 8, "AA11"), "0202:0:8": dev("0202", 8, "CC33")}
        self.assertEqual(so.unit_keys(devs, "0201:0:8"), ["0201:0:8"])

    def test_other_address_not_paired(self):
        devs = {"0201:0:8": dev("0201", 8, "AA11"), "0202:0:9": dev("0202", 9, "AA11")}
        self.assertEqual(so.unit_keys(devs, "0201:0:8"), ["0201:0:8"])

    def test_orphan_does_not_drag_live(self):
        """🔴 Главный гейт: снос ОСИРОТЕВШЕЙ записи не должен цеплять ЖИВУЮ с тем же devSn."""
        devs = {"orphan:AA11:0201": dev("0201", 8, "AA11", orphan=True),
                "0201:0:8": dev("0201", 8, "AA11"),
                "0202:0:8": dev("0202", 8, "AA11")}
        self.assertEqual(so.unit_keys(devs, "orphan:AA11:0201"), ["orphan:AA11:0201"])

    def test_orphan_pairs_with_orphan(self):
        devs = {"orphan:AA11:0201": dev("0201", 8, "AA11", orphan=True),
                "orphan:AA11:0202": dev("0202", 8, "AA11", orphan=True)}
        self.assertEqual(sorted(so.unit_keys(devs, "orphan:AA11:0201")),
                         ["orphan:AA11:0201", "orphan:AA11:0202"])

    def test_empty_devsn_no_kinship(self):
        """Без серийника связать записи нечем — не гадаем (ключ адресный, а адрес волатилен)."""
        devs = {"0201:0:8": dev("0201", 8, ""), "0202:0:8": dev("0202", 8, "")}
        self.assertEqual(so.unit_keys(devs, "0201:0:8"), ["0201:0:8"])

    def test_unknown_key(self):
        self.assertEqual(so.unit_keys({}, "0201:0:8"), [])

    def test_channels_are_separate(self):
        devs = {"0201:0:8": dev("0201", 8, "AA11", channel=0),
                "0202:1:8": dev("0202", 8, "AA11", channel=1)}
        self.assertEqual(so.unit_keys(devs, "0201:0:8"), ["0201:0:8"])


if __name__ == "__main__":
    unittest.main()
