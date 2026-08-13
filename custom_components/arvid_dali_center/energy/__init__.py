"""Сателлит энергомониторинга для ARVID DALI Center.

Сервис ПОВЕРХ основной интеграции: считает расчётную энергию ламп по состоянию их
СУЩНОСТЕЙ HA (истина — сущность, не readDev/onlineStatus; см. docs/PLAN_ENERGY.md).
Развязан от управляющей логики: только ПОДПИСЫВАЕТСЯ на сигналы/состояния, в
light/coordinator/transport НЕ пишет → дестабилизировать их не может (идёт сателлитом).

Состав:
- store.py       — EnergyStore: параметры ламп (power_w/model) + накопитель energy_wh/on_time + тариф.
- integrator.py  — EnergyIntegrator: слушает SIGNAL_LAMP_STATE → копит энергию.
- websocket_api  — WS: данные отчёта, массовое задание параметров, тариф.
- rest.py        — REST-выгрузка (pull, токен HA). HA-сенсоры убраны в v0.46 (масштаб).
"""

from __future__ import annotations

import logging
from datetime import timedelta

from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.core import HomeAssistant
from homeassistant.helpers.event import async_track_time_interval

from .integrator import EnergyIntegrator
from .rest import async_register_rest
from .store import EnergyStore, _HASS_KEY, get_energy_store
from .websocket_api import async_register_energy, load_user_curves_blocking

_LOGGER = logging.getLogger(__name__)

_INTEG_KEY = "arvid_dali_center_energy_integrator"
# Интервал «подбивки» открытых отрезков энергии: фиксируем накопленное на диск, не дожидаясь
# выключения лампы. Раз в час — потеря при вырубании света ≤1ч, нагрузка ничтожна (один файл).
_PODBIVKA_INTERVAL = timedelta(hours=1)


async def async_setup_energy(hass: HomeAssistant) -> None:
    """Поднять сателлит один раз (зовётся из async_setup компонента). Идемпотентно."""
    if get_energy_store(hass) is not None:
        return
    store = EnergyStore(hass)
    await store.async_load()
    hass.data[_HASS_KEY] = store


    integ = EnergyIntegrator(hass, store)
    integ.start()
    hass.data[_INTEG_KEY] = integ

    # Подбивка открытых отрезков: периодически (раз в час) и на остановке HA. Иначе при
    # вырубании света теряется весь незакрытый отрезок горения лампы (систематическое
    # занижение энергоучёта «за всё время»). Сателлит живёт весь lifetime hass (idempotent
    # setup), поэтому таймер отменять не нужно — но снимаем на STOP для чистоты.
    cancel = async_track_time_interval(hass, integ.async_podbivka, _PODBIVKA_INTERVAL)
    hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, integ.async_podbivka)
    hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, lambda _e: cancel())

    # КРИВЫЕ МОЩНОСТИ ИЗ ФАЙЛА (2026-08-13): таблицу «яркость → ватты» снимает пусконаладчик
    # на объекте, и она не должна требовать выпуска версии. Файла нет — работаем на встроенных
    # `linear`/`lbs`, это норма. Битый файл не глушим: проблемы уходят в лог (и в ответ
    # `curves_reload`, если человек перечитывает из карточки).
    loaded, problems = await hass.async_add_executor_job(load_user_curves_blocking, hass)
    if loaded or problems:
        _LOGGER.info("кривые мощности: из файла загружено %s, замечаний %s",
                     loaded, len(problems))

    async_register_energy(hass)
    async_register_rest(hass)   # REST-выгрузка (pull, токен HA): /api/arvid_dali_center/energy
    _LOGGER.info("энергомониторинг: сателлит запущен (стор+интегратор+WS+REST)")
