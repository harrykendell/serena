from typing import Optional

from sensai.util import logging

lg = logging


class SuspendedLoggersContext:
    """A context manager that provides an isolated logging environment.

    Temporarily removes all root log handlers upon entry, providing a clean slate
    for defining new log handlers within the context. Upon exit, restores the original
    logging configuration. This is useful when you need to temporarily configure
    an isolated logging setup with well-defined log handlers.

    The context manager:
        - Removes all existing (root) log handlers on entry
        - Allows defining new temporary handlers within the context
        - Restores the original configuration (handlers and root log level) on exit

    Example:
        >>> with SuspendedLoggersContext():
        ...     # No handlers are active here (configure your own and set desired log level)
        ...     pass
        >>> # Original log handlers are restored here

    """

    def __init__(self) -> None:
        self.saved_root_handlers: list = []
        self.saved_root_level: Optional[int] = None

    def __enter__(self) -> "SuspendedLoggersContext":
        root_logger = lg.getLogger()
        self.saved_root_handlers = root_logger.handlers.copy()
        self.saved_root_level = root_logger.level
        root_logger.handlers.clear()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        root_logger = lg.getLogger()
        root_logger.handlers = self.saved_root_handlers
        if self.saved_root_level is not None:
            root_logger.setLevel(self.saved_root_level)
