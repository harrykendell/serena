"""Session identity helpers for MCP requests."""

from mcp.server.fastmcp import Context


def get_mcp_session_id(context: Context | None) -> str:
    """Returns the host conversation identifier, falling back to the MCP session object."""
    if context is None:
        return "global"

    try:
        meta = context.request_context.meta
        if meta is not None and meta.model_extra is not None:
            openai_session = meta.model_extra.get("openai/session")
            if isinstance(openai_session, str) and openai_session:
                return openai_session
    except (AttributeError, ValueError):
        pass

    try:
        return f"{id(context.session):x}"
    except (AttributeError, ValueError):
        return "global"
