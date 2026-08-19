"""Операции над конфигурацией датчика — ОДНА реализация для сервисов и для карточки (v1.2.25).

Раньше логика расписания жила в `services.py`; карточка ходит по WS и адресует устройства
`gw_sn:devType:channel:address`, а сервисы — через `target`/entity_id. Чтобы два пути не разъехались
(классическая болезнь этого проекта — «фикс, доехавший до одного вызывающего из двух»), сама запись
живёт здесь, а `services.py` и `websocket_api.py` только резолвят цель и зовут эти функции.
"""

from __future__ import annotations

import logging

_LOGGER = logging.getLogger(__name__)

# Функция датчика → dpid её конфигурации: 0202 (люкс) dpid 3 = 恒照 (там же luxRange);
# 0201 (движение) dpid 2 = присутствие.
DEVTYPE_LUX = "0202"
DEVTYPE_MOTION = "0201"
FUNC_DPID = {"autobrightness": (DEVTYPE_LUX, 3), "motion": (DEVTYPE_MOTION, 2)}

# Условие «время» в runCondition (мануал стр. 45: devType = освещённость/ВРЕМЯ, channel/address
# можно не учитывать — в захвате DALI Center стоят 0/1, повторяем их 1:1).
TIME_DEVTYPE = "0701"
TIME_DPID = 2

# ── ОДНО ФИЗУСТРОЙСТВО = ДВЕ ЗАПИСИ КЕША (v1.2.56) ───────────────────────────
# Движение (0201) и освещённость (0202) — РАЗНЫЕ записи шлюза с ОБЩИМ devSn и общим адресом,
# но одна железка на стене. Человек это и видит: снял датчик — исчезли обе роли. «Забыть»
# же адресовалось одной записи, и приходилось снимать движение и освещённость по отдельности
# (замечание с объекта 2026-08-11). Ниже — ЕДИНСТВЕННОЕ место, где это родство описано.
SENSOR_UNIT_TYPES = (DEVTYPE_MOTION, DEVTYPE_LUX)


def unit_devtypes(devtype: str) -> tuple[str, ...]:
    """Типы записей, составляющие ОДНО физическое устройство. Для датчика — пара, иначе сам тип."""
    dt = str(devtype)
    return SENSOR_UNIT_TYPES if dt in SENSOR_UNIT_TYPES else (dt,)


def unit_keys(devices: dict, key: str) -> list[str]:
    """Ключи кеша, относящиеся к тому же ФИЗУСТРОЙСТВУ, что и `key` (сам ключ — первым).

    Родство считаем СТРОГО: тот же `devSn` + тот же (channel, address) + тип из пары + тот же
    признак `orphan`. Каждое условие тут не для красоты:
    - **devSn** — идентичность (закон 2), адрес волатилен;
    - **channel/address** — 0201 и 0202 одного прибора сидят на ОДНОМ адресе `dali2`;
    - **orphan** — у осиротевшего запись-двойник ЖИВА под тем же серийником. Без этого условия
      «Забыть» осиротевшего снесло бы живой датчик — ровно та беда, из-за которой на объекте
      2026-08-11 пропали работавшие устройства.
    Невалидный devSn → родство не выводим (связать нечем), возвращаем один ключ.
    """
    dev = (devices or {}).get(key)
    if not dev:
        return []
    sn = str(dev.get("devSn") or "")
    types = unit_devtypes(dev.get("devType"))
    if len(types) == 1 or not sn:
        return [key]
    same = (sn, dev.get("channel"), dev.get("address"), bool(dev.get("orphan")))
    out = [key]
    for k, e in devices.items():
        if k == key or str(e.get("devType")) not in types:
            continue
        if (str(e.get("devSn") or ""), e.get("channel"), e.get("address"),
                bool(e.get("orphan"))) == same:
            out.append(k)
    return out


def time_condition(windows: list[str]) -> list[dict]:
    """`runCondition` для окон. Несколько окон — МАССИВОМ в ОДНОМ условии (захват 2026-07-29)."""
    if not windows:
        return []
    return [{"devType": TIME_DEVTYPE, "channel": 0, "address": 1,
             "dpid": TIME_DPID, "value": list(windows)}]


def read_entry(read_res: dict | None, dpid: int) -> dict | None:
    """Запись конфигурации нужной функции из ответа `readSensor`."""
    for d in (read_res or {}).get("data", []) or []:
        if d.get("dpid") == dpid:
            return d
    return None


async def async_set_sensor_enabled(hub, dev: dict, enabled: bool) -> dict:
    """Мягкое вкл/выкл функции датчика (`setSensorOnOff`, мануал стр. 50).

    ⚠ НЕ ПУТАТЬ с `clear_lux_keep` (`delSensor`): тот СНОСИТ конфигурацию, а этот только
    приостанавливает — привязка на контроллере цела, возврат мгновенный и шину не грузит.
    Выбор человека персистится (переживает рестарт, v1.2.23)."""
    from .coordinator import dev_state_key
    dt, ch, addr = str(dev["devType"]), dev["channel"], dev["address"]
    res = await hub.async_request("setSensorOnOff", "setSensorOnOffRes", value=enabled,
                                  devType=dt, channel=ch, address=addr)
    ok = bool(res and res.get("ack"))
    key = dev_state_key(dt, ch, addr)
    hub.set_sensor_active(hub.sensor_pref_key(dev, key), enabled, persist=True)
    ent = hub.live_entity(f"active_{dt}", key)   # тумблер в UI и сервис — один источник правды
    if ent is not None:
        ent._attr_is_on = enabled                # noqa: SLF001 — та же интеграция
        ent.async_write_ha_state()
    _LOGGER.info("sensor %s ch%s addr%s → %s", dt, ch, addr,
                 "включён" if enabled else "ВЫКЛЮЧЕН (привязка сохранена)")
    return {"ok": ok, "res": res}


async def async_set_schedule(hub, dev: dict, dpid: int, windows: list[str]) -> dict:
    """Записать окна работы, СОХРАНИВ полезную нагрузку функции.

    ⚠ КЛЮЧЕВОЕ (захват DALI Center 2026-07-29): `addSensorObj` перезаписывает блок `data`
    ЦЕЛИКОМ. Поэтому читаем текущее (`readSensor`), забираем `outputObj` (сами лампы!) и
    `luxRange` и отправляем обратно вместе с новым расписанием — иначе назначение времени
    снесло бы автояркость. Порядок как у DALI Center: read → delSensorCondition → addSensorObj →
    read-сверка."""
    dt, ch, addr = str(dev["devType"]), dev["channel"], dev["address"]
    rr = await hub.async_request("readSensor", "readSensorRes", devType=dt, channel=ch,
                                 address=addr, timeout=8.0)
    if rr is None:
        return {"ok": False, "error": "readSensor не ответил (шина занята?)"}
    entry = read_entry(rr, dpid) or {}
    lux_range = entry.get("luxRange") or []
    out_obj = entry.get("outputObj") or []
    cur_cond = entry.get("runCondition") or []
    if not out_obj:
        return {"ok": False,
                "error": f"нет привязки (dpid {dpid}) — расписание вешать не на что"}

    if cur_cond:      # снять прежние условия (иначе шлюз может смёржить старое с новым)
        await hub.async_request(
            "delSensorCondition", "delSensorConditionRes", devType=dt, channel=ch, address=addr,
            dpid=dpid, runCondition=cur_cond, timeout=8.0)

    run_cond = time_condition(windows)
    data = {"dpid": dpid, "runCondition": run_cond, "outputObj": out_obj}
    if lux_range:
        data["luxRange"] = lux_range
    ares = await hub.async_request("addSensorObj", "addSensorObjRes", devType=dt, channel=ch,
                                   address=addr, linkSensor=[], mode={}, data=data, timeout=10.0)
    ok = bool(ares and ares.get("ack"))
    _LOGGER.info("расписание %s ch%s addr%s dpid%s окна=%s цели=%d → %s",
                 dt, ch, addr, dpid, windows or "(снято)", len(out_obj), ares)
    # «План Б»: копим ЗАПИСАННОЕ нами — если массовый readSensor окажется медленным на объекте,
    # источником станет стор, и он к тому моменту не будет пустым (docs/SERVICES.md).
    from .store import get_sensor_obj_store
    sos = get_sensor_obj_store(hub.hass)
    if sos and ok:
        await sos.async_set(hub.gw_sn, hub.name_key_for(dev) or "", dt, dpid,
                            {"luxRange": lux_range, "outputObj": out_obj, "runCondition": run_cond})
    verify = None
    if ok:
        rr2 = await hub.async_request("readSensor", "readSensorRes", devType=dt, channel=ch,
                                      address=addr, timeout=8.0)
        e2 = read_entry(rr2, dpid) or {}
        got = [c.get("value") for c in (e2.get("runCondition") or [])
               if str(c.get("devType")) == TIME_DEVTYPE]
        verify = {"windows": got[0] if got else [], "targets": len(e2.get("outputObj") or [])}
        if verify["targets"] == 0:
            _LOGGER.warning("расписание %s addr%s: после записи ЦЕЛЕЙ НЕТ — привязка потеряна!",
                            dt, addr)
    return {"ok": ok, "verify": verify}
