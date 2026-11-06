"use strict";
const $ = id => document.getElementById(id);
let state, csrf = sessionStorage.getItem("csrf") || "", client = sessionStorage.getItem("client") || "";
let generation = null, socket = null, canControl = false, replaced = false, activePrompt = null;
let lastLogSeq = -1, lastSummary = "", pending = false;
const error = message => { $("error").textContent = message || ""; };
const text = (tag, value, className) => { const el=document.createElement(tag); el.textContent=value ?? ""; if(className)el.className=className; return el; };
function rows(parent, values) { if(!values)return; const dl=document.createElement("dl"); for(const [key,value] of values){dl.append(text("dt",key),text("dd",value));} parent.append(dl); }
function availability() {
  document.querySelectorAll("[data-kind]").forEach(el => {el.disabled=!canControl || pending || !!state?.operation_id || state?.control.exit_requested;});
  $("cancel").disabled=!canControl || pending || !state?.operation_id || state?.control.cancelled;
  $("stop").disabled=!canControl || pending || state?.control.kind!=="learn" || state?.control.stop_after_current;
  $("exit").disabled=!canControl || pending || state?.control.exit_requested;
  $("prompt").querySelectorAll("button,textarea").forEach(el=>{el.disabled=!canControl || pending;});
}
async function send(path, payload) {
  if (!canControl || pending) return;
  const body=JSON.stringify({...payload,request_id:crypto.randomUUID()});
  pending=true; availability(); error("");
  try {
    let response;
    // Retransmission always preserves the exact ID and payload.
    for(let attempt=0;attempt<2;attempt++){
      try{response=await fetch(path,{method:"POST",headers:{"Content-Type":"application/json","X-CSRF-Token":csrf,"X-Control-Client":client,"X-Connection-ID":generation},body});break;}
      catch(e){if(attempt)throw e; await new Promise(r=>setTimeout(r,400));}
    }
    const data=await response.json();
    if(!response.ok)throw new Error(data.error||`请求失败 (${response.status})`);
  } catch(e) {error(e.message||"连接中断，请等待重连后查看操作状态");}
  finally{pending=false;availability();}
}
function answer(prompt, value) {return send(`/api/prompts/${encodeURIComponent(prompt.id)}/answer`,{operation_id:prompt.operation_id,version:prompt.version,value});}
function renderPrompt(prompt) {
  const host=$("prompt");
  if(!prompt){host.hidden=true;host.replaceChildren();activePrompt=null;return;}
  host.hidden=false;if(activePrompt===prompt.id)return;
  activePrompt=prompt.id;host.replaceChildren(text("h2",prompt.title),text("p",prompt.message));rows(host,prompt.rows);
  const actions=text("div","","prompt-actions");
  const button=(label,cb,primary=false)=>{const b=text("button",label,primary?"primary":"");b.type="button";b.addEventListener("click",cb);actions.append(b);};
  if(prompt.kind==="multiline") {const input=document.createElement("textarea");input.id="link-input";input.setAttribute("aria-label",prompt.title);host.append(input);button("继续",()=>answer(prompt,input.value),true);}
  if(prompt.kind==="choice")prompt.options.forEach((label,index)=>button(label,()=>answer(prompt,index+1)));
  if(prompt.kind==="confirm"){button("确认",()=>answer(prompt,true),prompt.default);button("不继续",()=>answer(prompt,false),!prompt.default);}
  if(prompt.kind==="pause")button("返回总览",()=>answer(prompt,null),true);
  if(prompt.kind!=="pause")button("取消本轮",()=>send("/api/control/cancel",{operation_id:prompt.operation_id}));host.append(actions);
}
function render(snapshot) {
  state=snapshot;const d=state.dashboard || {};
  $("account").textContent=d.account || "未登录";
  $("credential").textContent=d.credential?.status==="valid"?`凭证有效 · 到期 ${d.credential.expires_on||"未知"} · 剩余 ${d.credential.days_left||"—"} 天`:d.credential?.status==="expired"?"凭证已过期，请更新登录":"请先在账号与登录中完成登录";
  $("recommended").textContent=d.recommended||"账号与登录";
  for(const key of ["learning_count","learning_failure_count","exam_count","manual_exam_count"])$(key).textContent=d[key]??"—";
  $("operation-title").textContent=state.operation_id?state.title:"运行面板";
  $("phase").textContent=state.control.exit_requested?"正在退出":state.control.cancelled?"正在取消":state.prompt?"等待确认":state.operation_id?"运行中":"空闲";
  $("status").textContent=state.status;$("progress-area").hidden=!state.progress;
  if(state.progress){const p=state.progress;$("progress-label").textContent=`${p.description} · ${Math.floor(p.completed)} / ${p.total} 秒`;$("progress").max=p.total||1;$("progress").value=p.completed;}
  renderPrompt(state.prompt);const signature=JSON.stringify(state.summary);
  if(signature!==lastSummary){lastSummary=signature;$("summary").replaceChildren();$("summary").hidden=!state.summary;if(state.summary){$("summary").append(text("h2",state.summary.title));rows($("summary"),state.summary.rows);}}
  const newest=state.logs.at(-1)?.seq || 0;
  if(newest!==lastLogSeq){lastLogSeq=newest;const logs=$("logs");const pinned=logs.scrollHeight-logs.scrollTop-logs.clientHeight<45;logs.replaceChildren(...state.logs.map(log=>text("div",log.message,log.level)));if(pinned)logs.scrollTop=logs.scrollHeight;}
  $("log-meta").textContent=state.dropped_logs?`最近 400 条 · 较早 ${state.dropped_logs} 条见本地日志`:"显示最近 400 条";availability();
}
function connect() {
  if(replaced)return;socket=new WebSocket(location.origin.replace("http:","ws:")+"/api/events");
  socket.onopen=()=>socket.send(JSON.stringify({client}));
  socket.onmessage=event=>{const data=JSON.parse(event.data);generation=data.generation;canControl=!!data.controller;$("connection").textContent=canControl?"已连接 · 控制窗口":"已连接 · 只读";render(data);};
  socket.onclose=event=>{canControl=false;generation=null;replaced=event.code===4001;$("connection").textContent=state?.control.exit_requested?"程序已退出":replaced?"控制已转到重连窗口":"连接中断 · 正在重连";availability();if(!replaced&&!state?.control.exit_requested)setTimeout(connect,1500);};
  socket.onerror=()=>socket.close();
}
async function start() {
  availability();try {
    const boot=new URLSearchParams(location.hash.slice(1)).get("bootstrap");history.replaceState(null,"",location.pathname);
    if(boot){const r=await fetch("/api/session",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({bootstrap:boot})});if(!r.ok)throw new Error("启动凭据已使用或失效，请重新启动入口");const s=await r.json();client=s.client;csrf=s.csrf;sessionStorage.setItem("client",client);sessionStorage.setItem("csrf",csrf);}
    const response=await fetch("/api/state");if(!response.ok)throw new Error("请从项目启动脚本打开管理窗口");const initial=await response.json();csrf=initial.csrf;render(initial);
    // A duplicated tab can inherit sessionStorage. The origin-wide tab lock
    // keeps it read-only while refresh naturally releases and reacquires it.
    if(client && navigator.locks){await navigator.locks.request("course-control-tab",{ifAvailable:true},async lock=>{if(!lock)client="";connect();if(lock)await new Promise(()=>{});});}else{client="";connect();}
  }catch(e){error(e.message);$("connection").textContent="无法连接";}
}
document.querySelectorAll("[data-kind]").forEach(el=>el.addEventListener("click",()=>send("/api/operations",{kind:el.dataset.kind})));
$("cancel").onclick=()=>send("/api/control/cancel",{operation_id:state?.operation_id});$("stop").onclick=()=>send("/api/control/stop-after-current",{operation_id:state?.operation_id});$("exit").onclick=()=>send("/api/control/exit",{});start();
