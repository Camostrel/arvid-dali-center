"""Чистая логика КРОСС-ШЛЮЗОВЫХ DALI-групп — БЕЗ зависимостей от Home Assistant.

⚠ ЭТАЛОН — синхронный захват ДВУХ шлюзов (2026-08-04, группа «New Group1»):
кросс-шлюзовая группа = ОДИН И ТОТ ЖЕ `groupId` + ОДНО И ТО ЖЕ имя, заведённые на КАЖДОМ
контроллере, каждому — ТОЛЬКО ЕГО лампы. Никакого «главного» шлюза и объекта-моста нет;
`writeGroup` бьёт только по лампам своего шлюза, поэтому команду шлём веером на всех
участников. Флага `crossGateway` DALI Center не шлёт — он ни при чём (docs/CROSS_GATEWAY.md §2).

Здесь живёт только арифметика (разбивка состава, выбор свободного номера) — она под
stdlib-тестами `tests/test_group_ops.py`. Работа с шиной и сущностями — в вызывающем коде.
"""

from __future__ import annotations

# DALI-групп на линии физически 16 (0–15) — и номер должен быть свободен на КАЖДОМ
# участвующем контроллере, иначе на одном создастся, а на другом ляжет ПОВЕРХ чужой группы.
DALI_GROUP_MIN = 0
DALI_GROUP_MAX = 15


def member_key(m: dict) -> tuple:
    """Ключ лампы-члена: (gwSnObj, devType, channel, address). Шлюз — часть идентичности:
    короткие адреса 0..63 живут на КАЖДОМ контроллере (та же грабля, что у панелей)."""
    return (str(m.get("gwSnObj") or "").upper(), str(m.get("devType")),
            m.get("channel"), m.get("address"))


def members_by_gateway(members, default_gw: str | None = None) -> dict:
    """Разложить состав по контроллерам-владельцам: {серийник: [члены]}.

    Каждому шлюзу уйдёт СВОЙ `addGroup` только с его лампами. Пустой `gwSnObj` = `default_gw`
    (состав из старой однолшлюзовой карточки). Регистр серийника берём как в первом
    вхождении — его подставляем в payload; сравниваем без регистра."""
    out: dict = {}
    for m in (members or []):
        gw = str(m.get("gwSnObj") or default_gw or "")
        for known in out:
            if known.upper() == gw.upper():
                gw = known
                break
        out.setdefault(gw, []).append({
            "devType": str(m.get("devType")), "channel": m.get("channel"),
            "address": m.get("address"), "gwSnObj": gw,
        })
    return out


def participants(members, default_gw: str | None = None) -> list:
    """Шлюзы-участники группы, в порядке первого появления в составе."""
    return list(members_by_gateway(members, default_gw))


def is_cross_gateway(members, default_gw: str | None = None) -> bool:
    """Группа кросс-шлюзовая, если её лампы лежат больше чем на одном контроллере.
    Однолшлюзовая группа — ОТДЕЛЬНАЯ модель (решение 2026-08-04), сюда не превращается."""
    return len(participants(members, default_gw)) > 1


def free_group_ids(used_by_gw: dict) -> list:
    """Номера, свободные на ВСЕХ участниках сразу.

    `used_by_gw` = {серийник: множество занятых groupId}. Шлюз без записи считается пустым.
    ⚠ Проверять надо ИМЕННО пересечение: номер, свободный у одного контроллера, у другого
    может быть занят — тогда `addGroup` ляжет поверх чужой группы, и это НЕ будет видно
    (`readGroup` вернёт нашу таблицу, а биты на чужих лампах уже перезаписаны)."""
    free = set(range(DALI_GROUP_MIN, DALI_GROUP_MAX + 1))
    for used in (used_by_gw or {}).values():
        free -= set(used or ())
    return sorted(free)


def group_id_conflicts(group_id: int, used_by_gw: dict) -> list:
    """Список шлюзов, у которых номер УЖЕ занят (для честного отказа до записи на шину)."""
    return sorted(gw for gw, used in (used_by_gw or {}).items() if group_id in set(used or ()))


def gw_tail(gw_sn: str, n: int = 5) -> str:
    """Хвост серийника шлюза (по умолчанию 5 знаков), в нижнем регистре.

    ⚠ Именно ХВОСТ, а не начало: серийник шлюза — это MAC (`e2:24:35:08:87:27` →
    `E22435088727`), и у партии одинаковых контроллеров совпадает НАЧАЛО (вендорный
    префикс), а различается хвост. Плюс `sn5` во всём проекте означает последние знаки."""
    return str(gw_sn or "").strip().lower()[-n:]


def cross_group_uid(participant_gws, channel, group_id) -> str:
    """`unique_id` кросс-шлюзовой группы: `xgrp_<хвостA>_<хвостB>…_<channel>_<groupId>`.

    ⚠ ВЫЧИСЛЯЕТСЯ ОДИН РАЗ ПРИ СОЗДАНИИ И ХРАНИТСЯ (`CrossGroupStore`). Пересчитывать его
    от ЖИВОГО состава нельзя: убрали последнюю лампу второго шлюза — набор участников стал
    другим, id поехал, HA завёл новую сущность, история оборвалась, автоматизации на
    `entity_id` отвалились. Это ровно летучий ключ из закона 2 (CLAUDE.md).
    Серийники сортируем — набор шлюзов не должен зависеть от порядка выбора в карточке."""
    tails = sorted(gw_tail(g) for g in (participant_gws or []) if str(g or "").strip())
    return "xgrp_" + "_".join(tails + [str(channel), str(group_id)])


def split_write_plan(members, default_gw: str | None = None) -> list:
    """План записи: [(шлюз, его лампы)] в порядке участников.

    Отдельная функция, чтобы порядок записи был ОДИН и тот же у создания, правки состава и
    удаления — расхождение порядка между этими путями уже давало разъезжающиеся группы."""
    return list(members_by_gateway(members, default_gw).items())
