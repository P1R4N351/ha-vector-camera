"""Switch entities: 'Camera allowed' and 'Quiet mode' for Vector. Authored-by: CPCS."""

from __future__ import annotations

from typing import Any

import aiohttp

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .camera import device_info
from .const import CONF_SERIAL
from .wirepod import WirePodError, set_quiet_mode


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry,
                            async_add_entities: AddConfigEntryEntitiesCallback) -> None:
    async_add_entities([CameraAllowedSwitch(entry), QuietModeSwitch(entry)])


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


class QuietModeSwitch(CoordinatorEntity, SwitchEntity, RestoreEntity):
    """Hold RESERVE_CONTROL + mute on Vector to suppress autonomous activity and expressions."""

    _attr_has_entity_name = True
    _attr_name = "Quiet mode"

    def __init__(self, entry: ConfigEntry) -> None:
        data = entry.runtime_data
        super().__init__(data.coordinator)
        self._data = data
        self._attr_unique_id = f"{entry.data[CONF_SERIAL]}_quiet_mode"
        self._attr_device_info = device_info(entry)
        self._desired: bool = False

    @property
    def icon(self) -> str:
        return "mdi:sleep" if self.is_on else "mdi:sleep-off"

    @property
    def available(self) -> bool:
        if not super().available:
            return False
        qm = (self.coordinator.data or {}).get("quiet_mode")
        return isinstance(qm, dict)

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        qm = (self.coordinator.data or {}).get("quiet_mode")
        if last is not None and last.state in ("on", "off"):
            want = last.state == "on"
            self._desired = want
            if isinstance(qm, dict) and qm.get("enabled") != want:
                try:
                    res = await set_quiet_mode(
                        self._data.session, self._data.base_url, self._data.serial, want)
                    if isinstance(self.coordinator.data, dict):
                        self.coordinator.data["quiet_mode"] = res
                except (aiohttp.ClientError, TimeoutError, WirePodError):
                    pass
        elif isinstance(qm, dict):
            self._desired = bool(qm.get("enabled"))

    @property
    def is_on(self) -> bool:
        qm = (self.coordinator.data or {}).get("quiet_mode")
        if isinstance(qm, dict):
            return bool(qm.get("enabled"))
        return self._desired

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        qm = (self.coordinator.data or {}).get("quiet_mode")
        if not isinstance(qm, dict):
            return {"active": False, "holding_reserve": False, "muted": False,
                    "robot_reachable": False, "pending_restore": False, "last_error": ""}
        return {
            "active": bool(qm.get("active", False)),
            "holding_reserve": bool(qm.get("holding_reserve", False)),
            "muted": bool(qm.get("muted", False)),
            "robot_reachable": bool(qm.get("robot_reachable", False)),
            "pending_restore": bool(qm.get("pending_restore", False)),
            "last_error": str(qm.get("last_error") or ""),
        }

    async def _apply_state(self, enabled: bool) -> None:
        res = await set_quiet_mode(
            self._data.session, self._data.base_url, self._data.serial, enabled)
        self._desired = bool(res.get("enabled", enabled))
        if isinstance(self.coordinator.data, dict):
            self.coordinator.data["quiet_mode"] = res
        self.async_write_ha_state()

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._apply_state(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._apply_state(False)
