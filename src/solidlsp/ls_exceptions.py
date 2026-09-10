"""
This module contains the exceptions raised by the framework.
"""

from solidlsp.ls_config import LanguageServerId


class SolidLSPException(Exception):
    """Represents a language-server operation failure with optional structured cause."""

    def __init__(self, message: str, cause: Exception | None = None) -> None:
        """Initializes the exception with a diagnostic message and optional original cause."""
        self.cause = cause
        super().__init__(message)

    def is_language_server_terminated(self) -> bool:
        """Returns whether the failure was caused by language-server termination."""
        from .ls_process import LanguageServerTerminatedException

        return isinstance(self.cause, LanguageServerTerminatedException)

    def get_affected_language(self) -> LanguageServerId | None:
        """Returns the affected language when the server terminated, otherwise ``None``."""
        from .ls_process import LanguageServerTerminatedException

        if isinstance(self.cause, LanguageServerTerminatedException):
            return self.cause.ls_id
        return None

    def user_message(self) -> str:
        """Returns the concise operational message suitable for a tool caller."""
        if self.cause is not None and not self.is_language_server_terminated():
            cause_message = str(self.cause).strip()
            if cause_message:
                return cause_message
        return super().__str__().strip()

    def __str__(self) -> str:
        """Returns the diagnostic representation including its cause when present."""
        message = super().__str__()
        if self.cause:
            separator = "\n" if "\n" in message else " "
            message += f"{separator}(caused by {self.cause})"
        return message


class LanguageServerOperationError(SolidLSPException):
    """Indicates an expected language-server operation failure safe to report to a tool caller."""


class LanguageServerUnavailableError(LanguageServerOperationError):
    """Indicates that a language server cannot start because its runtime is unavailable."""


class InvalidTextLocationError(SolidLSPException):
    """
    Raised when a symbol's LSP range refers to a text location that does not exist
    in the file's current line buffer, other than the well-defined whole-line-through-EOF
    convention (end line exactly one past EOF at column 0), which is corrected rather
    than rejected.
    """
