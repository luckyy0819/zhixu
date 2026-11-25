import asyncio
import json
import uuid
import httpx
import pytest
from fastapi.testclient import TestClient
from server.main import create_app
from server.config import Settings
from server.model import model_error


def sign_in(c,cfg,name):
    n=c.get('/api/v1/auth/csrf').json()['data']['csrf_token']
    r=c.post('/api/v1/auth/register',json={'username':name,'display_name':name,'password':'synthetic-pass-123'},headers={'Origin':cfg.origin,'X-CSRF-Token':n})
    assert r.status_code==201,r.text
    return {'Origin':cfg.origin,'X-CSRF-Token':r.json()['data']['csrf_token'],'Idempotency-Key':str(uuid.uuid4())}


def ask(c,h,cid,text='hello'):
    h['Idempotency-Key']=str(uuid.uuid4())
    return c.post('/api/v1/conversations/'+cid+'/messages',headers=h,json={'content':text,'mode':'general','file_refs':[],'intent':'question'})


def test_failed_cancelled_retry_and_cross_account(tmp_path,monkeypatch):
    import server.generation as gen
    mode='fail'
    async def upstream(*args):
        yield 'partial'
        if mode=='fail':
            raise model_error('OUTPUT_LIMIT')
        if mode=='wait':
            await asyncio.Event().wait()
    monkeypatch.setattr(gen,'stream_model',upstream)
    cfg=Settings(data_dir=tmp_path,model_base_url='http://localhost:1/v1',model_name='unit')
    app=create_app(cfg)
    with TestClient(app,base_url=cfg.origin) as c:
        h=sign_in(c,cfg,'life_a')
        cid=c.post('/api/v1/conversations',headers=h,json={}).json()['data']['id']
        first=ask(c,h,cid).json()['data'];rid=first['generation']['id'];mid=first['user_message_id']
        assert 'event: error' in c.get(first['events_url']).text
        assert c.get('/api/v1/requests/'+rid).json()['data']['error']['code']=='OUTPUT_LIMIT'
        mode='success';h['Idempotency-Key']=str(uuid.uuid4())
        retry_body={'failed_request_id':rid,'scope_policy':'current'}
        second=c.post('/api/v1/messages/'+mid+'/retry',json=retry_body,headers=h).json()['data']
        assert second['user_message_id']==mid
        assert 'event: done' in c.get(second['events_url']).text
        assert c.post('/api/v1/messages/'+mid+'/retry',json=retry_body,headers=h).json()['data']['generation']['id']==second['generation']['id']
        messages=c.get('/api/v1/conversations/'+cid+'/messages').json()['data']['items']
        assert len(messages)==3 and [m['answer_version'] for m in messages if m['role']=='assistant']==[1,2]
        assert c.get('/api/v1/conversations/'+cid+'/messages?limit=1').json()['data']['next_cursor']
        mode='wait';third=ask(c,h,cid).json()['data'];rid3=third['generation']['id']
        assert ask(c,h,cid).json()['error']['code']=='GENERATION_ACTIVE'
        cancel=c.post('/api/v1/requests/'+rid3+'/cancel',json={},headers=h)
        assert cancel.status_code==200
        assert 'event: cancelled' in c.get(third['events_url']).text
        assert c.get('/api/v1/requests/'+rid3).json()['data']['status']=='cancelled'
        for suffix in ('?after=-1','?after=oops'):
            assert c.get('/api/v1/requests/'+rid3+'/events'+suffix).status_code==400
        assert c.get('/api/v1/requests/'+rid3+'/events?after=0',headers={'Last-Event-ID':'999'}).status_code==422
        cookies=dict(c.cookies);c.cookies.clear();hb=sign_in(c,cfg,'life_b')
        for path in ['/conversations/'+cid,'/conversations/'+cid+'/messages','/conversations/'+cid+'/files','/requests/'+rid,'/requests/'+rid+'/events']:
            assert c.get('/api/v1'+path).status_code==404
        assert c.post('/api/v1/requests/'+rid+'/cancel',json={},headers=hb).status_code==404
        assert c.post('/api/v1/messages/'+mid+'/retry',json=retry_body,headers=hb).status_code==404
        assert c.patch('/api/v1/conversations/'+cid,json={'title':'stolen','revision':1},headers=hb).status_code==404
        c.cookies.clear();c.cookies.update(cookies)
        assert c.get('/api/v1/conversations/'+cid).status_code==200


def test_recovery_snapshot_and_context_history(tmp_path,monkeypatch):
    import server.generation as gen
    sent=[]
    async def upstream(cfg,messages):
        sent.append(messages);yield 'answer'
    monkeypatch.setattr(gen,'stream_model',upstream)
    cfg=Settings(data_dir=tmp_path,model_base_url='http://localhost:1/v1',model_name='unit')
    app=create_app(cfg)
    with TestClient(app,base_url=cfg.origin) as c:
        h=sign_in(c,cfg,'restart_a');cid=c.post('/api/v1/conversations',headers=h,json={}).json()['data']['id']
        one=ask(c,h,cid,'first').json()['data'];c.get(one['events_url'])
        two=ask(c,h,cid,'second').json()['data'];c.get(two['events_url'])
        assert [m['role'] for m in sent[-1]]==['system','user','assistant','user']
        cookies=dict(c.cookies);db=app.state.services[0]
        rid=two['generation']['id']
        db.run("UPDATE generation_requests SET status='running' WHERE id=?",(rid,))
    with TestClient(create_app(cfg),base_url=cfg.origin) as c:
        c.cookies.update(cookies)
        snap=c.get('/api/v1/requests/'+rid).json()['data']
        assert snap['status']=='interrupted' and snap['content']=='answer'
        assert c.get('/api/v1/requests/'+rid+'/events?after='+str(snap['last_seq'])).text==''


def test_upstream_fragmentation_utf8_timeouts_and_disconnect(tmp_path):
    from server.model import stream_model
    from server.errors import DomainError
    cfg=Settings(data_dir=tmp_path,model_base_url='http://localhost:1/v1',model_name='unit')
    class Bytes(httpx.AsyncByteStream):
        async def __aiter__(self):
            raw=':hi\n\ndata: {"choices":[{"delta":{"content":"汉字"}}]}\n\ndata: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n'.encode()
            for b in raw:
                yield bytes([b])
    async def run(transport):
        async with httpx.AsyncClient(transport=transport) as client:
            return [x async for x in stream_model(cfg,[],client)]
    assert asyncio.run(run(httpx.MockTransport(lambda r:httpx.Response(200,stream=Bytes()))))==['汉字']
    for exc,code in [(httpx.ReadTimeout('test'),'MODEL_TIMEOUT'),(httpx.ConnectError('test'),'MODEL_UNAVAILABLE')]:
        def fail(request):
            raise exc
        with pytest.raises(DomainError,match=code):
            asyncio.run(run(httpx.MockTransport(fail)))
