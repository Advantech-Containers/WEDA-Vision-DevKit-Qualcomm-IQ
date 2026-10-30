"""How a model request can fail, shared by the model client (which raises these) and the engine (which counts them)."""


class InferTimeout(Exception):
    """The model request exceeded JUDGE_TIMEOUT_SEC."""


class InferError(Exception):
    """The model server failed (HTTP status or connection); carries the status, not a message to match."""

    def __init__(self, status=None, detail=""):
        super().__init__(f"model server error {status} {detail}".strip())
        self.status = status
