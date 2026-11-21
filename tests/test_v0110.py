"""Tests for 0.11.0 runtime cryo_mute client, zero-restart Cryosleep, and drift recovery."""
from __future__ import annotations

import asyncio
import tempfile
import unittest

import aiohttp
from aiohttp import web

from test_v0100 import CRYO, MODS, POD, WIREPOD, _CoordStub, _HassStub, _ScriptedPod, _new_pod_app


async def _cryo_http_handler(request: web.Request) -> web.Response:
    assert request is not None
    serial = request.query.get("serial")
    if serial == "4040":
        return web.Response(status=404, text="404 page not found")
    if serial == "5020":
        return web.json_response({"ok": False, "error": "robot_console_failed"}, status=502)
    if serial == "5030":
        return web.json_response({"ok": False, "error": "robot_ssh_not_configured"}, status=503)
    if serial == "badbool":
        return web.json_response({"ok": True, "muted": True, "pod_gated": True, "robot_muted": "yes"})
    if serial == "badjson":
        return web.Response(text="not-json")
    m = request.query.get("muted") == "true" if request.method == "POST" else False
    return web.json_response({"ok": True, "muted": m, "pod_gated": m, "robot_muted": m})


class CryoMuteUrlAndHttpTests(unittest.TestCase):
    def test_url_and_bad_args(self) -> None:
        self.assertEqual(
            WIREPOD.build_url(POD, "/api-sdk/cryo_mute", "0dd1cafe", muted=True),
            "http://pod:8080/api-sdk/cryo_mute?serial=0dd1cafe&muted=true",
        )
        self.assertEqual(
            WIREPOD.build_url(POD, "/api-sdk/cryo_mute", "0dd1cafe", muted=False),
            "http://pod:8080/api-sdk/cryo_mute?serial=0dd1cafe&muted=false",
        )
        for bad in (1, 0, "true", None.__class__):
            with self.assertRaises(ValueError):
                WIREPOD.build_url(POD, "/api-sdk/cryo_mute", "0dd1cafe", muted=bad)  # type: ignore[arg-type]

    def test_get_and_set_http_answers_and_errors(self) -> None:
        async def run() -> dict:
            app = web.Application()
            app.router.add_route("*", "/api-sdk/cryo_mute", _cryo_http_handler)
            runner = web.AppRunner(app)
            await runner.setup()
            await web.TCPSite(runner, "127.0.0.1", 0).start()
            base = f"http://127.0.0.1:{runner.addresses[0][1]}"
            out: dict = {}
            try:
                async with aiohttp.ClientSession() as session:
                    out["ok_set"] = await WIREPOD.set_cryo_mute(session, base, "0dd1cafe", True)
                    out["ok_get"] = await WIREPOD.get_cryo_mute(session, base, "0dd1cafe")
                    out["missing"] = await WIREPOD.get_cryo_mute(session, base, "4040")
                    for key in ("5020", "5030", "badbool", "badjson"):
                        try:
                            await WIREPOD.get_cryo_mute(session, base, key)
                            out[key] = "NO_ERROR"
                        except WIREPOD.WirePodError as exc:
                            out[key] = str(exc)
            finally:
                await runner.cleanup()
            return out

        out = asyncio.run(run())
        self.assertEqual(out["ok_set"], {"muted": True, "pod_gated": True, "robot_muted": True})
        self.assertEqual(out["ok_get"], {"muted": False, "pod_gated": False, "robot_muted": False})
        self.assertIsNone(out["missing"])
        self.assertIn("robot_console_failed", out["5020"])
        self.assertIn("503", out["5030"])
        self.assertIn("invalid answer", out["badbool"])
        self.assertIn("not JSON", out["badjson"])


class CryosleepRuntimeMuteTests(unittest.TestCase):
    def test_sleeping_robot_uses_only_cryo_mute_and_never_restarts_or_wakes(self) -> None:
        pod = _ScriptedPod(quiet=False, volume=4, backlight=0, mic_muted=False, asleep=True)
        seen: list[tuple[str, str]] = []

        async def run() -> dict:
            runner = web.AppRunner(_new_pod_app(pod, seen_paths=seen))
            await runner.setup()
            await web.TCPSite(runner, "127.0.0.1", 0).start()
            base = f"http://127.0.0.1:{runner.addresses[0][1]}"
            with tempfile.TemporaryDirectory() as tmp:
                try:
                    async with aiohttp.ClientSession() as session:
                        hass = _HassStub(tmp)
                        hub = WIREPOD.CameraHub(session, WIREPOD.HubConfig(base_url=base, serial="0dd1cafe"))
                        entry = type("E", (), {"title": "DEMO", "data": {"serial": "0dd1cafe", "wirepod_url": base}, "options": {}, "runtime_data": None})()
                        mgr = MODS["manager"].VectorManager(hass, entry, session, hub)
                        mgr.coordinator = _CoordStub({"robot_reachable": True, "quiet_mode": {"enabled": False}})
                        mgr.start_cryosleep()
                        await asyncio.gather(*hass.tasks)
                        hass.tasks.clear()
                        during = (pod.pod_gated, pod.robot_muted, pod.mic_muted, pod.volume)
                        mgr.stop_cryosleep()
                        await asyncio.gather(*hass.tasks)
                        return {"during": during, "after": (pod.pod_gated, pod.robot_muted, pod.volume)}
                finally:
                    await runner.cleanup()

        out = asyncio.run(run())
        self.assertEqual(out["during"], (True, True, False, 4))
        self.assertEqual(out["after"], (False, False, 4))
        self.assertEqual([m for m, p in seen if p == "/api-sdk/cryo_mute"], ["POST", "POST"])
        for forbidden in ("/api-sdk/mic_mute", "/api-sdk/volume", "/api-sdk/cloud_intent"):
            self.assertFalse(any(p == forbidden for _, p in seen), f"unexpected call to {forbidden}: {seen}")

    def test_drift_guard_and_quiet_unlock_reassert_cryo_mute(self) -> None:
        pod = _ScriptedPod(quiet=False, volume=3, backlight=5, asleep=True)
        st = CRYO.CryosleepState()
        st.arm(camera_allowed=True)
        asyncio.run(CRYO.execute_enter(st, pod.io(), lambda: False, quiet_settle_s=0.0))
        self.assertTrue(pod.pod_gated and pod.robot_muted)

        pod.robot_muted = False
        pod.backlight = 10
        pod.log.clear()
        asyncio.run(CRYO.execute_poll_action(CRYO.POLL_DRIFT_GUARD, st, pod.io(), lambda: False, quiet_settle_s=0.0))
        self.assertTrue(pod.robot_muted)
        self.assertEqual(pod.backlight, 0)
        self.assertIn(("set_cryo_mute", True), pod.log)

        st.holding_reserve = True
        pod.log.clear()
        asyncio.run(CRYO.execute_poll_action(CRYO.POLL_SETTLED_ASLEEP, st, pod.io(), lambda: False, quiet_settle_s=0.0))
        names = [k for k, _ in pod.log]
        self.assertEqual(names, ["set_quiet", "set_cryo_mute", "set_backlight"])
        self.assertFalse(st.holding_reserve)


if __name__ == "__main__":
    unittest.main()
