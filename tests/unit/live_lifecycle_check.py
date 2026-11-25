"""Real Qwen cancellation/retry and two-account HTTP isolation; synthetic text only."""
import json
import time
import uuid
from pathlib import Path
import httpx

base='http://127.0.0.1:8787'
record={'started_at':time.time(),'kind':'author real model cancellation/retry + HTTP ownership','checks':[]}
def check(name,condition):
    record['checks'].append({'name':name,'pass':bool(condition)})
    assert condition,name

def register(c):
    nonce=c.get('/api/v1/auth/csrf').json()['data']['csrf_token']
    h={'Origin':base,'X-CSRF-Token':nonce}
    data=c.post('/api/v1/auth/register',headers=h,json={'username':'life_'+uuid.uuid4().hex[:12],'display_name':'合成验证','password':uuid.uuid4().hex}).json()['data']
    h['X-CSRF-Token']=data['csrf_token'];h['Idempotency-Key']=str(uuid.uuid4())
    return h

try:
    with httpx.Client(base_url=base,timeout=200,trust_env=False) as a,httpx.Client(base_url=base,timeout=200,trust_env=False) as b:
        check('fixed_version_header',a.get('/api/v1/health').headers.get('x-zhixu-version')=='0.1.0-p0.2')
        h=register(a);cid=a.post('/api/v1/conversations',json={},headers=h).json()['data']['id']
        h['Idempotency-Key']=str(uuid.uuid4())
        data=a.post('/api/v1/conversations/'+cid+'/messages',headers=h,json={'content':'请按顺序输出数字1到30，每行一个数字，不要解释。','mode':'general','intent':'question','file_refs':[]}).json()['data']
        rid=data['generation']['id'];events=[];cancel_sent=False
        with a.stream('GET',data['events_url']) as s:
            for line in s.iter_lines():
                if not line.startswith('data: '):
                    continue
                event=json.loads(line[6:]);events.append(event)
                if event['type']=='delta' and not cancel_sent:
                    check('cancel_accepted',a.post('/api/v1/requests/'+rid+'/cancel',headers=h,json={}).status_code==200)
                    cancel_sent=True
        cancelled=a.get('/api/v1/requests/'+rid).json()['data']
        record['cancelled']=cancelled;record['cancel_events']=events
        check('real_content_before_cancel',cancel_sent and bool(cancelled['content']))
        check('cancelled_terminal',cancelled['status']=='cancelled')
        h['Idempotency-Key']=str(uuid.uuid4())
        retry=a.post('/api/v1/messages/'+data['user_message_id']+'/retry',headers=h,json={'failed_request_id':rid,'scope_policy':'current'})
        check('retry_accepted',retry.status_code==202)
        result=retry.json()['data'];retry_events=[]
        with a.stream('GET',result['events_url']) as s:
            for line in s.iter_lines():
                if line.startswith('data: '):
                    retry_events.append(json.loads(line[6:]))
        snapshot=a.get('/api/v1/requests/'+result['generation']['id']).json()['data']
        record['retry_snapshot']=snapshot;record['retry_events']=retry_events
        check('retry_real_completed',snapshot['status']=='completed')
        messages=a.get('/api/v1/conversations/'+cid+'/messages').json()['data']['items']
        check('retry_no_duplicate_question',len([m for m in messages if m['role']=='user'])==1)
        check('answer_versions_preserved',[m['answer_version'] for m in messages if m['role']=='assistant']==[1,2])
        hb=register(b)
        for path in ['/conversations/'+cid,'/conversations/'+cid+'/messages','/requests/'+rid,'/requests/'+rid+'/events']:
            check('B_cannot_read_'+path.split('/')[1],b.get('/api/v1'+path).status_code==404)
        check('B_cannot_cancel',b.post('/api/v1/requests/'+rid+'/cancel',headers=hb,json={}).status_code==404)
        check('B_cannot_retry',b.post('/api/v1/messages/'+data['user_message_id']+'/retry',headers=hb,json={'failed_request_id':rid,'scope_policy':'current'}).status_code==404)
        check('B_cannot_rename',b.patch('/api/v1/conversations/'+cid,headers=hb,json={'title':'stolen','revision':2}).status_code==404)
        check('A_still_reads',a.get('/api/v1/conversations/'+cid).status_code==200)
    record['result']='passed'
except Exception as error:
    record['result']='failed';record['error_type']=type(error).__name__
    raise
finally:
    record['ended_at']=time.time()
    Path('docs/engineering/audit/AI-DEV/2026-11-25-p0.2/live-lifecycle.json').write_text(json.dumps(record,ensure_ascii=False,indent=2),encoding='utf-8')
