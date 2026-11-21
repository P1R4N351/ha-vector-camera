"""Battery voltage, battery level, camera status and sleep cycle sensors."""

from __future__ import annotations

from datetime import datetime, timezone

from homeassistant.components.sensor import (SensorDeviceClass, SensorEntity,
                                             SensorStateClass)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory, UnitOfElectricPotential
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .camera import device_info
from .const import BATTERY_LEVELS, CONF_SERIAL
from .extras import extra_sensors
from .sleepcycle import IDLE, PHASES
from .wirepod import ALL_STATES


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry,
                            async_add_entities: AddConfigEntryEntitiesCallback) -> None:
    assert entry is not None and async_add_entities is not None
    async_add_entities([
        VectorBatteryVoltageSensor(entry),
        VectorBatteryLevelSensor(entry),
        VectorCameraStatusSensor(entry),
        VectorSleepCycleSensor(entry),
        *extra_sensors(hass, entry),
    ])


class _VectorBaseSensor(CoordinatorEntity, SensorEntity):
    _attr_has_entity_name = True

    def __init__(self, entry: ConfigEntry, suffix: str, name: str) -> None:
        assert entry is not None and bool(suffix)
        super().__init__(entry.runtime_data.coordinator)
        self._hub = entry.runtime_data.hub
        self._attr_name = name
        self._attr_unique_id = f"{entry.data[CONF_SERIAL]}_{suffix}"
        self._attr_device_info = device_info(entry)


class VectorBatteryVoltageSensor(_VectorBaseSensor):
    _attr_device_class = SensorDeviceClass.VOLTAGE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfElectricPotential.VOLT
    _attr_suggested_display_precision = 2

    def __init__(self, entry: ConfigEntry) -> None:
        super().__init__(entry, "battery_voltage", "Battery voltage")

    @property
    def native_value(self) -> float | None:
        data = self.coordinator.data or {}
        if not data.get("robot_reachable"):
            return None
        volts = data.get("battery_volts")
        return None if volts is None else float(volts)


class VectorBatteryLevelSensor(_VectorBaseSensor):
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = list(BATTERY_LEVELS.values())

    def __init__(self, entry: ConfigEntry) -> None:
        super().__init__(entry, "battery_level", "Battery level")

    @property
    def native_value(self) -> str | None:
        data = self.coordinator.data or {}
        if not data.get("robot_reachable"):
            return None
        return BATTERY_LEVELS.get(data.get("battery_level"), "unknown")


class VectorCameraStatusSensor(_VectorBaseSensor):
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = list(ALL_STATES)
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:camera-outline"

    def __init__(self, entry: ConfigEntry) -> None:
        super().__init__(entry, "camera_status", "Camera status")

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(self._hub.add_listener(self.async_write_ha_state))

    @property
    def native_value(self) -> str:
        return self._hub.state


class VectorSleepCycleSensor(_VectorBaseSensor, RestoreEntity):
    """Where the sleep cycle is: idle, going_to_sleep, sleeping or woken_quiet (sleepcycle.py)."""

    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = list(PHASES)
    _attr_icon = "mdi:sleep"

    def __init__(self, entry: ConfigEntry) -> None:
        super().__init__(entry, "sleep_cycle", "Sleep cycle")
        self._cycle = entry.runtime_data.manager.cycle

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last is not None and last.state in PHASES and last.state != IDLE:
            self._cycle.restore(last.state, last.attributes.get("since_ts"), last.attributes.get("wakes"))

    @property
    def native_value(self) -> str:
        return self._cycle.phase

    @property
    def extra_state_attributes(self) -> dict:
        cycle = self._cycle
        now = datetime.now(timezone.utc).timestamp()
        left = cycle.quiet_seconds_left(now)
        since = cycle.since if cycle.since > 0 else None
        return {
            "note": cycle.note,
            "wakes": cycle.wakes,
            "since": None if since is None else datetime.fromtimestamp(since, timezone.utc).isoformat(timespec="seconds"),
            "since_ts": since,
            "quiet_seconds_left": None if left is None else int(left),
            "quiet_hold_s": int(cycle.quiet_hold_s),
            "photo_on_wake": cycle.photo_on_wake,
        }
