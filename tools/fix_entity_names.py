#!/usr/bin/env python3
"""⚠⚠ УСТАРЕЛ с v1.2.7 — НЕ ЗАПУСКАТЬ. Зеркалит СТАРУЮ схему имён (`<тип>_<адрес>_<gw4>_<sn5>` +
имя устройства по адресу), которой больше нет. В модели v1.2.7 имя УСТРОЙСТВА производно от devSn
(стабильно, догонять нечего), подпись безымянной сущности не задаётся (следует за entity_id), а
шлюз из entity_id убран — то есть весь смысл этого скрипта (догнать разъехавшиеся имена, развязать
наложения парковкой) отпал: наложений больше нет. Оставлен как исторический референс. Для перехода
на новую схему миграция не нужна — сброс адресов + перевязка (решение пользователя).

Привести имена БЕЗЫМЯННЫХ устройств в соответствие с их DALI-адресом (разовая уборка).

ЗАЧЕМ. Дефолтное имя ПРОИЗВОДНО от адреса (`motion_<addr>_<gw4>`). После перераздачи адресов
имена разъезжаются с физикой, и опознать устройство становится нельзя. На железе наблюдалось:
устройство `sensor_11_0914` с сущностями `motion_0_0914`; имена УСТРОЙСТВ перетасованы у 15 из 17
датчиков, вплоть до ДУБЛЕЙ; переключатели активации образовали ЦИКЛ (у адреса 3 стоит имя 4,
у 4 — имя 7, у 7 — имя 3).

ЧТО ДЕЛАЕТ (только БЕЗЫМЯННЫЕ — у кого НЕТ пользовательского имени):
  entity_id + подпись сущностей  →  <тип>_<текущий адрес>_<gw4>
  имя устройства HA             →  sensor_<адрес>_<gw4> (у 02xx) / <тип>_<адрес>_<gw4>

ЧЕГО НЕ ДЕЛАЕТ:
  • НЕ трогает ИМЕНОВАННЫЕ устройства (продакшен `ms_/il_/l_/kp_`): их имя живёт по `devSn`,
    от адреса не зависит, и именно на них вешаются автоматизации;
  • НЕ трогает имена, заданные человеком вручную (не по нашему шаблону);
  • НЕ удаляет сущности — только переименовывает. История в recorder переживает (он мигрирует
    `entity_id`).

СХЕМА ИМЁН — v1.2.0: `<тип>_<адрес>_<gw4>_<sn5>` (хвост = 5 знаков devSn). Он и разводит
имена: старое имя соседа и желаемое имя переезжающего лежат в РАЗНЫХ коридорах, поэтому
перестановки/циклов больше не возникает (обоснование — naming.py). Двухфазная парковка ниже
оставлена как страховка: на новой схеме она просто не срабатывает.

ЗАПУСК (в терминале HA):
    export HA_TOKEN='<long-lived token>'
    python3 fix_entity_names.py                 # СУХОЙ ПРОГОН (по умолчанию) — только показать
    python3 fix_entity_names.py --apply         # применить (СУЩНОСТИ)
    python3 fix_entity_names.py --apply --devices   # + имена УСТРОЙСТВ (см. -h: name_by_user!)
    python3 fix_entity_names.py --gw 762417130914   # только один шлюз

Зависимости: только stdlib + websockets (есть в окружении HA). Токен — ТОЛЬКО из переменной
окружения, в коде не хранится.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys

try:
    import websockets
except ImportError:
    sys.exit("нужен модуль websockets:  pip install websockets")

# ── Именование: ЗЕРКАЛО naming.py интеграции (держать синхронно!) ──────────────
LIGHT_TYPES = {"0101", "0102", "0103", "0104", "0105", "0106"}
PANEL_KEYS = {"0302": 2, "0304": 4, "0306": 6, "0308": 8}
# Префиксы, которые генерируем МЫ. Имя вне этого списка — задано человеком, НЕ ТРОГАЕМ.
OUR_PREFIXES = ("motion_", "illuminance_", "ms_", "il_", "light_", "keypanel_", "rotary_",
                "dev_", "sensor_")


def type_word(devtype: str) -> str:
    d = str(devtype)
    if d in LIGHT_TYPES:
        return "light"
    if d == "0201":
        return "motion"
    if d == "0202":
        return "illuminance"
    if d == "0300":
        return "rotary"
    if d in PANEL_KEYS:
        return f"keypanel_{PANEL_KEYS[d]}"
    return f"dev_{d}"


def gw4(gw_sn: str) -> str:
    return str(gw_sn)[-4:].lower()


def sn5(devsn: str) -> str:
    """Хвост devSn (5 знаков) — как в naming.sn_suffix (v1.2.0)."""
    s = str(devsn or "").strip().lower()
    return s[-5:] if s else ""


def _join(base: str, gw_sn: str, devsn: str) -> str:
    parts = [base]
    if gw_sn:
        parts.append(gw4(gw_sn))
    if sn5(devsn):
        parts.append(sn5(devsn))
    return "_".join(parts)


def entity_name(devtype: str, address, gw_sn: str, devsn: str = "") -> str:
    return _join(f"{type_word(devtype)}_{address}", gw_sn, devsn)


def device_name(devtype: str, address, gw_sn: str, devsn: str = "") -> str:
    if str(devtype).startswith("02"):
        return _join(f"sensor_{address}", gw_sn, devsn)
    return entity_name(devtype, address, gw_sn, devsn)


def is_ours(entity_id: str) -> bool:
    """Имя порождено нашим шаблоном (а не человеком)?"""
    return str(entity_id or "").split(".", 1)[-1].startswith(OUR_PREFIXES)


# ── Транспорт: HA WebSocket ───────────────────────────────────────────────────
class HA:
    def __init__(self, url: str, token: str):
        self._url, self._token, self._id, self._ws = url, token, 0, None

    async def __aenter__(self):
        self._ws = await websockets.connect(self._url, max_size=32 * 1024 * 1024)
        await self._ws.recv()                                   # auth_required
        await self._ws.send(json.dumps({"type": "auth", "access_token": self._token}))
        if json.loads(await self._ws.recv()).get("type") != "auth_ok":
            sys.exit("авторизация не прошла — проверьте HA_TOKEN")
        return self

    async def __aexit__(self, *_):
        await self._ws.close()

    async def cmd(self, type_: str, **kw):
        self._id += 1
        await self._ws.send(json.dumps({"id": self._id, "type": type_, **kw}))
        while True:
            msg = json.loads(await self._ws.recv())
            if msg.get("id") == self._id and msg.get("type") == "result":
                if not msg.get("success"):
                    raise RuntimeError(f"{type_}: {msg.get('error')}")
                return msg.get("result")


# ── Сбор плана ────────────────────────────────────────────────────────────────
def roles_of(devtype: str) -> list:
    """[(role, domain, суффикс unique_id)] — ВСЕ сущности устройства.

    ⚠ Резолвим сущности САМИ, по `unique_id` из реестра, а НЕ по полю `entities` из WS `devices`:
    оно НЕ отдаёт switch'и активации (`active_0201`/`active_0202`), и первая версия скрипта их
    молча пропустила — а именно у них имена разъехались сильнее всего (там был ЦИКЛ).
    """
    t = str(devtype)
    if t in LIGHT_TYPES:
        return [("light", "light", "")]
    if t == "0201":
        return [("motion", "sensor", "_motion"), ("active_0201", "switch", "_active_0201")]
    if t == "0202":
        return [("lux", "sensor", "_lux"), ("active_0202", "switch", "_active_0202")]
    if t.startswith("03"):
        return [("event", "event", "_event")]
    return []


async def build_plan(ha: HA, only_gw: str | None):
    """→ (переименования сущностей, переименования устройств). Только БЕЗЫМЯННЫЕ."""
    entries = await ha.cmd("config/entity_registry/list")
    ent_reg = {e["entity_id"]: e for e in entries}
    by_uid = {e["unique_id"]: e for e in entries
              if e.get("platform") == "arvid_dali_center"}
    gateways = await ha.cmd("arvid_dali_center/gateways")
    ent_plan, dev_plan = [], []

    for gw in gateways.get("gateways", gateways if isinstance(gateways, list) else []):
        gw_sn = gw.get("gwSn") or gw.get("gw_sn")
        if not gw_sn or (only_gw and gw_sn != only_gw):
            continue
        res = await ha.cmd("arvid_dali_center/devices", gw_sn=gw_sn)
        for d in res.get("devices", []):
            if d.get("named") or d.get("zombie"):
                continue                                    # именованные и зомби — не трогаем
            t, addr, sn = str(d.get("devType")), d.get("address"), d.get("devSn")
            if not sn:
                continue
            base = entity_name(t, addr, gw_sn, sn)
            device_id = None
            for role, domain, uid_sfx in roles_of(t):
                rec = by_uid.get(f"{sn}{uid_sfx}")     # unique_id = devSn + суффикс роли
                if not rec:
                    continue
                device_id = device_id or rec.get("device_id")
                cur = rec["entity_id"]
                # switch активации несёт хвост `_active` (дефолт) — у него своё желаемое имя
                name = f"{base}_active" if role.startswith("active_") else base
                new_eid = f"{domain}.{name}"
                if cur == new_eid:
                    continue
                if not is_ours(cur):
                    print(f"  ⏭  {cur}: имя задано вручную — НЕ трогаю")
                    continue
                ent_plan.append({"old": cur, "new": new_eid, "name": name,
                                 "addr": addr, "devSn": sn})
            # имя УСТРОЙСТВА HA (у датчиков своя схема: одно устройство на пару движение+люкс)
            if device_id:
                dev_plan.append({"device_id": device_id,
                                 "name": device_name(t, addr, gw_sn, sn),
                                 "addr": addr, "devSn": sn})
    # дедуп устройств (движение+люкс дают одно и то же)
    dev_plan = list({p["device_id"]: p for p in dev_plan}.values())
    return ent_plan, dev_plan


# ── Применение: ДВЕ ФАЗЫ ──────────────────────────────────────────────────────
async def apply_entities(ha: HA, plan: list) -> int:
    """Парковка конфликтующих → расстановка. Без парковки циклы не развязываются."""
    olds = {p["old"] for p in plan}
    parked, done = 0, 0
    # ФАЗА 1: если желаемый id занят ДРУГИМ участником плана — паркуем на временное имя
    for p in plan:
        if p["new"] in olds and p["new"] != p["old"]:
            tmp = f"{p['old'].split('.', 1)[0]}.arvid_tmp_{re.sub(r'[^a-z0-9_]', '_', p['devSn'].lower())}"
            try:
                await ha.cmd("config/entity_registry/update",
                             entity_id=p["old"], new_entity_id=tmp)
                print(f"  ФАЗА1 парковка: {p['old']} → {tmp}")
                p["old"], parked = tmp, parked + 1
            except Exception as err:                        # noqa: BLE001
                print(f"  !! парковка {p['old']}: {err}")
    # ФАЗА 2: расстановка на желаемые (нужные id уже свободны)
    for p in plan:
        try:
            await ha.cmd("config/entity_registry/update", entity_id=p["old"],
                         new_entity_id=p["new"], name=p["name"])
            print(f"  ФАЗА2 addr{p['addr']:>3}: {p['old']} → {p['new']}")
            done += 1
        except Exception as err:                            # noqa: BLE001
            print(f"  !! {p['old']} → {p['new']}: {err}")
    if parked:
        print(f"  (через временную парковку прошло {parked} — это были циклы перестановки)")
    return done


async def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true", help="применить (по умолчанию — сухой прогон)")
    ap.add_argument("--gw", help="только этот серийник шлюза")
    ap.add_argument("--devices", action="store_true",
                    help="ТАКЖЕ править имена УСТРОЙСТВ HA. ⚠ Побочный эффект: WS умеет задать "
                         "только `name_by_user` («имя задано человеком»), а Fix V такие "
                         "устройства не трогает НИКОГДА → имя устройства перестанет следовать "
                         "за адресом при будущих перераздачах. Сущностей это НЕ касается.")
    ap.add_argument("--url", default=os.environ.get("HA_URL", "ws://localhost:8123/api/websocket"))
    args = ap.parse_args()
    token = os.environ.get("HA_TOKEN")
    if not token:
        sys.exit("нет HA_TOKEN — задайте: export HA_TOKEN='<long-lived token>'")

    async with HA(args.url, token) as ha:
        ent_plan, dev_plan = await build_plan(ha, args.gw)
        if not args.devices:
            dev_plan = []                        # имена устройств — только по явному --devices

        print(f"\n=== СУЩНОСТИ: переименовать {len(ent_plan)}")
        for p in sorted(ent_plan, key=lambda x: (x["addr"], x["old"])):
            print(f"  addr{p['addr']:>3}  {p['old']:<38} → {p['new']}")
        if args.devices:
            print(f"\n=== УСТРОЙСТВА HA: переименовать {len(dev_plan)}  "
                  "(⚠ ставит name_by_user — Fix V их больше не тронет)")
            for p in sorted(dev_plan, key=lambda x: x["addr"]):
                print(f"  addr{p['addr']:>3}  → {p['name']}   (devSn {p['devSn']})")
        else:
            print("\n=== УСТРОЙСТВА HA: пропущены (нужен --devices; см. -h про побочный эффект)")

        if not args.apply:
            print("\nСУХОЙ ПРОГОН. Ничего не изменено. Применить: --apply")
            return
        if not ent_plan and not dev_plan:
            print("\nнечего менять")
            return

        print("\n=== ПРИМЕНЯЮ")
        n = await apply_entities(ha, ent_plan)
        m = 0
        for p in dev_plan:
            try:                                 # у устройства правим ПОЛЬЗОВАТЕЛЬСКОЕ имя
                await ha.cmd("config/device_registry/update",
                             device_id=p["device_id"], name_by_user=p["name"])
                m += 1
            except Exception as err:             # noqa: BLE001
                print(f"  !! устройство {p['devSn']}: {err}")
        print(f"\nГОТОВО: сущностей {n}, устройств {m}. История в recorder сохранена "
              "(он мигрирует entity_id).")


if __name__ == "__main__":
    asyncio.run(main())
