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


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry,
                            async_add_entities: AddConfigEntryEntitiesCallback) -> None:
    assert entry is not None and async_add_entities is not None
    async_add_entities([
        VectorOnChargerBinarySensor(entry),
        VectorChargingBinarySensor(entry),
        VectorReachableBinarySensor(entry),
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
