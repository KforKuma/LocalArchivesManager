from __future__ import annotations

import contextvars
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(slots=True)
class RunContext:
    run_id: str
    caller: str
    library_root: Path
    dry_run: bool
    top_level_command: str
    lock_state: str = "not_required"
    final_check_allowed: bool = True
    report_context: dict[str, Any] = field(default_factory=dict)
    journal_context: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def create(
        cls,
        *,
        caller: str,
        library_root: Path,
        dry_run: bool,
        top_level_command: str,
    ) -> "RunContext":
        return cls(
            run_id=str(uuid.uuid4()),
            caller=caller,
            library_root=library_root.resolve(),
            dry_run=dry_run,
            top_level_command=top_level_command,
        )


_CURRENT_RUN_CONTEXT: contextvars.ContextVar[RunContext | None] = contextvars.ContextVar(
    "lam_run_context", default=None
)


def current_run_context() -> RunContext | None:
    return _CURRENT_RUN_CONTEXT.get()


def claim_final_check() -> bool:
    """Claim the one final-check allowed for the active CLI invocation."""
    context = current_run_context()
    if context is None:
        return True
    if not context.final_check_allowed:
        return False
    context.final_check_allowed = False
    return True


class activate_run_context:
    def __init__(self, context: RunContext):
        self.context = context
        self.token: contextvars.Token[RunContext | None] | None = None

    def __enter__(self) -> RunContext:
        self.token = _CURRENT_RUN_CONTEXT.set(self.context)
        return self.context

    def __exit__(self, exc_type, exc_value, traceback) -> bool:
        if self.token is not None:
            _CURRENT_RUN_CONTEXT.reset(self.token)
        return False
