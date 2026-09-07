import pytest

from interprompt.jinja_template import JinjaTemplate
from serena.config.context_mode import SerenaAgentContext
from serena.prompt_factory import SerenaPromptFactory

GROK_EXCLUDED_TOOLS = {
    "create_text_file",
    "read_file",
    "execute_shell_command",
    "find_file",
    "list_dir",
    "search_for_pattern",
}

BUILTIN_RUNTIME_CONTEXT_NAMES = [
    name for name in SerenaAgentContext.list_registered_context_names(include_user_contexts=False) if name != "context.template"
]


def _render_context_prompt(context: SerenaAgentContext) -> str:
    """Render with the same template variables SerenaAgent._format_prompt provides.

    Note: context YAMLs (e.g. grok.yml) are currently static (no Jinja), but the variables
    match exactly what _format_prompt supplies. The outer system prompt template uses them.
    """
    # Provide non-empty values so any future Jinja in a context prompt would still be exercised.
    return JinjaTemplate(context.prompt).render(
        available_tools=["find_symbol", "get_symbols_overview", "replace_content"],
        available_markers=["ToolMarkerSymbolicRead"],
        tool_names={"find_symbol": "find_symbol", "get_symbols_overview": "get_symbols_overview"},
    )


def test_grok_context_loads():
    context = SerenaAgentContext.from_name("grok")

    assert context.name == "grok"
    assert context.single_project is True
    assert context.structured_tool_output is None
    assert set(context.excluded_tools) == GROK_EXCLUDED_TOOLS


def test_grok_context_prompt_renders():
    context = SerenaAgentContext.from_name("grok")

    rendered_prompt = _render_context_prompt(context)

    assert rendered_prompt.strip()
    assert "Serena's code intelligence tools" in rendered_prompt


def test_chatgpt_context_explains_session_project_concurrency() -> None:
    context = SerenaAgentContext.from_name("chatgpt")

    rendered_prompt = _render_context_prompt(context)

    assert "Always activate the intended project" in rendered_prompt
    assert "Multiple conversations may read the same project" in rendered_prompt
    assert "writes to one project are serialized" in rendered_prompt
    assert "writes to different projects may proceed concurrently" in rendered_prompt


def test_chatgpt_context_includes_fork_optional_tools() -> None:
    context = SerenaAgentContext.from_name("chatgpt")

    assert {
        "start_job",
        "job_status",
        "read_tool_output",
        "cancel_job",
        "git_status",
        "git_fetch",
        "git_log",
        "git_diff",
        "git_branch",
        "git_commit",
        "git_pull",
        "git_push",
        "fetch_media_file",
        "render_pdf_page",
        "download_file",
        "upload_file",
    } <= set(context.included_optional_tools)


def test_chatgpt_context_steers_code_navigation_to_semantic_tools() -> None:
    context = SerenaAgentContext.from_name("chatgpt")

    assert "use this rather than regex/file reads" in context.tool_description_overrides["find_symbol"]
    assert (
        "discovery when a target code symbol cannot yet be identified semantically"
        in context.tool_description_overrides["search_for_pattern"]
    )
    assert "continue with symbolic retrieval" in context.tool_description_overrides["search_for_pattern"]
    assert "prefer get_symbols_overview/find_symbol" in context.tool_description_overrides["read_file"]


def test_system_prompt_maps_source_navigation_to_semantic_tools() -> None:
    prompt = SerenaPromptFactory().create_system_prompt(
        context_system_prompt="",
        mode_system_prompts=[],
        available_tools=[
            "find_symbol",
            "get_symbols_overview",
            "find_referencing_symbols",
            "find_declaration",
            "find_implementations",
            "search_for_pattern",
            "read_file",
        ],
        available_markers=["ToolMarkerSymbolicRead"],
        global_memories_list="",
        tool_names={
            "find_symbol": "find_symbol",
            "get_symbols_overview": "get_symbols_overview",
            "find_referencing_symbols": "find_referencing_symbols",
        },
    )

    assert "named symbol or body -> `find_symbol`" in prompt
    assert "declaration from a usage -> `find_declaration`" in prompt
    assert "implementations or overrides -> `find_implementations`" in prompt
    assert "references or callers -> `find_referencing_symbols`" in prompt
    assert "symbol discovery when semantic lookup cannot identify the target -> `search_for_pattern`" in prompt
    assert 'search_for_pattern("def __init__\\\\(", context_lines_after=70' in prompt


@pytest.mark.parametrize("context_name", BUILTIN_RUNTIME_CONTEXT_NAMES)
def test_builtin_contexts_load_and_prompt_templates_render(context_name: str):
    context = SerenaAgentContext.from_name(context_name)

    rendered_prompt = _render_context_prompt(context)

    assert context.name == context_name
    assert isinstance(context.excluded_tools, list)
    assert rendered_prompt == "" or rendered_prompt.strip()
