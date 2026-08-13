"""REST-выгрузка энергомониторинга (сателлит).

Pull-эндпоинт для удалённого сервера: один запрос → сводная таблица по зданию с роллапами
(здание → этажи → группы → лампы). Авторизация — штатным токеном HA (`requires_auth`).
GET `/api/arvid_dali_center/energy` (JSON, дефолт) или `?format=csv`.

Иерархия:
- здание = `hass.config.location_name` (1 HA = 1 здание; много зданий = много HA, сводит центр);
- этаж/зона = штатные Area/Floor HA (лампа → device → area → floor);
- группа = DALI-группа (членство по адресу+каналу из `hub.groups`);
- лампа = `devSn` + накопители `EnergyStore`.

Только ЧИТАЕТ (стор + реестры + кеш хаба) — управляющих путей не касается.
"""

from __future__ import annotations

import csv
import io
import logging

from aiohttp import web
from homeassistant.components.http import HomeAssistantView
from homeassistant.core import HomeAssistant
from homeassistant.helpers import (
    area_registry as ar_reg,
    device_registry as dr_reg,
    floor_registry as fr_reg,
)

from ..const import DOMAIN
from ..transport.decode import devtype_name
from .store import get_energy_store

_LOGGER = logging.getLogger(__name__)

CSV_COLUMNS = ["building", "floor", "area", "dali_groups", "gw_sn", "devSn",
               "name", "model", "power_w", "energy_kwh", "on_time_h", "cost"]


def _resolve_device(dreg, areg, freg, devsn: str):
    """Имя/зона/этаж лампы из реестра устройств HA (energy-сенсоры на device лампы)."""
    dev = dreg.async_get_device(identifiers={(DOMAIN, devsn)})
    if not dev:
        return None, None, None
    # v1.2.7: имя устройства теперь `light_<полный devSn>` — техн. идентификатор, нечитаемый в
    # отчёте. Берём ТОЛЬКО имя ЧЕЛОВЕКА (name_by_user); дефолт (dev.name) не показываем — вызывающий
    # подставит fallback по адресу.
    name = dev.name_by_user
    area_name = floor_name = None
    if dev.area_id:
        area = areg.async_get_area(dev.area_id)
        if area:
            area_name = area.name
            if area.floor_id:
                fl = freg.async_get_floor(area.floor_id)
                if fl:
                    floor_name = fl.name
    return name, area_name, floor_name


def _rollup(lamps: list[dict], key: str, label: str) -> list[dict]:
    """Свернуть лампы по полю (этаж/зона). Лампа без значения → «—»."""
    agg: dict[str, dict] = {}
    for l in lamps:
        k = l.get(key) or "—"
        a = agg.setdefault(k, {label: k, "energy_kwh": 0.0, "on_time_h": 0.0, "lamps": 0})
        a["energy_kwh"] += l["energy_kwh"]
        a["on_time_h"] += l["on_time_h"]
        a["lamps"] += 1
    for a in agg.values():
        a["energy_kwh"] = round(a["energy_kwh"], 6)
        a["on_time_h"] = round(a["on_time_h"], 4)
    return list(agg.values())


def _rollup_groups(lamps: list[dict]) -> list[dict]:
    """Свернуть по DALI-группам. Лампа в N группах попадает в каждую (роллап перекрывается —
    это «сколько потребляет группа», а не разбиение; истинный итог — в summary)."""
    agg: dict[str, dict] = {}
    for l in lamps:
        for g in (l.get("dali_groups") or ["—"]):
            a = agg.setdefault(g, {"group": g, "energy_kwh": 0.0, "on_time_h": 0.0, "lamps": 0})
            a["energy_kwh"] += l["energy_kwh"]
            a["on_time_h"] += l["on_time_h"]
            a["lamps"] += 1
    for a in agg.values():
        a["energy_kwh"] = round(a["energy_kwh"], 6)
        a["on_time_h"] = round(a["on_time_h"], 4)
    return list(agg.values())


def build_export(hass: HomeAssistant) -> dict:
    """Собрать выгрузку: лампы по всем шлюзам этого HA (= здание) + роллапы."""
    store = get_energy_store(hass)
    tariff = store.tariff if store else None
    dreg = dr_reg.async_get(hass)
    areg = ar_reg.async_get(hass)
    freg = fr_reg.async_get(hass)
    lamps: list[dict] = []
    for hub in hass.data.get(DOMAIN, {}).values():
        gw_sn = getattr(hub, "gw_sn", None)
        if not gw_sn:
            continue
        groups = getattr(hub, "groups", []) or []
        # индекс членства (channel,address) → [имена групп]: строим ОДИН раз на шлюз,
        # вместо O(ламп×групп×членов) вложенного перебора на каждую лампу
        gmember: dict[tuple, list[str]] = {}
        for g in groups:
            gname = g.get("name") or f"группа {g.get('groupId')}"
            for m in (g.get("members") or []):
                gmember.setdefault((m.get("channel"), m.get("address")), []).append(gname)
        for dev in getattr(hub, "devices", {}).values():
            if not str(dev.get("devType", "")).startswith("01"):
                continue
            devsn = dev.get("devSn")
            if not devsn:
                continue
            rec = store.get(devsn) if store else {}
            energy_kwh = round((rec.get("energy_wh") or 0.0) / 1000.0, 6)
            on_time_h = round((rec.get("on_time_s") or 0.0) / 3600.0, 4)
            power_w = rec.get("power_w")
            cost = round(energy_kwh * tariff, 4) if tariff is not None else None
            name, area_name, floor_name = _resolve_device(dreg, areg, freg, devsn)
            ch, addr = dev.get("channel"), dev.get("address")
            dali_groups = gmember.get((ch, addr), [])
            # имя: человек (name_by_user) → иначе читаемый fallback по типу+адресу (не devSn)
            fallback = f"{devtype_name(str(dev.get('devType')))} {addr}"
            lamps.append({
                "gw_sn": gw_sn, "devSn": devsn,
                "name": name or fallback, "model": rec.get("model"),
                "area": area_name, "floor": floor_name, "dali_groups": dali_groups,
                "power_w": power_w, "energy_kwh": energy_kwh,
                "on_time_h": on_time_h, "cost": cost,
            })
    # ПОКРЫТИЕ (E3, v1.2.19): доля ламп с заданным `power_w`. Непокрытые дают 0 Вт·ч → их
    # реальное потребление в суммах ОТСУТСТВУЕТ. Внешнему серверу это нужно, чтобы понимать,
    # насколько полны числа объекта (сравнивать можно только сопоставимо покрытые объекты).
    covered = sum(1 for l in lamps if l["power_w"] is not None)
    summary = {
        "lamps": len(lamps),
        "covered_lamps": covered,
        "uncovered_lamps": len(lamps) - covered,
        "coverage_pct": round(100.0 * covered / len(lamps), 1) if lamps else None,
        "energy_kwh": round(sum(l["energy_kwh"] for l in lamps), 6),
        "on_time_h": round(sum(l["on_time_h"] for l in lamps), 4),
        "cost": (round(sum(l["cost"] or 0.0 for l in lamps), 4) if tariff is not None else None),
    }
    return {
        "building": hass.config.location_name,
        "tariff": tariff,
        "summary": summary,
        "by_floor": _rollup(lamps, "floor", "floor"),
        "by_group": _rollup_groups(lamps),
        "lamps": lamps,
    }


def export_csv(data: dict) -> str:
    """Плоская таблица по лампам (роллапы считает удалённая сторона по колонкам иерархии)."""
    out = io.StringIO()
    out.write("﻿")   # BOM — чтобы Excel открыл UTF-8 корректно
    # разделитель ; (русская/евро локаль Excel ждёт его, иначе всё в одной ячейке)
    w = csv.writer(out, delimiter=";")
    w.writerow(CSV_COLUMNS)
    b = data.get("building") or ""
    for l in data["lamps"]:
        w.writerow([
            b, l.get("floor") or "", l.get("area") or "",
            "|".join(l.get("dali_groups") or []),   # | внутри ячейки (не путать с разделителем ;)
            l.get("gw_sn") or "", l.get("devSn") or "", l.get("name") or "",
            l.get("model") or "",
            "" if l.get("power_w") is None else l["power_w"],
            l.get("energy_kwh", 0), l.get("on_time_h", 0),
            "" if l.get("cost") is None else l["cost"],
        ])
    return out.getvalue()


class EnergyExportView(HomeAssistantView):
    """GET /api/arvid_dali_center/energy[?format=csv]. Токен-авторизация HA."""

    url = "/api/arvid_dali_center/energy"
    name = "api:arvid_dali_center:energy"

    async def get(self, request: web.Request) -> web.Response:
        hass = request.app["hass"]
        data = build_export(hass)
        if request.query.get("format") == "csv":
            return web.Response(
                text=export_csv(data), content_type="text/csv",
                headers={"Content-Disposition": "attachment; filename=arvid_energy.csv"})
        return self.json(data)


def async_register_rest(hass: HomeAssistant) -> None:
    hass.http.register_view(EnergyExportView())
