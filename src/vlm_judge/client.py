"""Model-server client (GenieX, OpenAI-style). Errors are classified by type and HTTP status, never by
matching digits in a message (the stock app's 400/422/500 text match switches to text-only for good)."""
import base64
import http.client
import json
import socket
import urllib.error
import urllib.request

from .errors import InferError, InferTimeout


# loopback calls must not go through HTTP_PROXY from the environment
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class GeniexClient:
    """HTTP client for the GenieX model server (OpenAI-style API). The picture goes as a small JPEG; one request at a time."""
    def __init__(self, url, model=None, max_tokens=512, temperature=0.0, jpeg_max_side=512):
        self.url, self.model = url.rstrip("/"), model
        self.max_tokens, self.temperature, self.jpeg_max_side = max_tokens, temperature, jpeg_max_side

    def discover_model(self, timeout=10):
        """Model id from /v1/models when none was configured (first listed)."""
        with _OPENER.open(f"{self.url}/v1/models", timeout=timeout) as r:
            ids = [m["id"] for m in json.loads(r.read()).get("data", []) if m.get("id")]
        if not ids:
            raise InferError(None, "no model is loaded on the server")
        self.model = self.model or ids[0]
        return self.model

    def _jpeg(self, frame):
        """Shrink the frame so its longest side is at most `jpeg_max_side` pixels, encode it as JPEG and return it as base64 text."""
        import cv2  # in the base image
        h, w = frame.shape[:2]
        scale = self.jpeg_max_side / float(max(h, w))
        if scale < 1.0:
            frame = cv2.resize(frame, (int(w * scale), int(h * scale)))
        ok, enc = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), 80])
        if not ok:
            raise InferError(None, "JPEG encode failed")
        return base64.b64encode(enc.tobytes()).decode("ascii")

    def infer(self, frame, prompt, timeout):
        """The engine's `infer` callable: returns the answer text."""
        if self.model is None:
            try:
                self.discover_model()
            except (urllib.error.URLError, socket.timeout, OSError) as e:
                raise InferError(None, str(e))
        body = json.dumps({
            "model": self.model, "max_tokens": self.max_tokens, "temperature": self.temperature, "stream": False,
            "messages": [{"role": "user", "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + self._jpeg(frame)}}]}],
        }).encode()
        req = urllib.request.Request(f"{self.url}/v1/chat/completions", body, {"Content-Type": "application/json"})
        try:
            with _OPENER.open(req, timeout=timeout) as r:
                data = json.loads(r.read())
        except urllib.error.HTTPError as e:
            try:
                detail = e.read(200).decode("utf-8", "replace").strip() or e.reason      # the server's own reason, e.g. SDKError
            except Exception:
                detail = e.reason
            finally:
                e.close()
            raise InferError(e.code, detail)
        except (socket.timeout, TimeoutError):
            raise InferTimeout()
        except urllib.error.URLError as e:
            if isinstance(e.reason, (socket.timeout, TimeoutError)):
                raise InferTimeout()
            raise InferError(None, str(e.reason))
        except (OSError, http.client.HTTPException, ValueError) as e:
            # the connection dropped mid-answer (server died or restarted) or the reply was not JSON
            raise InferError(None, type(e).__name__)
        try:
            text = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            raise InferError(None, "unexpected response shape")
        return "".join(p.get("text", "") if isinstance(p, dict) else str(p) for p in text) if isinstance(text, list) else text
