"""The error vocabulary.

Two kinds of failure, and the SDK treats them differently:

* `TriageError` -> the call returns `is_error=True` with our message in
  `content`. The calling model reads it and can try again. Every one carries an
  explicit recovery instruction, because an error the model cannot act on is a
  dead end with extra words.
* anything else -> a crash: the model gets "Error executing tool <name>" and
  nothing more, and the traceback goes to the log.

There is no configuration-error class here. This server shells out to nothing
and needs no reference data, so there is no "the deployment is broken" state to
report -- which is most of why it is a smaller project than it looks.
"""

import json

from mcp.server.mcpserver.exceptions import ToolError


class TriageError(ToolError):
    """A failure the caller can fix by calling again differently.

    The message is assembled rather than free-form so every error on this server
    has the same three parts in the same order: what happened, what to do about
    it, and any structured detail worth acting on. Making the constructor demand
    a recovery instruction is what stops "errors teach" from being a convention
    people forget under deadline.
    """

    def __init__(
        self,
        problem: str,
        *,
        recover: str,
        detail: dict[str, object] | None = None,
    ) -> None:
        self.problem = problem
        self.recover = recover
        self.detail = detail or {}
        super().__init__(self._render())

    def _render(self) -> str:
        parts = [self.problem, f"To recover: {self.recover}"]
        if self.detail:
            # sort_keys so the message is byte-stable, which makes it assertable
            # in tests and diffable in logs.
            parts.append(f"Detail: {json.dumps(self.detail, sort_keys=True, default=str)}")
        return "\n\n".join(parts)
