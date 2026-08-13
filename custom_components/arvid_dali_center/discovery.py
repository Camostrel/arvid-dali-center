"""Общий диспетчер discovery: ОДИН multicast-залп на все шлюзы — как «Add gateway» в DALI
Center и `/api/gateways` (=discover_all) в own_dali_center, вместо точечного поиска по
каждому шлюзу.

Зачем (масштаб 20-60 шлюзов на объектах):
- A3 (воровство ответов): при точечном `discover_gateway(gwSn)` у КАЖДОГО шлюза свой UDP-
  приёмник на общем порту 50569 → ОС отдаёт multicast-ответ лишь одному сокету → сокеты
  «воруют» ответы, шлюз может не услышать свой. Один залп = один приёмник → воровства нет.
- Массовый offline (вырубание света): десятки сторожей одновременно зовут блокирующий поиск
  по 12с → исчерпывают общий пул потоков HA → весь HA «душится». Один залп на всех + дедуп.

Модель (как протокол, стр.4: discover без gwSn → отвечают ВСЕ шлюзы):
- один `discover_all` (один сокет, собирает все шлюзы) с дедупликацией — параллельные
  запросы ШАРЯТ один залп (ждут общий asyncio.Lock, не плодят сокеты/потоки);
- короткий TTL-кеш: пачка одновременных стартов/реконнектов берёт результат из одного залпа;
- подключение (MQTT) — через общий семафор (по N за раз), а не «все разом».

Креды ДИНАМИЧЕСКИЕ, но один залп даёт валидные креды для подключения ВСЕХ найденных шлюзов
(так и работает `connect_all` в референсе: один discover_all → коннект каждого).
"""

from __future__ import annotations

import asyncio
import logging
import time

from homeassistant.core import HomeAssistant

from .transport.core import discover_all

_LOGGER = logging.getLogger(__name__)

_HASS_KEY = "arvid_dali_center_discovery"
_SEM_KEY = "arvid_dali_center_connect_sem"

_DEFAULT_TTL = 6.0            # c — пачка одновременных запросов шарит один залп
_SWEEP_TIMEOUT = 8.0         # c — окно сбора ответов в одном залпе
_MAX_CONCURRENT_CONNECT = 4  # одновременных MQTT-подключений (бережём пул потоков HA)


class DaliDiscovery:
    """Один на hass: общий залп discover_all с дедупом и коротким кешем (по bind_ip)."""

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass
        # bind_ip -> {"ts": monotonic, "gws": {gwSn: gw_dict}}
        self._cache: dict[str, dict] = {}
        # bind_ip -> asyncio.Lock (дедуп залпов по интерфейсу)
        self._locks: dict[str, asyncio.Lock] = {}

    def _lock(self, bind_ip: str) -> asyncio.Lock:
        lk = self._locks.get(bind_ip)
        if lk is None:
            lk = self._locks[bind_ip] = asyncio.Lock()
        return lk

    async def get(self, bind_ip: str, gw_sn: str, *, max_age: float = _DEFAULT_TTL,
                  force: bool = False) -> dict | None:
        """Данные шлюза `gw_sn` из ОБЩЕГО залпа. Свежий кеш → мгновенно; иначе один залп
        discover_all (дедуп: параллельные ждут его под локом). `force` — игнорировать кеш.
        Вернёт None, если шлюз не ответил в залпе (offline) — caller делает fallback/повтор."""
        now = time.monotonic()
        ent = self._cache.get(bind_ip)
        if not force and ent and (now - ent["ts"] < max_age):
            return ent["gws"].get(gw_sn)
        async with self._lock(bind_ip):
            # двойная проверка: пока ждали лок, другой залп мог обновить кеш (тогда не сканим)
            ent = self._cache.get(bind_ip)
            if not force and ent and (time.monotonic() - ent["ts"] < max_age):
                return ent["gws"].get(gw_sn)
            try:
                gws = await self._hass.async_add_executor_job(
                    lambda: discover_all(bind_ip=bind_ip, timeout=_SWEEP_TIMEOUT))
            except Exception as err:  # noqa: BLE001
                _LOGGER.warning("discovery: залп bind=%s не удался: %s", bind_ip, err)
                return None
            mapping = {g["gwSn"]: g for g in gws if g.get("gwSn")}
            self._cache[bind_ip] = {"ts": time.monotonic(), "gws": mapping}
            _LOGGER.info("discovery: общий залп bind=%s нашёл %d шлюзов", bind_ip, len(mapping))
            return mapping.get(gw_sn)


def get_discovery(hass: HomeAssistant) -> DaliDiscovery:
    """Сервис из hass.data (создаёт при первом обращении). Идемпотентно."""
    svc = hass.data.get(_HASS_KEY)
    if svc is None:
        svc = hass.data[_HASS_KEY] = DaliDiscovery(hass)
    return svc


def get_connect_semaphore(hass: HomeAssistant) -> asyncio.Semaphore:
    """Общий семафор на MQTT-подключения: не больше N одновременно (старт+реконнект),
    чтобы пачка коннектов при массовом восстановлении не исчерпала пул потоков HA."""
    sem = hass.data.get(_SEM_KEY)
    if sem is None:
        sem = hass.data[_SEM_KEY] = asyncio.Semaphore(_MAX_CONCURRENT_CONNECT)
    return sem
