"""Хранилище мониторинга здоровья устройств (сателлит).

Пороги (правятся в карточке) + активные ошибки (для непрерывности `since` через рестарт) +
лог «Восстановлено» (ограничен). Ключ записи — `gwSn:devSn:devType` (или `gwSn:gateway`).
"""

from __future__ import annotations

import logging

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

_LOGGER = logging.getLogger(__name__)

_STORE_KEY = "arvid_dali_center_health"
_HASS_KEY = "arvid_dali_center_health_store"
RETENTION_DAYS = 30    # хранилище лога «Восстановлено» — по времени
_HARD_CAP = 5000       # страховка от бесконечного роста (на случай частых флапов)
_SAVE_DELAY = 10.0

# Пороги по умолчанию (часы / минуты).
#
# ⚠ v1.1.2 — РАСЦЕПЛЕНЫ две роли, которые раньше играла одна цифра `interval_min`:
#   `grace_min`    — СКОЛЬКО ТЕРПИМ плохое состояние (unavailable/unknown), прежде чем назвать
#                    его ошибкой. Это СЕМАНТИКА аварии: подняли — авария всплывёт позже.
#   `interval_min` — КАК ЧАСТО подметаем (общий проход по всем устройствам). Это НАГРУЗКА.
# Раньше грейс вычислялся как `interval_min * 60` → «сделать проход реже» молча означало
# «терпеть аварию дольше». Теперь это РАЗНЫЕ пороги: грейс — терпение к транзиенту, интервал —
# период СТРАХОВОЧНОГО прохода.
# ⚠ v1.2.5: оценщик инкрементальный, и обход больше не является рабочим механизмом — аварию
# ловит событие сущности, а длительные пороги (1ч/7ч/грейс) — БУДИЛЬНИК на дедлайн (evaluator
# `_arm_deadline`). `interval_min` остался лишь как редкая СВЕРКА (страховка от пропущенного
# события/бага): 1 проход в 20 мин против прежних 2 проходов в МИНУТУ, которые гнал поллинг
# внешнего интерфейса.
DEFAULT_THRESHOLDS = {
    "motion_stuck_h": 1.0,   # состояние `motion` залипло дольше (v1.2.45: было «ДВИЖЕНИЕ»)
    "clear_h": 7.0,          # состояние `vacant` дольше (было «свободно»)
    "lux_stale_h": 7.0,      # освещённость не менялась дольше
    "grace_min": 5.0,        # терпим плохое состояние (грейс против транзиентов)
    "interval_min": 20.0,    # период СТРАХОВОЧНОЙ сверки (не рабочий механизм — см. выше)
}


class HealthStore:
    def __init__(self, hass: HomeAssistant) -> None:
        self._store = Store(hass, 1, _STORE_KEY)
        self._thresholds = dict(DEFAULT_THRESHOLDS)
        self._active: dict[str, dict] = {}
        self._recovered: list[dict] = []
        self._window_since = ""   # метка окна «Восстановлено» (показываем resolved ≥ неё)

    async def async_load(self) -> None:
        data = await self._store.async_load() or {}
        stored = data.get("thresholds") or {}
        self._thresholds = {**DEFAULT_THRESHOLDS, **stored}
        # МИГРАЦИЯ v1.1.2: в старом сторе `grace_min` нет, а грейс вычислялся из `interval_min`.
        # Значит СМЫСЛ, который пользователь мог осознанно настроить, сидел в `interval_min` —
        # переносим его в `grace_min` (семантика сохраняется), а период обхода берём из нового
        # дефолта (он про нагрузку, его раньше никто отдельно не выбирал).
        if stored and "grace_min" not in stored:
            # ⚠ фолбэк — СТАРЫЙ дефолт (5 мин), а НЕ новый `interval_min` (20): если в сторе нет
            # `interval_min`, грейс исторически был 5, и мигрировать его в 20 значило бы молча
            # утроить терпение к авариям.
            legacy = float(stored.get("interval_min") or 5.0)
            self._thresholds["grace_min"] = max(1.0, legacy)
            self._thresholds["interval_min"] = DEFAULT_THRESHOLDS["interval_min"]
            _LOGGER.info(
                "health: миграция порогов — грейс %.0f мин (из старого interval_min), "
                "период обхода %.0f мин (новый дефолт)",
                self._thresholds["grace_min"], self._thresholds["interval_min"])
            self._save()
        self._active = data.get("active") or {}
        self._recovered = data.get("recovered") or []
        self._window_since = data.get("window_since") or ""

    def _data(self) -> dict:
        return {"thresholds": self._thresholds, "active": self._active,
                "recovered": self._recovered, "window_since": self._window_since}

    def _save(self) -> None:
        self._store.async_delay_save(self._data, _SAVE_DELAY)

    # — пороги —
    @property
    def thresholds(self) -> dict:
        return dict(self._thresholds)

    def set_thresholds(self, values: dict) -> None:
        for k, v in (values or {}).items():
            if k in DEFAULT_THRESHOLDS and v is not None:
                self._thresholds[k] = max(0.1, float(v))
        self._save()

    # — активные ошибки —
    @property
    def active(self) -> dict[str, dict]:
        return self._active

    def set_active(self, active: dict[str, dict]) -> None:
        self._active = active
        self._save()

    # — лог «Восстановлено» —
    @property
    def recovered(self) -> list[dict]:
        return self._recovered

    def append_recovered(self, rec: dict) -> None:
        self._recovered.append(rec)   # сохранится вместе с set_active в том же цикле оценки

    def prune_recovered(self, cutoff_iso: str) -> None:
        """Хранилище 30 дней: выкинуть записи старше cutoff (UTC ISO) + страховочный кап."""
        self._recovered = [r for r in self._recovered if (r.get("resolved") or "") >= cutoff_iso]
        if len(self._recovered) > _HARD_CAP:
            self._recovered = self._recovered[-_HARD_CAP:]

    # — окно «Восстановлено» (показ), хранилище при сдвиге метки НЕ трогаем —
    @property
    def window_since(self) -> str:
        return self._window_since

    def set_window_since(self, iso: str) -> None:
        self._window_since = iso or ""
        self._save()


def get_health_store(hass: HomeAssistant) -> "HealthStore | None":
    return hass.data.get(_HASS_KEY)
