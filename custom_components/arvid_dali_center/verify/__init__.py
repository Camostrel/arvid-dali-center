"""Сателлит СВЕРКИ энергоучёта (временный, v1.2.32).

ЗАЧЕМ. Наш учёт РАСЧЁТНЫЙ (`P = power_w × кривая(яркость)` → `EnergyStore`,
docs/ENERGY_CALC_MODEL.md). Единственный способ проверить его — сравнить с прибором на входе
230 В (реле Shelly: реальные Вт и накопленные Вт·ч). Терминальная утилита
`tools/energy_compare.py` это умеет, но требует сидеть в консоли — сателлит делает то же самое
фоном, с показом в своей карточке.

⚠ ВРЕМЕННЫЙ. Сверка — исследовательская задача; когда модель подтвердят на нескольких драйверах,
сателлит снимается вместе с картой (как сняли «Замер» в v1.2.6). Поэтому он МАКСИМАЛЬНО развязан:

- **ядро не трогает** — только ЧИТАЕТ (`EnergyStore`, состояния сущностей) и пишет в СВОЙ стор;
- в управляющие пути не вмешивается, на шину не ходит НИ ОДНОЙ команды;
- падение сателлита не должно ронять интеграцию (setup обёрнут в try у зовущего).

КАК СЧИТАЕТ. Раз в интервал снимается срез: наша мощность (та же формула, что у бейджа —
`power_at(model, power_w, доля яркости)`), наш накопитель `energy_wh`, мощность и накопитель реле,
плюс состояния лампы и реле. Сравниваются **ДЕЛЬТЫ от старта сессии**: `ratio = Δнаш/Δреле`,
цель — 1.0.

⚠ ЛОВУШКА (docs/ENERGY_CALC_MODEL.md §5): счётчик Вт·ч у реле КВАНТОВАН (тики 0.2–0.4 Вт·ч) —
на коротком окне невыпавший тик даёт до 6–8 % ложной «ошибки модели». Поэтому карточка показывает
возраст сессии и не даёт трактовать `ratio` раньше часа: энергию сверяем ДЛИННЫМ прогоном, а форму
кривой — мгновенной мощностью (там квантования нет).
"""

from __future__ import annotations

import logging

from homeassistant.core import HomeAssistant

from .collector import VerifyCollector, _COLLECTOR_KEY
from .store import VerifyStore, _HASS_KEY, get_verify_store
from .websocket_api import async_register_verify

_LOGGER = logging.getLogger(__name__)


async def async_setup_verify(hass: HomeAssistant) -> None:
    """Поднять сателлит один раз (из async_setup компонента). Идемпотентно."""
    if get_verify_store(hass) is not None:
        return
    store = VerifyStore(hass)
    await store.async_load()
    hass.data[_HASS_KEY] = store

    collector = VerifyCollector(hass, store)
    hass.data[_COLLECTOR_KEY] = collector
    collector.start()          # если в сторе осталась активная сессия — продолжит её

    async_register_verify(hass)
    _LOGGER.info("сателлит сверки энергии поднят (сессия %s)",
                 "активна" if store.session else "не начата")
