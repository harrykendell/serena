"""Discoverable routing and hand-off guidance for the Orchestrator MCP."""

from mcp.server.fastmcp import FastMCP

DELEGATION_GUIDE_RESOURCE_URI = "orchestrator://delegation-guide"

_DELEGATION_GUIDE = """# Orchestrator delegation guide

Orchestrator manages delegate lifecycle and provider policy. Serena remains the coding/project/jobs MCP. The services do not call each other or share runtime state; ChatGPT is the bridge when a delegated ChatGPT worker also needs Serena.

## Checkout ownership

Treat checkout ownership as a hard routing invariant:

- **Serena work is live.** A parent or delegated ChatGPT worker that edits through Serena works directly in the active project's live checkout. Serena owns serialization of writes to that checkout.
- **Codex code work is isolated.** Every modifying Codex execution, whether selected directly with `provider_policy=codex` or reached through `auto` fallback, runs in an Orchestrator-created Git worktree and branch. Never instruct Codex to modify Serena's live checkout.
- The worktree is a candidate implementation. Review its Git-observed result and integrate it into the live checkout explicitly; completion never implies merge.

This separation prevents Codex subprocesses from bypassing Serena's write serialization without introducing shared locks or runtime coupling between the two MCPs.

## Route selection

| Work | Preferred route |
| --- | --- |
| Small lookup, edit, or interactive repository work in the current checkout | Parent ChatGPT + Serena (live checkout) |
| Broad read-only exploration, architecture comparison, or log/test diagnosis | ChatGPT delegate |
| Several independent read-only investigations | `delegate_batch`, normally with two ChatGPT-first workers |
| Substantial unattended implementation | Codex delegate (isolated worktree) |
| Iterative edit/test/fix work that should not affect the current checkout until review | Codex delegate (isolated worktree) |
| User explicitly requests Codex | Codex delegate; modifying work still uses an isolated worktree |

Use `provider_policy=chat` when a ChatGPT worker should be claimed manually, `codex` for unattended Codex execution, and `auto` for a ChatGPT-first claim window with Codex fallback when available. Codex-capable work requires an explicit `project_root`; Orchestrator does not resolve Serena projects.

## Parent -> ChatGPT worker -> Serena -> Orchestrator

1. The parent calls `create_delegate` with a bounded goal, scope, acceptance criteria, verification, and `provider_policy=chat` or `auto`.
2. Use the returned fresh-chat claim prompt in an independent ChatGPT conversation. Orchestrator does not create ordinary ChatGPT conversations itself.
3. The worker calls `claim_delegate(delegate_id)` and works only from the returned bounded task packet.
4. If repository work is required, the worker activates the named project in Serena in that worker session, gathers any missing repository context independently, and performs the task directly in that project's live checkout under Serena's write serialization before running the requested checks.
5. The worker calls `complete_delegate(delegate_id, result)` with the bounded typed result. Do not return full transcripts, tool traces, or large logs.
6. The parent calls `collect_delegate(delegate_id)` and integrates or reviews the result.

The worker's Serena session and Orchestrator claim are independent. Activating a Serena project does not claim or complete an Orchestrator delegate, and Orchestrator does not inspect Serena sessions, jobs, locks, or memories.

## Parent -> Codex worktree -> review

1. The parent calls `create_delegate` with `provider_policy=codex`, an explicit `project_root`, and a sufficiently precise task packet. For modifying code, use `kind=code`.
2. Orchestrator allocates an isolated Git worktree and branch for the modifying delegate. Codex must remain in that checkout and should commit completed changes when appropriate.
3. Poll `delegate_status` or use the activity panel while the provider runs; then call `collect_delegate` when terminal.
4. Review the returned `worktree`, `changed_files`, `commit`, `diff_summary`, verification, and caveats. Those review fields are derived from Git-observed worktree state rather than trusted provider prose.
5. Keep integration into the live checkout explicit. Orchestrator does not silently merge the delegate branch into Serena's active project checkout.

## Fan-out

Use `delegate_batch` only for independent read-only `explore`, `review`, or `research` tasks. The first release intentionally rejects modifying `code` fan-out. Refresh the batch as workers finish; Orchestrator promotes pending tasks under the configured concurrency cap and returns a compact deterministic aggregate rather than concatenating worker transcripts.

## Boundaries

Serena owns project routing, semantic coding, files/media, Git, shell execution, jobs, memories, language servers, Serena-side concurrency, and direct mutation of the live project checkout. Orchestrator owns delegate state, ChatGPT claims, Codex provider policy, timeout/fallback, isolated Codex worktrees, fan-out/fan-in, and delegate results. Do not use Orchestrator as a proxy for Serena tools, do not send modifying Codex into Serena's live checkout, and do not expect Serena to manage delegate lifecycle.
"""


def register_delegation_guide_resource(mcp: FastMCP) -> None:
    """Registers the static Orchestrator routing and hand-off guide."""

    @mcp.resource(
        DELEGATION_GUIDE_RESOURCE_URI,
        name="Orchestrator delegation guide",
        description="Routing, provider selection, and ChatGPT/Codex hand-off examples for Orchestrator delegates.",
        mime_type="text/markdown",
    )
    def delegation_guide_resource() -> str:
        return _DELEGATION_GUIDE
