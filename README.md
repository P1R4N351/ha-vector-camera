---
name: ha-vector-camera
description: Home Assistant custom integration (vector_wirepod) that shows a Vector robot's camera through wire-pod without waking, driving or fighting the robot.
---

# ha-vector-camera

Authored-by: CPCS.

A Home Assistant integration for one Vector's camera, served through wire-pod.
It is written around how Vector and wire-pod actually behave, measured on
the Home Assistant host and the wire-pod host on 2026-11-14.

## What it does to the robot, and what it never does

| Rule | Why |
|---|---|
| Only `get_sdk_info`, `get_battery` and `/cam-stream` are ever requested; a test fails if any other wire-pod path appears in the source. | No behaviour control, motion, speech, eye colour, wake word or event stream. Nothing here can wake the robot or take it from what it is doing. |
| All HA viewers share one `/cam-stream` connection, opened only while someone watches live and closed when the last viewer leaves. | wire-pod serves one viewer per robot and ends the previous one when a new one arrives. |
| A start that yields no frame within 6 s is abandoned, then retried no sooner than 60 s later, doubling to 30 min. | An unpatched wire-pod waits on a robot that is not producing frames with no deadline, and each abandoned start leaves a blocked request behind until its idle timer fires. |
| Thumbnails come from the last frame. A fresh one is fetched at most every `still_refresh_s` (default 300, 0 = never), and only when a start is due. | Fewer feed opens and closes on the robot. |
| It is meant to stay on during experiments and other processes: it only consumes frames and never commands the robot. | The operator, 2026-11-14. |
| Switch `Camera allowed` withholds the camera and closes a live stream. Its state survives restarts. | For the rare case where even passive viewing must stop. |
| When there is no fresh frame, the camera shows a status card (state, reason, age of the last frame, battery, charger) instead of a stale picture. | A stale frame or wire-pod's "NO LIVE VIDEO" image otherwise passes for live video. |

## Install

```bash
bash skills/ha-vector-camera/deploy.sh --host homeassistant --restart
```

`deploy.sh` copies `custom_components/vector_wirepod` into the HA config on the
host (keeping any previous copy as `vector_wirepod.bak-<UTC>`), runs
`ha core check`, and with `--restart` restarts HA core, which is needed to
load new code. Then add the integration in the UI (Settings, Devices,
Add integration, "Vector (wire-pod) camera"): enter the wire-pod URL and pick
the robot. Setup reads only wire-pod's robot list, so a sleeping robot can be
added.

## Entities

- `camera.<name>`: attributes `camera_state` (`idle`, `starting`, `streaming`,
  `no_frames`, `withheld`, `robot_unreachable`), `camera_detail`, `viewers`,
  `last_frame_age_s`, `next_start_in_s`, `robot_reachable`, `on_charger`,
  `battery_level`, `battery_volts`.
- `switch.<name>_camera_allowed`.

Battery is polled every 60 s. When wire-pod is unreachable the entities are
unavailable; when wire-pod answers but the robot does not, the camera says so.

## Tests

```bash
python3 -m unittest discover -s skills/ha-vector-camera/tests   # needs aiohttp, Pillow
python3 skills/ha-vector-camera/tests/mutate.py
```

`tests/test_wirepod.py` runs the robot-facing policy (`wirepod.py`) against a
fake wire-pod that streams, hangs, stalls, ends early, answers slowly or
returns an error. `mutate.py` breaks one guard at a time in a scratch copy;
a harness fault is reported `SETUP-ERROR`, a suite that does not finish
`TIMEOUT`.

## Why passivity depends on wire-pod

The robot's gateway turns image streaming on when a `CameraFeed` subscriber
connects and off, for the whole robot, when any subscriber leaves
(`vector-cloud` `gateway/message_handler.go`, `CameraFeed`). A wire-pod that
opens a feed per viewer therefore silences every other camera user,
including an experiment's SDK feed, each time an HA viewer closes.
kercre123/wire-pod#549 makes wire-pod share one feed per robot, keep it 60 s
after the last viewer, never turn streaming off, and turn it back on when
someone else's exit silenced it. Until escapepod runs that, HA viewing is
passive in commands but not in this side effect. An experiment can protect
itself by calling `EnableImageStreaming(true)` when its own feed stalls.

## Known limits

- `camera.py`, `switch.py` and `config_flow.py` have no offline tests (they
  need a running HA). They were imported inside HA 2026.11.1 on the Home Assistant host and
  exercised live there: config flow, entity states, status card, switch
  restore across a restart.
- No frame has yet been shown through this integration: wire-pod's
  `/cam-stream` produced none for the test robot on 2026-11-14 (see below).
- Unpatched wire-pod (`camStreamHandler`) blocks on a robot whose camera
  produces nothing and can panic when its idle timer removes the robot.
  Fixed upstream in kercre123/wire-pod#549 (open at the time of writing) and
  on escapepod by Gemini's `handleCamStream`.
