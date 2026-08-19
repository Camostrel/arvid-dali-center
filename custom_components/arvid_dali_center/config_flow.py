"""Config flow: запись ConfigEntry НА КАЖДЫЙ шлюз (отдельная карточка в HA).

Пользователь запускает: discovery находит все шлюзы → показываем СПИСОК, пользователь
ВЫБИРАЕТ, какие подключить (чекбоксы). Выбранные создаются записями: первый — этим
потоком (у него HA спросит область), остальные — через source=import (без промпта
области — назначить потом в настройках устройства). Каждая запись = свой контроллер со
своим списком устройств. Креды НЕ храним (динамические).
"""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import (
    SOURCE_IMPORT,
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.helpers import config_validation as cv

from .const import CONF_BIND_IP, CONF_GW_SN, DOMAIN
from .identity import MODE_ADDR, MODE_DEVSN
from .transport.core import auto_iface, discover_all

_LOGGER = logging.getLogger(__name__)


class ArvidDaliConfigFlow(ConfigFlow, domain=DOMAIN):
    """Поток настройки: одна запись на шлюз, выбор контроллеров вручную."""

    VERSION = 1

    @staticmethod
    def async_get_options_flow(config_entry: ConfigEntry) -> "ArvidDaliOptionsFlow":
        return ArvidDaliOptionsFlow()

    def __init__(self) -> None:
        # переносим между шагами user → select
        self._found: list[dict] = []   # новые (ещё не добавленные) найденные шлюзы
        self._bind_ip: str = ""

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Шаг 1: интерфейс для поиска → discovery → передать список на выбор."""
        errors: dict[str, str] = {}
        if user_input is not None:
            bind_ip = (
                user_input.get(CONF_BIND_IP)
                or await self.hass.async_add_executor_job(auto_iface)
                or "0.0.0.0"
            )
            found = await self.hass.async_add_executor_job(
                lambda: discover_all(bind_ip=bind_ip, timeout=8.0)
            )
            existing = {e.unique_id for e in self._async_current_entries()}
            new = [g for g in found if g.get("gwSn") and g["gwSn"] not in existing]
            if not found:
                errors["base"] = "no_gateways"
            elif not new:
                return self.async_abort(reason="already_configured")
            else:
                _LOGGER.info("новых шлюзов %d из %d найденных", len(new), len(found))
                self._found = new
                self._bind_ip = bind_ip
                return await self.async_step_select()

        default_ip = await self.hass.async_add_executor_job(auto_iface) or ""
        schema = vol.Schema({vol.Optional(CONF_BIND_IP, default=default_ip): str})
        return self.async_show_form(step_id="user", data_schema=schema, errors=errors)

    async def async_step_select(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Шаг 2: список найденных контроллеров чекбоксами → создать выбранные."""
        errors: dict[str, str] = {}
        # метки: gwSn · имя · IP (что есть)
        options: dict[str, str] = {}
        for g in self._found:
            sn = g["gwSn"]
            label = sn
            if g.get("name"):
                label += f" · {g['name']}"
            if g.get("gwIp"):
                label += f" · {g['gwIp']}"
            options[sn] = label

        if user_input is not None:
            chosen = user_input.get("gateways") or []
            if not chosen:
                errors["base"] = "no_selection"
            else:
                # остальные выбранные — отдельными записями через import (без промпта области)
                for sn in chosen[1:]:
                    self.hass.async_create_task(
                        self.hass.config_entries.flow.async_init(
                            DOMAIN, context={"source": SOURCE_IMPORT},
                            data={CONF_GW_SN: sn, CONF_BIND_IP: self._bind_ip})
                    )
                first = chosen[0]
                await self.async_set_unique_id(first)
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=f"DALI Gateway {first}",
                    data={CONF_GW_SN: first, CONF_BIND_IP: self._bind_ip},
                )

        # по умолчанию отмечены все найденные новые
        schema = vol.Schema({
            vol.Required("gateways", default=list(options)): cv.multi_select(options),
        })
        return self.async_show_form(step_id="select", data_schema=schema, errors=errors)

    async def async_step_import(self, data: dict[str, Any]) -> ConfigFlowResult:
        """Создать запись для ещё одного найденного шлюза."""
        await self.async_set_unique_id(data[CONF_GW_SN])
        self._abort_if_unique_id_configured()
        return self.async_create_entry(
            title=f"DALI Gateway {data[CONF_GW_SN]}", data=data
        )


class ArvidDaliOptionsFlow(OptionsFlow):
    """Настройки интеграции. Сейчас здесь ровно одно — РЕЖИМ ИДЕНТИЧНОСТИ.

    ⚠ Почему настройка живёт тут, а не в рабочей карточке: её дёргают один раз на объекте и
    больше не трогают. В карточке, среди ежедневных кнопок, такому переключателю не место —
    он деструктивный (см. `identity_ops`). Карточка режим только ПОКАЗЫВАЕТ.

    ⚠ Режим — ОДИН НА ОБЪЕКТ (решение 2026-08-19), хотя записей у нас по одной на шлюз.
    Поэтому меняется он не в опциях записи, а в общем хранилище: открыть можно из настроек
    любого контроллера, результат один и тот же.
    """

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        from . import identity_ops
        info = identity_ops.scope(self.hass)
        if user_input is not None:
            mode = user_input["identity_mode"]
            if mode == info["mode"]:
                return self.async_create_entry(title="", data={})
            if not user_input.get("confirm"):
                return self.async_show_form(
                    step_id="init", data_schema=self._schema(info),
                    errors={"base": "confirm_required"},
                )
            res = await identity_ops.switch_mode(self.hass, mode)
            _LOGGER.warning("режим идентичности переключён через настройки: %s", res)
            return self.async_create_entry(title="", data={})
        return self.async_show_form(step_id="init", data_schema=self._schema(info),
                                    description_placeholders={
                                        "devices": str(info["devices"]),
                                        "entities": str(info["entities"]),
                                        "cards": str(info["device_cards"]),
                                    })

    @staticmethod
    def _schema(info: dict) -> vol.Schema:
        return vol.Schema({
            vol.Required("identity_mode", default=info["mode"]): vol.In({
                MODE_DEVSN: "По серийнику устройства (штатный)",
                MODE_ADDR: "По DALI-адресу (серийникам верить нельзя)",
            }),
            vol.Optional("confirm", default=False): bool,
        })
