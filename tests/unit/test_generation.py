import asyncio
import uuid
from fastapi.testclient import TestClient


def test_generation_replay_cancel_retry_and_ownership(tmp_path,monkeypatch):
    from server.main import create_app
    from server.config import Settings
    import server.generation as gen
    async def content(*args):
        yield '真实适配器边界测试文本'
    monkeypatch.setattr(gen,'stream_model',content)
    cfg=Settings(data_dir=tmp_path,model_base_url='http://localhost:1/v1',model_name='test')
    with TestClient(create_app(cfg),base_url=cfg.origin) as c:
        nonce=c.get('/api/v1/auth/csrf').json()['data']['csrf_token']
        user=c.post('/api/v1/auth/register',json={'username':'gen_a','display_name':'A','password':'synthetic-pass-123'},headers={'Origin':cfg.origin,'X-CSRF-Token':nonce}).json()['data']
        h={'Origin':cfg.origin,'X-CSRF-Token':user['csrf_token'],'Idempotency-Key':str(uuid.uuid4())}
        cid=c.post('/api/v1/conversations',json={},headers=h).json()['data']['id']
        h['Idempotency-Key']=str(uuid.uuid4())
        body={'content':'hello','mode':'general','file_refs':[],'intent':'question'}
        r=c.post('/api/v1/conversations/'+cid+'/messages',json=body,headers=h)
        assert r.status_code==202,r.text
        result=r.json()['data'];rid=result['generation']['id']
        assert c.post('/api/v1/conversations/'+cid+'/messages',json=body,headers=h).json()['data']['generation']['id']==rid
        events=c.get(result['events_url']).text
        assert 'event: done' in events
        snap=c.get('/api/v1/requests/'+rid).json()['data']
        assert snap['content']=='真实适配器边界测试文本' and snap['status']=='completed'
        assert c.get('/api/v1/requests/'+rid+'/events?after=100000').status_code==422
        assert c.post('/api/v1/requests/'+rid+'/cancel',json={},headers=h).json()['data']['status']=='completed'
        h['Idempotency-Key']=str(uuid.uuid4())
        assert c.post('/api/v1/messages/'+result['user_message_id']+'/retry',json={'failed_request_id':rid,'scope_policy':'current'},headers=h).status_code==409
        messages=c.get('/api/v1/conversations/'+cid+'/messages').json()['data']['items']
        assert [m['role'] for m in messages]==['user','assistant']
        assert messages[-1]['content']==snap['content']
        assert c.post('/api/v1/auth/logout',json={},headers=h).status_code==200
        assert c.get('/api/v1/requests/'+rid).status_code==401
