"""Redacted diagnostics download support for vector_wirepod."""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import CONF_SERIAL, CONF_WIREPOD_URL

TO_REDACT = {CONF_WIREPOD_URL, CONF_SERIAL, "ip_address"}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    """Return redacted entry, options, coordinator (battery + quiet_mode), and hub state."""
    assert hass is not None and entry is not None
    data = entry.runtime_data
    return {
        "entry": async_redact_data(dict(entry.data), TO_REDACT),
        "options": dict(entry.options),
        "coordinator": async_redact_data(dict(data.coordinator.data or {}), TO_REDACT),
        "hub": data.hub.diagnostics(),
    }
