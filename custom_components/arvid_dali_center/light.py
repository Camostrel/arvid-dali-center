"""Платформа light — лампы DALI (devType 0101..0106).

Ф2: вкл/выкл (dpid20), яркость (dpid22, шкала шлюза 1..1000), цветовая температура
(dpid23, 2700..6500K для CCT-ламп 0102). Управление — writeDev. Состояние —
оптимистично + обновление по событиям devStatus (если шлюз их шлёт).
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.light import (
    ATTR_BRIGHTNESS,
    ATTR_COLOR_TEMP_KELVIN,
    ColorMode,
    LightEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import (
    async_dispatcher_connect,
    async_dispatcher_send,
)
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.util import slugify

from .const import DOMAIN
from .coordinator import (
    SIGNAL_DEV_UPDATE,
    XGROUP_ENTITIES,
    SIGNAL_LAMP_STATE,
    DaliAvailMixin,
    DaliBusEntity,
    DaliGatewayHub,
    dev_state_key,
)

from .transport.decode import devtype_name

_LOGGER = logging.getLogger(__name__)

# Типы ламп. 0102 — CCT (есть цветовая температура); остальные пока яркость.
LIGHT_TYPES = {"0101", "0102", "0103", "0104", "0105", "0106"}
CCT_TYPES = {"0102"}

DEV_BRI_MAX = 1000          # максимум яркости на шине (UI odc: 10..1000)
KELVIN_MIN = 2700
KELVIN_MAX = 6500
HOLD_MIN_ON = 1             # нижний предел яркости при dim-удержании (лампа НЕ гаснет, ~мин)


def _ha_to_dev_bri(ha_bri: int) -> int:
    """HA 1..255 → шлюз 1..1000."""
    return max(1, min(DEV_BRI_MAX, round(ha_bri / 255 * DEV_BRI_MAX)))


def _dev_to_ha_bri(dev_bri: int) -> int:
    """Шлюз 1..1000 → HA 1..255."""
    return max(1, min(255, round(dev_bri / DEV_BRI_MAX * 255)))


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Создать сущности-лампы по кешу устройств шлюза этой записи."""
    hub: DaliGatewayHub = hass.data[DOMAIN][entry.entry_id]
    entities: list[LightEntity] = []
    for dev in hub.devices_snapshot():
        if str(dev.get("devType")) in LIGHT_TYPES:
            custom = hub.custom_name(dev)
            entities.append(DaliLight(hub, hub.gw_sn, dev, custom))
    for g in hub.groups:
        entities.append(DaliGroupLight(hub, g))   # имя группы — с контроллера
    entities.append(DaliAllLights(hub))           # «все лампы шлюза» одной командой (v1.2.46)
    _LOGGER.info("%s [%s]: ламп %d, групп %d", DOMAIN, hub.gw_sn,
                 len(entities) - len(hub.groups) - 1, len(hub.groups))
    async_add_entities(entities)
    # позволяем добавлять light-сущности групп динамически (после create_group)
    hub.set_light_adder(async_add_entities)

    # динамика ламп: factory + adder в хабе → reconcile создаёт новые лампы без reload
    def _factory(dev):
        custom = hub.custom_name(dev)
        return DaliLight(hub, hub.gw_sn, dev, custom)
    hub.register_platform("light", async_add_entities, _factory)

    # ── КРОСС-ШЛЮЗОВЫЕ группы ────────────────────────────────────────────────
    # Они не принадлежат шлюзу, а платформа light поднимается на КАЖДУЮ ConfigEntry →
    # создаём их ровно один раз: «якорь» = алфавитно первый УЧАСТНИК. Если якорь ещё не
    # загружен, группу создаст его запись при своём старте (порядок записей не важен).
    from .store import get_cross_group_store
    xgs = get_cross_group_store(hass)
    x_entities: list[LightEntity] = []
    for xg in (xgs.all() if xgs else []):
        parts = sorted(str(p).upper() for p in xg.get("participants") or [])
        if parts and parts[0] == hub.gw_sn.upper():
            x_entities.append(DaliCrossGroupLight(hass, hub, xg))
    if x_entities:
        _LOGGER.info("%s [%s]: кросс-шлюзовых групп %d (эта запись — якорь)",
                     DOMAIN, hub.gw_sn, len(x_entities))
        async_add_entities(x_entities)


class DaliLight(DaliBusEntity, LightEntity, RestoreEntity):
    """Лампа DALI."""

    _attr_has_entity_name = False
    _role = "light"

    def __init__(self, hub: DaliGatewayHub, gw_sn: str, dev: dict, custom: str = "") -> None:
        self._hub = hub
        self._gw_sn = gw_sn
        self._devtype = str(dev.get("devType"))
        self._channel = dev.get("channel")
        self._address = dev.get("address")
        self._key = dev_state_key(self._devtype, self._channel, self._address)
        self._avail_key = self._key   # реальный online/offline из onlineStatus

        # ключ идентичности — ЕДИНЫМ методом хаба (см. `DaliGatewayHub.identity`):
        # у ламп исторический фолбэк включает devType, поэтому light=True
        uid = hub.identity(dev, light=True)
        self._attr_unique_id = uid

        if self._devtype in CCT_TYPES:
            self._attr_color_mode = ColorMode.COLOR_TEMP
            self._attr_supported_color_modes = {ColorMode.COLOR_TEMP}
            self._attr_min_color_temp_kelvin = KELVIN_MIN
            self._attr_max_color_temp_kelvin = KELVIN_MAX
        else:
            self._attr_color_mode = ColorMode.BRIGHTNESS
            self._attr_supported_color_modes = {ColorMode.BRIGHTNESS}

        # ИМЕНОВАННАЯ → подпись = custom; БЕЗЫМЯННАЯ → подпись НЕ задаём (v1.2.7), HA выведет её
        # из entity_id (форс coordinator: light_<addr>_<sn5>). Имя УСТРОЙСТВА — по devSn (стабильно).
        self._attr_name = custom or None
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, uid)},
            via_device=(DOMAIN, gw_sn),
            manufacturer="Sunricher",
            model=devtype_name(self._devtype),
            name=custom or hub.device_label(dev),   # режимное имя устройства (v1.2.74)
        )
        self._attr_is_on: bool | None = None
        self._attr_brightness: int | None = None

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()   # RestoreEntity: подключить рестор-стейт
        # Восстановить ПОСЛЕДНЕЕ состояние через рестарт HA. Лампа — slave, реального
        # состояния шлюз сам не шлёт сразу → без рестора висела бы «Неизвестно» до первой
        # команды/devStatus. Берём оптимистично последнее (B-Q1): энергоинтегратор заякорит
        # его первым _emit_state. Реальное подтянется devStatus'ом, если придёт.
        last = await self.async_get_last_state()
        if last is not None and last.state in ("on", "off"):
            self._attr_is_on = last.state == "on"
            bri = last.attributes.get(ATTR_BRIGHTNESS)
            if bri is not None:
                self._attr_brightness = int(bri)
            cct = last.attributes.get(ATTR_COLOR_TEMP_KELVIN)
            if cct is not None and self._devtype in CCT_TYPES:
                self._attr_color_temp_kelvin = int(cct)
        self.async_on_remove(
            async_dispatcher_connect(self.hass, SIGNAL_DEV_UPDATE, self._on_update)
        )
        self._wire_avail()   # доступность: связь шлюза + online устройства
        self._bus_register()  # трекинг в хабе (reconcile/resync адреса/gone + резолв в карточке)
        self._emit_state()   # засеять группы восстановленным состоянием (+ заякорить энергоучёт)

    def _emit_state(self) -> None:
        """Сообщить своё оптимистичное состояние подписчику SIGNAL_LAMP_STATE (энергоучёт;
        группы с v1.2.3 слушают лампы штатно, не через этот сигнал). brightness — HA 1..255/None.

        v1.2.19 (F6): 5-м полем несём ДОСТУПНОСТЬ (`self.available`) — недоступную лампу
        энергоучёт снимает с учёта (см. energy/integrator). Поэтому `_emit_state` зовётся и на
        смене доступности (см. `_avail_on_conn`/`_avail_on_dev`/`mark_gone` ниже)."""
        if self.hass:
            async_dispatcher_send(self.hass, SIGNAL_LAMP_STATE, self._gw_sn, self._key,
                                  self._attr_is_on, self._attr_brightness, self.available)

    # ── F6 (v1.2.19): смена ДОСТУПНОСТИ → сообщить энергоучёту ────────────────────
    # Availability меняют связь шлюза / onlineStatus (миксин) и скан (`mark_gone`) — все они
    # звали только `async_write_ha_state()`, МИМО `_emit_state`, поэтому интегратор не узнавал,
    # что лампа ушла в `unavailable`, и продолжал копить энергию (flush_open). Здесь дублируем
    # сигнал энергоучёту: недоступна → он закроет отрезок и снимет лампу с учёта; вернулась →
    # откроет свежий. Только у ЛАМП (SIGNAL_LAMP_STATE — их сигнал; датчики его не шлют).
    @callback
    def _avail_on_conn(self, gw_sn: str, state: str) -> None:
        super()._avail_on_conn(gw_sn, state)
        if gw_sn == self._gw_sn:
            self._emit_state()

    @callback
    def _avail_on_dev(self, gw_sn: str, key: str, online: bool) -> None:
        super()._avail_on_dev(gw_sn, key, online)
        if gw_sn == self._gw_sn and key == self._avail_key:
            self._emit_state()

    @callback
    def mark_gone(self) -> None:
        was = self._gone
        super().mark_gone()
        if self._gone != was:
            self._emit_state()

    @callback
    def apply_group_optimistic(self, is_on: bool, brightness: int | None) -> None:
        """G1: группа физически изменила эту лампу (writeGroup) → отразить в сущности
        лампы (writeDev на лампу при этом НЕ шлём — её уже накрыла групповая команда)."""
        self._attr_is_on = is_on
        if brightness is not None:
            self._attr_brightness = brightness
        if self.hass:
            self.async_write_ha_state()
        self._emit_state()

    @callback
    def nudge_brightness(self, delta_ha: float) -> None:
        """Баг2: нативная кнопочная рампа (dpid25/26) изменила яркость лампы АВТОНОМНО
        (рампу ведёт контроллер) — команду НЕ шлём, только ОЦЕНИВАЕМ результат и отражаем
        в сущности. `delta_ha` — оценка сдвига в шкале HA (0..255, знак = ярче/темнее).

        ВАЖНО (проверено на железе): плавное ярче/темнее НЕ включает и НЕ выключает лампы —
        выключенную не зажигает, включённую в ноль не гасит (держит физический минимум ~1%).
        Поэтому: выкл → игнорируем (dim на погашенной лампе ничего не делает); вкл → двигаем
        в пределах [HOLD_MIN_ON..255], в выкл НЕ роняем (иначе теряем точку отсчёта и оценка
        начинает отставать). Любой реальный devStatus/turn_on затем перетрёт оценку истиной."""
        if not self._attr_is_on:
            return
        base = self._attr_brightness if self._attr_brightness is not None else 255
        new = int(round(max(HOLD_MIN_ON, min(255, base + delta_ha))))
        self._attr_brightness = new
        if self.hass:
            self.async_write_ha_state()
        self._emit_state()   # группы пересчитают агрегат, энергоучёт заякорит оценку

    @callback
    def _on_update(self, gw_sn: str, key: str, data: dict) -> None:
        """Обновить состояние по событию devStatus."""
        if gw_sn != self._gw_sn or key != self._key:
            return
        for p in data.get("property", []) or []:
            dpid, val = p.get("dpid"), p.get("value")
            if dpid == 20:
                self._attr_is_on = bool(val)
            elif dpid == 22 and val is not None:
                v = int(val)
                if v <= 0:                   # арк-мощность 0 = ВЫКЛ (DALI), не «вкл 0%»
                    self._attr_is_on = False
                else:
                    self._attr_brightness = _dev_to_ha_bri(v)
                    self._attr_is_on = True
            elif dpid == 23 and val is not None:
                self._attr_color_temp_kelvin = max(KELVIN_MIN, min(KELVIN_MAX, int(val)))
        self.async_write_ha_state()
        self._emit_state()   # devStatus от шлюза → группы пересчитают агрегат

    async def _write(self, props: list[dict]) -> bool:
        res = await self._hub.async_request(
            "writeDev", "writeDevRes",
            data=[{"devType": self._devtype, "channel": self._channel,
                   "address": self._address, "property": props}])
        return bool(res and res.get("ack"))

    async def async_turn_on(self, **kwargs: Any) -> None:
        props: list[dict] = []
        if ATTR_BRIGHTNESS in kwargs:
            dev_bri = _ha_to_dev_bri(int(kwargs[ATTR_BRIGHTNESS]))
            props.append({"dpid": 20, "dataType": "bool", "value": True})
            props.append({"dpid": 22, "dataType": "uint16", "value": dev_bri})
        else:
            props.append({"dpid": 20, "dataType": "bool", "value": True})
        if ATTR_COLOR_TEMP_KELVIN in kwargs:
            k = max(KELVIN_MIN, min(KELVIN_MAX, int(kwargs[ATTR_COLOR_TEMP_KELVIN])))
            props.append({"dpid": 23, "dataType": "uint16", "value": k})

        await self._write(props)
        # оптимистично (лампы редко шлют статус сами)
        self._attr_is_on = True
        if ATTR_BRIGHTNESS in kwargs:
            self._attr_brightness = int(kwargs[ATTR_BRIGHTNESS])
        if ATTR_COLOR_TEMP_KELVIN in kwargs:
            self._attr_color_temp_kelvin = max(
                KELVIN_MIN, min(KELVIN_MAX, int(kwargs[ATTR_COLOR_TEMP_KELVIN])))
        self.async_write_ha_state()
        self._emit_state()   # индивидуальная команда → группы пересчитают агрегат (G2)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._write([{"dpid": 20, "dataType": "bool", "value": False}])
        self._attr_is_on = False
        self.async_write_ha_state()
        self._emit_state()


class DaliGroupLight(DaliAvailMixin, LightEntity):
    """DALI-группа как light-сущность HA — ГИБРИД (G1/G2).

    Команды — нативный `writeGroup` (одна шинная команда, эффективно). При команде
    РАСПРОСТРАНЯЕМ оптимистичное состояние на сущности ламп-членов (G1) — иначе после
    `writeGroup off` лампы показывали мусор (нет devStatus от slave-ламп). Состояние
    группы — АГРЕГАТ из членов (G2): вкл = хоть один член вкл; яркость = среднее по
    включённым. Это правдиво и для штатных сущностей, и в карточке. (Раньше агрегацию
    убрали и держали чисто оптимистично — группа и лампы были рассинхронизированы; корень
    был в отсутствии распространения, а не в агрегации.)

    ⚠ S1 (v1.2.3) — КАК группа слышит свои лампы. Раньше: подписка на `SIGNAL_LAMP_STATE` —
    ДИСПЕТЧЕР, то есть широковещалка БЕЗ КЛЮЧА: сигнал один на весь `hass`, поэтому HA звал
    колбэк КАЖДОЙ групповой сущности КАЖДОГО шлюза на КАЖДОЕ движение любой лампы, а «моё/не
    моё» отсеивалось уже внутри колбэка. На объекте (16 групп × 60 шлюзов ≈ 960 групп,
    автояркость пушит ~180 событий/с) это ~170 000 холостых вызовов в секунду — постоянный
    налог на петлю HA.

    Теперь — как штатная `LightGroup` в HA: `async_track_state_change_event` по `entity_id`
    ламп-членов. Это НЕ широковещалка: HA держит индекс `entity_id → колбэки` и разводит
    события по ключу ДО вызова, поэтому группа получает события ТОЛЬКО своих ламп, а холостых
    вызовов нет вовсе. `SIGNAL_LAMP_STATE` остаётся — но его слушает только энергоучёт
    (один интегратор на `hass`, поламповый), и для одного подписчика широковещалка безвредна.

    ⚠ Подписка идёт по `entity_id`, а он у нас МЕНЯЕТСЯ (форс при перераздаче адресов,
    ренейм) → после каждой такой правки хаб зовёт `resubscribe_members()`, иначе подписка
    осталась бы висеть на мёртвом id и группа «оглохла» бы.
    """

    _attr_has_entity_name = False
    _attr_color_mode = ColorMode.BRIGHTNESS
    _attr_supported_color_modes = {ColorMode.BRIGHTNESS}

    def __init__(self, hub: DaliGatewayHub, group: dict) -> None:
        self._hub = hub
        self._gw_sn = hub.gw_sn
        self._channel = group["channel"]
        self._group_id = group["groupId"]
        self._members = group.get("members", [])
        self._present = group.get("present", True)   # есть ли группа на контроллере
        # ключи членов devType:ch:addr — для распространения (G1) и агрегации (G2)
        self._member_keys = {
            dev_state_key(str(m.get("devType")), m.get("channel"), m.get("address"))
            for m in self._members}
        self._member_state: dict[str, tuple[bool, int | None]] = {}  # key → (on, bri)
        # S1 (v1.2.3): адресная подписка на лампы-члены по entity_id (вместо широковещалки)
        self._eid_key: dict[str, str] = {}       # entity_id лампы → её ключ в кеше
        self._unsub_members = None               # отписка от async_track_state_change_event
        uid = f"{hub.gw_sn}_group_{self._channel}_{self._group_id}"
        self._attr_unique_id = uid
        # имя — СТРОГО с контроллера (getGroup/setGroupName) → дефолт. NameStore для групп
        # НЕ используем: он залипал за groupId и затенял настоящее имя (хаос с именами).
        name = group.get("name") or f"group_{self._group_id}"
        self._attr_name = name
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, uid)},
            via_device=(DOMAIN, hub.gw_sn),
            manufacturer="Sunricher",
            model="DALI Group",
            name=name,
        )
        self._attr_is_on = False
        self._attr_brightness: int | None = None

    @property
    def available(self) -> bool:
        # доступна, если связь со шлюзом есть И группа реально присутствует на контроллере
        return super().available and self._present

    @callback
    def update_present(self, present: bool) -> None:
        """Группа снова найдена/потеряна на контроллере (повторный getGroup на реконнекте) →
        обновить доступность УЖЕ созданной сущности. Раньше `_present` фиксировался при
        СОЗДАНИИ → после оффлайн-старта + реконнекта группа залипала «не на связи», хотя
        контроллер её уже отдавал."""
        if present != self._present:
            self._present = present
            if self.hass:
                self.async_write_ha_state()

    async def async_added_to_hass(self) -> None:
        self._wire_avail()   # доступность группы — по связи шлюза
        # трекинг живой сущности — чтобы её можно было корректно УДАЛИТЬ при del/recreate
        self._hub.register_group_entity(self._channel, self._group_id, self)
        # G2 (S1, v1.2.3): слушаем СОСТОЯНИЯ СВОИХ ламп по entity_id — адресно, без fan-out
        self.resubscribe_members()
        # начальный посев: подтянуть текущее состояние живых ламп-членов
        for key in self._member_keys:
            ent = self._hub.live_entity("light", key)
            if ent is not None:
                self._member_state[key] = (bool(ent.is_on), ent.brightness)
        self._recompute(write=False)

    async def async_will_remove_from_hass(self) -> None:
        self._hub.unregister_group_entity(self._channel, self._group_id, self)
        if self._unsub_members:
            self._unsub_members()
            self._unsub_members = None

    @callback
    def resubscribe_members(self) -> None:
        """Переподписаться на состояния ламп-членов по их ТЕКУЩИМ `entity_id`.

        Зовётся хабом после всего, что могло изменить состав или `entity_id` ламп: reconcile
        (создание/переезд сущностей), форс `entity_id` при перераздаче адресов, ренейм. Без
        этого подписка осталась бы висеть на старом id — группа молча перестала бы обновляться.
        Дёшево: групп на шлюзе ≤16, ламп ≤64 (лимит DALI)."""
        if self.hass is None:
            return
        if self._unsub_members:                 # снять прежнюю подписку (id могли смениться)
            self._unsub_members()
            self._unsub_members = None
        # entity_id членов берём у ЖИВЫХ сущностей ламп (они и есть истина состояния)
        self._eid_key = {}
        for key in self._member_keys:
            ent = self._hub.live_entity("light", key)
            eid = getattr(ent, "entity_id", None) if ent is not None else None
            if eid:
                self._eid_key[eid] = key
        if not self._eid_key:
            return                              # лампы ещё не созданы → подпишемся на reconcile
        self._unsub_members = async_track_state_change_event(
            self.hass, list(self._eid_key), self._on_member_state)

    @callback
    def _on_member_state(self, event) -> None:
        """G2: лампа-член сменила состояние → обновить кеш и пересчитать агрегат.

        Приходит ТОЛЬКО по нашим лампам (HA развёл событие по `entity_id` до вызова)."""
        new = event.data.get("new_state")
        key = self._eid_key.get(event.data.get("entity_id"))
        if key is None or new is None:
            return
        if new.state in ("unavailable", "unknown"):
            return                              # лампа недоступна — состояние не трогаем
        self._member_state[key] = (new.state == "on", new.attributes.get("brightness"))
        self._recompute()

    @callback
    def _recompute(self, write: bool = True) -> None:
        """Агрегат группы из известных состояний членов: вкл = хоть один вкл;
        яркость = среднее по включённым (как штатная световая группа HA)."""
        if not self._member_state:
            return   # членов не отследить → оставляем последнее (оптимистичное) состояние
        on_states = [st for st in self._member_state.values() if st[0]]
        is_on = bool(on_states)
        bris = [st[1] for st in on_states if st[1] is not None]
        bri = round(sum(bris) / len(bris)) if bris else self._attr_brightness
        changed = (is_on != self._attr_is_on) or (is_on and bri != self._attr_brightness)
        self._attr_is_on = is_on
        if is_on and bri is not None:
            self._attr_brightness = bri
        if write and changed and self.hass:
            self.async_write_ha_state()

    def _propagate(self, is_on: bool, brightness: int | None) -> None:
        """G1: отразить групповую команду в сущностях ламп-членов (без writeDev — их
        уже накрыл writeGroup). Лампы при этом сами шлют SIGNAL_LAMP_STATE → агрегат."""
        for key in self._member_keys:
            ent = self._hub.live_entity("light", key)
            if ent is not None:
                ent.apply_group_optimistic(is_on, brightness)

    async def _write(self, props: list[dict]) -> None:
        await self._hub.async_request("writeGroup", "writeGroupRes",
                                      channel=self._channel, groupId=self._group_id, data=props)

    async def async_turn_on(self, **kwargs: Any) -> None:
        props = [{"dpid": 20, "dataType": "bool", "value": True}]
        bri = None
        if ATTR_BRIGHTNESS in kwargs:
            bri = int(kwargs[ATTR_BRIGHTNESS])
            props.append({"dpid": 22, "dataType": "uint16", "value": _ha_to_dev_bri(bri)})
        await self._write(props)
        self._propagate(True, bri)   # G1: лампы-члены → вкл (+ яркость) → агрегат (G2)
        # подстраховка, если членов не отследить: показать команду оптимистично
        self._attr_is_on = True
        if bri is not None:
            self._attr_brightness = bri
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._write([{"dpid": 20, "dataType": "bool", "value": False}])
        self._propagate(False, None)   # G1: лампы-члены → выкл → агрегат (G2)
        self._attr_is_on = False
        self.async_write_ha_state()


class DaliAllLights(DaliAvailMixin, LightEntity):
    """«Все лампы контроллера» — ОДНА команда `writeDev` на broadcast-адрес (v1.2.46).

    Зачем: выключить весь свет шлюза можно было только перебором ламп (60 ламп = 60 команд
    подряд по одной шине). Броадкаст — один кадр: гаснет разом и шину не забивает.

    Форма команды: `devType:"FFFF"`, `channel 0`, `address 1` — ⚠ взята из **upstream-SDK**
    (`AllLightsController`, PySrDaliGateway), НЕ из нашего захвата. Что `FFFF` — броадкаст,
    подтверждено дважды: наш захват `setDevParam` 2026-08-06 и симуляция нажатия панели
    (docs/PROTOCOL.md). Но конкретно `address 1` у `writeDev` мы своими глазами не видели —
    гейт **G44**, до проверки на железе так и писать.

    Уровень доверия — 🔴 ОПТИМИСТИЧНО, как у обычной лампы: `ack` не доказывает исполнения,
    а `readDev` врёт (закон 2). Состояние поэтому не агрегируем, а ставим сами и
    РАСПРОСТРАНЯЕМ на сущности ламп шлюза (иначе после «выключить всё» лампы показывали бы
    старое — та же болезнь, что лечил G1 у групп).
    """

    _attr_has_entity_name = False
    _attr_icon = "mdi:lightbulb-group-outline"
    _attr_color_mode = ColorMode.BRIGHTNESS
    _attr_supported_color_modes = {ColorMode.BRIGHTNESS}
    _avail_key = None            # доступность = связь шлюза (устройства-владельца нет)

    BROADCAST_DEVTYPE = "FFFF"
    BROADCAST_CHANNEL = 0
    BROADCAST_ADDRESS = 1

    def __init__(self, hub: DaliGatewayHub) -> None:
        self._hub = hub
        self._gw_sn = hub.gw_sn
        self._attr_unique_id = f"{hub.gw_sn}_all_lights"
        # имя латиницей с хвостом серийника — как в схеме именования v1.2.7 (sn5 разводит
        # «коридоры», драки за entity_id между шлюзами невозможны)
        self._attr_name = f"all_lights_{hub.gw_sn[-5:].lower()}"
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, hub.gw_sn)})

    async def async_added_to_hass(self) -> None:
        self._wire_avail()

    def _lamp_keys(self) -> list[str]:
        return [dev_state_key(str(d.get("devType")), d.get("channel"), d.get("address"))
                for d in self._hub.devices_snapshot()
                if str(d.get("devType")) in LIGHT_TYPES]

    def _propagate(self, is_on: bool, brightness: int | None) -> None:
        """Отразить броадкаст в сущностях ламп: команда их накрыла, writeDev им не шлём."""
        for key in self._lamp_keys():
            ent = self._hub.live_entity("light", key)
            if ent is not None:
                ent.apply_group_optimistic(is_on, brightness)

    async def _write(self, props: list[dict]) -> None:
        await self._hub.async_request(
            "writeDev", "writeDevRes",
            data=[{"devType": self.BROADCAST_DEVTYPE, "channel": self.BROADCAST_CHANNEL,
                   "address": self.BROADCAST_ADDRESS, "property": props}])

    async def async_turn_on(self, **kwargs: Any) -> None:
        props = [{"dpid": 20, "dataType": "bool", "value": True}]
        bri = None
        if ATTR_BRIGHTNESS in kwargs:
            bri = int(kwargs[ATTR_BRIGHTNESS])
            props.append({"dpid": 22, "dataType": "uint16", "value": _ha_to_dev_bri(bri)})
        await self._write(props)
        _LOGGER.info("шлюз %s: БРОАДКАСТ вкл%s", self._gw_sn,
                     f" (яркость {bri})" if bri is not None else "")
        self._propagate(True, bri)
        self._attr_is_on = True
        if bri is not None:
            self._attr_brightness = bri
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._write([{"dpid": 20, "dataType": "bool", "value": False}])
        _LOGGER.info("шлюз %s: БРОАДКАСТ выкл", self._gw_sn)
        self._propagate(False, None)
        self._attr_is_on = False
        self.async_write_ha_state()


class DaliCrossGroupLight(LightEntity):
    """КРОСС-ШЛЮЗОВАЯ DALI-группа — одна сущность HA поверх копий на нескольких контроллерах.

    ⚠ Отдельная модель от `DaliGroupLight` (решение 2026-08-04): та принадлежит одному хабу
    (`unique_id` от `gw_sn`), а здесь группы у каждого участника СВОИ, склеенные общим
    `groupId`+именем (захват 2026-08-04, docs/CROSS_GATEWAY.md §2).

    - **Команда** — `writeGroup` ВЕЕРОМ на каждого участника: ретранслятора нет, каждый
      контроллер бьёт только по своим лампам.
    - **Состояние** — агрегат из ламп-членов по их `entity_id` (как у обычной группы, G2);
      лампы живут на РАЗНЫХ хабах, поэтому сущности резолвим через все хабы.
    - **`unique_id`** берём ИЗ СТОРА (зафиксирован при создании) — пересчитывать от живого
      состава нельзя: правка состава увела бы id, и HA завёл бы новую сущность (закон 2).
    - **Доступность:** хотя бы ОДИН участник на связи. Полная недоступность при одном
      упавшем шлюзе заблокировала бы работающую половину помещения.
    """

    _attr_has_entity_name = False
    _attr_color_mode = ColorMode.BRIGHTNESS
    _attr_supported_color_modes = {ColorMode.BRIGHTNESS}

    def __init__(self, hass: HomeAssistant, anchor_hub: DaliGatewayHub, xg: dict) -> None:
        self.hass = hass
        self._anchor = anchor_hub
        self._uid = xg["uid"]
        self._channel = xg["channel"]
        self._group_id = xg["groupId"]
        self._participants = list(xg.get("participants") or [])
        self._members = list(xg.get("members") or [])
        self._member_state: dict[str, tuple[bool, int | None]] = {}
        self._eid_key: dict[str, str] = {}
        self._unsub_members = None
        self._attr_unique_id = self._uid
        name = xg.get("name") or f"xgroup_{self._group_id}"
        self._attr_name = name
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, self._uid)},
            manufacturer="Sunricher",
            model=f"DALI Cross-Group ({len(self._participants)} контроллера)",
            name=name,
        )

    @property
    def participants(self) -> list:
        """Серийники шлюзов-участников — по ним хаб находит «свои» кросс-группы."""
        return self._participants

    @callback
    def update_from_store(self, xg: dict) -> None:
        """Применить правку состава/имени БЕЗ пересоздания сущности (v1.2.43).

        `unique_id` при правке не меняется намеренно (он зафиксирован при создании), поэтому
        и сущность пересоздавать нельзя — надо обновить её на месте и пересобрать подписку на
        лампы: состав другой, значит и набор `entity_id` другой."""
        self._participants = list(xg.get("participants") or [])
        self._members = list(xg.get("members") or [])
        name = xg.get("name") or self._attr_name
        self._attr_name = name
        self._member_state = {}          # состав сменился — старые состояния не наши
        self.resubscribe_members()
        self._force_entity_id()          # имя могло смениться — id обязан пойти следом

    # ── участники и лампы ────────────────────────────────────────────────────
    def _hubs(self) -> list:
        """Живые хабы участников (шлюз мог быть удалён из HA — тогда его просто нет)."""
        out = []
        for gw in self._participants:
            for hub in (self.hass.data.get(DOMAIN) or {}).values():
                if getattr(hub, "gw_sn", "").upper() == str(gw).upper():
                    out.append(hub)
                    break
        return out

    def _member_entities(self) -> dict:
        """`entity_id` ламп-членов → ключ, по всем шлюзам-участникам."""
        eids: dict = {}
        for hub in self._hubs():
            gw = hub.gw_sn.upper()
            for m in self._members:
                if str(m.get("gwSnObj") or "").upper() != gw:
                    continue
                key = dev_state_key(str(m.get("devType")), m.get("channel"), m.get("address"))
                ent = hub.live_entity("light", key)
                eid = getattr(ent, "entity_id", None) if ent is not None else None
                if eid:
                    eids[eid] = key
        return eids

    @property
    def available(self) -> bool:
        # хотя бы один участник на связи (см. докстринг класса)
        return any(getattr(h, "connected", False) for h in self._hubs())

    async def async_added_to_hass(self) -> None:
        # ⚠ В ОБЩИЙ реестр (v1.2.42): подписка на лампы живёт по `entity_id`, а он меняется
        # (форс при перераздаче адресов, ренейм), и на СТАРТЕ наша сущность создаётся якорем
        # РАНЬШЕ, чем поднимутся лампы другого шлюза. Каждый хаб после подъёма своих платформ
        # зовёт `resubscribe_groups()` и забирает из реестра группы, где участвует сам —
        # так подписка и достраивается, независимо от порядка загрузки записей.
        # Без этого кросс-группа МОЛЧА не видела своих ламп.
        self.hass.data.setdefault(XGROUP_ENTITIES, set()).add(self)
        self.resubscribe_members()
        self._force_entity_id()

    @callback
    def _force_entity_id(self) -> None:
        """`entity_id` кросс-группы обязан следовать за ИМЕНЕМ (v1.2.58).

        🔴 Симптом с офиса 2026-08-11: группа создана как `103_dver_obshchii`, а сущность
        зовётся `light.cross_1_2`. Причина — ЗАКОН 1 (удаление в реестрах HA мягкое): наш
        `unique_id` считается от участников+канала+номера и потому СТАБИЛЕН, а в корзине
        (`deleted_entities`) лежала запись с тем же `unique_id` от прежней группы, которую
        когда-то звали `cross_1`. HA воскресил её вместе со старым `entity_id`, и имя с
        адресом сущности разъехались: автоматизации и назначение области бьют мимо.

        У устройств такой форс есть давно (`coordinator._force_entity_id`, Fix W/J), у
        однолшлюзовых групп — в `ws_rename_group`; кросс-группы остались без него. Логика та
        же и с тем же гейтом: **чужое не отбираем**. Освобождаем желаемый id, только если его
        держит НАША ЖЕ мёртвая запись кросс-группы (префикс `xgrp_`), иначе громко ругаемся
        и остаёмся как есть — молча увести id живого соседа нельзя.
        """
        from homeassistant.helpers import entity_registry as er

        name = self._attr_name or ""
        desired = f"light.{slugify(name)}" if name else ""
        if not desired or not self.entity_id or self.entity_id == desired:
            return
        reg = er.async_get(self.hass)
        holder = reg.async_get(desired)
        if holder is not None and holder.unique_id != self._uid:
            # держатель — осиротевшая запись ДРУГОЙ кросс-группы: её сущности нет в реестре
            # живых (XGROUP_ENTITIES), значит она пустая и место можно освободить
            live = {getattr(e, "unique_id", None)
                    for e in self.hass.data.get(XGROUP_ENTITIES, ())}
            if str(holder.unique_id).startswith("xgrp_") and holder.unique_id not in live:
                _LOGGER.info("xgroup %s: освобождаю %s от мёртвой записи %s",
                             self._uid, desired, holder.unique_id)
                reg.async_remove(desired)
            else:
                _LOGGER.warning("xgroup %s: желаемый %s занят живой сущностью (%s) — "
                                "оставляю %s, имя и entity_id разойдутся",
                                self._uid, desired, holder.unique_id, self.entity_id)
                return
        try:
            reg.async_update_entity(self.entity_id, new_entity_id=desired)
            _LOGGER.info("xgroup %s: entity_id %s → %s (следует за именем «%s»)",
                         self._uid, self.entity_id, desired, name)
        except (ValueError, KeyError) as err:      # занят/некорректен — не рушим загрузку
            _LOGGER.warning("xgroup %s: не смог перевести entity_id в %s: %s",
                            self._uid, desired, err)

    async def async_will_remove_from_hass(self) -> None:
        self.hass.data.get(XGROUP_ENTITIES, set()).discard(self)
        if self._unsub_members:
            self._unsub_members()
            self._unsub_members = None

    @callback
    def resubscribe_members(self) -> None:
        """Переподписка по ТЕКУЩИМ `entity_id` ламп + пересев их состояния.

        Пересев здесь, а не только при добавлении: подписка может собраться позже (лампы
        другого шлюза появились после нас), и без пересева агрегат остался бы пустым до
        первого щелчка лампы."""
        if self.hass is None:
            return
        if self._unsub_members:
            self._unsub_members()
            self._unsub_members = None
        self._eid_key = self._member_entities()
        if not self._eid_key:
            return
        self._unsub_members = async_track_state_change_event(
            self.hass, list(self._eid_key), self._on_member_state)
        for eid, key in self._eid_key.items():
            st = self.hass.states.get(eid)
            if st is not None and st.state not in ("unavailable", "unknown"):
                self._member_state[key] = (st.state == "on", st.attributes.get("brightness"))
        self._recompute(write=False)

    @callback
    def _on_member_state(self, event) -> None:
        new = event.data.get("new_state")
        key = self._eid_key.get(event.data.get("entity_id"))
        if key is None or new is None or new.state in ("unavailable", "unknown"):
            return
        self._member_state[key] = (new.state == "on", new.attributes.get("brightness"))
        self._recompute()

    @callback
    def _recompute(self, write: bool = True) -> None:
        if not self._member_state:
            return
        on_states = [st for st in self._member_state.values() if st[0]]
        is_on = bool(on_states)
        bris = [st[1] for st in on_states if st[1] is not None]
        bri = round(sum(bris) / len(bris)) if bris else self._attr_brightness
        self._attr_is_on = is_on
        if is_on and bri is not None:
            self._attr_brightness = bri
        if write and self.hass:
            self.async_write_ha_state()

    # ── управление: writeGroup ВЕЕРОМ ────────────────────────────────────────
    async def _write(self, props: list[dict]) -> None:
        """`writeGroup` каждому участнику. Недоступный шлюз — WARNING, а не тишина:
        половина помещения не отработает, и это должно быть видно в журнале."""
        hubs = self._hubs()
        seen = {h.gw_sn.upper() for h in hubs}
        for gw in self._participants:
            if str(gw).upper() not in seen:
                _LOGGER.warning("кросс-группа %s: контроллер %s недоступен — его лампы "
                                "команду НЕ получили", self._uid, gw)
        for hub in hubs:
            await hub.async_request("writeGroup", "writeGroupRes", channel=self._channel,
                                    groupId=self._group_id, data=props)

    async def async_turn_on(self, **kwargs: Any) -> None:
        props = [{"dpid": 20, "dataType": "bool", "value": True}]
        bri = None
        if ATTR_BRIGHTNESS in kwargs:
            bri = int(kwargs[ATTR_BRIGHTNESS])
            props.append({"dpid": 22, "dataType": "uint16", "value": _ha_to_dev_bri(bri)})
        await self._write(props)
        self._attr_is_on = True                  # оптимистично: лампы подтвердят агрегатом
        if bri is not None:
            self._attr_brightness = bri
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._write([{"dpid": 20, "dataType": "bool", "value": False}])
        self._attr_is_on = False
        self.async_write_ha_state()
