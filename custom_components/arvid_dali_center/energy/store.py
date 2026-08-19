"""Хранилище энергомониторинга (сателлит).

Истина «за всё время работы объекта» — ЗДЕСЬ, в нашем сторе, а НЕ в истории HA
(recorder чистит states за ~10 дней; см. docs/PLAN_ENERGY.md §A3). Наружу эти числа
отдаются картой/CSV/REST (HA-сенсоры убраны в v0.46, см. docs/ENERGY.md).

Структура персиста:
    {
      "devices": { devSn: {power_w: float, model: str, energy_wh: float, on_time_s: float} },
      "tariff":  float | None        # ₽/кВт·ч; None — стоимость не считаем (колонка пустая)
    }
Ключ — `devSn` (как идентичность/параметры): следует за устройством через ре-нумерацию.
"""

from __future__ import annotations

import logging
import time

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from ..store import PurgeableStore

_LOGGER = logging.getLogger(__name__)

_STORE_KEY = "arvid_dali_center_energy"
_HASS_KEY = "arvid_dali_center_energy_store"
_SAVE_DELAY = 30.0   # c — коалесинг частых обновлений накопителя (фоновый отложенный save)


class EnergyStore(PurgeableStore):
    """Параметры ламп + накопитель энергии/наработки + тариф. Накопитель НИКОГДА не
    сбрасываем (растёт пожизненно)."""

    purge_name = "энергоучёт"

    async def purge_identity(self, identity: str) -> int:
        """Ключ — devSn лампы. Зовётся из общей чистки (S5), а не отдельной строкой в
        каждой операции: сателлит легко было забыть, как забыли SensorObjStore."""
        return 1 if self.remove(identity) else 0

    async def purge_gateway(self, gw_sn: str) -> int:
        """Шлюза в ключах нет — энергия висит на устройствах, снимается их чисткой."""
        return 0

    def __init__(self, hass: HomeAssistant) -> None:
        self._store = Store(hass, 1, _STORE_KEY)
        self._devices: dict[str, dict] = {}
        self._tariff: float | None = None
        self._save_req_at = 0.0   # монотонная метка последнего запроса записи (троттл, см. _save)

    async def async_load(self) -> None:
        data = await self._store.async_load() or {}
        self._devices = data.get("devices", {}) or {}
        self._tariff = data.get("tariff")
        # v1.2.6: `real_wh` (накопитель энергии ОТ ШЛЮЗА) БОЛЬШЕ НЕ ВЕДЁТСЯ — шлюз энергию не
        # измеряет (ретранслирует энергобанк драйвера либо выдумывает; снаружи неразличимо).
        # Старое значение вычищаем ОДИН раз, чтобы недостоверное число не всплыло в отчёте.
        if any("real_wh" in rec for rec in self._devices.values()):
            for rec in self._devices.values():
                rec.pop("real_wh", None)
            self._save()
            _LOGGER.info("энергостор: накопитель real_wh (энергия ОТ шлюза) удалён — "
                         "шлюз не измеряет энергию, живёт расчётный путь")

    def _data(self) -> dict:
        return {"devices": self._devices, "tariff": self._tariff}

    def _save(self) -> None:
        """Отложенный save (флашится на остановке HA и часовой подбивкой).

        ⚠ v1.1.7 — ТРОТТЛ. `async_delay_save` в HA — это DEBOUNCE с ПЕРЕЗАВОДОМ таймера: каждый
        вызов отменяет висящий listener и ставит новый. `accumulate()` зовётся на КАЖДОЕ событие
        лампы (интегратор). На
        объекте события идут чаще, чем раз в 30 с, ВСЕГДА → запись перезаводилась вечно и не
        выполнялась НИ РАЗУ, попутно создавая/отменяя таймер сотни раз в секунду.
        Теперь перезаводим не чаще, чем раз в `_SAVE_DELAY`: запись реально происходит.
        (Данные и так не терялись — их писала часовая подбивка и STOP, — но защита была мнимой.)
        """
        now = time.monotonic()
        if now - self._save_req_at < _SAVE_DELAY:
            return                                  # запись уже запланирована — не перезаводим
        self._save_req_at = now
        self._store.async_delay_save(self._data, _SAVE_DELAY)

    async def async_flush(self) -> None:
        """Немедленно записать накопитель на диск (отменяет отложенную запись). Зовётся
        периодической подбивкой энергии и на остановке HA — чтобы при вырубании света
        потеря была ограничена интервалом подбивки, а не окном delay_save. Один файл на
        все лампы → дёшево даже на масштабе. Защищён, чтобы сбой записи не ронял HA-stop."""
        try:
            await self._store.async_save(self._data())
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("энергостор: не удалось записать накопитель: %s", err)

    # ── параметры ламп ───────────────────────────────────────────────────────
    def get(self, devsn: str) -> dict:
        return dict(self._devices.get(devsn, {}))

    def params(self, devsn: str) -> tuple[float | None, str | None]:
        """(power_w, model) одним чтением — для интегратора (v1.1.3: мощность считается по
        КРИВОЙ драйвера, а не линейно; см. energy/curves.py)."""
        rec = self._devices.get(devsn, {})
        return rec.get("power_w"), rec.get("model")

    def set_params(self, devsn: str, power_w=None, model=None) -> None:
        rec = self._devices.setdefault(devsn, {})
        if power_w is not None:
            rec["power_w"] = float(power_w)
        if model is not None:
            rec["model"] = str(model)
        self._save()

    # ── тариф ────────────────────────────────────────────────────────────────
    @property
    def tariff(self) -> float | None:
        return self._tariff

    def set_tariff(self, value) -> None:
        self._tariff = float(value) if value not in (None, "") else None
        self._save()

    # ── накопитель (зовёт интегратор) ────────────────────────────────────────
    def accumulate(self, devsn: str, add_wh: float, add_on_s: float) -> None:
        rec = self._devices.setdefault(devsn, {})
        rec["energy_wh"] = rec.get("energy_wh", 0.0) + add_wh
        rec["on_time_s"] = rec.get("on_time_s", 0.0) + add_on_s
        self._save()

    def remove(self, devsn: str) -> bool:
        """Снести энергоданные лампы (v1.2.9: кнопка «Стереть данные шлюза»). Возврат — было ли."""
        if self._devices.pop(devsn, None) is not None:
            self._save()
            return True
        return False


def get_energy_store(hass: HomeAssistant) -> "EnergyStore | None":
    return hass.data.get(_HASS_KEY)
