# Optional HTTP API (with Swagger)

**Contents**

- [What the API covers](#what-the-api-covers)
- [The calls](#the-calls)
- [Before you integrate](#before-you-integrate)
- [Behaviour a client should know](#behaviour-a-client-should-know)
- [Integrating: a result every few seconds, with its picture](#integrating-a-result-every-few-seconds-with-its-picture)

This API is off by default, in the code and in the WEDA stack. Turn it on with the stack variable `API_PORT` = `8081` (it then listens on the same address setting as the live view). It is meant for other programs; the demo itself is the live view.

The judge serves its results over a small HTTP API. Use it to read verdicts, pictures, settings and the data streams from another program. The Swagger page on the same address (`/api/docs`) describes every call and lets you try it in a browser. This page covers what Swagger cannot show: the order of the calls, a working client, the rules for trusting a result, and the cloud side.

## What the API covers

| Where | What | How to read it |
|---|---|---|
| The judge on the board | Everything the judge knows: the newest judgement and the last ones, events, health, counters, settings, the prompt, the camera picture, the 17 data streams | this API, port 8081, Swagger at `/api/docs` |
| The local message bus | The same results as MQTT messages, and the command `judge_now` | [reference](vlm-container-reference.md), section 2 |
| WEDA Cloud | The 17 data streams of the device `VlmJudge`, and the command `judge.now` | the WEDA API with a token: `GET /api/v1/orgs/{orgId}/devices/{deviceId}/data-streams/values/latest`; the command: [deployment guide](sop-run-on-edge-device.md#4-verify), step 5 |

## The calls

A small read-only API on `http://<board>:8081/api` (address `HTTP_BIND`, port `API_PORT`). It serves the same facts as the MQTT topics, without an MQTT client.
No login, `GET` only, JSON unless noted: use it on a trusted network, like the web page.

| Request | Answer |
|---|---|
| `GET /api` | the list of endpoints |
| `GET /api/docs` | **Swagger**: this API as a page you can try in the browser (it loads Swagger UI from the internet into your browser; the board needs no internet) |
| `GET /api/openapi.json` | the OpenAPI 3 description (any Swagger viewer or code generator can use it) |
| `GET /api/overview` | **start here**: device, judge state, camera, the newest judgement (with `ageSec`), the prompt (`active`, `custom`, `inUseSince`, `text`), model, required PPE and `ppeMeans` (what each item name means, e.g. `helmet` is a hard hat), stats and the last 3 events in one answer |
| `GET /api/latest` | the newest judgement and `ageSec`; `404` until the first one |
| `GET /api/judgements?limit=20` | the newest judgements first (1 to 200; the last 200 are kept in memory) |
| `GET /api/health` | `judge` (`online`, `degraded`), `camera` (`OK`, `LOST`), `lastJudgementAgeSec`, `promptVersion`, counters |
| `GET /api/events?limit=20` | the newest events first: `PpeViolationDetected`, `PpeViolationCleared`, `JudgeDegraded`, `JudgeRecovered` (1 to 100; the last 100 are kept in memory) |
| `GET /api/stats` | `judgedTotal`, `rejectedTotal`, `timeoutTotal`, `latencyMsAvg`, `latencyMsP95` since the judge started |
| `GET /api/meta` | the retained MQTT `meta` message: model, prompt version, required PPE, camera, timing settings |
| `GET /api/prompts` | the prompt in use (`active`, `custom`, `activeText`), `inUseSince` (when the judge started with it) and the text of every built-in prompt |
| `GET /api/sensors` | the 17 WEDA data streams of the Sub-Node (16 values and a heartbeat) and where each value comes from |
| `GET /api/latest/snapshot.jpg` | **the picture the newest judgement is about**: the exact frame the model judged, a JPEG without a drawing on it. The headers `X-Judgement-Seq` and `X-Judgement-Timestamp` say which judgement it belongs to (`404` until the first judgement) |
| `GET /api/judgements/{seq}/snapshot.jpg` | the picture behind judgement number `seq`; the last 20 judgements keep theirs (`404` for older ones). A judgement that still has its picture carries its address in the field `snapshot` |
| `GET /api/snapshot.jpg` | the newest camera picture, a few seconds younger than the newest judgement (`503` without a picture) |

```bash
curl http://<board>:8081/api/latest
curl "http://<board>:8081/api/judgements?limit=5"
curl http://<board>:8081/api/latest                       # the description (`scene`), the time (`timestamp`) and the address of the picture (`snapshot`)
curl -o now.jpg http://<board>:8081/api/latest/snapshot.jpg   # the picture that description is about
```

A judgement has `timestamp` (when its picture was taken), `seq`, `state` (`COMPLIANT`, `VIOLATION`, `NO_PERSON`), `personCount`, `violations`, `ppe`, `scene` (the
description) and `latencyMs`.

## Before you integrate

- **History is short.** The judge keeps the last 200 judgements and 100 events in memory, and they are lost when it restarts. The cloud keeps only the latest value of each data stream, and only for a few days. If you need a history, record the MQTT topic `judgements` or poll this API and store the results yourself.
- **One judge per board.** The judge and the Sub-Node use the board's host network and ports 8080 and, when the API is on, 8081 (`API_PORT` sets the second), so one stack runs on a board.
- **No login.** The API and the live view have no login, and the live view shows the camera picture. Setting `HTTP_BIND=0.0.0.0` opens both to the whole network, so use a trusted network, or keep the default `127.0.0.1` and reach them through an SSH tunnel.
- **Time.** Timestamps come from the board's clock, and the freshness rule below uses it. Keep the board's time synchronised (NTP).
- **Reading the data in WEDA Cloud.** You need three values: a token, your organisation id, and the Sub-Node's device id (portal: Devices, `VlmJudge`). A token lasts 2 hours, and an expired one answers `401 token is expired`. The server certificate of the SIT environment is self-signed: save it and let `curl` check against it ([deployment guide](sop-run-on-edge-device.md#32-with-the-weda-api)). The general WEDA documentation is at [learn.advantech.com/weda](https://learn.advantech.com/weda/docs/Getting_Started/Deploy_Container_to_Device/).

## Behaviour a client should know

Checked on the board:

- `GET` only. `POST` and the other writing methods answer `405` with a JSON error; `HEAD` and `OPTIONS` answer `501` (an HTML page), so a monitoring tool should use `GET /api/health`.
- There are no CORS headers: a web page served from another address cannot call the API from the browser. Call it from a server, or use the Swagger page itself (same address).
- The paths are exact: `/api/latest/` (with a slash) answers `404`.
- `limit` is clamped, not refused: `0` or a negative number gives 1, a number above the maximum gives the maximum (200 judgements, 100 events); a value that is not a number answers `400`.
- `GET /api/latest` and `GET /api/latest/snapshot.jpg` answer `404` until the first judgement; `GET /api/snapshot.jpg` answers `503` while there is no camera picture. Responses are never cached (`Cache-Control: no-store`).
- The Swagger page is the contract: a test compares every response with its declared schema, so a field you receive is a field the page describes.

## Integrating: a result every few seconds, with its picture

A client that reads the newest result and its picture (it needs only Python 3):

```python
import json, time, urllib.request

BASE = "http://<board>:8081"
last = 0
while True:
    j = json.load(urllib.request.urlopen(f"{BASE}/api/latest"))
    if j["seq"] != last and j["ageSec"] < 15:                 # a new judgement, and a fresh one
        last = j["seq"]
        ppe = j["ppe"]
        print(j["timestamp"], j["state"], "people", j["personCount"],
              "| helmet missing", ppe["helmet"]["missing"], "unclear", ppe["helmet"]["unclear"],
              "| vest missing", ppe["vest"]["missing"], "unclear", ppe["vest"]["unclear"], "|", j["scene"])
        if "snapshot" in j:                                       # the exact picture the description is about
            with open(f"frame_{last}.jpg", "wb") as f:
                f.write(urllib.request.urlopen(BASE + j["snapshot"]).read())
    time.sleep(1)
```

What an integrator needs to know:

- **A new judgement arrives every 2 to 5 s** (`seq` goes up by one each time). Poll every second or two, or listen to the MQTT topic `judgements` instead of polling ([reference](vlm-container-reference.md), section 2); `events` tells only when a violation starts or ends.
- **Trust only a fresh result.** `ageSec` is the seconds since the judgement was published; treat anything of 15 s or more as old (the Sub-Node does the same: `STALE` after 15 s). `/api/health` shows `judge` `online` or `degraded` and the camera state. `404` on `/api/latest` means no judgement yet.
- **A judgement and its picture belong together.** The field `snapshot` of a judgement is the address of the exact frame the model judged, taken at `timestamp`; the `scene` text describes that picture. Pictures are kept in memory for the last 20 judgements only, so fetch the picture soon after the result (a judgement older than 20 results has no picture). `/api/snapshot.jpg` is the newest camera picture instead, a few seconds younger than the newest judgement.
- **Items the model cannot read are `unclear`, not `missing`.** `compliant` is false only when something is `missing`; read `unclear` as "cannot tell" in your own logic. The small model can also report a person in an empty scene, so debounce: act on a violation that lasts more than one judgement.
- **In the cloud** the same facts are data streams of the Sub-Node device (`/api/sensors` names them). Read the latest values with `GET /api/v1/orgs/{org}/devices/{sub-node id}/data-streams/values/latest`. Ask for a fresh judgement with the command `judge.now` ([reference](vlm-container-reference.md#2a-the-weda-command-judgenow)). Do not use the values while `judge_state` is anything other than `CURRENT`.
