# Serena approval notifications

Serena-owned Linux feature for forwarding ChatGPT confirmation prompts to Serena's existing Web Push notification path.

The feature is passive with respect to ChatGPT. It does not poll, fetch conversations, subscribe to extra topics, or create any ChatGPT requests. It only inspects conversation objects that ChatGPT Desktop has already received through its normal catalog/update flow.

A notification is forwarded only when the current conversation node contains:

```text
message.metadata.jit_plugin_data.from_server.type === "confirm_action"
```

The notification title and description come directly from `tool_call_safety_summary.title` and `tool_call_safety_summary.description`. The conversation ID and current message ID are used for deduplication.

The only request created by this feature is the local handoff to Serena:

```text
POST http://127.0.0.1:24282/api/chatgpt-approval
```

Serena then sends the event through its existing Web Push subscriptions. No ChatGPT endpoint is called by this feature.

The `codex-desktop/install.sh` entry point stages this feature into a generated Linux-feature overlay alongside upstream `tray-usage`, and invokes an unmodified `codex-desktop-linux` checkout with both enabled. The native package's update-builder therefore receives concrete copies of both feature directories.
