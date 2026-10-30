"""One judgement cycle and the watchdog, with the model call and the publisher injected (testable
without a camera, a model server or a broker)."""
import collections
import logging
import statistics
import threading
import time
import uuid

from . import answer, mqtt
from .errors import InferError, InferTimeout

log = logging.getLogger("vlm_judge.engine")


class JudgeEngine:
    """Takes the newest picture, asks the model, decides the verdict and publishes it. Keeps the state the web page, `health` and `stats`
    show. The model call, the publisher and the clocks are passed in, so tests need no camera, model server or broker."""
    COMMAND_TTL_SEC = 20.0          # a judge_now that nobody has answered after this long is dropped (the caller gave up)
    SNAPSHOTS_KEPT = 20             # the pictures behind the newest judgements, for the HTTP API (memory only)

    def __init__(self, cfg, infer, publisher, clock=time.time, run_id=None, camera_age=None, mono=None, encode=None):
        # `clock` is wall time (message timestamps only). `mono` measures durations and never jumps, so an NTP step at
        # boot cannot trip the watchdog or the exit rule. With an injected test clock both are the same fake clock.
        """`cfg`: a JudgeConfig. `infer(frame, prompt, timeout)`: returns the model's answer text. `publisher`: where the messages go."""
        self.cfg, self.infer, self.pub, self.clock = cfg, infer, publisher, clock
        self.mono = mono or (time.monotonic if clock is time.time else clock)
        self.camera_age = camera_age        # callable: seconds since the camera thread last read a frame (None: use our own)
        self.encode = encode                # callable: frame -> JPEG bytes; None keeps no pictures
        self.snapshots = collections.deque(maxlen=self.SNAPSHOTS_KEPT)   # (seq, JPEG bytes) of the frames that were judged
        self._lock = threading.Lock()       # the watchdog thread and the judging thread both change `state`
        self.run_id = run_id or str(uuid.uuid4())
        self.state = "online"               # internal: online | degraded (shown as STALE on the page; the status topic stays online)
        self.seq = 0
        self.counters = {"published": 0, "rejected": 0, "timeouts": 0, "serverErrors": 0, "skipped": 0, "unconfirmed": 0}
        self.latencies = collections.deque(maxlen=1000)
        self.started = self.mono()
        self.last_valid_at = self.started
        self.last_server_ok = self.started
        self.last_compliant = None
        self.last_frame_at = self.started
        self.last_published_at = None       # wall time of the newest published judgement (health.lastJudgementAt)
        self.last_published_mono = None     # the same on the monotonic clock (ages)
        self.last_msg = None                # the newest published judgement (the web view shows it)
        self.history = collections.deque(maxlen=200)   # the newest judgements, for the HTTP API
        self.events = collections.deque(maxlen=100)    # the newest events, for the HTTP API
        self.should_exit = False
        self.wake = threading.Event()       # set by a command: start the next judgement now
        self.pending_command = None         # (command id, mono time) the next judgement answers

    def _ask(self, frame):
        """One model request. Returns (outcome, verdict): outcome is 'ok', 'rejected', 'timeout' or 'server_error'."""
        try:
            text = self.infer(frame, answer.prompt_for(self.cfg.prompt_version, self.cfg.required_ppe, self.cfg.prompt_text), self.cfg.timeout_sec)
        except InferTimeout:
            self.counters["timeouts"] += 1
            log.warning("model request timed out after %.0f s", self.cfg.timeout_sec)
            return "timeout", None
        except InferError as e:
            self.counters["serverErrors"] += 1
            log.warning("model server error: %s", e)
            return "server_error", None
        except Exception as e:  # the judge must never crash on a failing model call
            self.counters["serverErrors"] += 1
            log.warning("unexpected error from the model call: %r", e)
            return "server_error", None
        self.last_server_ok = self.mono()
        try:
            parsed = answer.validate(text, self.cfg.required_ppe)
        except answer.Rejected as e:
            self.counters["rejected"] += 1
            log.warning("answer rejected: %s", e)
            return "rejected", None
        return "ok", answer.compute_verdict(parsed, self.cfg.required_ppe)

    def request_judgement(self, command_id):
        """A `judge_now` command: the judgement that STARTS after this call carries the command id (the one in progress
        was taken from an older frame and does not answer it)."""
        with self._lock:
            self.pending_command = (str(command_id), self.mono())
        self.wake.set()

    def snapshot(self, seq):
        """The JPEG of the frame behind judgement `seq`, or None when it was not kept (only the newest are)."""
        return next((jpg for s, jpg in list(self.snapshots) if s == seq), None)       # a copy: the judging thread may append meanwhile

    def _publish(self, v, t_frame, t0, checks, command_id=None, frame=None):
        """`t_frame` (wall) is when the frame behind this verdict was taken; `t0` (mono) is when the cycle started."""
        latency_ms = (self.mono() - t0) * 1000
        self.seq += 1
        msg = mqtt.build_judgement(self.cfg, self.seq, latency_ms, v, self.run_id, t_frame)
        msg["checks"] = checks
        if command_id:
            msg["commandId"] = command_id
        self.pub.publish_judgement(msg)
        if self.cfg.log_judgements:
            log.info("judgement #%d %s, people %d, %.1f s: %s", self.seq, msg["state"], msg["personCount"], latency_ms / 1000, msg["scene"])
        if self.encode is not None and frame is not None:
            try:
                jpg = self.encode(frame)
            except Exception as e:              # a picture that cannot be encoded must not stop the judgement
                log.warning("snapshot not kept: %r", e)
                jpg = None
            if jpg:
                self.snapshots.append((self.seq, jpg))
        self.counters["published"] += 1
        self.latencies.append(latency_ms)
        self.last_valid_at = self.last_published_mono = self.mono()
        self.last_published_at = self.clock()
        self.history.append(msg)            # before last_msg: whoever reads last_msg can find it in the history
        self.last_msg = msg
        self._events_after(v)

    def judge_cycle(self, get_frame) -> str:
        """One judgement: a compliant answer is published at once; a violation is first confirmed on up to
        CONFIRM_FRAMES - 1 further frames and needs CONFIRM_MIN frames that agree, otherwise a compliant answer
        wins (the model often invents people). `get_frame` returns the newest frame or None. A pending `judge_now`
        stays pending until a judgement is published for it or it is older than COMMAND_TTL_SEC."""
        with self._lock:
            pending, self.pending_command = self.pending_command, None
        command_id = pending[0] if pending and self.mono() - pending[1] <= self.COMMAND_TTL_SEC else None
        outcome = self._cycle(get_frame, command_id)
        if command_id and outcome != "published":
            with self._lock:
                if self.pending_command is None:
                    self.pending_command = pending      # the cycle failed: try again for the same command
        return outcome

    def _cycle(self, get_frame, command_id):
        """One judgement attempt (see judge_cycle). Returns `published`, `no_frame`, `unconfirmed`, or the failure of the model call
        (`rejected`, `timeout`, `server_error`)."""
        t0 = self.mono()
        t_frame = self.clock()
        frame = get_frame()
        if frame is None:
            self.counters["skipped"] += 1
            self.last_server_ok = self.mono()           # no attempt was made: a missing camera is not a dead model server
            return "no_frame"
        self.last_frame_at = self.mono()
        outcome, v = self._ask(frame)
        if outcome != "ok":
            return outcome
        verdicts, checks = [(v, t_frame, frame)], 1
        if not v["compliant"] and self.cfg.confirm_frames > 1:
            for _ in range(self.cfg.confirm_frames - 1):
                if sum(1 for x, _, _ in verdicts if not x["compliant"]) >= self.cfg.confirm_min:
                    break                   # already confirmed: no further model request
                t2 = self.clock()
                frame = get_frame()
                if frame is None:
                    break
                o, v2 = self._ask(frame)
                checks += 1
                if o == "ok":
                    verdicts.append((v2, t2, frame))
            agree = sum(1 for x, _, _ in verdicts if not x["compliant"])
            if agree < self.cfg.confirm_min:
                ok = next(((x, t, f) for x, t, f in reversed(verdicts) if x["compliant"]), None)
                if ok is None:       # the other frames failed: nothing confirms the violation and nothing contradicts it
                    self.counters["unconfirmed"] += 1
                    log.info("violation not confirmed (%d of %d frames) and no compliant answer: nothing published", agree, len(verdicts))
                    return "unconfirmed"
                v, t_frame, frame = ok
                log.info("violation not confirmed (%d of %d frames): published as compliant", agree, len(verdicts))
            else:
                # the verdict published is the last violating answer: stamp the frame it came from
                v, t_frame, frame = next((x, t, f) for x, t, f in reversed(verdicts) if not x["compliant"])
        self._publish(v, t_frame, t0, checks, command_id, frame)
        return "published"

    def _event(self, evt):
        """Keep the event for the API, write it to the log (these are the state changes: the picture shows them only as a colour), publish it."""
        kind, details = evt.get("type"), evt.get("details") or {}
        if kind == "JudgeDegraded":
            log.warning("judge degraded: %s", details.get("reason", ""))
        elif kind == "JudgeRecovered":
            log.info("judge recovered")
        elif kind == "PpeViolationDetected":
            log.info("violation detected: missing %s", ", ".join(details.get("violations", [])))
        elif kind == "PpeViolationCleared":
            log.info("violation cleared")
        self.events.append(evt)
        self.pub.publish_event(evt)

    def _events_after(self, v):
        """After each published judgement: JudgeRecovered when the judge had been degraded, PpeViolationDetected when compliance turns
        false, PpeViolationCleared when it turns true again. Events are sent on changes, not on every judgement."""
        with self._lock:
            recovered = self.state == "degraded"
            if recovered:
                self.state = "online"
        if recovered:
            self._event(mqtt.build_event(self.cfg, "JudgeRecovered", self.seq, {}, self.clock()))
        compliant = v["compliant"]
        if not compliant and self.last_compliant is not False:
            self._event(mqtt.build_event(self.cfg, "PpeViolationDetected", self.seq, {
                "personCount": v["personCount"], "violations": v["violations"],
                "missing": {k: x["missing"] for k, x in v["ppe"].items()}, "scene": v["scene"]}, self.clock(), self.run_id))
        elif compliant and self.last_compliant is False:
            self._event(mqtt.build_event(self.cfg, "PpeViolationCleared", self.seq, {}, self.clock(), self.run_id))
        self.last_compliant = compliant

    def tick(self):
        """Call regularly: the watchdog (JudgeDegraded event; the `status` topic stays online as the spec says) and the exit
        rule (the model server was called and failed for too long)."""
        now = self.mono()
        with self._lock:
            degrade = self.state == "online" and now - self.last_valid_at > self.cfg.watchdog_sec
            if degrade:
                self.state = "degraded"
        if degrade:
            self._event(mqtt.build_event(self.cfg, "JudgeDegraded", self.seq, {
                "reason": f"no valid judgement for {int(now - self.last_valid_at)} s"}, self.clock()))
        if now - self.last_server_ok > self.cfg.exit_after_sec:
            self.should_exit = True

    def camera_state(self):
        age = self.camera_age() if self.camera_age else self.mono() - self.last_frame_at
        return "LOST" if age > self.cfg.camera_lost_after_sec else "OK"

    def health(self):
        return mqtt.build_health(self.cfg, self.run_id, self.camera_state(), self.last_published_at, self.counters, self.clock())

    def stats(self):
        """Counters and latency for the `stats` message (names as in the contract: judgedTotal, rejectedTotal, timeoutTotal)."""
        lat = sorted(self.latencies)
        c = dict(self.counters)
        c["judgedTotal"] = c.pop("published")
        c["rejectedTotal"], c["timeoutTotal"] = c.pop("rejected"), c.pop("timeouts")
        if lat:
            c["latencyMsAvg"] = int(statistics.mean(lat))
            c["latencyMsP95"] = int(lat[min(len(lat) - 1, int(0.95 * len(lat)))])
        return mqtt.build_stats(self.seq, c, self.mono() - self.started)
