"use strict";

const catalogMarker = "/*serena-approval-confirmation-forwarder*/";
const endpoint = "http://127.0.0.1:24282/api/chatgpt-approval";

function buildCatalogObserver() {
  return `${catalogMarker}(function(){
const seen=new Set;
function currentConfirmation(item){
  const message=item?.mapping?.[item?.current_node]?.message;
  const fromServer=message?.metadata?.jit_plugin_data?.from_server;
  if(fromServer?.type!==\`confirm_action\`)return null;
  const body=fromServer.body??{},summary=body.tool_call_safety_summary??{};
  const messageId=typeof message?.id===\`string\`?message.id:item?.current_node;
  if(typeof messageId!==\`string\`||messageId.length===0)return null;
  return {
    messageId,
    title:typeof summary.title===\`string\`&&summary.title.length>0?summary.title:\`Approval required\`,
    description:typeof summary.description===\`string\`&&summary.description.length>0?summary.description:\`ChatGPT needs your approval.\`,
    connectorId:typeof body.connector_id===\`string\`&&body.connector_id.length>0?body.connector_id:\`chatgpt\`,
    connectorName:typeof body.connector_name===\`string\`&&body.connector_name.length>0?body.connector_name:null
  }
}
globalThis.__serenaApprovalCatalogInspect=item=>{
  try{
    const conversationId=item?.id;if(typeof conversationId!==\`string\`||conversationId.length===0)return;
    const confirmation=currentConfirmation(item);if(confirmation==null)return;
    const key=conversationId+\`|\`+confirmation.messageId;if(seen.has(key))return;seen.add(key);
    const payload={conversation_id:conversationId,title:confirmation.title,description:confirmation.description,message_id:confirmation.messageId,connector_id:confirmation.connectorId,connector_name:confirmation.connectorName};
    void fetch(${JSON.stringify(endpoint)},{method:\`POST\`,headers:{\"Content-Type\":\`text/plain;charset=UTF-8\`},body:JSON.stringify(payload)}).then(response=>{if(!response.ok)seen.delete(key)}).catch(()=>{seen.delete(key)})
  }catch{}
}
})();`;
}

function applySerenaApprovalCatalogObserver(source) {
  if (source.includes(catalogMarker)) return source;

  const matches = [...source.matchAll(/function ([A-Za-z_$][\w$]*)\(([A-Za-z_$][\w$]*),([A-Za-z_$][\w$]*)\)\{let ([A-Za-z_$][\w$]*)=\3\.update_time==null\?NaN:Date\.parse\(\3\.update_time\)\/1e3;if\(\3\.is_temporary_chat===!0\|\|!Number\.isFinite\(\4\)\)/gu)];
  if (matches.length !== 1) return mainDrift(source);

  const [needle, fn, host, item, updatedAt] = matches[0];
  const replacement = `function ${fn}(${host},${item}){try{globalThis.__serenaApprovalCatalogInspect?.(${item})}catch{}let ${updatedAt}=${item}.update_time==null?NaN:Date.parse(${item}.update_time)/1e3;if(${item}.is_temporary_chat===!0||!Number.isFinite(${updatedAt}))`;
  return buildCatalogObserver() + source.replace(needle, replacement);
}

function mainDrift(source) {
  console.warn("[serena-approval-notifications] ChatGPT catalog contract changed or is ambiguous. Disable serena-approval-notifications and rebuild, or update its patch for the current official package.");
  return source;
}

module.exports = {
  applySerenaApprovalCatalogObserver,
  buildCatalogObserver,
  descriptors: [
    {
      id: "serena-approval-confirmation-forwarder",
      phase: "main-bundle",
      order: 22005,
      ciPolicy: "optional",
      apply: applySerenaApprovalCatalogObserver,
    },
  ],
};
