# Chat effort diagnostics

Temporary Serena-owned diagnostic feature for the signed ChatGPT Desktop renderer.

When enabled it disables the composer-local `InstantOnly` enforcement that otherwise makes Chat snap back to Instant. The patch matches the semantic guard rather than specific minified symbol names, so routine upstream minifier renames do not invalidate the feature.

Earlier ChatGPT Desktop builds also normalised the selected model through a conversation-level `reasoningBlocked` input. Current signed builds no longer expose that normalization path, so the feature no longer patches or requires it.

No workspace-wide policy resolver is patched, the existing model-change handler is left intact, and no runtime tracing state is injected. The renderer patch fails closed unless exactly one InstantOnly composer guard is present. It does not alter Codex mode, bypass model-specific limits, or construct custom ChatGPT API requests.
