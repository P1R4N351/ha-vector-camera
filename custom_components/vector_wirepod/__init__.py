"""Vector (wire-pod) camera: a passive Home Assistant camera for one Vector.

Authored-by: CPCS. The robot-facing policy lives in wirepod.py.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import timedelta

import aiohttp

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import (BATTERY_POLL_S, BATTERY_TIMEOUT_S, CONF_BATTERY_POLL_S,
                    CONF_SERIAL, CONF_WIREPOD_URL, DOMAIN, MIN_BATTERY_POLL_S,
                    PLATFORMS)
from .wirepod import CameraHub, HubConfig, WirePodError, get_battery

_LOGGER = logging.getLogger(__name__)


@dataclass
class VectorData:
    hub: CameraHub
    coordinator: DataUpdateCoordinator


def _make_coordinator(hass: HomeAssistant, entry: ConfigEntry,
                      session: aiohttp.ClientSession, hub: CameraHub) -> DataUpdateCoordinator:
    assert hass is not None and entry is not None
    assert session is not None and hub is not None
    base = entry.data[CONF_WIREPOD_URL]
    serial = entry.data[CONF_SERIAL]
    poll_s = max(MIN_BATTERY_POLL_S, int(entry.options.get(CONF_BATTERY_POLL_S, BATTERY_POLL_S)))

    async def _update() -> dict:
        try:
            data = await get_battery(session, base, serial, BATTERY_TIMEOUT_S)
        except WirePodError as exc:
            hub.set_robot_reachable(False, f"robot not answering wire-pod: {exc}"[:200])
            return {"robot_reachable": False, "error": str(exc)[:200]}
        except (aiohttp.ClientError, TimeoutError) as exc:
            hub.set_robot_reachable(False, "wire-pod unreachable")
            raise UpdateFailed(f"wire-pod unreachable: {exc}") from exc
        hub.set_robot_reachable(True)
        data["robot_reachable"] = True
        return data

    return DataUpdateCoordinator(
        hass, _LOGGER, config_entry=entry, name=f"{DOMAIN} {serial}",
        update_interval=timedelta(seconds=poll_s), update_method=_update)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    assert hass is not None and entry is not None
    session = async_get_clientsession(hass)
    hub = CameraHub(session, HubConfig(base_url=entry.data[CONF_WIREPOD_URL],
                                       serial=entry.data[CONF_SERIAL]))
    coordinator = _make_coordinator(hass, entry, session, hub)
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = VectorData(hub=hub, coordinator=coordinator)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))
    return True


async def _async_options_updated(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    assert hass is not None and entry is not None
    ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if ok:
        await entry.runtime_data.hub.close()
    return ok
