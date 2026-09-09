"""Shared Serena error types for model-facing tool failures."""

_MAX_USER_FACING_ERROR_CHARS = 4000


class UserFacingError(Exception):
    """Represents one expected failure that is safe to present directly to the model."""

    def __init__(self, message: str):
        text = message.strip()
        if len(text) > _MAX_USER_FACING_ERROR_CHARS:
            text = f"{text[: _MAX_USER_FACING_ERROR_CHARS - 3]}..."
        super().__init__(text)
