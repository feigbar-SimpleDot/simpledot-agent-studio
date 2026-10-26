"""控制台前端: 单文件 HTML(内嵌, 无构建、无外部资源)。

表格视图 + 归类(待处理/进行中/已完成) + 标题搜索 + worker 标签筛选。
blocked 语义: NEED_CLARIFICATION=等回答; BLOCKED=等裁决。
"""

HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="⟪CONSOLE_FALLBACK_HTML_LANG⟫">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
⟪CONSOLE_FALLBACK_TITLE⟫
<style>
:root{--bg:#0d1117;--panel:#161b22;--border:#30363d;--fg:#c9d1d9;--dim:#8b949e;
--green:#3fb950;--red:#f85149;--yellow:#d29922;--blue:#58a6ff;--purple:#bc8cff}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--fg);font:13px/1.5 -apple-system,"PingFang SC",
"Microsoft YaHei",sans-serif;padding:14px}
h1{font-size:17px;margin-bottom:10px}
.tabs{display:flex;gap:8px;margin-bottom:12px}
.tab{padding:6px 14px;border:1px solid var(--border);border-radius:6px;cursor:pointer;
background:var(--panel);color:var(--dim)}
.tab.on{color:#fff;background:#1f6feb33;border-color:var(--blue)}
.bar{display:flex;gap:10px;align-items:center;margin-bottom:10px;flex-wrap:wrap}
input,select{background:#0d1117;border:1px solid var(--border);color:var(--fg);
border-radius:6px;padding:6px 8px;font:13px monospace}
input#q{width:260px}
button{background:#21262d;border:1px solid var(--border);color:var(--fg);
border-radius:6px;padding:6px 12px;cursor:pointer}
button:hover{border-color:var(--blue)}
button.danger:hover{border-color:var(--red);color:var(--red)}
.dot{width:9px;height:9px;border-radius:50%;display:inline-block;margin-right:4px}
.dot.ok{background:var(--green)}.dot.dead{background:var(--red)}
.muted{color:var(--dim);font-size:12px}
.grp{margin:14px 0 6px;font-size:14px;font-weight:600;display:flex;align-items:center;gap:8px}
table{width:100%;border-collapse:collapse;background:var(--panel);
border:1px solid var(--border);border-radius:8px;overflow:hidden}
th{background:#1c2128;color:var(--dim);font-weight:500;font-size:12px;text-align:left;
padding:7px 10px;border-bottom:1px solid var(--border);white-space:nowrap}
td{padding:7px 10px;border-bottom:1px solid #21262d;vertical-align:top;max-width:340px}
tr:last-child td{border-bottom:none}
tr:hover td{background:#1c212866}
.badge{display:inline-block;padding:1px 8px;border-radius:10px;font-size:12px;font-weight:600;white-space:nowrap}
.done{background:#23863633;color:var(--green)}
.running{background:#1f6feb33;color:var(--blue)}
.wait-answer{background:#9e6a0333;color:var(--yellow)}
.wait-judge{background:#da363333;color:var(--red)}
.dim{background:#6e768133;color:var(--dim)}
.tag{display:inline-block;padding:1px 9px;border-radius:10px;font-size:12px;font-weight:600;
background:#8957e533;color:var(--purple);border:1px solid #8957e555}
.mono{font-family:monospace;font-size:12px;color:var(--dim);word-break:break-all}
.title{font-weight:500}
.detail{font-size:11px;color:var(--dim);margin-top:2px;max-height:44px;overflow:auto;
white-space:pre-wrap;word-break:break-all}
.qbox{margin-top:5px;padding:6px 9px;border-left:3px solid var(--yellow);
background:#9e6a0318;border-radius:0 6px 6px 0;font-size:12.5px;color:#e3b341;
white-space:pre-wrap;word-break:break-all}
/* ── 回答/续作 Dialog ── */
dialog{border:1px solid var(--border);border-radius:12px;background:var(--panel);
color:var(--fg);width:min(560px,92vw);padding:0;box-shadow:0 16px 48px #00000088;
margin:auto}
dialog::backdrop{background:#010409cc;backdrop-filter:blur(2px)}
.dlg-head{padding:14px 18px;border-bottom:1px solid var(--border);display:flex;
justify-content:space-between;align-items:center}
.dlg-kind{display:inline-block;padding:2px 10px;border-radius:10px;font-size:12px;font-weight:700}
.dlg-kind.clarify{background:#9e6a0333;color:var(--yellow)}
.dlg-kind.blocked{background:#da363333;color:var(--red)}
.dlg-title{font-size:15px;font-weight:600;margin:12px 18px 4px}
.dlg-meta{font-size:12px;color:var(--dim);margin:0 18px 10px}
.dlg-q{margin:0 18px 12px;padding:12px 14px;border-left:3px solid var(--yellow);
background:#0d1117;border-radius:0 8px 8px 0;font-size:13.5px;line-height:1.6;
white-space:pre-wrap;overflow-wrap:break-word;word-break:normal;
max-height:180px;overflow:auto}
.dlg-opts-label{margin:0 18px 6px;font-size:12px;color:var(--dim)}
.dlg-opts{margin:0 18px 12px;display:flex;flex-wrap:wrap;gap:8px}
.dlg-opt{background:#21262d;border:1px solid var(--border);border-radius:8px;
padding:7px 14px;font-size:13px;cursor:pointer;transition:all .15s}
.dlg-opt:hover{border-color:var(--blue);color:var(--blue)}
.dlg-opt.sel{border-color:var(--green);background:#23863622;color:var(--green);font-weight:600}
dialog textarea{margin:0 18px 8px;width:calc(100% - 36px);background:#0d1117;
border:1px solid var(--border);border-radius:8px;color:var(--fg);padding:10px;
font:13px/1.5 monospace;resize:vertical}
dialog textarea:focus{outline:none;border-color:var(--blue)}
.dlg-err{margin:0 18px;color:var(--red);font-size:12.5px;min-height:16px}
.dlg-lbl{display:block;font-size:12px;font-weight:600;margin-bottom:4px;color:var(--fg)}
.dlg-lbl input{width:100%}
.dlg-foot{padding:12px 18px;border-top:1px solid var(--border);display:flex;
justify-content:flex-end;gap:10px}
.dlg-foot .primary{background:#238636;border-color:#2ea043;color:#fff;font-weight:600}
.dlg-foot .primary:hover{background:#2ea043}
.dlg-foot .primary:disabled{opacity:.5;cursor:not-allowed}
.empty{padding:18px;color:var(--dim);text-align:center}
.card{background:var(--panel);border:1px solid var(--border);border-radius:8px;padding:12px}
.row{display:flex;gap:8px;margin:6px 0}
</style>
</head>
<body>
<h1>Simple-Agents-Studio <span style="font-size:13px;color:var(--dim);font-weight:400">⟪CONSOLE_FALLBACK_SUBTITLE⟫</h1>
<div class="tabs">
  <div class="tab on" data-v="tasks"⟪CONSOLE_FALLBACK_TAB_TASKS⟫
  <div class="tab" data-v="workers"⟪CONSOLE_FALLBACK_TAB_WORKERS⟫
  <div class="tab" data-v="settings"⟪CONSOLE_FALLBACK_TAB_SETTINGS⟫
</div>
<div class="bar" id="barTasks">
  <span class="muted"><span class="dot" id="reaperDot"></span>Reaper: <b id="reaperTxt">…</b></span>
  <input id="q" ⟪CONSOLE_FALLBACK_SEARCH_PLACEHOLDER⟫>
  <select id="fGroup">
    <option value=""⟪CONSOLE_FALLBACK_ALL_GROUPS⟫
    <option value="attention"⟪CONSOLE_FALLBACK_GROUP_ATTENTION⟫
    <option value="running"⟪CONSOLE_FALLBACK_GROUP_RUNNING⟫
    <option value="finished"⟪CONSOLE_FALLBACK_GROUP_FINISHED⟫
  </select>
  <select id="fWorker"><option value=""⟪CONSOLE_FALLBACK_ALL_WORKERS⟫</select>
  <select id="fHarvest">
    <option value=""⟪CONSOLE_FALLBACK_ALL_VERDICTS⟫
    <option value="NEED_CLARIFICATION"⟪CONSOLE_FALLBACK_VERDICT_CLARIFY⟫
    <option value="BLOCKED"⟪CONSOLE_FALLBACK_VERDICT_BLOCKED⟫
    <option value="HARVESTED"⟪CONSOLE_FALLBACK_VERDICT_HARVESTED⟫
    <option value="PENDING"⟪CONSOLE_FALLBACK_VERDICT_PENDING⟫
  </select>
  <span class="muted" id="statTxt"></span>
  <button ⟪CONSOLE_FALLBACK_REFRESH⟫
</div>
<div id="view"></div>

<dialog id="dlg">
  <div class="dlg-head">
    <span style="font-size:14px;font-weight:700">⟪CONSOLE_FALLBACK_ANSWER_CONTINUE⟫
    <button onclick="document.getElementById('dlg').close()">✕</button>
  </div>
  <div id="dlgBody"></div>
  <div class="dlg-foot">
    <button onclick="document.getElementById('dlg').close()"⟪CONSOLE_FALLBACK_CANCEL⟫
    <button class="primary" id="dlgOk" onclick="submitAnswer()"⟪CONSOLE_FALLBACK_SUBMIT_CONTINUE⟫
  </div>
</dialog>

<dialog id="dlgW">
  <div class="dlg-head">
    <span style="font-size:14px;font-weight:700">⟪CONSOLE_FALLBACK_WORKER_DETAIL⟫
    <button onclick="document.getElementById('dlgW').close()">✕</button>
  </div>
  <div id="dlgWBody" style="padding:6px 0 14px;max-height:60vh;overflow:auto"></div>
  <div class="dlg-foot">
    <button onclick="document.getElementById('dlgW').close()"⟪CONSOLE_FALLBACK_CLOSE⟫
  </div>
</dialog>

<dialog id="dlgA">
  <div class="dlg-head">
    <span id="dlgAWTitle" style="font-size:14px;font-weight:700">Worker</span>
    <button onclick="document.getElementById('dlgA').close()">✕</button>
  </div>
  <div id="dlgABody"></div>
  <div class="dlg-foot">
    <button onclick="document.getElementById('dlgA').close()"⟪CONSOLE_FALLBACK_CANCEL⟫
    <button class="primary" id="dlgAOk" onclick="dlgAAction()"⟪CONSOLE_FALLBACK_SAVE⟫
  </div>
</dialog>

<script>
⟪CONSOLE_FALLBACK_FB_BLOCK⟫
let cur='tasks', workersCache={}, tasksCache=[], dlgW=null, dlgMode='register';
const $=s=>document.querySelector(s);
document.querySelectorAll('.tab').forEach(t=>t.onclick=()=>{
  document.querySelectorAll('.tab').forEach(x=>x.classList.remove('on'));
  t.classList.add('on'); cur=t.dataset.v;
  $('#barTasks').style.display = cur==='tasks'?'flex':'none';
  load();});
['q','fGroup','fWorker','fHarvest'].forEach(id=>
  document.getElementById(id).addEventListener('input',()=>{if(cur==='tasks')renderTasks();}));

function esc(s){return (s==null?'':''+s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]))}
function ts(x){return x?new Date(x*1000).toLocaleString('zh-CN',{month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit'}):''}

// 归类 + 语义: 待处理(等回答/等裁决) / 进行中 / 已完成
function classify(t){
  if(t.status==='done'||t.status==='archived')
    return {group:'finished',bcls:'done',⟪CONSOLE_FALLBACK_SEM_DONE⟫;
  if(t.harvest_status==='NEED_CLARIFICATION'||(t.status==='blocked'&&t.block_kind==='needs_input'))
    return {group:'attention',bcls:'wait-answer',⟪CONSOLE_FALLBACK_SEM_CLARIFY⟫;
  if(t.status==='blocked')
    return {group:'attention',bcls:'wait-judge',⟪CONSOLE_FALLBACK_SEM_BLOCKED⟫;
  if(t.status==='running')
    return {group:'running',bcls:'running',⟪CONSOLE_FALLBACK_SEM_RUNNING⟫;
  return {group:'running',bcls:'dim',⟪CONSOLE_FALLBACK_SEM_QUEUED⟫;
}

async function load(){
  try{
    const w=await fetch('/api/workers').then(r=>r.json());
    workersCache=w.workers||{};
    const alive=w.reaper_alive;
    $('#reaperDot').className='dot '+(alive?'ok':'dead');
    $('#reaperTxt').textContent=alive?⟪CONSOLE_FALLBACK_ONLINE_AGE_EXPR⟫⟪CONSOLE_FALLBACK_OFFLINE⟫;
    if(cur==='tasks'){
      const d=await fetch('/api/tasks').then(r=>r.json());
      tasksCache=d.tasks||[];
      const ws=[...new Set(tasksCache.map(t=>t.assignee).filter(Boolean))];
      const sel=$('#fWorker'), v=sel.value;
      sel.innerHTML='<option value=""⟪CONSOLE_FALLBACK_ALL_WORKERS⟫'+ws.map(w=>`<option>${esc(w)}</option>`).join('');
      sel.value=v;
      renderTasks();
      // issue #75 s9 M-2: 列表投影已剥离 workspace/exit_code, 兜底页这两列
      // 若不补拉则恒显示 '-' (静默降级)。按需补拉: 只拉列表里缺字段的
      // 任务详情 (GET /api/tasks/<id>), 拉完原地刷新表格。
      // 失败不阻断 (逐条 catch 吞掉), 与 openDialog 的 fail-soft 同口径。
      loadTaskExtras();
    } else if(cur==='settings'){ renderSettings(); }
    else renderWorkers();
  }catch(e){$('#statTxt').textContent⟪CONSOLE_FALLBACK_LOAD_FAIL_EXPR⟫
}

// issue #75 s9 M-2: 兜底页 DIR/exit 两列的字段补拉 (列表投影已剥离,
// 不补拉则两列恒显示 '-' = 静默降级)。策略:
//   - extrasCache 跨轮询保留已补过的字段 (5s 刷新重建 tasksCache 后按
//     id 合并回来), 同一任务不重复拉;
//   - 单轮补拉上限 30 条 (列表 LIMIT 200; 兜底页本就该轻量, 拉不完的
//     行保持 '-', 下轮继续, 永不积压成无界请求);
//   - 逐条 catch 吞掉失败 (详情端点 404/网络抖动不阻断整页), 与
//     openDialog 的 fail-soft 同口径。端点走 encodeURIComponent,
//     任务 id 不进未转义的模板串。
const taskExtrasCache={};
let taskExtrasPending=false;
async function loadTaskExtras(){
  // 先把已补过的字段合并回新一轮的 tasksCache (免重复请求)。
  tasksCache.forEach(t=>{
    const c=taskExtrasCache[t.id];
    if(!c)return;
    if(t.workspace==null&&c.workspace!=null) t.workspace=c.workspace;
    if(t.exit_code==null&&c.exit_code!=null) t.exit_code=c.exit_code;
  });
  if(taskExtrasPending)return;
  const need=tasksCache.filter(t=>!taskExtrasCache[t.id]
    &&(t.workspace==null||t.exit_code==null)).slice(0,30);
  if(!need.length)return;
  taskExtrasPending=true;
  try{
    await Promise.all(need.map(t=>fetch('/api/tasks/'+encodeURIComponent(t.id))
      .then(r=>r.ok?r.json():null)
      .then(d=>{
        const f=(d&&d.task)||{};
        // 只回填这两个小字段 (详情大字段 result/harvest_log 不进列表缓存)。
        // 条目【无条件】建档 = 已尝试标记: 详情里本就没有的字段 (如未结算
        // 任务的 exit_code=null) 不建档的话每 5s 轮询会重复拉同一任务。
        const c=taskExtrasCache[t.id]||(taskExtrasCache[t.id]={});
        if(f.workspace!=null) c.workspace=f.workspace;
        if(f.exit_code!==undefined) c.exit_code=f.exit_code;
        if(t.workspace==null&&c.workspace!=null) t.workspace=c.workspace;
        if(t.exit_code==null&&c.exit_code!=null) t.exit_code=c.exit_code;
      })
      .catch(()=>{})));
    renderTasks();
  }finally{taskExtrasPending=false}
}

function renderTasks(){
  const q=$('#q').value.trim().toLowerCase();
  const fg=$('#fGroup').value, fw=$('#fWorker').value, fh=$('#fHarvest').value;
  let rows=tasksCache.map(t=>({t,c:classify(t)}));
  if(q) rows=rows.filter(x=>(x.t.title||'').toLowerCase().includes(q)||x.t.id.toLowerCase().includes(q));
  if(fw) rows=rows.filter(x=>x.t.assignee===fw);
  if(fh) rows=rows.filter(x=>x.t.harvest_status===fh);
  if(fg) rows=rows.filter(x=>x.c.group===fg);
  const cnt={attention:0,running:0,finished:0};
  tasksCache.forEach(t=>cnt[classify(t).group]++);
  $('#statTxt').textContent=⟪CONSOLE_FALLBACK_STATS_LINE_EXPR⟫;
  const order=[⟪CONSOLE_FALLBACK_GROUP_ATTENTION_JS⟫,⟪CONSOLE_FALLBACK_GROUP_RUNNING_JS⟫,⟪CONSOLE_FALLBACK_GROUP_FINISHED_JS⟫];
  let h='';
  for(const [g,label] of order){
    const sub=rows.filter(x=>x.c.group===g);
    if(!sub.length)continue;
    h+=`<div class="grp">${label} <span class="muted">(${sub.length})</span></div>`;
    h+=`<table><tr>⟪CONSOLE_FALLBACK_COL_STATUS⟫⟪CONSOLE_FALLBACK_COL_TASK⟫<th>Worker</th><th>Session</th>⟪CONSOLE_FALLBACK_COL_DIR⟫<th>exit</th>⟪CONSOLE_FALLBACK_COL_DETAIL⟫⟪CONSOLE_FALLBACK_COL_TIME⟫${g==='attention'?⟪CONSOLE_FALLBACK_COL_ACTION_JS⟫:''}</tr>`;
    for(const {t,c} of sub){
      // issue #75 s6: 列表投影已剥离 result/harvest_log (前端主 UI 零消费);
      // 兜底页详情列只展示 block_kind 短文本, 不再回显全量 result。
      const detail = t.block_kind || '';
      const act = g==='attention' ? `<td>${t.session_id?`<button class="answer" onclick="openDialog('${esc(t.id)}')"⟪CONSOLE_FALLBACK_BTN_ANSWER⟫`:⟪CONSOLE_FALLBACK_NO_SESSION⟫}</td>` : '';
      h+=`<tr>
        <td><span class="badge ${c.bcls}">${esc(c.sem)}</span></td>
        <td><div class="title">${esc(t.title)}</div><div class="mono">${esc(t.id)}</div></td>
        <td><span class="tag">${esc(t.assignee||'-')}</span></td>
        <td class="mono">${esc(t.session_id||'-')}</td>
        <td class="mono">${esc(t.workspace||'-')}</td>
        <td class="mono">${t.exit_code==null?'-':t.exit_code}</td>
        <td><div class="detail">${esc(detail)}</div></td>
        <td class="mono">${ts(t.created_at)}</td>${act}</tr>`;
    }
    h+='</table>';
  }
  if(!h) h=⟪CONSOLE_FALLBACK_NO_MATCH⟫;
  $('#view').innerHTML=h;
}

function renderWorkers(){
  const ws=workersCache;
  let h=`<div style="display:flex;gap:8px;margin-bottom:12px">
    <button class="primary" style="background:#238636;border-color:#2ea043;color:#fff;font-weight:600" ⟪CONSOLE_FALLBACK_ADD_WORKER⟫
    <button ⟪CONSOLE_FALLBACK_NEW_WORKER⟫
    <span class="muted" style="align-self:center">⟪CONSOLE_FALLBACK_TIP_REGISTER_VS_CREATE⟫</span>
  </div>`;
  h+='<table><tr><th>Worker</th><th>runner</th><th>api_url</th><th>model</th><th>hermes_home</th>⟪CONSOLE_FALLBACK_COL_ACTION⟫</tr>';
  for(const [name,e] of Object.entries(ws)){
    h+=`<tr><td><span class="tag">${esc(name)}</span></td><td class="mono">${esc(e.runner)}</td>
      <td class="mono">${esc(e.api_url)}</td><td class="mono">${esc(e.model)}</td>
      <td class="mono">${esc(e.hermes_home)}</td>
      <td><button onclick="viewWorker('${esc(name)}')"⟪CONSOLE_FALLBACK_VIEW⟫
      <button class="danger" onclick="delWorker('${esc(name)}')"⟪CONSOLE_FALLBACK_DELETE⟫</td></tr>`;
  }
  h+='</table>';
  if(!Object.keys(ws).length) h+=⟪CONSOLE_FALLBACK_NO_WORKER⟫;
  $('#view').innerHTML=h;
}

// ── 注册 Worker 弹窗 ─────────────────────────────────────────
function openRegister(){
  dlgMode='register';
  ⟪CONSOLE_FALLBACK_TITLE_REGISTER_EXPR⟫
  $('#dlgABody').innerHTML=`
    <div class="dlg-meta" style="margin:8px 18px 12px">⟪CONSOLE_FALLBACK_TIP_REGISTER⟫</div>
    <div style="margin:0 18px 10px"><label class="dlg-lbl"⟪CONSOLE_FALLBACK_LBL_NAME_REQUIRED⟫
      <input id="wName" ⟪CONSOLE_FALLBACK_PH_REVIEWER⟫ style="width:100%"></div>
    <div style="margin:0 18px 10px"><label class="dlg-lbl"⟪CONSOLE_FALLBACK_LBL_API_URL⟫
      <input id="wUrl" ⟪CONSOLE_FALLBACK_PH_URL⟫ style="width:100%"></div>
    <div style="margin:0 18px 10px"><label class="dlg-lbl"⟪CONSOLE_FALLBACK_LBL_API_KEY⟫
      <input id="wKey" ⟪CONSOLE_FALLBACK_PH_API_KEY⟫ style="width:100%"></div>
    <div style="margin:0 18px 10px"><label class="dlg-lbl">model</label>
      <input id="wModel" value="hermes-agent" style="width:100%"></div>
    <div style="margin:0 18px 10px"><label class="dlg-lbl">hermes_home <span class="muted" style="font-weight:400">⟪CONSOLE_FALLBACK_HINT_HERMES_HOME⟫</span></label>
      <input id="wHome" placeholder="/path/to/worker/hermes" style="width:100%"></div>
    <div class="dlg-err" id="awErr"></div>`;
  ⟪CONSOLE_FALLBACK_BTN_SAVE_REGISTER_EXPR⟫
  $('#dlgA').showModal();
  setTimeout(()=>$('#wName').focus(),50);
}

// ── 新建 Worker(compose) 弹窗 ────────────────────────────────
function openCompose(){
  dlgMode='compose';
  ⟪CONSOLE_FALLBACK_TITLE_COMPOSE_EXPR⟫
  $('#dlgABody').innerHTML=`
    <div class="dlg-meta" style="margin:8px 18px 12px">⟪CONSOLE_FALLBACK_TIP_COMPOSE⟫</div>
    <div style="margin:0 18px 10px"><label class="dlg-lbl"⟪CONSOLE_FALLBACK_LBL_WORKER_NAME⟫
      <input id="cName" ⟪CONSOLE_FALLBACK_PH_REVIEWER⟫ style="width:100%"></div>
    <div style="margin:0 18px 10px"><label class="dlg-lbl">api_key * <span class="muted" style="font-weight:400">⟪CONSOLE_FALLBACK_LBL_API_KEY_AT_LEAST_16⟫</span></label>
      <input id="cKey" ⟪CONSOLE_FALLBACK_PH_COMPOSE_API_KEY⟫ style="width:100%"></div>
    <pre id="cOut" class="mono" style="margin:10px 18px 0;background:#0d1117;border:1px solid var(--border);border-radius:8px;padding:12px;white-space:pre-wrap;overflow-wrap:break-word;display:none;max-height:40vh;overflow:auto"></pre>
    <div class="dlg-err" id="awErr"></div>`;
  ⟪CONSOLE_FALLBACK_BTN_GENERATE_EXPR⟫
  $('#dlgA').showModal();
  setTimeout(()=>$('#cName').focus(),50);
}

async function dlgAAction(){
  const err=$('#awErr'); err.textContent='';
  if(dlgMode==='register'){
    const name=$('#wName').value.trim();
    if(!name){⟪CONSOLE_FALLBACK_ERR_NAME_REQUIRED_EXPR⟫;return;}
    const body={...workersCache};
    body[name]={runner:'api',api_url:$('#wUrl').value.trim(),api_key:$('#wKey').value.trim(),
      model:$('#wModel').value.trim()||'hermes-agent',hermes_home:$('#wHome').value.trim()};
    const r=await fetch('/api/workers',{method:'PUT',body:JSON.stringify(body)});
    if(!r.ok){⟪CONSOLE_FALLBACK_ERR_SAVE_FAIL_EXPR⟫;return;}
    $('#dlgA').close(); load();
  } else {
    const out=$('#cOut');
    try{
      const r=await fetch('/api/compose/generate',{method:'POST',
        headers:{'Content-Type':'application/json'},
        body:JSON.stringify({name:$('#cName').value.trim(),api_key:$('#cKey').value.trim()})});
      const d=await r.json();
      if(!r.ok) throw new Error(⟪CONSOLE_FALLBACK_ERR_GENERATE_FAIL_EXPR⟫);
      out.style.display='block';
      out.textContent=d.yaml+⟪CONSOLE_FALLBACK_NEXT_STEPS_HEADER⟫+d.next_steps.map(s=>'# '+s).join('\n');
      ⟪CONSOLE_FALLBACK_BTN_COPY_CLOSE_EXPR⟫
      navigator.clipboard && navigator.clipboard.writeText(d.yaml).catch(()=>{});
    }catch(e){err.textContent=e.message}
  }
}

async function viewWorker(name){
  dlgW=name;
  $('#dlgWBody').innerHTML=⟪CONSOLE_FALLBACK_LOADING⟫;
  document.getElementById('dlgW').showModal();
  try{
    const d=await fetch('/api/workers/'+encodeURIComponent(name)).then(r=>r.json());
    if(d.error) throw new Error(d.error);
    const reg=Object.entries(d.registry).map(([k,v])=>`<tr><td class="muted">${esc(k)}</td><td class="mono">${esc(v)}</td></tr>`).join('');
    const realModel = d.actual_model
      ? `<span class="tag" style="border-color:var(--green);color:var(--green)">${esc(d.actual_model)}</span> <span class="muted">via ${esc(d.actual_provider||'?')}</span>`
      : ⟪CONSOLE_FALLBACK_GET_FAIL_EXPR⟫;
    const models=(d.models&&d.models.data||[]).map(m=>`<span class="tag">${esc(m.id||m.name||'?')}</span>`).join(' ')||'<span class="muted">—</span>';
    const ts=(d.toolsets&&d.toolsets.data||[]).filter(t=>t.enabled).map(t=>`<span class="tag">${esc(t.name)}</span>`).join(' ')||'<span class="muted">—</span>';
    $('#dlgWBody').innerHTML=`
      <div class="dlg-meta"⟪CONSOLE_FALLBACK_COL_ONLINE_STATUS⟫${d.online?'⟪CONSOLE_FALLBACK_COL_ONLINE⟫':'⟪CONSOLE_FALLBACK_COL_OFFLINE⟫'}</div>
      <div class="dlg-title" style="margin:10px 0 6px">⟪CONSOLE_FALLBACK_LBL_ACTUAL_MODEL⟫ <span class="muted" style="font-weight:400">/api/model/options</span></div>
      <div>${realModel}</div>
      <div class="dlg-title" style="margin:12px 0 6px">⟪CONSOLE_FALLBACK_LBL_REGISTRY⟫</div>
      <table>${reg}</table>
      <div class="dlg-title" style="margin:12px 0 6px">⟪CONSOLE_FALLBACK_LBL_API_MODELS⟫ <span class="muted" style="font-weight:400">/v1/models</span></div>
      <div>${models}${d.models_ok?'':' ⟪CONSOLE_FALLBACK_GET_FAIL_SUFFIX⟫'}
      <span class="muted" style="font-size:11.5px"> ⟪CONSOLE_FALLBACK_TIP_ALIAS_VS_REAL⟫</span></div>
      <div class="dlg-title" style="margin:12px 0 6px">⟪CONSOLE_FALLBACK_LBL_TOOLSETS⟫ <span class="muted" style="font-weight:400">/v1/toolsets</span></div>
      <div>${ts}${d.toolsets_ok?'':' ⟪CONSOLE_FALLBACK_GET_FAIL_SUFFIX⟫'}</div>
      <div class="dlg-q" style="border-left-color:var(--blue);margin-top:14px">
        ⟪CONSOLE_FALLBACK_TIP_NO_SOUL_BLOCK⟫<code>docker exec hermes_${esc(name)} cat /opt/data/SOUL.md</code>。</div>`;
  }catch(e){$('#dlgWBody').innerHTML='<div class="dlg-err">❌ '+esc(e.message)+'</div>'}
}

// ── 回答/续作 Dialog ──────────────────────────────────────────
// issue #75 s6: question/options/result/harvest_log 已移出 /api/tasks 列表
// (瘦身投影)。弹窗打开时按需拉 GET /api/tasks/<id> 详情补齐 —— 兜底页
// 不再依赖列表里的大字段 (列表字段自 s3 起已缺, 弹窗此前是静默降级)。
let dlgTask=null, dlgChoice='';
function openDialog(id){
  const t=tasksCache.find(x=>x.id===id);
  if(!t)return;
  dlgTask=t; dlgChoice='';
  fetch('/api/tasks/'+encodeURIComponent(id)).then(r=>r.ok?r.json():null)
    .then(d=>{
      const f=(d&&d.task)||{};
      // 详情字段覆盖列表缓存 (list 只剩白名单; 失败则保留现值不阻断弹窗)
      // issue #116: answered_by/answered_at_hhmm 一并合并, 弹窗里可见「已被 X 答复」
      ['question','options','harvest_log','result','session_id','answered_by','answered_at_hhmm'].forEach(k=>{
        if(f[k]!==undefined) dlgTask[k]=f[k];
      });
      renderDialog();
    })
    .catch(()=>renderDialog());
}
function renderDialog(){
  const t=dlgTask;
  const isClarify=t.harvest_status==='NEED_CLARIFICATION';
  const head=isClarify
    ? ⟪CONSOLE_FALLBACK_DLG_KIND_CLARIFY⟫
    : ⟪CONSOLE_FALLBACK_DLG_KIND_BLOCKED⟫;
  const q=⟪CONSOLE_FALLBACK_DLG_NO_QUESTION_EXPR⟫;
  let optsHtml='';
  if(isClarify && t.options && t.options.length){
    optsHtml=⟪CONSOLE_FALLBACK_DLG_OPTS_LABEL⟫
      t.options.map((o,i)=>`<button class="dlg-opt" data-i="${i}" onclick="pickOpt(${i})">${esc(o)}</button>`).join('')+
      '</div>';
  }
  document.getElementById('dlgBody').innerHTML=`
    ${head}
    <div class="dlg-title">${esc(t.title)}</div>
    <div class="dlg-meta"><span class="mono">${esc(t.id)}</span> · <span class="tag">${esc(t.assignee||'-')}</span> · session <span class="mono">${esc(t.session_id||'-')}</span></div>
    ${t.answered_by?⟪CONSOLE_FALLBACK_ANSWERED_LINE_EXPR⟫:''}
    <div class="dlg-q">${esc(q)}</div>
    ${optsHtml}
    <textarea id="dlgAns" rows="4" ⟪CONSOLE_FALLBACK_PH_ANSWER⟫></textarea>
    <div class="dlg-err" id="dlgErr"></div>`;
  document.getElementById('dlg').showModal();
  setTimeout(()=>document.getElementById('dlgAns').focus(),50);
}
function pickOpt(i){
  dlgChoice=(dlgTask.options&&dlgTask.options[i])||'';
  document.querySelectorAll('.dlg-opt').forEach(b=>b.classList.toggle('sel',+b.dataset.i===i));
  document.getElementById('dlgAns').value=dlgChoice;
  document.getElementById('dlgAns').focus();
}
async function submitAnswer(){
  const ans=document.getElementById('dlgAns').value.trim();
  if(!ans){document.getElementById('dlgErr').textContent=⟪CONSOLE_FALLBACK_ERR_ANSWER_REQUIRED⟫;return;}
  const btn=document.getElementById('dlgOk');
  btn.disabled=true; ⟪CONSOLE_FALLBACK_BTN_SUBMITTING_EXPR⟫;
  try{
    const r=await fetch('/api/resume',{method:'POST',
      body:JSON.stringify({task_id:dlgTask.id,instruction:ans})});
    const d=await r.json();
    if(!r.ok)throw new Error(d.error||('HTTP '+r.status));
    // issue #116: 后到答复的冲突提示 —— conflict 非空时写进提示位并保持
    // 弹窗打开 (答复已入队, 但操作员必须看见「已被 X 答复」再自行决定),
    // 不再静默 close() 让界面与盲注无异。
    if(d.conflict){
      document.getElementById('dlgErr').textContent=⟪CONSOLE_FALLBACK_CONFLICT_NOTICE_EXPR⟫;
      btn.disabled=false; ⟪CONSOLE_FALLBACK_BTN_RESTORE_SUBMIT_EXPR⟫;
      return;
    }
    document.getElementById('dlg').close();
    load();
  }catch(e){
    document.getElementById('dlgErr').textContent=⟪CONSOLE_FALLBACK_ERR_SUBMIT_FAIL_EXPR⟫;
    btn.disabled=false; ⟪CONSOLE_FALLBACK_BTN_RESTORE_SUBMIT_EXPR⟫;
  }
}
async function addWorker(){
  const name=$('#wName').value.trim();
  if(!name)return ⟪CONSOLE_FALLBACK_ALERT_NAME_REQUIRED⟫;
  const body={...workersCache};
  body[name]={runner:'api',api_url:$('#wUrl').value.trim(),api_key:$('#wKey').value.trim(),
    model:$('#wModel').value.trim()||'hermes-agent',hermes_home:$('#wHome').value.trim()};
  const r=await fetch('/api/workers',{method:'PUT',body:JSON.stringify(body)});
  if(!r.ok)⟪CONSOLE_FALLBACK_ALERT_SAVE_FAIL⟫;else load();
}
async function delWorker(name){
  if(!confirm(⟪CONSOLE_FALLBACK_CONFIRM_DELETE_EXPR⟫))return;
  const body={...workersCache};delete body[name];
  const r=await fetch('/api/workers',{method:'PUT',body:JSON.stringify(body)});
  if(!r.ok)⟪CONSOLE_FALLBACK_ALERT_DELETE_FAIL⟫;else load();
}

// ── 设置页 ────────────────────────────────────────────────────
const SETTING_LABELS={
  shared_workspace:⟪CONSOLE_FALLBACK_SETTING_PAIR_SHARED_WORKSPACE⟫,
  shared_workspace_host:⟪CONSOLE_FALLBACK_SETTING_PAIR_SHARED_WORKSPACE_HOST⟫,
  task_dir_base:⟪CONSOLE_FALLBACK_SETTING_PAIR_TASK_DIR_BASE⟫,
  docker_compose_dir:⟪CONSOLE_FALLBACK_SETTING_PAIR_DOCKER_COMPOSE_DIR⟫,
  default_timeout_seconds:⟪CONSOLE_FALLBACK_SETTING_PAIR_DEFAULT_TIMEOUT_SECONDS⟫,
  reaper_interval_seconds:⟪CONSOLE_FALLBACK_SETTING_PAIR_REAPER_INTERVAL_SECONDS⟫,
  default_image:⟪CONSOLE_FALLBACK_SETTING_PAIR_DEFAULT_IMAGE⟫,
};
async function renderSettings(){
  let d;
  try{ d=await fetch('/api/settings').then(r=>r.json()); }
  catch(e){ $('#view').innerHTML=⟪CONSOLE_FALLBACK_SETTINGS_LOAD_FAIL⟫; return; }
  const s=d.settings;
  let h=`<div class="card" style="max-width:640px">
    <h3 style="font-size:14px;margin-bottom:4px">⟪CONSOLE_FALLBACK_H_PUBLIC_SETTINGS⟫
    <div class="muted" style="margin-bottom:12px">${esc(d.note)}</div>`;
  for(const [k,[label,hint]] of Object.entries(SETTING_LABELS)){
    const v=s[k]??'';
    h+=`<div style="margin-bottom:12px">
      <label style="display:block;font-size:12.5px;font-weight:600;margin-bottom:3px">${esc(label)}
        <span class="mono" style="color:var(--dim);font-weight:400">(${esc(k)})</span></label>
      <input id="set_${k}" value="${esc(v)}" style="width:100%">
      <div class="muted" style="font-size:11.5px;margin-top:2px">${esc(hint)}</div></div>`;
  }
  h+=`<button class="primary" style="background:#238636;border-color:#2ea043;color:#fff;font-weight:600" ⟪CONSOLE_FALLBACK_BTN_SAVE_SETTINGS⟫
    <span class="muted" id="setMsg"></span>
    <div class="dlg-q" style="border-left-color:var(--yellow);margin-top:14px">
      ⟪CONSOLE_FALLBACK_TIP_NO_AUTORETRY_BLOCK⟫</div>
  </div>`;
  $('#view').innerHTML=h;
}
async function saveSettings(){
  const body={};
  for(const k of Object.keys(SETTING_LABELS)){
    const el=document.getElementById('set_'+k);
    if(!el)continue;
    let v=el.value.trim();
    if(k.endsWith('_seconds')) v=parseInt(v,10);
    body[k]=v;
  }
  const r=await fetch('/api/settings',{method:'PUT',
    headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
  const d=await r.json();
  $('#setMsg').textContent = ⟪CONSOLE_FALLBACK_MSG_SAVE_RESULT_EXPR⟫;
  if(r.ok) setTimeout(()=>{$('#setMsg').textContent='';},3000);
}
load(); setInterval(load,5000);
</script>
</body>
</html>"""

# ── i18n 服务端渲染表 (卡 fix-console-ui-i18n, 架构 §5.3 item 4) ──────────
# console_ui.py 模板 (HTML_TEMPLATE) 里的 ⟪KEY⟫ 哨兵按部署语言整页替换。
# ★零回归锚: zh-cn 值逐字 = main 基线原文 (parts/subst_console_ui.py 自动登记,
#   conftest 钉 MW_LANG=zh-cn → 页面与基线一致)。en 值保持同样的 JS/引号骨架。
_LANG_TABLES: dict = {
    "zh-cn": {
        'console.fallback.add_worker': 'onclick="openRegister()">➕ 注册 Worker</button>',
        'console.fallback.alert_delete_fail': "alert('删除失败: '+(await r.text()))",
        'console.fallback.alert_name_required': "alert('名称必填')",
        'console.fallback.alert_save_fail': "alert('保存失败: '+(await r.text()))",
        'console.fallback.all_groups': '>全部归类</option>',
        'console.fallback.all_verdicts': '>全部结算态</option>',
        'console.fallback.all_workers': '>全部 Worker</option>',
        'console.fallback.answer_continue': '💬 回答 / 续作</span>',
        # issue #116: 弹窗「已被 X 于 HH:MM 答复」行 + 后到冲突提示位文案
        'console.fallback.answered_line_expr': '\'<div class="dlg-meta" style="color:var(--yellow)">💬 已被 \'+esc(t.answered_by)+\' 于 \'+esc(t.answered_at_hhmm)+\' 答复</div>\'',
        'console.fallback.btn_answer': '>💬 回答/续作</button>',
        'console.fallback.btn_copy_close_expr': "$('#dlgAOk').textContent='复制并关闭';",
        'console.fallback.btn_generate_expr': "$('#dlgAOk').textContent='生成';",
        'console.fallback.btn_restore_submit_expr': "btn.textContent='提交并续作'",
        'console.fallback.btn_save_register_expr': "$('#dlgAOk').textContent='保存注册';",
        'console.fallback.btn_save_settings': 'onclick="saveSettings()">保存设置</button>',
        'console.fallback.btn_submitting_expr': "btn.textContent='提交中…'",
        'console.fallback.cancel': '>取消</button>',
        'console.fallback.close': '>关闭</button>',
        'console.fallback.col_action': '<th>操作</th>',
        'console.fallback.col_action_js': "'<th>操作</th>'",
        'console.fallback.col_detail': '<th>详情</th>',
        'console.fallback.col_dir': '<th>目录</th>',
        'console.fallback.col_offline': '<span style="color:var(--red)">● 离线</span>',
        'console.fallback.col_online': '<span style="color:var(--green)">● 在线</span>',
        'console.fallback.col_online_status': '>在线状态: ',
        'console.fallback.col_status': '<th>状态</th>',
        'console.fallback.col_task': '<th>任务</th>',
        'console.fallback.col_time': '<th>时间</th>',
        'console.fallback.confirm_delete_expr': "'删除 worker '+name+'?'",
        # issue #116: 后到答复冲突提示 (写进 dlgErr 提示位)
        'console.fallback.conflict_notice_expr': "'⚠ 冲突: '+d.conflict+' (你的答复已入队, 但对方已先答复, 请确认后再操作)'",
        'console.fallback.delete': '>删除</button>',
        'console.fallback.dlg_kind_blocked': '\'<div class="dlg-kind blocked">⛔ 验收未通过, 等待裁决</div>\'',
        'console.fallback.dlg_kind_clarify': '\'<div class="dlg-kind clarify">🤖 WORKER 请求澄清</div>\'',
        'console.fallback.dlg_no_question_expr': "t.question||'(无详细问题记录 — 可能为历史遗留任务)'",
        'console.fallback.dlg_opts_label': '\'<div class="dlg-opts-label">可选项(点击选择, 或直接输入自定义回答):</div><div class="dlg-opts">\'+',
        'console.fallback.err_answer_required': "'回答不能为空'",
        'console.fallback.err_generate_fail_expr': "d.error||'生成失败'",
        'console.fallback.err_name_required_expr': "err.textContent='名称必填'",
        'console.fallback.err_save_fail_expr': "err.textContent='保存失败: '+(await r.text())",
        'console.fallback.err_submit_fail_expr': "'提交失败: '+e.message",
        'console.fallback.fb_block': '// i18n v1.0: 本页文案由服务端按部署语言(MW_LANG)整页渲染;\n// page strings are rendered server-side from mw/i18n (deployment language,\n// MW_LANG). The language switch lives at the deploy layer.\n// See design.md §10 (i18n architecture summary).',
        'console.fallback.get_fail_expr': '`<span class="muted">获取失败${d.actual_model_error?\' (\'+esc(d.actual_model_error)+\')\':\'\'}</span>`',
        'console.fallback.get_fail_suffix': '<span class="muted">(获取失败)</span>',
        'console.fallback.group_attention': '>🔴 待处理</option>',
        'console.fallback.group_attention_js': "['attention','🔴 待处理']",
        'console.fallback.group_finished': '>✅ 已完成</option>',
        'console.fallback.group_finished_js': "['finished','✅ 已完成']",
        'console.fallback.group_running': '>🔵 进行中</option>',
        'console.fallback.group_running_js': "['running','🔵 进行中']",
        'console.fallback.h_public_settings': '⚙ 公共配置</h3>',
        'console.fallback.hint_hermes_home': '(worker Hermes 目录·宿主机路径)',
        'console.fallback.html_lang': 'zh',
        'console.fallback.lbl_actual_model': '实际生效模型',
        'console.fallback.lbl_api_key': '>api_key *</label>',
        'console.fallback.lbl_api_key_at_least_16': '(≥16位)',
        'console.fallback.lbl_api_models': 'API 模型别名',
        'console.fallback.lbl_api_url': '>api_url *</label>',
        'console.fallback.lbl_name_required': '>名称 *</label>',
        'console.fallback.lbl_registry': '注册表(registry.json)',
        'console.fallback.lbl_toolsets': '启用的工具集',
        'console.fallback.lbl_worker_name': '>worker 名称 *</label>',
        'console.fallback.load_fail_expr': "='加载失败: '+e}",
        'console.fallback.loading': '\'<div class="muted">加载中…</div>\'',
        'console.fallback.msg_save_result_expr': "r.ok ? '✓ 已保存' : '❌ '+(d.error||'保存失败')",
        'console.fallback.new_worker': 'onclick="openCompose()">🐳 新建 Worker（生成 docker-compose）</button>',
        'console.fallback.next_steps_header': "'\\n\\n# 下一步:\\n'",
        'console.fallback.no_match': '\'<div class="empty">无匹配任务</div>\'',
        'console.fallback.no_session': '\'<span class="muted">无session</span>\'',
        'console.fallback.no_worker': '\'<div class="empty">暂无 worker, 点上方按钮注册或新建</div>\'',
        'console.fallback.offline': ":'离线'",
        'console.fallback.online_age_expr': "('在线 '+(w.reaper_heartbeat_age_s|0)+'s 前')",
        'console.fallback.ph_answer': 'placeholder="输入你的回答 / 指令, 将以新上下文 resume 该 worker 会话..."',
        'console.fallback.ph_api_key': 'placeholder="worker gateway 的 API key"',
        'console.fallback.ph_compose_api_key': 'placeholder="给这个 worker 设一个 API key"',
        'console.fallback.ph_reviewer': 'placeholder="如 reviewer"',
        'console.fallback.ph_url': 'placeholder="http://hermes_<name>:8642"',
        'console.fallback.refresh': 'onclick="load()">刷新</button>',
        'console.fallback.save': '>保存</button>',
        'console.fallback.search_placeholder': 'placeholder="🔍 搜索任务标题 / ID"',
        'console.fallback.sem_blocked': "sem:'⛔ 等裁决'}",
        'console.fallback.sem_clarify': "sem:'⚠ 等回答'}",
        'console.fallback.sem_done': "sem:'已结算'}",
        'console.fallback.sem_queued': "sem:t.status||'排队'}",
        'console.fallback.sem_running': "sem:'执行中'}",
        'console.fallback.setting_pair_default_image': "['Worker 默认镜像','生成 compose 模版时的默认 image 字段']",
        'console.fallback.setting_pair_default_timeout_seconds': "['任务超时(秒)','挂死兜底(dead-man\\'s switch): 超过此时长 worker 未收工才按超时结算, 正常完成是事件驱动']",
        'console.fallback.setting_pair_docker_compose_dir': "['Worker compose 存放目录','控制台生成的 docker-compose.yaml 的建议落盘目录']",
        'console.fallback.setting_pair_reaper_interval_seconds': "['Reaper 轮询间隔(秒)','异步任务结算的最大延迟; 与 API 读取频率无关(读取次数=结算次数)']",
        'console.fallback.setting_pair_shared_workspace': "['共享项目 workspace 根路径','所有 worker 共同操作的任务/项目目录(容器内路径)']",
        'console.fallback.setting_pair_shared_workspace_host': "['共享 workspace 宿主机路径','compose volume 的挂载源(宿主机绝对路径), 生成 compose 时用']",
        'console.fallback.setting_pair_task_dir_base': "['临时任务目录基路径','非项目任务的工作目录, 位于共享 workspace 之下']",
        'console.fallback.settings_load_fail': '\'<div class="empty">加载失败: \'+esc(e.message)+\'</div>\'',
        'console.fallback.stats_line_expr': '`总 ${tasksCache.length} · 待处理 ${cnt.attention} · 进行中 ${cnt.running} · 已完成 ${cnt.finished} · 显示 ${rows.length}`',
        'console.fallback.submit_continue': '>提交并续作</button>',
        'console.fallback.subtitle': '简单点儿 Agents 工作室 · 多Agent长时任务管理</span>',
        'console.fallback.tab_settings': '>⚙ 设置</div>',
        'console.fallback.tab_tasks': '>任务看板</div>',
        'console.fallback.tab_workers': '>Worker 管理</div>',
        'console.fallback.tip_alias_vs_real': '← 这是请求别名, 非真实模型; 真实模型见上方',
        'console.fallback.tip_compose': '按模版生成 compose 文件内容; 起容器是宿主机上的显式操作, 本平台不代跑。',
        'console.fallback.tip_no_autoretry_block': '⚠️ 本系统<b>刻意不提供「自动重试」</b>：worker 可能已改动部分文件后失败，\n      自动重派会造成重复施工与状态崩溃。失败只 BLOCK，重派是人工/上游的显式决策。',
        'console.fallback.tip_no_soul_block': 'ℹ️ SOUL.md 等 profile 文件刻意不展示：worker gateway API 无文件读取端点，\n        读文件需挂载 worker hermes_home，与「免挂载」隔离设计冲突。\n        如需查看请 ',
        'console.fallback.tip_register': '仅注册到 registry.json(路由表), 不创建容器; 需 worker 容器已在运行。',
        'console.fallback.tip_register_vs_create': '注册=写入路由表; 新建=生成 compose 模版去宿主机起容器',
        'console.fallback.title': '<title>Simple-Agents-Studio 控制台</title>',
        'console.fallback.title_compose_expr': "$('#dlgAWTitle').textContent='🐳 新建 Worker（生成 docker-compose.yaml）';",
        'console.fallback.title_register_expr': "$('#dlgAWTitle').textContent='➕ 注册 Worker';",
        'console.fallback.verdict_blocked': '>等裁决</option>',
        'console.fallback.verdict_clarify': '>等回答</option>',
        'console.fallback.verdict_harvested': '>已结算</option>',
        'console.fallback.verdict_pending': '>未结算</option>',
        'console.fallback.view': '>👁 查看</button>',
        'console.fallback.worker_detail': '👁 Worker 详情</span>'
    },
    "en": {
        'console.fallback.add_worker': 'onclick="openRegister()">➕ Register Worker</button>',
        'console.fallback.alert_delete_fail': "alert('Delete failed: '+(await r.text()))",
        'console.fallback.alert_name_required': "alert('Name is required')",
        'console.fallback.alert_save_fail': "alert('Save failed: '+(await r.text()))",
        'console.fallback.all_groups': '>All groups</option>',
        'console.fallback.all_verdicts': '>All verdicts</option>',
        'console.fallback.all_workers': '>All workers</option>',
        'console.fallback.answer_continue': '💬 Answer / resume</span>',
        # issue #116: dialog "answered by X at HH:MM" line + late-conflict notice
        'console.fallback.answered_line_expr': '\'<div class="dlg-meta" style="color:var(--yellow)">💬 Answered by \'+esc(t.answered_by)+\' at \'+esc(t.answered_at_hhmm)+\'</div>\'',
        'console.fallback.btn_answer': '>💬 Answer/resume</button>',
        'console.fallback.btn_copy_close_expr': "$('#dlgAOk').textContent='Copy & close';",
        'console.fallback.btn_generate_expr': "$('#dlgAOk').textContent='Generate';",
        'console.fallback.btn_restore_submit_expr': "btn.textContent='Submit & resume'",
        'console.fallback.btn_save_register_expr': "$('#dlgAOk').textContent='Save registration';",
        'console.fallback.btn_save_settings': 'onclick="saveSettings()">Save settings</button>',
        'console.fallback.btn_submitting_expr': "btn.textContent='Submitting…'",
        'console.fallback.cancel': '>Cancel</button>',
        'console.fallback.close': '>Close</button>',
        'console.fallback.col_action': '<th>Action</th>',
        'console.fallback.col_action_js': "'<th>Action</th>'",
        'console.fallback.col_detail': '<th>Detail</th>',
        'console.fallback.col_dir': '<th>Directory</th>',
        'console.fallback.col_offline': '<span style="color:var(--red)">● offline</span>',
        'console.fallback.col_online': '<span style="color:var(--green)">● online</span>',
        'console.fallback.col_online_status': '>Connection: ',
        'console.fallback.col_status': '<th>Status</th>',
        'console.fallback.col_task': '<th>Task</th>',
        'console.fallback.col_time': '<th>Time</th>',
        'console.fallback.confirm_delete_expr': "'Delete worker '+name+'?'",
        # issue #116: late-answer conflict notice (written into dlgErr)
        'console.fallback.conflict_notice_expr': "'⚠ Conflict: '+d.conflict+' (your reply was queued, but the other side already answered — confirm before acting)'",
        'console.fallback.delete': '>Delete</button>',
        'console.fallback.dlg_kind_blocked': '\'<div class="dlg-kind blocked">⛔ Review failed, awaiting decision</div>\'',
        'console.fallback.dlg_kind_clarify': '\'<div class="dlg-kind clarify">🤖 WORKER requests clarification</div>\'',
        'console.fallback.dlg_no_question_expr': "t.question||'(no detailed question recorded — possibly a legacy task)'",
        'console.fallback.dlg_opts_label': '\'<div class="dlg-opts-label">Options (click to select, or type a custom answer):</div><div class="dlg-opts">\'+',
        'console.fallback.err_answer_required': "'Answer cannot be empty'",
        'console.fallback.err_generate_fail_expr': "d.error||'generation failed'",
        'console.fallback.err_name_required_expr': "err.textContent='Name is required'",
        'console.fallback.err_save_fail_expr': "err.textContent='Save failed: '+(await r.text())",
        'console.fallback.err_submit_fail_expr': "'Submit failed: '+e.message",
        'console.fallback.fb_block': '// i18n v1.0: 本页文案由服务端按部署语言(MW_LANG)整页渲染;\n// page strings are rendered server-side from mw/i18n (deployment language,\n// MW_LANG). The language switch lives at the deploy layer.\n// See design.md §10 (i18n architecture summary).',
        'console.fallback.get_fail_expr': '`<span class="muted">fetch failed${d.actual_model_error?\' (\'+esc(d.actual_model_error)+\')\':\'\'}</span>`',
        'console.fallback.get_fail_suffix': '<span class="muted">(fetch failed)</span>',
        'console.fallback.group_attention': '>🔴 Attention</option>',
        'console.fallback.group_attention_js': "['attention','🔴 Attention']",
        'console.fallback.group_finished': '>✅ Finished</option>',
        'console.fallback.group_finished_js': "['finished','✅ Finished']",
        'console.fallback.group_running': '>🔵 Running</option>',
        'console.fallback.group_running_js': "['running','🔵 Running']",
        'console.fallback.h_public_settings': '⚙ Shared settings</h3>',
        'console.fallback.hint_hermes_home': '(worker Hermes dir · host path)',
        'console.fallback.html_lang': 'en',
        'console.fallback.lbl_actual_model': 'Effective model',
        'console.fallback.lbl_api_key': '>api_key *</label>',
        'console.fallback.lbl_api_key_at_least_16': '(≥16 chars)',
        'console.fallback.lbl_api_models': 'API model aliases',
        'console.fallback.lbl_api_url': '>api_url *</label>',
        'console.fallback.lbl_name_required': '>Name *</label>',
        'console.fallback.lbl_registry': 'Registry (registry.json)',
        'console.fallback.lbl_toolsets': 'Enabled toolsets',
        'console.fallback.lbl_worker_name': '>Worker name *</label>',
        'console.fallback.load_fail_expr': "='Load failed: '+e}",
        'console.fallback.loading': '\'<div class="muted">Loading…</div>\'',
        'console.fallback.msg_save_result_expr': "r.ok ? '✓ Saved' : '❌ '+(d.error||'Save failed')",
        'console.fallback.new_worker': 'onclick="openCompose()">🐳 New Worker (generate docker-compose)</button>',
        'console.fallback.next_steps_header': "'\\n\\n# Next steps:\\n'",
        'console.fallback.no_match': '\'<div class="empty">No matching tasks</div>\'',
        'console.fallback.no_session': '\'<span class="muted">no session</span>\'',
        'console.fallback.no_worker': '\'<div class="empty">No workers yet — register or create one above</div>\'',
        'console.fallback.offline': ":'offline'",
        'console.fallback.online_age_expr': "('online '+(w.reaper_heartbeat_age_s|0)+'s ago')",
        'console.fallback.ph_answer': 'placeholder="Type your answer / instruction; the worker session resumes with fresh context..."',
        'console.fallback.ph_api_key': 'placeholder="API key of the worker gateway"',
        'console.fallback.ph_compose_api_key': 'placeholder="Set an API key for this worker"',
        'console.fallback.ph_reviewer': 'placeholder="e.g. reviewer"',
        'console.fallback.ph_url': 'placeholder="http://hermes_<name>:8642"',
        'console.fallback.refresh': 'onclick="load()">Refresh</button>',
        'console.fallback.save': '>Save</button>',
        'console.fallback.search_placeholder': 'placeholder="🔍 Search task title / ID"',
        'console.fallback.sem_blocked': "sem:'⛔ Needs decision'}",
        'console.fallback.sem_clarify': "sem:'⚠ Needs answer'}",
        'console.fallback.sem_done': "sem:'Harvested'}",
        'console.fallback.sem_queued': "sem:t.status||'Queued'}",
        'console.fallback.sem_running': "sem:'Running'}",
        'console.fallback.setting_pair_default_image': "['Worker default image','Default image field when generating compose templates']",
        'console.fallback.setting_pair_default_timeout_seconds': '["Task timeout (s)","Dead-man\'s switch: settled by timeout only when a worker exceeds this; normal completion is event-driven"]',
        'console.fallback.setting_pair_docker_compose_dir': "['Worker compose directory','Recommended location for docker-compose.yaml files generated by the console']",
        'console.fallback.setting_pair_reaper_interval_seconds': "['Reaper polling interval (s)','Max delay of async settlement; independent of API read frequency (reads == settlements)']",
        'console.fallback.setting_pair_shared_workspace': "['Shared project workspace root','Common task/project directory for all workers (container path)']",
        'console.fallback.setting_pair_shared_workspace_host': "['Shared workspace host path','Mount source of the compose volume (absolute host path); used when generating compose']",
        'console.fallback.setting_pair_task_dir_base': "['Ad-hoc task directory base','Working directory for non-project tasks; lives under the shared workspace']",
        'console.fallback.settings_load_fail': '\'<div class="empty">Load failed: \'+esc(e.message)+\'</div>\'',
        'console.fallback.stats_line_expr': '`total ${tasksCache.length} · attention ${cnt.attention} · running ${cnt.running} · finished ${cnt.finished} · showing ${rows.length}`',
        'console.fallback.submit_continue': '>Submit & resume</button>',
        'console.fallback.subtitle': 'Simple-Agents Studio · long-running multi-agent task manager</span>',
        'console.fallback.tab_settings': '>⚙ Settings</div>',
        'console.fallback.tab_tasks': '>Tasks</div>',
        'console.fallback.tab_workers': '>Workers</div>',
        'console.fallback.tip_alias_vs_real': ' ← this is the request alias, not the real model; see the effective model above',
        'console.fallback.tip_compose': 'Generates compose file content from the template; starting the container is an explicit host-side operation this platform never performs.',
        'console.fallback.tip_no_autoretry_block': '⚠️ This system <b>deliberately offers no auto-retry</b>: a worker may have\n      modified some files before failing, and auto redispatch would cause duplicated work\n      and broken state. Failures only BLOCK; redispatch is an explicit decision by a human or upstream.',
        'console.fallback.tip_no_soul_block': 'ℹ️ SOUL.md and other profile files are deliberately hidden: the worker\n        gateway API exposes no file-read endpoint, and reading files would require\n        mounting the worker hermes_home, which conflicts with the mount-free isolation design.\n        To view it, run ',
        'console.fallback.tip_register': 'Registers into registry.json (routing table) only; no container is created — the worker container must already be running.',
        'console.fallback.tip_register_vs_create': 'Register = write into the routing table; New = generate a compose template, then start the container on the host',
        'console.fallback.title': '<title>Simple-Agents-Studio Console</title>',
        'console.fallback.title_compose_expr': "$('#dlgAWTitle').textContent='🐳 New Worker (generate docker-compose.yaml)';",
        'console.fallback.title_register_expr': "$('#dlgAWTitle').textContent='➕ Register Worker';",
        'console.fallback.verdict_blocked': '>Needs decision</option>',
        'console.fallback.verdict_clarify': '>Needs answer</option>',
        'console.fallback.verdict_harvested': '>Harvested</option>',
        'console.fallback.verdict_pending': '>Pending</option>',
        'console.fallback.view': '>👁 View</button>',
        'console.fallback.worker_detail': '👁 Worker detail</span>'
    },
}
