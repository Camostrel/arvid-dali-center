"""Чистые помощники расписания датчиков (`runCondition` devType 0701) — БЕЗ зависимостей от HA.

Вынесено из `services.py`, чтобы покрывалось stdlib-тестами (tests/, без Home Assistant).
Формат окна подтверждён захватом DALI Center 2026-07-29: строка "HH:MM-HH:MM", несколько окон —
массивом в ОДНОМ условии (docs/PLAN_SENSOR_BINDINGS §H4).
"""

from __future__ import annotations

import re

_WINDOW_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)-([01]\d|2[0-3]):([0-5]\d)$")


class WindowError(ValueError):
    """Окно расписания не по формату / бессмысленное."""


def _minutes(hh: str, mm: str) -> int:
    return int(hh) * 60 + int(mm)


def validate_window(win: str) -> str:
    """Проверить одно окно "HH:MM-HH:MM" и вернуть нормализованную строку.

    ТОЛЬКО ВНУТРИ ДНЯ: конец должен быть строго позже начала. Переход через полночь
    ("22:00-06:00") ЗАПРЕЩЁН — решение пользователя (v1.2.25): DALI Center такие окна задать не
    даёт, поведение шлюза на них неизвестно, и «ночной» сценарий, тихо не сработавший на объекте,
    хуже явного отказа при вводе. Ночь задаётся ДВУМЯ окнами: "22:00-23:59" + "00:00-06:00".

    Вырожденное окно (начало == конец) тоже отклоняем: «никогда» это или «всегда» — неизвестно,
    а двусмысленность в расписании света недопустима."""
    w = str(win).strip()
    m = _WINDOW_RE.match(w)
    if not m:
        raise WindowError(f"окно «{win}» не в формате HH:MM-HH:MM (например 08:00-17:30)")
    start, end = _minutes(m[1], m[2]), _minutes(m[3], m[4])
    if start == end:
        raise WindowError(f"окно «{w}»: начало и конец совпадают — смысл неоднозначен")
    if start > end:
        raise WindowError(
            f"окно «{w}»: через полночь нельзя — задайте двумя окнами "
            f"({m[1]}:{m[2]}-23:59 и 00:00-{m[3]}:{m[4]})")
    return w


def normalize_windows(windows) -> list[str]:
    """Проверить список окон + отсеять дубли, СОХРАНИВ порядок.

    Пересечения НЕ схлопываем: как шлюз трактует перекрытие внутри одного `value` — не проверено
    (гейт). Молча менять заданное человеком расписание нельзя — пусть видит, что задал."""
    out: list[str] = []
    for w in (windows or []):
        v = validate_window(w)
        if v not in out:
            out.append(v)
    return out


def windows_overlap(windows: list[str]) -> list[tuple[str, str]]:
    """Пары ПЕРЕСЕКАЮЩИХСЯ окон — предупреждение, не ошибка (как шлюз трактует перекрытие
    внутри одного `value` — на железе не проверено). Все окна — внутри дня (см. validate_window),
    поэтому сравнение однозначно. Стык («…-13:00» и «13:00-…») пересечением НЕ считаем."""
    def rng(w: str):
        m = _WINDOW_RE.match(w)
        return (_minutes(m[1], m[2]), _minutes(m[3], m[4])) if m else None

    pairs: list[tuple[str, str]] = []
    items = [(w, rng(w)) for w in windows]
    for i, (w1, r1) in enumerate(items):
        for w2, r2 in items[i + 1:]:
            if r1 and r2 and r1[0] < r2[1] and r2[0] < r1[1]:
                pairs.append((w1, w2))
    return pairs
