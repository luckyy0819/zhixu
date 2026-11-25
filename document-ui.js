/* ZHIXU-TASK005 W6/W7: document controls on the confirmed native UI. */
'use strict';
window.ZhixuDocuments=function({state,api,assertEpoch,el,note,report,ensureConversation,loadMessages,loadHistory,setView,accountDeleted}){
const $=id=>document.getElementById(id);
let files=[],selected=new Set(),uploads=[],autoSelect=new Set(),timer,previewFocus;
function reset(){clearTimeout(timer);files=[];selected.clear();uploads=[];autoSelect.clear();$('files').value='';$('attachments').replaceChildren();$('document-list').replaceChildren();$('doc-count').textContent='0';$('qa-mode').value='general';$('qa-intent').value='question';$('preview').classList.add('hidden');$('preview-title').textContent='';$('preview-meta').textContent='';$('preview-content').replaceChildren();$('delete-current-password').value='';}
function opened(items,defaults=true){files=items;const live=new Set(files.map(f=>f.id));selected=new Set([...selected].filter(id=>live.has(id)));for(const f of files){if(f.enabled&&f.status==='ready'&&(defaults||autoSelect.has(f.id))){selected.add(f.id);autoSelect.delete(f.id);}}if(defaults&&files.length)$('qa-mode').value='document';render();schedule();}
function schedule(){clearTimeout(timer);if(files.some(f=>['security_check','parsing','indexing'].includes(f.status))){const epoch=state.epoch,cid=state.current?.id;timer=setTimeout(()=>{if(epoch===state.epoch&&cid===state.current?.id)reload().catch(report);},1000);}}
async function reload(){if(!state.current)return;const epoch=state.epoch,cid=state.current.id;const data=await api('/conversations/'+cid+'/files');assertEpoch(epoch);if(state.current?.id===cid)opened(data.items,false);}
function render(){
 $('doc-count').textContent=String(files.length);const list=$('document-list'),chips=$('attachments');list.replaceChildren();chips.replaceChildren();
 const pendingUploads=uploads.filter(u=>u.status!=='done');
 if(!files.length&&!pendingUploads.length)list.append(el('div','empty-docs','上传PDF、DOCX、TXT或MD。资料保存在当前账号的本地服务端，解析后才可问答。'));
 for(const f of files){
  const row=el('div','document-row'),info=el('div','doc-info'),label=el('label','document-select');const box=el('input');box.type='checkbox';box.checked=selected.has(f.id);box.disabled=f.status!=='ready'||!f.enabled;box.setAttribute('aria-label','选择 '+f.name);box.onchange=()=>{if(box.checked)selected.add(f.id);else selected.delete(f.id);$('qa-mode').value='document';render();};label.append(box,el('strong','',f.name));
  const status=({security_check:'安全检查中',parsing:'解析中',indexing:'建立索引中',ready:'可用',failed:'处理失败',cancelled:'已中止'})[f.status]||f.status;
  info.append(label,el('p','',status+' · 版本'+f.version+(f.enabled?' · 已启用':' · 已禁用')));
  if(f.job?.error)info.append(el('p','error-message',f.job.error.code+' · '+f.job.error.message));
  if(f.warnings?.length)info.append(el('p','error-message',f.warnings.map(w=>w.message+(w.pages?.length?'：'+w.pages.join(','):'')).join('；')));
  const actions=el('div','doc-buttons');const preview=el('button','secondary','查看原文');preview.disabled=f.status!=='ready';preview.onclick=()=>openFile(f).catch(report);actions.append(preview);
  const enable=el('button','secondary',f.enabled?'禁用资料':'启用资料');enable.onclick=async()=>{const epoch=state.epoch;try{await api('/conversations/'+f.conversation_id+'/files/'+f.id,{method:'PATCH',body:{enabled:!f.enabled,revision:f.enabled_revision}});assertEpoch(epoch);if(f.enabled)selected.delete(f.id);await reload();}catch(e){report(e);}};actions.append(enable);
  if(f.job?.status==='queued'||f.job?.status==='running'){const stop=el('button','secondary','停止解析');stop.onclick=()=>api('/files/'+f.id+'/cancel',{method:'POST',body:{}}).then(reload).catch(report);actions.append(stop);}
  if(f.job?.error?.retryable){const retry=el('button','secondary','重试解析');let key=crypto.randomUUID();retry.onclick=()=>api('/files/'+f.id+'/retry',{method:'POST',body:{},key}).then(reload).catch(report);actions.append(retry);}
  const remove=el('button','danger','删除资料');remove.onclick=()=>removeFile(f).catch(report);actions.append(remove);row.append(info,actions);list.append(row);
  const chip=el('button','file-chip', (selected.has(f.id)?'☑ ':'□ ')+f.name+' · '+status);chip.type='button';chip.onclick=()=>setView('docs');chips.append(chip);
 }
 for(const item of pendingUploads){const row=el('div','document-row');row.append(el('strong','',item.file.name),el('span',item.status==='failed'?'error-message':'muted',item.error||'上传中 · 未提供虚构百分比'));if(item.status==='failed'){const retry=el('button','secondary','重试上传');retry.onclick=()=>uploadItem(item).catch(report);row.append(retry);}list.append(row);}
}
async function uploadItem(item){
 const epoch=state.epoch;item.status='uploading';item.error='';render();
 try{const data=await api('/conversations/'+item.cid+'/files',{method:'POST',key:item.key,raw:item.file,extraHeaders:{'Content-Type':item.file.type||'application/octet-stream','X-File-Name':encodeURIComponent(item.file.name),'X-File-Size':String(item.file.size)}});assertEpoch(epoch);item.status='done';autoSelect.add(data.file.id);if(state.current?.id===item.cid){$('qa-mode').value='document';await reload();}}
 catch(error){if(error.stale)throw error;item.status='failed';item.error=(error.code?error.code+' · ':'')+error.message;report(error);}
 assertEpoch(epoch);render();
}
async function addFiles(list){
 if(!state.user)throw new Error('请先登录');const epoch=state.epoch;const conv=await ensureConversation();assertEpoch(epoch);const items=[];
 for(const file of Array.from(list)){if(!/\.(pdf|docx|txt|md)$/i.test(file.name)||file.size===0||file.size>20000000){note(file.name+'：格式须为PDF/DOCX/TXT/MD，且为1—20,000,000字节',true);continue;}const item={file,cid:conv.id,key:crypto.randomUUID(),status:'pending'};uploads.push(item);items.push(item);}
 for(let i=0;i<items.length;i+=2){assertEpoch(epoch);await Promise.allSettled(items.slice(i,i+2).map(uploadItem));}
 assertEpoch(epoch);$('files').value='';await loadHistory();
}
function body(){const mode=$('qa-mode').value;const refs=mode==='document'?files.filter(f=>selected.has(f.id)&&f.enabled&&f.status==='ready').map(f=>({file_id:f.id,version:f.version})):[];if(mode==='document'&&!refs.length)throw new Error('请选择至少一份已启用且解析完成的资料，不会自动降为通用问答');return {mode,intent:$('qa-intent').value,file_refs:refs};}
async function removeFile(file){
 if(!confirm('删除此资料及索引？历史回答可能仍含摘录，但引用和后续模型使用会失效。此操作不能撤销。'))return;
 const epoch=state.epoch;const data=await api('/files/'+file.id,{method:'DELETE',body:{confirmation:'DELETE'}});assertEpoch(epoch);selected.delete(file.id);await reload();assertEpoch(epoch);await loadMessages(file.conversation_id);assertEpoch(epoch);note('访问已撤销；物理清理状态：'+data.status);$('preview').classList.add('hidden');
}
async function preview(path){const epoch=state.epoch;previewFocus=document.activeElement;const data=await api(path);assertEpoch(epoch);$('preview-title').textContent=data.name;$('preview-meta').textContent='版本'+data.version+' · '+(data.page?'第'+data.page+'页':'段落'+data.paragraph)+' · '+data.parser_revision;const root=$('preview-content');root.replaceChildren();const chars=Array.from(data.text),h=data.highlight;
 if(h){const a=h.start-data.text_start,b=h.end-data.text_start;root.append(document.createTextNode(chars.slice(0,a).join('')),el('mark','',chars.slice(a,b).join('')),document.createTextNode(chars.slice(b).join('')));}else root.textContent=data.text;
 const nav=el('div','preview-navigation');for(const [key,title] of [['previous_paragraph','上一段'],['next_paragraph','下一段']]){if(data[key]){const button=el('button','secondary',title);button.onclick=()=>preview('/files/'+data.file_id+'/preview?version='+data.version+'&paragraph='+data[key]).catch(report);nav.append(button);}}root.append(nav);$('preview').classList.remove('hidden');$('preview-close').focus();
}
function openFile(file){return preview('/files/'+file.id+'/preview?version='+file.version+'&paragraph=1');}
function claims(message,container){if(!message.claims?.length)return false;for(const claim of message.claims){const block=el('section','claim');block.append(el('small','claim-kind',({fact:'原文事实',inference:'推断',no_answer:'资料局限'})[claim.kind]));block.append(el('p','',claim.text));for(const c of (message.citations||[]).filter(c=>c.claim_index===claim.claim_index)){const button=el('button','citation',c.source_status==='deleted'?'资料已删除 · 引用失效':'来源 · 版本'+c.version+' · '+(c.page?'第'+c.page+'页':'第'+c.paragraph+'段')+(c.source_status==='disabled'?' · 已禁用':''));button.disabled=c.source_status==='deleted';button.onclick=()=>preview('/citations/'+c.id+'/preview').catch(async error=>{report(error);if(!error.stale&&state.current)await loadMessages(state.current.id);});block.append(button);}container.append(block);}return true;}
function bind(){
 for(const id of ['sample-card','sample-start'])$(id).onclick=()=>setView('docs');
 for(const id of ['attach','docs-upload']){$(id).disabled=false;$(id).onclick=()=>$('files').click();}
 $('files').onchange=e=>addFiles(e.target.files).catch(report);$('composer').addEventListener('dragover',e=>e.preventDefault());$('composer').addEventListener('drop',e=>{e.preventDefault();addFiles(e.dataTransfer.files).catch(report);});
 $('docs-summary').onclick=()=>{$('qa-mode').value='document';$('qa-intent').value='summary';$('prompt').value='请总结所选资料，标明依据与资料局限。';setView('chat');$('prompt').focus();};
 $('preview-close').onclick=()=>{$('preview').classList.add('hidden');previewFocus?.focus();};
 $('delete-account-form').onsubmit=async e=>{e.preventDefault();if($('delete-confirmation').value!=='DELETE')return;const epoch=state.epoch;const saved=sessionStorage.getItem('zhixu-deletion');let pending=saved?JSON.parse(saved):null;if(!pending){const bytes=crypto.getRandomValues(new Uint8Array(32));pending={id:crypto.randomUUID(),token:btoa(String.fromCharCode(...bytes)).replaceAll('+','-').replaceAll('/','_').replaceAll('=','')};sessionStorage.setItem('zhixu-deletion',JSON.stringify(pending));}const password=$('delete-current-password').value;$('delete-current-password').value='';$('delete-account').disabled=true;try{const data=await api('/account/data',{method:'DELETE',body:{current_password:password,confirmation:'DELETE',deletion_key:pending.id,receipt_token:pending.token}});assertEpoch(epoch);accountDeleted();showDeletion(data);}catch(error){report(error);$('deletion-status').textContent='删除结果尚未确认。可查询预持回执；404表示尚未建立任务，需登录后重试。';}finally{$('delete-account').disabled=false;}};
 for(const id of ['deletion-query','auth-deletion-query'])$(id).onclick=()=>queryDeletion().catch(error=>{report(error);$('auth-deletion-status').textContent='状态未确认：'+error.message;});
}
function showDeletion(data){for(const id of ['deletion-status','auth-deletion-status'])$(id).textContent='删除任务：'+data.status+(data.error_code?' · '+data.error_code:'');if(data.status==='completed')sessionStorage.removeItem('zhixu-deletion');}
async function queryDeletion(){const raw=sessionStorage.getItem('zhixu-deletion');if(!raw){$('deletion-status').textContent='没有待查询的删除回执';return;}const saved=JSON.parse(raw);showDeletion(await api('/deletions/'+saved.id,{extraHeaders:{'X-Deletion-Receipt':saved.token}}));}
bind();return {reset,opened,body,claims,reload,addFiles};
};
