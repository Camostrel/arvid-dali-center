"""Свой журнал событий интеграции — независимо от логов HA.

Зачем: лог HA не отслеживает состояние шлюза/устройств и быстро вытесняется
(буфер ~100 строк). Здесь — своя семантика: связь (online/offline/reauth),
доступность устройств, команды (ack/таймаут), скан, операции с группами.

Хранилище двухуровневое:
  • кольцо последних MEM_MAX событий в памяти — отдаётся в карточку (панель «Журнал»);
  • файл на диске с ротацией по размеру — долгая история (ёмкость ≥ 20000 записей).

Логировать можно из ЛЮБОГО потока (paho/reader): запись в память/файл потокобезопасна,
уведомление карточки прокидывается в петлю HA через call_soon_threadsafe.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import threading
import time
from collections import deque

from homeassistant.core import HomeAssistant
from homeassistant.helpers.dispatcher import async_dispatcher_send

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

_HASS_KEY = "arvid_dali_center_eventlog"
SIGNAL_EVENTLOG = f"{DOMAIN}_eventlog"   # (record: dict) — новое событие журнала

# Видимых в карточке (последние) и ёмкость файла.
MEM_MAX = 1000
FILE_MAX_BYTES = 4_000_000   # ~4 МБ на файл
FILE_BACKUPS = 1             # + одна ротация → ≤ ~8 МБ, ёмкость ~40k строк (≥ 20000)

_LOG_NAME = "arvid_dali_center_events.log"


class EventLog:
    """Журнал событий: память (кольцо) + файл с ротацией."""

    def __init__(self, hass: HomeAssistant, path: str) -> None:
        self.hass = hass
        self._path = path
        self._ring: deque[dict] = deque(maxlen=MEM_MAX)
        self._lock = threading.Lock()
        self._seq = 0

    def log(self, gw_sn: str | None, kind: str, message: str,
            level: str = "info", **extra) -> None:
        """Записать событие. kind — категория (conn/avail/cmd/scan/group/...),
        level — info|warn|error. extra — произвольные поля (devType/address/...)."""
        with self._lock:
            self._seq += 1
            rec = {
                "seq": self._seq, "ts": round(time.time(), 3), "gw": gw_sn or "",
                "kind": kind, "level": level, "msg": message,
            }
            if extra:
                rec["extra"] = extra
            self._ring.append(rec)
            self._write_file(rec)
        # уведомить карточку (подписчиков) — строго в петле HA
        if self.hass:
            self.hass.loop.call_soon_threadsafe(
                async_dispatcher_send, self.hass, SIGNAL_EVENTLOG, rec)

    def _write_file(self, rec: dict) -> None:
        """Дозапись строки в файл с ротацией по размеру (под _lock)."""
        try:
            if (os.path.exists(self._path)
                    and os.path.getsize(self._path) >= FILE_MAX_BYTES):
                self._rotate()
            with open(self._path, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except OSError as err:
            _LOGGER.debug("eventlog: запись в файл не удалась: %s", err)

    def _rotate(self) -> None:
        """Сдвиг .log → .log.1 (старше FILE_BACKUPS — удаляем)."""
        oldest = f"{self._path}.{FILE_BACKUPS}"
        with contextlib.suppress(OSError):
            if os.path.exists(oldest):
                os.remove(oldest)
        for i in range(FILE_BACKUPS, 0, -1):
            src = self._path if i == 1 else f"{self._path}.{i - 1}"
            dst = f"{self._path}.{i}"
            if os.path.exists(src):
                with contextlib.suppress(OSError):
                    os.replace(src, dst)

    def recent(self, gw_sn: str | None = None, limit: int = MEM_MAX) -> list[dict]:
        """Последние события (опц. фильтр по шлюзу) для карточки."""
        with self._lock:
            items = list(self._ring)
        if gw_sn:
            items = [r for r in items if r["gw"] == gw_sn]
        return items[-limit:]


def get_eventlog(hass: HomeAssistant) -> EventLog | None:
    return hass.data.get(_HASS_KEY)


async def async_setup_eventlog(hass: HomeAssistant) -> EventLog:
    """Создать журнал один раз при старте (путь — в каталоге конфигурации HA)."""
    el = EventLog(hass, hass.config.path(_LOG_NAME))
    hass.data[_HASS_KEY] = el
    el.log(None, "system", "журнал инициализирован")
    return el
