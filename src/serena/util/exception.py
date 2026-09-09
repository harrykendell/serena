import sys

from serena.agent import log


def show_fatal_exception_safe(e: Exception) -> None:
    """Logs and prints a fatal exception without invoking a desktop UI."""
    log.error(f"Fatal exception: {e}", exc_info=e)
    print(f"Fatal exception: {e}", file=sys.stderr)
