"""The booth web view: the base image's own overlay and MJPEG web server (`/gemma/inference.py`, the VLM app's
front end), fed with the judge's camera frames and its latest judgement. The judge keeps the camera; this only
draws. Never fatal: if the app's code is missing or a draw fails, the judge keeps running."""
import importlib.util
import logging
import os
import threading
import time

log = logging.getLogger("vlm_judge.webview")

APP_PATH = os.environ.get("VLM_APP_PATH", "/gemma/inference.py")
GREEN, RED, GREY, AMBER = (60, 180, 60), (40, 40, 220), (150, 150, 150), (0, 170, 255)   # BGR
STALE_AFTER_SEC = 15.0      # the same as the Sub-Node's StaleAfterSec: the picture and the cloud call a result old at the same time


def describe(engine, cfg):
    """What the screen says: (hud_lines, answer_text, border_colour). Pure, so it can be tested without a camera."""
    m = engine.last_msg
    if m is None:
        return ["WAITING FOR THE FIRST JUDGEMENT"], "", GREY
    age = max(0.0, engine.mono() - engine.last_published_mono)
    stale = engine.state == "degraded" or age > STALE_AFTER_SEC
    state = m["state"]
    if state == "VIOLATION":
        head, colour = "VIOLATION: missing " + ", ".join(m["violations"]), RED
    elif state == "COMPLIANT":
        head, colour = "COMPLIANT", GREEN
    else:
        head, colour = "NO PERSON IN VIEW", GREY
    if stale:
        head, colour = "STALE - " + head, AMBER
    hud = [head, f"People {m['personCount']}   #{m['seq']}   {m['latencyMs'] / 1000:.1f} s   age {int(age)} s",
           "Demo only - not a PPE safety system"]       # the SD requires this label on the screen
    return hud, m.get("scene", ""), colour


def view_parts(engine, cfg):
    """What goes onto the picture: (hud_lines, description, border_colour). The top-left block and the description are each a setting
    (VIEW_HUD, VIEW_DESCRIPTION); the border always shows the state."""
    hud, answer, colour = describe(engine, cfg)
    return (hud if cfg.view_hud else []), (answer if cfg.view_description else ""), colour


def load_app(path=APP_PATH):
    """Load the base image's app file as a module to reuse its `Overlay` and `MjpegHttpServer` (the two names we rely on)."""
    spec = importlib.util.spec_from_file_location("vlm_app", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class WebView(threading.Thread):
    """A thread that draws the newest camera frame with the verdict on it and serves it as a live picture."""
    def __init__(self, cfg, frames, engine, app_path=APP_PATH, fps=10):
        """Loads the base app and builds the overlay and the web server on cfg.http_bind and cfg.http_port. Raises when the app cannot be
        loaded; the caller then runs without the web view."""
        super().__init__(daemon=True, name="webview")
        app = load_app(app_path)
        self.cv2 = __import__("cv2")
        self.np = __import__("numpy")
        self.cfg, self.frames, self.engine, self.period = cfg, frames, engine, 1.0 / fps
        self.overlay = app.Overlay(font_path=None)
        self.server = app.MjpegHttpServer(host=cfg.http_bind, port=cfg.http_port)
        self.stop_flag = False
        self.draw_failures = 0
        log.info("web view on http://%s:%d/", cfg.http_bind, self.server.port)

    def render(self):
        """One picture: the newest frame (or a placeholder) with the HUD text, the scene text and the coloured border."""
        frame = self.frames.latest()
        if frame is None:
            frame = self.np.zeros((480, 640, 3), self.np.uint8)
            hud, answer, colour = ["NO CAMERA FRAME"], "", (0, 170, 255)
        else:
            hud, answer, colour = view_parts(self.engine, self.cfg)
        out = self.overlay.draw(frame.copy(), answer, hud)
        h, w = out.shape[:2]
        self.cv2.rectangle(out, (0, 0), (w - 1, h - 1), colour, 8)
        return out

    def run(self):
        """Draw about `fps` pictures a second until closed; a failed draw is logged and skipped."""
        while not self.stop_flag:
            try:
                self.server.submit(self.render())
            except Exception as e:
                self.draw_failures += 1       # a failing draw repeats 10 times a second: the first 3, then every 100th, like the publisher
                if self.draw_failures <= 3 or self.draw_failures % 100 == 0:
                    log.warning("web view draw failed: %r (failures so far: %d)", e, self.draw_failures)
            time.sleep(self.period)

    def close(self):
        self.stop_flag = True
        self.server.close()
