"""Charger, charging, and reachability binary sensors (piggybacked on get_battery)."""

from __future__ import annotations

from homeassistant.components.binary_sensor import (BinarySensorDeviceClass,
                                                    BinarySensorEntity)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .camera import device_info
from .const import CONF_SERIAL
from .extras import extra_binary_sensors


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry,
                            async_add_entities: AddConfigEntryEntitiesCallback) -> None:
    assert entry is not None and async_add_entities is not None
    async_add_entities([
        VectorOnChargerBinarySensor(entry),
        VectorChargingBinarySensor(entry),
        VectorReachableBinarySensor(entry),
        VectorAsleepBinarySensor(entry),
        *extra_binary_sensors(entry),
    ])


class _VectorBaseBinarySensor(CoordinatorEntity, BinarySensorEntity):
    _attr_has_entity_name = True

    def __init__(self, entry: ConfigEntry, suffix: str, name: str) -> None:
        assert entry is not None and bool(suffix)
        super().__init__(entry.runtime_data.coordinator)
        self._attr_name = name
        self._attr_unique_id = f"{entry.data[CONF_SERIAL]}_{suffix}"
        self._attr_device_info = device_info(entry)


class VectorOnChargerBinarySensor(_VectorBaseBinarySensor):
    _attr_device_class = BinarySensorDeviceClass.PLUG

    def __init__(self, entry: ConfigEntry) -> None:
        super().__init__(entry, "on_charger", "On charger")

    @property
    def is_on(self) -> bool | None:
        data = self.coordinator.data or {}
        if not data.get("robot_reachable"):
            return None
        return bool(data.get("is_on_charger_platform", False))


class VectorChargingBinarySensor(_VectorBaseBinarySensor):
    _attr_device_class = BinarySensorDeviceClass.BATTERY_CHARGING

    def __init__(self, entry: ConfigEntry) -> None:
        super().__init__(entry, "charging", "Charging")

    @property
    def is_on(self) -> bool | None:
        data = self.coordinator.data or {}
        if not data.get("robot_reachable"):
            return None
        return bool(data.get("is_charging", False))


class VectorReachableBinarySensor(_VectorBaseBinarySensor):
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, entry: ConfigEntry) -> None:
        super().__init__(entry, "reachable", "Robot reachable")

    @property
    def is_on(self) -> bool | None:
        data = self.coordinator.data
        if data is None:
            return None
        return bool(data.get("robot_reachable"))


class VectorAsleepBinarySensor(_VectorBaseBinarySensor):
    """On while the robot itself reports that it is asleep. Unknown when it cannot be read."""

    def __init__(self, entry: ConfigEntry) -> None:
        super().__init__(entry, "asleep", "Asleep")

    def _state(self) -> dict | None:
        data = self.coordinator.data or {}
        state = data.get("robot_state")
        return state if data.get("robot_reachable") and isinstance(state, dict) else None

    @property
    def icon(self) -> str:
        return "mdi:sleep" if self.is_on else "mdi:sleep-off"

    @property
    def is_on(self) -> bool | None:
        state = self._state()
        return None if state is None else bool(state.get("asleep"))

    @property
    def extra_state_attributes(self) -> dict:
        state = self._state() or {}
        return {key: state.get(key) for key in ("picked_up", "being_held", "button_pressed", "moving")}
