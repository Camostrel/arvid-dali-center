"""Сторож хранилищ (S5): каждое умеет убирать за собой и включено в реестр чистки.

Почему через `ast`, а не импортом: `store.py` тянет Home Assistant, которого в наших
stdlib-тестах нет. Разбор исходника даёт ровно то, что нужно проверить, — наличие методов и
состав реестра, — и не требует окружения HA.

Ради чего сторож. Дыры S1–S4 (docs/DEBT.md §S) появились не от сложности, а от
невнимательности: завели стор и забыли дописать его в СПИСКИ чистки, а списков было три и
разных. Ошибка невидима — код работает, мусор копится. Тест делает её видимой сразу.
"""

from __future__ import annotations

import ast
import pathlib
import unittest

STORE_PY = (pathlib.Path(__file__).resolve().parent.parent
            / "custom_components" / "arvid_dali_center" / "store.py")

REQUIRED = ("purge_identity", "purge_gateway")
BASE_CLASS = "PurgeableStore"


def _tree() -> ast.Module:
    return ast.parse(STORE_PY.read_text(encoding="utf-8"))


def _store_classes(tree: ast.Module) -> list[ast.ClassDef]:
    """Классы-хранилища: имя оканчивается на Store, кроме самого базового."""
    return [n for n in tree.body
            if isinstance(n, ast.ClassDef) and n.name.endswith("Store")
            and n.name != BASE_CLASS]


class TestStoresPurgeable(unittest.TestCase):

    def test_found_stores(self):
        """Сторож бесполезен, если классы перестали находиться (переименование/переезд)."""
        names = [c.name for c in _store_classes(_tree())]
        self.assertGreaterEqual(len(names), 10, f"хранилищ найдено мало: {names}")

    def test_every_store_inherits_base(self):
        for cls in _store_classes(_tree()):
            bases = [b.id for b in cls.bases if isinstance(b, ast.Name)]
            self.assertIn(BASE_CLASS, bases,
                          f"{cls.name} не наследует {BASE_CLASS} — он не попадёт в чистку")

    def test_every_store_implements_purge(self):
        for cls in _store_classes(_tree()):
            methods = {n.name for n in cls.body
                       if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
            for req in REQUIRED:
                self.assertIn(req, methods,
                              f"{cls.name}.{req} не реализован. Если чистка не нужна — "
                              f"метод всё равно обязателен: верните 0 и напишите ПОЧЕМУ "
                              f"(иначе «забыли» и «решили не чистить» неразличимы)")

    def test_every_store_has_purge_name(self):
        """`purge_name` попадает в отчёт чистки — без него в логе будет «?»."""
        for cls in _store_classes(_tree()):
            assigns = {t.id for n in cls.body if isinstance(n, ast.Assign)
                       for t in n.targets if isinstance(t, ast.Name)}
            self.assertIn("purge_name", assigns, f"{cls.name} без purge_name")

    def test_all_stores_registered(self):
        """Каждый класс-хранилище должен попасть в реестр `_ALL_STORES_HASS_KEY`."""
        src = STORE_PY.read_text(encoding="utf-8")
        marker = "hass.data[_ALL_STORES_HASS_KEY] = ["
        self.assertIn(marker, src, "реестр сторов не найден — чистка ходить не по чему")
        registry = src.split(marker, 1)[1].split("]", 1)[0]
        # в реестре лежат локальные переменные setup — сверяем по числу, а не по именам
        count = len([x for x in registry.replace("\n", " ").split(",") if x.strip()])
        self.assertEqual(count, len(_store_classes(_tree())),
                         "в реестре чистки не все хранилища — новый стор забыли добавить")


if __name__ == "__main__":
    unittest.main()
