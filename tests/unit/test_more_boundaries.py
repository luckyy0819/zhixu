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
from server.model import stream_model
from test_lifecycle import sign_in,ask


def test_capacity_stale_events_and_storage_failure(tmp_path,monkeypatch):
    import server.generation as gen
    async def waiting(*args):
        yield 'partial'
        await asyncio.Event().wait()
    monkeypatch.setattr(gen,'stream_model',waiting)
    cfg=Settings(data_dir=tmp_path,model_base_url='http://localhost:1/v1',model_name='unit')
    app=create_app(cfg)
    with TestClient(app,base_url=cfg.origin) as c:
        h=sign_in(c,cfg,'capacity_a');db,auth,convs,gens,gates=app.state.services
        cids=[];responses=[]
        for _ in range(3):
            h['Idempotency-Key']=str(uuid.uuid4());cids.append(c.post('/api/v1/conversations',headers=h,json={}).json()['data']['id'])
        for cid in cids[:2]:
            responses.append(ask(c,h,cid).json()['data'])
        assert ask(c,h,cids[2]).json()['error']['code']=='GENERATION_CAPACITY'
        for data in responses:
            rid=data['generation']['id'];c.post('/api/v1/requests/'+rid+'/cancel',headers=h,json={});c.get(data['events_url'])
            db.run('UPDATE generation_requests SET events_invalidated=1 WHERE id=?',(rid,))
            assert c.get('/api/v1/requests/'+rid+'/events').status_code==410
        h['Idempotency-Key']=str(uuid.uuid4());assert c.post('/api/v1/conversations',headers=h,json={'title':'   '}).status_code==422
        cid=cids[0]
        assert c.get('/api/v1/conversations/'+cid+'/messages?limit=0').status_code==422
        page=c.get('/api/v1/conversations/'+cid+'/messages?limit=1').json()['data']
        assert len(c.get('/api/v1/conversations/'+cid+'/messages?limit=1&cursor='+page['next_cursor']).json()['data']['items'])==1
        c.get('/api/v1/conversations/'+cid+'/messages')
        rid=responses[0]['generation']['id'];gens.finish(rid,'completed')
        # A failed resource transaction must roll back and return a sanitized storage error.
        import sqlite3
        def broken(*args):
            raise sqlite3.OperationalError('synthetic private storage detail')
        monkeypatch.setattr(convs,'listing',broken)
        r=c.get('/api/v1/conversations');assert r.status_code==503 and 'private' not in r.text


def test_logout_idempotence_bad_password_and_uniqueness(tmp_path):
    cfg=Settings(data_dir=tmp_path,model_base_url='http://localhost:1/v1',model_name='unit')
    with TestClient(create_app(cfg),base_url=cfg.origin) as c:
        h=sign_in(c,cfg,'unique_a')
        n=c.get('/api/v1/auth/csrf').json()['data']['csrf_token']
        r=c.post('/api/v1/auth/register',json={'username':'UNIQUE_A','display_name':'A','password':'synthetic-pass-123'},headers={**h,'X-CSRF-Token':n})
        assert r.status_code==409
        n=c.get('/api/v1/auth/csrf').json()['data']['csrf_token']
        assert c.post('/api/v1/auth/login',json={'username':'unique_a','password':'synthetic-pass-WRONG'},headers={**h,'X-CSRF-Token':n}).status_code==401
        assert c.post('/api/v1/auth/logout',headers=h,json={}).status_code==200
        assert c.post('/api/v1/auth/logout',headers=h,json={}).status_code==200
        n=c.get('/api/v1/auth/csrf').json()['data']['csrf_token']
        r=c.post('/api/v1/auth/login',json={'username':'unique_a','password':'synthetic-pass-123'},headers={**h,'X-CSRF-Token':n})
        assert r.status_code==200


def test_real_network_unavailable_closes_client(tmp_path):
    cfg=Settings(data_dir=tmp_path,model_base_url='http://127.0.0.1:1/v1',model_name='unit')
    async def run():
        return [x async for x in stream_model(cfg,[])]
    with pytest.raises(DomainError,match='MODEL_UNAVAILABLE'):
        asyncio.run(run())


@pytest.mark.parametrize('raw,status,code',[
    ('x'*40000,400,'MODEL_UNAVAILABLE'),
    ('{broken',400,'MODEL_UNAVAILABLE'),
    ('data: '+('x'*1048577)+'\n\n',200,'MODEL_FORMAT_ERROR'),
    ('data: {"choices":[]}\n\ndata: [DONE]\n\n',200,'UPSTREAM_INCOMPLETE'),
], ids=['large-http-error','malformed-http-error','oversized-sse-frame','missing-finish-reason'])
def test_bounded_model_protocol(tmp_path,raw,status,code):
    cfg=Settings(data_dir=tmp_path,model_base_url='http://localhost:1/v1',model_name='unit')
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r:httpx.Response(status,text=raw))) as client:
            return [x async for x in stream_model(cfg,[],client)]
    with pytest.raises(DomainError,match=code):
        asyncio.run(run())


def test_config_environment_and_data_root(tmp_path,monkeypatch):
    from server.config import ROOT
    monkeypatch.setenv('MODEL_BASE_URL','http://localhost:1/v1');monkeypatch.setenv('MODEL_NAME','unit');monkeypatch.setenv('DATA_DIR',str(tmp_path))
    assert Settings.load().data_dir==tmp_path
    with pytest.raises(ValueError,match='CONFIG_INVALID'):
        Settings(data_dir=ROOT,model_base_url='http://localhost:1/v1',model_name='unit')
