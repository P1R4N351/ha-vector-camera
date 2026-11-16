"""Passive access to one Vector's camera through wire-pod.

Authored-by: CPCS. No Home Assistant imports, so the policy below is testable
on its own.

What "compliant with Vector" means here, each point enforced in this file:

1. Read only. The only wire-pod paths ever requested are ALLOWED_PATHS. No
   behaviour control, motion, speech, eye colour, wake word or event stream.
   Nothing here can wake the robot or take it from what it is doing.
2. One upstream per robot. wire-pod serves one camera viewer per robot and
   ends the previous one when a new one arrives, so every viewer shares a
   single /cam-stream connection, opened only while someone is watching and
   closed as soon as nobody is.
3. Every wait has a deadline. wire-pod sends no headers until the robot starts
   its camera, and a sleeping robot never does. A start that yields no frame
   within header_timeout_s is abandoned.
4. A sleeping robot is asked rarely. Each failed start doubles the wait before
   the next one, from backoff_min_s to backoff_max_s. Each abandoned start
   leaves a blocked request inside wire-pod (its handler has no deadline), so
   this bounds that cost too.
5. The camera can be withheld. When not allowed (for example during a
   navigation run that needs the camera), nothing connects and a live
   connection is closed.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import quote

import aiohttp

ALLOWED_PATHS = ("/api-sdk/get_sdk_info", "/api-sdk/get_battery", "/cam-stream")

SOI = b"\xff\xd8"
EOI = b"\xff\xd9"
MIN_FRAME_BYTES = 64
MAX_FRAMES_PER_CHUNK = 64
MAX_WAKEUPS = 10_000
MAX_LISTENERS = 16

STATE_IDLE = "idle"
STATE_STARTING = "starting"
STATE_STREAMING = "streaming"
STATE_NO_FRAMES = "no_frames"
STATE_WITHHELD = "withheld"
STATE_ROBOT_UNREACHABLE = "robot_unreachable"


class WirePodError(Exception):
    """wire-pod answered, but not with what was asked for."""


def build_url(base_url: str, path: str, serial: str | None = None) -> str:
    """Return a wire-pod URL. Refuses any path outside ALLOWED_PATHS."""
    if path not in ALLOWED_PATHS:
        raise ValueError(f"path not allowed: {path!r}")
    if not base_url.startswith(("http://", "https://")):
        raise ValueError("base_url must be http(s)")
    url = base_url.rstrip("/") + path
    if serial is not None:
        if not serial.isalnum():
            raise ValueError(f"invalid serial: {serial!r}")
        url += "?serial=" + quote(serial)
    return url


async def get_sdk_info(session: aiohttp.ClientSession, base_url: str,
                       timeout_s: float = 10.0) -> list[dict]:
    """Robots wire-pod knows: [{'esn': ..., 'ip_address': ...}]."""
    url = build_url(base_url, "/api-sdk/get_sdk_info")
    tmo = aiohttp.ClientTimeout(total=timeout_s)
    async with session.get(url, timeout=tmo) as resp:
        text = await resp.text()
        if resp.status != 200:
            raise WirePodError(f"get_sdk_info: http {resp.status}")
    try:
        robots = json.loads(text).get("robots") or []
    except (ValueError, AttributeError) as exc:
        raise WirePodError(f"get_sdk_info: not JSON: {text[:80]!r}") from exc
    return [{"esn": str(r.get("esn", "")), "ip_address": str(r.get("ip_address", ""))}
            for r in robots if isinstance(r, dict) and r.get("esn")]


async def get_battery(session: aiohttp.ClientSession, base_url: str, serial: str,
                      timeout_s: float = 20.0) -> dict:
    """Battery state. wire-pod bounds this call at 15 s on its side.

    Raises WirePodError when wire-pod answers but the robot does not, and
    aiohttp.ClientError or TimeoutError when wire-pod itself is not reachable.
    """
    url = build_url(base_url, "/api-sdk/get_battery", serial)
    tmo = aiohttp.ClientTimeout(total=timeout_s)
    async with session.get(url, timeout=tmo) as resp:
        text = await resp.text()
    if text.startswith("error"):
        raise WirePodError(text[:200])
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise WirePodError(f"get_battery: not JSON: {text[:80]!r}") from exc
    if not isinstance(data, dict):
        raise WirePodError("get_battery: not an object")
    return data


class FrameParser:
    """Cuts JPEG frames out of an MJPEG byte stream by SOI/EOI markers.

    wire-pod's multipart boundary does not match its own header, so the
    boundary is ignored and only the JPEG markers are trusted.
    """

    def __init__(self, max_frame_bytes: int) -> None:
        assert max_frame_bytes > MIN_FRAME_BYTES
        self._buf = bytearray()
        self._max = max_frame_bytes
        self.dropped = 0

    def feed(self, chunk: bytes) -> list[bytes]:
        self._buf += chunk
        frames: list[bytes] = []
        for _ in range(MAX_FRAMES_PER_CHUNK):
            start = self._buf.find(SOI)
            if start < 0:
                del self._buf[:-1]
                break
            if start > 0:
                del self._buf[:start]
            end = self._buf.find(EOI, 2)
            if end < 0:
                if len(self._buf) > self._max:
                    self._discard_oversized()
                break
            frame = bytes(self._buf[:end + 2])
            del self._buf[:end + 2]
            if len(frame) >= MIN_FRAME_BYTES:
                frames.append(frame)
            else:
                self.dropped += 1
        return frames

    def _discard_oversized(self) -> None:
        nxt = self._buf.find(SOI, 2)
        del self._buf[:nxt if nxt > 0 else len(self._buf) - 1]
        self.dropped += 1


@dataclass(frozen=True)
class HubConfig:
    base_url: str
    serial: str
    header_timeout_s: float = 6.0
    stall_timeout_s: float = 5.0
    backoff_min_s: float = 60.0
    backoff_max_s: float = 1800.0
    max_frame_bytes: int = 2_000_000
    chunk_bytes: int = 16384
    max_chunks_per_session: int = 5_000_000


class Lease:
    """One viewer's claim on the shared upstream. Release exactly once."""

    def __init__(self, hub: CameraHub) -> None:
        self._hub = hub
        self._held = True

    def release(self) -> None:
        if self._held:
            self._held = False
            self._hub._release()


class CameraHub:
    """Shares one wire-pod camera connection among any number of viewers."""

    def __init__(self, session: aiohttp.ClientSession, cfg: HubConfig,
                 clock: Callable[[], float] = time.monotonic) -> None:
        build_url(cfg.base_url, "/cam-stream", cfg.serial)
        assert 0 < cfg.backoff_min_s <= cfg.backoff_max_s
        self.cfg = cfg
        self._session = session
        self._clock = clock
        self._viewers = 0
        self._allowed = True
        self._reachable = True
        self._task: asyncio.Task | None = None
        self._frame: bytes | None = None
        self._frame_at: float | None = None
        self._seq = 0
        self._fails = 0
        self._retry_at = 0.0
        self._state = STATE_IDLE
        self._detail = ""
        self.sessions_opened = 0
        self._changed = asyncio.Event()
        self._listeners: list[Callable[[], None]] = []

    # ---- read-only view -------------------------------------------------
    @property
    def state(self) -> str:
        return self._state

    @property
    def detail(self) -> str:
        return self._detail

    @property
    def allowed(self) -> bool:
        return self._allowed

    @property
    def viewers(self) -> int:
        return self._viewers

    @property
    def seq(self) -> int:
        return self._seq

    @property
    def frame(self) -> bytes | None:
        return self._frame

    def frame_age_s(self) -> float | None:
        if self._frame_at is None:
            return None
        return max(0.0, self._clock() - self._frame_at)

    def retry_in_s(self) -> float:
        return max(0.0, self._retry_at - self._clock())

    def can_start(self) -> bool:
        return (self._allowed and self._reachable
                and self._clock() >= self._retry_at)

    # ---- control --------------------------------------------------------
    def add_listener(self, callback: Callable[[], None]) -> Callable[[], None]:
        """Call callback on every state change (not every frame). Returns a remover."""
        assert len(self._listeners) < MAX_LISTENERS, "too many listeners"
        self._listeners.append(callback)

        def remove() -> None:
            if callback in self._listeners:
                self._listeners.remove(callback)
        return remove

    def acquire(self) -> Lease:
        self._viewers += 1
        self._maybe_start()
        return Lease(self)

    def _release(self) -> None:
        assert self._viewers > 0, "release without acquire"
        self._viewers -= 1
        if self._viewers == 0:
            self._stop()

    def set_allowed(self, allowed: bool) -> None:
        self._allowed = bool(allowed)
        if not self._allowed:
            self._stop()
            self._set_state(STATE_WITHHELD, "camera withheld by switch")
        else:
            self._set_state(STATE_IDLE, "")
            self._maybe_start()

    def set_robot_reachable(self, reachable: bool, detail: str = "") -> None:
        self._reachable = bool(reachable)
        if not self._reachable:
            self._stop()
            self._set_state(STATE_ROBOT_UNREACHABLE, detail or "robot not answering")
        elif self._state == STATE_ROBOT_UNREACHABLE:
            self._set_state(STATE_IDLE, "")
            self._maybe_start()

    async def close(self) -> None:
        task = self._task
        self._stop()
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)

    # ---- frames ---------------------------------------------------------
    async def next_frame(self, after_seq: int, timeout_s: float) -> tuple[int, bytes] | None:
        """Wait for a frame newer than after_seq. None when none is coming."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_s
        for _ in range(MAX_WAKEUPS):
            if self._seq > after_seq and self._frame is not None:
                return self._seq, self._frame
            if self._task is None:
                return None
            remaining = deadline - loop.time()
            if remaining <= 0:
                return None
            event = self._changed
            try:
                async with asyncio.timeout(remaining):
                    await event.wait()
            except TimeoutError:
                return None
        return None

    async def still(self, max_age_s: float, refresh: bool, wait_s: float) -> bytes | None:
        """A frame for a thumbnail. Opens the upstream only when the cached
        frame is older than max_age_s, refresh is set, and a start is due."""
        age = self.frame_age_s()
        if self._frame is not None and age is not None and age <= max_age_s:
            return self._frame
        if refresh and self.can_start():
            lease = self.acquire()
            try:
                await self.next_frame(self._seq, wait_s)
            finally:
                lease.release()
        return self._frame

    # ---- internals ------------------------------------------------------
    def _signal(self) -> None:
        event = self._changed
        self._changed = asyncio.Event()
        event.set()

    def _notify(self) -> None:
        for callback in list(self._listeners)[:MAX_LISTENERS]:
            callback()

    def _set_state(self, state: str, detail: str) -> None:
        self._state = state
        self._detail = detail
        self._signal()
        self._notify()

    def _maybe_start(self) -> None:
        if self._viewers > 0 and self._task is None and self.can_start():
            self.sessions_opened += 1
            self._task = asyncio.get_running_loop().create_task(self._run())

    def _stop(self) -> None:
        task, self._task = self._task, None
        if task is not None and not task.done():
            task.cancel()
        self._signal()

    def _publish(self, frame: bytes) -> None:
        self._frame = frame
        self._frame_at = self._clock()
        self._seq += 1
        self._fails = 0
        self._retry_at = 0.0
        if self._state != STATE_STREAMING:
            self._set_state(STATE_STREAMING, "")
            return
        self._signal()

    def _record_failure(self, detail: str) -> None:
        self._fails += 1
        delay = min(self.cfg.backoff_max_s,
                    self.cfg.backoff_min_s * (2 ** min(self._fails - 1, 20)))
        self._retry_at = self._clock() + delay
        self._set_state(STATE_NO_FRAMES, f"{detail}; next try in {int(delay)} s")

    async def _run(self) -> None:
        me = asyncio.current_task()
        seq_before = self._seq
        failure = ""
        self._set_state(STATE_STARTING, "")
        try:
            await self._session_frames()
            failure = "stream ended before any frame"
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            failure = (f"no frames within {self.cfg.header_timeout_s:g} s "
                       "(robot asleep, or camera busy)")
        except (aiohttp.ClientError, WirePodError) as exc:
            failure = f"wire-pod: {exc}"[:200]
        finally:
            if self._task is me:
                self._task = None
                self._finish(self._seq - seq_before, failure)

    def _finish(self, got: int, failure: str) -> None:
        if got == 0:
            self._record_failure(failure)
            return
        self._fails = 0
        self._retry_at = 0.0
        self._set_state(STATE_IDLE, "")
        self._maybe_start()

    async def _session_frames(self) -> int:
        """Open /cam-stream, publish frames until EOF, stall or cancel.

        A stall raises TimeoutError; the caller counts frames by sequence
        number, so a stall after frames is not mistaken for a failed start."""
        cfg = self.cfg
        url = build_url(cfg.base_url, "/cam-stream", cfg.serial)
        # sock_read would also cut the wait for headers short; the explicit
        # deadlines below govern the start and each read instead.
        tmo = aiohttp.ClientTimeout(total=None, sock_connect=cfg.header_timeout_s)
        got = 0
        async with asyncio.timeout(cfg.header_timeout_s):
            resp = await self._session.get(url, timeout=tmo)
        try:
            await self._check_start(resp)
            parser = FrameParser(cfg.max_frame_bytes)
            for _ in range(cfg.max_chunks_per_session):
                limit = cfg.header_timeout_s if got == 0 else cfg.stall_timeout_s
                async with asyncio.timeout(limit):
                    chunk = await resp.content.read(cfg.chunk_bytes)
                if not chunk:
                    break
                for frame in parser.feed(chunk):
                    got += 1
                    self._publish(frame)
        finally:
            resp.close()
        return got

    async def _check_start(self, resp: aiohttp.ClientResponse) -> None:
        if resp.status != 200:
            raise WirePodError(f"cam-stream: http {resp.status}")
        ctype = resp.headers.get("Content-Type", "")
        if "multipart" not in ctype.lower():
            body = await resp.content.read(200)
            raise WirePodError("cam-stream: " + body.decode("utf-8", "replace").strip())
