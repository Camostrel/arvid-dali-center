"""Чистые помощники привязок панелей/датчиков (состав ячейки outObj) — БЕЗ зависимостей от HA.

Вынесено из `websocket_api.py`, чтобы покрывалось stdlib-тестами (tests/, без Home Assistant):
здесь живёт вся арифметика состава ячейки, а она и была источником тихих отказов.

⚠ ЭТАЛОН — захват DALI Center 2026-08-03 (кросс-шлюзовая привязка кнопки, потом «+ цель»):
ячейка (кнопка × жест) ВСЕГДА переписывается целиком —
    readPanel → delPanelObj(ВСЕ текущие) → addPanelObj(ПОЛНЫЙ новый состав) → setPanelArg → readPanel
Инкрементального добавления DALI Center не использует даже когда человек жмёт «+ цель».

⚠ ШЛЮЗ ЦЕЛИ (`gwSnObj`) — ЧАСТЬ ИДЕНТИЧНОСТИ цели, а не украшение. Короткие адреса 0..63
живут на КАЖДОМ контроллере, поэтому ключ без шлюза склеивает разные лампы: сверка считала
«совпало», когда контроллер записал свою лампу вместо цели на чужом шлюзе (v1.2.38).
"""

from __future__ import annotations


def target_key(o: dict, gw_sn: str | None = None) -> tuple:
    """Ключ цели привязки: (gwSnObj, devType, channel, address).

    Пустой `gwSnObj` = свой шлюз → нормализуем к `gw_sn`, иначе одна и та же цель в двух
    формах записи (с полем и без) дала бы два разных ключа. Регистр серийника не значим."""
    gw = str(o.get("gwSnObj") or gw_sn or "").upper()
    return (gw, str(o.get("devType")), o.get("channel"), o.get("address"))


def target_set(out_obj, gw_sn: str | None = None) -> set:
    """Набор целей для сверки «запрошено ⊆ фактически» (ключ — с учётом шлюза цели)."""
    return {target_key(o, gw_sn) for o in (out_obj or [])}


def cell_target(o: dict) -> dict:
    """Цель в форме, которую пишет на шину DALI Center: devType/address/channel/gwSnObj/property.
    `property` всегда список (контроллер не любит None), лишние поля (`act`) отбрасываем."""
    return {"devType": str(o.get("devType")), "address": o.get("address"),
            "channel": o.get("channel"), "gwSnObj": o.get("gwSnObj"),
            "property": o.get("property") or []}


def merge_targets(current, incoming, gw_sn: str | None = None) -> list:
    """Полный НОВЫЙ состав ячейки: текущие цели + добавляемые.

    Цель с тем же ключом ЗАМЕНЯЕТСЯ новой — это правка действия у уже привязанной цели,
    а не второй её экземпляр. Порядок существующих целей сохраняется (человек видит список
    в том же порядке, что и до правки)."""
    out: list = []
    idx: dict = {}
    for o in list(current or []) + list(incoming or []):
        k = target_key(o, gw_sn)
        if k in idx:
            out[idx[k]] = cell_target(o)
        else:
            idx[k] = len(out)
            out.append(cell_target(o))
    return out


def targets_by_gateway(targets, gw_sn: str | None = None) -> dict:
    """Разложить состав ячейки по КОНТРОЛЛЕРАМ-ВЛАДЕЛЬЦАМ целей: {серийник: [цели]}.

    Нужно потому, что ячейка живёт на ДВУХ шлюзах (захват 2026-08-04): полный состав — на
    шлюзе панели, а на каждом шлюзе цели — только ЕГО собственные цели. Пустой `gwSnObj` =
    свой шлюз. Регистр серийника в ключе сохраняем как в первом вхождении (его подставляем
    в payload), сравниваем — без регистра."""
    out: dict = {}
    for o in (targets or []):
        gw = str(o.get("gwSnObj") or gw_sn or "")
        for known in out:                     # один шлюз в двух регистрах — одна корзина
            if known.upper() == gw.upper():
                gw = known
                break
        out.setdefault(gw, []).append(cell_target(o))
    return out


def foreign_gateway_targets(targets, gw_sn: str | None = None) -> dict:
    """То же, но ТОЛЬКО чужие контроллеры (свой шлюз исключён) — им нужна отдельная запись."""
    own = str(gw_sn or "").upper()
    return {gw: lst for gw, lst in targets_by_gateway(targets, gw_sn).items()
            if gw.upper() != own}


def remaining_targets(current, drop, gw_sn: str | None = None) -> list:
    """Остаток ячейки после снятия целей `drop` (в форме записи на шину).

    Нужен потому, что выборочного удаления у контроллера мы не наблюдали ни разу: снимаем
    ВСЕ цели, затем возвращаем остаток одним addPanelObj."""
    drop_keys = target_set(drop, gw_sn)
    return [cell_target(o) for o in (current or []) if target_key(o, gw_sn) not in drop_keys]


# ── Типы событий панели (event-сущность) ─────────────────────────────────────────
# С v1.2.45 номер клавиши входит в ТИП события (`key3_click`), а не только в атрибут:
# иначе триггер автоматизации ловит нажатие ЛЮБОЙ клавиши, и отделить нужную можно
# лишь шаблонным условием. Здесь — чистая арифметика списка, платформа `event.py`
# только раздаёт её сущностям.
GESTURES = ["click", "hold", "double", "rotate", "hold_end"]


def panel_key_count(devtype) -> int:
    """Число клавиш из devType `03NN`: 0302→2, 0304→4, 0306→6, 0308→8; 0300 — поворотная (0)."""
    try:
        return int(str(devtype)[2:])
    except (TypeError, ValueError):
        return 0


def key_event_types(devtype) -> list[str]:
    """Полный список типов событий сущности панели: `key<N>_<жест>` + голые жесты.

    Голые жесты нужны не для совместимости, а по необходимости: HA принимает только тип из
    этого списка, а `keyNo` у поворотной панели (0300) не приходит вовсе и у любой панели
    может прийти вне диапазона — событие обязано быть выпущено, а не потеряно молча."""
    n = panel_key_count(devtype)
    return [f"key{k}_{g}" for k in range(1, n + 1) for g in GESTURES] + list(GESTURES)


def key_event_type(devtype, key_no, gesture: str) -> str | None:
    """Тип события для (клавиша, жест) или `None`, если такой клавиши у devType нет.

    `None` — сигнал вызывающему: выпускать голым жестом и оставить след в журнале."""
    try:
        k = int(key_no)
    except (TypeError, ValueError):
        return None
    return f"key{k}_{gesture}" if 1 <= k <= panel_key_count(devtype) else None
