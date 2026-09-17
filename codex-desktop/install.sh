#!/usr/bin/env bash
set -Eeuo pipefail

CODEX_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
UPSTREAM_DIR="$CODEX_ROOT/upstream"
OVERLAY_ROOT="$CODEX_ROOT/overlay"
UPSTREAM_URL="https://github.com/ilysenko/codex-desktop-linux.git"

if [ ! -d "$UPSTREAM_DIR/.git" ]; then
    git clone "$UPSTREAM_URL" "$UPSTREAM_DIR"
fi

for required in \
    "$UPSTREAM_DIR/Makefile" \
    "$UPSTREAM_DIR/linux-features/features.example.json" \
    "$UPSTREAM_DIR/linux-features/compatibility.json" \
    "$UPSTREAM_DIR/linux-features/tray-usage/feature.json" \
    "$CODEX_ROOT/features/chat-effort-diagnostics/feature.json"
do
    if [ ! -e "$required" ]; then
        printf 'Missing required bootstrap input: %s\n' "$required" >&2
        exit 1
    fi
done

rm -rf "$OVERLAY_ROOT"
mkdir -p "$OVERLAY_ROOT"
cp "$UPSTREAM_DIR/linux-features/features.example.json" "$OVERLAY_ROOT/features.example.json"
cp "$UPSTREAM_DIR/linux-features/compatibility.json" "$OVERLAY_ROOT/compatibility.json"
cp -a "$UPSTREAM_DIR/linux-features/tray-usage" "$OVERLAY_ROOT/tray-usage"
cp -a "$CODEX_ROOT/features/chat-effort-diagnostics" "$OVERLAY_ROOT/chat-effort-diagnostics"
cat >"$OVERLAY_ROOT/features.json" <<'EOF'
{
  "enabled": [
    "tray-usage",
    "chat-effort-diagnostics"
  ]
}
EOF

printf '[serena] Codex Desktop checkout: %s\n' "$UPSTREAM_DIR"
printf '[serena] Linux feature overlay: %s\n' "$OVERLAY_ROOT"
printf '[serena] Enabled features: tray-usage, chat-effort-diagnostics\n'

if [ "${SERENA_CODEX_BOOTSTRAP_PREPARE_ONLY:-0}" = "1" ]; then
    exit 0
fi

CODEX_LINUX_FEATURES_ROOT="$OVERLAY_ROOT" \
CODEX_LINUX_FEATURES_CONFIG="$OVERLAY_ROOT/features.json" \
make -C "$UPSTREAM_DIR" bootstrap-native
