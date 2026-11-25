import asyncio
import json
import time
import uuid
import httpx
import pytest
from fastapi.testclient import TestClient
from server.main import create_app
from server.config import Settings
from server.errors import DomainError
from server.context import build_context
from server.model import stream_model
from server.gates import Gate


@pytest.fixture
def client(tmp_path):
    cfg=Settings(data_dir=tmp_path,model_base_url='http://localhost:1/v1',model_name='unit')
    app=create_app(cfg)
    with TestClient(app,base_url=cfg.origin) as c:
        n=c.get('/api/v1/auth/csrf').json()['data']['csrf_token']
        h={'Origin':cfg.origin,'X-CSRF-Token':n}
        r=c.post('/api/v1/auth/register',json={'username':'boundary_a','display_name':'A','password':'synthetic-password-123'},headers=h)
        h['X-CSRF-Token']=r.json()['data']['csrf_token'];h['Idempotency-Key']=str(uuid.uuid4())
        cid=c.post('/api/v1/conversations',json={},headers=h).json()['data']['id']
        yield c,h,cid,app.state.services,cfg


def test_origin_host_dto_and_nonce(client):
    c,h,cid,services,cfg=client
    assert c.get('/api/v1/health',headers={'Host':'evil.invalid'}).status_code==403
    assert c.get('/api/v1/health',headers={'Host':'localhost:8787'},follow_redirects=False).status_code==307
    assert c.post('/api/v1/conversations',json={},headers={**h,'Origin':'http://evil.invalid'}).status_code==403
    assert c.post('/api/v1/conversations',content='bad',headers={**h,'Content-Type':'text/plain'}).status_code==422
    assert c.post('/api/v1/conversations',content='x'*32769,headers={**h,'Content-Type':'application/json'}).status_code==422
    assert c.post('/api/v1/conversations',json={'owner_id':str(uuid.uuid4())},headers=h).status_code==422
    c.get('/api/v1/auth/me')
    assert c.post('/api/v1/conversations',json={},headers=h).json()['error']['code']=='CSRF_INVALID'
    n=c.get('/api/v1/auth/csrf').json()['data']['csrf_token']
    c.cookies.set('zhixu_preauth','wrong',domain='127.0.0.1',path='/')
    assert c.post('/api/v1/auth/login',json={'username':'boundary_a','password':'synthetic-password-123'},headers={**h,'X-CSRF-Token':n}).status_code==403


def test_pagination_revision_search_and_question_validation(client):
    c,h,cid,services,cfg=client
    assert c.get('/api/v1/conversations?limit=0').status_code==422
    assert c.get('/api/v1/conversations?cursor=!!').status_code==400
    assert c.get('/api/v1/conversations?cursor=WzEsMl0').status_code==400
    assert c.get('/api/v1/conversations?q=%25').json()['data']['items']==[]
    assert c.get('/api/v1/conversations/not-uuid').status_code==422
    assert c.patch('/api/v1/conversations/'+cid,json={'title':'new','revision':2},headers=h).status_code==409
    assert c.patch('/api/v1/conversations/'+cid,json={'title':'  ','revision':1},headers=h).status_code==422
    assert c.patch('/api/v1/conversations/'+cid,json={'title':'new','revision':1},headers=h).json()['data']['revision']==2
    for _ in range(3):
        h['Idempotency-Key']=str(uuid.uuid4());assert c.post('/api/v1/conversations',json={},headers=h).status_code==201
    page=c.get('/api/v1/conversations?limit=2').json()['data']
    second=c.get('/api/v1/conversations?limit=2&cursor='+page['next_cursor']).json()['data']
    assert len(page['items'])==len(second['items'])==2
    assert not set(x['id'] for x in page['items']) & set(x['id'] for x in second['items'])
    assert c.get('/api/v1/conversations/'+cid+'/files').json()['data']['items']==[]
    for body in [{'content':'  ','mode':'general','file_refs':[],'intent':'question'},{'content':'x','mode':'document','file_refs':[],'intent':'question'}]:
        h['Idempotency-Key']=str(uuid.uuid4());assert c.post('/api/v1/conversations/'+cid+'/messages',json=body,headers=h).status_code in (422,409)
    assert c.get('/api/v1/conversations/'+cid+'/messages').json()['data']['items']==[]
    assert c.get('/api/v1/capabilities').json()['data']['features']['documents'] is True


@pytest.mark.parametrize('status,expected',[(401,'MODEL_AUTH_ERROR'),(403,'MODEL_AUTH_ERROR'),(429,'MODEL_RATE_LIMIT'),(503,'MODEL_UNAVAILABLE'),(400,'CONTEXT_LIMIT')])
def test_model_status_mapping(tmp_path,status,expected):
    cfg=Settings(data_dir=tmp_path,model_base_url='http://localhost:1/v1',model_name='test')
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r:httpx.Response(status,json={'error':{'code':'context_length_exceeded'}}))) as client:
            return [x async for x in stream_model(cfg,[],client)]
    with pytest.raises(DomainError,match=expected):
        asyncio.run(run())


@pytest.mark.parametrize('raw',[
    'data: {"choices":{}}\n\n', 'data: {"choices":[{"delta":{"content":123}}]}\n\n',
    'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\ndata: {"choices":[{"delta":{"content":"late"}}]}\n\n',
])
def test_model_rejects_invalid_protocol(tmp_path,raw):
    cfg=Settings(data_dir=tmp_path,model_base_url='http://localhost:1/v1',model_name='test')
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r:httpx.Response(200,text=raw))) as client:
            return [x async for x in stream_model(cfg,[],client)]
    with pytest.raises(DomainError,match='MODEL_FORMAT_ERROR'):
        asyncio.run(run())


def test_gate_revoke_priority_and_timeout():
    async def run():
        gate=Gate();entered=asyncio.Event();release=asyncio.Event();sent=[]
        async def slow_send(message):
            entered.set();await release.wait();sent.append(message)
        s0=asyncio.create_task(gate.deliver(lambda:True,slow_send,'first'))
        await entered.wait()
        s1=asyncio.create_task(gate.deliver(lambda:True,slow_send,'stale'))
        await asyncio.sleep(0)
        revoke=gate.revoke(lambda:sent.append('revoked'))
        release.set();await s0
        assert await s1 is False
        await revoke
        assert sent==['first','revoked']
        assert gate.clear.is_set() and not gate.tokens
        async def never_send(message):
            await asyncio.Event().wait()
        with pytest.raises(TimeoutError):
            await gate.deliver(lambda:True,never_send,'timeout')
        assert not gate.lock.locked()
    asyncio.run(run())


def test_context_refuses_without_truncating(client):
    c,h,cid,services,cfg=client
    db,auth,convs,gens,gates=services
    owner=db.one('SELECT owner_id FROM conversations WHERE id=?',(cid,))['owner_id']
    with pytest.raises(DomainError,match='CONTEXT_LIMIT'):
        build_context(db,cfg,cid,owner,'汉'*6000)
    assert db.one('SELECT count(*) AS n FROM messages')['n']==0


def test_session_expiry_uniform_auth_and_rate(client):
    c,h,cid,services,cfg=client
    db,auth,*_=services
    db.run('UPDATE auth_sessions SET expires_at=?',(time.time()-1,))
    assert c.get('/api/v1/auth/me').json()['error']['code']=='SESSION_EXPIRED'
    for i in range(4):
        n=c.get('/api/v1/auth/csrf').json()['data']['csrf_token']
        r=c.post('/api/v1/auth/login',headers={**h,'X-CSRF-Token':n},json={'username':'unknown_a','password':'synthetic-password-wrong'})
        assert r.json()['error']['code']=='AUTH_INVALID'
    n=c.get('/api/v1/auth/csrf').json()['data']['csrf_token']
    assert c.post('/api/v1/auth/login',headers={**h,'X-CSRF-Token':n},json={'username':'boundary_a','password':'synthetic-password-123'}).status_code==429
