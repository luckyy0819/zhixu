const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const {JSDOM}=require('jsdom');
const root=path.resolve(__dirname,'../..');
const tick=()=>new Promise(resolve=>setImmediate(resolve));

test('real frontend code wires auth, persistent history, SSE, cancel and safe Markdown',async()=>{
  const dom=new JSDOM(fs.readFileSync(path.join(root,'index.html'),'utf8'),{url:'http://127.0.0.1:8787',runScripts:'outside-only'});
  const w=dom.window, doc=w.document, requests=[];let logged=false,failWrite=false;
  w.HTMLDialogElement.prototype.showModal=function(){this.setAttribute('open','');};
  w.HTMLDialogElement.prototype.close=function(){this.removeAttribute('open');};
  w.confirm=()=>true;w.prompt=()=> 'Renamed';
  Object.defineProperty(w.navigator,'clipboard',{value:{writeText:async()=>{}}});
  const conv={id:'conv-a',title:'hello',revision:1,active_request_id:null};
  const user={id:'u-a',username:'user_a',display_name:'合成A'};
  const message={id:'answer-a',role:'assistant',conversation_id:'conv-a',content:'',status:'running',parent_user_message_id:'user-message',model:'test'};
  const generation={id:'request-a',conversation_id:'conv-a',assistant_message_id:'answer-a',content:'',status:'running',last_seq:1,error:null};
  class Source{
    static instances=[];
    constructor(url){this.url=url;this.handlers={};Source.instances.push(this);}
    addEventListener(type,fn){this.handlers[type]=fn;}
    close(){this.closed=true;}
    async event(type,seq,payload){await this.handlers[type]({data:JSON.stringify({type,seq,request_id:'request-a',payload})});}
  }
  w.EventSource=Source;
  w.fetch=async(url,options={})=>{
    requests.push({url,...options});let data,ok=true,error;
    const p=url.replace('/api/v1','').split('?')[0];
    if(p==='/auth/me') {if(!logged){ok=false;error={code:'AUTH_REQUIRED',message:'请先登录'};}else data={user,csrf_token:'csrf'};}
    else if(p==='/auth/csrf')data={csrf_token:'nonce'};
    else if(p==='/auth/register'||p==='/auth/login'){logged=true;data={user,csrf_token:'csrf'};}
    else if(p==='/auth/logout'){logged=false;data={logged_out:true};}
    else if(p==='/conversations'&&options.method==='POST')data=conv;
    else if(p==='/conversations')data={items:[conv],next_cursor:null};
    else if(p==='/conversations/conv-a/messages'&&options.method==='POST'){
      if(failWrite){ok=false;error={code:'MODEL_UNAVAILABLE',message:'连接失败'};}else data={generation,user_message_id:'user-message'};
    }
    else if(p==='/conversations/conv-a/messages')data={items:[{id:'user-message',role:'user',content:'hello'},message],next_cursor:null};
    else if(p==='/conversations/conv-a'&&options.method==='PATCH')data=conv;
    else if(p==='/conversations/conv-a')data={conversation:conv,files:[]};
    else if(p==='/requests/request-a/cancel')data={status:'running',cancel_requested:true};
    else if(p==='/requests/request-a')data={...generation};
    else if(p==='/messages/user-message/retry')data={generation};
    else throw new Error('unexpected test request '+url);
    return {ok,status:ok?200:401,json:async()=>ok?{data}:{error}};
  };
  for(const file of ['vendor/marked.js','vendor/purify.js','client-core.js','app.js'])w.eval(fs.readFileSync(path.join(root,file),'utf8')+'\n//# sourceURL='+path.join(root,file).replaceAll('\\','/'));
  for(let i=0;i<5;i++)await tick();
  assert(doc.getElementById('auth-dialog').open);
  doc.getElementById('auth-switch').click();
  doc.getElementById('auth-username').value='user_a';doc.getElementById('auth-password').value='synthetic-pass-123';doc.getElementById('auth-display-name').value='A';
  doc.getElementById('auth-form').dispatchEvent(new w.Event('submit',{bubbles:true,cancelable:true}));
  for(let i=0;i<8;i++)await tick();
  assert(!doc.getElementById('auth-dialog').open);assert.equal(doc.getElementById('profile-name').textContent,'合成A');
  doc.getElementById('prompt').value='hello';doc.getElementById('composer').dispatchEvent(new w.Event('submit',{cancelable:true}));
  for(let i=0;i<10;i++)await tick();
  assert.equal(Source.instances.length,1);
  const source=Source.instances[0];
  await source.event('delta',2,{append:'**bold** <img src=x onerror=alert(1)> [bad](javascript:alert(1)) [good](https://example.com)'});
  assert(doc.querySelector('.answer-text strong'));assert(!doc.querySelector('.answer-text img'));assert(!doc.querySelector('[onerror]'));
  assert(![...doc.querySelectorAll('.answer-text a')].some(a=>a.href.startsWith('javascript:')));
  assert.equal(doc.querySelector('.answer-text a[href^="https:"]').rel,'noopener noreferrer');
  doc.getElementById('send').click();for(let i=0;i<3;i++)await tick();
  assert(requests.some(r=>r.url.endsWith('/cancel')));
  generation.status='failed';generation.content='partial';generation.last_seq=3;generation.error={code:'OUTPUT_LIMIT',message:'截断'};message.content='partial';message.status='failed';
  await source.event('error',3,{code:'OUTPUT_LIMIT',message:'截断',status:'failed'});
  assert(doc.getElementById('connection-state').textContent.includes('OUTPUT_LIMIT'));
  const retry=[...doc.querySelectorAll('button')].find(b=>b.textContent==='重试此问题');assert(retry);retry.click();for(let i=0;i<8;i++)await tick();
  assert(requests.some(r=>r.url.endsWith('/retry')));
  doc.getElementById('nav-docs').click();assert(!doc.getElementById('documents').classList.contains('hidden'));
  doc.getElementById('settings-open').click();assert(doc.getElementById('settings').open);
  doc.getElementById('logout').click();for(let i=0;i<8;i++)await tick();
  assert(doc.getElementById('auth-dialog').open);assert(!doc.getElementById('messages').textContent.includes('partial'));
  for(const [k,v] of Object.entries(w.localStorage))assert(!v.includes('synthetic-pass-123')&&!v.includes('partial'));
  dom.window.close();
});
