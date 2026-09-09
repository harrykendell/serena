from typing import Any

from jinja2.sandbox import SandboxedEnvironment


class SerenaPromptFactory:
    """Creates Serena's fixed ChatGPT prompts and renders trusted local templates."""

    _CONNECTION_PROMPT = (
        "CRITICAL: Before starting to work on a coding task, call the `initial_instructions` tool to read the 'Serena Instructions Manual'."
    )
    _SYSTEM_FOOTER = "You have hereby read the 'Serena Instructions Manual' and do not need to read it again."
    _ONBOARDING_PROMPT = """You are viewing the project for the first time.
Your task is to assemble durable, non-obvious information about the project and write it
to memory files that future agents will consult.
The project is being developed on the system: {{ system }}.

**Before writing anything, read `mem:{{ memory_maintenance_name }}`** using the `read_memory` tool.
It defines the style, naming, reference, and add/update threshold conventions that every
memory in this project must follow. Do not skip this step.

Target memory layout — use the `write_memory` tool, one call per memory:

* `mem:core` — top-level source map and project-wide invariants that don't belong in a
  focused memory.
* `mem:tech_stack` — language(s), framework(s), build tools, package manager, version
  pins where they matter.
* `mem:suggested_commands` — project commands the user will actually run (dev, test, lint,
  format, run entrypoints) and any system util commands (`git`, `ls`, `grep`, …) whose
  form differs on {{ system }} from a standard unix shell. Skip generic commands that
  behave identically across systems.
* `mem:conventions` — code style, naming, type hints, docstring conventions, design
  patterns specific to this codebase.
* `mem:task_completion` — exact commands to run when a coding task is considered done
  (linter, formatter, test runner, type checker, etc.).

If the project has clearly distinct modules (e.g. frontend/backend), create per-module
`mem:<module>/core` memories with module-specific references to further memories instead
of pushing everything into `mem:core`.

Acquire information using the available tools. Read only the files needed; do not load
entire directory trees. If essential information is missing, ask the user.

When all memories are written, sanity-check the references by mentioning that the user can run
`serena memories check` from the project root.

**Important**: the onboarding is only complete once you have actually called `write_memory`
for each memory above — do not summarize the content in chat and skip writing.
"""

    def __init__(self) -> None:
        self._environment = SandboxedEnvironment()

    def render_template(self, template: str, **params: Any) -> str:
        """Renders a trusted Serena-owned Jinja template with live runtime values."""
        return self._environment.from_string(template).render(**params)

    def create_connection_prompt(self) -> str:
        """Returns the fixed connection-time bootstrap instruction."""
        return self._CONNECTION_PROMPT

    def create_system_prompt(self, *, chatgpt_product_prompt: str, global_memories_list: str) -> str:
        """Builds the fixed instruction manual around runtime product-policy content."""
        sections = [chatgpt_product_prompt]
        if global_memories_list:
            sections.append(f"The following global (not project-specific) memories are available to you: {global_memories_list}")
        sections.append(self._SYSTEM_FOOTER)
        return "\n\n".join(sections)

    def create_onboarding_prompt(self, *, memory_maintenance_name: str, system: str) -> str:
        """Builds the onboarding instructions with the current host and memory name."""
        return self.render_template(
            self._ONBOARDING_PROMPT,
            memory_maintenance_name=memory_maintenance_name,
            system=system,
        )
