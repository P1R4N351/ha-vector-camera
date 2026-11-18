"""Offline tests for the Vector camera policy, against a fake wire-pod.

Authored-by: CPCS. Needs aiohttp and Pillow; no Home Assistant.
Run: python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import ast
import asyncio
import io
import pathlib
import struct
import sys
import unittest

from aiohttp import web
import aiohttp
from PIL import Image

PKG = pathlib.Path(__file__).resolve().parents[1] / "custom_components" / "vector_wirepod"
if str(PKG) not in sys.path:
    sys.path.insert(0, str(PKG))
import wirepod as wp  # noqa: E402

SERIAL = "0dd1cafe"


def jpeg(n: int) -> bytes:
    assert isinstance(n, int) and n >= 0
    out = io.BytesIO()
    Image.new("RGB", (32, 24), (n % 255, 40, 90)).save(out, format="JPEG")
    raw = out.getvalue()
    assert raw[:2] == wp.SOI and raw[-2:] == wp.EOI
    return raw


def jpeg_with_eoi_in_com(n: int) -> bytes:
    """Build a valid JPEG carrying an embedded FF D9 inside a COM header segment."""
    raw = jpeg(n)
    payload = b"\xff\xd9fake-exif-thumb-end"
    seg = b"\xff\xfe" + struct.pack(">H", len(payload) + 2) + payload
    out = raw[:2] + seg + raw[2:]
    assert out[:4] == b"\xff\xd8\xff\xfe"
    assert wp.EOI in seg
    return out


class FakeWirePod:
    """Mimics wire-pod's routes and its camera-stream behaviour."""

    def __init__(self) -> None:
        self.mode = "stream"
        self.frames_before = 3
        self.connections = 0
        self.active = 0
        self.other_paths: list[str] = []
        self.stopping = False
        self.battery = '{"battery_level":3,"battery_volts":3.55,"is_on_charger_platform":true}'
        self.battery_status = 200
        self.quiet_enabled = False
        self.quiet_supported = True
        self.quiet_status = 200
        self.cam_status = 504
        self.cam_error_body = b"error: camera_frame_timeout"
        self.injected_prefix = b""
        self.app = web.Application()
        self.app.router.add_get("/cam-stream", self.cam)
        self.app.router.add_get("/api-sdk/get_battery", self.bat)
        self.app.router.add_get("/api-sdk/get_sdk_info", self.info)
        self.app.router.add_route("*", "/api-sdk/quiet_mode", self.quiet)
        self.app.router.add_route("*", "/{tail:.*}", self.other)

    async def start(self) -> str:
        assert self.app is not None
        self.runner = web.AppRunner(self.app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        assert port > 0
        return f"http://127.0.0.1:{port}"

    async def stop(self) -> None:
        self.stopping = True
        await self.runner.cleanup()

    async def nap(self, seconds: float) -> None:
        assert seconds >= 0
        for _ in range(int(seconds / 0.05)):
            if self.stopping:
                return
            await asyncio.sleep(0.05)

    async def other(self, request: web.Request) -> web.Response:
        self.other_paths.append(request.path)
        return web.Response(status=404)

    async def info(self, request: web.Request) -> web.Response:
        return web.Response(text='{"robots":[{"esn":"0dd1cafe","ip_address":"192.0.2.9"}]}')

    async def bat(self, request: web.Request) -> web.Response:
        return web.Response(status=self.battery_status, text=self.battery)

    async def quiet(self, request: web.Request) -> web.Response:
        if not self.quiet_supported:
            return web.Response(status=404, text="not found")
        if self.quiet_status != 200:
            return web.json_response(
                {"enabled": False, "last_error": "quiet_mode failed"},
                status=self.quiet_status,
            )
        en = request.query.get("enable")
        if en == "true":
            self.quiet_enabled = True
        elif en == "false":
            self.quiet_enabled = False
        return web.json_response({
            "serial": request.query.get("serial", SERIAL),
            "enabled": self.quiet_enabled,
            "active": self.quiet_enabled,
            "holding_reserve": self.quiet_enabled,
            "muted": self.quiet_enabled,
            "robot_reachable": True,
            "pending_restore": False,
            "last_error": "",
            "updated_at": "2026-11-18T12:00:00Z",
        })

    async def cam(self, request: web.Request) -> web.StreamResponse:
        self.connections += 1
        self.active += 1
        try:
            return await self._cam(request)
        finally:
            self.active -= 1

    async def _send_cam_headers_mode(self, request: web.Request) -> web.StreamResponse | None:
        if self.mode == "hang":
            await self.nap(60)
            return web.Response(status=503)
        if self.mode == "slow_headers":
            await self.nap(0.35)
        if self.mode == "error":
            return web.Response(text="error: robot not found")
        if self.mode == "http_error":
            return web.Response(status=self.cam_status, body=self.cam_error_body)
        return None

    async def _cam(self, request: web.Request) -> web.StreamResponse:
        assert request is not None
        early = await self._send_cam_headers_mode(request)
        if early is not None:
            return early
        resp = web.StreamResponse()
        resp.headers["Content-Type"] = "multipart/x-mixed-replace; boundary=--boundary"
        await resp.prepare(request)
        if self.mode == "eof_now":
            return resp
        prefix, self.injected_prefix = self.injected_prefix, b""
        for i in range(100000):
            if self.stopping:
                return resp
            if self.mode in ("stall_after", "eof_after") and i >= self.frames_before:
                if self.mode == "eof_after":
                    return resp
                await self.nap(60)
                return resp
            try:
                head = prefix if i == 0 else b""
                await resp.write(b"--boundary\r\nContent-Type: image/jpeg\r\n\r\n" + head + jpeg(i))
            except ConnectionResetError:
                return resp
            await asyncio.sleep(0.02)
        return resp


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


def cfg(base: str, **kw) -> wp.HubConfig:
    base_kw = dict(header_timeout_s=0.4, stall_timeout_s=0.3, backoff_min_s=1.0,
                   backoff_max_s=3.0)
    base_kw.update(kw)
    return wp.HubConfig(base_url=base, serial=SERIAL, **base_kw)


async def settle(pred, limit_s: float = 2.0) -> bool:
    for _ in range(int(limit_s / 0.02)):
        if pred():
            return True
        await asyncio.sleep(0.02)
    return pred()


class Policy(unittest.TestCase):
    def test_only_read_paths_can_be_built(self):
        for path in ("/api-sdk/drive_straight", "/api-sdk/assume_behavior_control",
                     "/api-sdk/say_text", "/api-sdk/cam_stream", "/api-sdk/begin_event_stream"):
            with self.assertRaises(ValueError):
                wp.build_url("http://x", path, SERIAL)
        with self.assertRaises(ValueError):
            wp.build_url("http://x", "/cam-stream", "0050&x=1")
        self.assertEqual(wp.build_url("http://x/", "/cam-stream", SERIAL),
                         "http://x/cam-stream?serial=0dd1cafe")
        self.assertEqual(wp.build_url("http://x/", "/api-sdk/quiet_mode", SERIAL, enable=True),
                         "http://x/api-sdk/quiet_mode?serial=0dd1cafe&enable=true")
        with self.assertRaises(ValueError):
            wp.build_url("http://x/", "/cam-stream", SERIAL, enable=True)

    def test_source_names_no_other_wirepod_path(self):
        literals: set[str] = set()
        for src in PKG.glob("*.py"):
            for node in ast.walk(ast.parse(src.read_text())):
                if isinstance(node, ast.Constant) and isinstance(node.value, str):
                    literals.add(node.value)
        paths = {s for s in literals if s.startswith(("/api-sdk", "/cam"))}
        self.assertTrue(paths <= set(wp.ALLOWED_PATHS), paths - set(wp.ALLOWED_PATHS))
        actuating = ("behavior_control", "drive_", "turn_in_place", "move_", "set_head",
                     "set_lift", "say_text", "eye_color", "wake_word", "event_stream",
                     "play_sound", "volume")
        hits = sorted(s for s in literals for a in actuating if a in s)
        self.assertEqual(hits, [])


class Parser(unittest.TestCase):
    def test_frames_split_across_chunks_and_garbage(self):
        a, b = jpeg(1), jpeg(2)
        blob = b"--boundary\r\n\r\n" + a + b"\r\n--boundary\r\n" + b
        p = wp.FrameParser(100000)
        got = []
        for i in range(0, len(blob), 7):
            got += p.feed(blob[i:i + 7])
        self.assertEqual(got, [a, b])

    def test_tiny_marker_pair_is_not_a_frame(self):
        p = wp.FrameParser(100000)
        self.assertEqual(p.feed(b"\xff\xd8xx\xff\xd9" + jpeg(3)), [jpeg(3)])
        self.assertEqual(p.dropped, 1)

    def test_oversized_frame_is_dropped_and_parser_recovers(self):
        p = wp.FrameParser(500)
        self.assertEqual(p.feed(b"\xff\xd8" + b"\x00" * 600), [])
        self.assertEqual(p.dropped, 1)
        self.assertEqual(p.feed(jpeg(4)), [jpeg(4)])

    def test_jpeg_header_segment_containing_eoi_bytes_is_not_truncated(self):
        frame = jpeg_with_eoi_in_com(7)
        p = wp.FrameParser(100000)
        got = []
        for i in range(0, len(frame), 13):
            got += p.feed(frame[i:i + 13])
        self.assertEqual(got, [frame])
        self.assertEqual(p.dropped, 0)
        img = Image.open(io.BytesIO(got[0]))
        img.verify()


class StatusImage(unittest.TestCase):
    def test_renders_jpeg_with_and_without_frame(self):
        import placeholder
        lines = ["Vector DEMO", "Not live: camera no frames",
                 "no frames within 6 s (robot asleep, or camera busy); next try in 60 s"]
        for frame in (None, jpeg(5), b"not a jpeg"):
            out = placeholder.render(lines, frame)
            img = Image.open(io.BytesIO(out))
            self.assertEqual((img.format, img.size), ("JPEG", (640, 360)))
        self.assertNotEqual(placeholder.render(lines, None), jpeg(5))


class HubTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.pod = FakeWirePod()
        self.base = await self.pod.start()
        self.session = aiohttp.ClientSession()
        self.clock = Clock()

    async def asyncTearDown(self):
        await self.session.close()
        await self.pod.stop()

    def hub(self, **kw) -> wp.CameraHub:
        return wp.CameraHub(self.session, cfg(self.base, **kw), clock=self.clock)

    async def test_viewers_share_one_upstream(self):
        hub = self.hub()
        a, b = hub.acquire(), hub.acquire()
        self.assertIsNotNone(await hub.next_frame(0, 2))
        got = await hub.next_frame(hub.seq, 2)
        self.assertIsNotNone(got)
        self.assertEqual(self.pod.connections, 1)
        self.assertEqual(hub.sessions_opened, 1)
        self.assertEqual(hub.state, wp.STATE_STREAMING)
        a.release(), b.release()
        await hub.close()

    async def test_last_release_closes_upstream_and_returns_idle(self):
        hub = self.hub()
        lease = hub.acquire()
        self.assertIsNotNone(await hub.next_frame(0, 2))
        self.assertEqual(self.pod.active, 1)
        self.assertEqual(hub.state, wp.STATE_STREAMING)
        lease.release()
        self.assertEqual(hub.state, wp.STATE_IDLE)
        self.assertTrue(await settle(lambda: self.pod.active == 0, 1.5))
        lease.release()
        self.assertEqual(hub.viewers, 0)
        self.pod.mode = "hang"
        lease2 = hub.acquire()
        await settle(lambda: hub.state == wp.STATE_STARTING, 1.0)
        lease2.release()
        self.assertEqual(hub.state, wp.STATE_IDLE)
        await hub.close()

    async def test_sleeping_robot_gives_up_and_backs_off(self):
        self.pod.mode = "hang"
        hub = self.hub()
        lease = hub.acquire()
        loop = asyncio.get_running_loop()
        t0 = loop.time()
        self.assertIsNone(await hub.next_frame(0, 5))
        self.assertLess(loop.time() - t0, 1.5)
        self.assertEqual(hub.state, wp.STATE_NO_FRAMES)
        self.assertIn("no frames", hub.detail)
        self.assertGreater(hub.retry_in_s(), 0)
        lease.release()
        again = hub.acquire()
        self.assertIsNone(await hub.still(10, True, 0.5))
        await asyncio.sleep(0.2)
        self.assertEqual(self.pod.connections, 1)
        again.release()
        await hub.close()

    async def test_backoff_doubles_and_caps(self):
        self.pod.mode = "hang"
        hub = self.hub()
        delays = []
        for _ in range(4):
            lease = hub.acquire()
            await hub.next_frame(0, 5)
            lease.release()
            delays.append(round(hub.retry_in_s(), 3))
            self.clock.t += hub.retry_in_s() + 0.01
        self.assertEqual(delays, [1.0, 2.0, 3.0, 3.0])
        self.assertEqual(self.pod.connections, 4)
        await hub.close()

    async def test_success_resets_backoff(self):
        hub = self.hub()
        for mode in ("hang", "stream", "hang"):
            self.pod.mode = mode
            lease = hub.acquire()
            await hub.next_frame(hub.seq, 5)
            lease.release()
            await settle(lambda: self.pod.active == 0, 1.5)
            if mode == "hang":
                self.assertEqual(round(hub.retry_in_s(), 3), 1.0)
                self.clock.t += 1.01
            else:
                self.assertEqual(hub.retry_in_s(), 0)
        await hub.close()

    async def test_headers_slower_than_stall_limit_are_waited_for(self):
        self.pod.mode = "slow_headers"
        hub = self.hub()
        lease = hub.acquire()
        self.assertIsNotNone(await hub.next_frame(0, 2))
        self.assertEqual(hub.state, wp.STATE_STREAMING)
        lease.release()
        await hub.close()

    async def test_listeners_hear_state_changes_not_frames(self):
        hub = self.hub()
        seen = []
        remove = hub.add_listener(lambda: seen.append(hub.state))
        lease = hub.acquire()
        await hub.next_frame(0, 2)
        await hub.next_frame(hub.seq, 2)
        await hub.next_frame(hub.seq, 2)
        self.assertEqual(seen, [wp.STATE_STARTING, wp.STATE_STREAMING])
        remove()
        hub.set_allowed(False)
        self.assertEqual(len(seen), 2)
        lease.release()
        await hub.close()

    async def test_wirepod_error_text_and_http_error_are_failures(self):
        self.pod.mode = "error"
        hub = self.hub()
        lease = hub.acquire()
        self.assertIsNone(await hub.next_frame(0, 2))
        self.assertEqual(hub.state, wp.STATE_NO_FRAMES)
        self.assertIn("robot not found", hub.detail)
        lease.release()
        self.clock.t += 5.0
        self.pod.mode = "http_error"
        self.pod.cam_status = 504
        self.pod.cam_error_body = b"error: camera_frame_timeout"
        lease2 = hub.acquire()
        self.assertIsNone(await hub.next_frame(0, 2))
        self.assertIn("504", hub.detail)
        self.assertIn("camera_frame_timeout", hub.detail)
        lease2.release()
        await hub.close()

    async def test_stream_ending_before_a_frame_is_a_failure(self):
        self.pod.mode = "eof_now"
        hub = self.hub()
        lease = hub.acquire()
        self.assertIsNone(await hub.next_frame(0, 2))
        self.assertEqual(hub.state, wp.STATE_NO_FRAMES)
        lease.release()
        await hub.close()

    async def test_withheld_closes_refuses_and_keeps_precedence(self):
        hub = self.hub()
        lease = hub.acquire()
        self.assertIsNotNone(await hub.next_frame(0, 2))
        hub.set_allowed(True)
        self.assertEqual(hub.state, wp.STATE_STREAMING)
        hub.set_allowed(False)
        self.assertTrue(await settle(lambda: self.pod.active == 0, 1.5))
        self.assertEqual(hub.state, wp.STATE_WITHHELD)
        hub.set_robot_reachable(False, "robot down")
        self.assertEqual(hub.state, wp.STATE_WITHHELD)
        hub.set_robot_reachable(True)
        self.assertEqual(hub.state, wp.STATE_WITHHELD)
        other = hub.acquire()
        await asyncio.sleep(0.15)
        self.assertEqual(self.pod.connections, 1)
        hub.set_robot_reachable(False, "robot down")
        hub.set_allowed(True)
        self.assertEqual(hub.state, wp.STATE_ROBOT_UNREACHABLE)
        self.assertEqual(hub.detail, "robot down")
        hub.set_robot_reachable(True)
        self.assertIsNotNone(await hub.next_frame(hub.seq, 2))
        self.assertEqual(self.pod.connections, 2)
        lease.release(), other.release()
        await hub.close()

    async def test_waiting_viewer_auto_starts_after_backoff_and_wake(self):
        self.pod.mode = "hang"
        hub = self.hub()
        lease = hub.acquire()
        self.assertIsNone(await hub.next_frame(0, 5))
        self.clock.t += 2.0
        self.pod.mode = "stream"
        got = await hub.next_frame(0, 2)
        self.assertIsNotNone(got)
        self.assertEqual(self.pod.connections, 2)
        hub.set_allowed(False)
        wait_task = asyncio.create_task(hub.wait_wake(hub.seq, 2.0))
        await asyncio.sleep(0.05)
        hub.set_allowed(True)
        woken = await wait_task
        self.assertIsNotNone(woken)
        lease.release()
        await hub.close()

    async def test_still_uses_cache_and_refreshes_briefly(self):
        hub = self.hub()
        self.assertIsNone(await hub.still(10, False, 1))
        self.assertEqual(self.pod.connections, 0)
        first = await hub.still(10, True, 2)
        self.assertIsNotNone(first)
        self.assertEqual(self.pod.connections, 1)
        self.assertTrue(await settle(lambda: self.pod.active == 0, 1.5))
        self.assertEqual(await hub.still(10, True, 2), first)
        self.assertEqual(self.pod.connections, 1)
        self.clock.t += 11
        self.assertIsNotNone(await hub.still(10, True, 2))
        self.assertEqual(self.pod.connections, 2)
        await hub.close()

    async def test_stall_after_frames_and_dropped_diagnostics(self):
        self.pod.mode = "stall_after"
        self.pod.injected_prefix = b"\xff\xd8xx\xff\xd9"
        hub = self.hub()
        lease = hub.acquire()
        self.assertIsNotNone(await hub.next_frame(0, 2))
        self.assertTrue(await settle(lambda: hub.sessions_opened >= 2, 2.0))
        self.assertEqual(hub.retry_in_s(), 0)
        self.assertNotEqual(hub.state, wp.STATE_NO_FRAMES)
        self.assertGreaterEqual(hub.dropped_frames, 1)
        self.assertEqual(hub.diagnostics()["dropped_frames"], hub.dropped_frames)
        lease.release()
        await hub.close()

    async def test_unreachable_robot_is_not_asked(self):
        hub = self.hub()
        hub.set_robot_reachable(False, "robot not answering wire-pod")
        lease = hub.acquire()
        await asyncio.sleep(0.2)
        self.assertEqual(self.pod.connections, 0)
        self.assertEqual(hub.state, wp.STATE_ROBOT_UNREACHABLE)
        hub.set_robot_reachable(True)
        self.assertIsNotNone(await hub.next_frame(0, 2))
        self.assertEqual(self.pod.connections, 1)
        lease.release()
        await hub.close()

    async def test_read_calls(self):
        robots = await wp.get_sdk_info(self.session, self.base)
        self.assertEqual(robots, [{"esn": SERIAL, "ip_address": "192.0.2.9"}])
        bat = await wp.get_battery(self.session, self.base, SERIAL)
        self.assertEqual(bat["battery_level"], 3)
        self.assertIs(bat["is_on_charger_platform"], True)
        self.assertIs(bat["is_charging"], False)
        self.pod.battery = '{"battery_volts": 3.8126}'
        omitted = await wp.get_battery(self.session, self.base, SERIAL)
        self.assertIs(omitted["is_on_charger_platform"], False)
        self.assertIs(omitted["is_charging"], False)
        self.assertEqual(omitted["battery_level"], 0)
        self.assertEqual(omitted["battery_volts"], 3.813)
        self.pod.battery_status = 502
        self.pod.battery = '{"error": "bad gateway"}'
        with self.assertRaises(wp.WirePodError):
            await wp.get_battery(self.session, self.base, SERIAL)
        self.pod.battery_status = 200
        self.pod.battery = "error: robot unreachable"
        with self.assertRaises(wp.WirePodError):
            await wp.get_battery(self.session, self.base, SERIAL)
        self.assertEqual(self.pod.other_paths, [])

    async def test_quiet_mode_calls_and_errors(self):
        st0 = await wp.get_quiet_mode(self.session, self.base, SERIAL)
        self.assertEqual(st0["enabled"], False)
        st1 = await wp.set_quiet_mode(self.session, self.base, SERIAL, True)
        self.assertTrue(st1["enabled"])
        self.assertTrue(st1["holding_reserve"])
        self.assertTrue(st1["muted"])
        st2 = await wp.set_quiet_mode(self.session, self.base, SERIAL, False)
        self.assertFalse(st2["enabled"])
        self.pod.quiet_supported = False
        self.assertIsNone(await wp.get_quiet_mode(self.session, self.base, SERIAL))
        self.pod.quiet_supported = True
        self.pod.quiet_status = 500
        with self.assertRaises(wp.WirePodError):
            await wp.set_quiet_mode(self.session, self.base, SERIAL, True)
        with self.assertRaises(wp.WirePodError):
            wp._normalize_quiet_mode({"enabled": "yes"})


if __name__ == "__main__":
    unittest.main()
