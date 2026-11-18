---
name: ha-vector-camera
description: Home Assistant custom integration (vector_wirepod) that shows a Vector robot's camera through wire-pod without waking, driving or fighting the robot.
---

# ha-vector-camera

Authored-by: CPCS.

A Home Assistant integration for one Vector's camera and passive telemetry, served
through wire-pod. It is written around how Vector and wire-pod actually behave,
measured against live wire-pod and Home Assistant hosts.

## What it does to the robot, and what it never does

| Rule | Why |
|---|---|
| Only `get_sdk_info`, `get_battery` and `/cam-stream` are ever requested; a test fails if any other wire-pod path appears in the source. | No behaviour control, motion, speech, eye colour, wake word or event stream. Nothing here can wake the robot or take it from what it is doing. |
| All HA viewers share one `/cam-stream` connection, opened only while someone watches live and closed when the last viewer leaves (returning camera state to `idle`). | wire-pod serves one viewer per robot and ends the previous one when a new one arrives. |
| A start that yields no frame within 6 s is abandoned, then retried no sooner than 60 s later, doubling to 30 min. Waiting live streams keep a calm 5 s status keepalive instead of disconnecting and auto-recover when frames arrive. | An unpatched wire-pod waits on a robot that is not producing frames with no deadline, and browser reconnect storms otherwise hammer wire-pod. |
| JPEG frames are bounded by walking ISO 10918-1 header segments (`APPn`/`COM`/`SOFn`/`SOS`). | Raw `EOI` byte scans split valid frames early whenever `APPn` or `COM` metadata contains `0xFF 0xD9`. |
| Thumbnails come from the last frame. A fresh one is fetched at most every `still_refresh_s` (default 300, 0 = never), and only when a start is due. | Fewer feed opens and closes on the robot. |
| It is meant to stay on during experiments and other processes: it only consumes frames and never commands the robot. | Operator requirement, 2026-11-14. |
| Switch `Camera allowed` withholds the camera and closes a live stream. Its state survives restarts and is not overwritten by robot reachability polls. | For the rare case where even passive viewing must stop. |
| When there is no fresh frame, the camera shows a status card (state, reason, age of the last frame, battery, charger) instead of a stale picture. | A stale frame or wire-pod's "NO LIVE VIDEO" image otherwise passes for live video. |

## Install

### HACS (custom repository)

1. In Home Assistant, open **HACS** -> menu -> **Custom repositories**.
2. Add `https://github.com/P1R4N351/ha-vector-camera` with category **Integration**.
3. Install **Vector (wire-pod) camera**, restart Home Assistant, then go to **Settings -> Devices & services -> Add integration**.

### Manual / SSH script

```bash
bash deploy.sh --host homeassistant --restart
```

`deploy.sh` copies `custom_components/vector_wirepod` into the HA config on the
host (keeping any previous copy as `vector_wirepod.bak-<UTC>`), runs
`ha core check`, and with `--restart` restarts HA core, which is needed to
load new code. Setup reads only wire-pod's robot list, so a sleeping robot can
be added. Use **Reconfigure** on the integration entry to change the wire-pod
URL without losing entity history.

## Entities

- `camera.<name>`: attributes `camera_state` (`idle`, `starting`, `streaming`,
  `no_frames`, `withheld`, `robot_unreachable`), `camera_detail`,
  `camera_allowed`, `viewers`, `last_frame_age_s`, `next_start_in_s`,
  `dropped_frames`, `robot_reachable`, `on_charger`, `charging`,
  `battery_level`, `battery_volts`.
- `switch.<name>_camera_allowed`: withholds the camera from Home Assistant.
- `sensor.<name>_battery_voltage`: live voltage (`V`) from `get_battery`.
- `sensor.<name>_battery_level`: enum (`unknown`, `low`, `nominal`, `full`).
- `sensor.<name>_camera_status`: diagnostic enum mirroring `camera_state`.
- `binary_sensor.<name>_on_charger`: charger platform presence (normalizing Go `omitempty` false fields).
- `binary_sensor.<name>_charging`: active charging state.
- `binary_sensor.<name>_robot_reachable`: diagnostic connectivity via wire-pod.

Integration diagnostics download (`diagnostics.py`) exports redacted config,
battery coordinator payload, and `CameraHub` counters.

Battery is polled every `battery_poll_s` seconds (default `60`, configurable
in integration options). When wire-pod is unreachable the entities are
unavailable; when wire-pod answers but the robot does not, the camera and
diagnostics report `robot_unreachable`.

## Tests

```bash
bash tests/check.sh                        # metadata + unit/entity tests + mutate.py
bash tests/check.sh --quick                # metadata + unit/entity tests only
python3 -m unittest discover -s tests -v   # needs aiohttp, Pillow
python3 tests/mutate.py
```

- `tests/test_wirepod.py` runs `wirepod.py` and `placeholder.py` against a local
  fake wire-pod that streams, injects APPn/COM `0xFF 0xD9` metadata, hangs,
  stalls, ends early, returns HTTP 502/504 errors, or omits false `get_battery`
  fields.
- `tests/test_ha_entities.py` exercises `__init__.py`, `camera.py`, `switch.py`,
  `sensor.py`, `binary_sensor.py`, `diagnostics.py`, `config_flow.py`
  (including `reconfigure` and options), and integration metadata offline
  against `FakeWirePod` using a stubbed `homeassistant` harness.
- `tests/mutate.py` breaks one guard at a time in a scratch copy and checks
  that the test suite catches every injected defect.
- Live Home Assistant runtime UI verification requires an active HA instance
  and an awake Vector robot connected to wire-pod.

## Why passivity depends on wire-pod

The robot's gateway turns image streaming on when a `CameraFeed` subscriber
connects and off, for the whole robot, when any subscriber leaves
(`vector-cloud` `gateway/message_handler.go`, `CameraFeed`). A wire-pod that
opens a feed per viewer therefore silences every other camera user,
including an experiment's SDK feed, each time an HA viewer closes.
`kercre123/wire-pod#549` proposed a shared-feed proxy in wire-pod; if a
single-viewer `handleCamStream` with idle timeout and streaming-ownership
tracking is used instead, keep `Camera allowed` off during active navigation
experiments (`still_refresh_s: 0`) so HA does not compete for the stream or
re-open/close streaming mid-run.
