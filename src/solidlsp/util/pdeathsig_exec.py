"""Exec-safe Linux parent-death-signal wrapper for managed subprocesses."""

from __future__ import annotations

import ctypes
import os
import signal
import sys

_PR_SET_PDEATHSIG = 1


def _set_parent_death_signal(expected_parent_pid: int) -> None:
    """Installs ``SIGTERM`` on parent death and closes the setup race."""
    libc = ctypes.CDLL(None, use_errno=True)
    prctl = libc.prctl
    prctl.argtypes = [ctypes.c_int, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong]
    prctl.restype = ctypes.c_int

    # an exec resets caught handlers but preserves ignored signals; enforce the intended disposition
    signal.signal(signal.SIGTERM, signal.SIG_DFL)
    result = prctl(_PR_SET_PDEATHSIG, signal.SIGTERM, 0, 0, 0)
    if result != 0:
        error_number = ctypes.get_errno()
        raise OSError(error_number, os.strerror(error_number))

    # If the parent died between spawning this helper and installing PDEATHSIG, the kernel could
    # not deliver the signal retroactively. Detect that race after registration and terminate now.
    if os.getppid() != expected_parent_pid:
        os.kill(os.getpid(), signal.SIGTERM)
        os._exit(128 + signal.SIGTERM)


def main(argv: list[str] | None = None) -> None:
    """Installs parent-death protection and replaces this process with the requested command."""
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) < 2:
        raise SystemExit("usage: pdeathsig_exec <expected-parent-pid> <command> [args ...]")

    try:
        expected_parent_pid = int(args[0])
    except ValueError as error:
        raise SystemExit(f"invalid expected parent pid: {args[0]!r}") from error

    command = args[1:]
    _set_parent_death_signal(expected_parent_pid)
    os.execvpe(command[0], command, os.environ)


if __name__ == "__main__":
    main()
