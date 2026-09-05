"""Discoverable routing and hand-off guidance for the Orchestrator MCP."""

from mcp.server.fastmcp import FastMCP

DELEGATION_GUIDE_RESOURCE_URI = "orchestrator://delegation-guide"

_DELEGATION_GUIDE = """# Orchestrator delegation guide

Orchestrator currently manages bounded delegates for human-run ChatGPT conversations only. Codex execution and automatic provider fallback are disabled. Serena remains the coding/project/jobs MCP; ChatGPT is the bridge when a delegated worker needs Serena.

## Route selection

| Work | Preferred route |
| --- | --- |
| Small lookup, edit, or interactive repository work in the current checkout | Parent ChatGPT + Serena |
| Broad read-only exploration, architecture comparison, or diagnosis | Human-run ChatGPT delegate |
| Several independent read-only investigations | `delegate_batch`, normally with two human-run ChatGPT workers |
| Substantial implementation | Human-run ChatGPT delegate + Serena |

Use `provider_policy=chat`. Orchestrator returns a fresh-chat claim prompt; copy it into an independent ChatGPT conversation and run that worker manually. `provider_policy=codex` and `provider_policy=auto` are currently rejected.

## Parent -> ChatGPT worker -> Serena -> Orchestrator

1. The parent calls `create_delegate` with a bounded goal, scope, acceptance criteria, verification, and `provider_policy=chat`.
2. Use the returned fresh-chat claim prompt in an independent ChatGPT conversation. Orchestrator does not create ordinary ChatGPT conversations itself.
3. The worker calls `claim_delegate(delegate_id)` and works only from the returned bounded task packet.
4. If repository work is required, the worker activates the named project in Serena, gathers any missing repository context independently, edits the live checkout under Serena's write serialization, and runs the requested checks.
5. The worker calls `complete_delegate(delegate_id, result)` with the bounded typed result.
6. The parent calls `collect_delegate(delegate_id)` and integrates or reviews the result.

## Fan-out

Use `delegate_batch` only for independent read-only `explore`, `review`, or `research` tasks. Refresh the batch as workers finish; Orchestrator promotes pending tasks under the configured concurrency cap and returns a compact deterministic aggregate rather than concatenating worker transcripts.

## Boundaries

Serena owns project routing, semantic coding, files/media, Git, shell execution, jobs, memories, language servers, and direct mutation of the live project checkout. Orchestrator owns human-run ChatGPT delegate state, claims, fan-out/fan-in, and bounded results. Codex provider execution, automatic fallback, and unattended provider lifecycle are currently disabled.
"""


def register_delegation_guide_resource(mcp: FastMCP) -> None:
    """Registers the static Orchestrator routing and hand-off guide."""

    @mcp.resource(
        DELEGATION_GUIDE_RESOURCE_URI,
        name="Orchestrator delegation guide",
        description="Routing and human-run ChatGPT hand-off guidance for Orchestrator delegates.",
        mime_type="text/markdown",
    )
    def delegation_guide_resource() -> str:
        return _DELEGATION_GUIDE
