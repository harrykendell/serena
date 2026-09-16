"use strict";

const JS_ASSET_PATTERN = /\.js$/u;
const PATCH_MARKER = "/*serena-chat-effort-diagnostics*/";

const COMPOSER_POLICY_GUARD_PATTERN =
  /([A-Za-z_$][\w$]*)=([A-Za-z_$][\w$]*)===([A-Za-z_$][\w$]*)\.InstantOnly&&([A-Za-z_$][\w$]*)!==`tpp`&&\4!==`flora`/gu;

function composerPolicyMatches(source) {
  return Array.from(source.matchAll(COMPOSER_POLICY_GUARD_PATTERN));
}

function contract(source) {
  if (source.includes(PATCH_MARKER)) return "applied";
  return composerPolicyMatches(source).length === 1 ? "available" : "drifted";
}

function applyChatEffortDiagnostics(source) {
  if (source.includes(PATCH_MARKER)) return source;
  const matches = composerPolicyMatches(source);
  if (matches.length !== 1) {
    console.warn(
      "[chat-effort-diagnostics] Chat effort-selection contract changed; refusing to patch the current webview asset.",
    );
    return source;
  }

  COMPOSER_POLICY_GUARD_PATTERN.lastIndex = 0;
  return PATCH_MARKER + source.replace(
    COMPOSER_POLICY_GUARD_PATTERN,
    (_match, target) => `${target}=!1`,
  );
}

const descriptors = [
  {
    id: "chat-effort-diagnostics-renderer",
    phase: "webview-asset",
    order: 20_810,
    ciPolicy: "optional",
    pattern: JS_ASSET_PATTERN,
    assetMatch: (source) => contract(source) !== "drifted",
    missingDescription: "ChatGPT Chat composer effort-selection bundle",
    skipDescription: "Chat effort diagnostics renderer patch",
    apply: applyChatEffortDiagnostics,
  },
];

module.exports = {
  COMPOSER_POLICY_GUARD_PATTERN,
  PATCH_MARKER,
  applyChatEffortDiagnostics,
  composerPolicyMatches,
  contract,
  descriptors,
};
