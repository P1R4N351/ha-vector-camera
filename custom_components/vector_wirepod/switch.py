"""'Camera allowed' switch. Off withholds the camera from Home Assistant, for
example while a navigation run needs it. Survives restarts. Authored-by: CPCS."""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from .camera import device_info
from .const import CONF_SERIAL


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry,
                            async_add_entities: AddConfigEntryEntitiesCallback) -> None:
    async_add_entities([CameraAllowedSwitch(entry)])


class CameraAllowedSwitch(SwitchEntity, RestoreEntity):
    _attr_has_entity_name = True
    _attr_name = "Camera allowed"
    _attr_icon = "mdi:cctv"

    def __init__(self, entry: ConfigEntry) -> None:
        self._hub = entry.runtime_data.hub
        self._attr_unique_id = f"{entry.data[CONF_SERIAL]}_camera_allowed"
        self._attr_device_info = device_info(entry)

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        self._hub.set_allowed(last is None or last.state != "off")

    @property
    def is_on(self) -> bool:
        return self._hub.allowed

    async def async_turn_on(self, **kwargs: Any) -> None:
        self._hub.set_allowed(True)
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        self._hub.set_allowed(False)
        self.async_write_ha_state()
