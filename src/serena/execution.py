import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar, Token
from enum import Enum
from typing import TypeVar

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


T = TypeVar("T")


class ProjectExecutionCoordinator:
    """Coordinates concurrent execution within one project runtime.

    Reads may overlap, writes are exclusive, and waiting writers prevent later reads from
    overtaking them. Symbolic reads additionally share one conservative service lock until
    SolidLSP's higher-level request/cache path is proven safe for concurrent use.
    """

    def __init__(self) -> None:
        self._condition = threading.Condition()
        self._active_readers = 0
        self._writer_active = False
        self._waiting_writers = 0
        self._symbolic_read_lock = threading.Lock()

    def execute(self, access: ExecutionAccess, call: Callable[[], T], *, symbolic_read: bool = False) -> T:
        """Executes ``call`` under the requested project access policy."""
        if access is ExecutionAccess.SESSION_CONTROL:
            return call()

        if symbolic_read and access is ExecutionAccess.READ:
            with self._symbolic_read_lock:
                with self._access(access):
                    return call()

        with self._access(access):
            return call()

    @contextmanager
    def _access(self, access: ExecutionAccess) -> Iterator[None]:
        if access is ExecutionAccess.READ:
            with self._condition:
                while self._writer_active or self._waiting_writers > 0:
                    self._condition.wait()
                self._active_readers += 1
            try:
                yield
            finally:
                with self._condition:
                    self._active_readers -= 1
                    if self._active_readers == 0:
                        self._condition.notify_all()
            return

        if access is not ExecutionAccess.WRITE:
            raise ValueError(f"Unsupported project execution access: {access}")

        with self._condition:
            self._waiting_writers += 1
            try:
                while self._writer_active or self._active_readers > 0:
                    self._condition.wait()
                self._writer_active = True
            finally:
                self._waiting_writers -= 1
        try:
            yield
        finally:
            with self._condition:
                self._writer_active = False
                self._condition.notify_all()


class RuntimeReadiness:
    """Owns the one-time asynchronous initialisation state of a project runtime."""

    class State(Enum):
        PENDING = "pending"
        INITIALIZING = "initializing"
        READY = "ready"
        FAILED = "failed"

    def __init__(self) -> None:
        self._condition = threading.Condition()
        self._state = self.State.PENDING
        self._error: BaseException | None = None
        self._thread: threading.Thread | None = None

    def start(self, initializer: Callable[[], None], *, thread_name: str) -> bool:
        """Starts initialisation exactly once and returns whether this call started it."""

        def run() -> None:
            try:
                initializer()
            except BaseException as exc:
                with self._condition:
                    self._error = exc
                    self._state = self.State.FAILED
                    self._condition.notify_all()
            else:
                with self._condition:
                    self._state = self.State.READY
                    self._condition.notify_all()

        with self._condition:
            if self._state is not self.State.PENDING:
                return False
            thread = threading.Thread(target=run, name=thread_name, daemon=True)
            self._thread = thread
            self._state = self.State.INITIALIZING
            thread.start()

        return True

    def wait_until_ready(self) -> None:
        """Blocks until initialisation is complete and re-raises a stable failure if it failed."""
        with self._condition:
            while self._state in (self.State.PENDING, self.State.INITIALIZING):
                self._condition.wait()
            error = self._error

        if error is not None:
            raise RuntimeError(f"Project runtime initialisation failed: {error}") from error

    def join(self, timeout: float | None = None) -> None:
        """Waits for an in-flight initialisation thread without starting one."""
        with self._condition:
            thread = self._thread
        if thread is not None:
            thread.join(timeout=timeout)


def get_current_execution_id() -> str | None:
    """Returns the execution identifier bound to the current tool-call context, if any."""
    return _CURRENT_EXECUTION_ID.get()


def bind_execution_id(execution_id: str | None) -> Token[str | None]:
    """Binds one execution identifier to the current context and returns the reset token."""
    return _CURRENT_EXECUTION_ID.set(execution_id)


def reset_execution_id(token) -> None:
    """Restores the execution identifier context captured before ``bind_execution_id``."""
    _CURRENT_EXECUTION_ID.reset(token)
