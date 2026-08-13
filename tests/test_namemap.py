"""Тесты карты сопоставления «адрес → имя» — ЧИСТЫЕ функции `namemap.py`, без Home Assistant.

ЗАЧЕМ ЭТИ ТЕСТЫ. Карта применяется массово (на объекте ~1250 строк), и ошибка сшивки — это
тихое переименование НЕ ТОГО устройства: имя ляжет на соседа по адресу, а человек увидит
«применено» и пойдёт дальше. Поэтому здесь зафиксировано ровно то, что должно быть видно
глазами: противоречия в файле не глотаются, осиротевшие к применению не предлагаются, а
«уже названо так же» не выдаётся за работу.

Запуск (из корня проекта, HA не нужен):
    python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import importlib.util
import sys
import types
import unittest
from pathlib import Path

# namemap.py делает `from .naming import sensor_body` → нужен пакет. Собираем минимальный:
# сам пакет пустой, внутрь кладём только naming.py (он тоже чистый — stdlib и re).
_DIR = Path(__file__).resolve().parent.parent / "custom_components" / "arvid_dali_center"
_PKG = "arvid_dali_center"
if _PKG not in sys.modules:
    _pkg = types.ModuleType(_PKG)
    _pkg.__path__ = [str(_DIR)]
    sys.modules[_PKG] = _pkg
for _name in ("naming", "namemap"):
    _spec = importlib.util.spec_from_file_location(f"{_PKG}.{_name}", _DIR / f"{_name}.py")
    _mod = importlib.util.module_from_spec(_spec)
    sys.modules[f"{_PKG}.{_name}"] = _mod
    _spec.loader.exec_module(_mod)
namemap = sys.modules[f"{_PKG}.namemap"]

GW = "2E24350891E2"          # шлюз линии 4.1 (объект «школа №45»)
OTHER = "22250205565A"       # соседний шлюз — его строки не должны попадать в таблицу

HEAD = ("gw_sn;devtype;address;role;project_key;target_name;group_id;space;"
        "source;verify;danger;note")


def csv_of(*lines: str) -> str:
    return "\n".join((HEAD, *lines)) + "\n"


def dev(devtype="0101", address=0, name="", devsn="AABBCCDD", **kw) -> dict:
    """Запись устройства в формате WS `devices` (что отдаёт бэкенд карточке)."""
    d = {"devType": devtype, "channel": 0, "address": address, "name": name,
         "devSn": devsn, "status": "online", "zombie": False, "orphan": False,
         "key": f"{devtype}:0:{address}"}
    d.update(kw)
    return d


class TestParse(unittest.TestCase):
    """Разбор файла: что принимаем и о чём обязаны сказать вслух."""

    def test_basic(self):
        rows, problems = namemap.parse_map(csv_of(
            f"{GW};0101;43;light;4.1.52;l_4_1_52;417_3;417_Коридор;таблица;YES;;переехало с 4.2.1"))
        self.assertEqual(problems, [])
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertEqual((r["gw_sn"], r["devtype"], r["address"]), (GW, "0101", 43))
        self.assertEqual(r["target_name"], "l_4_1_52")
        self.assertTrue(r["verify"])          # YES из колонки verify → флаг «проверить на месте»

    def test_bom_and_excel_damage(self):
        """BOM в начале файла, съеденный ведущий ноль devtype и адрес «12.0» — всё это норма."""
        rows, problems = namemap.parse_map("﻿" + csv_of(f"{GW};101;12.0;light;4.1.1;l_4_1_1;;;;;;"))
        self.assertEqual(problems, [])
        self.assertEqual((rows[0]["devtype"], rows[0]["address"]), ("0101", 12))

    def test_missing_column_is_fatal(self):
        rows, problems = namemap.parse_map("gw_sn;devtype;address\nX;0101;1\n")
        self.assertEqual(rows, [])
        self.assertIn("target_name", problems[0])

    def test_empty_name_reported_not_swallowed(self):
        """Пустое имя = устройство останется безымянным. Молча пропустить нельзя."""
        rows, problems = namemap.parse_map(csv_of(f"{GW};0101;5;light;4.1.5;;;;;;;"))
        self.assertEqual(rows, [])
        self.assertEqual(len(problems), 1)
        self.assertIn("пустое target_name", problems[0])

    def test_duplicate_address_takes_first_and_warns(self):
        """Два имени на один адрес — противоречие карты: берём первое и говорим об этом."""
        rows, problems = namemap.parse_map(csv_of(
            f"{GW};0101;7;light;4.1.7;l_4_1_7;;;;;;",
            f"{GW};0101;7;light;4.1.8;l_4_1_8;;;;;;"))
        self.assertEqual([r["target_name"] for r in rows], ["l_4_1_7"])
        self.assertEqual(len(problems), 1)
        self.assertIn("адрес уже занят", problems[0])


class TestStitch(unittest.TestCase):
    """Сшивка карты со сканом: три категории строк и запреты на применение."""

    def setUp(self):
        self.rows, _ = namemap.parse_map(csv_of(
            f"{GW};0101;43;light;4.1.52;l_4_1_52;417_3;417_Коридор;таблица;YES;;переехало с 4.2.1",
            f"{GW};0101;44;light;4.1.55;l_4_1_55;417_3;417_Коридор;таблица;;;",
            f"{OTHER};0101;17;light;4.3.24;l_4_3_24;532_0;532_Лестница;таблица;YES;;"))

    def test_three_categories(self):
        table = namemap.stitch(self.rows, [dev(address=43), dev(address=99)], GW)
        by_addr = {r["address"]: r for r in table}
        self.assertEqual(by_addr[43]["status"], namemap.ST_MATCHED)      # карта + шина
        self.assertEqual(by_addr[99]["status"], namemap.ST_NOT_IN_MAP)   # на шине, карта молчит
        self.assertEqual(by_addr[44]["status"], namemap.ST_NOT_ON_BUS)   # карта знает, скана нет
        self.assertTrue(by_addr[44]["skip"])                             # применять нечему

    def test_other_gateway_rows_do_not_leak(self):
        """Строка соседнего шлюза с тем же адресом не должна подставиться: адреса 0..63 есть
        на КАЖДОМ контроллере — ровно так подменялась цель привязки до v1.2.38."""
        table = namemap.stitch(self.rows, [dev(address=17)], GW)
        self.assertEqual(len(table), 3)                                  # 17 + два «нет на шине»
        row17 = next(r for r in table if r["address"] == 17 and r["devSn"])
        self.assertEqual(row17["status"], namemap.ST_NOT_IN_MAP)
        self.assertEqual(row17["target_name"], "")

    def test_verify_flag_survives(self):
        table = namemap.stitch(self.rows, [dev(address=43)], GW)
        row = next(r for r in table if r["address"] == 43)
        self.assertTrue(row["verify"])
        self.assertEqual(row["project_key"], "4.1.52")
        self.assertEqual(row["space"], "417_Коридор")

    def test_orphan_is_not_offered(self):
        """Осиротевший держит адрес НОВОГО жильца — переименование ушло бы не туда (v1.2.2)."""
        table = namemap.stitch(self.rows, [dev(address=43, orphan=True)], GW)
        row = next(r for r in table if r["address"] == 43)
        self.assertTrue(row["skip"])
        self.assertTrue(any("осиротевш" in w for w in row["warn"]))

    def test_zombie_is_not_offered(self):
        table = namemap.stitch(self.rows, [dev(address=43, zombie=True)], GW)
        self.assertTrue(next(r for r in table if r["address"] == 43)["skip"])

    def test_no_devsn_warns(self):
        """Без серийника имя ляжет только в реестр HA и не переживёт сброс (v1.2.51)."""
        table = namemap.stitch(self.rows, [dev(address=43, devsn="")], GW)
        row = next(r for r in table if r["address"] == 43)
        self.assertTrue(any("devSn" in w for w in row["warn"]))
        self.assertFalse(row["skip"])          # применить всё же можно — это предупреждение

    def test_already_named_is_not_work(self):
        table = namemap.stitch(self.rows, [dev(address=43, name="l_4_1_52")], GW)
        self.assertTrue(next(r for r in table if r["address"] == 43)["same"])

    def test_sensor_name_compared_by_body(self):
        """`rename` принимает и `ms_4_3_9`, и `4_3_9` (sensor_body режет префикс). Без
        нормализации карточка предлагала бы переименовать уже названный датчик."""
        rows, _ = namemap.parse_map(csv_of(f"{GW};0201;6;ms;4.1.12;ms_4_1_12;;;;;;"))
        table = namemap.stitch(rows, [dev(devtype="0201", address=6, name="ms_4_1_12")], GW)
        self.assertTrue(table[0]["same"])
        table = namemap.stitch(rows, [dev(devtype="0201", address=6, name="4_1_12")], GW)
        self.assertTrue(table[0]["same"])
        table = namemap.stitch(rows, [dev(devtype="0201", address=6, name="ms_4_1_99")], GW)
        self.assertFalse(table[0]["same"])

    def test_lux_is_paired_not_a_problem(self):
        """Освещённость отдельно НЕ именуется: движение и люкс — одно устройство (общий devSn),
        `rename` переименует пару. Карта строк 0202 не содержит, и люкс на шине не должен
        выглядеть «нет в карте» — иначе на объекте это 190 строк мнимых проблем."""
        rows, _ = namemap.parse_map(csv_of(f"{GW};0201;5;ms;2.1.1;ms_2_1_1;;;;;;"))
        table = namemap.stitch(rows, [dev(devtype="0201", address=5, devsn="SN1"),
                                      dev(devtype="0202", address=6, devsn="SN1")], GW)
        lux = next(r for r in table if r["devType"] == "0202")
        self.assertEqual(lux["status"], namemap.ST_PAIRED)
        self.assertTrue(lux["skip"])
        self.assertEqual(namemap.summary(table)["not_in_map"], 0)

    def test_lonely_lux_is_still_visible(self):
        """А вот люкс БЕЗ движения (своего devSn нет среди 0201) прятать нельзя — это
        устройство, о котором карта молчит, и человек должен его увидеть."""
        rows, _ = namemap.parse_map(csv_of(f"{GW};0201;5;ms;2.1.1;ms_2_1_1;;;;;;"))
        table = namemap.stitch(rows, [dev(devtype="0202", address=6, devsn="SN9")], GW)
        lux = next(r for r in table if r["devType"] == "0202")
        self.assertEqual(lux["status"], namemap.ST_NOT_IN_MAP)

    def test_lux_row_in_map_is_ignored(self):
        """Даже если строка 0202 в карте всё-таки есть (старая версия файла) — применять её
        нельзя: адрес люкса живёт в СВОЁМ адресном пространстве, и по нему легко зацепить
        чужой прибор. Пара с движением важнее любой строки карты."""
        rows, _ = namemap.parse_map(csv_of(
            f"{GW};0201;5;ms;2.1.1;ms_2_1_1;;;;;;",
            f"{GW};0202;6;il;2.1.2;il_2_1_2;;;;;;"))
        table = namemap.stitch(rows, [dev(devtype="0201", address=5, devsn="SN1"),
                                      dev(devtype="0202", address=6, devsn="SN1")], GW)
        lux = next(r for r in table if r["devType"] == "0202")
        self.assertEqual(lux["status"], namemap.ST_PAIRED)
        self.assertTrue(lux["skip"])


class TestDangerAndLog(unittest.TestCase):
    """«Опасные» (отвалившиеся / проблема с адресом) и журнал неназванного."""

    def test_danger_not_counted_as_ready(self):
        """Опасное имя предложить можно, но в «к работе» оно не идёт — карточка по этому
        счётчику проставляет галочки, а применять такое вслепую нельзя: адрес мог уехать."""
        rows, _ = namemap.parse_map(csv_of(
            f"{GW};0101;1;light;5.1.34;l_5_1_34;;;;;YES;отвалившееся",
            f"{GW};0101;2;light;5.1.35;l_5_1_35;;;;;;"))
        table = namemap.stitch(rows, [dev(address=1), dev(address=2)], GW)
        s = namemap.summary(table)
        self.assertEqual(s["danger"], 1)
        self.assertEqual(s["ready"], 1)            # только неопасная строка
        self.assertTrue(next(r for r in table if r["address"] == 1)["danger"])

    def test_unnamed_report_lists_everything_left(self):
        """Всё, что останется без имени, должно попасть в журнал: и «нет в карте», и
        «нет на шине», и опасные, и осиротевшие."""
        rows, _ = namemap.parse_map(csv_of(
            f"{GW};0101;1;light;5.1.34;l_5_1_34;;;;;YES;",     # опасное
            f"{GW};0101;3;light;5.1.36;l_5_1_36;;;;;;"))       # его нет на шине
        table = namemap.stitch(rows, [dev(address=1), dev(address=9)], GW)   # 9 — нет в карте
        left = namemap.unnamed(table)
        self.assertEqual(len(left), 3)
        joined = " | ".join(left)
        self.assertIn("опасное", joined)
        self.assertIn("нет в карте", joined)
        self.assertIn("скан не нашёл", joined)

    def test_already_named_not_in_report(self):
        rows, _ = namemap.parse_map(csv_of(f"{GW};0101;1;light;5.1.34;l_5_1_34;;;;;;"))
        table = namemap.stitch(rows, [dev(address=1, name="l_5_1_34")], GW)
        self.assertEqual(namemap.unnamed(table), [])


class TestSummary(unittest.TestCase):
    def test_counters(self):
        rows, _ = namemap.parse_map(csv_of(
            f"{GW};0101;1;light;4.1.1;l_4_1_1;;;;;;",
            f"{GW};0101;2;light;4.1.2;l_4_1_2;;;;YES;;",
            f"{GW};0101;3;light;4.1.3;l_4_1_3;;;;;;"))
        table = namemap.stitch(rows, [dev(address=1, name="l_4_1_1"),   # уже названа
                                      dev(address=2),                   # к применению
                                      dev(address=9)], GW)              # нет в карте
        s = namemap.summary(table)
        self.assertEqual(s["matched"], 2)
        self.assertEqual(s["not_in_map"], 1)
        self.assertEqual(s["not_on_bus"], 1)      # адрес 3 из карты не найден
        self.assertEqual(s["already"], 1)
        self.assertEqual(s["verify"], 1)
        self.assertEqual(s["ready"], 1)           # ровно одна строка требует работы


class TestNeedsWork(unittest.TestCase):
    """«Нужна ли работа» — ренейм ИЛИ простановка области (v1.2.57).

    Повод: офисный прогон 2026-08-11. Имена применились, области нет (старый фронт слал `area`
    вместо `area_id`), и вернуться было нечем — строки с верным именем выпадали из отметок
    навсегда, «там уже всё названо». Второй проход по объекту обязан их видеть.
    """

    def row(self, **kw):
        base = {"status": namemap.ST_MATCHED, "skip": False, "danger": False, "same": False,
                "area_id": "", "area_current": ""}
        base.update(kw)
        return base

    def test_rename_needed(self):
        self.assertTrue(namemap.needs_work(self.row()))

    def test_all_done(self):
        self.assertFalse(namemap.needs_work(
            self.row(same=True, area_id="103_dver", area_current="103_dver")))

    def test_name_ok_but_area_missing(self):
        """🔴 Ровно случай офиса: имя стоит, область не проставлена."""
        self.assertTrue(namemap.needs_work(self.row(same=True, area_id="103_dver")))

    def test_name_ok_but_area_differs(self):
        self.assertTrue(namemap.needs_work(
            self.row(same=True, area_id="103_dver", area_current="kab_301")))

    def test_name_ok_and_map_has_no_area(self):
        """Карта области не задаёт — работы нет (чужую область не трогаем)."""
        self.assertFalse(namemap.needs_work(self.row(same=True, area_current="kab_301")))

    def test_danger_never_auto(self):
        self.assertFalse(namemap.needs_work(self.row(danger=True, area_id="103_dver")))

    def test_skip_never_auto(self):
        self.assertFalse(namemap.needs_work(self.row(skip=True)))

    def test_other_statuses(self):
        for st in (namemap.ST_NOT_ON_BUS, namemap.ST_NOT_IN_MAP, namemap.ST_PAIRED):
            self.assertFalse(namemap.needs_work(self.row(status=st)))

    def test_stitch_rows_carry_area_current(self):
        """Поле есть у всех строк — карточка не должна получать undefined."""
        table = namemap.stitch(*_stitch_args())
        for r in table:
            self.assertIn("area_current", r)


def _stitch_args():
    rows, _ = namemap.parse_map(csv_of(f"{GW};0101;0;light;1.1.1;l_1_1_1;;;;;"))
    return rows, [dev(address=0), dev(address=1)], GW


if __name__ == "__main__":
    unittest.main()
