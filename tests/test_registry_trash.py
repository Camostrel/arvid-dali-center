"""Сторож чистки КОРЗИНЫ реестров HA (v1.2.60) — без Home Assistant, разбором кода (ast).

ЗАЧЕМ ИМЕННО СТОРОЖ. Штатной команды «удали из корзины» у HA нет, и мы работаем со
СЛУЖЕБНЫМИ полями реестра (`deleted_entities` / `deleted_devices`). Публичным контрактом это
не покрыто: обновится HA — поле может переехать или сменить структуру ключа, и чистка тихо
перестанет работать. Тихо — это худший исход: «бывшие» снова начнут всплывать на объекте, а
мы узнаем об этом от пусконаладчика.

Здесь фиксируется то, что должно остаться верным:
  • чистка вызывается ТОЛЬКО из ручных операций (авто-деструктива нет — принцип проекта);
  • обращения к служебным полям обёрнуты в suppress, чтобы смена контракта не роняла интеграцию;
  • ключ корзины сущностей — тройка (domain, platform, unique_id).
Проверить сами поля на живом HA тесты не могут (HA в окружении тестов нет), поэтому в
`docs/DEBT.md` §T5 записано, что при обновлении HA это место проверяется руками.
"""

import ast
import unittest
from pathlib import Path

WS = Path(__file__).resolve().parent.parent / "custom_components" / "arvid_dali_center" / "websocket_api.py"


class TestRegistryTrash(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.src = WS.read_text(encoding="utf-8")
        cls.tree = ast.parse(cls.src)
        cls.funcs = {n.name: n for n in ast.walk(cls.tree)
                     if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}

    def test_purge_function_exists(self):
        self.assertIn("purge_registry_trash", self.funcs)
        self.assertIn("_trash_keys_for_uids", self.funcs)

    def test_key_is_domain_platform_unique_id(self):
        """Ключ корзины сущностей — тройка; фильтруем по нашему DOMAIN и своим unique_id."""
        body = ast.get_source_segment(self.src, self.funcs["_trash_keys_for_uids"])
        self.assertIn("len(key) == 3", body)
        self.assertIn("key[1] == DOMAIN", body)
        self.assertIn("key[2] in unique_ids", body)

    def test_service_fields_guarded(self):
        """Служебные поля — только под suppress: смена контракта HA не должна ронять запись."""
        body = ast.get_source_segment(self.src, self.funcs["purge_registry_trash"])
        for field in ("deleted_entities", "deleted_devices"):
            idx = body.index(field)
            head = body[:idx]
            self.assertIn("contextlib.suppress", head,
                          f"{field} используется вне suppress — обновление HA уронит интеграцию")

    def test_saves_after_purge(self):
        body = ast.get_source_segment(self.src, self.funcs["purge_registry_trash"])
        self.assertIn("async_schedule_save", body)

    def test_callers_are_manual_only(self):
        """Чистку зовут ТОЛЬКО ручные операции: «Забыть», «Стереть данные», команда корзины.

        Если вызов появится в скане/реконcile/загрузке — это авто-деструктив, чего в проекте
        не бывает по принципу: проблемы должны быть ВИДНЫ, а решает человек.
        """
        allowed = {"ws_forget_device", "ws_wipe_gateway_data", "ws_registry_trash"}
        callers = set()
        for name, node in self.funcs.items():
            for call in ast.walk(node):
                if (isinstance(call, ast.Call) and isinstance(call.func, ast.Name)
                        and call.func.id == "purge_registry_trash"):
                    callers.add(name)
        self.assertTrue(callers, "никто не зовёт purge_registry_trash — чистка мертва")
        self.assertEqual(callers - allowed, set(),
                         f"чистку корзины зовёт кто-то ещё: {callers - allowed}")

    def test_purge_runs_after_device_removal(self):
        """🔴 ПОРЯДОК в «Забыть»: чистим корзину ПОСЛЕ сноса карточки устройства.

        Иначе получается бессмыслица, которую поймал пользователь 2026-08-12: сущности
        вымели, а карточка уехала в `deleted_devices` уже после уборки — и осталась там.
        Тест держит порядок, потому что по коду он неочевиден: снос идёт внутри `if`, а
        чистка — за ним.
        """
        body = ast.get_source_segment(self.src, self.funcs["ws_forget_device"])
        remove_at = body.index("dreg.async_remove_device(")
        purge_at = body.index("purge_registry_trash(hass")
        self.assertGreater(purge_at, remove_at,
                           "корзина чистится ДО сноса карточки — карточка в ней и останется")

    def test_preview_does_not_purge(self):
        """`registry_trash` без `purge` обязана только ПОКАЗЫВАТЬ (человек смотрит, потом решает)."""
        body = ast.get_source_segment(self.src, self.funcs["ws_registry_trash"])
        self.assertIn('if msg.get("purge")', body)
        purge_at = body.index('if msg.get("purge")')
        call_at = body.index("purge_registry_trash(")
        self.assertGreater(call_at, purge_at, "чистка вызывается ДО проверки флага purge")


if __name__ == "__main__":
    unittest.main()
