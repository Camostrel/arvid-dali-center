"""Интегратор энергии (сателлит, event-driven, без таймеров).

Слушает SIGNAL_LAMP_STATE — лампа шлёт его на изменении состояния (наша команда, `devStatus`
от шлюза по кнопкам/автоматике, агрегат группы) И на смене ДОСТУПНОСТИ (v1.2.19, F6: связь
шлюза / onlineStatus / зомби). Сигнал несёт `available`: недоступная лампа → отрезок закрываем
и УБИРАЕМ из учёта (не копим Вт·ч на негоревшей). Это даёт правдивое «состояние сущности», на
котором и считаем энергию (см. docs/PLAN_ENERGY.md). В управляющие пути НЕ пишем — только сигнал.

Модель: на каждое изменение закрываем предыдущий «отрезок» (Δt с прошлого события) и
открываем новый. E += P(яркость) · Δt/3600; on_time += Δt (пока было on).
Мощность P — по КРИВОЙ драйвера (v1.1.3, `curves.power_at`): `power_w` (полная мощность ЛАМПЫ)
× форма кривой её драйвера (`model`). Линейная кривая — дефолт (как считалось до v1.1.3).
Почему не берём энергочисло шлюза — docs/ENERGY_CALC_MODEL.md §1 (шлюз не измеряет).

Рестарт HA: НЕ считаем простой (downtime неизвестен и неограничен). Первое событие по лампе
в сессии лишь «заякоривает» отрезок (prev is None → интеграции нет). Накопитель energy_wh
при этом восстановлен из стора (истина за всё время).
"""

from __future__ import annotations

import logging
import time

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect

from ..const import DOMAIN
from ..coordinator import SIGNAL_LAMP_STATE
from .curves import power_at
from .store import EnergyStore

_LOGGER = logging.getLogger(__name__)

# ── ДИАГНОСТИКА (ПО УМОЛЧАНИЮ ВЫКЛЮЧЕНА): трассировка расчёта энергии ОДНОЙ лампы ─────
# Зачем: сверить расчётный энергоучёт с внешним реле по конкретной лампе. Пишем НЕ отдельный
# тестовый расчёт, а ЛОГИРУЕМ БОЕВОЙ ПУТЬ — те же `_on_lamp_state`/`_close_segment`, что считают
# всем лампам. Видно: переходы яркости, длину отрезка, кривую, мощность, прирост Вт·ч и итог.
#
# ⚠ v1.2.0 (долг T1): трассировка была ВКЛЮЧЕНА В ПОСТАВЛЯЕМОМ КОДЕ — боевой лог сыпал EN-TRACE
# по чужой лампе у любого, кто поставит интеграцию. Теперь ВЫКЛЮЧЕНА. Механизм оставлен: сверка
# кривой `lbs` длинным прогоном (гейт G5) ещё не проведена, инструмент понадобится.
#
# ВКЛЮЧИТЬ: вписать серийник шлюза и DALI-адрес лампы. Фильтр:  ha core logs | grep EN-TRACE
# Порядок сверки — docs/ENERGY_CALC_MODEL.md §5.
TRACE_GW_SN = ""                # "" = трассировка выключена (боевой режим)
TRACE_ADDRESS = 1               # DALI-адрес лампы (действует только при заданном TRACE_GW_SN)


class EnergyIntegrator:
    """Один на hass: слушает все шлюзы (SIGNAL_LAMP_STATE несёт gw_sn)."""

    def __init__(self, hass: HomeAssistant, store: EnergyStore) -> None:
        self._hass = hass
        self._store = store
        # devSn → (last_monotonic, bri_frac) текущего отрезка
        self._seg: dict[str, tuple[float, float]] = {}
        self._unsub = None
        self._trace_devsn: str | None = None   # ВРЕМЕННО: devSn трассируемой лампы (см. выше)

    @callback
    def start(self) -> None:
        self._unsub = async_dispatcher_connect(
            self._hass, SIGNAL_LAMP_STATE, self._on_lamp_state)

    @callback
    def stop(self) -> None:
        if self._unsub:
            self._unsub()
            self._unsub = None

    def _close_segment(self, devsn: str, last_t: float, frac: float, now: float) -> None:
        """Закрыть отрезок: начислить энергию и наработку за (now − last_t).

        Мощность — по КРИВОЙ драйвера (v1.1.3, `curves.power_at`), а не линейно: `power_w` —
        полная мощность ЛАМПЫ, форму даёт кривая её драйвера (`model`). Линейная кривая —
        дефолт, т.е. лампы без заданной кривой считаются как раньше.
        Наработка (`on_time_s`) копится ТОЛЬКО пока лампа горит; у выключенной начисляется лишь
        дежурное потребление (`standby_w`, по замерам 0 → ничего).
        """
        dt = now - last_t
        if dt <= 0:
            return
        power_w, model = self._store.params(devsn)
        watts = power_at(model, power_w, frac)
        add_wh = watts * dt / 3600.0
        add_on_s = dt if frac > 0 else 0.0
        if add_wh or add_on_s:
            self._store.accumulate(devsn, add_wh, add_on_s)
        # ВРЕМЕННО (v1.1.5): трассировка боевого расчёта по одной лампе — см. шапку модуля.
        # Показываем ВСЮ начинку: длина отрезка, доля яркости, кривая, мощность, прирост, итог.
        if devsn == self._trace_devsn:
            rec = self._store.get(devsn)
            shape = (watts / power_w) if power_w else 0.0   # доля полной мощности по кривой
            _LOGGER.info(
                "EN-TRACE seg  devSn=%s | Δt=%.1fс frac=%.3f (%.0f%%) | кривая=%s power_w=%s "
                "→ shape=%.3f → P=%.2f Вт | +%.4f Вт·ч, +%.1fс наработки | ИТОГО: %.3f Вт·ч, "
                "%.2f ч",
                devsn, dt, frac, frac * 100, model or "linear (дефолт)",
                power_w if power_w is not None else "НЕ ЗАДАНА → энергию НЕ копим",
                shape, watts, add_wh, add_on_s,
                rec.get("energy_wh", 0.0), rec.get("on_time_s", 0.0) / 3600.0)

    @callback
    def flush_open(self) -> None:
        """Подбить ОТКРЫТЫЕ отрезки: насчитать энергию/наработку с момента последнего
        события лампы и ПЕРЕОТКРЫТЬ отрезок (last_t = сейчас), не дожидаясь следующего
        события. Без этого при вырубании света теряется весь незакрытый отрезок (лампа
        горела часами → нигде не записано). Стоимость — арифметика в RAM по числу
        ВКЛЮЧЁННЫХ ламп; запись на диск делает вызывающий (один файл на все лампы)."""
        now = time.monotonic()
        for devsn, (last_t, frac) in list(self._seg.items()):
            self._close_segment(devsn, last_t, frac, now)
            self._seg[devsn] = (now, frac)     # переоткрыть отрезок с нуля

    async def async_podbivka(self, _now=None) -> None:
        """Периодическая подбивка (раз в час) + НЕМЕДЛЕННАЯ запись на диск, чтобы при
        вырубании света потеря энергоучёта была ограничена интервалом подбивки, а не
        «временем с последнего переключения лампы». Зовётся таймером и на остановке HA."""
        self.flush_open()
        await self._store.async_flush()

    def _resolve_devsn(self, gw_sn: str, key: str) -> str | None:
        """Ключ сигнала (`devType:ch:addr`) → КЛЮЧ ИДЕНТИЧНОСТИ лампы через кеш её шлюза.

        v1.2.73: спрашиваем идентичность у хаба, а не берём `devSn` из записи. В штатном режиме
        результат тот же (серийник), в адресном — координата. Имя метода историческое: менять
        его вместе с ключом значило бы трогать весь сателлит ради косметики.

        ⚠ Учёт ведётся ПО ЭТОМУ ключу, поэтому в адресном режиме замена лампы на том же адресе
        продолжит чужой счётчик — это осознанная цена модели (Н6 плана: сигнал о смене
        справочного серийника + ручное обнуление, а не тихий сброс)."""
        for hub in self._hass.data.get(DOMAIN, {}).values():
            if getattr(hub, "gw_sn", None) == gw_sn:
                rec = getattr(hub, "devices", {}).get(key)
                if not rec:
                    return None
                ident = hub.name_key_for(rec) if hasattr(hub, "name_key_for") else rec.get("devSn")
                return ident or None
        return None

    @callback
    def _on_lamp_state(self, gw_sn: str, key: str, is_on, brightness,
                       available: bool = True) -> None:
        devsn = self._resolve_devsn(gw_sn, key)
        if not devsn:
            return
        # ВРЕМЕННО (v1.1.5): опознать трассируемую лампу по (шлюз, DALI-адрес) — см. шапку.
        # key = devType:channel:address, поэтому адрес — третье поле.
        if TRACE_GW_SN and gw_sn == TRACE_GW_SN and self._trace_devsn is None:
            parts = key.split(":")
            if len(parts) == 3 and parts[2] == str(TRACE_ADDRESS):
                self._trace_devsn = devsn
                _LOGGER.info("EN-TRACE старт: лампа gw=%s addr=%s → devSn=%s "
                             "(трассирую боевой расчёт энергии)", gw_sn, TRACE_ADDRESS, devsn)
        now = time.monotonic()
        prev = self._seg.get(devsn)
        # ВРЕМЕННО: монитор ПЕРЕХОДА яркости (что именно пришло от шлюза/команды и как мы это
        # трактуем). Отсюда видно, успевает ли сущность за реальной яркостью (автояркость и т.п.).
        if devsn == self._trace_devsn:
            new_frac = ((brightness / 255.0) if brightness else 1.0) if is_on else 0.0
            _LOGGER.info(
                "EN-TRACE evt  devSn=%s | on=%s bri=%s → frac=%.3f | было: %s",
                devsn, is_on, brightness, new_frac,
                (f"frac={prev[1]:.3f}" if prev else "отрезок не открыт (первое событие/после "
                 "рестарта → только якорим, простой НЕ досчитываем)"))
        # закрыть предыдущий отрезок (доначислить до сейчас — на всю известную длину горения)
        if prev is not None:
            last_t, last_frac = prev
            self._close_segment(devsn, last_t, last_frac, now)
        # F6 (v1.2.19): лампа НЕДОСТУПНА (нет связи со шлюзом / offline по onlineStatus / зомби
        # после скана) → сколько она реально горит НЕИЗВЕСТНО. Отрезок НЕ открываем и УБИРАЕМ из
        # учёта, чтобы `flush_open` (часовая подбивка) не копил Вт·ч на негоревшей лампе. Раньше
        # переход в `unavailable` шёл `async_write_ha_state()` МИМО `_emit_state` → интегратор не
        # знал о пропаже и держал отрезок открытым с последней `frac` (систематическое ЗАВЫШЕНИЕ
        # при вырубании света). Вернётся на связь → лампа пришлёт `available=True` + состояние →
        # откроется СВЕЖИЙ отрезок с текущей яркостью; «дырка» отсутствия не считается никак.
        if not available:
            self._seg.pop(devsn, None)
            return
        # открыть новый отрезок (on без яркости трактуем как полный — power-on/неизвестно)
        frac = ((brightness / 255.0) if brightness else 1.0) if is_on else 0.0
        self._seg[devsn] = (now, frac)
