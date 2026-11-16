"""Config flow. Validates against wire-pod's robot list, never the camera, so a
sleeping robot can be added. Authored-by: CPCS."""

from __future__ import annotations

from typing import Any

import aiohttp
import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (SelectOptionDict, SelectSelector,
                                            SelectSelectorConfig)

from .const import (CONF_NAME, CONF_SERIAL, CONF_STILL_REFRESH_S, CONF_WIREPOD_URL,
                    DEFAULT_STILL_REFRESH_S, DEFAULT_WIREPOD_URL, DOMAIN,
                    MAX_STILL_REFRESH_S)
from .wirepod import WirePodError, get_sdk_info


class VectorConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    def __init__(self) -> None:
        self._url = ""
        self._robots: list[dict] = []

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            url = user_input[CONF_WIREPOD_URL].strip().rstrip("/")
            try:
                robots = await get_sdk_info(async_get_clientsession(self.hass), url)
            except ValueError:
                errors["base"] = "invalid_url"
            except (aiohttp.ClientError, TimeoutError, WirePodError):
                errors["base"] = "cannot_connect"
            else:
                robots = [r for r in robots if r["esn"].isalnum()]
                if robots:
                    self._url, self._robots = url, robots
                    return await self.async_step_robot()
                errors["base"] = "no_robots"
        schema = vol.Schema({vol.Required(CONF_WIREPOD_URL, default=DEFAULT_WIREPOD_URL): str})
        return self.async_show_form(step_id="user", data_schema=schema, errors=errors)

    async def async_step_robot(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            serial = user_input[CONF_SERIAL]
            await self.async_set_unique_id(serial)
            self._abort_if_unique_id_configured()
            title = (user_input.get(CONF_NAME) or "").strip() or f"Vector {serial}"
            return self.async_create_entry(
                title=title, data={CONF_WIREPOD_URL: self._url, CONF_SERIAL: serial})
        options = [SelectOptionDict(value=r["esn"], label=f"{r['esn']} ({r['ip_address']})")
                   for r in self._robots]
        schema = vol.Schema({
            vol.Required(CONF_SERIAL): SelectSelector(SelectSelectorConfig(options=options)),
            vol.Optional(CONF_NAME, default=""): str,
        })
        return self.async_show_form(step_id="robot", data_schema=schema)

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return VectorOptionsFlow()


class VectorOptionsFlow(OptionsFlow):
    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(data=user_input)
        current = self.config_entry.options.get(CONF_STILL_REFRESH_S, DEFAULT_STILL_REFRESH_S)
        schema = vol.Schema({
            vol.Required(CONF_STILL_REFRESH_S, default=current):
                vol.All(vol.Coerce(int), vol.Range(min=0, max=MAX_STILL_REFRESH_S)),
        })
        return self.async_show_form(step_id="init", data_schema=schema)
