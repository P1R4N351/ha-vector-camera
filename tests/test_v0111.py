"""Tests for 0.11.1 Cryosleep fixes: 0 ms control re-enablement and zero-wake sleep entry."""
from __future__ import annotations

import asyncio
import tempfile
import types
import unittest

import aiohttp
from aiohttp import web

from test_v0100 import CRYO, MODS, WIREPOD, _CoordStub, _HassStub, _ScriptedPod, _new_pod_app


async def _async_state(state: str, attrs: dict | None = None):
    return types.SimpleNamespace(state=state, attributes=attrs or {})


def _gated_pod_app(pod: _ScriptedPod, seen: list[tuple[str, str]], gate: asyncio.Event):
    assert pod is not None and gate is not None
    base_app = _new_pod_app(pod, seen_paths=seen)

    async def handle(req: web.Request) -> web.Response:
        if req.path == "/api-sdk/cryo_mute" and req.method == "POST" and req.query.get("muted") == "false":
            await gate.wait()
        match = await base_app.router.resolve(req)
        return await match.handler(req)

    app = web.Application()
    for path in (
        "/api-sdk/quiet_mode", "/api-sdk/get_sdk_settings", "/api-sdk/volume",
        "/api-sdk/backlight", "/api-sdk/mic_mute", "/api-sdk/cryo_mute",
        "/api-sdk/robot_state", "/api-sdk/get_battery", "/api-sdk/cloud_intent",
    ):
        app.router.add_route("*", path, handle)
    return app


def _make_entities(session: aiohttp.ClientSession, base: str, tmp: str, *, asleep: bool = True):
    assert session is not None and bool(base)
    hass = _HassStub(tmp)
    hub = WIREPOD.CameraHub(session, WIREPOD.HubConfig(base_url=base, serial="0dd1cafe"))
    entry = types.SimpleNamespace(
        title="Vector DEMO", data={"serial": "0dd1cafe", "wirepod_url": base}, options={}, runtime_data=None,
    )
    mgr = MODS["manager"].VectorManager(hass, entry, session, hub)
    coord = _CoordStub({"robot_reachable": True, "quiet_mode": {"enabled": False}, "robot_state": {"asleep": asleep}})
    mgr.coordinator = coord
    entry.runtime_data = types.SimpleNamespace(
        hub=hub, coordinator=coord, session=session, base_url=base, serial="0dd1cafe", manager=mgr,
    )
    ents = {
        "cryo": MODS["switch"].CryosleepSwitch(entry),
        "cam": MODS["switch"].CameraAllowedSwitch(entry),
        "quiet": MODS["switch"].QuietModeSwitch(entry),
        "wake_photo": MODS["switch"].PhotoOnWakeSwitch(entry),
        "mic": MODS["switch"].MicMuteSwitch(entry),
        "bl": MODS["light"].FaceBacklight(entry),
        "vol": MODS["number"].VectorVolumeNumber(entry),
        "dock": MODS["button"].DockButton(entry),
    }
    return hass, mgr, ents


async def _bind_entities(ents: dict, last_states: dict | None = None) -> tuple[list, list]:
    assert "cryo" in ents and isinstance(ents, dict)
    refreshes, writes = [], []
    for name, ent in ents.items():
        ent.async_on_remove = lambda _fn: None
        ent.async_write_ha_state = lambda n=name: writes.append(n)
        ent.async_schedule_update_ha_state = lambda force_refresh=False, n=name: refreshes.append((n, force_refresh))
        state_val = (last_states or {}).get(name)
        ent.async_get_last_state = (lambda s=state_val: _async_state(*s)) if state_val else (lambda: _async_state("off"))
        await ent.async_added_to_hass()
    for key in ("mic", "bl", "vol"):
        await ents[key].async_update()
    return refreshes, writes


async def _run_gated_unlock_scenario(pod: _ScriptedPod, seen: list[tuple[str, str]]) -> dict:
    assert pod is not None and isinstance(seen, list)
    gate = asyncio.Event()
    runner = web.AppRunner(_gated_pod_app(pod, seen, gate))
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", 0).start()
    base = f"http://127.0.0.1:{runner.addresses[0][1]}"
    with tempfile.TemporaryDirectory() as tmp:
        try:
            async with aiohttp.ClientSession() as session:
                hass, mgr, ents = _make_entities(session, base, tmp, asleep=False)
                refreshes, _ = await _bind_entities(ents, {"cam": ("on", {})})
                await ents["cryo"].async_turn_on()
                await asyncio.gather(*hass.tasks)
                hass.tasks.clear()
                refreshes.clear()
                await ents["cryo"].async_turn_off()
                immediate = {
                    "cryo_on": ents["cryo"].is_on,
                    "phase": mgr.cryosleep.phase,
                    "avail": {k: ents[k].available for k in ("cam", "quiet", "wake_photo", "mic", "bl", "vol", "dock")},
                    "hub_allowed": mgr.hub.allowed,
                    "premature_refreshes": list(refreshes),
                }
                await ents["cam"].async_turn_off()
                await ents["cam"].async_turn_on()
                await ents["bl"].async_turn_on(brightness=255)
                seen.clear()
                gate.set()
                await asyncio.gather(*hass.tasks)
                return {**immediate, "seen_after_gate": list(seen), "final_bl": pod.backlight, "final_refreshes": sorted(refreshes), "final_phase": mgr.cryosleep.phase}
        finally:
            await runner.cleanup()


class CryosleepInstantUnlockTests(unittest.TestCase):
    def test_disable_unlocks_all_controls_at_zero_ms_before_exit_finishes(self) -> None:
        pod = _ScriptedPod(quiet=False, volume=4, backlight=12, mic_muted=False, asleep=False)
        out = asyncio.run(_run_gated_unlock_scenario(pod, []))
        self.assertFalse(out["cryo_on"])
        self.assertEqual(out["phase"], CRYO.PHASE_EXITING)
        self.assertTrue(all(out["avail"].values()), out["avail"])
        self.assertTrue(out["hub_allowed"])
        self.assertEqual(out["premature_refreshes"], [])
        self.assertEqual(out["final_bl"], 20)
        self.assertFalse(any(p == "/api-sdk/robot_state" for _, p in out["seen_after_gate"]))
        self.assertEqual(out["final_refreshes"], [("bl", True), ("mic", True), ("vol", True)])
        self.assertEqual(out["final_phase"], CRYO.PHASE_OFF)

    def test_disable_cancels_stalled_enter_and_restores_immediately(self) -> None:
        pod = _ScriptedPod(quiet=False, volume=3, backlight=15, asleep=True)

        async def run() -> dict:
            release_enter, stalled_called = asyncio.Event(), asyncio.Event()
            runner = web.AppRunner(_new_pod_app(pod))
            await runner.setup()
            await web.TCPSite(runner, "127.0.0.1", 0).start()
            base = f"http://127.0.0.1:{runner.addresses[0][1]}"

            async def blocked_state():
                stalled_called.set()
                await release_enter.wait()
                return await pod._get_robot_state()

            with tempfile.TemporaryDirectory() as tmp:
                try:
                    async with aiohttp.ClientSession() as session:
                        hass, mgr, ents = _make_entities(session, base, tmp, asleep=True)
                        await _bind_entities(ents, {"cam": ("on", {})})
                        io = mgr._cryo_io()
                        stalled_io = types.SimpleNamespace(**{**io.__dict__, "get_robot_state": blocked_state})
                        mgr._cryo_io = lambda: stalled_io  # type: ignore[method-assign]
                        enter_task = mgr.start_cryosleep()
                        await stalled_called.wait()
                        mgr.stop_cryosleep()
                        avail_now = all(ents[k].available for k in ("cam", "quiet", "wake_photo", "mic", "bl", "vol", "dock"))
                        hub_now = mgr.hub.allowed
                        await asyncio.gather(*hass.tasks)
                        return {"cancelled": enter_task.cancelled() or enter_task.done(), "avail_now": avail_now, "hub_now": hub_now, "phase": mgr.cryosleep.phase}
                finally:
                    await runner.cleanup()

        out = asyncio.run(run())
        self.assertTrue(out["cancelled"])
        self.assertTrue(out["avail_now"])
        self.assertTrue(out["hub_now"])
        self.assertEqual(out["phase"], CRYO.PHASE_OFF)

    def test_boot_during_cryosleep_seeds_entities_for_instant_unlock(self) -> None:
        pod = _ScriptedPod(quiet=False, volume=4, backlight=0, mic_muted=False, asleep=True)

        async def run() -> dict:
            runner = web.AppRunner(_new_pod_app(pod))
            await runner.setup()
            await web.TCPSite(runner, "127.0.0.1", 0).start()
            base = f"http://127.0.0.1:{runner.addresses[0][1]}"
            with tempfile.TemporaryDirectory() as tmp:
                try:
                    async with aiohttp.ClientSession() as session:
                        hass, mgr, ents = _make_entities(session, base, tmp, asleep=True)
                        attrs = {"saved_backlight": 14, "saved_camera_allowed": True, "snapshot_taken": True}
                        await _bind_entities(ents, {"cryo": ("on", attrs)})
                        await asyncio.gather(*hass.tasks)
                        hass.tasks.clear()
                        self.assertFalse(ents["mic"].available or ents["bl"].available or ents["vol"].available)
                        mgr.stop_cryosleep()
                        out = {
                            "mic": ents["mic"].available, "bl": ents["bl"].available, "vol": ents["vol"].available,
                            "bl_level": ents["bl"].extra_state_attributes["level"], "vol_val": ents["vol"].native_value,
                        }
                        await asyncio.gather(*hass.tasks)
                        return out
                finally:
                    await runner.cleanup()

        out = asyncio.run(run())
        self.assertEqual((out["mic"], out["bl"], out["vol"]), (True, True, True))
        self.assertEqual((out["bl_level"], out["vol_val"]), (14, 4.0))


class CryosleepZeroWakeTests(unittest.TestCase):
    def test_enter_timeout_or_flicker_never_sends_cloud_sleep_intent(self) -> None:
        async def _timeout_state():
            raise WIREPOD.WirePodError("robot_state timeout")

        for asleep_val, timeout_err, known in ((True, False, None), (False, False, None), (None, True, True), (None, True, False)):
            pod = _ScriptedPod(quiet=False, volume=4, backlight=10, asleep=bool(asleep_val))
            io = pod.io()
            if timeout_err:
                io = types.SimpleNamespace(**{**io.__dict__, "get_robot_state": _timeout_state})  # type: ignore[assignment]
            st = CRYO.CryosleepState()
            st.arm(camera_allowed=True)
            asyncio.run(CRYO.execute_enter(st, io, lambda: False, quiet_settle_s=0.0, known_asleep=known))
            self.assertNotIn(("send_sleep", None), pod.log)
            self.assertTrue(pod.pod_gated and pod.robot_muted)

        async def _none_state():
            return None

        pod_no_con = _ScriptedPod(console_ok=False, backlight=10, asleep=True)
        io_no_con = types.SimpleNamespace(**{**pod_no_con.io().__dict__, "get_robot_state": _none_state})  # type: ignore[assignment]
        st2 = CRYO.CryosleepState()
        st2.arm(camera_allowed=True)
        asyncio.run(CRYO.execute_enter(st2, io_no_con, lambda: False, quiet_settle_s=0.0, known_asleep=None))
        self.assertTrue(st2.applied)
        self.assertNotIn(("send_sleep", None), pod_no_con.log)

    def test_flicker_debounce_and_reenforce_never_wake_via_intent(self) -> None:
        st = CRYO.CryosleepState()
        st.arm(camera_allowed=True)
        st.applied, st.phase, st.was_asleep = True, CRYO.PHASE_ACTIVE, True
        self.assertEqual(st.decide_poll(True, False, 110.0), CRYO.POLL_NOOP)
        self.assertEqual(st.decide_poll(True, True, 120.0), CRYO.POLL_NOOP)
        self.assertEqual(st.decide_poll(True, False, 130.0), CRYO.POLL_NOOP)
        self.assertEqual(st.decide_poll(True, False, 140.0), CRYO.POLL_REENFORCE_SLEEP)

        pod = _ScriptedPod(quiet=False, volume=4, backlight=5, asleep=False)
        asyncio.run(CRYO.execute_poll_action(CRYO.POLL_REENFORCE_SLEEP, st, pod.io(), lambda: False, quiet_settle_s=0.0))
        self.assertTrue(pod.pod_gated and pod.robot_muted)
        self.assertEqual(pod.backlight, 0)
        self.assertNotIn(("send_sleep", None), pod.log)


if __name__ == "__main__":
    unittest.main()
