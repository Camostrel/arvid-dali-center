"""Coordinator — постоянная сессия со шлюзом DALI.

Порт идей odc/manager.py БЕЗ db/reslog: держит одно MQTT-подключение, фоновым
потоком читает сообщения, сопоставляет ответы команд по msgId, собирает список
устройств шины (searchDev) и публикует обновления состояний в HA через диспетчер.
Блокирующий транспорт (сокеты/paho) изолирован в executor-потоках HA.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import random
import threading
import time

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import (
    async_dispatcher_connect,
    async_dispatcher_send,
)

from .const import (
    DOMAIN,
    PANEL_DEFAULT_FADE_RATE,
    PANEL_FADE_RATE_STEPS,
    PANEL_HOLD_RATE_GAIN,
)
from .eventlog import get_eventlog
# naming — чистый модуль (ни HA, ни coordinator не тянет) → импорт на уровне модуля безопасен,
# цикла нет. Остальные функции naming импортируются отложенно (исторически), см. _desired_entity_id.
from .naming import is_auto_suffix
from .transport.core import GatewaySession, dev_key
from .transport.decode import is_valid_devsn

_LOGGER = logging.getLogger(__name__)

# alarmCodeReport: шлюз шлёт его РЕГУЛЯРНО как диагностику драйвера (наработка/температура/
# счётчик отказов), а НЕ только при аварии — сам факт кода ≠ авария (проверено захватом: приходят
# gearRunningTime + overTemperature=45°C, это норма; DALI Center показывает их как инфо, не тревогу).
# Реальной аварией считаем ТОЛЬКО жёсткие коды-отказы, счётчик отказов > 0, и перегрев ВЫШЕ порога.
_HARD_FAULT = {"openCircuit", "shortCircuit", "gearFailure", "lampFailure", "ballastFailure"}
_OVERTEMP_WARN_C = 80   # overTemperature.value = текущая T драйвера (°C); тревога только выше порога

# Fix E (v1.2.22): окно, в котором `devStatus` считается ЭХОМ на нашу же команду, а не
# самостоятельным признаком жизни устройства (см. `_revive_from_status`). Ответ шлюза приходит
# за доли секунды; 3 с — с запасом на забитую шину, но заметно меньше периода push'ей (~24 с).
_ECHO_WINDOW_S = 3.0
# Потолок трекера наших команд: ключ на устройство, чистим по возрасту (S5 — рост без TTL).
_CMD_SENT_MAX = 4096
# Порог, с которого расхождение часов шлюза с HA считаем значимым (расписания датчиков —
# окна "HH:MM-HH:MM" — исполняет сам шлюз по своим часам, минута мимо уже видна человеку).
_GW_TIME_SKEW_WARN_S = 60

# Сколько составов групп читаем ОДНОВРЕМЕННО (v1.2.46). Лимит — не осторожность ради
# осторожности: шлюз обслуживает одну DALI-шину, и залп без ограничителя превращается в
# шторм MQTT-сообщений. Тот же лимит держит и upstream-SDK (MAX_CONCURRENT_READS).
GROUP_READ_CONCURRENCY = 3

# Сигнал обновления состояния устройства: (gw_sn, key, data).
# ОБЩИЙ реестр живых кросс-шлюзовых групп (ключ в hass.data). Общий, а не пошлюзный:
# сущность создаёт ЯКОРЬ (алфавитно первый участник), и его ConfigEntry может подняться
# раньше остальных — поздний участник должен найти группу сам, чтобы пересобрать подписку.
XGROUP_ENTITIES = f"{DOMAIN}_xgroup_entities"

SIGNAL_DEV_UPDATE = f"{DOMAIN}_dev_update"
# Сигнал смены состояния связи шлюза: (gw_sn, state).
SIGNAL_CONN_UPDATE = f"{DOMAIN}_conn_update"
# Сигнал смены доступности устройства (из onlineStatus): (gw_sn, key, online).
SIGNAL_AVAIL_UPDATE = f"{DOMAIN}_avail_update"
# Сигнал смены ОПТИМИСТИЧНОГО состояния лампы: (gw_sn, key, is_on, brightness).
# Лампа шлёт его при своей команде / распространении из группы / readDev → группы,
# содержащие эту лампу, пересчитывают агрегат (G2). brightness — HA 1..255 или None.
SIGNAL_LAMP_STATE = f"{DOMAIN}_lamp_state"

# Состояния связи шлюза.
ST_INIT = "init"
ST_ONLINE = "online"
ST_OFFLINE = "offline"
ST_REAUTH = "reauth"   # короткое состояние во время попытки пересборки сессии

# Watchdog: период авто-попыток восстановления связи (объект работает автономно —
# ждать ручного запуска нельзя). Re-discovery, БЕЗ скана шины.
# Масштаб (десятки шлюзов): каждый offline-хаб шлёт multicast-поиск независимо. Чтобы
# N сторожей не били синхронным залпом, период идёт с ДЖИТТЕРОМ (случайный разброс), а
# при длительном offline растёт по BACKOFF до потолка (мёртвый шлюз не молотит каждые 20с).
_WATCHDOG_PERIOD = 20.0          # базовый период (первая попытка)
_WATCHDOG_JITTER = 10.0          # верхняя граница случайной добавки к периоду, с
_WATCHDOG_MAX_PERIOD = 300.0     # потолок периода при затяжном offline (5 мин)


def dev_state_key(devtype: str, channel: int, address: int) -> str:
    """Единый ключ устройства devType:channel:address (как в odc)."""
    return f"{devtype}:{channel}:{address}"


# Типы ламп (дублируем здесь, чтобы не импортировать light.py — был бы цикл).
_LIGHT_TYPES = {"0101", "0102", "0103", "0104", "0105", "0106"}

def orphan_key(devsn: str, devtype: str) -> str:
    """Ключ ОСИРОТЕВШЕЙ записи (v1.2.2) — по ИДЕНТИЧНОСТИ, а не по адресу.

    Осиротевший — устройство, чей адрес занял другой devSn, а само оно на шине не нашлось. Его
    адресный ключ принадлежит теперь НОВОМУ жильцу, поэтому хранить осиротевшего по адресу нельзя:
    записи затрут друг друга (так он раньше и исчезал из кеша бесследно). Отдельный префикс
    гарантирует, что ключ не столкнётся с адресным (`dev_state_key`)."""
    return f"orphan:{devsn}:{devtype}"


def conflict_class(devtype: str) -> str:
    """Класс адресного пространства DALI, как его называет шлюз в `AddrConflicts`.

    У ламп (управляющие устройства, `dali`) и датчиков/панелей (устройства ввода, `dali2`)
    РАЗНЫЕ пространства коротких адресов: адрес 5 у лампы и адрес 5 у датчика — не конфликт.
    Поэтому конфликт сопоставляем с устройством по ТРОЙКЕ (канал, класс, адрес), а не по адресу.
    """
    return "dali" if str(devtype) in _LIGHT_TYPES else "dali2"


class DaliAvailMixin:
    """Доступность сущности: связь шлюза + (опц.) online устройства из onlineStatus.

    Подмешивается к сущностям платформ. Использует self._hub и self._avail_key
    (None → завязка только на связь шлюза, без per-device online — напр. группа)."""

    _avail_key: str | None = None

    @property
    def available(self) -> bool:
        hub = self._hub
        if getattr(hub, "state", ST_ONLINE) != ST_ONLINE:
            return False
        if self._avail_key is None:
            return True
        return hub.online_map.get(self._avail_key, True)

    def _wire_avail(self) -> None:
        """Подписки на связь шлюза и доступность устройства (звать из async_added)."""
        self.async_on_remove(async_dispatcher_connect(
            self.hass, SIGNAL_CONN_UPDATE, self._avail_on_conn))
        self.async_on_remove(async_dispatcher_connect(
            self.hass, SIGNAL_AVAIL_UPDATE, self._avail_on_dev))

    @callback
    def _avail_on_conn(self, gw_sn: str, state: str) -> None:
        if gw_sn == self._hub.gw_sn:
            self.async_write_ha_state()

    @callback
    def _avail_on_dev(self, gw_sn: str, key: str, online: bool) -> None:
        if gw_sn == self._hub.gw_sn and self._avail_key is not None and key == self._avail_key:
            self.async_write_ha_state()


class DaliBusEntity(DaliAvailMixin):
    """Шинная сущность (лампа/датчик/панель/переключатель) с ДИНАМИЧЕСКИМ жизненным
    циклом: саморегистрация в хабе, обновление адреса по devSn при ре-нумерации,
    пометка «ушла». Подмешивается ПЕРЕД классом платформы (light/sensor/...).
    Требует у наследника: self._hub, _devtype, _channel, _address, _key, _role,
    self._attr_unique_id (стабилен — обычно devSn)."""

    _role: str = ""          # light/motion/lux/event/active_<devtype>
    _gone: bool = False      # устройство исчезло из кеша шлюза (после сверки скана)

    @property
    def available(self) -> bool:
        if self._gone:
            return False
        return super().available

    def _bus_register(self) -> None:
        """Зарегистрировать живую сущность в хабе (звать из async_added_to_hass).
        Хаб трекает её по unique_id (для reconcile) и по (role,key) (для резолва в карточке)."""
        self._hub.register_bus_entity(self._role, self._attr_unique_id, self)
        self._hub.register_entity_uid(self._role, self._key, self._attr_unique_id)
        self.async_on_remove(self._bus_unregister)
        # Персист ЗОМБИ через рестарт: устройство, помеченное зомби прошлым сканом (не найдено
        # на шине), при старте грузится из DeviceStore, и платформа создаёт сущность заново.
        # reconcile при старте НЕ зовётся (только после скана) → без этого сущность «оживала»
        # доступной без данных (особенно датчик — push-only, offline сам не шлёт). Применяем
        # сохранённый флаг → gone, чтобы зомби оставался красным/недоступным (чистка — «Забыть»).
        dev = self._hub.devices.get(self._key)
        if dev and dev.get("zombie") and not self._gone:
            self._gone = True

    def _bus_unregister(self) -> None:
        self._hub.unregister_bus_entity(self._role, self._attr_unique_id, self)
        self._hub.unregister_entity_uid(self._role, self._key, self._attr_unique_id)

    @callback
    def update_from_dev(self, dev: dict) -> None:
        """Ре-нумерация: тот же devSn, но сменился адрес/канал → обновить, чтобы команды
        (writeDev и т.п.) уходили на актуальный адрес. Чинит «команда мимо» после auto."""
        ch, addr = dev.get("channel"), dev.get("address")
        if ch == self._channel and addr == self._address and not self._gone:
            return
        old_key = self._key
        self._channel, self._address = ch, addr
        self._key = dev_state_key(self._devtype, ch, addr)
        self._avail_key = self._key
        self._gone = False
        # перерегистрировать (role,key) под новым адресом (карточка резолвит по нему)
        self._hub.unregister_entity_uid(self._role, old_key, self._attr_unique_id)
        self._hub.register_entity_uid(self._role, self._key, self._attr_unique_id)
        if self.hass:
            self.async_write_ha_state()

    @callback
    def mark_gone(self) -> None:
        """Устройство исчезло с шины (сверка скана) → сущность недоступна (не удаляем)."""
        if not self._gone:
            self._gone = True
            if self.hass:
                self.async_write_ha_state()


class DaliGatewayHub:
    """Одна постоянная сессия на шлюз. Создаётся в async_setup_entry."""

    def __init__(self, hass: HomeAssistant, bind_ip: str, gw_sn: str) -> None:
        self.hass = hass
        self.bind_ip = bind_ip
        self.gw_sn = gw_sn
        self.entry_id: str | None = None   # ConfigEntry этого шлюза (для перепривязки сущностей)
        self.gw: dict | None = None
        self.session: GatewaySession | None = None
        self.sw = ""
        self.fw = ""
        # часы шлюза (getTimeZone) — для расписаний датчиков; skew=None пока не прочитано
        self.gw_time = ""
        self.gw_timezone = ""
        self.gw_time_skew_s: float | None = None
        self.connected = False
        # состояние связи (init/online/offline/reauth/failed) — реальное, по событиям paho
        self.state = ST_INIT
        self._was_online = False
        self._rebuilding = False
        self._watchdog_task: asyncio.Task | None = None
        # трекинг фоновых задач (rotary-драйв, force-entity_id, пост-реконнект) — чтобы
        # отменить их при unload; иначе висят и трогают реестр/сессию после отвязки, за
        # годы reload копятся untracked корутины (W5)
        self._tasks: set[asyncio.Task] = set()
        # живая доступность устройств из onlineStatus (key → bool); шлюз шлёт сам
        self.online_map: dict[str, bool] = {}
        # DALI-шина занята (пуш `statusBus`, мануал стр. 64) — при ней шлюз отклоняет команды
        self.bus_busy = False
        # когда МЫ последний раз слали команду на этот адрес (key → monotonic). Нужно ТОЛЬКО
        # для Fix E (v1.2.22): спонтанный `devStatus` снимает залипший offline, а эхо на нашу
        # же команду — НЕ снимает (шлюз может отозваться, не дождавшись ответа лампы).
        self._cmd_sent: dict[str, float] = {}
        # желаемая активность датчиков — ключ ИДЕНТИЧНОСТИ `devSn:devType` (Fix L), НЕ адрес
        self.sensor_active: dict[str, bool] = {}
        # кеш устройств шины (devType/channel/address/name/devSn/status + live)
        self.devices: dict[str, dict] = {}
        self._lock = threading.RLock()
        self._pending: dict[str, dict] = {}   # msgId -> {event,result}
        self._running = False
        self._reader: threading.Thread | None = None
        # сбор searchDev (как в manager.py — фон-поток владеет recv)
        self._search_active = False
        self._search_overall = True
        self._search_buf: dict[str, dict] = {}
        self._search_done = threading.Event()
        # получили ли ХОТЬ ОДИН searchDevRes за скан: отличает «шлюз не ответил» (обрыв —
        # кеш НЕ трогаем) от «шлюз ответил: 0 устройств» (пустая шина — сверка чистит).
        self._search_got_response = False
        self._scan_cb = None  # колбэк прогресса скана (живой лог найденных)
        # конфликтные адреса шины (push AddrConflicts от шлюза в режиме manual)
        self._conflict_cb = None            # колбэк живого лога конфликтов
        self._search_conflicts: list[dict] = []
        self._conflict_keys: set = set()
        self.conflicts: list[dict] = []     # последний снимок конфликтов (для карточки)
        self.groups: list[dict] = []   # DALI-группы с составом (для light-сущностей групп)
        self._light_adder = None        # callback платформы light для динамического добавления
        self._group_entities: dict[tuple, object] = {}  # (channel,groupId) → живая DaliGroupLight

        # карта реальных unique_id живых шинных сущностей: (role, dev_state_key) → unique_id.
        # Карточка резолвит entity_id по СТАБИЛЬНОМУ ключу devType:channel:address, а не по
        # (волатильному) devSn — шлюз иногда портит devSn и ломает резолв entity_id.
        self._entity_uids: dict[tuple, str] = {}
        # ── динамика сущностей (Фаза D) ──
        # платформы регистрируют (adder, factory) — чтобы хаб мог создавать сущности на лету
        self._platforms: dict[str, tuple] = {}     # platform → (async_add_entities, factory)
        # живые шинные сущности по unique_id (стабилен) — для reconcile/update/gone
        self._bus_entities: dict[str, object] = {}
        # рантайм регулировки яркости поворотной панелью (devSn → состояние энкодера):
        # {last: позиция 0..255, level: яркость шины 0..1000, busy/dirty для коалесинга)
        self._rotary_rt: dict[str, dict] = {}
        # рантайм удержания кнопки «плавно» (баг2): (panel_key, keyNo) → monotonic старта hold.
        # На hold_end: dt → эмпирическая яркость цели (см. docs/PLAN_PANEL_HOLD_DIM.md).
        self._hold_rt: dict[tuple, float] = {}
        # ── алармы/диагностика ОТ шлюза (RAM-кеш) ────────────────────────────────
        # ⚠ v1.2.6: ЭНЕРГИЯ ОТ ШЛЮЗА (`reportEnergy`) БОЛЬШЕ НЕ ПРИНИМАЕТСЯ. Шлюз энергию не
        # измеряет: он либо ретранслирует энергобанк драйвера, либо ВЫДУМЫВАЕТ число, и снаружи
        # эти случаи неразличимы (разброс ×0.2…×1.35 — docs/ENERGY_CALC_MODEL.md §1). Показывать
        # такое число — врать. Живёт только РАСЧЁТНЫЙ путь: P = power_w × кривая(яркость).
        # Вместе с приёмом сняты: `energy_live` (RAM-кеш ватт от шлюза), накопитель `real_wh` и
        # калибровочный «Замер» (он и питался ИСКЛЮЧИТЕЛЬНО этими числами — мерил то, чему нельзя
        # верить; свою задачу — доказать несостоятельность шлюза — он выполнил).
        # ⚠ alarmCodeReport ОСТАЁТСЯ: это НЕ энергия, а аварии/телеметрия драйвера.
        #
        # alarmCodeReport: только РЕАЛЬНЫЕ аварии по лампе (жёсткий отказ/перегрев выше порога/
        # счётчик отказов>0). {codes: {code: value}, ts}. Отдаётся в `energy_live` → бейдж карты.
        self.alarms: dict[str, dict] = {}
        # Телеметрия драйвера из того же alarmCodeReport (наработка/температура/счётчик) — НЕ авария,
        # для инфо/будущего показа. {codes: {code: value}, ts}. Ключ devSn.
        self.diagnostics: dict[str, dict] = {}

    # ── жизненный цикл (блокирующее — через executor) ────────────────────────
    def _connect_blocking(self, gw: dict | None = None) -> dict:
        # Креды ДИНАМИЧЕСКИЕ (каждую сессию). gw приходит ТОЛЬКО из ОБЩЕГО залпа discovery
        # (get_discovery, один сокет на все шлюзы). Свой точечный discover_gateway больше НЕ
        # открываем: индивидуальный приёмник на 50569 ВНЕ общего залпа воскрешал «воровство»
        # multicast-ответов при массовом старте/offline (то, что чинил v0.72). Нет в залпе
        # (шлюз offline) → offline + watchdog повторит через ОБЩИЙ залп.
        if not gw:
            raise RuntimeError(f"шлюз {self.gw_sn} не найден в общем залпе discovery "
                               f"(bind={self.bind_ip}) — offline, watchdog повторит")
        self.gw = gw
        sess = GatewaySession(gw)
        sess.on_state = lambda st, s=sess: self._on_session_state(st, s)  # привязка к ЭТОЙ сессии
        try:
            self.session = sess.connect(timeout=10.0)
        except Exception:
            # ВАЖНО: при ошибке connect закрыть сессию, иначе paho-loop остаётся
            # «зомби» и бесконечно шлёт on_disconnect (ложная «связь потеряна»).
            with contextlib.suppress(Exception):
                sess.close()
            raise
        self._running = True
        self._start_reader(self.session)
        # «представиться» шлюзу + снять версию (после getVersion идут события)
        v = self._request_blocking("getVersion", "getVersionRes", timeout=5.0)
        if v:
            self.sw = v.get("data", {}).get("swVersion", "")
            self.fw = v.get("data", {}).get("fwVersion", "")
        self._read_gw_time()      # v1.2.23: часы шлюза — критичны для расписаний датчиков
        self.connected = True
        self.state = ST_ONLINE
        self._was_online = True
        _LOGGER.info("шлюз %s подключён: ip=%s sw=%s fw=%s",
                     self.gw_sn, gw.get("gwIp"), self.sw or "?", self.fw or "?")
        self._log("conn", f"шлюз {self.gw_sn} подключён "
                  f"(ip={gw.get('gwIp')}, sw={self.sw or '?'})")
        return gw

    def _read_gw_time(self) -> None:
        """Снять часы шлюза (`getTimeZone`, мануал стр. 8) и посчитать расхождение с HA.

        ЗАЧЕМ (переоценка v1.2.23). Раньше время шлюза нам было не нужно — энергию считаем сами,
        и `getTimeZone`/`updateTimeZone` мы намеренно не звали. Но РАСПИСАНИЕ датчиков
        (`runCondition` devType `0701`, окна "HH:MM-HH:MM", docs/PLAN_SENSOR_BINDINGS §H4)
        исполняет САМ ШЛЮЗ по СВОИМ часам. Сбитые часы (сброс питания, нет NTP, чужой пояс) =
        свет включается не тогда, и снаружи это невидимо: расписание «есть», а срабатывает мимо.

        Только ЧИТАЕМ и показываем расхождение. `updateTimeZone` (запись) отсюда НЕ зовём —
        принцип «без авто-деструктива»: часами шлюза пользуется и DALI Center, решение за
        человеком (кнопка синхронизации)."""
        from datetime import datetime
        self.gw_time = self.gw_timezone = ""
        self.gw_time_skew_s = None
        r = self._request_blocking("getTimeZone", "getTimeZoneRes", timeout=5.0)
        if not r:
            return
        self.gw_time = str(r.get("time") or "")
        self.gw_timezone = str(r.get("timezone") or "")
        try:                                  # сравниваем ЛОКАЛЬНОЕ время шлюза с локальным HA
            gw_dt = datetime.strptime(self.gw_time, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            _LOGGER.warning("шлюз %s: не разобрал время «%s» (ждём YYYY-MM-DD HH:MM:SS)",
                            self.gw_sn, self.gw_time)
            return
        self.gw_time_skew_s = (gw_dt - datetime.now()).total_seconds()
        skew = abs(self.gw_time_skew_s)
        msg = (f"часы шлюза {self.gw_time} ({self.gw_timezone}), расхождение с HA "
               f"{self.gw_time_skew_s:+.0f} с")
        if skew > _GW_TIME_SKEW_WARN_S:
            _LOGGER.warning("шлюз %s: %s — РАСПИСАНИЯ датчиков сработают не вовремя", self.gw_sn, msg)
            self._log("conn", f"⚠ {msg} — расписания датчиков сработают не вовремя")
        else:
            _LOGGER.info("шлюз %s: %s", self.gw_sn, msg)

    def _start_reader(self, session) -> None:
        """Поток-читатель привязан к КОНКРЕТНОЙ сессии: при её замене (реконнект)
        старый читатель сам выходит (self.session is session → False)."""
        self._reader = threading.Thread(
            target=self._read_loop, args=(session,),
            name=f"adc-reader-{self.gw_sn}", daemon=True)
        self._reader.start()

    async def async_connect(self, gw: dict | None = None) -> dict:
        return await self.hass.async_add_executor_job(self._connect_blocking, gw)

    def ensure_watchdog(self) -> None:
        """Запустить watchdog связи (ФОНОВАЯ задача — HA не ждёт её на старте). Зовётся
        из setup ВСЕГДА (даже если коннект не удался) — чтобы оффлайн-шлюз поднимался."""
        if self._watchdog_task is None:
            self._watchdog_task = self.hass.async_create_background_task(
                self._watchdog(), name=f"adc-watchdog-{self.gw_sn}")

    def mark_offline(self) -> None:
        """Пометить хаб оффлайн (коннект при setup не удался). was_online=True — чтобы
        ПЕРВЫЙ успешный реконнект активировал датчики (как делает онлайн-setup).

        КРИТИЧНО: `_running=True` — хаб ЖИВ (запись загружена), сторож должен работать и
        восстанавливать связь. Без этого при провале коннекта на старте (шлюз ещё не
        поднялся — напр. после вырубания света) `_running` оставался False (его ставит
        только УСПЕШНЫЙ _connect_blocking) → цикл сторожа `while self._running` выходил
        мгновенно → шлюз застревал offline НАВСЕГДА, даже став доступным."""
        self.state = ST_OFFLINE
        self.connected = False
        self._was_online = True
        self._running = True

    def load_persisted(self) -> None:
        """Поднять устройства и группы из персиста — чтобы шлюз/сущности были видны
        даже без связи (offline). Живые данные обновятся при коннекте/`onlineStatus`."""
        self.load_sensor_prefs()   # v1.2.23: ручные «выключено» — ДО перевзвода датчиков
        from .store import get_device_store, get_group_store
        ds = get_device_store(self.hass)
        if ds:
            # дедуп по (devSn, devType): в персисте, накопленном ДО фикса re-link, один
            # devSn мог осесть на двух адресах (перераздача адресов). Поднять оба нельзя —
            # унесённый unique_id=devSn даёт коллизию сущностей. Оставляем ПЕРВУЮ запись
            # (после старта с живой связью re-link по кешу шлюза выправит на верный адрес).
            seen_ids: set = set()
            for d in ds.all(self.gw_sn):
                if not is_valid_devsn(d.get("devSn")):   # Z1: не поднимаем фантомы (пустой devSn)
                    continue
                ident = (d.get("devSn"), str(d.get("devType")))
                if ident in seen_ids:
                    _LOGGER.warning("шлюз %s: дубль в персисте %s addr%s (devSn %s) — пропущен "
                                    "при подъёме (схлопнётся при скане/коннекте)", self.gw_sn,
                                    d.get("devType"), d.get("address"), d.get("devSn"))
                    continue
                seen_ids.add(ident)
                # ОСИРОТЕВШИЙ (v1.2.2) хранится по ИДЕНТИЧНОСТИ: его адрес принадлежит другому
                # устройству, и адресный ключ столкнул бы их (запись затёрла бы живого жильца).
                k = (orphan_key(d.get("devSn"), d.get("devType")) if d.get("orphan")
                     else dev_state_key(d.get("devType"), d.get("channel"), d.get("address")))
                rec = dict(d)
                rec.pop("bus_seen", None)   # P0: физика подтверждается ТОЛЬКО живым busDevice-сканом
                # этой сессии, не персистом — иначе после рестарта устройство «жило бы» без скана.
                self.devices[k] = rec
                # v1.2.23: поднять ПОСЛЕДНЕЕ ЗНАНИЕ о присутствии. Без этого `online_map` пуст, а
                # его дефолт — «доступна» (`get(k, True)`), и снятое с шины устройство после
                # рестарта HA выглядело живым и управляемым, пока шлюз не пришлёт очередной
                # `onlineStatus` (наблюдение с железа 2026-07-29: лампа на выключенном реле).
                # Только offline: «online» в персисте — прошлое, пусть его подтвердит шлюз.
                if not d.get("orphan") and rec.get("status") == "offline":
                    self.online_map[k] = False
        gs = get_group_store(self.hass)
        if gs:
            self.groups = [{"channel": g["channel"], "groupId": g["groupId"],
                            "name": g.get("name", ""), "members": g.get("members", []),
                            "present": False}
                           for g in gs.all(self.gw_sn)]

    def _wake_pending(self) -> None:
        """Разбудить всех ждущих ответа при разрыве/смене сессии — ответ старой сессии уже
        не придёт. Иначе отправители висят до полного таймаута (5-15с), занимая потоки пула
        executor HA. Будим без результата → caller увидит None (как таймаут) и не зависнет."""
        with self._lock:
            recs = list(self._pending.values())
        for r in recs:
            r["event"].set()

    def _disconnect_blocking(self) -> None:
        self._running = False
        if self.session:
            self.session.close()
        self.session = None
        self.connected = False
        self.state = ST_OFFLINE
        self._wake_pending()                    # не держим ждущих до таймаута

    def devices_snapshot(self) -> list[dict]:
        """Консистентный снимок кеша устройств (копии полей) под локом. Reader-поток и скан
        мутируют self.devices (in-place `.update()` и присвоение нового dict в async_scan),
        поэтому прямая итерация `hub.devices.values()` из петли HA рисковала `RuntimeError:
        dict changed size during iteration` при скане на живой карточке (W4). Внешние читатели
        (платформы, __init__, WS, energy) ходят через этот снимок.

        v1.2.2: в снимок кладём и КЛЮЧ (`key`). Раньше внешний мир адресовал устройство тройкой
        (devType, channel, address) — но у ОСИРОТЕВШЕГО (см. `orphan_key`) адрес тот же, что у
        занявшего его место жильца, и «Забыть» по адресу попало бы НЕ В ТОГО."""
        with self._lock:
            return [dict(d, key=k) for k, d in self.devices.items()]

    def _track_task(self, coro) -> None:
        """Создать фоновую задачу с трекингом → отменяется в async_disconnect (W5): иначе
        задача висит и трогает реестр/сессию после отвязки, за годы reload копятся untracked."""
        task = self.hass.async_create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def async_disconnect(self) -> None:
        # ВАЖНО: глушим _running ДО отмены watchdog и ДОЖИДАЕМСЯ его завершения. Иначе если
        # watchdog в этот момент пересобирает сессию в executor (_reconnect_session), отмена
        # его не прервёт, и он создаст НОВУЮ MQTT-сессию + reader-поток уже после unload —
        # «зомби»-сессия, не привязанная к hub (копится при reload/обновлениях за годы).
        self._running = False
        task = self._watchdog_task
        self._watchdog_task = None
        if task:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        # отменить фоновые задачи (rotary/force-entity_id/пост-реконнект), чтобы не трогали
        # реестр/сессию после отвязки записи (W5)
        for t in list(self._tasks):
            t.cancel()
        self._tasks.clear()
        await self.hass.async_add_executor_job(self._disconnect_blocking)

    # ── связь: состояние, журнал, watchdog, реконнект ────────────────────────
    def _log(self, kind: str, message: str, level: str = "info", **extra) -> None:
        """Записать событие в свой журнал (если он поднят)."""
        el = get_eventlog(self.hass)
        if el:
            el.log(self.gw_sn, kind, message, level, **extra)

    def _dispatch_conn(self) -> None:
        """Известить сущности/карточку о смене состояния связи (в петле HA)."""
        if self.hass:
            self.hass.loop.call_soon_threadsafe(
                async_dispatcher_send, self.hass, SIGNAL_CONN_UPDATE,
                self.gw_sn, self.state)

    def _on_session_state(self, state: str, session=None) -> None:
        """Колбэк из потока paho: реальная связь установлена/потеряна.

        session — какая именно сессия породила событие. offline от УСТАРЕВШЕЙ
        (заменённой/закрытой при реконнекте) сессии игнорируем — иначе закрытие
        старой сессии гасит состояние новой (ложная «связь потеряна»)."""
        if (state == "offline" and session is not None
                and self.session is not None and session is not self.session):
            return
        if state == "online":
            was = self._was_online
            self.state = ST_ONLINE
            self.connected = True
            self._was_online = True
            self._log("conn", f"шлюз {self.gw_sn}: связь установлена")
            self._dispatch_conn()
            if was:   # это ВОССТАНОВЛЕНИЕ, не первичный коннект → перевзвести датчики
                self._schedule_coro(self._async_on_reconnected)
        elif state == "offline":
            self.state = ST_OFFLINE
            self.connected = False
            self._log("conn", f"шлюз {self.gw_sn}: связь потеряна", level="warn")
            self._dispatch_conn()

    def _schedule_coro(self, factory) -> None:
        """Запустить корутину в петле HA из стороннего потока (factory() → coro). Задача
        трекается (_track_task) → отменяется при unload (W5)."""
        if self.hass:
            self.hass.loop.call_soon_threadsafe(
                lambda: self._track_task(factory()))

    async def _async_on_reconnected(self) -> None:
        """После восстановления связи: представиться, активировать датчики (кроме
        выключенных вручную) и обновить presence групп. Скан шины НЕ запускаем."""
        self._log("conn", f"шлюз {self.gw_sn}: восстановление — getVersion + датчики + группы")
        # РЕСИНК доступности: online_map=False залипал (устройство помечено offline и больше
        # не переопрашивалось при потере/смене адреса) → сущность висела «не на связи» до
        # рестарта HA. На реконнекте сбрасываем карту к дефолту (online); шлюз сам пришлёт
        # onlineStatus и уточнит реальные offline. Перерисовку дёргает сигнал связи ниже.
        with self._lock:
            self.online_map.clear()
        with contextlib.suppress(Exception):
            await self.async_request("getVersion", "getVersionRes", timeout=5.0)
        await self._rearm_sensors()
        await self._refresh_groups_presence()
        self._dispatch_conn()   # перерисовать available сущностей с очищенной online_map

    async def _refresh_groups_presence(self) -> None:
        """Перечитать группы с контроллера (getGroup — не скан шины) и обновить
        доступность их живых сущностей (после оффлайна present был False)."""
        try:
            groups = await self.async_load_groups()
        except Exception:  # noqa: BLE001
            return
        present = {(g["channel"], g["groupId"]) for g in groups if g.get("present")}
        for (ch, gid), ent in list(self._group_entities.items()):
            new = (ch, gid) in present
            if getattr(ent, "_present", None) != new:
                ent._present = new
                with contextlib.suppress(Exception):
                    ent.async_write_ha_state()

    async def _migrate_names_to_devsn(self) -> None:
        """Перенести имя с АДРЕСНОГО ключа на ключ `devSn`, когда серийник появился (v1.2.50).

        Пока скан терял `devSn` (см. `_dispatch`), имена ламп сохранялись по адресному ключу
        `<gw>:<devType>:<ch>:<addr>`. Теперь серийник приходит, и `name_key` ищет имя по нему —
        без переноса пользовательские имена выглядели бы стёртыми. Переносим ТОЛЬКО когда по
        `devSn` ещё пусто (чужое имя не затираем) и devSn валиден.
        """
        from .store import get_name_store, legacy_name_key
        ns = get_name_store(self.hass)
        if not ns:
            return
        moved = 0
        for dev in self.devices_snapshot():
            sn = dev.get("devSn")
            if not is_valid_devsn(sn) or ns.get(str(sn)):
                continue
            addr_key = legacy_name_key(self.gw_sn, dev.get("devType"),
                                       dev.get("channel"), dev.get("address"))
            old = ns.get(addr_key)
            if not old:
                continue
            await ns.async_set(str(sn), old)     # имя → на идентичность
            await ns.async_set(addr_key, "")     # адресный ключ убираем (пустое = удаление)
            moved += 1
            _LOGGER.info("шлюз %s: имя «%s» перенесено с адресного ключа %s на devSn %s",
                         self.gw_sn, old, addr_key, sn)
        if moved:
            self._log("scan", f"перенесено имён на серийники: {moved}")

    async def async_rearm_sensors(self) -> None:
        """Публичная обёртка перевзвода датчиков — её зовёт setup записи (v1.2.49).

        Раньше `__init__` держал СВОЮ копию активации, которая будила и выключенные вручную
        датчики: решение v1.2.23 «не будить выключённое» доехало до одного пути из двух.
        Одна реализация — чтобы они снова не разъехались."""
        await self._rearm_sensors()

    async def _rearm_sensors(self) -> None:
        """Включить датчики (`setSensorOnOff`), кроме выключенных ВРУЧНУЮ.

        Люкс (0202) сам не рапортует — без этой команды он молчит (движение 0201 работает и без
        неё; отсюда асимметрия «люкс погас, движение нет»). Зовётся на реконнекте И после
        физического скана (Fix K) — на скане адреса свежие, иначе команды уходят по старым.
        """
        with self._lock:                         # снимок: reader-поток мутирует self.devices
            items = list(self.devices.items())
        for key, dev in items:
            if not str(dev.get("devType")).startswith("02"):
                continue
            # Fix E (v1.1.2): ЗОМБИ не расталкиваем. Раньше setSensorOnOff уходил и снятым с шины
            # датчикам — мы сами на каждом коннекте напоминали шлюзу об их существовании, из-за
            # чего мёртвый датчик держался в кеше шлюза (а панель, которую никто не трогает, из
            # кеша выпадала — отсюда асимметрия «панель зомби, датчик воскрес»).
            if dev.get("zombie"):
                continue
            if not self.sensor_active.get(self._sensor_pref_key(dev, key), True):
                self._log("sensor", f"датчик addr{dev.get('address')} выключен вручную — "
                          "не активирую", devType=dev.get("devType"), address=dev.get("address"))
                continue
            with contextlib.suppress(Exception):
                await self.async_request(
                    "setSensorOnOff", "setSensorOnOffRes", value=True,
                    devType=dev["devType"], channel=dev["channel"], address=dev["address"])

    @staticmethod
    def _sensor_pref_key(dev: dict, addr_key: str) -> str:
        """Ключ ПРЕДПОЧТЕНИЯ активности датчика — по ИДЕНТИЧНОСТИ (`devSn:devType`), Fix L.

        Раньше ключом был АДРЕС → при перенумерации предпочтение протухало: выключенный вручную
        датчик тихо включался обратно, а его «выключено» наследовало ЧУЖОЕ устройство, которому
        достался этот адрес. Фолбэк на адресный ключ — только если devSn невалиден."""
        sn = dev.get("devSn")
        return f"{sn}:{dev.get('devType')}" if is_valid_devsn(sn) else addr_key

    def sensor_pref_key(self, dev: dict, addr_key: str) -> str:
        """Публичная обёртка `_sensor_pref_key` (зовёт switch-сущность)."""
        return self._sensor_pref_key(dev, addr_key)

    def set_sensor_active(self, key: str, value: bool, *, persist: bool = False) -> None:
        """Зафиксировать желаемую активность датчика (зовёт switch при ручном вкл/выкл).
        `key` — ключ ИДЕНТИЧНОСТИ (`sensor_pref_key`), не адрес (Fix L).

        `persist=True` — РУЧНОЕ решение человека, оно должно пережить рестарт HA (v1.2.23,
        решение пользователя «не будить выключённое — это вредно»). Дефолтные взводы при
        создании сущности персист НЕ трогают, иначе затрут выбор человека своим `True`."""
        self.sensor_active[key] = value
        if persist:
            # ⚠ v1.2.51: в ПЕРСИСТ пишем только ключ ИДЕНТИЧНОСТИ (`devSn:devType`). Адресный
            # ключ пережил бы само устройство и достался бы преемнику по адресу — ровно тот
            # класс отказа, что вернул имя `l_2_2_2`. В памяти адресный ключ допустим: он
            # живёт до рестарта и ни на что не претендует.
            if not is_valid_devsn(str(key).split(":")[0]):
                _LOGGER.debug("предпочтение датчика %s не персистим — ключ не по devSn", key)
                return
            from .store import get_sensor_pref_store
            sp = get_sensor_pref_store(self.hass)
            if sp:
                self.hass.async_create_task(sp.async_set(key, value))

    def load_sensor_prefs(self) -> None:
        """Поднять ручные предпочтения активности датчиков из персиста (v1.2.23).

        Зовётся до `_rearm_sensors`: иначе перевзвод включит датчик, который пусконаладчик
        осознанно выключил (в памяти-то пусто после рестарта)."""
        from .store import get_sensor_pref_store
        sp = get_sensor_pref_store(self.hass)
        if not sp:
            return
        prefs = sp.all()
        if prefs:
            self.sensor_active.update(prefs)
            off = [k for k, v in prefs.items() if not v]
            if off:
                _LOGGER.info("шлюз %s: из персиста подняты выключенные ВРУЧНУЮ датчики (%d) — "
                             "перевзвод их не тронет: %s", self.gw_sn, len(off), off)

    async def _watchdog(self) -> None:
        """Объект работает автономно: пока связь не online — периодически пробуем
        восстановить сами (targeted re-discovery: свежие креды/IP того же SN +
        пересборка MQTT-сессии). БЕЗ скана шины. Ручного запуска не ждём.

        Период с джиттером (рассинхрон залпов multicast при многих шлюзах) и backoff
        при затяжном offline (растёт до потолка, сбрасывается при восстановлении)."""
        backoff = _WATCHDOG_PERIOD
        try:
            while self._running:
                # джиттер: разброс, чтобы N сторожей не слали multicast синхронно
                await asyncio.sleep(backoff + random.uniform(0, _WATCHDOG_JITTER))
                if not self._running:
                    break
                if self.state == ST_OFFLINE and not self._rebuilding:
                    await self._attempt_rebuild()
                    if self.state == ST_OFFLINE:
                        # не вышло → растим паузу до потолка (затяжной offline не молотит)
                        backoff = min(backoff * 2, _WATCHDOG_MAX_PERIOD)
                    else:
                        backoff = _WATCHDOG_PERIOD   # восстановились → сброс
                else:
                    backoff = _WATCHDOG_PERIOD       # online/идёт rebuild → базовый
        except asyncio.CancelledError:
            pass

    async def _attempt_rebuild(self) -> None:
        self._rebuilding = True
        self.state = ST_REAUTH
        self._dispatch_conn()
        self._log("conn", f"шлюз {self.gw_sn}: авто-восстановление связи "
                  "(re-discovery, без скана шины)…", level="warn")
        ok = False
        try:
            from .discovery import get_connect_semaphore, get_discovery
            # ОБЩИЙ залп discovery (дедуп): после массового offline пачка сторожей берёт
            # результат ОДНОГО залпа, а не плодит десятки блокирующих поисков по 12с (что
            # исчерпывало пул потоков HA). gw=None → точечный fallback внутри _reconnect_session.
            gw = await get_discovery(self.hass).get(self.bind_ip, self.gw_sn, max_age=5.0)
            # коннект — через общий семафор (не больше N одновременно)
            async with get_connect_semaphore(self.hass):
                ok = await self.hass.async_add_executor_job(self._reconnect_session, gw)
        except Exception as err:  # noqa: BLE001
            self._log("conn", f"шлюз {self.gw_sn}: ошибка реконнекта: {err}", level="error")
        finally:
            # ВАЖНО: всегда сбрасываем _rebuilding и НЕ залипаем в ST_REAUTH — иначе при
            # отмене/исключении посреди await сторож навсегда пропускал бы попытки (условие
            # `state==OFFLINE and not _rebuilding`). finally отрабатывает и на CancelledError.
            if not ok and self.state != ST_ONLINE:
                self.state = ST_OFFLINE
                self._log("conn", f"шлюз {self.gw_sn}: не удалось — повтор по расписанию "
                          "сторожа", level="warn")
                self._dispatch_conn()
            self._rebuilding = False

    def _reconnect_session(self, gw: dict | None) -> bool:
        """Пересборка MQTT-сессии на ДАННЫЙ gw (discovery — ТОЛЬКО через общий диспетчер).
        gw=None (шлюза нет в общем залпе → offline) → неудача, watchdog повторит следующим
        циклом (по TTL диспетчер сделает свежий залп). Возвращает успех. Под семафором коннектов.

        Свой точечный discover_gateway здесь НЕ открываем (был регресс v0.72): индивидуальный
        сокет на 50569 при массовом offline воскрешал воровство multicast-ответов + держал
        слот connect-семафора блокирующим 12с-поиском."""
        if not gw:
            self._log("conn", f"шлюз {self.gw_sn}: нет в общем залпе discovery — offline, "
                      "watchdog повторит через общий залп", level="warn")
            return False
        old = self.session
        sess = GatewaySession(gw)
        sess.on_state = lambda st, s=sess: self._on_session_state(st, s)  # привязка к ЭТОЙ сессии
        try:
            new = sess.connect(timeout=10.0)
        except Exception as err:  # noqa: BLE001
            self._log("conn", f"шлюз {self.gw_sn}: MQTT-реконнект не удался: {err}", level="error")
            with contextlib.suppress(Exception):
                sess.close()
            return False
        if not self._running:        # disconnect/unload пока шли — не плодим зомби-сессию
            with contextlib.suppress(Exception):
                sess.close()
            return False
        self.gw = gw
        self.session = new            # старый читатель увидит смену и выйдет
        self._wake_pending()          # ответы старой сессии уже не придут — разбудить ждущих
        self._start_reader(new)
        with contextlib.suppress(Exception):
            if old:
                old.close()           # глушим старый paho-loop (старый IP/креды)
        self._log("conn", f"шлюз {self.gw_sn}: сессия пересобрана (ip={gw.get('gwIp')})")
        return True

    # ── список устройств шины (кеш шлюза) ────────────────────────────────────
    def _load_devices_blocking(self) -> dict[str, dict]:
        """Снять кеш устройств шлюза (searchDev exited) → self.devices."""
        self._search_buf = {}
        self._search_overall = False
        self._search_done.clear()
        self._search_active = True
        try:
            self.session.send("searchDev", searchFlag="exited")
            self._search_done.wait(20.0)
            with self._lock:                 # копия под локом (reader пишет _search_buf под ним)
                found = dict(self._search_buf)
        finally:
            self._search_active = False
        ignored = 0
        with self._lock:
            for d in found.values():
                k = dev_state_key(d.get("devType"), d.get("channel"), d.get("address"))
                e = self.devices.get(k)
                # ⚠ v1.2.14 — КЕШ ШЛЮЗА БОЛЬШЕ НЕ СОЗДАЁТ УСТРОЙСТВА.
                # `searchDev exited` — это ПАМЯТЬ контроллера «кого я когда-либо знал», а не опрос
                # шины. Раньше мы принимали её за реальность и заводили по ней записи → давно снятые
                # устройства («древние лампы») всплывали при каждом подключении, а мы потом городили
                # валидацию, чтобы отличить их от живых. Источник СУЩЕСТВОВАНИЯ теперь ровно два:
                #   • наш персист (`load_persisted` — до всякой связи, переживает рестарт HA);
                #   • ФИЗИЧЕСКИЙ скан `busDevice` (единственная истина о шине).
                # Кеш здесь только ОСВЕЖАЕТ поля устройства, которое мы и так знаем.
                # Новый шлюз → пусто до первого скана. Это осознанно (решение пользователя):
                # список устройств набирается сканом руками, а не памятью контроллера.
                if e is None:
                    ignored += 1
                    continue
                # мусорный devSn НЕ затирает ранее известный валидный (защита идентичности)
                new_sn = d.get("devSn", "")
                keep_sn = new_sn if is_valid_devsn(new_sn) else (e.get("devSn") or new_sn)
                # Z1: запись без валидного devSn — фантом (напр. 0300 с devSn='') → не трогаем
                if not is_valid_devsn(keep_sn):
                    _LOGGER.debug("шлюз %s: пропуск фантома %s (devSn=%r)", self.gw_sn, k, new_sn)
                    continue
                e.update({                       # in-place: e — та же запись, что в self.devices
                    "devType": d.get("devType"), "channel": d.get("channel"),
                    "address": d.get("address"), "name": d.get("name", ""),
                    "devSn": keep_sn, "status": d.get("status", ""),
                })
            # RE-LINK при загрузке кеша (тот же принцип, что в скане): если устройство
            # (devSn, devType) шлюз сейчас видит на НОВОМ адресе, а из персиста поднялась
            # его СТАРАЯ адресная координата — убираем старую. Так дубли, накопленные до
            # фикса (перераздача адресов), схлопываются на первом же старте с живой связью,
            # а не дают коллизию unique_id. Пара с devType — пары 0201/0202 не трогаем.
            found_keys = {dev_state_key(d.get("devType"), d.get("channel"), d.get("address"))
                          for d in found.values()}
            live_ids = {(self.devices[k].get("devSn"), str(self.devices[k].get("devType")))
                        for k in found_keys
                        if k in self.devices and is_valid_devsn(self.devices[k].get("devSn"))}
            for k in list(self.devices.keys()):
                if k in found_keys:
                    continue
                e = self.devices[k]
                ident = (e.get("devSn"), str(e.get("devType")))
                if is_valid_devsn(e.get("devSn")) and ident in live_ids:
                    self.devices.pop(k, None)
                    self.online_map.pop(k, None)   # online_map АДРЕСНЫЙ → чистим по адресу
                    # sensor_active НЕ трогаем: он ключуется ИДЕНТИЧНОСТЬЮ (Fix L), а устройство
                    # ПЕРЕЕХАЛО — его предпочтение «выключен» обязано переехать вместе с ним.
                    _LOGGER.info("шлюз %s: re-link при загрузке — убрана старая координата %s "
                                 "(devSn %s переехал)", self.gw_sn, k, e.get("devSn"))
        _LOGGER.info("шлюз %s: кеш шлюза вернул %d, освежено известных %d, "
                     "проигнорировано незнакомых %d (кеш устройств не создаёт — только скан)",
                     self.gw_sn, len(found), len(found) - ignored, ignored)
        return self.devices

    async def async_load_devices(self) -> dict[str, dict]:
        """Освежить поля известных устройств из кеша шлюза + сохранить персист.

        ⚠ v1.2.14: Z2-«отбор» devSn у других шлюзов (`async_claim`) ОТСЮДА УБРАН. Он строился из
        `self.devices` (персист ∪ кеш) — то есть по ПАМЯТИ: шлюз, который лишь ПОМНИЛ устройство,
        отбирал его у того, где оно физически стоит, и вычищал из RAM чужого хаба → `devStatus`
        отбрасывался как «неизвестное устройство», состояние сущности замирало. Исход зависел от
        порядка загрузки записей (кто последний — тот и отобрал) → плавало между рестартами.
        Владение теперь заявляет ТОЛЬКО физический скан (см. `async_scan`)."""
        devs = await self.hass.async_add_executor_job(self._load_devices_blocking)
        # персист устройств (чтобы шлюз/сущности не пропадали оффлайн/при рестарте)
        from .store import get_device_store
        ds = get_device_store(self.hass)
        if ds and devs:
            await ds.async_replace(self.gw_sn, devs)
        return devs

    # ── DALI-группы с составом (для light-сущностей групп) ────────────────────
    async def async_load_groups(self) -> list[dict]:
        """Группы = (что есть на контроллере) ∪ (что сохранено у нас). Живые персистим;
        сохранённые, но отсутствующие на контроллере — оставляем как `present=False`
        (сущность станет недоступной, но НЕ удалится). Команды контроллера:
        getGroup(список) + readGroup(состав)."""
        from .store import get_cross_group_store, get_group_store
        gs = get_group_store(self.hass)
        # ⚠ КРОСС-ШЛЮЗОВЫЕ группы пропускаем: на КАЖДОМ участнике лежит их копия (тот же
        # channel:groupId), и без этого гейта на один и тот же свет появились бы ТРИ
        # сущности — одна кросс-групповая и по одной «обычной» на каждом шлюзе.
        # Владеет ими CrossGroupStore, а не GroupStore (модели разделены).
        xgs = get_cross_group_store(self.hass)
        skip = {(g.get("channel"), g.get("groupId"))
                for g in (xgs.for_gateway(self.gw_sn) if xgs else [])}
        # 1) снимок с контроллера. ДВЕ ФАЗЫ: список групп → составы ПАРАЛЛЕЛЬНО.
        # Раньше `readGroup` шёл строго по одному в цикле: 16 групп = 16 круговых задержек
        # подряд, и так на КАЖДОМ шлюзе при старте и на каждом реконнекте. Семафор нужен,
        # чтобы пачка не превратилась в шторм на шлюзе (upstream держит тот же лимит 3).
        live: dict[tuple, dict] = {}
        res = await self.async_request("getGroup", "getGroupRes", getFlag="exited", timeout=8.0)
        _LOGGER.info("шлюз %s: getGroup raw → %s", self.gw_sn, res)   # truth с контроллера
        wanted: list[tuple] = []          # [(channel, groupId, имя из getGroup)]
        for blk in (res or {}).get("group", []) or []:
            ch = blk.get("channel")
            for g in blk.get("data", []) or []:
                gid = g.get("groupId")
                if (ch, gid) in skip:            # копия кросс-группы — не наша забота
                    continue
                wanted.append((ch, gid, g.get("name") or ""))

        sem = asyncio.Semaphore(GROUP_READ_CONCURRENCY)

        async def _read_members(ch, gid):
            async with sem:
                return await self.async_request("readGroup", "readGroupRes",
                                                channel=ch, groupId=gid, timeout=8.0)

        # return_exceptions: одна упавшая группа не должна ронять загрузку остальных —
        # она просто останется без живого состава (её подхватит персист ниже).
        reads = await asyncio.gather(*[_read_members(ch, gid) for ch, gid, _ in wanted],
                                     return_exceptions=True)
        for (ch, gid, name), rr in zip(wanted, reads):
            if isinstance(rr, BaseException):
                _LOGGER.warning("шлюз %s: readGroup ch%s id%s упал: %s — состав возьмём "
                                "из персиста", self.gw_sn, ch, gid, rr)
                continue
            _LOGGER.info("шлюз %s: readGroup ch%s id%s raw → %s",
                         self.gw_sn, ch, gid, rr)   # фактический состав группы
            members = [{"devType": str(m.get("devType")), "channel": m.get("channel"),
                        "address": m.get("address")}
                       for m in (rr or {}).get("data", []) or []]
            live[(ch, gid)] = {"channel": ch, "groupId": gid,
                               "name": name, "members": members}
        # 2) персистим живые (источник правды переживает рестарт/обрыв)
        if gs:
            for g in live.values():
                await gs.async_upsert(self.gw_sn, g)
        # 3) слияние с сохранёнными
        stored = {(g["channel"], g["groupId"]): g for g in (gs.all(self.gw_sn) if gs else [])
                  if (g["channel"], g["groupId"]) not in skip}
        merged: list[dict] = []
        for key in set(live) | set(stored):
            base = live.get(key) or stored.get(key)
            merged.append({
                "channel": base["channel"], "groupId": base["groupId"],
                "name": base.get("name") or "",
                "members": (live.get(key) or stored.get(key)).get("members", []),
                "present": key in live,   # есть ли сейчас на контроллере
            })
        self.groups = merged
        # синхронизировать УЖЕ живые сущности групп с актуальным present (на реконнекте он
        # мог смениться False→True; раньше _present залипал на момент создания → «не на связи»);
        # создать недостающие сущности для появившихся групп. На ПЕРВИЧНОМ setup _light_adder
        # ещё не задан/сущностей нет → no-op, их создаст платформа light из обновлённого self.groups.
        for g in merged:
            ent = self._group_entities.get((g["channel"], g["groupId"]))
            if ent is not None:
                ent.update_present(g["present"])
            elif g["present"] and self._light_adder:
                self.add_group_entity(g)
        _LOGGER.info("шлюз %s: групп %d (на контроллере %d)",
                     self.gw_sn, len(merged), len(live))
        return merged

    def set_light_adder(self, fn) -> None:
        """Платформа light регистрирует свой async_add_entities для дин. добавления групп."""
        self._light_adder = fn

    def add_group_entity(self, group: dict) -> None:
        """Добавить light-сущность для новой группы (после create_group). Имя — с
        контроллера (в group['name']); NameStore для групп не используем."""
        if self._light_adder:
            from .light import DaliGroupLight  # отложенный импорт (избегаем цикла)
            self._light_adder([DaliGroupLight(self, group)])

    def add_cross_group_entity(self, xgroup: dict) -> bool:
        """Добавить сущность КРОСС-шлюзовой группы сразу после создания (v1.2.43).

        Раньше её создавал только `async_setup_entry` при старте платформы — то есть до
        рестарта HA группа была на шине, а сущности в HA не было. У обычных групп этого нет
        (`add_group_entity` зовётся из `ws_create_group`), и расхождение было чисто нашим
        недосмотром. Зовётся на ЯКОРЕ — алфавитно первом участнике (тот же выбор, что при
        старте, иначе после рестарта появился бы дубль сущности с другим владельцем)."""
        if not self._light_adder:
            return False
        from .light import DaliCrossGroupLight        # отложенный импорт (цикл)
        self._light_adder([DaliCrossGroupLight(self.hass, self, xgroup)])
        return True

    def cross_group_entity(self, uid: str):
        """Живая сущность кросс-группы по её `uid` (для правки состава и удаления)."""
        for ent in self.hass.data.get(XGROUP_ENTITIES, ()):
            if getattr(ent, "unique_id", None) == uid:
                return ent
        return None

    # ── трекинг ЖИВЫХ сущностей групп ───────────────────────────────────────
    # Снос только записи реестра бесполезен: пока жива сама сущность (с unique_id),
    # HA пересоздаёт запись со старым entity_id. Поэтому удаляем/пересоздаём СУЩНОСТЬ.
    # ── трекинг unique_id живых шинных сущностей (light/motion/lux/event) ─────
    # Сущности регистрируют себя при добавлении; карточка через _entities() резолвит
    # entity_id по (role, dev_state_key), а не по devSn — устойчиво к порче devSn.
    def register_entity_uid(self, role: str, key: str, unique_id: str) -> None:
        self._entity_uids[(role, key)] = unique_id

    def unregister_entity_uid(self, role: str, key: str, unique_id: str | None = None) -> None:
        if unique_id is None or self._entity_uids.get((role, key)) == unique_id:
            self._entity_uids.pop((role, key), None)

    def entity_uid(self, role: str, key: str) -> str | None:
        return self._entity_uids.get((role, key))

    def devices_snapshot_map(self) -> dict[str, dict]:
        """Снимок кеша устройств КЛЮЧ→запись под локом (reader-поток мутирует `devices`).
        Нужен сервисам (v1.2.24): им важен ключ, а не только значения `devices_snapshot()`."""
        with self._lock:
            return {k: dict(v) for k, v in self.devices.items()}

    def entity_uids_for_key(self, key: str) -> list[str]:
        """Все unique_id сущностей одной координаты (у датчика их несколько: ms_/il_/тумблер)."""
        return [uid for (_role, k), uid in self._entity_uids.items() if k == key]

    def bus_entity(self, unique_id: str):
        """Живой объект шинной сущности по unique_id (у него есть entity_id)."""
        return self._bus_entities.get(unique_id)

    def live_entity(self, role: str, key: str):
        """Живой объект сущности по (role, key=devType:ch:addr) — для распространения
        состояния группа→лампы (G1). None, если сущность не на связи/не создана."""
        uid = self._entity_uids.get((role, key))
        return self._bus_entities.get(uid) if uid else None

    # ── динамика сущностей: реестр платформ + reconcile (Фаза D) ──────────────
    def register_platform(self, platform: str, adder, factory) -> None:
        """Платформа отдаёт свой async_add_entities + factory(dev)->entity (с именем)."""
        self._platforms[platform] = (adder, factory)

    def register_bus_entity(self, role: str, unique_id: str, entity) -> None:
        self._bus_entities[unique_id] = entity

    def unregister_bus_entity(self, role: str, unique_id: str, entity=None) -> None:
        if entity is None or self._bus_entities.get(unique_id) is entity:
            self._bus_entities.pop(unique_id, None)

    def _roles_for_dev(self, dev: dict) -> list[tuple]:
        """Какие сущности ДОЛЖНЫ существовать для устройства: [(role, platform, unique_id)].
        unique_id строится по devSn (стабилен) — совпадает с тем, что задают сами сущности."""
        t = str(dev.get("devType")); ch = dev.get("channel"); addr = dev.get("address")
        sn = dev.get("devSn")
        base = sn or f"{self.gw_sn}:{ch}:{addr}"
        if t in _LIGHT_TYPES:
            return [("light", "light", sn or f"{self.gw_sn}:{t}:{ch}:{addr}")]
        if t == "0201":
            return [("motion", "sensor", f"{base}_motion"),
                    ("active_0201", "switch", f"{base}_active_0201")]
        if t == "0202":
            return [("lux", "sensor", f"{base}_lux"),
                    ("active_0202", "switch", f"{base}_active_0202")]
        if t.startswith("03"):
            return [("event", "event", f"{base}_event")]
        return []

    def async_reconcile(self) -> None:
        """Привести живые сущности в соответствие с кешем устройств (после скана):
        добавить новые, обновить адрес существующих (ре-нумерация), пометить ушедшие,
        перепривязать «переехавшие» с другого шлюза. Зовётся в петле HA."""
        from homeassistant.helpers import entity_registry as er
        reg = er.async_get(self.hass)
        # (v1.1.7: маппинг platform→domain был тождественным словарём в ТРЁХ местах —
        #  `platform` из `_roles_for_dev` И ЕСТЬ домен HA. Убран.)
        desired: dict[str, tuple] = {}   # unique_id → (role, platform, dev)
        with self._lock:                 # снимок: reader-поток мутирует self.devices
            devs = list(self.devices.values())
        for dev in devs:
            for role, platform, uid in self._roles_for_dev(dev):
                prev = desired.get(uid)
                # остаточный дубль (два dev дают один uid): предпочитаем ЖИВУЮ запись,
                # чтобы сущность не осталась помечена gone своим зомби-двойником (порядок
                # в devices не гарантирован → без этого «побеждала» последняя, часто зомби)
                if prev is not None and dev.get("zombie") and not prev[2].get("zombie"):
                    continue
                desired[uid] = (role, platform, dev)
        added = updated = rehomed = 0
        for uid, (role, platform, dev) in desired.items():
            ent = self._bus_entities.get(uid)
            if ent is not None:
                if dev.get("zombie"):
                    ent.mark_gone()                  # не найден сканом → красный (запись цела)
                else:
                    before = (ent._channel, ent._address)
                    ent.update_from_dev(dev)         # найден → живой (update_from_dev снимает gone)
                    moved_addr = (ent._channel, ent._address) != before
                    if moved_addr:
                        updated += 1
                    # v1.2.7: у БЕЗЫМЯННОГО entity_id = `<тип>_<адрес>_<sn5>` → при смене АДРЕСА
                    # он обязан следовать (rename=True: новый id + сброс подписи). ИМЕНОВАННЫЕ не
                    # трогаем: их id = slug(имя), от адреса не зависит (на них висят автоматизации).
                    # Имя УСТРОЙСТВА больше НЕ синхронизируем (Fix V удалён): оно по devSn и
                    # стабильно навсегда. Переезд между ШЛЮЗАМИ id не меняет (шлюза в имени нет).
                    if moved_addr and not self.has_custom_name(dev):
                        self._track_task(self._force_entity_id(
                            role, platform, uid, dev, created=False, rename=True))
                    else:
                        # Fix J: вернуть «свой» entity_id, если HA приписал автосуффикс `_2`
                        # (желаемый занял фантом/вытесненная идентичность). Гейт внутри — дешёвый.
                        self._maybe_reclaim_entity_id(role, platform, uid, dev)
                continue
            if dev.get("zombie"):
                continue                             # зомби без живой сущности — не создаём
            reg_pair = self._platforms.get(platform)
            if not reg_pair:
                continue
            adder, factory = reg_pair
            old_eid = reg.async_get_entity_id(platform, DOMAIN, uid)
            if old_eid:
                rec = reg.async_get(old_eid)
                foreign = rec is not None and rec.config_entry_id != self.entry_id
                # 🔴 v1.2.48. НАША запись в реестре ≠ живая сущность. Прежний код здесь
                # выходил (`continue`), считая, что раз запись наша — объект существует.
                # Это ломало сценарий с объекта (2026-08-06): «Стереть данные» чистит
                # DeviceStore → после рестарта HA платформам создавать НЕ ИЗ ЧЕГО, и записи
                # висят осиротевшими (HA держит их сам: `state=unavailable`, атрибут
                # `restored`, надпись «объект больше не предоставляется интеграцией»).
                # Скан находил устройство, а `reconcile` молча пропускал создание → датчик
                # оставался «не на связи», и лечил это только ВТОРОЙ рестарт HA.
                # Теперь: объекта нет (мы уже в ветке `ent is None`) и состояние либо
                # отсутствует, либо `restored` → СОЗДАЁМ. HA подхватит существующую запись
                # по `unique_id`, поэтому `entity_id` и история сохранятся.
                st = self.hass.states.get(old_eid)
                stale = st is None or st.attributes.get("restored")
                if not foreign and stale:
                    _LOGGER.info("шлюз %s: запись сущности %s есть в реестре, а живого "
                                 "объекта нет — создаю заново (был осиротевшим)",
                                 self.gw_sn, old_eid)
                # перепривязка: сущность под ДРУГОЙ записью, которая этот devSn уже не
                # видит → снять старую запись и создать под нашей. Иначе (наша запись с
                # ЖИВОЙ сущностью / живой чужой владелец) — НЕ трогаем.
                elif foreign and self._foreign_owner_lost(uid):
                    old_entry_id = rec.config_entry_id      # запомнить ДО сноса (для M3)
                    reg.async_remove(old_eid)
                    rehomed += 1
                    # M3 (v1.2.8): снять со ЗАПИСИ УСТРОЙСТВА старую ConfigEntry. Иначе устройство
                    # числится за ОБОИМИ шлюзами: новая сущность подцепляет ту же запись
                    # `device_registry` (identifiers=devSn) и добавляет НАШУ запись, а старая
                    # ссылка не убирается (её чистит только `_cleanup_foreign_devices` на рестарте).
                    self._drop_stale_config_entry(dev, old_entry_id)
                    self._log("scan", f"перепривязка {uid} → шлюз {self.gw_sn} ({old_eid})")
                else:
                    continue
            new_ent = factory(dev)
            if new_ent is not None:
                adder([new_ent])
                added += 1
                # E2.2 + Fix J (v1.1.4): при ПЕРВОМ появлении устройства задать
                # entity_id = slug(имя), чтобы имя и entity_id не расходились. Раньше — только
                # для ЛАМП; теперь для ВСЕХ ролей (датчик/панель/switch тоже страдали от
                # коллизий: фантом занимал id, настоящий получал `..._2`).
                # Существующие (update-путь выше) — только возврат автосуффикса, ручные не трогаем.
                self._track_task(self._force_entity_id(role, platform, uid, dev, created=True))
        for uid, ent in list(self._bus_entities.items()):
            if uid not in desired:
                ent.mark_gone()
        # S1 (v1.2.3): сущности ламп могли появиться/переехать → группам пересобрать подписки
        self.resubscribe_groups()
        if added or updated or rehomed:
            _LOGGER.info("шлюз %s reconcile: +%d сущностей, адрес обновлён %d, перепривязано %d",
                         self.gw_sn, added, updated, rehomed)

    def has_custom_name(self, dev: dict) -> bool:
        """Есть ли у устройства ПОЛЬЗОВАТЕЛЬСКОЕ имя (NameStore, ключ devSn)?

        Различие принципиально: пользовательское имя — данные ЧЕЛОВЕКА, привязаны к devSn и от
        адреса не зависят (на них висят автоматизации: `ms_`/`il_`/`l_`/`kp_`) → при смене адреса
        НЕ трогаем. У БЕЗЫМЯННОГО entity_id производен от адреса → обязан следовать за ним.
        (v1.2.7: имя УСТРОЙСТВА за адресом больше не следует — оно по devSn; этот гейт остался
        только для entity_id сущностей.)
        """
        from .store import get_name_store, name_key
        ns = get_name_store(self.hass)
        if not ns:
            return False
        return bool(ns.get(name_key(self.gw_sn, dev.get("devType"), dev.get("channel"),
                                    dev.get("address"), dev.get("devSn"))))

    # v1.2.18 (B4): `live_devsns` / `_live_devsn_set` / `_identity_is_live` УДАЛЕНЫ вместе с веткой
    # отбирания entity_id в `_force_entity_id` — осевшая машинерия эпохи, когда id выводился из
    # адреса и устройства дрались за него. Fix W (v1.2.0) зашил в id собственный хвост devSn →
    # драки невозможны; защита осталась и ВРЕДИЛА (`split("_")[0]` группы/безсерийного = серийник
    # шлюза → «мёртв» → снос живого). См. `_force_entity_id`.

    def _desired_entity_id(self, role: str, platform: str, dev: dict) -> tuple:
        """Желаемые (entity_id, подпись) роли. Подпись = None → «не задавать, следовать за id».

        ⚠ КЛЮЧЕВОЕ: именованные и БЕЗЫМЯННЫЕ именуются по РАЗНЫМ правилам, смешивать нельзя:
        - **есть имя** (продакшен, NameStore) → правило РЕНЕЙМА (`_rename_roles`): `ms_`/`il_` +
          `_act`; подпись ЗАДАЁМ (данные человека, точное написание важно);
        - **нет имени** → правило ДЕФОЛТА (`entity_name`, v1.2.7): `<тип>_<адрес>_<sn5>`, `…_active`
          (БЕЗ шлюза). Подпись — **None**: её выводит HA из entity_id, догонять нечего.

        Раньше (v1.1.6–1.2.6) безымянному тоже задавалась подпись, производная от адреса → её
        приходилось переименовывать вслед за адресом (Fix N), и она разъезжалась с id при переезде
        между шлюзами (M2). С v1.2.7 подписи у безымянного нет — класс дефектов снят.
        """
        from homeassistant.util import slugify

        from .naming import entity_name
        from .store import get_name_store, name_key
        ns = get_name_store(self.hass)
        custom = ns.get(name_key(self.gw_sn, dev.get("devType"), dev.get("channel"),
                                 dev.get("address"), dev.get("devSn"))) if ns else ""
        if custom:
            # отложенный импорт: websocket_api импортирует coordinator (цикл на уровне модуля)
            from .websocket_api import _rename_roles
            for _dom, r, _key, object_id, friendly in _rename_roles(self.gw_sn, dev, custom):
                if r == role:
                    oid = slugify(object_id)
                    return (f"{platform}.{oid}", friendly) if oid else (None, None)
            return (None, None)
        # БЕЗЫМЯННОЕ: entity_id по типу+адресу+sn5 (без шлюза); подпись НЕ задаём (None)
        base = entity_name(dev.get("devType"), dev.get("address"), dev.get("devSn") or "")
        oid = slugify(f"{base}_active" if role.startswith("active_") else base)
        return (f"{platform}.{oid}", None) if oid else (None, None)

    async def _force_entity_id(self, role: str, platform: str, uid: str, dev: dict,
                               created: bool = True, rename: bool = False) -> None:
        """Держать entity_id = slug(имя) — для ЛЮБОЙ роли (лампа/датчик/панель/switch).

        Fix J (v1.1.4). Раньше форс был ТОЛЬКО для ламп и ТОЛЬКО если желаемый id свободен;
        иначе HA молча оставлял автосуффикс `..._2`, и это никем не лечилось.

        v1.2.18 (B4): если желаемый id держит ДРУГАЯ сущность — НЕ ОТБИРАЕМ (раньше был путь
        Fix M `_identity_is_live` → `reg.async_remove`, снят: причину драки за id убрал Fix W,
        а «мёртв ли держатель» всегда ошибалась для групп/безсерийных → сносила живое). Занят
        желаемый → оставляем `_2`, WARNING, разбирается человек. Своего мёртвого сироту при этом
        по-прежнему НЕ трогаем специально — его просто нет (id разведены хвостом devSn).

        `created=False` — существующая сущность: вмешиваемся только в автосуффикс `<желаемый>_N`
        (ручные entity_id не переименовываем, решение E2.2), если не задан `rename`.
        `rename=True` (v1.2.7) — адрес сменился у БЕЗЫМЯННОГО устройства: entity_id производен от
        адреса → обновляем id + СБРАСЫВАЕМ подпись (friendly=None), чтобы HA вывел её из нового id.
        """
        from homeassistant.helpers import entity_registry as er
        desired, friendly = self._desired_entity_id(role, platform, dev)
        if not desired:
            return
        reg = er.async_get(self.hass)
        for attempt in range(30):                # ждём появления сущности (до ~3с)
            eid = reg.async_get_entity_id(platform, DOMAIN, uid)
            if not eid:
                if attempt == 29:                # сдались молча → на объекте это невидимая потеря
                    self._log("scan", f"{role} {uid}: сущность не зарегистрировалась за 3с — "
                              f"entity_id НЕ форсирован (хотел {desired})", level="warn")
                await asyncio.sleep(0.1)
                continue
            if eid == desired:
                return
            # существующая сущность: только АВТОСУФФИКС ЖЕЛАЕМОГО id (`<desired>_2`);
            # ручные entity_id не переименовываем (E2.2) — см. is_auto_suffix()
            if not created and not rename and not is_auto_suffix(eid, desired):
                return
            holder = reg.async_get(desired)
            if holder is not None and holder.unique_id != uid:
                # v1.2.18 (B4): желаемый id занят ДРУГОЙ сущностью → НИЧЕГО НЕ ОТБИРАЕМ, оставляем
                # автосуффикс `_2`, разбирается ЧЕЛОВЕК по журналу. Раньше здесь был деструктивный
                # путь (`_identity_is_live` → `reg.async_remove`), защищавший от драки за entity_id.
                # Причину драки убрал Fix W (v1.2.0): в id зашит собственный хвост devSn → занять
                # желаемое может только само устройство (у именованных v0.90 даёт отказ `duplicate`).
                # Ветка стала осевшей машинерией, как `_purge_identity` (снят v1.2.2), и при этом её
                # «мёртв ли держатель» ВСЕГДА ошибалась для ГРУПП (uid=`gw_group_…`) и устройств без
                # devSn (uid=`gw:ch:addr_…`): `split("_")[0]` = серийник шлюза, которого в множестве
                # живых нет → «мёртв» → снос ЖИВОЙ группы/сущности в корзину HA (Закон 1). Убрано.
                self._log("scan", f"{role} {uid}: entity_id {desired} занят другой сущностью "
                          f"(uid={holder.unique_id}) — оставляю {eid}, разбирается вручную",
                          level="warn")
                return
            with contextlib.suppress(Exception):
                if rename:
                    # адрес сменился у БЕЗЫМЯННОГО → новый entity_id + СБРОС подписи (friendly=None
                    # для безымянного): HA заново выведет её из нового id (v1.2.7). Раньше здесь
                    # ставилась производная от адреса подпись (Fix N) — теперь подписи просто нет.
                    reg.async_update_entity(eid, new_entity_id=desired, name=friendly)
                else:
                    reg.async_update_entity(eid, new_entity_id=desired)
                self._log("scan", f"{role} {uid}: entity_id → {desired}")
                # S1: entity_id лампы сменился → подписки групп на него ПРОТУХЛИ (v1.2.3)
                if platform == "light":
                    self.resubscribe_groups()
            return

    def _maybe_reclaim_entity_id(self, role: str, platform: str, uid: str, dev: dict) -> None:
        """Гейт Fix J для СУЩЕСТВУЮЩИХ сущностей (зовётся из reconcile на каждое устройство):
        задачу заводим ТОЛЬКО если сущность реально сидит под автосуффиксом ЖЕЛАЕМОГО id
        (`<desired>_2`) — признак коллизии. Остальные (подавляющее большинство) не трогаем.

        ⚠ v1.1.7: раньше признаком было «eid кончается на `_<цифры>`» — под это правило подпадали
        ВСЕ дефолтные имена (суффикс шлюза `_8727`) и весь продакшен-нейминг (`l_2_5_13`), поэтому
        гейт пропускал почти каждое устройство и плодил тысячи задач на каждом скане.
        """
        from homeassistant.helpers import entity_registry as er
        reg = er.async_get(self.hass)
        eid = reg.async_get_entity_id(platform, DOMAIN, uid)
        if not eid:
            return
        desired, _friendly = self._desired_entity_id(role, platform, dev)
        if is_auto_suffix(eid, desired):
            self._track_task(self._force_entity_id(role, platform, uid, dev, created=False))

    def _foreign_owner_lost(self, unique_id: str) -> bool:
        """devSn = начало unique_id. True, если НИ ОДИН другой хаб уже не видит это устройство
        ЖИВЫМ (по devSn) → можно безопасно перепривязать сюда.

        ⚠ M1 (v1.2.8): проверяем ЖИВЫХ (`live_only=True`), а НЕ «есть в кеше вообще». Раньше
        `has_devsn` считала владельцем и ЗОМБИ — а зомби-запись мы принципиально не удаляем.
        Значит после переезда датчика на другой шлюз: старый шлюз его СКАНОМ не нашёл → пометил
        зомби → но по старой проверке всё ещё «владел» им → перепривязка на новый шлюз была
        НЕДОСТИЖИМА штатно (устройство висело «не на связи» под старым шлюзом навсегда).
        Теперь: не найден физическим сканом (зомби/осиротевший) → владельцем не считается.
        """
        # unique_id шинной сущности = devSn[+суффикс]; devSn — префикс до '_'
        # (v1.1.7: через `has_devsn` — БЕЗ копирования словарей, как делал devices_snapshot())
        sn = unique_id.split("_")[0]
        for hub in self.hass.data.get(DOMAIN, {}).values():
            if hub is self:
                continue
            if hub.has_devsn(sn, live_only=True):
                return False
        return True

    def has_devsn(self, devsn: str, live_only: bool = False) -> bool:
        """Есть ли устройство с таким devSn в кеше. Без копий словарей.

        `live_only=True` — только ЖИВЫЕ (не зомби и не осиротевшие): зомби-запись физически на
        шине не подтверждена, владельцем устройства она быть не может (M1, v1.2.8)."""
        with self._lock:
            return any(d.get("devSn") == devsn and (not live_only or not d.get("zombie"))
                       for d in self.devices.values())

    def _drop_stale_config_entry(self, dev: dict, old_entry_id: str) -> None:
        """M3 (v1.2.8): отвязать запись УСТРОЙСТВА (identifiers=devSn) от СТАРОЙ ConfigEntry
        при перепривязке на этот шлюз. Иначе устройство висит за обоими шлюзами до рестарта.

        Идемпотентно: если записи/привязки уже нет — no-op. HA пересобирает `config_entries`
        устройства по его сущностям, но старую ссылку сам снимет лишь когда УЙДЁТ последняя её
        сущность — здесь снимаем явно (сущности этой роли уже сняты выше)."""
        if not old_entry_id or old_entry_id == self.entry_id:
            return
        sn = dev.get("devSn")
        if not sn:
            return
        from homeassistant.helpers import device_registry as dr_reg
        reg = dr_reg.async_get(self.hass)
        entry = reg.async_get_device(identifiers={(DOMAIN, sn)})
        if entry and old_entry_id in entry.config_entries:
            with contextlib.suppress(Exception):
                reg.async_update_device(entry.id, remove_config_entry_id=old_entry_id)

    async def _remove_dev_entities(self, dev: dict) -> list[tuple[str, str]]:
        """Снести ВСЕ сущности устройства: живые объекты + записи реестра. Возврат — пары
        (unique_id, entity_id).

        ⚠ v1.2.2: зовётся ТОЛЬКО из «Забыть» — то есть по решению ЧЕЛОВЕКА. Второй звавший,
        `_purge_identity` (авто-снос вытесненной идентичности), удалён: вытесненные теперь
        становятся ОСИРОТЕВШИМИ (видны, красные), а сносит их человек. Авто-удаления в
        интеграции больше нет ни одного.
        `platform` из `_roles_for_dev` И ЕСТЬ домен HA — маппинг не нужен."""
        from homeassistant.helpers import entity_registry as er
        reg = er.async_get(self.hass)
        out: list[tuple[str, str]] = []
        for _role, platform, uid in self._roles_for_dev(dev):
            ent = self._bus_entities.get(uid)
            if ent is not None:                       # снести живую сущность
                with contextlib.suppress(Exception):
                    await ent.async_remove(force_remove=True)
            eid = reg.async_get_entity_id(platform, DOMAIN, uid)
            if eid:                                   # снести запись реестра (если осталась)
                with contextlib.suppress(Exception):
                    reg.async_remove(eid)
            out.append((uid, eid or ""))
        return out

    def devsn_shared_with_other_key(self, key: str, devsn: str) -> bool:
        """Есть ли ТА ЖЕ ПАРА (devSn, devType) на ДРУГОМ адресном ключе того же шлюза —
        т.е. живой дубль-двойник ТОГО ЖЕ устройства (после перераздачи адресов). Если да —
        «Забыть» этой координаты НЕ должно сносить сущности и чистить device-level сторы:
        unique_id/имя/параметры общие → снесём живого двойника.

        ⚠ Fix (v1.1.4): сравниваем ПАРУ (devSn, devType), а не один devSn — как это давно
        делают re-link и зомбирование. Один devSn на РАЗНЫХ devType — это НЕ двойник:
        - штатная пара датчика (0201 движение + 0202 люкс = одно физустройство, но unique_id
          у них РАЗНЫЕ: `_motion` / `_lux`) — снос одной координаты другую не заденет;
        - МИС-ЭНУМЕРАЦИЯ шлюза (датчик пришёл с серийником кнопки — наблюдалось при конфликте
          адресов). Сравнение по одному devSn делало такого фантома НЕУДАЛЯЕМЫМ: он
          «прикрывался» реальной кнопкой с тем же серийником, «Забыть» отказывалось сносить
          его сущности, и занятый им entity_id оставался занятым НАВСЕГДА.
        """
        if not is_valid_devsn(devsn):
            return False
        with self._lock:
            devtype = str((self.devices.get(key) or {}).get("devType"))
            return any(k != key and e.get("devSn") == devsn
                       and str(e.get("devType")) == devtype
                       for k, e in self.devices.items())

    def devsn_live_under_other_type(self, key: str, devsn: str) -> str | None:
        """Есть ли ЖИВАЯ запись с тем же `devSn`, но ДРУГОГО типа? Возвращает её ключ.

        🔴 v1.2.66 — защита от «перекрёста devSn» (docs/DEVSN_CROSSWIRE.md). Шлюз на одном
        адресе меняет серийники местами между `dali` и `dali2`, и в кеше оказываются ДВЕ
        записи с общим `devSn`: живой датчик `0201:0:0` и осиротевшая «лампа»
        `orphan:<тот же devSn>:0101`. Сущности у них разные (`_motion` против голого `devSn`),
        а вот КАРТОЧКА УСТРОЙСТВА (`identifiers = devSn`) и все device-level сторы (имя,
        параметры, предпочтения, энергия — ключ `devSn`) ОБЩИЕ. Поэтому «Забыть» осиротевшего
        сносило имя и карточку РАБОТАЮЩЕГО устройства (офис, 2026-08-11).

        ⚠ Отличие от `devsn_shared_with_other_key`: та ищет ту же ПАРУ (devSn, devType) —
        живого двойника после перенумерации. Здесь наоборот: тип ДРУГОЙ, и именно поэтому
        общее трогать нельзя. Зомби и осиротевшие за «живых» не считаем — они сами кандидаты
        на снос.
        """
        if not is_valid_devsn(devsn):
            return None
        with self._lock:
            mine = str((self.devices.get(key) or {}).get("devType"))
            for k, e in self.devices.items():
                if k == key or e.get("devSn") != devsn:
                    continue
                if str(e.get("devType")) == mine:
                    continue                      # тот же тип — это случай `devsn_shared…`
                if e.get("orphan") or e.get("zombie"):
                    continue                      # сам не жилец — не защитник
                return k
        return None

    def devsn_live_on_other_hub(self, devsn: str) -> bool:
        """M1 (v1.2.8): жив ли тот же `devSn` ЖИВЫМ на ДРУГОМ шлюзе (устройство переехало туда).

        Нужна «Забыть»: device-level сторы (имя/параметры/энергия) ключуются `devSn` и ОБЩИЕ для
        всех шлюзов. Если устройство переехало на другой шлюз, «Забыть» его зомби-координаты на
        СТАРОМ шлюзе НЕ должно чистить сторы — иначе живое устройство на новом шлюзе теряет имя и
        параметры. Раньше проверка (`devsn_shared_with_other_key`) смотрела только СВОЙ хаб."""
        if not is_valid_devsn(devsn):
            return False
        for hub in self.hass.data.get(DOMAIN, {}).values():
            if hub is not self and hub.has_devsn(devsn, live_only=True):
                return True
        return False

    # v1.2.18 (F3): `has_bus_confirmed_devsn` / `devsn_bus_confirmed_on_other_hub` УДАЛЕНЫ.
    # Их единственные вызовы («Забыть»/«Стереть») переведены на `devsn_live_on_other_hub`
    # (персист-знание, переживает рестарт). Сессионный флаг `bus_seen` остаётся — им пользуется
    # Z2-claim в `async_scan` (владение заявляет только тот, кто РЕАЛЬНО увидел устройство на шине).

    async def async_forget_device(self, key: str) -> list[str]:
        """Ручное «Забыть»: снести сущности устройства (живые + записи реестра) и убрать из
        кеша/online/sensor_active/персиста устройств. Param/Name-сторы чистит зовущий (по
        devSn). ЕДИНСТВЕННАЯ точка реального удаления (авто-удаления нет). Возврат — uid's.

        ЗАЩИТА ОТ ДУБЛЯ: если тот же devSn живёт на другом адресе (двойник после
        перераздачи адресов), сущности принадлежат ЖИВОМУ двойнику (общий unique_id) —
        их НЕ трогаем, убираем только ЭТУ устаревшую адресную координату (и её хвосты).
        Возврат [] сигналит зовущему, что device-level сторы (имя/параметры) чистить нельзя."""
        from homeassistant.helpers import entity_registry as er
        dev = self.devices.get(key)
        if not dev:
            return []
        if self.devsn_shared_with_other_key(key, dev.get("devSn")):
            with self._lock:
                self.devices.pop(key, None)
                self.online_map.pop(key, None)   # online_map АДРЕСНЫЙ → чистим по адресу
                # sensor_active НЕ трогаем: ключ — ИДЕНТИЧНОСТЬ (Fix L), а она принадлежит ЖИВОМУ
                # двойнику (убираем лишь его устаревшую адресную координату). Раньше здесь стоял
                # pop по адресному ключу — после Fix L это был гарантированный no-op, то есть в
                # одном методе жили ДВА разных ключа к одному словарю.
            from .store import get_device_store
            ds = get_device_store(self.hass)
            if ds:
                await ds.async_replace(self.gw_sn, dict(self.devices))
            self._log("scan", f"«Забыть»: убрана лишь дубль-координата {dev.get('devType')} "
                      f"addr{dev.get('address')} (devSn={dev.get('devSn')}) — живой двойник и "
                      "его имя целы", level="warn")
            return []
        removed = [uid for uid, _eid in await self._remove_dev_entities(dev)]
        with self._lock:
            self.devices.pop(key, None)
            self.online_map.pop(key, None)
            self.sensor_active.pop(self._sensor_pref_key(dev, key), None)   # Fix L: по identity
        from .store import get_device_store
        ds = get_device_store(self.hass)
        if ds:
            await ds.async_replace(self.gw_sn, dict(self.devices))
        self._log("scan", f"забыто устройство {dev.get('devType')} addr{dev.get('address')} "
                  f"(devSn={dev.get('devSn')}) — {len(removed)} сущностей снесено", level="warn")
        return removed

    @callback
    def resubscribe_groups(self) -> None:
        """S1 (v1.2.3): пересобрать подписки ГРУПП на их лампы-члены.

        Группы слушают лампы АДРЕСНО, по `entity_id` (штатный механизм HA вместо нашей
        широковещалки — см. `DaliGroupLight`). Значит после всего, что меняет состав или
        `entity_id` ламп, подписку надо пересобрать, иначе она висит на мёртвом id и группа
        молча перестаёт обновляться. Точки, где это происходит, ровно три: reconcile (создание
        и переезд сущностей), форс `entity_id` (перераздача адресов) и ренейм из WS.
        Дёшево: групп на шлюзе ≤16, ламп ≤64 (лимит адресов DALI)."""
        for ent in list(self._group_entities.values()):
            with contextlib.suppress(Exception):
                ent.resubscribe_members()
        # КРОСС-ШЛЮЗОВЫЕ группы (v1.2.42) — по той же причине и в те же три точки.
        # Реестр ОБЩИЙ (на hass), а не пошлюзный: сущность создаёт ЯКОРЬ, и на старте его
        # ConfigEntry поднимается РАНЬШЕ остальных — регистрируйся она по хабам, поздний
        # участник о ней бы не узнал и никогда её не пересобрал. Здесь каждый хаб забирает
        # из общего реестра те группы, в которых участвует САМ.
        # Без этого кросс-группа МОЛЧА не видела своих ламп: единственная подписка собиралась
        # до того, как поднялись лампы другого шлюза, и выходила пустой.
        me = self.gw_sn.upper()
        for ent in list(self.hass.data.get(XGROUP_ENTITIES, ())):
            parts = {str(p).upper() for p in getattr(ent, "participants", ())}
            if me in parts:
                with contextlib.suppress(Exception):
                    ent.resubscribe_members()

    def register_group_entity(self, channel, group_id, entity) -> None:
        self._group_entities[(channel, group_id)] = entity

    def unregister_group_entity(self, channel, group_id, entity=None) -> None:
        cur = self._group_entities.get((channel, group_id))
        if entity is None or cur is entity:
            self._group_entities.pop((channel, group_id), None)

    async def async_remove_group_entity(self, channel, group_id) -> bool:
        """Удалить ЖИВУЮ сущность группы (из hass + реестра). Возвращает, была ли она."""
        ent = self._group_entities.pop((channel, group_id), None)
        if ent is None:
            return False
        with contextlib.suppress(Exception):
            await ent.async_remove(force_remove=True)
        return True

    # ── скан с живым логом (для карточки управления) ─────────────────────────
    def _scan_blocking(self, flag: str, channels, assign: str = "manual") -> tuple[dict, bool]:
        self._search_buf = {}
        self._search_overall = (flag == "busDevice")
        self._search_done.clear()
        self._search_got_response = False
        self._search_active = True
        try:
            fields: dict = {"searchFlag": flag}
            if flag == "busDevice":
                # manual: шлюз НЕ трогает конфликтные адреса, шлёт AddrConflicts (мы их ловим
                # и показываем). auto: шлюз САМ переназначает дубли (разрешение конфликтов).
                fields["AddrAssignment"] = assign
                fields["channel"] = channels or [0]
            self.session.send("searchDev", **fields)
            self._search_done.wait(90.0 if flag == "busDevice" else 20.0)
            # (found, ответил ли шлюз вообще) — пустой+ответил = реально пустая шина
            with self._lock:                 # копия под локом (reader пишет _search_buf под ним)
                found = dict(self._search_buf)
            # ⏳ ВРЕМЕННО (2026-08-11, снять вместе с дампом в _dispatch): ИТОГ скана —
            # что реально уйдёт в сверку. Сравнив с сырьём выше, видно, на каком шаге
            # серийник лампы оказался у датчика (или наоборот).
            _LOGGER.info("⏳ВРЕМЕННО ИТОГ скана [%s] flag=%s: %r", self.gw_sn, flag,
                         {k: (v.get("devType"), v.get("address"), v.get("devSn"))
                          for k, v in found.items()})
            return found, self._search_got_response
        finally:
            self._search_active = False

    async def async_scan(self, flag: str = "busDevice", channels=None,
                         progress_cb=None, conflict_cb=None,
                         assign: str = "manual") -> dict[str, dict]:
        """Скан шины с колбэком на каждое найденное устройство (живой лог) и на
        конфликтные адреса (AddrConflicts).

        `assign`:
        - **manual** — НАСТОЯЩИЙ скан: шлюз перечисляет шину и шлёт `AddrConflicts` по
          дублям (мы их показываем, адреса не трогаем). Только он выносит приговор о составе.
        - **auto** — ⚠ НЕ СКАН, а **ОПЕРАЦИЯ разрешения конфликтов** (Fix F, v1.1.4): шлюз
          САМ переназначает дублирующиеся адреса и **НИЧЕГО НЕ ПЕРЕЧИСЛЯЕТ** (проверено на
          железе: конфликт исправляется, список устройств приходит ПУСТОЙ). Раньше мы
          трактовали её как разновидность скана → пустой результат уходил в сверку и
          **зомбировал ВЕСЬ кеш** (на объекте — всё здание краснеет от одной кнопки).
          Теперь кеш НЕ трогаем вовсе; состав перечитывает ручной скан, который карточка
          запускает следом.
        """
        self._scan_cb = progress_cb
        self._conflict_cb = conflict_cb
        self._search_conflicts = []
        self._conflict_keys = set()
        try:
            found, responded = await self.hass.async_add_executor_job(
                self._scan_blocking, flag, channels, assign)
        finally:
            self._scan_cb = None
            self._conflict_cb = None
            self.conflicts = list(self._search_conflicts)
        # Fix F: «авто» — операция, а не перечисление → НИКАКОЙ сверки (ни зомби, ни
        # воскрешения). Возвращаем что пришло (обычно пусто) и выходим.
        if assign == "auto":
            self._log("scan", f"разрешение конфликтов (auto): выполнено, устройств в ответе "
                      f"{len(found)} — состав НЕ пересматриваем (это не скан), "
                      "следом нужен обычный скан")
            return found
        # Fix I (v1.1.4): устройства с КОНФЛИКТНОГО адреса в кеш НЕ принимаем. Шлюз сам сказал,
        # что на этом адресе двое → всё, что он оттуда отдал, недостоверно. Наблюдалось на
        # железе: датчик пришёл с СЕРИЙНИКОМ КНОПКИ (мис-энумерация) → создавалась сущность-
        # фантом, занимавшая entity_id настоящего датчика (после разрешения конфликта тот
        # получал `..._2`, и автоматизации на старый id молчали). Конфликт остаётся видимым в
        # своём блоке карточки; устройство появится после разрешения конфликта + скана.
        if self._search_conflicts and found:
            cset = {(c.get("channel"), c.get("devClass"), c.get("address"))
                    for c in self._search_conflicts}
            skipped = [k for k, d in found.items()
                       if (d.get("channel"), conflict_class(d.get("devType")),
                           d.get("address")) in cset]
            for k in skipped:
                found.pop(k, None)
            if skipped:
                self._log("scan", f"скан: ПРОПУЩЕНО {len(skipped)} устройств(а) с конфликтных "
                          "адресов — данные шлюза о них недостоверны, сущности не создаём; "
                          "разрешите конфликты и повторите скан", level="warn", skipped=skipped)
        # Пусто + шлюз НЕ ответил (обрыв связи) → кеш НЕ трогаем (иначе сверка снесла бы
        # всё при выдернутом ethernet). Пусто, но шлюз ОТВЕТИЛ → реально пустая шина →
        # идём в сверку: устаревшие устройства убираются (карточка очищается).
        # ДЕТЕРМИНИЗМ (решение пользователя): состояние устройства = был ли он в результате
        # скана. Пусто (в т.ч. нет ответа шлюза) → ВСЕ известные в ЗОМБИ (красные). Не
        # выгораживаем «работящие», не гадаем про прогрев. Чинится повторным сканом. При
        # этом записи НИКОГДА не удаляются сканом (только ручное «Забыть») — см. ниже.
        if not found and not responded:
            self._log("scan", f"скан ({flag}): нет ответа шлюза — все известные в зомби "
                      "(пусто = красное, вернётся при ре-скане)", level="warn")
        elif not found:
            self._log("scan", f"скан ({flag}): шлюз ответил «0 устройств» — все известные в зомби")
        # ДИАГНОСТИКА: устройство пришло с ДРУГИМ типом под тем же devSn (напр., датчик
        # 0201 → панель 03xx) — это мис-энумерация шины шлюзом (часто после ребутов).
        with self._lock:                 # снимок: reader-поток мутирует self.devices
            prev_by_sn = {e.get("devSn"): str(e.get("devType"))
                          for e in self.devices.values() if e.get("devSn")}
        for d in found.values():
            sn, dt = d.get("devSn"), str(d.get("devType"))
            if sn and prev_by_sn.get(sn) and prev_by_sn[sn] != dt:
                self._log("scan", f"устройство {sn} (addr{d.get('address')}) сменило тип "
                          f"{prev_by_sn[sn]}→{dt} — мис-энумерация шлюза?",
                          level="warn", devSn=sn, old=prev_by_sn[sn], new=dt)
        zombied: list[str] = []
        moved: list[str] = []
        # Fix E (v1.1.2): ВОСКРЕШАТЬ зомби имеет право только ФИЗИЧЕСКИЙ скан (`busDevice`).
        # `exited` — это выгрузка КЕША ШЛЮЗА («кого я когда-то знал»), а не опрос шины (та же
        # ловушка, что с readDev, см. docs). Снятое с шины устройство в этом кеше ОСТАЁТСЯ →
        # раньше кнопка «Обновить» (она шлёт scan flag=exited) тихо снимала zombie, сущность
        # снова становилась доступной, но данных не получала (датчик push-only) и висела
        # `unknown`. Кеш шлюза — не доказательство физического существования.
        physical = (flag == "busDevice")
        # ВЫТЕСНЕННЫЕ идентичности → ОСИРОТЕВШИЕ (v1.2.2, было Fix O/S/T).
        #
        # Явление: кеш ключуется АДРЕСОМ. Когда на адрес приходит ДРУГОЕ устройство, мы
        # перезаписываем `devSn` в записи этого адреса — и прежняя идентичность исчезает из кеша
        # БЕССЛЕДНО. Зомби она не становится (её записи больше нет), «Забыть» её не достанет, а
        # её СУЩНОСТИ в HA остаются висеть `unavailable` НАВСЕГДА, и убрать их нечем.
        #
        # Как было (v1.1.6–1.1.8): сущности такой идентичности АВТОМАТИЧЕСКИ СНОСИЛИСЬ
        # (`_purge_identity`) — единственное место, где программа решала деструктивно сама. А
        # признак вытеснения врёт как минимум трижды: прогрев шины, мис-энумерация (шлюз отдал
        # чужой серийник), плавающий devSn. Поэтому снос обкладывался защитами: подтверждение
        # ВТОРЫМ сканом (Fix S), гейт вырожденного скана (v1.1.8), `_live_devsn_set` (Fix T) —
        # и вся эта конструкция существовала ровно для того, чтобы не снести ЖИВОЕ устройство.
        #
        # Как теперь: НЕ СНОСИМ. Вытесненная запись остаётся в кеше под СВОИМ ключом
        # `orphan:<devSn>:<devType>` (адресный ключ занял новый жилец) с флагами `zombie`+`orphan`
        # → она ВИДНА в карточке красной, её сущности помечаются `gone`, а снести их может
        # ЧЕЛОВЕК кнопкой «Забыть». Ошибиться программе больше нечем: если идентичность объявится
        # живой на любом адресе, re-link ниже уберёт осиротевшую запись сам (устройство воскресло).
        #
        # Заодно ушла причина, ради которой снос вообще затевался: занятый `entity_id`. С хвостом
        # `sn5` в имени (Fix W, v1.2.0) имена старого и нового жильца РАЗНЫЕ — они не конфликтуют.
        orphaned: list[dict] = []            # снимки вытесненных записей (до перезаписи devSn)
        orphan_keys: list[str] = []          # что реально осиротело в ЭТОМ скане (для лога)
        with self._lock:
            rebuilt: dict[str, dict] = {}
            for d in found.values():
                k = dev_state_key(d.get("devType"), d.get("channel"), d.get("address"))
                # v1.2.18 (F1): НЕ ФИЗИЧЕСКИЙ скан (`exited`, кеш шлюза) НЕ создаёт устройств —
                # только освежает уже известные (тот же гейт, что в `_load_devices_blocking`).
                # UI-кнопка «Обновить» убрана; это страховка на случай прямого вызова WS `scan`
                # с flag=exited, чтобы «древние лампы» из памяти шлюза не всплывали (корень P0).
                if not physical and k not in self.devices:
                    continue
                e = self.devices.get(k, {})
                # мусорный devSn НЕ затирает ранее известный валидный (защита идентичности)
                new_sn = d.get("devSn", "")
                keep_sn = new_sn if is_valid_devsn(new_sn) else (e.get("devSn") or new_sn)
                old_sn = e.get("devSn")
                if (physical and is_valid_devsn(old_sn) and is_valid_devsn(keep_sn)
                        and old_sn != keep_sn):
                    orphaned.append(dict(e))         # СНИМОК старой записи (до перезаписи)
                e.update({
                    "devType": d.get("devType"), "channel": d.get("channel"),
                    "address": d.get("address"), "name": d.get("name", ""),
                    "devSn": keep_sn, "status": d.get("status", ""),
                })
                if physical:
                    e["zombie"] = False              # найден на ШИНЕ → живой (только busDevice)
                    e["bus_seen"] = True             # P0: ФИЗИЧЕСКИ подтверждён busDevice-сканом
                    # В ЭТОЙ СЕССИИ. Только этот флаг = «жив»; exited-кеш/персист его НЕ несут,
                    # поэтому «помнит из кеша» больше не выдаёт устройство за живое (корень P0).
                    # v1.2.18: НАЙДЕН ФИЗИЧЕСКИМ СКАНОМ = он на шине (истина, Закон 2) → снимаем
                    # ЗАЛИПШЕЕ `online_map=False`. Шлюз мог ОДНОКРАТНО прислать offline (глюк шины/
                    # перенумерация) и больше не переопросить → сущность висела «не на связи» до
                    # рестарта HA. Реконнект это уже чистил (`online_map.clear()`), а СКАН — нет
                    # (фикс доезжал до одного пути из двух). Свежий `onlineStatus` уточнит реально
                    # погасшие. `.pop` → дефолт `available=True` (`online_map.get(k, True)`).
                    self.online_map.pop(k, None)
                rebuilt[k] = e
            # RE-LINK при ре-нумерации: идентичности (devSn, devType), найденные ЖИВЫМИ в
            # этом скане. Старая запись с ТАКОЙ ЖЕ парой на ДРУГОМ адресе — это то же
            # устройство, переехавшее (перераздача адресов), а НЕ пропавшее → её старую
            # адресную координату УДАЛЯЕМ, не зомбируем. Иначе копится дубль: один devSn на
            # двух адресах → зомби-двойник в карточке и коллизия unique_id при рестарте HA.
            # Пара включает devType, чтобы движение 0201 и люкс 0202 с ОБЩИМ devSn не
            # схлопнулись друг в друга. Это не нарушение «без авто-деструктива»: устройство
            # живо на новом адресе, а имя/параметры/энергия (ключ devSn) переезжают с ним —
            # убираем лишь устаревшую адресную запись (её мы всё равно сами и создали).
            live_ids = {(e.get("devSn"), str(e.get("devType")))
                        for e in rebuilt.values() if is_valid_devsn(e.get("devSn"))}
            # НЕ найденные на ШИНЕ → ЗОМБИ (красные), но запись СОХРАНЯЕМ (скан НИКОГДА не
            # удаляет; удаление — только ручное «Забыть»). Зомбируем только каналы, которые
            # реально сканировали (остальные не трогаем). Исключение — переехавшие (re-link).
            # Fix E: приговор о жизни/смерти выносит ТОЛЬКО физический скан. `exited` (кеш шлюза)
            # zombie не трогает ВОВСЕ — ни ставит, ни снимает: отсутствие в кеше шлюза так же не
            # доказывает смерть, как присутствие не доказывает жизнь. «Обновить» теперь только
            # освежает поля (имя/адрес/статус) и ловит ре-нумерацию (re-link).
            scanned = set(channels or [0]) if physical else set()
            for k, e in self.devices.items():
                if k in rebuilt:
                    continue
                ident = (e.get("devSn"), str(e.get("devType")))
                if is_valid_devsn(e.get("devSn")) and ident in live_ids:
                    moved.append(k)                  # переехал → старую координату убираем
                    continue                         # (в rebuilt не кладём)
                if physical and e.get("channel") in scanned:
                    e["zombie"] = True
                    zombied.append(k)
                rebuilt[k] = e                       # запись остаётся в любом случае
            # ОСИРОТЕВШИЕ (v1.2.2): вытесненные с адреса — НЕ сносим, а СОХРАНЯЕМ под собственным
            # ключом `orphan:<devSn>:<devType>` (адресный занял новый жилец). Помечаем `zombie`
            # (красный в карточке, сущности → gone, заново не создаются) + `orphan` (карточка
            # объясняет, ПОЧЕМУ он мёртв: его адрес занят другим устройством).
            # Если идентичность объявилась живой на ЛЮБОМ адресе — она в `live_ids`, и осиротевшим
            # НЕ становится (а прежняя осиротевшая запись уйдёт через re-link выше, как «moved»):
            # прогрев шины, мис-энумерация и плавающий devSn лечатся сами, без нашего вмешательства.
            for old in orphaned:
                ident = (old.get("devSn"), str(old.get("devType")))
                if ident in live_ids:
                    continue                         # переехало на другой адрес — оно ЖИВО
                ok = orphan_key(old.get("devSn"), old.get("devType"))
                rebuilt[ok] = dict(old, zombie=True, orphan=True)
                orphan_keys.append(ok)
            self.devices = rebuilt
            for k in zombied:
                self.online_map.pop(k, None)         # зомби → снять живой online
            for k in moved:
                self.online_map.pop(k, None)         # переехавшая координата → чистим хвосты
            # чистим вспомогательные словари от ключей, которых уже нет в devices — защита
            # от накопления «мёртвых» ключей за годы ре-нумераций (иначе растут бесконечно
            # при смене адресов). online_map — АДРЕСНЫЙ; sensor_active — по ИДЕНТИЧНОСТИ
            # (Fix L), поэтому чистится по набору живых identity, а НЕ по адресным ключам
            # (иначе снесло бы вообще все предпочтения — ключи разной природы).
            self.online_map = {k: v for k, v in self.online_map.items() if k in self.devices}
            live_prefs = {self._sensor_pref_key(e, k) for k, e in self.devices.items()}
            self.sensor_active = {k: v for k, v in self.sensor_active.items() if k in live_prefs}
            snapshot = dict(self.devices)
        if moved:
            self._log("scan", f"скан ({flag}): переехало (ре-нумерация) {len(moved)} — старые "
                      "адресные координаты убраны, устройства живы на новых адресах",
                      moved=moved)
        if zombied:
            self._log("scan", f"скан ({flag}): в зомби (не найдено) {len(zombied)} — записи "
                      "сохранены, удаление только вручную «Забыть»", level="warn", zombied=zombied)
        if orphan_keys:
            self._log("scan", f"скан ({flag}): ОСИРОТЕВШИХ {len(orphan_keys)} — их адрес занят "
                      "другим устройством, а сами они на шине не найдены. Сущности СОХРАНЕНЫ и "
                      "помечены недоступными; снести — вручную, кнопкой «Забыть». Если это был "
                      "прогрев шины / мис-энумерация — устройство вернётся на следующем скане, и "
                      "запись уйдёт сама.", level="warn", orphans=orphan_keys)
        # Fix K: ПЕРЕВЗВОД ДАТЧИКОВ после физического скана. Люкс (0202) сам не рапортует — его
        # включает `setSensorOnOff`, а слали мы её только на реконнекте. После сброса/перераздачи
        # адресов реконнект происходил, когда кеш держал ещё СТАРЫЕ адреса → команды уходили мимо
        # (а то и в чужие устройства), и люкс оставался выключенным. Теперь взводим на СВЕЖИХ
        # адресах, сразу после скана.
        if physical:
            with contextlib.suppress(Exception):
                await self._rearm_sensors()
            # v1.2.50: у устройства ПОЯВИЛСЯ devSn (скан дослал его второй пачкой) — переносим
            # имя с АДРЕСНОГО ключа на ключ идентичности. Иначе после фикса имя «исчезло бы»:
            # `name_key` при валидном devSn ищет по нему, а лежало оно по адресу (пока серийника
            # не было). Перенос односторонний и только когда по devSn ещё пусто — чужое имя не
            # затираем.
            with contextlib.suppress(Exception):
                await self._migrate_names_to_devsn()
        # персист обновлённого набора (чтобы переживал оффлайн/рестарт)
        from .store import get_device_store
        ds = get_device_store(self.hass)
        if ds:
            await ds.async_replace(self.gw_sn, snapshot)   # снимок снят под локом выше
            # Z2 (v1.2.14): «отбор» devSn у ДРУГИХ шлюзов — ТОЛЬКО по ФИЗИЧЕСКОМУ скану.
            # Смысл Z2 не изменился: devSn уникален на один шлюз, и переехавшее устройство
            # оставляет хвост в персисте старого → два владельца одного unique_id → лампа не
            # управляется. Изменилось ОСНОВАНИЕ: раньше отбор шёл из `async_load_devices`, где
            # набор строился из кеша/персиста (ПАМЯТЬ) — и шлюз, который лишь помнил устройство,
            # отбирал его у того, где оно физически стоит. Теперь владение заявляет только тот,
            # кто РЕАЛЬНО УВИДЕЛ устройство на шине в этом скане (`bus_seen` + не зомби).
            if physical:
                mine = {e.get("devSn") for e in snapshot.values()
                        if is_valid_devsn(e.get("devSn")) and e.get("bus_seen")
                        and not e.get("zombie")}
                if mine:
                    stolen = await ds.async_claim(self.gw_sn, mine)
                    for other in self.hass.data.get(DOMAIN, {}).values():
                        if other is self:
                            continue
                        for k in stolen.get(other.gw_sn, []) or []:
                            other.devices.pop(k, None)     # хвост переехавшего в RAM старого шлюза
                            other.online_map.pop(k, None)
        # динамика сущностей: создать новые/обновить адрес/пометить ушедшие — без reload
        with contextlib.suppress(Exception):
            self.async_reconcile()
        return found

    # ── фон-чтение и корреляция ответов ──────────────────────────────────────
    def _read_loop(self, session) -> None:
        # читатель привязан к КОНКРЕТНОЙ сессии: при замене сессии (реконнект) выходит
        while self._running and self.session is session:
            try:
                msg = session.recv(0.5)
            except Exception as err:  # noqa: BLE001
                # неожиданный сбой recv: залогировать (раньше глохло молча) и, если
                # выход НЕ из-за штатной смены сессии (реконнект), пометить связь
                # offline → сторож (watchdog) подхватит восстановление
                if self._running and self.session is session:
                    self._log("conn", f"шлюз {self.gw_sn}: сбой чтения сессии: {err}",
                              level="warn")
                    self._on_session_state("offline", session)
                break
            if msg:
                # ВАЖНО: разбор кадра отдельно защищён — необработанное исключение здесь
                # (мусорный/битый кадр от шлюза за годы работы) НЕ должно убивать поток-
                # читатель. Раньше _dispatch был вне try → один кривой кадр глушил приём
                # навсегда (paho жив → offline не стреляет → watchdog молчит). Теперь —
                # логируем и читаем дальше.
                try:
                    self._dispatch(msg)
                except Exception as err:  # noqa: BLE001
                    self._log("conn", f"шлюз {self.gw_sn}: сбой разбора кадра "
                              f"(пропущен): {err}", level="error")

    def _dispatch(self, msg: dict) -> None:
        cmd = msg.get("cmd")
        # сбор searchDev — ПЕРВЫМ (у searchDevRes тоже есть msgId)
        if self._search_active and cmd == "searchDevRes":
            self._search_got_response = True   # шлюз ответил (даже если устройств 0)
            # ⏳ ВРЕМЕННАЯ ДИАГНОСТИКА (2026-08-11) — СНЯТЬ после разбора «перекрёста devSn».
            # На боксе на адресах 0/2/8 серийники лампы (0101) и датчика (0201) поменялись
            # местами: в кеше зеркальные осиротевшие записи. Наш буфер ключуется
            # channel:address:devType (склейка невозможна), поэтому надо увидеть СЫРЬЁ: какие
            # поля и какой devSn шлюз отдаёт на каждый (devType, address) — и есть ли в записях
            # поле `devid` (dev_key предпочитает его, а спека такого поля не знает).
            _LOGGER.info("⏳ВРЕМЕННО СЫРОЙ searchDevRes [%s] status=%s: %r", self.gw_sn,
                         msg.get("searchStatus"), msg.get("data", []))
            # _search_buf общий с executor-потоком (копирует dict(self._search_buf) в
            # _load_devices_blocking/_scan_blocking) → мутируем под тем же self._lock, иначе
            # запоздавший searchDevRes во время копии рушит скан «dict changed size».
            with self._lock:
                for d in msg.get("data", []) or []:
                    k = dev_key(d)
                    prev = self._search_buf.get(k)
                    if prev is None:
                        self._search_buf[k] = d
                        # живой лог: каждое новое найденное устройство → в HA-петлю
                        if self._scan_cb and self.hass:
                            self.hass.loop.call_soon_threadsafe(self._scan_cb, d)
                        continue
                    # 🔴 v1.2.50: ПОВТОРНУЮ запись НЕЛЬЗЯ отбрасывать — она несёт devSn.
                    # Физический скан отдаёт лампу ДВАЖДЫ (дамп busDevice 2026-08-06):
                    # сначала «нашёл по адресу» с пустым devSn, следом ту же лампу С
                    # СЕРИЙНИКОМ. Ключ (`channel:address:devType`) у них один, и прежний код
                    # оставлял ПЕРВУЮ — то есть выбрасывал единственный источник идентичности.
                    # Пока запись жила в кеше, потерю прикрывал `keep_sn` (подставлял прежний
                    # серийник); после «Забыть» прикрывать стало нечем → лампы приходили
                    # безымянными, не переживали рестарт (гейт Z1) и подбирали чужие имена по
                    # адресному ключу. Датчики/панели присылаются одной записью — их не задело.
                    if not prev.get("devSn") and d.get("devSn"):
                        prev["devSn"] = d["devSn"]
                        _LOGGER.debug("шлюз %s: скан дослал devSn для %s → %s",
                                      self.gw_sn, k, d.get("devSn"))
                        # ⏳ ВРЕМЕННО (2026-08-11, снять вместе с дампом выше): ключ буфера
                        # покажет, в ЧЬЮ запись лёг серийник — если k окажется ключом лампы,
                        # а devSn датчика, значит перекрёст рождается ровно здесь.
                        _LOGGER.info("⏳ВРЕМЕННО дослан devSn: ключ=%s ← %r (тип записи %s, "
                                     "адрес %s)", k, d.get("devSn"),
                                     prev.get("devType"), prev.get("address"))
                    # прочие поля тоже уточняем, но НЕ затираем непустое пустым
                    for fld in ("status", "name"):
                        if not prev.get(fld) and d.get(fld):
                            prev[fld] = d[fld]
            # searchStatus: 1 — поиск завершён; 0 — устройств не найдено (тоже завершение,
            # пустая шина) → не ждём полный таймаут. Для busDevice финал — без поля channel.
            status = msg.get("searchStatus")
            if status == 0 or (status == 1 and (not self._search_overall or "channel" not in msg)):
                self._search_done.set()
            return
        # statusBus — ПУШ шлюза о состоянии DALI-шины (мануал стр. 64, раздел 网关数据总线管理):
        # value:true = 总线忙状态 «шина занята», false = «данных на шине нет». Шлюз шлёт его И сам
        # по себе, И ВМЕСТО ответа на команду (эхом нашего msgId) — так он отказывает, когда шина
        # забита. Раньше мы про эту команду не знали: она съедалась msgId-корреляцией как «ответ»,
        # `ack` в ней нет → пользователь видел глухое «не подтверждено шлюзом» вместо причины.
        # Разбор с железа 2026-07-29: активная автояркость держит шину занятой, и создание группы
        # падает — в том числе в РОДНОМ DALI Center (наш код тут ни при чём).
        if cmd == "statusBus":
            busy = bool(msg.get("value"))
            if busy != self.bus_busy:
                self.bus_busy = busy
                _LOGGER.info("шлюз %s: DALI-шина %s", self.gw_sn,
                             "ЗАНЯТА (команды могут отклоняться)" if busy else "свободна")
                self._log("bus", "шина занята — команды могут отклоняться" if busy
                          else "шина освободилась")
        mid = msg.get("msgId")
        if mid:
            # _pending общий с потоком-отправителем (_request_blocking) → под локом:
            # «нашёл запись + записал result» атомарно к pop отправителя (без лока —
            # гонка с pop по таймауту → KeyError здесь рушил поток-читатель)
            with self._lock:
                rec = self._pending.get(mid)
                if rec is not None:
                    rec["result"] = msg
            if rec is not None:
                rec["event"].set()   # будим отправителя вне лока
                return
        # Фолбэк-корреляция по имени ответной команды (res_cmd). Некоторые ответы шлюза
        # НЕ несут msgId — напр. setGatewayNameRes возвращает {gwPid, ack} без msgId (см.
        # Wireshark-захват). Срабатывает ТОЛЬКО когда корреляция по msgId не нашла запись:
        # ищем самого старого (insertion-order) ждущего ИМЕННО этот res_cmd. Команды с
        # нормальным msgId сюда не доходят (выше return). Имя res_cmd специфично
        # (…Res) → коллизий с devStatus/событиями нет.
        if cmd:
            rec = None
            with self._lock:
                for r in self._pending.values():
                    if r.get("result") is None and r.get("res_cmd") == cmd:
                        r["result"] = msg
                        rec = r
                        break
            if rec is not None:
                rec["event"].set()
                return
        # (v1.1.3: временный диагностический лог RAW-DEV снят — разбор энергии/алармов закончен,
        # энергия от шлюза закрыта по принципу (docs/ENERGY_CALC_MODEL.md §1). На масштабе это был
        # INFO-лог на КАЖДЫЙ push-кадр всех ламп (~24с/лампа) — лишний шум и нагрузка.)
        if cmd == "devStatus":
            d = msg.get("data", {}) or {}
            k = dev_state_key(d.get("devType"), d.get("channel"), d.get("address"))
            # обновляем ТОЛЬКО известные устройства; неизвестные из devStatus НЕ создаём
            # (новые появляются через скан — иначе плодятся «зомби» от шумных событий)
            with self._lock:
                known = k in self.devices
                if known:
                    # не позволяем мусорному devSn из шумного события затереть валидный
                    upd = d
                    if "devSn" in d and not is_valid_devsn(d.get("devSn")):
                        upd = {kk: vv for kk, vv in d.items() if kk != "devSn"}
                    self.devices[k].update(upd)
            if known:
                self._notify(k, d)
                self._revive_from_status(k, d)   # Fix E: живой push снимает залипший offline
            # поворотная панель: событие поворота (dpid 4, позиция 0..255) → регулировка
            # яркости цели логикой в HA (натив «следовать за ручкой» не умеет, см. docs).
            if known and str(d.get("devType")) == "0300":
                for p in d.get("property", []) or []:
                    if p.get("dpid") == 4 and p.get("value") is not None:
                        self._schedule_rotary(k, int(p["value"]))
            # кнопочная панель: удержание «плавно» (баг2) — hold (dpid 2) старт, hold_end
            # (dpid 5) финиш; по длительности оцениваем яркость цели (docs/PLAN_PANEL_HOLD_DIM).
            if known and str(d.get("devType")).startswith("03"):
                for p in d.get("property", []) or []:
                    if p.get("dpid") in (2, 5) and p.get("keyNo") is not None:
                        self._schedule_hold(k, int(p["keyNo"]), int(p["dpid"]))
        elif cmd == "onlineStatus":
            # Реальный online/offline. Шлюз САМ опрашивает шину и шлёт это (нагрузки
            # от нас нет). Матчим устройство по address + классу devType[:2] (вкл. лампы).
            self._handle_online(msg.get("data", []) or [])
        elif cmd == "AddrConflicts":
            # Конфликт коротких адресов на шине (manual-режим): копим + живой лог в карточку.
            self._handle_conflicts(msg)
        # ⚠ v1.2.6: `reportEnergy` больше НЕ обрабатывается (шлюз энергию не измеряет — см.
        # комментарий в __init__). Пакеты продолжают приходить (~180/с на объекте) и молча
        # игнорируются: с ними ушёл и постоянный поток `accumulate_real` + запись стора в петлю HA.
        elif cmd == "alarmCodeReport":
            # ПУШ алармов (openCircuit=перегорела и т.п.). Копим последние коды по лампе.
            self._handle_alarm_report(msg)

    def _handle_conflicts(self, msg: dict) -> None:
        """Push AddrConflicts: devType — класс (dali:контроллеры, dali2:датчики/панели),
        address — конфликтный короткий адрес. Дедуп по (channel,class,address)."""
        ch = msg.get("channel")
        new: list[dict] = []
        for c in msg.get("data", []) or []:
            item = {"channel": ch, "devClass": c.get("devType"), "address": c.get("address")}
            k = (ch, item["devClass"], item["address"])
            if k not in self._conflict_keys:
                self._conflict_keys.add(k)
                self._search_conflicts.append(item)
                new.append(item)
        for item in new:
            self._log("scan", f"конфликт адреса: ch{item['channel']} addr{item['address']} "
                      f"({item['devClass']})", level="warn",
                      channel=item["channel"], address=item["address"])
            if self._conflict_cb and self.hass:
                self.hass.loop.call_soon_threadsafe(self._conflict_cb, item)

    def _revive_from_status(self, k: str, d: dict) -> None:
        """Fix E (v1.2.22): СПОНТАННЫЙ `devStatus` снимает залипшее «не на связи».

        ПРИЧИНА (симптом с железа 2026-07-28: вырубил лампы физически → включил → часть
        висит «не на связи», хотя реально работают; лечил только рестарт HA). `online_map`
        выставлялся ЕДИНСТВЕННЫМ путём — событием `onlineStatus` (`_handle_online`). Шлюз
        рассылает `offline` на всю пачку при пропадании питания, а `online` при возврате
        присылает НЕ ВСЕМ → вход в offline есть, выхода нет. Снимали флаг только скан
        (v1.2.18) и реконнект (`online_map.clear()`, v0.81) — поэтому рестарт HA и лечил.
        Это ЗАКОН 2 в чистом виде: наш «онлайн» — память о событиях шлюза, а не физика.

        ЧТО ДЕЛАЕМ. Устройство прислало свой статус ⟹ оно на шине и отвечает. Это ПРЯМОЕ
        наблюдение (в отличие от опроса кеша шлюза, который запрещён законом 2), значит
        залипшая пометка снимается.

        ДВЕ ТОНКОСТИ, без которых фикс был бы неверным:
        1) `.pop`, а НЕ `= True` — дефолт `online_map.get(k, True)` даёт «доступна», и
           следующий ЧЕСТНЫЙ `onlineStatus offline` снова пометит. Так же поступает скан
           (v1.2.18) — поведение согласовано, «зелёным навсегда» никто не залепляется.
        2) ЭХО НЕ СЧИТАЕТСЯ. `devStatus` в пределах `_ECHO_WINDOW_S` после НАШЕЙ команды на
           этот адрес игнорируем: шлюз мог отозваться из своей памяти, не дождавшись лампу.
           Спонтанный push (смена состояния, автояркость `dpid22` — она пушит часто) —
           засчитываем."""
        with self._lock:
            if self.online_map.get(k) is not False:
                return                                  # не залипло — делать нечего
            sent = self._cmd_sent.get(k)
            if sent is not None and time.monotonic() - sent < _ECHO_WINDOW_S:
                return                                  # это эхо на нашу команду, не жизнь
            self.online_map.pop(k, None)
            e = self.devices.get(k) or {}
            e["status"] = "online"
            dt, addr = e.get("devType"), e.get("address")
        _LOGGER.info("Fix E: %s addr%s прислало devStatus — снимаю залипшее «не на связи»",
                     dt, addr)
        self._log("avail", f"{dt} addr{addr}: снова на связи (свой статус, без скана)",
                  devType=dt, address=addr, online=True)
        if self.hass:
            self.hass.loop.call_soon_threadsafe(
                async_dispatcher_send, self.hass, SIGNAL_AVAIL_UPDATE, self.gw_sn, k, True)

    def _handle_online(self, data: list[dict]) -> None:
        changed: list[tuple] = []
        with self._lock:
            for d in data:
                addr = d.get("address")
                ch = d.get("channel")   # если шлюз прислал канал — матчим и по нему
                cls = str(d.get("devType", ""))[:2]
                online = bool(d.get("status"))
                for k, e in self.devices.items():
                    # ОСИРОТЕВШИЙ (v1.2.2) держит СТАРЫЙ адрес — а он теперь принадлежит НОВОМУ
                    # жильцу. Без этого гейта осиротевший ловил бы чужой onlineStatus и выглядел
                    # живым (зелёным), хотя устройства на шине нет.
                    if e.get("orphan"):
                        continue
                    if (e.get("address") == addr
                            and str(e.get("devType", "")).startswith(cls)
                            and (ch is None or e.get("channel") == ch)):
                        prev = self.online_map.get(k)
                        self.online_map[k] = online
                        e["status"] = "online" if online else "offline"
                        if prev != online:
                            changed.append((k, online, e.get("devType"), addr))
        for k, online, dt, addr in changed:
            self._log("avail", f"{dt} addr{addr}: {'online' if online else 'offline'}",
                      devType=dt, address=addr, online=online)
            if self.hass:
                self.hass.loop.call_soon_threadsafe(
                    async_dispatcher_send, self.hass, SIGNAL_AVAIL_UPDATE,
                    self.gw_sn, k, online)

    # ── алармы ОТ шлюза ───────────────────────────────────────────────────────
    # ⚠ v1.2.6: здесь жили `_handle_report_energy` (энергия от шлюза), `_accumulate_real`
    # (накопитель `real_wh`) и весь калибровочный ЗАМЕР (`measure_start`/`measure_stop`/
    # `_measure_persist`/`_measure_autostop`/`measure_active_snapshot`). УДАЛЕНЫ: шлюз энергию не
    # измеряет (ретранслирует энергобанк драйвера либо выдумывает — снаружи неразличимо), а Замер
    # питался ИСКЛЮЧИТЕЛЬНО этими числами. Живёт расчётный путь: P = power_w × кривая(яркость).
    # Побочно снят постоянный поток в петлю HA: ~180 reportEnergy/с × (accumulate_real + запись стора).
    def _handle_alarm_report(self, msg: dict) -> None:
        """alarmCodeReport → РАЗДЕЛЯЕМ реальные аварии и периодическую телеметрию драйвера.
        Сам факт кода ≠ авария: шлюз шлёт наработку/температуру/счётчик отказов регулярно как
        диагностику (см. _HARD_FAULT/_OVERTEMP_WARN_C). Реальная авария → self.alarms (бейдж),
        иначе — self.diagnostics (инфо) и снимаем залипший ложный аларм."""
        d = msg.get("data", {}) or {}
        k = dev_state_key(d.get("devType"), d.get("channel"), d.get("address"))
        codes = {c.get("code"): c.get("value")
                 for c in (d.get("alarmCode", []) or []) if c.get("code")}
        if not codes:
            return
        # выделяем РЕАЛЬНЫЕ аварии
        faults: list[str] = []
        for code, val in codes.items():
            if code in _HARD_FAULT:                       # обрыв/КЗ/отказ драйвера/лампы — по факту
                faults.append(code)
            elif code == "gearFailureNumber":             # счётчик отказов > 0
                try:
                    if float(val) > 0:
                        faults.append(code)
                except (TypeError, ValueError):
                    pass                                  # пусто/None = 0 отказов
            elif code == "overTemperature":               # value = текущая T °C; тревога выше порога
                try:
                    if float(val) >= _OVERTEMP_WARN_C:
                        faults.append(code)
                except (TypeError, ValueError):
                    pass
            # прочие коды (gearRunningTime, неизвестные) — телеметрия, не авария
        with self._lock:
            dev = self.devices.get(k)
            devsn = dev.get("devSn") if dev else None
            if not is_valid_devsn(devsn):
                return
            self.diagnostics[devsn] = {"codes": codes, "ts": time.time()}
            if faults:
                self.alarms[devsn] = {"codes": {c: codes[c] for c in faults}, "ts": time.time()}
            else:
                self.alarms.pop(devsn, None)              # аварий нет → снять залипший ложный аларм
        if faults:
            self._log("avail", f"{d.get('devType')} addr{d.get('address')}: авария "
                      f"{','.join(faults)}", level="warn",
                      devType=d.get("devType"), address=d.get("address"))
        else:
            _LOGGER.debug("диагностика драйвера %s addr%s: %s",
                          d.get("devType"), d.get("address"), codes)

    # ── регулировка яркости поворотной панелью (Path B: логика в HA) ──────────
    def _schedule_rotary(self, key: str, value: int) -> None:
        """Из фон-потока: передать событие поворота в HA-петлю (там вся логика)."""
        if self.hass:
            self.hass.loop.call_soon_threadsafe(self._on_rotary, key, value)

    @callback
    def _on_rotary(self, key: str, value: int) -> None:
        """Событие поворота (в петле HA): дельта позиции → накопить яркость → послать
        (коалесинг: пока команда в полёте, события только копят level)."""
        from .store import get_rotary_store
        rs = get_rotary_store(self.hass)
        dev = self.devices.get(key)
        devsn = dev.get("devSn") if dev else None
        binding = rs.get(devsn) if (rs and devsn) else None
        if not binding:
            return
        st = self._rotary_rt.get(devsn)
        if st is None:                                    # первое событие — базовая точка
            self._rotary_rt[devsn] = {"last": value, "level": self._seed_rotary_level(binding),
                                      "busy": False, "dirty": False}
            return
        delta = value - st["last"]                        # знаковая дельта по uint8 (заворот)
        if delta > 127:
            delta -= 256
        elif delta < -127:
            delta += 256
        st["last"] = value
        if delta == 0:
            return
        step = int(binding.get("step", 20))               # шина 0..1000; дефолт ~2%/щелчок
        st["level"] = max(0, min(1000, st["level"] + delta * step))
        st["dirty"] = True
        if not st["busy"]:
            self._track_task(self._drive_rotary(devsn, binding))

    async def _drive_rotary(self, devsn: str, binding: dict) -> None:
        """Слать яркость, коалесируя: одна команда в полёте, последнее значение — догоняет.
        ТРОТТЛ ПО ВРЕМЕНИ между отправками = таймаут (дефолт 0.8с, пол 0.7с): каждая команда
        яркости запускает fade-разжигание (~0.7с), слать чаще бессмысленно и забивает шину.
        Между событиями копится только `level` — при быстром кручении уходит не поток команд,
        а ~1 на таймаут, финал гарантированно доезжает (пауза после отправки, без таймера-поллера)."""
        st = self._rotary_rt.get(devsn)
        if not st or st["busy"]:
            return
        throttle = max(0.7, float(binding.get("throttle", 0.8)))   # сек; пол = время разжигания
        st["busy"] = True
        try:
            while st["dirty"]:
                st["dirty"] = False
                await self._send_rotary(binding, st["level"])
                await asyncio.sleep(throttle)   # пауза = таймаут отправки (бережём шину/fade)
        finally:
            st["busy"] = False

    async def _send_rotary(self, binding: dict, level: int) -> None:
        """Послать яркость цели. Низ хода (<min) → ВЫКЛ; выше → ВКЛ+яркость. Группа —
        ОДНОЙ командой writeGroup (не разворачиваем); лампа — writeDev."""
        t = binding.get("target") or {}
        if level < 10:                                    # ниже шинного min → выключить
            prop = [{"dpid": 20, "dataType": "bool", "value": False}]
        else:
            prop = [{"dpid": 20, "dataType": "bool", "value": True},
                    {"dpid": 22, "dataType": "uint16", "value": int(level)}]
        if str(t.get("devType")) == "0401":               # цель-группа → writeGroup
            await self.async_request("writeGroup", "writeGroupRes",
                                     channel=t.get("channel"), groupId=t.get("address"),
                                     data=prop, timeout=6.0)
        else:                                             # цель-лампа → writeDev
            await self.async_request("writeDev", "writeDevRes",
                                     data=[{"devType": t.get("devType"), "channel": t.get("channel"),
                                            "address": t.get("address"), "property": prop}], timeout=6.0)

    def _seed_rotary_level(self, binding: dict) -> int:
        """Старт level от текущей яркости цели (чтобы первый поворот не дёргал скачком).
        Не вышло резолвить — 0 (выкл). HA-яркость 0..255 → шина 0..1000."""
        t = binding.get("target") or {}
        try:
            if str(t.get("devType")) == "0401":
                ent = self._group_entities.get((t.get("channel"), t.get("address")))
            else:
                ent = self.live_entity("light", dev_state_key(
                    str(t.get("devType")), t.get("channel"), t.get("address")))
            if ent and getattr(ent, "is_on", False):
                b = getattr(ent, "brightness", None)
                if b:
                    return int(round(b / 255 * 1000))
        except Exception:  # noqa: BLE001 — сид best-effort
            pass
        return 0

    # ── эмпирика яркости от удержания кнопки «плавно» (баг2) ──────────────────
    def _schedule_hold(self, panel_key: str, key_no: int, dpid: int) -> None:
        """Из фон-потока: событие удержания панели → в петлю HA (там вся логика)."""
        if self.hass:
            self.hass.loop.call_soon_threadsafe(self._on_hold, panel_key, key_no, dpid)

    @callback
    def _on_hold(self, panel_key: str, key_no: int, dpid: int) -> None:
        """hold (dpid 2) — засечь старт; hold_end (dpid 5) — длительность → оценка яркости.
        Команду НЕ шлём: рампу ведёт контроллер, мы лишь ОТРАЖАЕМ результат в сущности."""
        rt_key = (panel_key, key_no)
        if dpid == 2:                                     # начало удержания
            self._hold_rt[rt_key] = time.monotonic()
            return
        # dpid == 5: конец удержания
        t0 = self._hold_rt.pop(rt_key, None)
        if t0 is None:                                    # hold_end без нашего hold — пропуск
            return
        dt = time.monotonic() - t0
        if dt <= 0:
            return
        # диагностика: событие удержания дошло (видно даже если цель/привязка не срезонирует)
        _LOGGER.info("hold-dim: hold_end panel=%s key%s dt=%.2fс", panel_key, key_no, dt)
        self._apply_hold_dim(key_no, dt)

    def _rate_from_fade(self, fr) -> float:
        """fadeRate → скорость рампы (шаг/с ≈ уровень HA/с), с калибровочным коэффициентом
        (таблица занижает реальную рампу). Дефолт при пустом/неизвестном fadeRate."""
        try:
            fr = int(fr)
        except (TypeError, ValueError):
            fr = PANEL_DEFAULT_FADE_RATE
        base = PANEL_FADE_RATE_STEPS.get(fr, PANEL_FADE_RATE_STEPS[PANEL_DEFAULT_FADE_RATE])
        return base * PANEL_HOLD_RATE_GAIN

    @callback
    def _apply_hold_dim(self, key_no: int, dt: float) -> None:
        """Найти цели кнопки×удержания (PanelActStore, жест dpid 2), для dimup/dimdown
        оценить сдвиг яркости = скорость(fadeRate)×dt и применить оптимистично. Цель-ЛАМПА —
        напрямую (fadeRate из ParamStore); цель-ГРУППА (0401) — развернуть в члены-лампы
        (fadeRate из GroupParamStore, его задавали группе). Команду НЕ шлём."""
        from .store import get_group_param_store, get_panel_act_store, get_store
        pas = get_panel_act_store(self.hass)
        if not pas:
            return
        ps = get_store(self.hass)
        gps = get_group_param_store(self.hass)
        targets = pas.targets_for(self.gw_sn, key_no, 2)
        if not targets:
            _LOGGER.debug("hold-dim: key%s dt=%.2fс — привязок удержания нет", key_no, dt)
            return
        for act, dt_type, ch, addr in targets:
            if act not in ("dimup", "dimdown"):           # только «плавно ярче/темнее»
                continue
            sign = 1 if act == "dimup" else -1
            if str(dt_type) == "0401":                    # цель-ГРУППА → члены-лампы
                gent = self._group_entities.get((ch, addr))
                if gent is None:
                    _LOGGER.debug("hold-dim: группа ch%s id%s не найдена", ch, addr)
                    continue
                fr = (gps.get(self.gw_sn, ch, addr).get("fadeRate") if gps else None)
                rate = self._rate_from_fade(fr if fr is not None else PANEL_DEFAULT_FADE_RATE)
                delta = rate * dt * sign
                n = 0
                for mkey in list(getattr(gent, "_member_keys", set()) or set()):
                    ment = self.live_entity("light", mkey)
                    if ment is not None:
                        ment.nudge_brightness(delta)
                        n += 1
                _LOGGER.info("hold-dim: key%s %s ГРУППА ch%s id%s dt=%.2fс fadeRate=%s Δ=%.0f членов=%d",
                             key_no, act, ch, addr, dt, fr, delta, n)
                continue
            tkey = dev_state_key(str(dt_type), ch, addr)  # цель-ЛАМПА
            ent = self.live_entity("light", tkey)
            if ent is None:                               # цель не на связи/не создана
                continue
            tdev = self.devices.get(tkey)
            devsn = tdev.get("devSn") if tdev else None
            fr = (ps.get(devsn).get("fadeRate") if (ps and devsn) else None)
            rate = self._rate_from_fade(fr if fr is not None else PANEL_DEFAULT_FADE_RATE)
            delta = rate * dt * sign                      # шаг DALI ≈ уровень HA
            ent.nudge_brightness(delta)
            _LOGGER.info("hold-dim: key%s %s цель %s dt=%.2fс fadeRate=%s Δ=%.0f",
                         key_no, act, tkey, dt, fr, delta)

    def _notify(self, key: str, data: dict) -> None:
        """Прокинуть обновление в HA-петлю (из фон-потока — потокобезопасно)."""
        if self.hass:
            self.hass.loop.call_soon_threadsafe(
                async_dispatcher_send, self.hass, SIGNAL_DEV_UPDATE,
                self.gw_sn, key, data)

    # ── команды (msgId-корреляция; блокирующее — через executor) ─────────────
    def _mark_cmd_targets(self, fields: dict) -> None:
        """Запомнить, КОМУ мы только что послали команду (Fix E, v1.2.22).

        Цели живут либо в `data` списком (`writeDev`/`readDev`/`setDevParam`), либо прямо в
        корне полей (`readPanel`/`readSensor`). Время нужно, чтобы отличить СПОНТАННЫЙ
        `devStatus` (= устройство живо, снимаем залипший offline) от ЭХА на нашу команду
        (доказательством жизни не является — шлюз мог ответить из своей памяти)."""
        now = time.monotonic()
        items = fields.get("data")
        targets = items if isinstance(items, list) else []
        if fields.get("address") is not None:
            targets = [*targets, fields]
        for t in targets:
            if not isinstance(t, dict) or t.get("address") is None:
                continue
            k = dev_state_key(t.get("devType"), t.get("channel"), t.get("address"))
            self._cmd_sent[k] = now
        # ключи старше окна бесполезны (решение уже принято) — не даём словарю расти годами
        if len(self._cmd_sent) > _CMD_SENT_MAX:
            self._cmd_sent = {kk: ts for kk, ts in self._cmd_sent.items()
                              if now - ts < _ECHO_WINDOW_S}

    def _request_blocking(self, cmd: str, res_cmd: str, timeout: float = 5.0,
                          **fields) -> dict | None:
        # локальная ссылка: сессия может смениться/обнулиться при реконнекте между
        # проверкой и send → берём один раз, при отсутствии связи не падаем
        sess = self.session
        if sess is None:
            return None
        ev = threading.Event()
        # msgId берём ЗАРАНЕЕ (потокобезопасный счётчик сессии) и регистрируем _pending ДО
        # публикации: иначе сверхбыстрый ответ шлюза мог прийти в окне send→регистрация и
        # потеряться (уходил в res_cmd-фолбэк или в никуда) → ложный таймаут команды.
        # _pending общий с потоком-читателем (_dispatch) → под локом (читатель пишет
        # result/event, мы делаем pop; без лока возможен KeyError и тихая смерть читателя).
        # Само ожидание ev.wait — ВНЕ лока, иначе читатель не войдёт (взаимоблокировка).
        mid = sess.next_msg_id()
        with self._lock:
            # res_cmd храним для фолбэк-корреляции по имени ответа (когда msgId в ответе нет)
            self._pending[mid] = {"event": ev, "result": None, "res_cmd": res_cmd}
            self._mark_cmd_targets(fields)   # Fix E: чей devStatus будет «эхом», а не жизнью
        try:
            sess.send(cmd, msg_id=mid, **fields)
        except Exception:
            with self._lock:                 # сбой публикации — не оставляем висячий pending
                self._pending.pop(mid, None)
            raise
        ev.wait(timeout)
        with self._lock:
            rec = self._pending.pop(mid, None)
        return rec.get("result") if rec else None

    async def async_request(self, cmd: str, res_cmd: str, timeout: float = 5.0,
                            **fields) -> dict | None:
        return await self.hass.async_add_executor_job(
            lambda: self._request_blocking(cmd, res_cmd, timeout, **fields))
