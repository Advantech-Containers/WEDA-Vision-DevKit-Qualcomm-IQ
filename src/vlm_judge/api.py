"""A small read-only HTTP API next to the web page, so another team can use the judge without MQTT: the newest judgement, the recent ones, the health
and the newest camera picture. Nothing here changes the judge, and it never writes. Same address as the web page (HTTP_BIND), its own port (API_PORT)."""
import http.server
import json
import logging
import re
import threading
import time
import urllib.parse

from . import answer, mqtt

log = logging.getLogger("vlm_judge.api")
ENDPOINTS = {
    "/api/overview": "start here: the device, the judge state, the newest judgement, the prompt and since when, the model, counters and the last events in one answer",
    "/api/latest": "the newest judgement (same fields as the MQTT judgements message) and its age in seconds",
    "/api/judgements?limit=20": "the newest judgements, newest first (at most 200 are kept in memory)",
    "/api/health": "judge and camera state, prompt version, counters",
    "/api/events?limit=20": "the newest events (violation detected or cleared, judge degraded or recovered), newest first (at most 100 are kept in memory)",
    "/api/stats": "counters and latency (average, 95th percentile) since the judge started",
    "/api/meta": "model, prompt version, required PPE, camera and timing settings (the retained MQTT meta message)",
    "/api/prompts": "the prompt in use, since when, and every built-in prompt text",
    "/api/sensors": "the data streams the WEDA Sub-Node sends and where each value comes from",
    "/api/latest/snapshot.jpg": "the picture the newest judgement is about (the frame the model saw), as a JPEG; the headers X-Judgement-Seq and X-Judgement-Timestamp say which judgement",
    "/api/judgements/{seq}/snapshot.jpg": "the picture behind judgement number seq (the last 20 are kept in memory), as a JPEG",
    "/api/snapshot.jpg": "the newest camera picture as a JPEG, without the drawing on it (a few seconds younger than the newest judgement)",
    "/api/docs": "this API as a Swagger page (try it in the browser)",
    "/api/openapi.json": "the OpenAPI description of this API",
}

# The WEDA data streams of the Sub-Node (examples/vlm-judge-subnode, edge_subnode), 16 values and a heartbeat: key, where the value comes from, meaning.
SENSORS = [
    ("ppe_state", "judgement.state", "COMPLIANT, VIOLATION or NO_PERSON"),
    ("ppe_compliant", "judgement.compliant", "true, false, or empty when nobody is in view (the portal shows a boolean as a blank cell)"),
    ("person_count", "judgement.personCount", "people in the picture"),
    ("violation_count", "judgement.violations", "how many of the required items are missing for someone"),
    ("helmet_missing", "judgement.ppe.helmet.missing", "people without a hard hat"),
    ("vest_missing", "judgement.ppe.vest.missing", "people without a vest"),
    ("scene_summary", "judgement.scene", "one sentence from the model"),
    ("judgement_latency_ms", "judgement.latencyMs", "time the check took"),
    ("judgement_seq", "judgement.seq", "running number of the judgement"),
    ("prompt_version", "judgement.promptVersion", "the prompt in use"),
    ("ppe_event", "events", "the newest PpeViolationDetected or PpeViolationCleared as JSON"),
    ("camera_ok", "health.camera", "camera state, sent with every health message (every 10 s by default)"),
    ("rejected_total", "health.rejectedTotal", "answers the checker refused"),
    ("timeout_total", "health.timeoutTotal", "model requests that timed out"),
    ("judge_state", "decided by the Sub-Node", "CURRENT; STALE after 15 s without a judgement; OFFLINE when the judge is gone"),
    ("judge_state_reason", "decided by the Sub-Node", "why the state is what it is (NONE, PUBLISHER_OFFLINE, ...)"),
    ("hb", "the SDK", "heartbeat of the Sub-Node; not recorded"),
]

PPE_WORDS = {"helmet": "a hard hat", "vest": "a high-visibility vest", "gloves": "protective gloves", "goggles": "safety goggles or glasses"}      # what the item names in the judgement mean

SWAGGER_UI = "5.17.14"            # loaded by the viewer's browser from jsdelivr: the board itself needs no internet
DOCS_PAGE = f"""<!doctype html><html><head><meta charset="utf-8"><title>VLM PPE judge API</title>
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/swagger-ui-dist@{SWAGGER_UI}/swagger-ui.css"></head><body>
<div id="ui"><p style="font:16px sans-serif;padding:20px">Loading Swagger UI from the internet. If this stays empty, this computer is offline:
open <a href="/api/openapi.json">/api/openapi.json</a> in any Swagger viewer instead.</p></div>
<script src="https://cdn.jsdelivr.net/npm/swagger-ui-dist@{SWAGGER_UI}/swagger-ui-bundle.js"></script>
<script>window.onload=function(){{SwaggerUIBundle({{url:"/api/openapi.json",dom_id:"#ui",deepLinking:true}});}};</script></body></html>"""

PROCESS = """This is the judge on device **__DEVICE__**, the board that serves this page. Every few seconds it takes a camera picture and asks the AI model (Gemma 4 on the
board's NPU) whether the people in it wear a hard hat and a high-visibility vest. It checks the answer, decides **COMPLIANT**, **VIOLATION** or **NO_PERSON** (a violation
is re-checked on further pictures first), and publishes the result. This API only shows that result; nothing here changes the judge.

**Start here:** open **GET /api/overview** → Try it out → Execute. One answer shows this device, the judge state, the newest judgement with its time, the prompt (the question asked of the model) and since when, the model, counters and the last events. The other calls give the same facts one at a time:
- *Picture*: **GET /api/latest/snapshot.jpg** is the picture the newest judgement is about, so it matches the `scene` description and the `timestamp` of **/api/latest**; every judgement of the last 20 also has its picture at the address in its `snapshot` field. **GET /api/snapshot.jpg** is the newest camera picture. The live view with the drawing is the web page on the same address, port 8080.
- *Results*: **/api/latest**, **/api/judgements**, **/api/events**.
- *Status*: **/api/health**, **/api/stats**.
- *Settings*: **/api/meta**, **/api/prompts**, **/api/sensors** (the names these results get in WEDA).

**To integrate:** poll **/api/latest** every second or two; a new judgement comes every 2 to 5 s (`seq` rises by one). Use a result only while `ageSec` is below 15. The `snapshot` field of a judgement gives the address of the exact picture the model judged. An item the model cannot read is `unclear`, not `missing`.

**Other ways to get the same results** (not on this page): the MQTT topics `advantech/<id>/vision-vlm/*` on the board, and the WEDA cloud, where the WEDA Sub-Node turns the
results into 17 data streams (portal: Devices, VlmJudge). The cloud also takes one command, `judge.now` (judge a picture now and return the verdict), and its API gives the newest value of each stream
(`GET /api/v1/orgs/{orgId}/devices/{deviceId}/data-streams/values/latest`, with a WEDA token). The container reference, sections 2 and 2a, describes both.

No picture is sent to the cloud: only this API serves pictures, from the board's memory (the last 20 judged ones and the newest camera picture). This API has no login and is read-only: use it on a trusted network."""


def openapi(cfg, version):
    ppe_item = {"type": "object", "properties": {"required": {"type": "boolean"}, "missing": {"type": "integer", "description": "people without it"},
                                                  "unclear": {"type": "integer", "description": "people for whom it could not be told"}}}
    judgement = {"type": "object", "properties": {
        "schemaVersion": {"type": "string", "example": "1.0"},
        "timestamp": {"type": "string", "format": "date-time", "description": "when the picture was taken, not when the model answered"},
        "deviceId": {"type": "string", "example": cfg.device_id}, "publisherId": {"type": "string"},
        "seq": {"type": "integer", "description": "running number of the judgement since the judge started"},
        "state": {"type": "string", "enum": ["COMPLIANT", "VIOLATION", "NO_PERSON"]},
        "compliant": {"type": "boolean", "nullable": True, "description": "null when nobody is in view"},
        "personCount": {"type": "integer"},
        "violations": {"type": "array", "items": {"type": "string", "enum": ["helmet", "vest", "gloves", "goggles"]}},
        "ppe": {"type": "object", "properties": {"helmet": ppe_item, "vest": ppe_item, "gloves": ppe_item, "goggles": ppe_item}, "description": "one entry for each required item (REQUIRED_PPE)"},
        "scene": {"type": "string", "description": "one short sentence from the model"},
        "latencyMs": {"type": "integer", "description": "time the check took, including re-checks"},
        "checks": {"type": "integer", "description": "on how many pictures the verdict was checked (a violation is re-checked)"},
        "promptVersion": {"type": "string", "example": cfg.prompt_version}, "runId": {"type": "string"},
        "snapshot": {"type": "string", "description": "address of the picture the model judged; present for the last 20 judgements"}}}
    err = {"description": "an error", "content": {"application/json": {"schema": {"$ref": "#/components/schemas/Error"}}}}
    return {
        "openapi": "3.0.3",
        "info": {"title": "VLM PPE judge API", "version": version, "description": PROCESS.replace("__DEVICE__", cfg.device_id)},
        "servers": [{"url": "/"}],
        "tags": [{"name": "Start here"}, {"name": "Results"}, {"name": "Status"}, {"name": "Settings"}, {"name": "Picture"}],
        "paths": {
            "/api/overview": {"get": {"tags": ["Start here"], "summary": "Everything in one answer: state, newest judgement, prompt, model, counters, last events",
                "responses": {"200": {"description": "everything a client needs to see that the judge works: its state, the prompt and since when, the model, the newest judgement (its `scene` is the description), counters and the last 3 events",
                                      "content": {"application/json": {"schema": {"$ref": "#/components/schemas/Overview"}}}}}}},
            "/api/latest": {"get": {"tags": ["Results"], "summary": "The newest judgement",
                "responses": {"200": {"description": "the newest judgement and its age", "content": {"application/json": {"schema": {"allOf": [
                    {"$ref": "#/components/schemas/Judgement"}, {"type": "object", "properties": {"ageSec": {"type": "number"}}}]}}}}, "404": err}}},
            "/api/judgements": {"get": {"tags": ["Results"], "summary": "The newest judgements, newest first",
                "parameters": [{"name": "limit", "in": "query", "schema": {"type": "integer", "minimum": 1, "maximum": 200, "default": 20}}],
                "responses": {"200": {"description": "the last judgements (up to 200 are kept in memory)", "content": {"application/json": {"schema": {
                    "type": "object", "properties": {"count": {"type": "integer"}, "items": {"type": "array", "items": {"$ref": "#/components/schemas/Judgement"}}}}}}},
                              "400": err}}},
            "/api/health": {"get": {"tags": ["Status"], "summary": "Judge and camera state",
                "responses": {"200": {"description": "state and counters", "content": {"application/json": {"schema": {"$ref": "#/components/schemas/Health"}}}}}}},
            "/api/events": {"get": {"tags": ["Results"], "summary": "The newest events, newest first",
                "parameters": [{"name": "limit", "in": "query", "schema": {"type": "integer", "minimum": 1, "maximum": 100, "default": 20}}],
                "responses": {"200": {"description": "PpeViolationDetected, PpeViolationCleared, JudgeDegraded, JudgeRecovered (up to 100 are kept in memory)",
                                      "content": {"application/json": {"schema": {"type": "object", "properties": {"count": {"type": "integer"}, "items": {
                                          "type": "array", "items": {"$ref": "#/components/schemas/Event"}}}}}}}, "400": err}}},
            "/api/stats": {"get": {"tags": ["Status"], "summary": "Counters and latency since the judge started",
                "responses": {"200": {"description": "counters and latency since the judge started",
                                      "content": {"application/json": {"schema": {"$ref": "#/components/schemas/Stats"}}}}}}},
            "/api/meta": {"get": {"tags": ["Settings"], "summary": "Model, prompt, required PPE, camera and timing settings",
                "responses": {"200": {"description": "the same content as the retained MQTT meta message",
                                      "content": {"application/json": {"schema": {"$ref": "#/components/schemas/Meta"}}}}}}},
            "/api/prompts": {"get": {"tags": ["Settings"], "summary": "The prompt in use and the built-in prompts",
                "responses": {"200": {"description": "active version, whether it is your own text (PROMPT_TEXT), and the texts",
                                      "content": {"application/json": {"schema": {"type": "object", "properties": {
                                          "active": {"type": "string"}, "custom": {"type": "boolean"}, "activeText": {"type": "string"},
                                          "inUseSince": {"type": "string", "format": "date-time", "description": "when the judge started with this prompt (the MQTT meta configuredAt)"},
                                          "versions": {"type": "object", "additionalProperties": {"type": "string"}}}}}}}}}},
            "/api/sensors": {"get": {"tags": ["Settings"], "summary": "The WEDA data streams of the Sub-Node and where each value comes from",
                "responses": {"200": {"description": "17 streams: 16 values and a heartbeat",
                                      "content": {"application/json": {"schema": {"type": "object", "properties": {"count": {"type": "integer"}, "items": {
                                          "type": "array", "items": {"type": "object", "properties": {"key": {"type": "string"}, "from": {"type": "string"},
                                                                                                      "meaning": {"type": "string"}}}}}}}}}}}},
            "/api/latest/snapshot.jpg": {"get": {"tags": ["Picture"], "summary": "The picture the newest judgement is about",
                "responses": {"200": {"description": "a JPEG: the frame the model judged. Headers X-Judgement-Seq and X-Judgement-Timestamp say which judgement it belongs to", "content": {"image/jpeg": {"schema": {"type": "string", "format": "binary"}}}}, "404": err}}},
            "/api/judgements/{seq}/snapshot.jpg": {"get": {"tags": ["Picture"], "summary": "The picture behind one judgement",
                "parameters": [{"name": "seq", "in": "path", "required": True, "schema": {"type": "integer"}}],
                "responses": {"200": {"description": "a JPEG: the frame the model judged (the last 20 judgements keep their picture)", "content": {"image/jpeg": {"schema": {"type": "string", "format": "binary"}}}}, "404": err}}},
            "/api/snapshot.jpg": {"get": {"tags": ["Picture"], "summary": "The newest camera picture (no drawing on it)",
                "responses": {"200": {"description": "a JPEG", "content": {"image/jpeg": {"schema": {"type": "string", "format": "binary"}}}}, "503": err}}},
        },
        "components": {"schemas": {
            "Judgement": judgement,
            "Health": {"type": "object", "properties": {
                "service": {"type": "string"}, "deviceId": {"type": "string"}, "promptVersion": {"type": "string"},
                "judge": {"type": "string", "enum": ["online", "degraded"], "description": "degraded: no valid judgement for WATCHDOG_SEC"},
                "camera": {"type": "string", "enum": ["OK", "LOST"]}, "lastJudgementAgeSec": {"type": "number", "nullable": True},
                "uptimeSec": {"type": "integer"}, "counters": {"type": "object", "additionalProperties": {"type": "integer"}}}},
            "Event": {"type": "object", "properties": {
                "schemaVersion": {"type": "string"}, "eventId": {"type": "string"}, "deviceId": {"type": "string"}, "seq": {"type": "integer"},
                "type": {"type": "string", "enum": ["PpeViolationDetected", "PpeViolationCleared", "JudgeDegraded", "JudgeRecovered"]},
                "severity": {"type": "string"}, "timestamp": {"type": "string", "format": "date-time"}, "details": {"type": "object"}}},
            "Stats": {"type": "object", "properties": {
                "schemaVersion": {"type": "string"}, "seq": {"type": "integer"}, "uptimeSec": {"type": "integer"},
                "judgedTotal": {"type": "integer"}, "rejectedTotal": {"type": "integer"}, "timeoutTotal": {"type": "integer"},
                "serverErrors": {"type": "integer"}, "skipped": {"type": "integer"}, "unconfirmed": {"type": "integer"},
                "latencyMsAvg": {"type": "integer", "description": "absent until the first judgement"},
                "latencyMsP95": {"type": "integer", "description": "absent until the first judgement"}}},
            "Meta": {"type": "object", "properties": {
                "schemaVersion": {"type": "string"}, "deviceId": {"type": "string"}, "publisherId": {"type": "string"}, "runId": {"type": "string"},
                "model": {"type": "string"}, "promptVersion": {"type": "string"}, "requiredPpe": {"type": "array", "items": {"type": "string"}},
                "camera": {"type": "string"}, "intervalSec": {"type": "number"}, "minGapSec": {"type": "number"}, "timeoutSec": {"type": "number"},
                "configuredAt": {"type": "string", "format": "date-time"}, "appVersion": {"type": "string"}}},
            "Overview": {"type": "object", "properties": {
                "device": {"type": "string"}, "service": {"type": "string"}, "judge": {"type": "string", "enum": ["online", "degraded"]},
                "camera": {"type": "string", "enum": ["OK", "LOST"]}, "uptimeSec": {"type": "integer"},
                "prompt": {"type": "object", "properties": {"active": {"type": "string"}, "custom": {"type": "boolean"},
                                                            "inUseSince": {"type": "string", "format": "date-time"}, "text": {"type": "string"}}},
                "model": {"type": "string"}, "requiredPpe": {"type": "array", "items": {"type": "string"}},
                "ppeMeans": {"type": "object", "additionalProperties": {"type": "string"}, "description": "what each item name means, for example helmet: a hard hat"},
                "latest": {"allOf": [{"$ref": "#/components/schemas/Judgement"}, {"type": "object", "properties": {"ageSec": {"type": "number"}}}],
                           "nullable": True, "description": "the newest judgement; null until the first one"},
                "stats": {"$ref": "#/components/schemas/Stats"},
                "recentEvents": {"type": "array", "items": {"$ref": "#/components/schemas/Event"}}}},
            "Error": {"type": "object", "properties": {"error": {"type": "string"}}}}},
    }


def _handler(engine, frames, cfg, version, meta_fn=None):
    started = time.monotonic()

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            pass

        def _send(self, code, body, ctype="application/json", headers=None):
            data = body if isinstance(body, bytes) else json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def _with_snapshot(self, msg):
            """The judgement, plus the address of its picture when the picture is still kept."""
            if msg is not None and engine.snapshot(msg["seq"]) is not None:
                return {**msg, "snapshot": f"/api/judgements/{msg['seq']}/snapshot.jpg"}
            return msg

        def _snapshot_of(self, seq):
            """The judged picture of judgement `seq` with its headers, or a 404."""
            jpg = engine.snapshot(seq)
            msg = next((m for m in list(engine.history) if m["seq"] == seq), None)
            if jpg is None or msg is None:
                return self._send(404, {"error": "no picture kept for this judgement (the last %d are kept)" % engine.SNAPSHOTS_KEPT})
            return self._send(200, jpg, "image/jpeg", {"X-Judgement-Seq": str(seq), "X-Judgement-Timestamp": msg["timestamp"]})

        def do_GET(self):
            url = urllib.parse.urlparse(self.path)
            try:
                if url.path in ("/api", "/api/"):
                    return self._send(200, {"service": version, "deviceId": cfg.device_id, "endpoints": ENDPOINTS})
                if url.path == "/api/openapi.json":
                    return self._send(200, openapi(cfg, version))
                if url.path == "/api/docs":
                    return self._send(200, DOCS_PAGE.encode(), "text/html; charset=utf-8")
                if url.path == "/api/overview":
                    meta = meta_fn() if meta_fn else mqtt.build_meta(cfg, engine.run_id)
                    msg = engine.last_msg
                    latest = None if msg is None else {**self._with_snapshot(msg), "ageSec": round(engine.mono() - engine.last_published_mono, 1)}
                    return self._send(200, {
                        "device": cfg.device_id, "service": version, "judge": engine.state,
                        "camera": "OK" if frames.latest() is not None else "LOST", "uptimeSec": round(time.monotonic() - started),
                        "prompt": {"active": cfg.prompt_version, "custom": bool(cfg.prompt_text), "inUseSince": meta.get("configuredAt"),
                                   "text": answer.prompt_for(cfg.prompt_version, cfg.required_ppe, cfg.prompt_text)},
                        "model": meta.get("model"), "requiredPpe": meta.get("requiredPpe"),
                        "ppeMeans": {k: PPE_WORDS.get(k, k) for k in cfg.required_ppe},
                        "latest": latest, "stats": engine.stats(), "recentEvents": list(engine.events)[::-1][:3]})
                if url.path == "/api/health":
                    cam = frames.latest() is not None
                    age = None if engine.last_published_mono is None else round(engine.mono() - engine.last_published_mono, 1)
                    return self._send(200, {"service": version, "deviceId": cfg.device_id, "promptVersion": cfg.prompt_version,
                                            "judge": engine.state, "camera": "OK" if cam else "LOST", "lastJudgementAgeSec": age,
                                            "uptimeSec": round(time.monotonic() - started), "counters": dict(engine.counters)})
                if url.path == "/api/latest":
                    msg = engine.last_msg
                    if msg is None:
                        return self._send(404, {"error": "no judgement yet"})
                    age = round(engine.mono() - engine.last_published_mono, 1)
                    return self._send(200, {**self._with_snapshot(msg), "ageSec": age})
                if url.path == "/api/judgements":
                    try:
                        limit = int(urllib.parse.parse_qs(url.query).get("limit", ["20"])[0])
                    except ValueError:
                        return self._send(400, {"error": "limit must be a whole number"})
                    items = [self._with_snapshot(m) for m in list(engine.history)[::-1][:max(1, min(limit, 200))]]
                    return self._send(200, {"count": len(items), "items": items})
                if url.path == "/api/events":
                    try:
                        limit = int(urllib.parse.parse_qs(url.query).get("limit", ["20"])[0])
                    except ValueError:
                        return self._send(400, {"error": "limit must be a whole number"})
                    items = list(engine.events)[::-1][:max(1, min(limit, 100))]
                    return self._send(200, {"count": len(items), "items": items})
                if url.path == "/api/stats":
                    return self._send(200, engine.stats())
                if url.path == "/api/meta":
                    return self._send(200, meta_fn() if meta_fn else mqtt.build_meta(cfg, engine.run_id))
                if url.path == "/api/prompts":
                    meta = meta_fn() if meta_fn else mqtt.build_meta(cfg, engine.run_id)
                    return self._send(200, {"active": cfg.prompt_version, "custom": bool(cfg.prompt_text), "inUseSince": meta.get("configuredAt"),
                                            "activeText": answer.prompt_for(cfg.prompt_version, cfg.required_ppe, cfg.prompt_text),
                                            "versions": {**answer.PROMPTS, "ppe-v5": answer.build_prompt(cfg.required_ppe)}})
                if url.path == "/api/sensors":
                    return self._send(200, {"count": len(SENSORS), "items": [{"key": k, "from": f, "meaning": m} for k, f, m in SENSORS]})
                if url.path == "/api/latest/snapshot.jpg":
                    msg = engine.last_msg
                    if msg is None:
                        return self._send(404, {"error": "no judgement yet"})
                    return self._snapshot_of(msg["seq"])
                m = re.fullmatch(r"/api/judgements/(\d{1,9})/snapshot\.jpg", url.path)
                if m:
                    return self._snapshot_of(int(m.group(1)))
                if url.path == "/api/snapshot.jpg":
                    frame = frames.latest()
                    if frame is None:
                        return self._send(503, {"error": "no camera picture"})
                    import cv2
                    ok, jpg = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), 85])
                    return self._send(200, jpg.tobytes(), "image/jpeg") if ok else self._send(500, {"error": "JPEG encode failed"})
                return self._send(404, {"error": "unknown path", "see": "/api"})
            except Exception as e:                # an API request must never hurt the judge
                log.warning("api request %s failed: %r", self.path, e)
                self._send(500, {"error": "internal error"})

        def _read_only(self):
            self._send(405, {"error": "this API is read-only: use GET"})
        do_POST = do_PUT = do_DELETE = do_PATCH = _read_only
    return Handler


class ApiServer:
    """Starts on construction. `port` 0 picks a free port (tests); `self.port` is the one in use."""
    def __init__(self, host, port, engine, frames, cfg, version, meta_fn=None):
        self.httpd = http.server.ThreadingHTTPServer((host, port), _handler(engine, frames, cfg, version, meta_fn))
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True, name="api").start()
        log.info("HTTP API on http://%s:%d/api", host, self.port)

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()
