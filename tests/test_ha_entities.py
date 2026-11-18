"""Offline tests for HA entities, config flow, options, reconfigure, and metadata.

Uses lightweight in-memory homeassistant/voluptuous module stubs in setUpModule and
exercises the real integration modules against FakeWirePod over loopback HTTP.
Run: python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
import importlib
import io
import json
import pathlib
import sys
import types
import unittest
from urllib.parse import urlparse

import aiohttp
from PIL import Image

ROOT = pathlib.Path(__file__).resolve().parents[1]
TESTS_DIR = pathlib.Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))

from test_wirepod import FakeWirePod, SERIAL  # noqa: E402

_SAVED_MODULES: dict[str, types.ModuleType | None] = {}
_LAST_RESTORE_STATE: str | None = "on"


def _put_mod(name: str, attrs: dict | None = None) -> types.ModuleType:
    assert name
    if name not in _SAVED_MODULES:
        _SAVED_MODULES[name] = sys.modules.get(name)
    mod = types.ModuleType(name)
    if attrs:
        for k, v in attrs.items():
            setattr(mod, k, v)
    sys.modules[name] = mod
    return mod


def _make_voluptuous_stub() -> dict:
    class Schema:
        def __init__(self, schema: dict) -> None:
            self.schema = schema

    class _Marker:
        def __init__(self, key: str, default: object = None) -> None:
            self.key = key
            self.default = default

    return {
        "Schema": Schema,
        "Required": lambda k, default=None: _Marker(k, default),
        "Optional": lambda k, default=None: _Marker(k, default),
        "All": lambda *fns: fns,
        "Coerce": lambda tp: tp,
        "Range": lambda **kw: kw,
    }


def _make_config_entry_cls() -> type:
    class ConfigEntry:
        def __init__(self, data: dict, options: dict | None = None,
                     title: str = "Vector Demo", entry_id: str = "entry-1") -> None:
            assert isinstance(data, dict)
            self.data = dict(data)
            self.options = dict(options or {})
            self.title = title
            self.entry_id = entry_id
            self.unique_id: str | None = data.get("serial")
            self.runtime_data = None
            self.unload_cbs: list = []
            self.update_listeners: list = []

        def async_on_unload(self, cb) -> None:
            self.unload_cbs.append(cb)

        def add_update_listener(self, cb):
            self.update_listeners.append(cb)
            return lambda: (
                self.update_listeners.remove(cb) if cb in self.update_listeners else None
            )

    return ConfigEntry


def _make_config_flow_cls() -> type:
    class ConfigFlow:
        def __init_subclass__(cls, domain: str = "", **kw) -> None:
            cls.domain = domain

        hass = None
        reconfigure_entry = None
        existing_unique_ids: set[str] = set()

        async def async_set_unique_id(self, uid: str) -> None:
            self._pending_uid = uid

        def _abort_if_unique_id_configured(self) -> None:
            if getattr(self, "_pending_uid", None) in self.existing_unique_ids:
                raise RuntimeError("already_configured")

        def async_create_entry(self, *, title: str, data: dict) -> dict:
            return {"type": "create_entry", "title": title, "data": data}

        def async_show_form(self, *, step_id: str, data_schema: object,
                            errors: dict | None = None) -> dict:
            return {"type": "form", "step_id": step_id, "schema": data_schema,
                    "errors": dict(errors or {})}

        def _get_reconfigure_entry(self):
            assert self.reconfigure_entry is not None
            return self.reconfigure_entry

        def async_update_reload_and_abort(self, entry, *, data_updates: dict) -> dict:
            entry.data.update(data_updates)
            return {"type": "abort", "reason": "reconfigure_successful", "data": dict(entry.data)}

    return ConfigFlow


def _make_options_and_hass_clss() -> tuple[type, type]:
    class OptionsFlow:
        config_entry = None

        def async_create_entry(self, *, data: dict) -> dict:
            return {"type": "create_entry", "data": data}

        def async_show_form(self, *, step_id: str, data_schema: object) -> dict:
            return {"type": "form", "step_id": step_id, "schema": data_schema}

    class HomeAssistant:
        def __init__(self, session: aiohttp.ClientSession) -> None:
            assert session is not None
            self.session = session
            self.forwarded: list[str] = []
            self.unloaded: list[str] = []
            self.reloaded: list[str] = []
            self.config_entries = types.SimpleNamespace(
                async_forward_entry_setups=self._forward,
                async_unload_platforms=self._unload,
                async_reload=self._reload,
            )

        async def _forward(self, entry, platforms: list[str]) -> None:
            self.forwarded.extend(platforms)

        async def _unload(self, entry, platforms: list[str]) -> bool:
            self.unloaded.extend(platforms)
            return True

        async def _reload(self, entry_id: str) -> None:
            self.reloaded.append(entry_id)

        async def async_add_executor_job(self, func, *args):
            return func(*args)

    return OptionsFlow, HomeAssistant


def _make_coordinator_clss() -> tuple[type, type]:
    class UpdateFailed(Exception):
        pass

    class DataUpdateCoordinator:
        def __init__(self, hass, logger, *, config_entry, name: str,
                     update_interval: timedelta, update_method) -> None:
            assert hass is not None and update_method is not None
            self.hass = hass
            self.logger = logger
            self.config_entry = config_entry
            self.name = name
            self.update_interval = update_interval
            self.update_method = update_method
            self.data: dict | None = None
            self.last_update_success = False

        async def async_config_entry_first_refresh(self) -> None:
            await self.async_refresh()

        async def async_refresh(self) -> None:
            try:
                self.data = await self.update_method()
                self.last_update_success = True
            except UpdateFailed:
                self.last_update_success = False

    return UpdateFailed, DataUpdateCoordinator


def _make_entity_bases(coordinator_cls: type) -> tuple[type, type, type]:
    class Entity:
        _attr_has_entity_name = False
        _attr_name = None
        _attr_unique_id = None
        _attr_device_info = None
        _attr_entity_category = None
        _attr_icon = None
        hass = None
        writes = 0

        def __init__(self) -> None:
            self._removers: list = []
            self.writes = 0

        async def async_added_to_hass(self) -> None:
            return None

        def async_on_remove(self, cb) -> None:
            if not hasattr(self, "_removers"):
                self._removers = []
            self._removers.append(cb)

        def async_write_ha_state(self) -> None:
            self.writes = getattr(self, "writes", 0) + 1

    class CoordinatorEntity(Entity):
        def __init__(self, coordinator: coordinator_cls) -> None:
            super().__init__()
            self.coordinator = coordinator

        @property
        def available(self) -> bool:
            return bool(self.coordinator.last_update_success)

    class RestoreEntity:
        async def async_get_last_state(self):
            if _LAST_RESTORE_STATE is None:
                return None
            return types.SimpleNamespace(state=_LAST_RESTORE_STATE)

    return Entity, CoordinatorEntity, RestoreEntity


def _register_platform_stubs(entity_cls: type) -> None:
    async def async_get_still_stream(request, image_fn, content_type, interval) -> list[bytes]:
        frames: list[bytes] = []
        limit = getattr(request, "max_frames", 3)
        for _ in range(limit):
            img = await image_fn()
            if img is None:
                break
            frames.append(img)
            await asyncio.sleep(0.01)
        return frames

    def async_redact_data(data: dict, to_redact: set) -> dict:
        return {k: ("**REDACTED**" if k in to_redact else v) for k, v in data.items()}

    _put_mod("homeassistant.components.camera", {
        "Camera": type("Camera", (entity_cls,), {}),
        "async_get_still_stream": async_get_still_stream,
    })
    _put_mod("homeassistant.components.switch", {
        "SwitchEntity": type("SwitchEntity", (entity_cls,), {}),
    })
    _put_mod("homeassistant.components.sensor", {
        "SensorDeviceClass": types.SimpleNamespace(VOLTAGE="voltage", ENUM="enum"),
        "SensorEntity": type("SensorEntity", (entity_cls,), {}),
        "SensorStateClass": types.SimpleNamespace(MEASUREMENT="measurement"),
    })
    _put_mod("homeassistant.components.binary_sensor", {
        "BinarySensorDeviceClass": types.SimpleNamespace(
            PLUG="plug", BATTERY_CHARGING="battery_charging", CONNECTIVITY="connectivity"),
        "BinarySensorEntity": type("BinarySensorEntity", (entity_cls,), {}),
    })
    _put_mod("homeassistant.components.diagnostics", {"async_redact_data": async_redact_data})


def _build_ha_stubs() -> None:
    for root_pkg in ("homeassistant", "homeassistant.helpers", "homeassistant.components"):
        _put_mod(root_pkg)
    config_entry_cls = _make_config_entry_cls()
    config_flow_cls = _make_config_flow_cls()
    options_cls, hass_cls = _make_options_and_hass_clss()
    upd_failed, coord_cls = _make_coordinator_clss()
    entity_cls, coord_ent_cls, restore_cls = _make_entity_bases(coord_cls)
    _put_mod("voluptuous", _make_voluptuous_stub())
    _put_mod("homeassistant.config_entries", {
        "ConfigEntry": config_entry_cls, "ConfigFlow": config_flow_cls,
        "ConfigFlowResult": dict, "OptionsFlow": options_cls,
    })
    _put_mod("homeassistant.core", {"HomeAssistant": hass_cls, "callback": lambda fn: fn})
    _put_mod("homeassistant.const", {
        "EntityCategory": types.SimpleNamespace(DIAGNOSTIC="diagnostic"),
        "UnitOfElectricPotential": types.SimpleNamespace(VOLT="V"),
    })
    _put_mod("homeassistant.helpers.aiohttp_client", {
        "async_get_clientsession": lambda hass: hass.session,
    })
    _put_mod("homeassistant.helpers.update_coordinator", {
        "DataUpdateCoordinator": coord_cls, "UpdateFailed": upd_failed,
        "CoordinatorEntity": coord_ent_cls,
    })
    _put_mod("homeassistant.helpers.device_registry", {"DeviceInfo": type("DeviceInfo", (dict,), {})})
    _put_mod("homeassistant.helpers.entity_platform", {"AddConfigEntryEntitiesCallback": object})
    _put_mod("homeassistant.helpers.selector", {
        "SelectOptionDict": dict, "SelectSelector": lambda c: c,
        "SelectSelectorConfig": lambda **kw: kw,
    })
    _put_mod("homeassistant.helpers.restore_state", {"RestoreEntity": restore_cls})
    _register_platform_stubs(entity_cls)


def _restore_modules() -> None:
    for mod_name in list(sys.modules):
        if mod_name.startswith("custom_components.vector_wirepod"):
            sys.modules.pop(mod_name, None)
    for name, orig in _SAVED_MODULES.items():
        if orig is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = orig
    _SAVED_MODULES.clear()


def setUpModule() -> None:
    _build_ha_stubs()


def tearDownModule() -> None:
    _restore_modules()


def _load_comp(sub: str = ""):
    name = "custom_components.vector_wirepod" + (f".{sub}" if sub else "")
    mod = importlib.import_module(name)
    return importlib.reload(mod)


class MetadataTests(unittest.TestCase):
    def test_manifest_hacs_and_translations(self):
        comp_dir = ROOT / "custom_components" / "vector_wirepod"
        manifest = json.loads((comp_dir / "manifest.json").read_text())
        keys = list(manifest.keys())
        self.assertEqual(keys[:2], ["domain", "name"])
        self.assertEqual(keys[2:], sorted(keys[2:]))
        hacs = json.loads((ROOT / "hacs.json").read_text())
        self.assertEqual(hacs["name"], manifest["name"])
        strings = json.loads((comp_dir / "strings.json").read_text())
        en = json.loads((comp_dir / "translations" / "en.json").read_text())
        self.assertEqual(strings, en)


class EntityAndFlowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.pod = FakeWirePod()
        self.base = await self.pod.start()
        self.session = aiohttp.ClientSession()
        self.ha_core = sys.modules["homeassistant.core"]
        self.ha_ce = sys.modules["homeassistant.config_entries"]
        self.hass = self.ha_core.HomeAssistant(self.session)

    async def asyncTearDown(self):
        await self.session.close()
        await self.pod.stop()

    async def _setup_entry(self, options: dict | None = None):
        comp = _load_comp()
        entry = self.ha_ce.ConfigEntry(
            {"wirepod_url": self.base, "serial": SERIAL},
            options=options, title="Vector Demo",
        )
        ok = await comp.async_setup_entry(self.hass, entry)
        self.assertTrue(ok)
        return comp, entry

    async def _collect_entities(self, entry) -> list:
        added: list = []

        def _add(ents) -> None:
            for e in ents:
                e.hass = self.hass
                added.append(e)

        for mod_name in ("camera", "switch", "sensor", "binary_sensor"):
            await _load_comp(mod_name).async_setup_entry(self.hass, entry, _add)
        return added

    async def test_setup_entities_sensors_diagnostics_and_unload(self):
        global _LAST_RESTORE_STATE
        self.pod.battery = '{"battery_volts": 3.742}'
        comp, entry = await self._setup_entry(options={"battery_poll_s": 30, "still_refresh_s": 0})
        added = await self._collect_entities(entry)
        self.assertEqual(len(added), 8)
        cam, sw, v_sen, lvl_sen, st_sen, plug_bs, chg_bs, reach_bs = added
        _LAST_RESTORE_STATE = "off"
        await sw.async_added_to_hass()
        self.assertFalse(sw.is_on)
        self.assertEqual(st_sen.native_value, "withheld")
        await sw.async_turn_on()
        self.assertTrue(sw.is_on)
        self.assertGreaterEqual(sw.writes, 1)
        self.assertEqual(v_sen.native_value, 3.742)
        self.assertEqual(lvl_sen.native_value, "unknown")
        self.assertIs(plug_bs.is_on, False)
        self.assertIs(chg_bs.is_on, False)
        self.assertIs(reach_bs.is_on, True)
        self.assertIs(cam.extra_state_attributes["on_charger"], False)
        diag = await _load_comp("diagnostics").async_get_config_entry_diagnostics(self.hass, entry)
        self.assertEqual(diag["entry"]["wirepod_url"], "**REDACTED**")
        self.assertEqual(diag["entry"]["serial"], "**REDACTED**")
        self.assertEqual(diag["hub"]["state"], "idle")
        self.assertTrue(await comp.async_unload_entry(self.hass, entry))
        _LAST_RESTORE_STATE = "on"

    async def test_unreachable_wirepod_marks_entities_unavailable(self):
        comp, entry = await self._setup_entry()
        added = await self._collect_entities(entry)
        port = urlparse(self.base).port
        entry.data["wirepod_url"] = f"http://127.0.0.1:{port + 1}" if port else "http://127.0.0.1:1"
        await comp.async_unload_entry(self.hass, entry)
        comp2, entry2 = await self._setup_entry()
        added2 = await self._collect_entities(entry2)
        entry2.runtime_data.coordinator.last_update_success = False
        self.assertFalse(added2[2].available)
        self.assertIsNotNone(added[0])
        await comp2.async_unload_entry(self.hass, entry2)

    async def test_camera_still_and_live_mjpeg_stream_cleanup(self):
        comp, entry = await self._setup_entry(options={"still_refresh_s": 0})
        added = await self._collect_entities(entry)
        cam = added[0]
        await cam.async_added_to_hass()
        still_bytes = await cam.async_camera_image()
        self.assertEqual(Image.open(io.BytesIO(still_bytes)).format, "JPEG")
        self.assertEqual(self.pod.connections, 0)
        entry.runtime_data.hub.cfg = type(entry.runtime_data.hub.cfg)(
            base_url=self.base, serial=SERIAL, header_timeout_s=0.4,
            stall_timeout_s=0.3, backoff_min_s=0.1, backoff_max_s=0.3)
        req = types.SimpleNamespace(max_frames=3)
        frames = await cam.handle_async_mjpeg_stream(req)
        self.assertEqual(len(frames), 3)
        self.assertEqual(entry.runtime_data.hub.viewers, 0)
        self.assertEqual(entry.runtime_data.hub.state, "idle")
        self.assertFalse(cam.is_streaming)
        await comp.async_unload_entry(self.hass, entry)

    async def test_sleeping_stream_keepalive_recovers_live(self):
        self.pod.mode = "hang"
        comp, entry = await self._setup_entry(options={"still_refresh_s": 0})
        cam_mod = _load_comp("camera")
        cam = (await self._collect_entities(entry))[0]
        await cam.async_added_to_hass()
        entry.runtime_data.hub.cfg = type(entry.runtime_data.hub.cfg)(
            base_url=self.base, serial=SERIAL, header_timeout_s=0.12,
            stall_timeout_s=0.12, backoff_min_s=0.12, backoff_max_s=0.25)
        old_keepalive = cam_mod.STATUS_KEEPALIVE_S
        cam_mod.STATUS_KEEPALIVE_S = 0.08
        try:
            task = asyncio.create_task(
                cam.handle_async_mjpeg_stream(types.SimpleNamespace(max_frames=3)))
            await asyncio.sleep(0.18)
            self.pod.mode = "stream"
            frames = await task
        finally:
            cam_mod.STATUS_KEEPALIVE_S = old_keepalive
        self.assertEqual(len(frames), 3)
        self.assertEqual(Image.open(io.BytesIO(frames[0])).size, (640, 360))
        self.assertEqual(Image.open(io.BytesIO(frames[-1])).size, (32, 24))
        self.assertEqual(entry.runtime_data.hub.viewers, 0)
        self.assertEqual(entry.runtime_data.hub.state, "idle")
        await comp.async_unload_entry(self.hass, entry)

    async def test_config_reconfigure_and_options_flow(self):
        cf_mod = _load_comp("config_flow")
        flow = cf_mod.VectorConfigFlow()
        flow.hass = self.hass
        res1 = await flow.async_step_user({"wirepod_url": f"  {self.base}/  "})
        self.assertEqual(res1["step_id"], "robot")
        res2 = await flow.async_step_robot({"serial": SERIAL, "name": "Test Bot"})
        self.assertEqual(res2["type"], "create_entry")
        self.assertEqual(res2["title"], "Test Bot")
        bad = await flow.async_step_user({"wirepod_url": "ftp://bad"})
        self.assertEqual(bad["errors"]["base"], "invalid_url")
        entry = self.ha_ce.ConfigEntry({"wirepod_url": "http://127.0.0.1:9", "serial": SERIAL})
        flow.reconfigure_entry = entry
        rec = await flow.async_step_reconfigure({"wirepod_url": self.base})
        self.assertEqual(rec["type"], "abort")
        self.assertEqual(entry.data["wirepod_url"], self.base)
        entry_other = self.ha_ce.ConfigEntry({"wirepod_url": self.base, "serial": "9999dead"})
        flow.reconfigure_entry = entry_other
        rec_err = await flow.async_step_reconfigure({"wirepod_url": self.base})
        self.assertEqual(rec_err["errors"]["base"], "robot_not_on_server")
        opt = cf_mod.VectorOptionsFlow()
        opt.config_entry = entry
        opt_res = await opt.async_step_init({"still_refresh_s": 120, "battery_poll_s": 45})
        self.assertEqual(opt_res["type"], "create_entry")
        self.assertEqual(opt_res["data"]["battery_poll_s"], 45)


if __name__ == "__main__":
    unittest.main()
