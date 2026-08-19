"""Тесты именования — ЧИСТЫЕ функции `naming.py`, без запуска Home Assistant.

ЗАЧЕМ ЭТИ ТЕСТЫ. Именование — место, где мы наступили на грабли много раз (Fix J/M/N/O/R/S/V/W),
и каждый раз узнавали об этом ТОЛЬКО с железа. Все эти баги — в чистых функциях, проверяемых за
миллисекунды. Здесь зафиксированы РЕГРЕССИИ модели v1.2.7 (имя устройства по devSn, entity_id по
адресу+sn5 без шлюза, подпись безымянного не задаётся).

Запуск (из корня проекта, HA не нужен):
    python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

# naming.py не тянет ни HA, ни другие модули проекта → грузим его напрямую, без пакета
sys.path.insert(0, str(Path(__file__).resolve().parents[1]
                       / "custom_components" / "arvid_dali_center"))

import naming  # noqa: E402
from naming import (  # noqa: E402
    device_name,
    device_word,
    entity_name,
    is_auto_suffix,
    sensor_body,
    sensor_name,
    sn_suffix,
)


class TestEntityName(unittest.TestCase):
    """entity_id БЕЗЫМЯННОЙ сущности: <тип>_<адрес>_<sn5>. ⚠ v1.2.7: ШЛЮЗА В ИМЕНИ НЕТ."""

    SN = "0000000A8F6CAB12"      # sn5 = cab12

    def test_no_gateway_in_id(self):
        """gw4 убран (v1.2.7): id зависит только от типа, адреса и devSn."""
        self.assertEqual(entity_name("0101", 1, self.SN), "light_1_cab12")
        self.assertEqual(entity_name("0201", 5, self.SN), "motion_5_cab12")
        self.assertEqual(entity_name("0202", 5, self.SN), "illuminance_5_cab12")
        self.assertEqual(entity_name("0308", 3, self.SN), "keypanel_8_3_cab12")
        self.assertEqual(entity_name("0300", 2, self.SN), "rotary_2_cab12")

    def test_stable_across_gateway_move(self):
        """ГЛАВНОЕ v1.2.7: устройство переехало на ДРУГОЙ шлюз, адрес тот же → id НЕ меняется.
        Раньше в id был gw4 → id менялся → история recorder рвалась."""
        # gw в сигнатуре entity_name больше нет вообще — id физически не может зависеть от шлюза
        self.assertEqual(entity_name("0201", 5, self.SN), entity_name("0201", 5, self.SN))

    def test_changes_on_readdress(self):
        """Смена АДРЕСА (перераздача) id меняет — это честно, физика поменялась."""
        self.assertNotEqual(entity_name("0201", 5, self.SN), entity_name("0201", 6, self.SN))

    def test_no_devsn_falls_back_without_tail(self):
        self.assertEqual(entity_name("0201", 3, ""), "motion_3")
        self.assertEqual(sn_suffix(""), "")

    def test_sn_suffix_is_last_five_lowercase(self):
        self.assertEqual(sn_suffix("0000000A8F6CAB12"), "cab12")
        self.assertEqual(sn_suffix("ABC"), "abc")


class TestFixW_NoCollisionOnReaddress(unittest.TestCase):
    """РЕГРЕССИЯ Fix W: перераздача адресов (перестановка) не даёт наложения — sn5 разводит."""

    SN_A = "0000000A8F6CAB12"    # cab12
    SN_B = "0000000B1234CD9E"    # 4cd9e

    def test_swap_two_sensors(self):
        """A: 3→4, B: 4→3. Желаемое имя каждого ≠ текущему имени соседа."""
        self.assertNotEqual(entity_name("0201", 4, self.SN_A), entity_name("0201", 4, self.SN_B))
        self.assertNotEqual(entity_name("0201", 3, self.SN_B), entity_name("0201", 3, self.SN_A))

    def test_same_address_different_devices_distinct(self):
        """Два устройства на одном адресе (напр. разные шлюзы) → разные id по sn5."""
        self.assertNotEqual(entity_name("0201", 5, self.SN_A), entity_name("0201", 5, self.SN_B))


class TestDeviceName(unittest.TestCase):
    """Имя УСТРОЙСТВА HA: <тип-слово>_<полный devSn>. ⚠ v1.2.7: НИ адреса, НИ шлюза."""

    SN = "E0387029A088D0B9"

    def test_full_devsn_no_address_no_gateway(self):
        self.assertEqual(device_name("0201", self.SN), "sensor_E0387029A088D0B9")
        self.assertEqual(device_name("0101", self.SN), "light_E0387029A088D0B9")
        self.assertEqual(device_name("0308", self.SN), "keypanel_8_E0387029A088D0B9")

    def test_motion_and_lux_share_one_device_name(self):
        """Движение+люкс — ОДНО устройство (общий devSn) → общее имя `sensor_<devSn>`."""
        self.assertEqual(device_name("0201", self.SN), "sensor_E0387029A088D0B9")
        self.assertEqual(device_name("0202", self.SN), device_name("0201", self.SN))
        self.assertEqual(device_word("0201"), "sensor")
        self.assertEqual(device_word("0202"), "sensor")

    def test_stable_across_address_and_gateway(self):
        """Имя устройства НЕ зависит ни от адреса, ни от шлюза (нет их в сигнатуре) → стабильно."""
        self.assertEqual(device_name("0201", self.SN, address=5),
                         device_name("0201", self.SN, address=99))

    def test_fallback_on_empty_devsn(self):
        """Битый/пустой devSn → устройство добавится по адресу, а не пропадёт."""
        self.assertEqual(device_name("0201", "", address=7), "sensor_addr7")
        self.assertEqual(device_name("0101", ""), "light")


class TestFixR_AutoSuffix(unittest.TestCase):
    """РЕГРЕССИЯ Fix R: «автосуффикс» ≠ «кончается цифрой»."""

    def test_real_auto_suffix(self):
        self.assertTrue(is_auto_suffix("sensor.motion_5_cab12_2", "sensor.motion_5_cab12"))

    def test_production_naming_is_not_auto_suffix(self):
        self.assertFalse(is_auto_suffix("light.l_2_5_13", "light.light_3_cab12"))
        self.assertFalse(is_auto_suffix("sensor.ms_5_1_3", "sensor.motion_5_cab12"))

    def test_manual_id_is_not_auto_suffix(self):
        self.assertFalse(is_auto_suffix("light.office_3", "light.light_3_cab12"))

    def test_empty_desired(self):
        self.assertFalse(is_auto_suffix("light.whatever_2", ""))


class TestSensorNaming(unittest.TestCase):
    """Продакшен-имя пары движение/люкс: тип в ПРЕФИКСЕ, общее «тело» (v0.58)."""

    def test_prefix_by_type(self):
        self.assertEqual(sensor_name("0201", "5_1_3"), "ms_5_1_3")
        self.assertEqual(sensor_name("0202", "5_1_3"), "il_5_1_3")

    def test_body_normalizes_user_input(self):
        for src in ("ms_5_1_3", "il_5_1_3", "motion_5_1_3", "illuminance_5_1_3",
                    "5_1_3", "ms_5_1_3_act"):
            self.assertEqual(sensor_body(src), "5_1_3", f"тело из {src!r}")

    def test_body_of_plain_name(self):
        self.assertEqual(sensor_body("kitchen"), "kitchen")


class TestAddrModeNaming(unittest.TestCase):
    """Н4 плана docs/ADDRESS_IDENTITY.md: в адресном режиме хвост имени — `gw4`, не `sn5`.

    Почему хвост вообще нужен: имя безымянной сущности производно от АДРЕСА, а адреса 0..63
    повторяются на каждом контроллере — без хвоста `light_5` на двух шлюзах столкнулись бы.
    """

    def test_gw_suffix_four_chars(self):
        self.assertEqual(naming.gw_suffix("E22435088727"), "8727")
        self.assertEqual(naming.gw_suffix(""), "")

    def test_entity_name_takes_explicit_tail(self):
        """Хвост задаёт вызывающий: режим — знание хаба, не функции имени."""
        self.assertEqual(naming.entity_name("0201", 14, tail="8727"), "motion_14_8727")
        self.assertEqual(naming.entity_name("0201", 14, tail=""), "motion_14")

    def test_entity_name_default_is_unchanged(self):
        """⚠ Без явного хвоста поведение штатного режима прежнее (sn5)."""
        self.assertEqual(naming.entity_name("0201", 14, "E0387029A0FFD6"),
                         naming.entity_name("0201", 14, tail=naming.sn_suffix("E0387029A0FFD6")))

    def test_device_name_addr_has_no_serial(self):
        """Имя устройства в адресном режиме не содержит серийника — он там справочный
        и может быть пустым или перекошенным."""
        name = naming.device_name_addr("0201", "E22435088727", 14)
        self.assertEqual(name, "sensor_8727_14")
        self.assertNotIn("E2243508", name)

    def test_device_name_addr_survives_missing_parts(self):
        self.assertEqual(naming.device_name_addr("0101", "", None), "light")


if __name__ == "__main__":
    unittest.main()
