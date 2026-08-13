"""Персист сессии сверки энергии (свой стор — ядро не трогаем)."""

from __future__ import annotations

import logging

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

_LOGGER = logging.getLogger(__name__)

_STORE_KEY = "arvid_dali_center_verify"
_HASS_KEY = "arvid_dali_center_verify_store"
_SAVE_DELAY = 30.0          # срезы копятся медленно, писать чаще смысла нет

# Потолок хранимых срезов: при 60 с это ~сутки. Дальше режем СТАРЫЕ — сессия длинная, а
# смысл в накопленных дельтах (они хранятся отдельно) и в свежем хвосте для графика.
MAX_SAMPLES = 1440


class VerifyStore:
    """Одна активная сессия сверки + её срезы.

    Одна — сознательно: задача исследовательская, параллельные сверки только запутают
    («какой ratio относится к какой лампе»). Нужна другая лампа — стоп, старт заново."""

    def __init__(self, hass: HomeAssistant) -> None:
        self._store = Store(hass, 1, _STORE_KEY)
        self._data: dict = {}

    async def async_load(self) -> None:
        self._data = await self._store.async_load() or {}

    # ── сессия ────────────────────────────────────────────────────────────────
    @property
    def session(self) -> dict | None:
        return self._data.get("session")

    @property
    def samples(self) -> list[dict]:
        return self._data.get("samples") or []

    async def async_start(self, cfg: dict) -> None:
        """Начать сессию (старая, если была, ЗАМЕЩАЕТСЯ — но сперва уходит в архив)."""
        if self.session:
            await self.async_archive()
        self._data["session"] = cfg
        self._data["samples"] = []
        await self._store.async_save(self._data)

    async def async_stop(self) -> None:
        s = self._data.get("session")
        if s:
            s["stopped_at"] = s.get("stopped_at") or None
            s["running"] = False
        await self._store.async_save(self._data)

    async def async_resume(self) -> None:
        s = self._data.get("session")
        if s:
            s["running"] = True
        await self._store.async_save(self._data)

    async def async_clear(self) -> None:
        """Снять сессию целиком (архив не трогаем)."""
        self._data.pop("session", None)
        self._data.pop("samples", None)
        await self._store.async_save(self._data)

    async def async_rebase(self, base: dict) -> None:
        """Сбросить БАЗУ дельт на текущие показания (сессия и срезы остаются).
        Нужно, когда сменили `power_w`/кривую: прошлое пересчитано НЕ будет
        (docs/ENERGY_CALC_MODEL.md §5), поэтому сверять дальше надо от новой точки."""
        s = self._data.get("session")
        if not s:
            return
        s.update(base)
        s["rebased"] = True
        await self._store.async_save(self._data)

    async def async_archive(self) -> None:
        """Сложить итог завершённой сессии в архив (без срезов — только сводка)."""
        s = self._data.get("session")
        if not s:
            return
        arch = self._data.setdefault("archive", [])
        arch.append({k: s.get(k) for k in
                     ("name", "devsn", "lamp_entity", "started_at", "base_our_wh",
                      "base_relay_wh", "last_our_wh", "last_relay_wh", "power_w", "model")})
        del arch[:-20]                       # держим последние 20 сводок
        await self._store.async_save(self._data)

    @property
    def archive(self) -> list[dict]:
        return self._data.get("archive") or []

    # ── срезы ─────────────────────────────────────────────────────────────────
    def add_sample(self, sample: dict) -> None:
        """Добавить срез (в память; на диск — отложенно, срезов много)."""
        arr = self._data.setdefault("samples", [])
        arr.append(sample)
        if len(arr) > MAX_SAMPLES:
            del arr[:len(arr) - MAX_SAMPLES]
        s = self._data.get("session")
        if s:                                # хвост в сессии — чтобы пережить рестарт
            s["last_our_wh"] = sample.get("our_wh")
            s["last_relay_wh"] = sample.get("relay_wh")
            s["last_ts"] = sample.get("ts")
        self._store.async_delay_save(lambda: self._data, _SAVE_DELAY)


def get_verify_store(hass: HomeAssistant) -> VerifyStore | None:
    return hass.data.get(_HASS_KEY)
