"""Оценщик здоровья устройств (сателлит) — ИНКРЕМЕНТАЛЬНЫЙ (v1.2.5).

Истина — СОСТОЯНИЕ СУЩНОСТИ HA. В управление не пишет (только читает).

⚠ ПОЧЕМУ ПЕРЕПИСАН. Раньше это был «оценщик по расписанию»: любой чих (сигнал связи, тик таймера,
и — главное — КАЖДЫЙ запрос `health_data` от внешнего потребителя) запускал ПОЛНЫЙ синхронный
проход по всем устройствам всех шлюзов: 4400 итераций, 4400 `states.get`, 4400 копий словарей под
локами хабов, перестройка 30-дневного лога и запись стора на диск — в петле HA, дважды в минуту,
круглосуточно, даже когда в здании ничего не менялось (долг HD1). Внешний веб-интерфейс своим
поллингом фактически УПРАВЛЯЛ нагрузкой на HA.

**Теперь работа пропорциональна числу РЕАЛЬНЫХ изменений, а не числу устройств × частоту опроса.**

Как это работает — три механизма вместо обхода:

1. **Событие о КОНКРЕТНОЙ сущности** (`async_track_state_change_event` по `entity_id` — штатный
   механизм HA: он разводит события по ключу ДО вызова колбэка). Лампа ушла в `unavailable` →
   пересчитываем ЭТУ ЛАМПУ, и только её. O(1) вместо O(4400).

2. **Будильник на ДЕДЛАЙН** вместо периодического обхода. Все пороги health — это «состояние
   держится дольше T» (движение залипло >1ч, люкс не менялся >7ч, оффлайн дольше грейса). Момент,
   когда состояние СТАНОВИТСЯ проблемой, не сопровождается никаким событием — просто прошло время.
   Раньше ради этого и обходили всех подряд. Теперь: зная `last_changed` и порог, мы ЗНАЕМ точный
   момент → держим карту дедлайнов и ставим ОДИН будильник на ближайший. Событие смены состояния
   дедлайн сбрасывает. Просыпаемся — проверяем только тех, у кого срок истёк (единицы, не 4400).

3. **Чтение НЕ считает.** `health_data` отдаёт готовый снимок из стора. Пересчёт — только по
   событию/дедлайну (и по явному `refresh: true` — кнопка «Обновить» в нашей карточке).

Полный проход остался ровно в двух местах: посев на старте и явный `refresh()`.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta

from homeassistant.const import EVENT_HOMEASSISTANT_STARTED
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import (
    area_registry as ar_reg,
    device_registry as dr_reg,
    entity_registry as er,
    floor_registry as fr_reg,
)
from homeassistant.helpers.dispatcher import (
    async_dispatcher_connect,
    async_dispatcher_send,
)
from homeassistant.helpers.event import (
    async_call_later,
    async_track_state_change_event,
    async_track_time_interval,
)
from homeassistant.util import dt as dt_util

from ..const import DOMAIN
from ..coordinator import SIGNAL_AVAIL_UPDATE, SIGNAL_CONN_UPDATE
from .store import DEFAULT_THRESHOLDS, RETENTION_DAYS, HealthStore

_LOGGER = logging.getLogger(__name__)

ST_ONLINE = "online"
_GW_BAD = ("offline", "failed")   # реальная потеря связи (init/reauth — транзиентны, не ошибка)
SIGNAL_HEALTH_UPDATE = f"{DOMAIN}_health_update"   # для живого обновления карточки
_PUSH_DEBOUNCE = 1.0    # с — коалесинг пачки изменений в один push подписчикам
_REINDEX_DEBOUNCE = 3.0  # с — пересбор индекса после создания/переименования сущностей

# человекочитаемые подписи ошибок
KIND_LABEL = {
    "gw_offline": "Шлюз оффлайн",
    "lamp_unknown": "Лампа: состояние неизвестно",
    "lamp_offline": "Лампа: оффлайн",
    "sensor_unknown": "Датчик: состояние неизвестно",
    "motion_stuck": "Движение залипло",
    "motion_idle": "Свободно слишком долго",
    "lux_stale": "Освещённость не меняется",
    "panel_unknown": "Панель: нет связи",
}
_BAD = ("unavailable", "unknown")   # сущность ЕСТЬ, но состояние плохое (≠ «сущности нет»)


def _roles(devtype: str) -> list[tuple[str, str, str]]:
    """Какие сущности устройство даёт ЗДОРОВЬЮ: [(platform, суффикс unique_id, тег роли)].

    Switch'и активации здоровью не интересны (это настройка, а не состояние железа)."""
    t = str(devtype)
    if t.startswith("01"):
        return [("light", "", "lamp")]
    if t == "0201":
        return [("sensor", "_motion", "motion")]
    if t == "0202":
        return [("sensor", "_lux", "lux")]
    if t.startswith("03"):
        return [("event", "_event", "panel")]
    return []


class HealthEvaluator:
    def __init__(self, hass: HomeAssistant, store: HealthStore) -> None:
        self._hass = hass
        self._store = store
        self._unsubs: list = []
        self._unsub_states = None      # подписка на состояния наших сущностей
        self._push_timer = None        # дебаунс push подписчикам
        self._deadline_timer = None    # ОДИН будильник на ближайший дедлайн
        self._reindex_timer = None
        self._sweep_timer = None       # страховочный редкий полный посев
        # индексы (строятся посевом, обновляются при появлении/переименовании сущностей)
        self._by_eid: dict[str, dict] = {}    # entity_id → ctx устройства
        self._by_key: dict[str, dict] = {}    # key (gw:<идентичность>:devType) → ctx
        self._deadlines: dict[str, float] = {}   # key → utc-timestamp, когда станет ошибкой
        self._generated_at: str | None = None    # когда снимок последний раз пересчитывался

    # ── жизненный цикл ────────────────────────────────────────────────────────
    @callback
    def start(self) -> None:
        # связь шлюза — по-прежнему сигналом (это не сущность, состояния в HA у неё нет)
        self._unsubs.append(async_dispatcher_connect(
            self._hass, SIGNAL_CONN_UPDATE, self._on_conn))
        self._arm_sweep()
        # доступность устройства (onlineStatus) сама по себе диагнозом не является: она лишь
        # МЕНЯЕТ состояние сущности (available → unavailable), а это придёт событием состояния.
        # Сигнал слушаем только чтобы поймать появление НОВЫХ устройств в кеше (переиндексация).
        self._unsubs.append(async_dispatcher_connect(
            self._hass, SIGNAL_AVAIL_UPDATE, self._on_avail))
        # сущности создаются/переименовываются (скан, перераздача адресов, ренейм) → entity_id
        # меняется, а подписка идёт ПО НЕМУ → пересобираем индекс (дебаунс: правки идут пачкой)
        self._unsubs.append(self._hass.bus.async_listen(
            "entity_registry_updated", self._on_registry))
        # ПЕРВЫЙ посев — когда HA поднялся (сущности/хабы готовы), иначе всё «неизвестно»
        if self._hass.is_running:
            self._unsubs.append(async_call_later(self._hass, 15, lambda _n: self._seed()))
        else:
            self._unsubs.append(self._hass.bus.async_listen_once(
                EVENT_HOMEASSISTANT_STARTED, lambda _e: self._seed()))

    @callback
    def stop(self) -> None:
        for u in self._unsubs:
            u()
        self._unsubs = []
        for t in ("_unsub_states", "_push_timer", "_deadline_timer", "_reindex_timer",
                  "_sweep_timer"):
            cancel = getattr(self, t)
            if cancel:
                cancel()
                setattr(self, t, None)

    @callback
    def refresh(self) -> None:
        """Полный пересчёт — ЯВНОЕ действие (кнопка «Обновить», смена порогов).

        ⚠ Обычное чтение (`health_data`) сюда БОЛЬШЕ НЕ ХОДИТ: раньше каждый запрос внешнего
        потребителя запускал полный обход объекта (долг HD1)."""
        self._seed()

    @callback
    def rearm(self) -> None:
        """Пороги сменились → все дедлайны пересчитать заново."""
        self._arm_sweep()
        self._seed()

    @callback
    def _arm_sweep(self) -> None:
        """СТРАХОВОЧНЫЙ полный посев раз в `interval_min` (дефолт 20 мин).

        Зачем он нужен, раз всё событийно: инкрементальная модель верна ровно настолько, насколько
        мы не пропустили событие (гонка при старте, сущность вне индекса, наш собственный баг).
        Редкий сверочный проход — дешёвая страховка от «тихо разъехавшейся» картины: 1 проход в
        20 минут против прежних 2 проходов в МИНУТУ, которые гнал поллинг внешнего интерфейса.
        Он же оставляет осмысленной настройку `interval_min` (она в контракте порогов)."""
        if self._sweep_timer:
            self._sweep_timer()
            self._sweep_timer = None
        mins = max(1.0, float(self._store.thresholds.get(
            "interval_min", DEFAULT_THRESHOLDS["interval_min"])))
        self._sweep_timer = async_track_time_interval(
            self._hass, lambda _now: self._seed(), timedelta(minutes=mins))

    # ── индекс и посев ────────────────────────────────────────────────────────
    @callback
    def _reindex(self) -> None:
        """Собрать карту `entity_id → устройство` и переподписаться на состояния.

        Подписка адресная (`async_track_state_change_event`): HA разводит событие по `entity_id`
        ДО вызова колбэка → мы получаем ТОЛЬКО свои сущности и ТОЛЬКО изменившиеся."""
        hass = self._hass
        ereg = er.async_get(hass)
        by_eid: dict[str, dict] = {}
        by_key: dict[str, dict] = {}
        for hub in hass.data.get(DOMAIN, {}).values():
            gw = getattr(hub, "gw_sn", None)
            if not gw:
                continue
            for dev in hub.devices_snapshot():
                dtv = str(dev.get("devType", ""))
                # v1.2.73: сущности ищем по КЛЮЧУ ИДЕНТИЧНОСТИ, а не по серийнику напрямую.
                # В штатном режиме это тот же devSn, в адресном — координата; собери мы
                # `unique_id` из devSn вручную, здоровье просто перестало бы находить сущности
                # (тихо: список ошибок пуст — «всё хорошо»).
                # `name_key_for` (а не `identity`) — он воспроизводит ПРЕЖНИЙ гейт: в штатном
                # режиме устройство без валидного серийника здоровью не показывалось.
                devsn = (hub.name_key_for(dev) if hasattr(hub, "name_key_for")
                         else dev.get("devSn"))
                if not devsn:
                    continue
                for platform, sfx, role in _roles(dtv):
                    uid = (hub.identity(dev, light=True)
                           if (platform == "light" and hasattr(hub, "identity")) else f"{devsn}{sfx}")
                    eid = ereg.async_get_entity_id(platform, DOMAIN, uid)
                    if not eid:
                        continue            # сущности ещё нет → появится, поймаем реестром
                    ctx = {
                        "key": f"{gw}:{devsn}:{dtv}", "gw": gw, "devSn": devsn, "devType": dtv,
                        "role": role, "entity_id": eid, "hub": hub,
                        "fallback": f"{dev.get('typeName') or dtv} {dev.get('address')}",
                    }
                    by_eid[eid] = ctx
                    by_key[ctx["key"]] = ctx
        self._by_eid, self._by_key = by_eid, by_key
        if self._unsub_states:
            self._unsub_states()
            self._unsub_states = None
        if by_eid:
            self._unsub_states = async_track_state_change_event(
                hass, list(by_eid), self._on_state)
        _LOGGER.debug("health: индекс — %d сущностей", len(by_eid))

    @callback
    def _seed(self) -> None:
        """ПОЛНЫЙ проход: посев на старте / явный refresh. Единственное место O(N)."""
        if not self._in_loop():
            return
        if not self._hass.data.get(DOMAIN):
            return          # хабов ещё нет → не трогаем active (иначе ложно «восстановим» всё)
        self._reindex()
        now = dt_util.utcnow()
        current: dict[str, dict] = {}
        self._deadlines = {}
        for hub in self._hass.data.get(DOMAIN, {}).values():
            gw = getattr(hub, "gw_sn", None)
            if not gw:
                continue
            rec = self._judge_gateway(hub, gw)
            if rec:
                current[f"{gw}:gateway"] = rec
                continue    # шлюз оффлайн → его устройства не судим (они unavailable «за компанию»)
        for key, ctx in self._by_key.items():
            if self._gw_bad(ctx["hub"]):
                continue
            rec = self._judge(ctx, now)
            if rec:
                current[key] = rec
        self._commit(current, full=True)

    # ── реакция на события ────────────────────────────────────────────────────
    @callback
    def _on_state(self, event) -> None:
        """Состояние НАШЕЙ сущности изменилось → пересудить ЭТО устройство. O(1)."""
        if not self._in_loop():
            return
        ctx = self._by_eid.get(event.data.get("entity_id"))
        if ctx is None:
            return
        if self._gw_bad(ctx["hub"]):
            return          # шлюз оффлайн — диагноз по устройствам не ставим (одна запись о шлюзе)
        key = ctx["key"]
        # ⚠ дедлайн этого устройства мог сдвинуться (состояние сменилось → отсчёт порога с нуля).
        # Сравниваем ДО/ПОСЛЕ: если не сдвинулся и диагноз не изменился — таймер не трогаем.
        # Без этого `_arm_deadline` считал бы `min()` по ВСЕМ дедлайнам на КАЖДОЕ событие лампы
        # (~180/с на объекте) — мы бы просто заменили один налог на петлю другим.
        before = self._deadlines.get(key)
        rec = self._judge(ctx, dt_util.utcnow())
        self._commit_one(key, rec, redeadline=(before != self._deadlines.get(key)))

    @callback
    def _on_conn(self, gw_sn: str, state: str) -> None:
        """Связь шлюза сменилась → пересудить ШЛЮЗ (и снять/вернуть диагнозы его устройств)."""
        if not self._in_loop():
            return
        hub = None
        for h in self._hass.data.get(DOMAIN, {}).values():
            if getattr(h, "gw_sn", None) == gw_sn:
                hub = h
                break
        if hub is None:
            return
        # F7 (v1.2.20): ТРАНЗИЕНТ реконнекта (`reauth`/`init`) — это НЕ восстановление и НЕ новый
        # оффлайн. Раньше сюда проваливались все состояния: `_judge_gateway` возвращал None (reauth
        # ∉ _GW_BAD) → шлюз ложно помечался «Восстановлен», а ветка else судила ВСЕ ≤128 устройств
        # (все `unavailable` → создавались ошибки), затем попытка проваливалась (offline) → снова
        # «Восстановлено» на все. На КАЖДОЙ попытке сторожа ~258 записей стора + push → лежащий
        # шлюз выносил 30-дневный лог здоровья за часы. Транзиент теперь НЕ трогает ни диагноз
        # шлюза, ни устройства: ждём УСТОЙЧИВОГО online (восстановление) либо offline (потеря).
        st = getattr(hub, "state", ST_ONLINE)
        if st != ST_ONLINE and st not in _GW_BAD:
            return
        gw_key = f"{gw_sn}:gateway"
        rec = self._judge_gateway(hub, gw_sn)
        self._commit_one(gw_key, rec)
        if rec:
            # шлюз оффлайн → снимаем диагнозы его устройств (они недоступны «за компанию»,
            # каскад ошибок не нужен — так было и в прежней модели)
            for key, ctx in self._by_key.items():
                if ctx["gw"] == gw_sn:
                    self._commit_one(key, None)
                    self._deadlines.pop(key, None)
        else:
            # шлюз вернулся → пересудить его устройства (их состояния уже подтянулись)
            now = dt_util.utcnow()
            for key, ctx in self._by_key.items():
                if ctx["gw"] == gw_sn:
                    self._commit_one(key, self._judge(ctx, now))
        self._arm_deadline()

    @callback
    def _on_avail(self, gw_sn: str, key: str, online: bool) -> None:
        """onlineStatus сам по себе не диагноз (он меняет available → придёт событие состояния).
        Но если устройство НОВОЕ (его нет в индексе) — переиндексируемся."""
        if not self._by_key:
            self._schedule_reindex()

    @callback
    def _on_registry(self, event) -> None:
        """Сущности создаются/переименовываются (скан, перераздача адресов, ренейм) → подписка
        идёт по `entity_id`, поэтому индекс надо пересобрать, иначе оценщик «оглохнет».

        Фильтруем строго по НАШИМ сущностям: в чужом HA сущности создаются и переименовываются
        постоянно, и переиндексироваться на каждую — тот же холостой налог, от которого уходим."""
        data = event.data
        action = data.get("action")
        eid = data.get("entity_id") or ""
        if action == "update" and "entity_id" not in (data.get("changes") or {}):
            return                       # правка имени/иконки — entity_id цел, подписка жива
        if eid in self._by_eid:          # наша сущность переименована/удалена
            self._schedule_reindex()
            return
        if action == "remove":
            return                       # чужая ушла — нам всё равно
        rec = er.async_get(self._hass).async_get(eid)
        if rec is not None and rec.platform == DOMAIN:
            self._schedule_reindex()     # появилась/переехала НАША сущность

    @callback
    def _schedule_reindex(self) -> None:
        if self._reindex_timer:
            return
        self._reindex_timer = async_call_later(
            self._hass, _REINDEX_DEBOUNCE, self._fire_reindex)

    @callback
    def _fire_reindex(self, _now) -> None:
        self._reindex_timer = None
        if self._hass.data.get(DOMAIN):
            self._seed()                 # пересобрать индекс + пересудить (сущности изменились)

    # ── суждение (диагноз одного устройства) ──────────────────────────────────
    @callback
    def _gw_bad(self, hub) -> bool:
        return getattr(hub, "state", ST_ONLINE) != ST_ONLINE

    @callback
    def _judge_gateway(self, hub, gw: str) -> dict | None:
        state = getattr(hub, "state", ST_ONLINE)
        if state not in _GW_BAD:
            return None                  # online / init / reauth (транзиенты — не ошибка)
        rec = self._blank("gw_offline", f"Шлюз {gw}", "gateway", gw, None)
        dev = dr_reg.async_get(self._hass).async_get_device(identifiers={(DOMAIN, gw)})
        if dev:
            rec["device_id"] = dev.id
        return rec

    @callback
    def _judge(self, ctx: dict, now) -> dict | None:
        """Диагноз ОДНОГО устройства + дедлайн «когда станет ошибкой» (для будильника).

        Возвращает запись ошибки либо None. Побочно пишет `self._deadlines[key]`."""
        key = ctx["key"]
        self._deadlines.pop(key, None)
        st = self._hass.states.get(ctx["entity_id"])
        if st is None:
            return None                  # сущности нет → не «неизвестно», а пропуск
        th = self._store.thresholds
        grace_s = max(60.0, th.get("grace_min", DEFAULT_THRESHOLDS["grace_min"]) * 60)
        motion_stuck_s = th["motion_stuck_h"] * 3600
        clear_s = th["clear_h"] * 3600
        lux_stale_s = th["lux_stale_h"] * 3600
        state, lc = st.state, st.last_changed
        age = (now - lc).total_seconds() if lc else 0.0
        role = ctx["role"]

        def verdict(kind: str, limit: float) -> dict | None:
            """Состояние ПЛОХОЕ, но засчитываем только если держится дольше `limit`.
            Иначе — ставим дедлайн: вернёмся ровно в момент, когда порог будет перейдён."""
            if age >= limit:
                return self._enrich(ctx, kind, lc)
            self._deadlines[key] = (lc.timestamp() + limit) if lc else 0.0
            return None

        if role == "lamp":
            if state == "unavailable":
                return verdict("lamp_offline", grace_s)
            if state == "unknown":
                return verdict("lamp_unknown", grace_s)
            return None
        if role == "motion":
            if state == "unavailable":
                return verdict("sensor_unknown", grace_s)
            if state == "unknown":       # push-only ещё не рапортовал → ошибка лишь если ДОЛГО
                return verdict("sensor_unknown", lux_stale_s)
            if state == "motion":        # v1.2.45: состояния латиницей (было «ДВИЖЕНИЕ»)
                return verdict("motion_stuck", motion_stuck_s)
            if state == "vacant":        # было «свободно»
                return verdict("motion_idle", clear_s)
            return None
        if role == "lux":
            if state == "unavailable":
                return verdict("sensor_unknown", grace_s)
            if state == "unknown":
                return verdict("sensor_unknown", lux_stale_s)
            return verdict("lux_stale", lux_stale_s)   # показания не меняются слишком долго
        if role == "panel":
            if state == "unavailable":
                return verdict("panel_unknown", grace_s)
            return None
        return None

    # ── запись/публикация ─────────────────────────────────────────────────────
    @callback
    def _blank(self, kind, name, dtv, gw, since) -> dict:
        """Единая форма записи (контракт!): поля адресации есть ВСЕГДА (у шлюза — None),
        чтобы внешние потребители не проверяли наличие ключей."""
        return {"kind": kind, "name": name, "devType": dtv, "gw_sn": gw, "since": since,
                "area": None, "floor": None, "area_id": None, "floor_id": None,
                "device_id": None, "entity_id": None}

    @callback
    def _enrich(self, ctx: dict, kind: str, lc) -> dict:
        """Запись ошибки + резолв реестров. Резолвим ТОЛЬКО ошибку (не здоровых) — раньше это
        были 13 200 обращений к реестрам на каждый проход (HD1)."""
        rec = self._blank(kind, ctx["fallback"], ctx["devType"], ctx["gw"],
                          lc.isoformat() if lc else None)
        rec["entity_id"] = ctx["entity_id"]
        hass = self._hass
        dev = dr_reg.async_get(hass).async_get_device(identifiers={(DOMAIN, ctx["devSn"])})
        if dev:
            # v1.2.7: имя устройства теперь `<тип>_<полный devSn>` — техн. идентификатор,
            # нечитаемый в отчёте. Берём имя ЧЕЛОВЕКА (name_by_user) или fallback (typeName+addr),
            # минуя dev.name.
            rec["name"] = dev.name_by_user or ctx["fallback"]
            rec["device_id"] = dev.id
            if dev.area_id:
                rec["area_id"] = dev.area_id
                area = ar_reg.async_get(hass).async_get_area(dev.area_id)
                if area:
                    rec["area"] = area.name
                    if area.floor_id:
                        rec["floor_id"] = area.floor_id
                        fl = fr_reg.async_get(hass).async_get_floor(area.floor_id)
                        if fl:
                            rec["floor"] = fl.name
        return rec

    @callback
    def _commit_one(self, key: str, rec: dict | None, redeadline: bool = True) -> None:
        """Обновить ОДНУ запись: появилась ошибка / исчезла (→ «Восстановлено») / без изменений.

        `redeadline` — сдвинулся ли дедлайн этого устройства (нужно ли переставлять будильник).
        Здоровая лампа мигает состоянием сотни раз в час: если ни диагноз, ни дедлайн не
        изменились, мы НЕ трогаем ни стор, ни диск, ни подписчиков, ни таймер."""
        active = dict(self._store.active)
        prev = active.get(key)
        now_iso = dt_util.utcnow().isoformat()
        pruned = False
        if rec is None:
            if prev is None:
                if redeadline:
                    self._arm_deadline()
                return                   # ничего не изменилось — ни записи, ни push
            self._store.append_recovered({**prev, "resolved": now_iso})
            active.pop(key, None)
            pruned = True                # лог пополнился → пора применить ретенцию
        else:
            since = (prev["since"] if prev and prev.get("since")
                     else (rec.get("since") or now_iso))
            new = {**rec, "since": since}
            if prev == new:
                if redeadline:
                    self._arm_deadline()
                return                   # тот же диагноз — не трогаем стор и не будим подписчиков
            active[key] = new
        self._store.set_active(active)
        # ретенция (проход по логу до 5000 записей) — только когда лог реально пополнился,
        # а не на каждое изменение: иначе пачка «восстановлений» гоняла бы его сотни раз
        self._housekeep(prune=pruned)
        self._generated_at = now_iso
        self._schedule_push()
        self._arm_deadline()

    @callback
    def _commit(self, current: dict[str, dict], full: bool = False) -> None:
        """Полный посев: заменить картину целиком (только из `_seed`/`refresh`)."""
        active = self._store.active
        now_iso = dt_util.utcnow().isoformat()
        new_active: dict[str, dict] = {}
        for key, rec in current.items():
            prev = active.get(key)
            since = (prev["since"] if prev and prev.get("since")
                     else (rec.get("since") or now_iso))
            new_active[key] = {**rec, "since": since}
        for key, prev in active.items():
            if key not in current:
                self._store.append_recovered({**prev, "resolved": now_iso})
        self._store.set_active(new_active)
        self._housekeep()
        self._generated_at = now_iso
        self._schedule_push()
        self._arm_deadline()

    @callback
    def _housekeep(self, prune: bool = True) -> None:
        """Ретенция лога (30 дней) + суточное окно «Восстановлено».

        `prune=False` — лог не пополнялся, чистить нечего (проход по нему стоит до 5000 итераций)."""
        if prune:
            cutoff = (dt_util.utcnow() - timedelta(days=RETENTION_DAYS)).isoformat()
            self._store.prune_recovered(cutoff)
        midnight = dt_util.as_utc(dt_util.start_of_local_day()).isoformat()
        if (self._store.window_since or "") < midnight:
            self._store.set_window_since(midnight)

    @property
    def generated_at(self) -> str | None:
        """Когда снимок последний раз менялся (для внешних потребителей: свежесть данных)."""
        return self._generated_at

    # ── push подписчикам (коалесинг) ──────────────────────────────────────────
    @callback
    def _schedule_push(self) -> None:
        """Пачка изменений (реконнект шлюза = сотни событий) → ОДИН push, а не сотни."""
        if self._push_timer:
            return
        self._push_timer = async_call_later(self._hass, _PUSH_DEBOUNCE, self._fire_push)

    @callback
    def _fire_push(self, _now) -> None:
        self._push_timer = None
        async_dispatcher_send(self._hass, SIGNAL_HEALTH_UPDATE)

    # ── будильник на ближайший дедлайн ────────────────────────────────────────
    @callback
    def _arm_deadline(self) -> None:
        """ОДИН будильник на ближайший момент, когда чьё-то состояние станет ошибкой.

        Это замена периодическому обходу: раньше мы будили петлю каждые 20 минут и обходили ВСЕХ,
        чтобы узнать, не истёк ли у кого-то порог. Теперь момент известен точно (`last_changed` +
        порог), поэтому просыпаемся ровно к нему и проверяем только просроченных."""
        if self._deadline_timer:
            self._deadline_timer()
            self._deadline_timer = None
        if not self._deadlines:
            return
        soonest = min(self._deadlines.values())
        delay = max(5.0, soonest - dt_util.utcnow().timestamp() + 1.0)   # +1с: порог точно перейдён
        self._deadline_timer = async_call_later(self._hass, delay, self._fire_deadline)

    @callback
    def _fire_deadline(self, _now) -> None:
        self._deadline_timer = None
        if not self._in_loop():
            return
        now = dt_util.utcnow()
        now_ts = now.timestamp()
        due = [k for k, ts in self._deadlines.items() if ts <= now_ts]
        for key in due:
            ctx = self._by_key.get(key)
            if ctx is None:
                self._deadlines.pop(key, None)
                continue
            if self._gw_bad(ctx["hub"]):
                self._deadlines.pop(key, None)
                continue
            self._commit_one(key, self._judge(ctx, now))
        self._arm_deadline()

    # ── служебное ─────────────────────────────────────────────────────────────
    @callback
    def _in_loop(self) -> bool:
        """Пишем в стор и шлём сигналы — это ОБЯЗАНО идти в петле HA. Если попали из чужого
        потока (регресс) — перепланируем, а не портим данные."""
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            self._hass.loop.call_soon_threadsafe(self._seed)
            return False
        return True
