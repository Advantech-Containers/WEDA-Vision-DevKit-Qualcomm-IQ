"""Runs the judge: camera or video file -> model server -> engine -> MQTT. Configuration from the environment
(see config.py). Extra: JUDGE_MODEL = model id (the first one the server lists when unset)."""
import logging
import os
import re
import signal
import threading
import time

from .client import GeniexClient
from .config import ConfigError, JudgeConfig
from .engine import JudgeEngine
from .mqtt import MqttPublisher, build_meta

log = logging.getLogger("vlm_judge.runner")


class FrameSource(threading.Thread):
    """Reads continuously and keeps only the newest frame (the model request is slow). A file is looped.
    A camera that stops delivering is closed and opened again with back-off; `age()` is the time since the last good
    frame, so a frozen or dead reader shows up as an old frame instead of a picture that looks current."""

    def __init__(self, source, max_age=5.0):
        """`source`: a camera device (/dev/video0) or a video file. `max_age`: a frame older than this counts as missing."""
        super().__init__(daemon=True, name="frames")
        import cv2
        self.cv2, self.source, self.max_age = cv2, source, max_age
        self.lock, self.frame, self.stop_flag = threading.Lock(), None, False
        self.last_ok_at = time.monotonic()            # grace period at start
        self.is_file = os.path.isfile(source)
        self.cap = self._open()

    def _open(self):
        """Open the camera or the file. Returns the capture, or None (and logs why) when it is not there or will not open."""
        try:
            if self.is_file:
                cap = self.cv2.VideoCapture(self.source)
            else:
                real = os.path.realpath(self.source)             # /dev/v4l/by-id/... -> /dev/videoN
                m = re.search(r"video(\d+)$", real)
                if not os.path.exists(real) or not m:
                    log.warning("camera %s is not available", self.source)
                    return None
                cap = self.cv2.VideoCapture(int(m.group(1)), self.cv2.CAP_V4L2)
                cap.set(self.cv2.CAP_PROP_FRAME_WIDTH, 640)
                cap.set(self.cv2.CAP_PROP_FRAME_HEIGHT, 480)
            if not cap.isOpened():
                cap.release()
                log.warning("camera %s could not be opened", self.source)
                return None
            return cap
        except Exception as e:
            log.warning("camera %s: %r", self.source, e)
            return None

    def age(self):
        return time.monotonic() - self.last_ok_at

    def run(self):
        """The reading loop: keep the newest frame; loop a file at its end; reopen a camera that stopped delivering, with back-off
        1 to 10 s."""
        delay = 1 / 25 if self.is_file else 0
        fails, backoff = 0, 1.0
        while not self.stop_flag:
            ok, f = False, None
            if self.cap is not None:
                try:
                    ok, f = self.cap.read()
                except Exception as e:
                    log.warning("camera read failed: %r", e)
            if ok:
                fails, backoff = 0, 1.0
                with self.lock:
                    self.frame, self.last_ok_at = f, time.monotonic()
            elif self.is_file and self.cap is not None:      # end of the clip: loop it
                self.cap.set(self.cv2.CAP_PROP_POS_FRAMES, 0)
                time.sleep(0.2)                               # an empty or unreadable clip must not spin a CPU
                continue
            else:
                fails += 1
                if fails >= 3:                                # the handle is dead (unplugged, re-enumerated): open it again
                    if self.cap is not None:
                        self.cap.release()
                    self.cap = self._open()
                    if self.cap is not None:
                        log.info("camera %s opened again", self.source)
                    time.sleep(backoff)
                    backoff = min(backoff * 2, 10.0)
                else:
                    time.sleep(0.5)
                continue
            time.sleep(delay)

    def latest(self):
        with self.lock:
            return self.frame if self.age() <= self.max_age else None


def _jpeg(frame):
    """A camera frame as JPEG bytes (the picture behind a judgement), or None when it cannot be encoded."""
    import cv2
    ok, jpg = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), 85])
    return jpg.tobytes() if ok else None


def _version():
    return "vlm-judge " + __import__("vlm_judge").__version__


def _setup_logging():
    level = os.environ.get("LOG_LEVEL", "INFO").upper()
    logging.basicConfig(level=level if level in ("DEBUG", "INFO", "WARNING", "ERROR") else "INFO",
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")


def _load_config():
    """The settings, or a clear exit: a wrong setting must stop the start, and so must JUDGE_MODE=off."""
    try:
        cfg = JudgeConfig.from_env()
    except ConfigError as e:
        raise SystemExit(f"configuration error: {e}")
    if cfg.mode != "ppe":
        raise SystemExit("JUDGE_MODE is 'off': nothing to do")
    return cfg


def _start_web_view(cfg, frames, engine):
    """The live picture, or None: the judge never depends on the screen."""
    if not cfg.web_view:
        return None
    try:
        from .webview import WebView
        web = WebView(cfg, frames, engine)
        web.start()
        return web
    except Exception as e:
        log.warning("web view not started (%r): the judge runs without it", e)
        return None


def _start_api(cfg, frames, engine, meta_fn=None):
    """The read-only HTTP API, or None: the judge never depends on it."""
    if not cfg.api_port:
        return None
    try:
        from .api import ApiServer
        return ApiServer(cfg.http_bind, cfg.api_port, engine, frames, cfg, _version(), meta_fn)
    except Exception as e:
        log.warning("HTTP API not started (%r): the judge runs without it", e)
        return None


def _background(engine, pub, cfg, stop):
    """Watchdog, health and stats, independent of the slow model request."""
    last_stats = last_health = -1e9
    while not stop.is_set():
        try:
            engine.tick()
            if engine.should_exit:
                log.error("model server failed for %.0f s: exiting so Docker restarts the container", cfg.exit_after_sec)
                stop.set()
            now = time.monotonic()
            if now - last_health >= cfg.health_interval_sec:
                pub.publish_health(engine.health())
                last_health = now
            if now - last_stats >= 60:
                pub.publish_stats(engine.stats())
                last_stats = now
        except Exception:                # one bad tick must not silence the watchdog for good
            log.exception("background task failed; continuing")
        stop.wait(1)


def _judge_loop(cfg, engine, frames, client, pub, stop, meta_for):
    """Judge, wait (a command skips the wait, a stop ends it at once), and back off while the model server fails."""
    meta_has_model = bool(client.model)
    fails = 0
    while not stop.is_set():
        try:
            outcome = engine.judge_cycle(frames.latest)
        except Exception:                # an unexpected failure must not end the judge: log it and try again after a pause
            log.exception("judgement cycle failed unexpectedly")
            outcome = "error"
        if outcome == "no_frame":
            stop.wait(1)
            continue
        fails = 0 if outcome in ("published", "unconfirmed") else fails + 1       # the server answered, so no back-off
        if fails and (fails == 1 or fails % 20 == 0):     # failures only (a judgement has its own line); a dead server must not fill the log
            log.info("judgement cycle: %s (seq=%d)%s", outcome, engine.seq, "" if fails <= 1 else f" [{fails} in a row]")
        if client.model and not meta_has_model:      # the model id is only known after the first request: refresh the retained meta
            meta_has_model = True
            pub.publish_meta(meta_for())
        backoff = cfg.min_gap_sec if fails == 0 else max(cfg.min_gap_sec, min(10.0, 0.5 * 2 ** min(fails, 5)))
        end = time.monotonic() + backoff
        while not stop.is_set() and time.monotonic() < end and not engine.wake.wait(min(0.5, max(0.0, end - time.monotonic()))):
            pass
        engine.wake.clear()


def main():
    """Start everything and run until stopped: config, camera, model client, MQTT, the engine, the web view and the
    HTTP API (both in the background), and a thread for watchdog, health and stats; then the judging loop. Returns 1 when the model server
    failed for too long (Docker then restarts the container), otherwise 0."""
    _setup_logging()
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())     # installed first: a stop during start-up must not be lost
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    cfg = _load_config()
    source = cfg.camera_device
    client = GeniexClient(cfg.model_server_url, model=os.environ.get("JUDGE_MODEL"))
    configured_at = time.time()
    holder = {}

    def meta_for():
        return build_meta(cfg, holder["engine"].run_id, model=client.model or "", app_version=_version(), camera=source,
                          configured_at=configured_at)
    pub = MqttPublisher(cfg, meta_for)
    frames = FrameSource(source, max_age=cfg.camera_lost_after_sec)
    engine = JudgeEngine(cfg, client.infer, pub, camera_age=frames.age, encode=_jpeg if cfg.api_port else None)    # pictures are kept only for the API
    holder["engine"] = engine
    frames.start()
    pub.on_command = engine.request_judgement
    pub.connect()
    web = _start_web_view(cfg, frames, engine)
    api = _start_api(cfg, frames, engine, meta_for)
    threading.Thread(target=_background, args=(engine, pub, cfg, stop), daemon=True, name="watchdog").start()
    log.info("judge started: source=%s model=%s device=%s", source, client.model or "(first listed)", cfg.device_id)
    log.info("VLM scene understanding, demo only: not a PPE safety system")
    _judge_loop(cfg, engine, frames, client, pub, stop, meta_for)
    frames.stop_flag = True
    if web:
        web.close()
    if api:
        api.close()
    pub.close()
    return 0 if not engine.should_exit else 1


if __name__ == "__main__":
    raise SystemExit(main())
