from enum import Enum


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
