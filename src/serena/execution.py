from contextvars import ContextVar, Token
from enum import Enum

_CURRENT_EXECUTION_ID: ContextVar[str | None] = ContextVar("serena_execution_id", default=None)


class ExecutionAccess(Enum):
    """Defines how one tool invocation participates in project execution coordination.

    ``READ`` operations may overlap within one project, ``WRITE`` operations require exclusive
    writer-preferring access, and ``SESSION_CONTROL`` operations alter only the calling session's
    binding. Symbolic reads remain ``READ`` operations, but the coordinator must additionally
    serialize access to an affected SolidLSP service while its higher-level file buffers and symbol
    caches remain mutable without their own synchronization.
    """

    READ = "read"
    WRITE = "write"
    SESSION_CONTROL = "session_control"


def get_current_execution_id() -> str | None:
    """Returns the execution identifier bound to the current tool-call context, if any."""
    return _CURRENT_EXECUTION_ID.get()


def bind_execution_id(execution_id: str | None) -> Token[str | None]:
    """Binds one execution identifier to the current context and returns the reset token."""
    return _CURRENT_EXECUTION_ID.set(execution_id)


def reset_execution_id(token) -> None:
    """Restores the execution identifier context captured before ``bind_execution_id``."""
    _CURRENT_EXECUTION_ID.reset(token)
