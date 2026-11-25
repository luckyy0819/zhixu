const {test}=require('node:test'),assert=require('node:assert/strict');
const {JSDOM}=require('jsdom'),fs=require('node:fs'),path=require('node:path');
const root=path.resolve(__dirname,'../..'),tick=()=>new Promise(r=>setImmediate(r));
for(const staleKind of ['history','snapshot','old401'])test('F01 discards old account '+staleKind+' after B signs in',async()=>{
 const dom=new JSDOM(fs.readFileSync(path.join(root,'index.html'),'utf8'),{url:'http://127.0.0.1:8787',runScripts:'outside-only'});
 const w=dom.window,d=w.document;let who='A',hold=false,release,streamCount=0;
 const users={A:{id:'user-a',display_name:'A'},B:{id:'user-b',display_name:'B'}};
 w.HTMLDialogElement.prototype.showModal=function(){this.setAttribute('open','');};w.HTMLDialogElement.prototype.close=function(){this.removeAttribute('open');};
 w.EventSource=class{constructor(){streamCount++;}addEventListener(){}close(){}};
 const delayed=new Promise(r=>release=r);
 w.fetch=async(url,opts={})=>{
  const p=url.replace('/api/v1','').split('?')[0];let data,ok=true,status=200,later=false;
  if(p==='/auth/me')data={user:users[who],csrf_token:'csrf'};
  else if(p==='/auth/csrf')data={csrf_token:'nonce'};
  else if(p==='/auth/logout')data={logged_out:true};
  else if(p==='/auth/login'){who='B';data={user:users.B,csrf_token:'csrf-b'};}
  else if(p==='/conversations'){
   data={items:[{id:who+'-conv',title:who+'-PRIVATE-TITLE',revision:1,active_request_id:null}],next_cursor:null};
   if(hold&&who==='A'&&staleKind!=='snapshot'){later=true;if(staleKind==='old401'){ok=false;status=401;}}
  }else if(p==='/conversations/A-conv')data={conversation:{id:'A-conv',title:'A-PRIVATE-TITLE',active_request_id:'A-request'},files:[]};
  else if(p==='/conversations/A-conv/messages')data={items:[],next_cursor:null};
  else if(p==='/requests/A-request'){data={id:'A-request',conversation_id:'A-conv',assistant_message_id:'A-msg',content:'A-PRIVATE-BODY',status:'running',last_seq:1};later=true;}
  else throw new Error('unexpected '+url);
  return {ok,status,json:async()=>{if(later)await delayed;return ok?{data}:{error:{code:'AUTH_REQUIRED',message:'old A expired'}};}};
 };
 for(const file of ['vendor/marked.js','vendor/purify.js','client-core.js','app.js'])w.eval(fs.readFileSync(path.join(root,file),'utf8')+'\n//# sourceURL='+path.join(root,file).replaceAll('\\','/'));
 for(let i=0;i<6;i++)await tick();hold=true;
 if(staleKind==='snapshot'){d.querySelector('.history-title').click();for(let i=0;i<6;i++)await tick();}
 else{d.getElementById('history-search').value='filter';d.getElementById('history-search').dispatchEvent(new w.Event('input'));await new Promise(r=>setTimeout(r,275));}
 d.getElementById('logout').click();for(let i=0;i<6;i++)await tick();
 const clearedName=d.getElementById('profile-name').textContent;
 d.getElementById('auth-username').value='user_b';d.getElementById('auth-password').value='synthetic-pass-123';d.getElementById('auth-form').dispatchEvent(new w.Event('submit',{cancelable:true}));
 for(let i=0;i<8;i++)await tick();assert.equal(d.getElementById('profile-name').textContent,'B');
 release();for(let i=0;i<10;i++)await tick();
 assert(!d.getElementById('history').textContent.includes('A-PRIVATE'));
 assert(!d.getElementById('messages').textContent.includes('A-PRIVATE'));
 assert.equal(d.getElementById('profile-name').textContent,'B');assert.equal(streamCount,0);
 assert(!d.getElementById('auth-dialog').open,'old 401 must not log B out');
 assert.equal(clearedName,'未登录');
 dom.window.close();
});
