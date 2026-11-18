"""Break wirepod.py one guard at a time and check the suite notices.

Authored-by: CPCS. Works on a scratch copy of the skill; the source is never
touched. Each arm's needle must occur exactly once and change the file, or the
arm is reported SETUP-ERROR, never as a verdict. A suite that does not finish
in time is reported TIMEOUT, separately from CAUGHT.

Run: python3 tests/mutate.py   (needs aiohttp, Pillow)
"""

from __future__ import annotations

import pathlib
import shutil
import subprocess
import sys
import tempfile

SKILL = pathlib.Path(__file__).resolve().parents[1]
TARGET = pathlib.Path("custom_components/vector_wirepod/wirepod.py")
SUITE_TIMEOUT_S = 120

ARMS: list[tuple[str, list[tuple[str, str]]]] = [
    ("any wire-pod path can be built",
     [('    if path not in ALLOWED_PATHS:\n', '    if False:\n')]),
    ("serial not checked", [("        if not serial.isalnum():\n", "        if False:\n")]),
    ("second upstream opened for a second viewer",
     [("if self._viewers > 0 and self._task is None and self.can_start():",
       "if self._viewers > 0 and self.can_start():")]),
    ("upstream kept open after last viewer leaves",
     [("        if self._viewers == 0:\n            self._stop()\n",
       "        if self._viewers == 0:\n            pass\n")]),
    ("last viewer release leaves state streaming",
     [("        if self._viewers == 0:\n            self._stop()\n"
       "            if self._state in (STATE_STARTING, STATE_STREAMING):",
       "        if self._viewers == 0:\n            self._stop()\n            if False:")]),
    ("no deadline on the start",
     [("        async with asyncio.timeout(cfg.header_timeout_s):\n            resp =",
       "        async with asyncio.timeout(None):\n            resp ="),
      ("total=None, sock_connect=cfg.header_timeout_s)", "total=None, sock_connect=None)"),
      ("limit = cfg.header_timeout_s if got == 0 else cfg.stall_timeout_s",
       "limit = None")]),
    ("no backoff after a failed start",
     [("        self._retry_at = self._clock() + delay\n", "        self._retry_at = self._clock()\n")]),
    ("backoff does not grow",
     [("self.cfg.backoff_min_s * (2 ** min(self._fails - 1, 20))", "self.cfg.backoff_min_s")]),
    ("backoff not capped", [("delay = min(self.cfg.backoff_max_s,", "delay = min(1e9,")]),
    ("a delivered frame does not reset backoff",
     [("        self._seq += 1\n        self._fails = 0\n", "        self._seq += 1\n")]),
    ("withholding does not close the stream",
     [("        if not self._allowed:\n            self._stop()\n",
       "        if not self._allowed:\n            pass\n")]),
    ("withheld camera still starts",
     [("return (self._allowed and self._reachable", "return (True and self._reachable")]),
    ("unreachable robot still asked",
     [("return (self._allowed and self._reachable", "return (self._allowed and True")]),
    ("unreachable recovery clears withheld state",
     [("            if self._allowed:\n                self._set_state(STATE_ROBOT_UNREACHABLE",
       "            if True:\n                self._set_state(STATE_ROBOT_UNREACHABLE")]),
    ("still ignores its cache",
     [("if self._frame is not None and age is not None and age <= max_age_s:", "if False:")]),
    ("still refreshes when told not to",
     [("        if refresh and self.can_start():\n", "        if self.can_start():\n")]),
    ("stall after frames counted as a failed start",
     [("self._finish(self._seq - seq_before, failure)", "self._finish(0, failure)")]),
    ("wire-pod error text read as a stream",
     [('        if "multipart" not in ctype.lower():\n', "        if False:\n")]),
    ("cam-stream non-200 drops error body",
     [('            msg = f"cam-stream: http {resp.status}: {body}" if body else f"cam-stream: http {resp.status}"\n',
       '            msg = f"cam-stream: http {resp.status}"\n')]),
    ("get_battery ignores HTTP status",
     [("        if resp.status != 200:\n            detail = text.strip()[:120]",
       "        if False:\n            detail = text.strip()[:120]")]),
    ("get_battery drops false on_charger default",
     [('    out["is_on_charger_platform"] = bool(data.get("is_on_charger_platform", False))\n',
       '    out["is_on_charger_platform"] = data.get("is_on_charger_platform")\n')]),
    ("JPEG header segment EOI truncates frame",
     [("            end = _find_jpeg_end(self._buf)\n", "            end = _scan_eoi(self._buf, 2)\n")]),
    ("tiny marker pair accepted as a frame",
     [("            if len(frame) >= MIN_FRAME_BYTES:\n", "            if True:\n")]),
    ("oversized frame never discarded",
     [("                    self._discard_oversized()\n", "                    pass\n")]),
    ("header wait cut short by the stall limit",
     [("tmo = aiohttp.ClientTimeout(total=None, sock_connect=cfg.header_timeout_s)",
       "tmo = aiohttp.ClientTimeout(total=None, sock_connect=cfg.header_timeout_s,\n"
       "                                    sock_read=cfg.stall_timeout_s)")]),
    ("listeners not told of state changes",
     [("        self._signal()\n        self._notify()\n", "        self._signal()\n")]),
    ("lease can be released twice",
     [("        if self._held:\n", "        if True:\n")]),
]
NEUTRAL = ("comment only (must survive)",
           [('"""Passive access to one Vector', '"""Passive  access to one Vector')])


def run_suite(root: pathlib.Path) -> str:
    assert root.is_dir()
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "unittest", "discover", "-s", str(root / "tests")],
            cwd=root, capture_output=True, text=True, timeout=SUITE_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        return "TIMEOUT"
    return "PASS" if proc.returncode == 0 else "FAIL"


def apply(path: pathlib.Path, pairs: list[tuple[str, str]]) -> str:
    assert path.is_file() and len(pairs) >= 1
    text = path.read_text()
    for old, new in pairs:
        if old == new:
            return "old == new"
        if text.count(old) != 1:
            return f"needle found {text.count(old)} times: {old[:50]!r}"
        text = text.replace(old, new)
    if text == path.read_text():
        return "file unchanged"
    path.write_text(text)
    return ""


def arm(name: str, pairs: list[tuple[str, str]], pristine: str, root: pathlib.Path) -> str:
    target = root / TARGET
    target.write_text(pristine)
    err = apply(target, pairs)
    if err:
        return f"SETUP-ERROR  {name}: {err}"
    result = run_suite(root)
    target.write_text(pristine)
    verdict = {"FAIL": "CAUGHT", "TIMEOUT": "TIMEOUT", "PASS": "SURVIVED"}[result]
    return f"{verdict:<12} {name}"


def main() -> int:
    assert SKILL.is_dir() and (SKILL / TARGET).is_file()
    with tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(tmp) / "skill"
        shutil.copytree(SKILL, root, ignore=shutil.ignore_patterns("__pycache__", ".git"))
        pristine = (root / TARGET).read_text()
        if run_suite(root) != "PASS":
            print("SETUP-ERROR  control: suite does not pass on the pristine copy")
            return 3
        lines = [arm(n, p, pristine, root) for n, p in ARMS]
        neutral = arm(NEUTRAL[0], NEUTRAL[1], pristine, root)
        restored = run_suite(root)
    for line in lines:
        print(line)
    print(neutral)
    caught = sum(line.startswith("CAUGHT") for line in lines)
    print(f"\n{caught} caught of {len(ARMS)}; neutral "
          f"{'survived' if neutral.startswith('SURVIVED') else 'DID NOT SURVIVE'}; "
          f"restored copy {restored}")
    ok = caught == len(ARMS) and neutral.startswith("SURVIVED") and restored == "PASS"
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
