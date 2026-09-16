"use strict";

const assert = require("node:assert/strict");
const test = require("node:test");
const vm = require("node:vm");

const {
  applySerenaApprovalCatalogObserver,
  buildCatalogObserver,
} = require("./patch.js");

function catalogSourceFixture() {
  return "function M5(e,t){let n=t.update_time==null?NaN:Date.parse(t.update_time)/1e3;if(t.is_temporary_chat===!0||!Number.isFinite(n))return{id:t.id,updatedAt:n,entry:null};let r=t.create_time==null?NaN:Date.parse(t.create_time)/1e3;return{id:t.id,updatedAt:n,entry:{hostId:e,threadId:t.id,displayTitle:t.title?.trim()||t.id,sourceCreatedAt:Number.isFinite(r)?r:n,sourceUpdatedAt:n}}}";
}

function confirmationItem() {
  return {
    id: "chat-background",
    title: "Background chat",
    create_time: "2026-09-16T12:00:00.000Z",
    update_time: "2026-09-16T12:01:00.000Z",
    current_node: "approval-message",
    mapping: {
      "approval-message": {
        message: {
          id: "approval-message",
          metadata: {
            jit_plugin_data: {
              from_server: {
                type: "confirm_action",
                body: {
                  connector_id: "serena",
                  connector_name: "Serena",
                  tool_call_safety_summary: {
                    title: "Allow file materialization?",
                    description: "ChatGPT needs your approval to materialize 1 file attachments returned by Serena.",
                  },
                },
              },
            },
          },
        },
      },
    },
  };
}

test("catalog observer forwards current confirmation prompts already present in native responses", async () => {
  const posts = [];
  const source = catalogSourceFixture();
  const patched = applySerenaApprovalCatalogObserver(source);

  assert.notEqual(patched, source);
  assert.equal(applySerenaApprovalCatalogObserver(patched), patched);

  const item = confirmationItem();
  const context = {
    fetch: async (url, options) => {
      posts.push([url, options]);
      return { ok: true };
    },
    Date,
    JSON,
    Math,
    Number,
    Object,
    Set,
    String,
  };
  vm.runInNewContext(`${patched};result=M5('chatgpt:account:user',item);M5('chatgpt:account:user',item);`, { ...context, item });
  await new Promise(resolve => setTimeout(resolve, 10));

  assert.equal(posts.length, 1);
  assert.equal(posts[0][0], "http://127.0.0.1:24282/api/chatgpt-approval");
  assert.deepEqual(JSON.parse(posts[0][1].body), {
    conversation_id: "chat-background",
    title: "Allow file materialization?",
    description: "ChatGPT needs your approval to materialize 1 file attachments returned by Serena.",
    message_id: "approval-message",
    connector_id: "serena",
    connector_name: "Serena",
  });
});

test("observer contains no ChatGPT polling or conversation fetches", () => {
  const observer = buildCatalogObserver();
  assert.doesNotMatch(observer, /safeGet|safePost|\/conversation\/|\/conversations\?|setInterval|setTimeout/);
});
