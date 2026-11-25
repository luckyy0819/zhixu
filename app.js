/* ZHIXU-TASK-005 W6: real server-backed UI; prototype retained in BL001. */
(() => {
'use strict';
const $=id=>document.getElementById(id), core=window.ZhixuCore;
const state={epoch:0,controllers:new Set(),user:null,csrf:'',conversations:[],current:null,messages:[],generations:new Map(),streams:new Map(),view:'chat',composing:false,pending:null};
let toastTimer,authMode='login',historyTimer,newKey=crypto.randomUUID();
function el(tag,cls,text){const n=document.createElement(tag);if(cls)n.className=cls;if(text!==undefined)n.textContent=text;return n;}
function note(text,error=false){$('toast').textContent=text;$('toast').classList.add('show');clearTimeout(toastTimer);toastTimer=setTimeout(()=>$('toast').classList.remove('show'),4500);if(error)$('connection-state').textContent=text;}
function assertEpoch(epoch){if(epoch!==state.epoch)throw Object.assign(new Error('已丢弃旧账号响应'),{stale:true});}
function report(error){if(!error.stale)note((error.code?error.code+' · ':'')+error.message,true);}
async function api(path,{method='GET',body,key,recover=true,raw=null,extraHeaders={}}={}){
  const epoch=state.epoch,controller=new AbortController();state.controllers.add(controller);
  try{
  const headers={};if(method!=='GET'){headers['Content-Type']='application/json';headers['X-CSRF-Token']=state.csrf;}
  Object.assign(headers,extraHeaders);
  if(key)headers['Idempotency-Key']=key;
  let r;try{r=await fetch('/api/v1'+path,{method,headers,body:raw||(body===undefined?undefined:JSON.stringify(body)),credentials:'same-origin',cache:'no-store',signal:controller.signal});}catch{assertEpoch(epoch);throw new Error('网络连接失败；写入重试将沿用原请求编号');}
  assertEpoch(epoch);const value=await r.json();assertEpoch(epoch);
  if(!r.ok){const e=Object.assign(new Error(value.error?.message||'请求失败'),value.error||{});
    if(e.code==='CSRF_INVALID'&&recover&&state.user){const me=await api('/auth/me');assertEpoch(epoch);state.csrf=me.csrf_token;return api(path,{method,body,key,recover:false,raw,extraHeaders});}
    if(r.status===401&&state.user){clearAccount();showAuth();}throw e;
  }return value.data;
  }finally{state.controllers.delete(controller);}
}
function closeMenu(){$('sidebar').classList.remove('open');$('scrim').classList.add('hidden');}
function clearAccount(){state.epoch++;for(const c of state.controllers)c.abort();state.controllers.clear();clearTimeout(historyTimer);for(const s of state.streams.values()){s.closed=true;s.source?.close();clearTimeout(s.timer);}state.streams.clear();state.generations.clear();state.user=null;state.csrf='';state.current=null;state.conversations=[];state.messages=[];state.pending=null;documents?.reset();$('profile-name').textContent='未登录';$('connection-state').replaceChildren();$('auth-submit').disabled=false;renderHistory();renderMessages();}
function showAuth(){if(!$('auth-dialog').open)$('auth-dialog').showModal();$('auth-username').focus();}
async function loadHistory(){const epoch=state.epoch;let cursor=null,items=[];do{const data=await api('/conversations?limit=100&q='+encodeURIComponent($('history-search').value)+(cursor?'&cursor='+encodeURIComponent(cursor):''));assertEpoch(epoch);items.push(...data.items);cursor=data.next_cursor;}while(cursor);assertEpoch(epoch);state.conversations=items;renderHistory();}
function renderHistory(){const root=$('history');root.replaceChildren();if(!state.conversations.length)root.append(el('div','empty-history','还没有对话记录\n从一个好问题开始吧'));for(const c of state.conversations){const row=el('div','history-item'+(state.current?.id===c.id?' selected':''));const button=el('button','history-title',c.title);button.onclick=()=>openConversation(c.id).catch(report);const rename=el('button','delete-chat','✎');rename.setAttribute('aria-label','重命名 '+c.title);rename.onclick=async()=>{const title=prompt('会话名称（1—80字）',c.title);if(title===null)return;try{await api('/conversations/'+c.id,{method:'PATCH',body:{title,revision:c.revision}});await loadHistory();}catch(e){report(e);await loadHistory();}};const remove=el('button','delete-chat','×');remove.setAttribute('aria-label','删除会话 '+c.title);remove.onclick=async()=>{if(!confirm('删除此会话全部消息和资料？此操作不能撤销。'))return;const epoch=state.epoch;try{const data=await api('/conversations/'+c.id,{method:'DELETE',body:{confirmation:'DELETE',include_files:true}});assertEpoch(epoch);for(const [rid,g] of state.generations)if(g.conversation_id===c.id){stopSubscription(rid);state.generations.delete(rid);}if(state.current?.id===c.id)newChat();await loadHistory();assertEpoch(epoch);note('会话访问已撤销；物理清理状态：'+data.status);}catch(error){report(error);}};row.append(button,rename,remove);root.append(row);}}
function setView(view){state.view=view;const conversation=view==='chat'&&state.current&&state.messages.length>0;$('welcome').classList.toggle('hidden',view!=='chat'||!!conversation);$('chat').classList.toggle('hidden',!conversation);$('documents').classList.toggle('hidden',view!=='docs');$('page-title').textContent=view==='docs'?'文档工作台':'智能问答';$(conversation?'composer-chat':'composer-home').append($('composer'));$('nav-chat').classList.toggle('active',view==='chat');$('nav-docs').classList.toggle('active',view==='docs');closeMenu();updateBusy();}
async function openConversation(id){const epoch=state.epoch;const data=await api('/conversations/'+id);assertEpoch(epoch);state.current=data.conversation;documents?.opened(data.files||[]);localStorage.setItem('zhixu-current-id',id);state.pending=null;await loadMessages(id);assertEpoch(epoch);if(state.current?.id!==id)return;setView('chat');renderHistory();if(data.conversation.active_request_id)subscribe(data.conversation.active_request_id).catch(report);}
async function loadMessages(cid){const epoch=state.epoch;let cursor=null,items=[];do{const data=await api('/conversations/'+cid+'/messages?limit=100'+(cursor?'&cursor='+encodeURIComponent(cursor):''));assertEpoch(epoch);items.push(...data.items);cursor=data.next_cursor;}while(cursor);if(state.current?.id===cid){state.messages=items;for(const m of items){const rid=readRequestMap()[m.id];if(rid){try{const snap=await api('/requests/'+rid);assertEpoch(epoch);putSnapshot(snap);}catch(e){if(e.stale)throw e;report(e);}}}assertEpoch(epoch);renderMessages();}}
function newChat(){state.current=null;state.messages=[];state.pending=null;documents?.reset();newKey=crypto.randomUUID();localStorage.removeItem('zhixu-current-id');$('prompt').value='';setView('chat');renderHistory();renderMessages();$('prompt').focus();}
async function ensureConversation(){const epoch=state.epoch;if(!state.current){const created=await api('/conversations',{method:'POST',body:{},key:newKey});assertEpoch(epoch);state.current=created;localStorage.setItem('zhixu-current-id',created.id);}return state.current;}
function updateBusy(){const busy=!!state.current?.active_request_id;$('send').textContent=busy?'■':'↑';$('send').setAttribute('aria-label',busy?'停止生成':'发送问题');$('prompt').disabled=false;}
const escapeHTML=text=>text.replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
marked.use({renderer:{html(token){return escapeHTML(token.text);}}});
function markdown(root,text){
  root.innerHTML=DOMPurify.sanitize(marked.parse(text),{ALLOWED_TAGS:['p','br','strong','em','del','ul','ol','li','blockquote','pre','code','h1','h2','h3','h4','hr','table','thead','tbody','tr','th','td','a'],ALLOWED_ATTR:['href','title'],ALLOW_DATA_ATTR:false,ALLOWED_URI_REGEXP:/^https?:/i});
  for(const a of root.querySelectorAll('a')){
    if(!/^https?:\/\//i.test(a.getAttribute('href')||''))a.removeAttribute('href');
    else{a.target='_blank';a.rel='noopener noreferrer';}
  }
}
function renderMessages(){
  const root=$('messages');root.replaceChildren();
  for(const m of state.messages){
    const row=el('div','message '+m.role);row.dataset.messageId=m.id;
    if(m.role==='user')row.append(el('div','user-bubble',m.content));
    else{
      const label=el('div','assistant-label','✳ 知序');label.append(el('small','',m.model||'真实问答'));
      const answer=el('div','answer-text');if(!documents?.claims(m,answer))markdown(answer,m.content);
      const status=el('div','message-status',({queued:'等待生成',running:'正在生成',completed:'已完成',cancelled:'已中止 · 已保留部分正文',failed:'生成失败 · 请检查错误后重试',interrupted:'服务中断 · 可重试'})[m.status]||m.status);
      row.append(label,answer,status);
      const gen=[...state.generations.values()].find(g=>g.assistant_message_id===m.id);
      if(gen?.coverage?.files){const c=gen.coverage;row.append(el('p','coverage-note','资料覆盖：'+(c.selection==='sampled_summary'?'抽样摘要':'相关片段检索')+' · 选取'+c.selected_chunks+'个片段 · 未纳入'+c.omitted_chunks+'个候选片段。非全文覆盖，请打开引用核对。'));}if(gen?.error)row.append(el('p','error-message',gen.error.code+' · '+gen.error.message));
      const tools=el('div','message-tools'),copy=el('button','','复制回答');
      copy.onclick=async()=>{try{await navigator.clipboard.writeText(m.content);note('已复制回答');}catch{note('复制失败，请手动选择文字',true);}};
      tools.append(copy);
      if(['failed','cancelled','interrupted'].includes(m.status)){
        const retry=el('button','','重试此问题');retry.disabled=!!state.current?.active_request_id;
        retry.onclick=()=>retryMessage(m).catch(report);tools.append(retry);
      }
      row.append(tools);
    }
    root.append(row);
  }
  root.scrollTop=root.scrollHeight;updateBusy();
}
function putSnapshot(snapshot){
  const gen={...snapshot,seq:snapshot.last_seq};state.generations.set(gen.id,gen);
  try{const ids=readRequestMap();ids[gen.assistant_message_id]=gen.id;localStorage.setItem('zhixu-request-ids-'+state.user.id,JSON.stringify(ids));}catch{/* IDs only; lack of browser storage does not discard server messages. */}
  if(state.current?.id===gen.conversation_id){
    const m=state.messages.find(m=>m.id===gen.assistant_message_id);
    if(m){m.content=gen.content;m.status=gen.status;m.claims=gen.claims||[];m.citations=gen.citations||[];}
    state.current.active_request_id=['queued','running'].includes(gen.status)?gen.id:null;
    renderMessages();
  }
  return gen;
}
function readRequestMap(){try{return JSON.parse(localStorage.getItem('zhixu-request-ids-'+state.user.id)||'{}');}catch{return {};}}
function stopSubscription(rid){const s=state.streams.get(rid);if(s){s.closed=true;s.source?.close();clearTimeout(s.timer);state.streams.delete(rid);}}
async function subscribe(rid,attempt=0){
  const epoch=state.epoch;
  stopSubscription(rid);
  const snapshot=await api('/requests/'+rid);assertEpoch(epoch);const gen=putSnapshot(snapshot);
  if(!['queued','running'].includes(gen.status)){await loadHistory();return;}
  const sub={closed:false,source:null,timer:null};state.streams.set(rid,sub);
  const source=new EventSource('/api/v1/requests/'+rid+'/events?after='+gen.seq);sub.source=source;
  const types=['meta','status','delta','citation','done','error','cancelled'];
  for(const type of types)source.addEventListener(type,async event=>{
    if(sub.closed||epoch!==state.epoch||!event.data)return;
    try{
      const value=JSON.parse(event.data);
      if(value.request_id!==rid)throw new Error('响应编号不匹配');
      core.applyEvent(gen,value);
      if(state.current?.id===gen.conversation_id){const m=state.messages.find(m=>m.id===gen.assistant_message_id);if(m){m.content=gen.content;m.status=gen.status;m.claims=gen.claims||[];m.citations=gen.citations||[];}renderMessages();}
      if(['done','error','cancelled'].includes(type)){
        stopSubscription(rid);const snapshot=await api('/requests/'+rid);assertEpoch(epoch);putSnapshot(snapshot);
        $('connection-state').textContent=gen.error?gen.error.code+' · '+gen.error.message:'生成已结束；对话已保存到本地服务';
        await loadHistory();
      }
    }catch(e){source.close();if(!e.stale)scheduleReconnect(rid,sub,attempt,e);}
  });
  source.onerror=()=>scheduleReconnect(rid,sub,attempt,new Error('订阅连接中断；服务端生成继续'));
}
function scheduleReconnect(rid,sub,attempt,error){
  if(sub.closed)return;
  sub.closed=true;sub.source?.close();
  $('connection-state').replaceChildren(document.createTextNode(error.message));
  if(attempt>=4){const b=el('button','secondary','重新连接');b.onclick=()=>subscribe(rid).catch(report);$('connection-state').append(b);return;}
  sub.timer=setTimeout(()=>subscribe(rid,attempt+1).catch(report),[1000,2000,5000,10000][attempt]);
}
async function submitQuestion(){
  const epoch=state.epoch;
  if(!state.user){showAuth();return;}
  if(state.current?.active_request_id){
    await api('/requests/'+state.current.active_request_id+'/cancel',{method:'POST',body:{}});
    assertEpoch(epoch);
    note('已请求中止，等待上游连接关闭');return;
  }
  const content=$('prompt').value.trim();if(!content){note('请先写下问题');return;}
  await ensureConversation();assertEpoch(epoch);
  const payload={content,...(documents?.body()||{mode:'general',file_refs:[],intent:'question'})},signature=JSON.stringify(payload);
  if(!state.pending||state.pending.signature!==signature)state.pending={content,signature,key:crypto.randomUUID()};
  const data=await api('/conversations/'+state.current.id+'/messages',{method:'POST',key:state.pending.key,body:payload});
  assertEpoch(epoch);
  state.pending=null;$('prompt').value='';state.current.active_request_id=data.generation.id;
  $('connection-state').textContent='正在生成；离开页面只断开订阅，不会自动取消';
  await loadMessages(state.current.id);assertEpoch(epoch);setView('chat');await loadHistory();assertEpoch(epoch);await subscribe(data.generation.id);
}
async function retryMessage(message){
  const epoch=state.epoch;
  let gen=[...state.generations.values()].find(g=>g.assistant_message_id===message.id);
  if(!gen){const conv=await api('/conversations/'+message.conversation_id);assertEpoch(epoch);if(conv.conversation.active_request_id)throw new Error('当前会话仍在生成');
    throw new Error('此浏览器没有该旧回答的请求编号；回答仍保留，可发送新问题。');
  }
  const key=message.retryKey||(message.retryKey=crypto.randomUUID());
  const path='/messages/'+message.parent_user_message_id+'/retry',body={failed_request_id:gen.id,scope_policy:'current'};let data;
  try{data=await api(path,{method:'POST',key,body});}catch(error){
    assertEpoch(epoch);if(error.code!=='SCOPE_CHANGED')throw error;
    const refs=error.details?.available_file_refs||[];
    if(!refs.length)throw new Error('原资料范围已全部不可用；请选择资料并发送新问题，不会改为通用问答。');
    if(!confirm('原资料范围有变化。仅使用仍可用的'+refs.length+'份资料重试？已删除或禁用资料将被排除。'))return;
    data=await api(path,{method:'POST',key:crypto.randomUUID(),body:{...body,confirmed_file_refs:refs}});
  }
  assertEpoch(epoch);state.current.active_request_id=data.generation.id;await loadMessages(state.current.id);assertEpoch(epoch);await subscribe(data.generation.id);
}
const documents=window.ZhixuDocuments?.({state,api,assertEpoch,el,note,report,ensureConversation,loadMessages,loadHistory,setView,accountDeleted:()=>{clearAccount();$('settings').close();setView('chat');showAuth();}});
function unavailable(){note('文档组件未加载，请重新载入页面',true);}
$('composer').addEventListener('submit',e=>{e.preventDefault();submitQuestion().catch(report);});
$('prompt').addEventListener('compositionstart',()=>state.composing=true);
$('prompt').addEventListener('compositionend',()=>state.composing=false);
$('prompt').addEventListener('keydown',e=>{if(core.shouldSubmit(e,state.composing)){e.preventDefault();submitQuestion().catch(report);}});
$('new-chat').onclick=newChat;document.querySelector('.brand').onclick=e=>{e.preventDefault();newChat();};
$('nav-chat').onclick=()=>setView('chat');$('nav-docs').onclick=()=>setView('docs');
if(!documents)for(const id of ['attach','docs-upload'])$(id).onclick=unavailable;
document.querySelectorAll('[data-prompt]').forEach(b=>b.onclick=()=>{$('prompt').value=b.dataset.prompt;$('prompt').focus();});
$('search-toggle').onclick=()=>{$('history-search').classList.toggle('hidden');if(!$('history-search').classList.contains('hidden'))$('history-search').focus();};
$('history-search').oninput=()=>{clearTimeout(historyTimer);historyTimer=setTimeout(()=>loadHistory().catch(report),250);};
$('settings-open').onclick=()=>$('settings').showModal();$('settings-close').onclick=()=>$('settings').close();
$('logout').onclick=async()=>{const epoch=state.epoch;try{await api('/auth/logout',{method:'POST',body:{}});assertEpoch(epoch);clearAccount();$('settings').close();setView('chat');showAuth();}catch(e){report(e);}};
$('help').onclick=()=>note('使用本地账号登录；通用回答来自真实模型。断开页面不取消生成，停止按钮主动中止。');
if(!documents)$('preview-close').onclick=()=>$('preview').classList.add('hidden');$('menu').onclick=()=>{$('sidebar').classList.add('open');$('scrim').classList.remove('hidden');};$('scrim').onclick=closeMenu;
document.addEventListener('keydown',e=>{if((e.ctrlKey||e.metaKey)&&e.key.toLowerCase()==='k'){e.preventDefault();newChat();}if(e.key==='Escape'){$('preview').classList.add('hidden');closeMenu();}});
$('auth-dialog').addEventListener('cancel',e=>{if(!state.user)e.preventDefault();});
$('auth-switch').onclick=()=>{authMode=authMode==='login'?'register':'login';$('auth-title').textContent=authMode==='login'?'登录个人空间':'创建本地账号';$('auth-name-row').hidden=authMode==='login';$('auth-display-name').required=authMode==='register';$('auth-submit').textContent=authMode==='login'?'登录':'注册并登录';$('auth-switch').textContent=authMode==='login'?'没有账号？创建账号':'已有账号？登录';$('auth-password').autocomplete=authMode==='login'?'current-password':'new-password';};
$('auth-form').onsubmit=async e=>{
  let epoch=state.epoch;
  e.preventDefault();$('auth-submit').disabled=true;$('auth-error').textContent='';
  try{
    const nonce=await api('/auth/csrf');assertEpoch(epoch);state.csrf=nonce.csrf_token;
    const body={username:$('auth-username').value,password:$('auth-password').value};
    if(authMode==='register')body.display_name=$('auth-display-name').value;
    const data=await api('/auth/'+authMode,{method:'POST',body,recover:false});
    assertEpoch(epoch);clearAccount();epoch=state.epoch;state.user=data.user;state.csrf=data.csrf_token;$('auth-password').value='';$('auth-dialog').close();
    $('profile-name').textContent=data.user.display_name;await loadHistory();assertEpoch(epoch);newChat();$('connection-state').textContent='本地个人空间 · 已登录';
  }catch(err){if(!err.stale)$('auth-error').textContent=(err.code?err.code+' · ':'')+err.message;}finally{if(epoch===state.epoch)$('auth-submit').disabled=false;}
};
async function init(){
  const epoch=state.epoch;
  renderHistory();setView('chat');
  try{const data=await api('/auth/me');assertEpoch(epoch);state.user=data.user;state.csrf=data.csrf_token;$('profile-name').textContent=data.user.display_name;await loadHistory();assertEpoch(epoch);const id=localStorage.getItem('zhixu-current-id');if(id)try{await openConversation(id);}catch(e){if(e.stale)throw e;report(e);newChat();}}
  catch(e){if(!e.stale)showAuth();}
}
init().catch(report);
})();
