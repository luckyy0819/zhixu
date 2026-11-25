"""ZHIXU-DOCQA-CR-IDEMP-001 normal synthetic owner/TTL/restart checks."""
import json,time,uuid,secrets
from fastapi.testclient import TestClient
from server.main import create_app
from test_document_boundaries import env,ready,upload
from test_lifecycle import sign_in

def test_deleted_create_key_is_opaque_persistent_and_owner_scoped(env):
    c,h,unused,app,cfg=env;db=app.state.services[0];key=str(uuid.uuid4());body={'title':'synthetic deletion key '+uuid.uuid4().hex}
    heads={**h,'Idempotency-Key':key};created=c.post('/api/v1/conversations',headers=heads,json=body).json()['data'];cid=created['id']
    old=dict(db.one('SELECT * FROM idempotency_keys WHERE key=?',(key,)));owner=old['owner_id']
    r=c.request('DELETE','/api/v1/conversations/'+cid,headers=h,json=dict(confirmation='DELETE',include_files=True));assert r.status_code==202
    c.portal.call(app.state.deletion.wait_for_job,r.json()['data']['id'])
    replay=c.post('/api/v1/conversations',headers=heads,json=body);assert replay.status_code==410,replay.text
    assert replay.json()['error']['code']=='RESOURCE_DELETED'
    assert db.one('SELECT * FROM idempotency_keys WHERE key=?',(key,)) is None
    stones=[dict(r) for r in db.all('SELECT * FROM idempotency_tombstones')];assert len(stones)==1
    stone=stones[0];assert set(stone)=={'owner_hash','scope_hash','key_hash','payload_hash','expires_at'}
    assert stone['expires_at']==old['expires_at'] and stone['payload_hash']==old['payload_hash']
    raw=json.dumps(stone)
    for secret in (owner,key,cid,body['title'],'POST /api/v1/conversations'):assert secret not in raw
    assert c.post('/api/v1/conversations',headers=heads,json={'title':'changed'}).status_code==409
    assert c.post('/api/v1/conversations/'+cid+'/messages',headers=heads,json=dict(content='normal',mode='general',intent='question',file_refs=[])).status_code==404
    cookies=dict(c.cookies);c.cookies.clear();bh=sign_in(c,cfg,'stone_b');bh['Idempotency-Key']=key
    other=c.post('/api/v1/conversations',headers=bh,json=body);assert other.status_code==201 and other.json()['data']['id']!=cid
    assert c.get('/api/v1/conversations/'+cid).status_code==404
    c.cookies.clear();c.cookies.update(cookies)
    assert c.post('/api/v1/conversations',headers=heads,json=body).status_code==410
    assert db.one('SELECT expires_at FROM idempotency_tombstones')['expires_at']==old['expires_at']
    deletion_key=str(uuid.uuid4());token=secrets.token_urlsafe(32)
    removed=c.request('DELETE','/api/v1/account/data',headers=h,json=dict(confirmation='DELETE',current_password='synthetic-pass-123',deletion_key=deletion_key,receipt_token=token))
    assert removed.status_code==202; c.portal.call(app.state.deletion.wait_for_job,deletion_key)
    assert db.all('SELECT * FROM idempotency_tombstones')==[]
    assert c.post('/api/v1/conversations',headers=heads,json=body).status_code==401
    assert c.get('/api/v1/deletions/'+deletion_key,headers={'X-Deletion-Receipt':token}).json()['data']['status']=='completed'

def test_deleted_upload_key_410_only_while_conversation_is_accessible(env):
    c,h,cid,app,cfg=env;fid,heads=ready(env)
    result=c.request('DELETE','/api/v1/files/'+fid,headers=h,json=dict(confirmation='DELETE'));assert result.status_code==202
    raw='培训预算7,600元。'.encode()
    replay=c.post('/api/v1/conversations/'+cid+'/files',content=raw,headers=heads);assert replay.status_code==410,replay.text
    assert c.post('/api/v1/files/'+fid+'/retry',json={},headers=h).status_code==404
    assert c.get('/api/v1/files/'+fid+'/preview?version=1&paragraph=1').status_code==410
    c.request('DELETE','/api/v1/conversations/'+cid,headers=h,json=dict(confirmation='DELETE',include_files=True))
    assert c.post('/api/v1/conversations/'+cid+'/files',content=raw,headers=heads).status_code==404

def test_tombstone_survives_restart_and_never_extends_original_ttl(tmp_path):
    from server.config import Settings
    cfg=Settings(data_dir=tmp_path,port=8790,origin='http://127.0.0.1:8790',model_base_url='http://localhost:1/v1',model_name='unit')
    key=str(uuid.uuid4());body={'title':'restart synthetic'}
    with TestClient(create_app(cfg),base_url=cfg.origin) as c:
        h=sign_in(c,cfg,'stone_restart');h['Idempotency-Key']=key;cid=c.post('/api/v1/conversations',headers=h,json=body).json()['data']['id']
        r=c.request('DELETE','/api/v1/conversations/'+cid,headers=h,json=dict(confirmation='DELETE',include_files=True));c.portal.call(c.app.state.deletion.wait_for_job,r.json()['data']['id'])
        cookies=dict(c.cookies);expiry=c.app.state.services[0].one('SELECT expires_at FROM idempotency_tombstones')['expires_at']
    with TestClient(create_app(cfg),base_url=cfg.origin) as c:
        c.cookies.update(cookies);h['X-CSRF-Token']=c.get('/api/v1/auth/me').json()['data']['csrf_token']
        assert c.post('/api/v1/conversations',headers=h,json=body).status_code==410
        db=c.app.state.services[0];assert db.one('SELECT expires_at FROM idempotency_tombstones')['expires_at']==expiry
        db.run('UPDATE idempotency_tombstones SET expires_at=?',(time.time()-1,))
        cookies=dict(c.cookies)
    with TestClient(create_app(cfg),base_url=cfg.origin) as c:
        db=c.app.state.services[0];assert db.all('SELECT * FROM idempotency_tombstones')==[]
        c.cookies.update(cookies);h['X-CSRF-Token']=c.get('/api/v1/auth/me').json()['data']['csrf_token']
        new=c.post('/api/v1/conversations',headers=h,json=body);assert new.status_code==201 and new.json()['data']['id']!=cid
        assert db.all('SELECT * FROM idempotency_tombstones')==[]
        assert c.get('/api/v1/conversations/'+cid).status_code==404
