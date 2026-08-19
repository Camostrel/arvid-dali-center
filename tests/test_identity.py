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
PANEL = {"devType": "0308", "channel": 0, "address": 5, "devSn": "336DC33D99FB610D"}
ROTARY = {"devType": "0300", "channel": 0, "address": 5, "devSn": "336DC33D99FB6111"}
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
                         "addr:E22435088727:0:01:5")

    def test_gw_case_normalized(self):
        """Серийник шлюза приходит и строчными, и прописными — ключ обязан быть один."""
        a = identity.identity_key(identity.MODE_ADDR, GW.lower(), LAMP)
        b = identity.identity_key(identity.MODE_ADDR, GW.upper(), LAMP)
        self.assertEqual(a, b)

    def test_lamp_and_sensor_on_same_address_are_different_devices(self):
        """У ламп и у датчиков адреса НЕЗАВИСИМЫ — адрес 5 у обоих не конфликт. Именно на
        сосуществовании пары на одном адресе и ломался перекрёст (docs/DEVSN_CROSSWIRE.md)."""
        self.assertNotEqual(identity.identity_key(identity.MODE_ADDR, GW, LAMP),
                            identity.identity_key(identity.MODE_ADDR, GW, MOTION))

    def test_motion_and_lux_are_ONE_device(self):
        """Движение 0201 и освещённость 0202 на одном адресе — одно физическое устройство.
        В штатном режиме их склеивает общий серийник, здесь — координата."""
        self.assertEqual(identity.identity_key(identity.MODE_ADDR, GW, MOTION),
                         identity.identity_key(identity.MODE_ADDR, GW, LUX))

    def test_sensor_and_panel_on_same_address_differ(self):
        """Разбор с пользователем 2026-08-19: `dali2` — ОДИН класс на датчики и панели. Ключ по
        пространству склеил бы датчик и панель одного адреса ТИХО. Класс типа (`02` против `03`)
        делает вопрос «бывает ли так на железе» безразличным."""
        self.assertNotEqual(identity.identity_key(identity.MODE_ADDR, GW, MOTION),
                            identity.identity_key(identity.MODE_ADDR, GW, PANEL))

    def test_three_classes_are_three_keys(self):
        keys = {identity.identity_key(identity.MODE_ADDR, GW, d) for d in (LAMP, MOTION, PANEL)}
        self.assertEqual(len(keys), 3, "лампа, датчик и панель одного адреса обязаны различаться")

    def test_panel_variants_are_one_device(self):
        """Поворотная `0300` и клавишная `0308` — обе класс `03`; на одном адресе физически
        может быть только одна, склейка безопасна и склеивать их МОЖНО."""
        self.assertEqual(identity.identity_key(identity.MODE_ADDR, GW, PANEL),
                         identity.identity_key(identity.MODE_ADDR, GW, ROTARY))

    def test_unknown_type_is_visible_not_silent(self):
        """Пустой/битый `devType` даёт `??` — ключ виден глазом и не притворяется нормальным."""
        k = identity.identity_key(identity.MODE_ADDR, GW, {"devType": "", "channel": 0, "address": 5})
        self.assertIn(":??:", k)

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
        self.assertIsNone(identity.function_key("addr:X:0:02:5", None))


class TestIdentityNotDuplicated(unittest.TestCase):
    """Сторож шага 4: ключ идентичности считается ОДНИМ методом хаба.

    До v1.2.72 формула жила в пяти местах (четыре платформы + `_roles_for_dev`), причём в двух
    видах: у ламп фолбэк с `devType`, у остальных без. Разъехались бы — `reconcile` перестал бы
    находить сущности по своему же ключу, и это тот самый класс §F.
    """

    PLATFORMS = ("light.py", "sensor.py", "switch.py", "event.py")

    def _src(self, name):
        return (pathlib.Path(__file__).resolve().parents[1] / "custom_components"
                / "arvid_dali_center" / name).read_text(encoding="utf-8")

    def test_platforms_ask_the_hub(self):
        for name in self.PLATFORMS:
            self.assertIn("hub.identity(dev", self._src(name),
                          f"{name}: unique_id обязан браться из hub.identity()")

    def test_platforms_do_not_rebuild_the_key(self):
        """Ни одна платформа не собирает базу ключа сама (`devSn or f"{gw}:..."`)."""
        for name in self.PLATFORMS:
            for line in self._src(name).splitlines():
                if line.lstrip().startswith("#"):
                    continue
                bad = ('uid_base = ' in line or 'uid = ' in line) and 'devSn' in line
                self.assertFalse(bad, f"{name}: база ключа собирается на месте — {line.strip()}")

    def test_name_is_read_through_the_hub(self):
        """Шаг 5 (NameStore): имя читается ОДНОЙ точкой — `hub.custom_name(dev)`.

        Было десять мест с `ns.get(name_key(...))`, каждое решало про ключ самостоятельно.
        В адресном режиме такие места молча читали бы имя не по тому ключу."""
        for name in self.PLATFORMS:
            self.assertNotIn("name_key(", self._src(name),
                             f"{name}: ключ имени собирается на месте")
        coord = self._src("coordinator.py")
        self.assertEqual(coord.count("def custom_name(self, dev"), 1)
        self.assertEqual(coord.count("def name_key_for(self, dev"), 1)

    def test_hub_has_single_identity_method(self):
        src = self._src("coordinator.py")
        self.assertEqual(src.count("def identity(self, dev"), 1)
        self.assertIn("base = self.identity(dev)", src)


class TestStoresKeyedThroughHub(unittest.TestCase):
    """Сторож шага 5: device-level хранилища берут ключ у хаба, а не собирают из `devSn`.

    Опасность именно тихая: в адресном режиме такое место продолжит работать, но будет писать
    и читать НЕ ПО ТОМУ ключу — данные «пропадут», хотя ошибок в логе не будет.
    """

    def _src(self, rel):
        return (pathlib.Path(__file__).resolve().parents[1] / "custom_components"
                / "arvid_dali_center" / rel).read_text(encoding="utf-8")

    def test_param_store_key_via_hub(self):
        self.assertIn("hub.name_key_for(rec)", self._src("websocket_api.py"))

    def test_rotary_binding_via_hub(self):
        src = self._src("websocket_api.py")
        self.assertNotIn('devsn = dev.get("devSn") if dev else None', src,
                         "привязка поворота всё ещё ключуется серийником напрямую")

    def test_sensor_obj_store_keeps_function_in_key(self):
        """Н2: конфигурация функции датчика различает 0201 и 0202 в ОБОИХ режимах."""
        self.assertIn('f"{identity}:{dev_type}:{dpid}"', self._src("store.py"))

    def test_energy_resolves_identity_not_serial(self):
        self.assertIn("hub.name_key_for(rec)", self._src("energy/integrator.py"))

    def test_health_finds_entities_by_identity(self):
        src = self._src("health/evaluator.py")
        self.assertNotIn('ereg.async_get_entity_id(platform, DOMAIN, f"{devsn}{sfx}")', src,
                         "здоровье собирает unique_id из серийника — в адресном режиме оно "
                         "просто перестанет находить сущности, и это будет ТИХО")


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


class TestAddrModeDisablesSerialMachinery(unittest.TestCase):
    """Шаг 7: механизмы, существующие ТОЛЬКО ради серийника, в адресном режиме не работают.

    Если оставить их включёнными, они начнут «чинить» то, что чинить не надо: re-link уведёт
    запись с адреса, потому что тот же серийник нашёлся на другом, а вытеснение серийника
    сделает живое устройство осиротевшим. Оба отказа тихие.
    """

    def _src(self, rel="coordinator.py"):
        return (pathlib.Path(__file__).resolve().parents[1] / "custom_components"
                / "arvid_dali_center" / rel).read_text(encoding="utf-8")

    def test_relink_gated(self):
        src = self._src()
        self.assertIn("if (not addr_mode and is_valid_devsn(e.get(\"devSn\")) and ident in live_ids):",
                      src, "re-link в скане не закрыт гейтом режима")
        self.assertIn("not addr_mode_load", src, "re-link при загрузке персиста не закрыт гейтом")

    def test_orphaning_replaced_by_signal(self):
        """Смена серийника на адресе в адресном режиме — СИГНАЛ, а не вытеснение (Н6)."""
        src = self._src()
        self.assertIn("serial_changed.append", src)
        self.assertIn("if addr_mode:", src)

    def test_claim_gated(self):
        self.assertIn("if physical and not addr_mode:", self._src())

    def test_name_migration_skipped(self):
        self.assertIn("return          # в адресном режиме имена ключуются координатой",
                      self._src())


class TestModeSwitchIsAnOperation(unittest.TestCase):
    """Н10: смена режима сносит поколение старых ключей, и ПОРЯДОК шагов существенный."""

    def _src(self):
        return (pathlib.Path(__file__).resolve().parents[1] / "custom_components"
                / "arvid_dali_center" / "identity_ops.py").read_text(encoding="utf-8")

    def test_flag_is_written_last(self):
        """Сначала собираем ключи и сносим — потом меняем правило. Иначе сбор пойдёт уже по
        новому режиму и не найдёт ничего, а мусор останется навсегда."""
        src = self._src()
        self.assertLess(src.index("purge_gateway_everywhere(hass, gw_sn)"),
                        src.index("await store.async_set(mode)"))
        self.assertLess(src.index("purge_registry_trash(hass, uids, idents)"),
                        src.index("await store.async_set(mode)"))

    def test_trash_is_swept(self):
        """Без выметания корзины возврат режима поднимет старые записи (закон 1, T5)."""
        self.assertIn("purge_registry_trash", self._src())

    def test_no_bus_commands(self):
        """Смена режима НЕ трогает железо: группы и привязки живут в контроллерах."""
        src = self._src()
        for forbidden in ("async_request(", "writeDev", "addGroup", "delGroup", "addSensorObj"):
            self.assertNotIn(forbidden, src, f"смена режима шлёт на шину: {forbidden}")

    def test_switch_is_never_automatic(self):
        """Годность серийников оценивает человек — программа режим сама не меняет."""
        coord = (pathlib.Path(__file__).resolve().parents[1] / "custom_components"
                 / "arvid_dali_center" / "coordinator.py").read_text(encoding="utf-8")
        self.assertNotIn("switch_mode", coord, "координатор переключает режим сам")


if __name__ == "__main__":
    unittest.main()
