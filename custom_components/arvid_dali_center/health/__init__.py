"""Сателлит мониторинга здоровья устройств для ARVID DALI Center.

Лог ошибок устройств ПОВЕРХ интеграции: считает ошибки из состояния сущностей HA и связи
шлюзов (лампа неизвестно/оффлайн; датчик неизвестно/залипший `motion`/долгий `vacant`/
неизменная освещённость; панель неизвестно; шлюз оффлайн). Разделы «Ошибки»/«Восстановлено».
Развязан от управления: только подписки + чтение состояний. См. docs/HEALTH.md.

⚠ v1.2.5 — оценщик ИНКРЕМЕНТАЛЬНЫЙ: работа пропорциональна числу РЕАЛЬНЫХ изменений, а не числу
устройств × частоту опроса. Событие о конкретной сущности → пересчёт ТОЛЬКО её; длительности
(«залипло на N часов») — будильником на дедлайн, а не обходом; `health_data` отдаёт снимок и
НИЧЕГО не считает (долг HD1 закрыт). Раньше каждый запрос внешнего потребителя гнал полный
обход объекта.

Состав: store.py (HealthStore: пороги+active+recovered), evaluator.py (HealthEvaluator),
websocket_api.py (health_data/health_set_thresholds).
"""

from __future__ import annotations

import logging

from homeassistant.core import HomeAssistant

from .evaluator import HealthEvaluator
from .store import HealthStore, _HASS_KEY, get_health_store
from .websocket_api import _EVAL_KEY, async_register_health

_LOGGER = logging.getLogger(__name__)


async def async_setup_health(hass: HomeAssistant) -> None:
    """Поднять сателлит один раз (из async_setup компонента). Идемпотентно."""
    if get_health_store(hass) is not None:
        return
    store = HealthStore(hass)
    await store.async_load()
    hass.data[_HASS_KEY] = store

    evaluator = HealthEvaluator(hass, store)
    evaluator.start()
    hass.data[_EVAL_KEY] = evaluator

    async_register_health(hass)
    _LOGGER.info("мониторинг здоровья: сателлит запущен (стор+оценщик+WS)")
