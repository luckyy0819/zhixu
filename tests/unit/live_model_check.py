"""Author-only real local HTTP + configured model check, synthetic text. No secrets recorded."""
import json
import time
import uuid
from pathlib import Path
import httpx

out=Path('docs/engineering/audit/AI-DEV/2026-11-25-p0.2/live-model.json')
record={'started_at':time.time(),'type':'author real HTTP and Qwen, synthetic input only','checks':[]}
def check(name,value):
    record['checks'].append({'name':name,'pass':bool(value)})
    assert value,name
try:
    with httpx.Client(base_url='http://127.0.0.1:8787',timeout=200,trust_env=False) as c:
        check('health',c.get('/api/v1/health').status_code==200)
        nonce=c.get('/api/v1/auth/csrf').json()['data']['csrf_token']
        name='dev_'+uuid.uuid4().hex[:12]
        password=uuid.uuid4().hex
        h={'Origin':'http://127.0.0.1:8787','X-CSRF-Token':nonce}
        r=c.post('/api/v1/auth/register',headers=h,json={'username':name,'display_name':'合成验证A','password':password})
        check('register',r.status_code==201)
        h['X-CSRF-Token']=r.json()['data']['csrf_token'];h['Idempotency-Key']=str(uuid.uuid4())
        cid=c.post('/api/v1/conversations',headers=h,json={}).json()['data']['id']
        h['Idempotency-Key']=str(uuid.uuid4())
        r=c.post('/api/v1/conversations/'+cid+'/messages',headers=h,json={'content':'只回答“测试成功”四个字，不要解释。','mode':'general','file_refs':[],'intent':'question'})
        check('generation_accepted',r.status_code==202)
        generation=r.json()['data'];record['request_id']=generation['generation']['id']
        with c.stream('GET',generation['events_url']) as stream:
            check('SSE_200',stream.status_code==200)
            events=[]
            for line in stream.iter_lines():
                if line.startswith('data: '):
                    events.append(json.loads(line[6:]))
        snap=c.get('/api/v1/requests/'+record['request_id']).json()['data']
        record['events']=events;record['snapshot']=snap
        check('real_model_completed',snap['status']=='completed')
        check('actual_content',bool(snap['content']))
        check('single_terminal',len([e for e in events if e['type'] in ('done','error','cancelled')])==1)
        check('refresh_persisted',c.get('/api/v1/conversations/'+cid+'/messages').json()['data']['items'][-1]['content']==snap['content'])
        check('logout',c.post('/api/v1/auth/logout',headers=h,json={}).status_code==200)
        nonce=c.get('/api/v1/auth/csrf').json()['data']['csrf_token'];h['X-CSRF-Token']=nonce
        r=c.post('/api/v1/auth/login',headers=h,json={'username':name,'password':password})
        check('relogin',r.status_code==200)
        check('relogin_persistence',c.get('/api/v1/conversations/'+cid+'/messages').json()['data']['items'][-1]['content']==snap['content'])
    record['result']='passed'
except Exception as e:
    record['result']='failed';record['error_type']=type(e).__name__
    raise
finally:
    record['ended_at']=time.time();out.write_text(json.dumps(record,ensure_ascii=False,indent=2),encoding='utf-8')
