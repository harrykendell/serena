"use strict";

const assert = require("node:assert/strict");
const test = require("node:test");

const {
  PATCH_MARKER,
  applyChatEffortDiagnostics,
  contract,
} = require("./patch.js");

function fixture({ target = "$e", policy = "ry", origin = "He" } = {}) {
  return `prefix;${target}=Ze===${policy}.InstantOnly&&${origin}!==\`tpp\`&&${origin}!==\`flora\`;suffix`;
}

test("patch disables InstantOnly enforcement across minifier renames", () => {
  for (const source of [
    fixture(),
    fixture({ target: "tt", policy: "Yf", origin: "We" }),
  ]) {
    assert.equal(contract(source), "available");

    const patched = applyChatEffortDiagnostics(source);
    assert.equal(contract(patched), "applied");
    assert.ok(patched.includes(PATCH_MARKER));
    assert.match(patched, /(?:\$e|tt)=!1/u);
    assert.equal(applyChatEffortDiagnostics(patched), patched);
  }
});

test("patch fails closed when the InstantOnly contract is absent or ambiguous", () => {
  const source = fixture();
  for (const candidate of [
    source.replace(".InstantOnly", ".ChangedPolicy"),
    `${source};${fixture({ target: "xx", policy: "Yf", origin: "We" })}`,
  ]) {
    assert.equal(contract(candidate), "drifted");
    assert.equal(applyChatEffortDiagnostics(candidate), candidate);
  }
});
