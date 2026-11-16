"""Camera entity for one Vector, passive and shared. Authored-by: CPCS.

Thumbnails never open the robot's camera on their own schedule: they are served
from the last frame, refreshed at most once per still_refresh_s and only when a
start is due (see CameraHub.still). Opening the live view takes a lease on the
one shared upstream; closing it releases the lease.
"""

from __future__ import annotations

from aiohttp import web

from homeassistant.components.camera import Camera, async_get_still_stream
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import placeholder
from .const import (BATTERY_LEVELS, CONF_SERIAL, CONF_STILL_REFRESH_S,
                    DEFAULT_STILL_REFRESH_S, DOMAIN, STILL_WAIT_S)
from .wirepod import STATE_STREAMING

LIVE_INTERVAL_S = 0.05
FRESH_WHEN_NO_REFRESH_S = 60.0


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry,
                            async_add_entities: AddConfigEntryEntitiesCallback) -> None:
    async_add_entities([VectorCamera(entry)])


def device_info(entry: ConfigEntry) -> DeviceInfo:
    serial = entry.data[CONF_SERIAL]
    return DeviceInfo(identifiers={(DOMAIN, serial)}, name=entry.title,
                      manufacturer="Digital Dream Labs", model="Vector", serial_number=serial)


def _fmt_age(age: float) -> str:
    if age < 120:
        return f"{int(age)} s"
    if age < 7200:
        return f"{int(age // 60)} min"
    return f"{age / 3600:.1f} h"


class VectorCamera(CoordinatorEntity, Camera):
    _attr_has_entity_name = True
    _attr_name = None

    def __init__(self, entry: ConfigEntry) -> None:
        data = entry.runtime_data
        CoordinatorEntity.__init__(self, data.coordinator)
        Camera.__init__(self)
        self._entry = entry
        self._hub = data.hub
        self._attr_unique_id = f"{entry.data[CONF_SERIAL]}_camera"
        self._attr_device_info = device_info(entry)
        self._refresh_s = int(entry.options.get(CONF_STILL_REFRESH_S, DEFAULT_STILL_REFRESH_S))

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(self._hub.add_listener(self.async_write_ha_state))

    @property
    def is_streaming(self) -> bool:
        return self._hub.state == STATE_STREAMING

    @property
    def extra_state_attributes(self) -> dict:
        hub = self._hub
        age = hub.frame_age_s()
        data = self.coordinator.data or {}
        return {
            "camera_state": hub.state,
            "camera_detail": hub.detail,
            "camera_allowed": hub.allowed,
            "viewers": hub.viewers,
            "last_frame_age_s": None if age is None else round(age, 1),
            "next_start_in_s": round(hub.retry_in_s(), 1),
            "robot_reachable": data.get("robot_reachable"),
            "on_charger": data.get("is_on_charger_platform"),
            "battery_level": BATTERY_LEVELS.get(data.get("battery_level"), "unknown"),
            "battery_volts": data.get("battery_volts"),
        }

    def _fresh_s(self) -> float:
        return float(self._refresh_s) if self._refresh_s > 0 else FRESH_WHEN_NO_REFRESH_S

    def _status_lines(self) -> list[str]:
        hub = self._hub
        data = self.coordinator.data or {}
        age = hub.frame_age_s()
        lines = [self._entry.title, f"Not live: camera {hub.state.replace('_', ' ')}"]
        if hub.detail:
            lines.append(hub.detail)
        lines.append("No frame yet" if age is None else f"Last frame {_fmt_age(age)} ago")
        if data.get("robot_reachable"):
            place = "on charger" if data.get("is_on_charger_platform") else "off charger"
            level = BATTERY_LEVELS.get(data.get("battery_level"), "unknown")
            lines.append(f"Battery {level}, {place}")
        return lines

    async def _status_image(self, frame: bytes | None) -> bytes:
        return await self.hass.async_add_executor_job(
            placeholder.render, self._status_lines(), frame)

    async def async_camera_image(self, width: int | None = None,
                                 height: int | None = None) -> bytes | None:
        hub = self._hub
        fresh = self._fresh_s()
        frame = await hub.still(fresh, self._refresh_s > 0, STILL_WAIT_S)
        age = hub.frame_age_s()
        if frame is not None and age is not None and age <= fresh:
            return frame
        return await self._status_image(frame)

    async def handle_async_mjpeg_stream(self, request: web.Request) -> web.StreamResponse | None:
        hub = self._hub
        lease = hub.acquire()
        last = [hub.seq]
        status_sent = [False]
        wait_s = hub.cfg.header_timeout_s + hub.cfg.stall_timeout_s

        async def next_image() -> bytes | None:
            got = await hub.next_frame(last[0], wait_s)
            if got is not None:
                last[0] = got[0]
                return got[1]
            if status_sent[0]:
                return None
            status_sent[0] = True
            return await self._status_image(hub.frame)

        try:
            return await async_get_still_stream(request, next_image, "image/jpeg",
                                                LIVE_INTERVAL_S)
        finally:
            lease.release()
