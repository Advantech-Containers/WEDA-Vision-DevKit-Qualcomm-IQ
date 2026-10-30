"""MQTT: the messages (contract version 1.0) and the best-effort publisher (pattern of the YOLO demo's publisher: retained
status and meta, Last Will `offline`, back-off 1 to 30 s, a failed publish is counted and never fatal)."""
import datetime
import json
import logging
import re
import time

log = logging.getLogger("vlm_judge.publisher")

# --- messages -------------------------------------------------------------------------------------------------------
SCHEMA_VERSION = "1.0"


def iso(ts=None) -> str:
    t = datetime.datetime.fromtimestamp(time.time() if ts is None else ts, datetime.timezone.utc)
    return t.strftime("%Y-%m-%dT%H:%M:%S.") + f"{t.microsecond // 1000:03d}Z"


def build_judgement(cfg, seq: int, latency_ms: float, verdict: dict, run_id: str, ts=None) -> dict:
    """The `judgements` message (contract 1.0). `state` is NO_PERSON, COMPLIANT or VIOLATION; `compliant` is null when nobody is in view.
    `ts` is when the picture was taken, not when the model answered."""
    state = "NO_PERSON" if verdict["personCount"] == 0 else ("COMPLIANT" if verdict["compliant"] else "VIOLATION")
    return {
        "schemaVersion": SCHEMA_VERSION,
        "timestamp": iso(ts),
        "deviceId": cfg.device_id,
        "publisherId": cfg.device_id,
        "seq": seq,
        "latencyMs": int(latency_ms),
        "personCount": verdict["personCount"],
        "ppe": verdict["ppe"],
        "compliant": None if verdict["personCount"] == 0 else verdict["compliant"],   # SA: null when nobody is in view
        "violations": verdict["violations"],
        "scene": verdict["scene"],
        "state": state,
        "runId": run_id,
        "promptVersion": cfg.prompt_version,
    }


def build_meta(cfg, run_id: str, model: str = "", app_version: str = "", camera: str = "", configured_at=None) -> dict:
    """The retained `meta` message: model, prompt version, required PPE, camera and timing settings."""
    return {
        "schemaVersion": SCHEMA_VERSION, "deviceId": cfg.device_id, "publisherId": cfg.device_id, "runId": run_id, "model": model,
        "promptVersion": cfg.prompt_version, "requiredPpe": list(cfg.required_ppe),
        "camera": camera or cfg.camera_device, "intervalSec": cfg.min_gap_sec, "minGapSec": cfg.min_gap_sec,
        "timeoutSec": cfg.timeout_sec, "configuredAt": iso(configured_at),
        "appVersion": app_version,
    }


def build_health(cfg, run_id: str, camera: str, last_judgement_at, counters: dict, ts=None) -> dict:
    """The SA's `health` message: camera state and counters, published whether or not judgements succeed."""
    return {
        "schemaVersion": SCHEMA_VERSION, "timestamp": iso(ts), "publisherId": cfg.device_id, "runId": run_id,
        "camera": camera, "lastJudgementAt": None if last_judgement_at is None else iso(last_judgement_at),
        "judgedTotal": counters["published"], "rejectedTotal": counters["rejected"],
        "timeoutTotal": counters["timeouts"], "skippedTotal": counters["skipped"],
    }


def build_stats(seq: int, counters: dict, uptime_sec: float) -> dict:
    return {"schemaVersion": SCHEMA_VERSION, "seq": seq, **counters, "uptimeSec": int(uptime_sec)}


SEVERITY = {
    "PpeViolationDetected": "critical",
    "PpeViolationCleared": "info",
    "JudgeDegraded": "warning",
    "JudgeRecovered": "info",
}


def build_event(cfg, etype: str, seq: int, details: dict, ts=None, run_id: str = "") -> dict:
    """An event message. For the PpeViolation events the id is made from the run id and the sequence number (the sequence restarts with
    every run); for the others it uses the time."""
    t = time.time() if ts is None else ts
    key = f"{run_id[:8]}-{seq}" if etype.startswith("Ppe") and run_id else (seq if etype.startswith("Ppe") else int(t * 1000))   # seq restarts with every run
    return {
        "schemaVersion": SCHEMA_VERSION,
        "eventId": f"{cfg.device_id}-{key}-{etype}",
        "type": etype,
        "severity": SEVERITY[etype],
        "timestamp": iso(t),
        "deviceId": cfg.device_id,
        "seq": seq,
        "details": details,
    }


# --- publisher ------------------------------------------------------------------------------------------------------


class MqttPublisher:
    """Publishes to the local broker and never blocks or crashes the judge: a failed publish is counted and logged. Sets a Last
    Will, so a dead judge shows as `offline`."""
    def __init__(self, cfg, meta_factory, client_factory=None):
        self.cfg = cfg
        self._meta_factory = meta_factory
        self._factory = client_factory
        self.client = None
        self.failures = 0
        self.on_command = None            # callable(command_id), set by the runner

    def _topic(self, name):
        return f"{self.cfg.topic_base}/{name}"

    def connect(self):
        """Connect in the background (paho retries with back-off 1 to 30 s). The Last Will is set before connecting, so a crash is
        announced by the broker."""
        if self._factory is None:
            import paho.mqtt.client as mqtt  # not in the base image; installed pinned in the judge image
            self._factory = lambda: mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"vlm-judge-{self.cfg.device_id}")
        self.client = self._factory()
        self.client.will_set(self._topic("status"), "offline", qos=1, retain=True)   # first, before connecting
        self.client.on_connect = self._on_connect
        self.client.on_message = self._on_message
        self.client.reconnect_delay_set(min_delay=1, max_delay=30)
        if hasattr(self.client, "max_queued_messages_set"):
            self.client.max_queued_messages_set(200)     # while the broker is down, do not queue without limit
        try:
            self.client.connect_async(self.cfg.mqtt_host, self.cfg.mqtt_port, keepalive=10)
            self.client.loop_start()
        except Exception as e:  # never fatal: the web view keeps running
            self.failures += 1
            log.warning("MQTT connect failed: %s", e)

    def _announce(self):
        """On every (re)connect: listen for commands, then say `online` and send `meta` (both retained)."""
        try:
            self.client.subscribe(self._topic("commands"), qos=1)   # the Sub-Node asks for a judgement on a WEDA command
        except Exception as e:
            log.warning("subscribe to commands failed: %s", e)
        self._send("status", "online", qos=1, retain=True)
        self._send("meta", self._meta_factory(), qos=1, retain=True)

    def _send(self, name, payload, qos=0, retain=False):
        """Publish one message. Returns True when it was handed to the broker; False (and counted) when not connected, the queue is
        full, or paho raised."""
        try:
            data = payload if isinstance(payload, str) else json.dumps(payload, separators=(",", ":"))
            info = self.client.publish(self._topic(name), data, qos=qos, retain=retain)
            rc = getattr(info, "rc", 0)
            if rc != 0:                       # paho returns an error code (not connected, queue full) instead of raising
                self.failures += 1
                if self.failures <= 3 or self.failures % 100 == 0:
                    log.warning("publish %s not sent (rc=%s); failures so far: %d", name, rc, self.failures)
                return False
            return True
        except Exception as e:
            self.failures += 1
            log.warning("publish %s failed: %s", name, e)
            return False

    def _on_connect(self, client, userdata, flags, reason_code=None, *args, **kwargs):
        """paho callback: announce online, unless the broker refused the connection."""
        if getattr(reason_code, "is_failure", False):        # refused (bad credentials, not authorised): do not announce online
            self.failures += 1
            log.warning("MQTT connect refused: %s", reason_code)
            return
        self._announce()

    def _on_message(self, client, userdata, msg):
        """Only `judge_now` with a short id is accepted; anything else is logged and ignored."""
        try:
            d = json.loads(msg.payload)
            cid = d["commandId"]
            ok = (str(d.get("schemaVersion", "")).split(".")[0] == "1" and d.get("command") == "judge_now"
                  and isinstance(cid, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,64}", cid))
        except Exception:
            ok = False
        if not ok:
            log.warning("ignored a command message that is not a valid judge_now (%d bytes)", len(msg.payload))
            return
        log.info("command judge_now %s", cid)
        if self.on_command:
            self.on_command(cid)

    def publish_meta(self, meta):
        return self._send("meta", meta, qos=1, retain=True)

    def publish_judgement(self, msg):
        return self._send("judgements", msg, qos=0)

    def publish_event(self, evt):
        return self._send("events", evt, qos=1)

    def publish_health(self, health):
        return self._send("health", health, qos=0)

    def publish_stats(self, stats):
        return self._send("stats", stats, qos=0)

    def close(self):
        """A clean stop publishes 'offline' (a crash is covered by the Last Will)."""
        try:
            info = self.client.publish(self._topic("status"), "offline", qos=1, retain=True)
            if hasattr(info, "wait_for_publish"):
                info.wait_for_publish(timeout=2)      # a clean disconnect suppresses the Last Will: the message must leave first
            self.client.loop_stop()
            self.client.disconnect()
        except Exception as e:
            log.warning("MQTT close failed (the Last Will still announces offline): %r", e)
