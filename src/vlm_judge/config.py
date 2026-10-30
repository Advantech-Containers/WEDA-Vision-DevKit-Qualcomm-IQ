"""Settings from environment variables. Any invalid value stops the start with an error naming the variable."""
import os
import re
from dataclasses import dataclass
from urllib.parse import urlparse

from .answer import PROMPTS

DEFAULT_PPE = ("helmet", "vest")
ALLOWED_PPE = ("helmet", "vest", "gloves", "goggles")
LOOPBACK = {"127.0.0.1", "localhost", "::1"}


class ConfigError(ValueError):
    """Raised at start; the message names the variable."""


def _float(env, name, default, lo, hi):
    """Read a number from the environment. Text and values outside lo..hi are rejected with an error that names the variable."""
    raw = env.get(name, str(default))
    try:
        v = float(raw)
    except ValueError:
        raise ConfigError(f"{name}: '{raw}' is not a number")
    if not lo <= v <= hi:
        raise ConfigError(f"{name}: {v} is outside {lo}..{hi}")
    return v


# Values pasted into a portal often carry a stray space or capital letter. Spaces around a value are ignored and these are not case-sensitive.
# The board id is left exactly as given: the Sub-Node gets its own copy of it, and the two must be identical.
_LOWER = ("JUDGE_MODE", "WEB_VIEW", "VIEW_HUD", "VIEW_DESCRIPTION", "LOG_JUDGEMENTS", "PROMPT_VERSION", "REQUIRED_PPE")
_EXACT = ("DEVICE_ID", "PUBLISHER_ID")


def _tidy(env):
    out = {}
    for k, v in env.items():
        if isinstance(v, str) and not v.strip():
            continue                    # a blank field (for example an empty row in the portal) is the same as not set
        if isinstance(v, str) and k not in _EXACT:
            v = v.strip()
            if k in _LOWER:
                v = v.lower()
        out[k] = v
    return out


def _port(env, name, default):
    """Read a TCP port from the environment (a whole number, 1..65535); errors name the variable."""
    try:
        v = int(env.get(name, str(default)))
    except ValueError:
        raise ConfigError(f"{name}: not an integer")
    if not 1 <= v <= 65535:
        raise ConfigError(f"{name}: {v} is outside 1..65535")
    return v


def _flag(env, name, default):
    """A true/false setting; anything else stops the start with the name of the variable."""
    value = env.get(name, "true" if default else "false")
    if value not in ("true", "false"):
        raise ConfigError(f"{name}: '{value}' must be 'true' or 'false'")
    return value == "true"


@dataclass(frozen=True)
class JudgeConfig:
    """Every setting of the judge, checked once at start. Build it with `JudgeConfig.from_env()`; the settings are listed in
    docs/vlm-container-reference.md."""
    mode: str = "off"
    device_id: str = ""
    required_ppe: tuple = DEFAULT_PPE
    prompt_version: str = "ppe-v3"
    prompt_text: str = ""                   # PROMPT_TEXT: the question in your own words (then prompt_version is "custom")
    min_gap_sec: float = 0.5       # idle gap between judgements (one request in flight)
    timeout_sec: float = 15.0      # about 7x a request (about 2 s), above the first request after a model load (about 8 s)
    watchdog_sec: float = 60.0     # no valid judgement for this long -> degraded
    exit_after_sec: float = 600.0  # model server gone for this long -> exit so Docker restarts
    confirm_frames: int = 3        # a violation is confirmed on up to this many frames before it is published
    confirm_min: int = 2           # ... and needs at least this many frames to agree
    health_interval_sec: float = 10.0     # `health` topic cadence
    camera_lost_after_sec: float = 5.0    # no frame for this long -> camera LOST
    topic_base_override: str = ""
    model_server_url: str = "http://127.0.0.1:18181"
    mqtt_host: str = "127.0.0.1"
    mqtt_port: int = 1883
    camera_device: str = "/dev/video0"       # the camera node or a video file: JUDGE_SOURCE
    http_bind: str = "127.0.0.1"
    http_port: int = 8080
    api_port: int = 0                       # API_PORT: the read-only HTTP API (0 = off, the default)
    web_view: bool = True
    view_hud: bool = False                  # VIEW_HUD: the state, people count, number, latency and demo label on the picture
    view_description: bool = True           # VIEW_DESCRIPTION: the model's sentence on the picture
    log_judgements: bool = True             # LOG_JUDGEMENTS: one log line per judgement with state, people and description

    @property
    def topic_base(self) -> str:
        return self.topic_base_override or f"advantech/{self.device_id}/vision-vlm"

    @classmethod
    def from_env(cls, env=None) -> "JudgeConfig":
        """Read and check all environment variables. `JUDGE_MODE=off` (the default) returns a config that does nothing. Anything invalid
        raises ConfigError naming the variable, so a wrong setting stops the start instead of running with a surprise."""
        env = _tidy(os.environ if env is None else env)
        mode = env.get("JUDGE_MODE", "off")
        if mode not in ("off", "ppe"):
            raise ConfigError(f"JUDGE_MODE: '{mode}' must be 'off' or 'ppe'")
        if mode == "off":
            return cls(mode="off")

        id_var = "PUBLISHER_ID" if env.get("PUBLISHER_ID") else "DEVICE_ID"   # PUBLISHER_ID is the SA's name
        device_id = env.get(id_var, "")
        if not re.fullmatch(r"[0-9a-f]{12}", device_id):
            raise ConfigError(f"{id_var}: '{device_id}' must be 12 lowercase hex characters (the node id)")

        ppe = tuple(p.strip() for p in env.get("REQUIRED_PPE", ",".join(DEFAULT_PPE)).split(",") if p.strip())
        if not ppe or any(p not in ALLOWED_PPE for p in ppe) or len(set(ppe)) != len(ppe):
            raise ConfigError(f"REQUIRED_PPE: '{','.join(ppe)}' must be a non-empty subset of {','.join(ALLOWED_PPE)}")

        if not env.get("PROMPT_TEXT") and env.get("PROMPT_VERSION", "ppe-v3") not in PROMPTS:
            raise ConfigError(f"PROMPT_VERSION: '{env.get('PROMPT_VERSION')}' is not a known prompt ({', '.join(PROMPTS)})")
        extra = [i for i in ppe if i not in DEFAULT_PPE]
        if extra and not env.get("PROMPT_TEXT") and env.get("PROMPT_VERSION", "ppe-v3") != "ppe-v5":
            raise ConfigError(f"REQUIRED_PPE: {','.join(extra)} needs PROMPT_VERSION=ppe-v5 (the other built-in prompts ask only for helmet and vest)")
        url = env.get("MODEL_SERVER_URL", "http://127.0.0.1:18181")
        if urlparse(url).hostname not in LOOPBACK:
            raise ConfigError(f"MODEL_SERVER_URL: '{url}' must be a loopback address")
        mqtt_host = env.get("MQTT_HOST", "127.0.0.1")
        if mqtt_host not in LOOPBACK:
            raise ConfigError(f"MQTT_HOST: '{mqtt_host}' must be a loopback address")
        mqtt_port = _port(env, "MQTT_PORT", 1883)
        base = env.get("MQTT_TOPIC_BASE", "")
        if base and not re.fullmatch(r"[A-Za-z0-9._-]+(/[A-Za-z0-9._-]+)*", base):
            raise ConfigError(f"MQTT_TOPIC_BASE: '{base}' must be plain topic levels (letters, digits . _ -), no # + $ or empty level")

        timeout = _float(env, "JUDGE_TIMEOUT_SEC", 15.0, 1.0, 600.0)
        watchdog = _float(env, "WATCHDOG_SEC", 60.0, 10.0, 3600.0)
        if watchdog <= timeout:
            raise ConfigError(f"WATCHDOG_SEC: {watchdog} must be longer than JUDGE_TIMEOUT_SEC {timeout}")

        cf_raw, cm_raw = _float(env, "CONFIRM_FRAMES", 3, 1, 9), _float(env, "CONFIRM_MIN", 2, 1, 9)
        for name, v in (("CONFIRM_FRAMES", cf_raw), ("CONFIRM_MIN", cm_raw)):
            if v != int(v):
                raise ConfigError(f"{name}: {v} must be a whole number")
        cf, cm = int(cf_raw), int(cm_raw)
        if cm > cf:
            raise ConfigError(f"CONFIRM_MIN: {cm} must not exceed CONFIRM_FRAMES {cf}")
        prompt_text = env.get("PROMPT_TEXT", "")
        if len(prompt_text) > 4000:
            raise ConfigError(f"PROMPT_TEXT: {len(prompt_text)} characters, at most 4000")
        http_port = _port(env, "HTTP_PORT", 8080)
        try:
            api_port = int(env.get("API_PORT", "0"))
        except ValueError:
            raise ConfigError("API_PORT: not an integer")
        if not 0 <= api_port <= 65535:
            raise ConfigError(f"API_PORT: {api_port} is outside 0..65535 (0 turns the API off)")
        if api_port and api_port == http_port and env.get("WEB_VIEW", "true") == "true":
            raise ConfigError(f"API_PORT: {api_port} is also HTTP_PORT; the live view and the API need different ports")
        if env.get("WEB_VIEW", "true").lower() not in ("true", "false"):
            raise ConfigError(f"WEB_VIEW: '{env.get('WEB_VIEW')}' must be 'true' or 'false'")
        health = _float(env, "HEALTH_INTERVAL_SEC", 10.0, 5.0, 60.0)
        cam_lost = _float(env, "CAMERA_LOST_AFTER_SEC", 5.0, 1.0, 60.0)
        return cls(
            mode=mode,
            health_interval_sec=health,
            camera_lost_after_sec=cam_lost,
            topic_base_override=base,
            device_id=device_id,
            required_ppe=ppe,
            prompt_version="custom" if prompt_text else env.get("PROMPT_VERSION", "ppe-v3"),
            prompt_text=prompt_text,
            # JUDGE_INTERVAL_SEC (the SA's name) is the fallback and is checked only when JUDGE_MIN_GAP_SEC is not set
            min_gap_sec=(_float(env, "JUDGE_MIN_GAP_SEC", 0.5, 0.0, 600.0) if "JUDGE_MIN_GAP_SEC" in env
                         else _float(env, "JUDGE_INTERVAL_SEC", 0.5, 0.0, 600.0)),
            timeout_sec=timeout,
            watchdog_sec=watchdog,
            exit_after_sec=_float(env, "EXIT_AFTER_SEC", 600.0, 30.0, 86400.0),
            confirm_frames=cf,
            confirm_min=cm,
            model_server_url=url,
            mqtt_host=mqtt_host,
            mqtt_port=mqtt_port,
            camera_device=env.get("JUDGE_SOURCE") or "/dev/video0",
            http_bind=env.get("HTTP_BIND", "127.0.0.1"),
            http_port=http_port,
            api_port=api_port,
            web_view=env.get("WEB_VIEW", "true").lower() == "true",
            view_hud=_flag(env, "VIEW_HUD", False),
            view_description=_flag(env, "VIEW_DESCRIPTION", True),
            log_judgements=_flag(env, "LOG_JUDGEMENTS", True),
        )
