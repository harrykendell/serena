from serena.chatgpt_policy import CHATGPT_PRODUCT_PROMPT, CHATGPT_TOOL_DESCRIPTION_OVERRIDES
from serena.prompt_factory import SerenaPromptFactory
from serena.tools import MCP_TOOL_CLASSES


def test_chatgpt_policy_preserves_project_concurrency_and_job_guidance() -> None:
    assert "activate the intended project" in CHATGPT_PRODUCT_PROMPT
    assert "serializes project mutations" in CHATGPT_PRODUCT_PROMPT
    assert "different project runtimes remain independent" in CHATGPT_PRODUCT_PROMPT
    assert "read_tool_output" in CHATGPT_PRODUCT_PROMPT
    assert "start_job" in CHATGPT_PRODUCT_PROMPT
    assert "job_status(wait_for=...)" in CHATGPT_PRODUCT_PROMPT


def test_chatgpt_policy_preserves_structural_editing_guidance() -> None:
    assert "replace a complete named symbol -> `replace_symbol_body`" in CHATGPT_PRODUCT_PROMPT
    assert "make a small edit inside a symbol or file-level text -> `replace_content`" in CHATGPT_PRODUCT_PROMPT
    assert "inspect references first" in CHATGPT_PRODUCT_PROMPT
    assert "prefer `replace_in_files`" in CHATGPT_PRODUCT_PROMPT


def test_chatgpt_tool_descriptions_steer_code_navigation_to_semantic_tools() -> None:
    assert "use this rather than regex/file reads" in CHATGPT_TOOL_DESCRIPTION_OVERRIDES["find_symbol"]
    assert "prefer get_symbols_overview/find_symbol" in CHATGPT_TOOL_DESCRIPTION_OVERRIDES["read_file"]
    assert (
        "discovery when a target code symbol cannot yet be identified semantically"
        in CHATGPT_TOOL_DESCRIPTION_OVERRIDES["search_for_pattern"]
    )
    assert "continue with symbolic retrieval" in CHATGPT_TOOL_DESCRIPTION_OVERRIDES["search_for_pattern"]
    assert "Do not use it to replace an entire named function" in CHATGPT_TOOL_DESCRIPTION_OVERRIDES["replace_content"]


def test_fixed_chatgpt_registry_contains_runtime_product_tools() -> None:
    tool_names = {tool_class.get_name_from_cls() for tool_class in MCP_TOOL_CLASSES}
    assert {
        "find_symbol",
        "get_symbols_overview",
        "replace_symbol_body",
        "replace_content",
        "read_tool_output",
        "start_job",
        "job_status",
        "cancel_job",
        "fetch_media_file",
        "render_pdf_page",
        "download_file",
        "upload_file",
        "git_status",
        "git_commit",
        "activate_project",
        "initial_instructions",
    } <= tool_names


def test_system_prompt_includes_fixed_semantic_tool_policy() -> None:
    prompt = SerenaPromptFactory().create_system_prompt(
        chatgpt_product_prompt=CHATGPT_PRODUCT_PROMPT,
        global_memories_list="",
    )

    assert "named symbol or body -> `find_symbol`" in prompt
    assert "declaration from a usage -> `find_declaration`" in prompt
    assert "implementations or overrides -> `find_implementations`" in prompt
    assert "references or callers -> `find_referencing_symbols`" in prompt
    assert "discovery when a target code symbol cannot yet be identified semantically -> `search_for_pattern`" in prompt
    assert "serializes project mutations" in prompt
    assert "job_status(wait_for=...)" in prompt
