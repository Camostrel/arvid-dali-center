"""Тесты слоя идентичности (`identity.py`) — чем ключуется всё остальное.

Шаг 1 плана docs/ADDRESS_IDENTITY.md. Модуль чистый (stdlib), Home Assistant не нужен.
"""

import ast
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]
                       / "custom_components" / "arvid_dali_center"))

import identity  # noqa: E402


LAMP = {"devType": "0101", "channel": 0, "address": 5, "devSn": "25102C228F6CB1D5"}
MOTION = {"devType": "0201", "channel": 0, "address": 5, "devSn": "060002CE00000000"}
LUX = {"devType": "0202", "channel": 0, "address": 5, "devSn": "060002CE00000000"}
GW = "e22435088727"


class TestDefaultMode(unittest.TestCase):
    def test_default_is_devsn(self):
        """⚠ Главный инвариант шага 1: по умолчанию НИЧЕГО не меняется."""
        self.assertEqual(identity.DEFAULT_MODE, identity.MODE_DEVSN)

    def test_devsn_mode_returns_serial_as_is(self):
        self.assertEqual(identity.identity_key(identity.MODE_DEVSN, GW, LAMP), LAMP["devSn"])

    def test_unknown_mode_falls_back_without_crashing(self):
        """Мусор в файле режима не должен ронять интеграцию — но и угадывать нельзя."""
        self.assertEqual(identity.normalize_mode("ерунда"), identity.MODE_DEVSN)
        self.assertEqual(identity.normalize_mode(None), identity.MODE_DEVSN)
        self.assertEqual(identity.normalize_mode(" ADDR "), identity.MODE_ADDR)


class TestAddrMode(unittest.TestCase):
    def test_key_shape(self):
        self.assertEqual(identity.identity_key(identity.MODE_ADDR, GW, LAMP),
                         "addr:E22435088727:0:dali:5")

    def test_gw_case_normalized(self):
        """Серийник шлюза приходит и строчными, и прописными — ключ обязан быть один."""
        a = identity.identity_key(identity.MODE_ADDR, GW.lower(), LAMP)
        b = identity.identity_key(identity.MODE_ADDR, GW.upper(), LAMP)
        self.assertEqual(a, b)

    def test_lamp_and_sensor_on_same_address_are_different_devices(self):
        """У ламп (`dali`) и датчиков (`dali2`) адреса НЕЗАВИСИМЫ — адрес 5 у обоих не конфликт.
        Именно на этом ломался перекрёст серийников (docs/DEVSN_CROSSWIRE.md)."""
        self.assertNotEqual(identity.identity_key(identity.MODE_ADDR, GW, LAMP),
                            identity.identity_key(identity.MODE_ADDR, GW, MOTION))

    def test_motion_and_lux_are_ONE_device(self):
        """Движение 0201 и освещённость 0202 на одном адресе — одно физическое устройство.
        В штатном режиме их склеивает общий серийник, здесь — координата."""
        self.assertEqual(identity.identity_key(identity.MODE_ADDR, GW, MOTION),
                         identity.identity_key(identity.MODE_ADDR, GW, LUX))

    def test_serial_does_not_participate(self):
        """Смысл режима: перекошенный/пустой/сентинельный серийник на ключ НЕ влияет."""
        broken = dict(LAMP, devSn="00000000FFFFFFFF")
        empty = dict(LAMP, devSn="")
        self.assertEqual(identity.identity_key(identity.MODE_ADDR, GW, LAMP),
                         identity.identity_key(identity.MODE_ADDR, GW, broken))
        self.assertEqual(identity.identity_key(identity.MODE_ADDR, GW, LAMP),
                         identity.identity_key(identity.MODE_ADDR, GW, empty))

    def test_incomplete_coordinate_gives_none(self):
        """Без адреса ключа НЕТ. Придумать его — значит склеить всех безадресных в одну запись
        (ровно так когда-то склеивались лампы с сентинелом `00000000FFFFFFFF`)."""
        self.assertIsNone(identity.identity_key(identity.MODE_ADDR, GW,
                                                {"devType": "0101", "channel": 0}))
        self.assertIsNone(identity.identity_key(identity.MODE_ADDR, "", LAMP))

    def test_address_zero_is_valid(self):
        """Адрес 0 — законный DALI-адрес. Ловушка `if not address`."""
        self.assertIsNotNone(identity.identity_key(identity.MODE_ADDR, GW, dict(LAMP, address=0)))


class TestKeysNeverCollide(unittest.TestCase):
    def test_addr_prefix_separates_generations(self):
        """Корзина HA хранит удалённые записи БЕССРОЧНО (DEBT §T5) и воскрешает по `unique_id`.
        Ключи двух режимов не должны пересекаться, иначе возврат режима поднимет чужое."""
        addr = identity.identity_key(identity.MODE_ADDR, GW, LAMP)
        devsn = identity.identity_key(identity.MODE_DEVSN, GW, LAMP)
        self.assertTrue(identity.is_addr_key(addr))
        self.assertFalse(identity.is_addr_key(devsn))
        self.assertNotEqual(addr, devsn)


class TestFunctionKey(unittest.TestCase):
    """Н2 плана: ключ УСТРОЙСТВА не различает функции датчика, а два стора обязаны."""

    def test_motion_and_lux_differ_by_function(self):
        for mode in (identity.MODE_DEVSN, identity.MODE_ADDR):
            ident_m = identity.identity_key(mode, GW, MOTION)
            ident_l = identity.identity_key(mode, GW, LUX)
            fk_m = identity.function_key(ident_m, MOTION["devType"])
            fk_l = identity.function_key(ident_l, LUX["devType"])
            self.assertNotEqual(fk_m, fk_l, f"режим {mode}: движение и люкс схлопнулись — "
                                            f"«выключил движение» погасит и освещённость")

    def test_no_identity_no_key(self):
        self.assertIsNone(identity.function_key(None, "0201"))
        self.assertIsNone(identity.function_key("addr:X:0:dali2:5", None))


class TestSingleSourceOfLightTypes(unittest.TestCase):
    """Сторож: список типов ламп живёт в ОДНОМ месте. Вторая копия неизбежно разойдётся —
    это ровно класс §F («фикс доехал до одного вызывающего из двух»)."""

    def test_coordinator_does_not_redeclare_light_types(self):
        src = (pathlib.Path(__file__).resolve().parents[1] / "custom_components"
               / "arvid_dali_center" / "coordinator.py").read_text(encoding="utf-8")
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Assign):
                continue
            names = [t.id for t in node.targets if isinstance(t, ast.Name)]
            if "_LIGHT_TYPES" not in names:
                continue
            self.assertNotIsInstance(node.value, ast.Set,
                                     "_LIGHT_TYPES объявлен литералом — берите из identity.py")


if __name__ == "__main__":
    unittest.main()
