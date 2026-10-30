# VLM PPE Judge: Reference

**Contents**

- [1. How it works](#1-how-it-works)
- [2. MQTT topics](#2-mqtt-topics)
- [2a. The WEDA command `judge.now`](#2a-the-weda-command-judgenow)
- [3. Data streams reported to WEDA (17)](#3-data-streams-reported-to-weda-17)
- [4. Web view](#4-web-view)
- [5. Settings](#5-settings)
- [6. Limitations](#6-limitations)
- [7. Security](#7-security)
- [8. Performance](#8-performance)
- [9. WEDA integration](#9-weda-integration)

This page describes how the judge works, its MQTT messages, the data streams reported to WEDA, the settings, the limits and the measured performance. The optional HTTP API is described in the [Optional HTTP API](api.md) page; deployment and troubleshooting in the [Deployment and Operations Guide](sop-run-on-edge-device.md); building in the [Build and Release Guide](sop-build-push-image.md).

## 1. How it works

1. **Frames.** The judge reads the camera and keeps only the newest frame.
2. **Model call.** One frame and the prompt (`ppe-v3` by default, or your own `PROMPT_TEXT`) go to the model server. It answers with a short JSON text (people, helmet, vest). One request is in flight at a time.
3. **Check and verdict.** The judge validates the answer and does not repair a bad one. It computes the verdict itself: `COMPLIANT`, `VIOLATION` or `NO_PERSON`. A violation is checked on up to three frames, and two must agree before it is published.
4. **MQTT.** Only the small JSON result is published on the loopback broker, never the picture. The topics are under `advantech/<id>/vision-vlm/`: `status` and `meta` (retained, with a Last Will), `judgements`, `events`, `health` and `stats`.
5. **Sub-Node.** It subscribes to these topics and reports telemetry (one report per message, with the message's own timestamp) and events to WEDA Cloud through the WedaNode.

## 2. MQTT topics

| Topic | Retained | Content |
|---|---|---|
| `status` | yes | `online` or `offline` (the Last Will when the judge dies). Never `degraded`: a quiet judge is reported by the `JudgeDegraded` event and by `judge_state` |
| `meta` | yes | model, prompt version, required PPE, camera, timeouts |
| `judgements` | no | one valid judgement: counts, items missing, verdict, scene text, latency |
| `events` | no | `PpeViolationDetected`, `PpeViolationCleared`, `JudgeDegraded`, `JudgeRecovered` |
| `health` | no | camera state and counters, every `HEALTH_INTERVAL_SEC` |
| `stats` | no | counters and latency, every 60 s |
| `commands` | no | **to the judge:** the command `judge_now` (below) |

**The `judge_now` command** is `{"schemaVersion":"1.0","command":"judge_now","commandId":"<1-64 of A-Za-z0-9_->"}`. The Sub-Node sends it for the WEDA command `judge.now`, and the judgement that starts after it carries the same `commandId`. A `judge_now` that arrives while another is waiting replaces it, and the replaced sender gets no answer (the Sub-Node sends one at a time). If the judgement for a command fails, the judge tries again for the same command for up to 20 s.

**Example of a `judgements` message** (a violation checked on two pictures; `checks` is the number of pictures, `compliant` is `null` and `state` is `NO_PERSON` when nobody is in view):

```json
{"schemaVersion":"1.0","timestamp":"2026-10-27T01:49:12.459Z","deviceId":"fa9be6af32ee","publisherId":"fa9be6af32ee",
 "seq":342,"latencyMs":5100,"personCount":2,"state":"VIOLATION","compliant":false,"violations":["helmet","vest"],
 "ppe":{"helmet":{"required":true,"missing":1,"unclear":0},"vest":{"required":true,"missing":1,"unclear":1}},
 "scene":"Two workers at the line; one has no hard hat and no vest.","checks":2,"runId":"478ce075-9474-4090-a807-0b1e1d4f6c11","promptVersion":"ppe-v3"}
```

## 2a. The WEDA command `judge.now`

Send `POST /api/v1/devices/<Sub-Node device id>/commands:invoke` with `deviceCommand` `judge.now` and `parameters` `{}`.

The Sub-Node publishes a `judge_now` message and waits for the judgement that carries its `commandId` (the command timeout minus 2 s, between 1 and 15 s). It returns the verdict in the command response: `state`, `compliant`, `personCount`, `violations`, `scene`, `latencyMs`, `seq` and `judgedAt`.

- One command runs at a time; a second one gets `DeviceBusy`.
- If no judgement arrives in time, the answer is `Timeout` and names the judge state.
- A judgement that was already running when the command arrived does not answer it, because its frame is older.

## 3. Data streams reported to WEDA (17)

| Stream | Notes |
|---|---|
| `ppe_state`, `ppe_compliant` | `ppe_compliant` is not sent for `NO_PERSON` |
| `violation_count`, `helmet_missing`, `vest_missing`, `person_count` | counts from the judgement |
| `judgement_latency_ms`, `judgement_seq`, `scene_summary` | latency, running number and the model's sentence |
| `prompt_version` | sent when it changes, and again after `STALE` or `OFFLINE` |
| `judge_state`, `judge_state_reason` | kept by the Sub-Node (below) |
| `rejected_total`, `timeout_total`, `camera_ok` | counters and camera state |
| `ppe_event` | the newest event as JSON |
| `hb` | heartbeat of the Sub-Node |

`GET /api/sensors` of the judge lists the same streams and where each value comes from.

Only the hard hat and the vest have a stream of their own (`helmet_missing`, `vest_missing`). When gloves or goggles are required, they count in `violation_count`, `ppe_state`, `ppe_compliant` and `ppe_event`, and each has its entry in the `ppe` object of the judgement (MQTT and API).

**`judge_state`** is kept by the Sub-Node:

| Value | Meaning |
|---|---|
| `UNKNOWN` | no judgement yet (reported once at start) |
| `CURRENT` | judgements are arriving |
| `STALE` | no judgement for `StaleAfterSec` (default 15 s) |
| `OFFLINE` | the judge announced `offline` or died |

When the state becomes `STALE` or `OFFLINE`, the Sub-Node also clears its cached judgement values, so a cached read never returns a verdict from before the judge went quiet. The cloud keeps its latest values as they were, so a consumer must read `judge_state` first.

A judgement without a valid `state`, `personCount` (0 to 50) or `timestamp`, and a `health` message without `camera` (`OK` or `LOST`), are dropped and logged.

**`judge_state_reason`** says why: `NONE`, `NO_JUDGEMENT`, `CAMERA_LOST`, `MODEL_TIMEOUTS`, `ANSWERS_REJECTED`, `BROKER_LOST` or `PUBLISHER_OFFLINE`.

## 4. Web view

The judge serves the live camera picture on `http://<board>:8080/` with the model's description at the bottom and a border that shows the state: red for a violation, green for compliant, grey for nobody in view, amber when the newest judgement is older than 15 s (the cloud calls it `STALE` at the same time). The state, the number of people, the judgement number and its latency are in the judge's log, and the "demo only" label is logged when the judge starts. `VIEW_HUD=true` also draws them, with the age of the result, at the top left of the picture.

It uses the overlay and web server of the VLM container (`/gemma/inference.py` in the base image), and the judge owns the camera. The pages are `/` and `/stream.mjpg` (the exact path, no query string). The server answers `GET` only; `HEAD` gets 501.

The page has no login and shows the live picture. Use it on a trusted network, or keep `HTTP_BIND=127.0.0.1` and open it through an SSH tunnel: `ssh -L 8080:127.0.0.1:8080 <board>`.

## 5. Settings

All settings are environment variables. An invalid value stops the start with an error that names the variable. A blank value is the same as not set.

**Settings of the stack.** These are the ones to change in the WEDA portal (stack environment) or in a `.env` file.

| Variable | Default | Meaning |
|---|---|---|
| `PUBLISHER_ID` (or `DEVICE_ID`) | required | 12 lowercase hex characters, the node id. On WEDA the agent supplies it (`EDGESYNC_EDGE_ID`) |
| `JUDGE_SOURCE` | `/dev/video0` | camera node, or a video file (looped). The demo video is built into the image: `/app/clips/OD_Jar.mp4`. Inside the container only `/dev/videoN` exists; the by-id links are not visible |
| `HTTP_BIND` | `127.0.0.1` | address of the web view (port 8080). The code and the stack default to loopback; the deployment guide shows how to set `HTTP_BIND=0.0.0.0` to reach the live view directly |
| `REQUIRED_PPE` | `helmet,vest` | the items judged, any of `helmet`, `vest`, `gloves`, `goggles` (each at most once). Gloves and goggles need `PROMPT_VERSION=ppe-v5` or your own `PROMPT_TEXT` |
| `PROMPT_VERSION` | `ppe-v3` | the built-in prompt. `ppe-v3` ends with one sentence that stops the model from inventing a person in an empty scene. `ppe-v4` makes the model say whether each head is visible and ignores people whose head is not: a forearm is no longer counted as a person, but fewer real violations are found, so it is not the default. `ppe-v5` builds the question from `REQUIRED_PPE`, so it is the prompt for gloves and goggles; for helmet and vest it asks the same as `ppe-v3` |
| `PROMPT_TEXT` | empty | your own question instead of a built-in prompt (see below); at most 4000 characters, reported as prompt version `custom` |
| `WEB_VIEW` | `true` | the web view |
| `VIEW_HUD` | `false` | `true` draws the state, the number of people, the judgement number, its latency and age, and the "demo only" label at the top left of the picture. All of them except the age are in the log either way |
| `VIEW_DESCRIPTION` | `true` | the model's one-sentence description at the bottom of the picture; `false` hides it |
| `LOG_JUDGEMENTS` | `true` | one line per judgement in the judge's log (`docker logs`): number, state, people, time and description; `false` silences them |
| `API_PORT` | `0` (off) | port of the optional read-only HTTP API ([Optional HTTP API](api.md)), on the same address as the web page (`HTTP_BIND`); the code and the stack both leave it off (set `8081` to turn it on) |
| `LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING` or `ERROR` |
| `SUBNODE_NAME` | `VlmJudge-<board id>` | the Sub-Node's name in WEDA for a new registration (read by the Sub-Node, stack only) |
| `WEDA_DATA_PATH` | `/opt/Advantech/vlm-judge-subnode/weda-data-sit` | the board folder that keeps the Sub-Node's registration, so a redeploy is the same WEDA device. A different folder makes a new registration (read by the stack, stack only) |
| `SystemConfig__WedaNode__Password` | the one in the Sub-Node image | only for a board whose WedaNode uses another password (read by the Sub-Node, stack only) |

**Tuning and code-only settings.** The stack does not pass these on: they keep the defaults below. Change them only with `docker run` or your own compose file; `PUBLISHER_ID` is the same as `DEVICE_ID`, and `JUDGE_INTERVAL_SEC` is the older name of `JUDGE_MIN_GAP_SEC`.

| Variable | Default | Meaning |
|---|---|---|
| `JUDGE_MODE` | `off` (the stack fixes it at `ppe`) | `ppe` runs the judge. `off` stops it at once with exit code 1, so a container with a restart policy keeps restarting: stop the stack instead |
| `JUDGE_MIN_GAP_SEC` | `0.5` | idle gap between judgements, 0 to 600 (`JUDGE_INTERVAL_SEC` is accepted as an older name) |
| `JUDGE_TIMEOUT_SEC` | `15` | model request timeout; 1 to 600 |
| `WATCHDOG_SEC` | `60` | no valid judgement for this long: `JudgeDegraded`; 10 to 3600, and longer than `JUDGE_TIMEOUT_SEC` |
| `CONFIRM_FRAMES` / `CONFIRM_MIN` | `3` / `2` | frames a violation is checked on / how many must agree; whole numbers 1 to 9, `CONFIRM_MIN` not above `CONFIRM_FRAMES` |
| `HEALTH_INTERVAL_SEC` | `10` | `health` cadence (5 to 60) |
| `CAMERA_LOST_AFTER_SEC` | `5` | no frame for this long: camera `LOST`; 1 to 60 |
| `EXIT_AFTER_SEC` | `600` | the judge exits, and is restarted, after the model server has not answered a request (error or timeout) for this long; 30 to 86400 |
| `HTTP_PORT` | `8080` | port of the web view |
| `MODEL_SERVER_URL` | `http://127.0.0.1:18181` | loopback only |
| `MQTT_HOST` / `MQTT_PORT` | `127.0.0.1` / `1883` | loopback only |
| `MQTT_TOPIC_BASE` | `advantech/<id>/vision-vlm` | topic base |
| `JUDGE_MODEL` | the first model the server lists | the model id to ask for |
| `VLM_APP_PATH` | `/gemma/inference.py` | the base image's app file whose overlay and web server the web view reuses |


**Your own prompt.** The stack passes `PROMPT_TEXT` through; with compose, put it in `.env` in the `docker` folder. The model must still answer one line of JSON with the same words `yes`, `no` and `unclear`; any other answer is rejected and counted:

```json
{"people":[{"helmet":"yes","vest":"no"}],"scene":"one short sentence"}
```

## 6. Limitations

- A judgement takes about 2 s, about 4 s when a violation is re-checked, and about 30 s when the model did not come from the image. The model server handles one request at a time.
- Gloves and goggles are small items: whether the small model judges them reliably has not been measured. Try your own scene before relying on them. An item whose body part is hidden (hands for gloves, eyes for goggles) should come back `unclear`, which is never a violation.
- The small model can be wrong, for example by reporting people in an empty scene, and its answers are not repeatable even at temperature 0. This is a best-effort scene-understanding demo, not a safety system.
- The model server sometimes hangs without an error. The stack's supervisor restarts it after three failed probes, and also when it keeps answering with a server error (HTTP 5xx).
- Only the verdict and a text description go to WEDA Cloud; pictures are never sent there. With the optional API on, they stay in the board's memory (the last 20 judged pictures and the newest camera picture) and the HTTP API serves them to anyone who can reach it, so keep the API on a trusted network.

## 7. Security

This is a demo configuration.

| Area | State | For production |
|---|---|---|
| MQTT | anonymous, plaintext, loopback only | credentials and TLS if the broker is ever reachable from the network |
| Web view and API | no login; the picture is live; both listen on loopback by default and are opened to the network only by setting `HTTP_BIND=0.0.0.0` | a login, or `HTTP_BIND=127.0.0.1` with a tunnel |
| `geniex-serve`, `vlm-judge` | `privileged: true` and host network (the judge only for the camera, because WEDA Node v1.2 ignores `devices:`) | explicit devices and capabilities |
| Sub-Node credential | the WedaNode's default NATS password is built into the Sub-Node image. It protects only the board's loopback NATS | keep the image registry private; set a different password in the stack variable `SystemConfig__WedaNode__Password` |
| Images | named by version in the stack (a version is never pushed twice). The model server and the broker are public images. The model server and the broker listen on the board's loopback only, and the judge makes no TLS connections | scan every image before release ([build guide](sop-build-push-image.md), section 5) |

## 8. Performance

Measured on the AIR-055 with the camera and WEDA Cloud. The numbers help to plan an integration; they are not a guarantee.

**Latency from the camera to the cloud**

| Leg | Median | 95% within |
|---|---|---|
| Camera frame to the judge's message (one model request) | 1.85 s | 2.1 s |
| That message to the Sub-Node | under 0.01 s | 0.01 s |
| Sub-Node to WedaNode to the value in WEDA Cloud | about 2.5 s | 4.4 s |
| **Camera frame to the cloud value** | **4.5 s** | **6.0 s** |

The model accounts for about 40% of the time, and most of the rest is the link through the WedaNode to the cloud. A violation is checked again on further pictures, so the cloud shows it about 2 s later than the table says.

**Speed of one judgement**

| What the camera sees | Median | 95% within |
|---|---|---|
| Nobody | 1.2 s | 5.1 s |
| People, nothing missing | 2.1 s | 5.3 s |
| A violation (re-checked on 2 or 3 pictures) | 4.0 s | 6.1 s |

About 17 judgements a minute. Every JSON call of the HTTP API answers in a few milliseconds.

**Delivery and recovery**

| What | Result |
|---|---|
| Frozen model server | cloud state `STALE` after 18 s, `CURRENT` again after 47 s |
| Docker restart / board reboot | back to normal in about 20 s / about 1 minute after boot |
| Cloud unreachable for 10 minutes | the demo kept working; 159 of about 164 values from the outage recovered with their original times |
| Eight hours unattended | 6,390 judgements, no container restarts, no growth in latency |

## 9. WEDA integration

| Part | Role |
|---|---|
| WedaNode (`dmagent`, `system-agent`) | The board's agent: its identity and link to WEDA. It pulls and starts the stack, and carries the Sub-Node's data over its local NATS server (`127.0.0.1:4224`) |
| Sub-Node (`vlm-judge-subnode`) | Turns the judge's MQTT messages into WEDA data streams, keeps `judge_state`, and takes the command `judge.now` |
| WEDA Cloud | Deploys the stack, stores and shows the data streams, sends commands, opens tunnels |

**The Sub-Node.** Its messages, command and streams are described in sections 2, 2a and 3.
- **Source and image.** The source is the example `examples/vlm-judge-subnode` of the WEDA Sub-Node SDK repository (branch `feature/vlm-judge-subnode`). The stack pulls the image `vlm-judge-subnode`; how it is built is in the [build guide](sop-build-push-image.md#4-build).
- **Registration.** On the first start it registers the judge as a WEDA device named `VlmJudge-<board id>` (`SUBNODE_NAME` changes the name). The registration folder `/opt/Advantech/vlm-judge-subnode/weda-data-sit` keeps it the same device across redeploys.
- **After an outage,** the command `commands:resync` fills the gap from the Sub-Node's local recording ([deployment guide](sop-run-on-edge-device.md#5-operate)).
- **Not reported.** Gloves and goggles have no stream of their own; they count in `violation_count`, `ppe_state` and the event.

**Tunnel management.** WEDA opens an outbound connection from the board's agent to a service on the board, on request through the WEDA API. For this demo the service is the live view on port 8080. Neither the judge nor the Sub-Node contains tunnel code, and the stack needs no change for it. The calls and the limits are in the [deployment guide](sop-run-on-edge-device.md#34-open-the-live-view-through-a-weda-tunnel); WEDA's own pages are [Tunnel Management](https://learn.advantech.com/weda/docs/Function_Explaination/Tunnel_Management) and [Create tunnel session](https://learn.advantech.com/weda/api/create-tunnel-session).
