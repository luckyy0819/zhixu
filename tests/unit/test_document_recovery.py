import asyncio,json,secrets,uuid,time
import pytest
from server.errors import DomainError
from test_document_boundaries import env,ready,upload

def test_scope_changed_retry_requires_explicit_remaining_files(env,monkeypatch):
    import server.generation as module
    async def unavailable(*args):raise DomainError('MODEL_UNAVAILABLE');yield
    monkeypatch.setattr(module,'stream_model',unavailable)
    c,h,cid,app,cfg=env;fid,_=ready(env);other,_=ready(env,raw='培训安排记录。'.encode())
    refs=[{'file_id':fid,'version':1},{'file_id':other,'version':1}]
    created=c.post('/api/v1/conversations/'+cid+'/messages',json=dict(content='培训预算？',mode='document',intent='question',file_refs=refs),headers={**h,'Idempotency-Key':str(uuid.uuid4())}).json()['data'];c.get(created['events_url'])
    rid=created['generation']['id'];mid=created['user_message_id']
    def retry(confirmed=None):
        body=dict(failed_request_id=rid,scope_policy='current')
        if confirmed is not None:body['confirmed_file_refs']=confirmed
        return c.post('/api/v1/messages/'+mid+'/retry',json=body,headers={**h,'Idempotency-Key':str(uuid.uuid4())})
    first=retry();assert first.status_code==202,first.text;c.get(first.json()['data']['events_url'])
    c.patch('/api/v1/conversations/'+cid+'/files/'+other,json={'enabled':False,'revision':1},headers=h)
    changed=retry();assert changed.status_code==409 and changed.json()['error']['code']=='SCOPE_CHANGED'
    assert changed.json()['error']['details']['available_file_refs']==[{'file_id':fid,'version':1}]
    assert retry([{'file_id':str(uuid.uuid4()),'version':1}]).status_code==422
    accepted=retry([{'file_id':fid,'version':1}]);assert accepted.status_code==202,accepted.text;c.get(accepted.json()['data']['events_url'])
    assert accepted.json()['data']['generation']['scope']==[{'file_id':fid,'version':1}]

def test_gc_failure_receipt_replay_and_recovery(env,monkeypatch):
    from pathlib import Path
    c,h,cid,app,cfg=env;fid,_=ready(env);original=Path.unlink
    def fail(path,*a,**kw):
        if path.parent.name=='objects':raise PermissionError('controlled unit storage refusal')
        return original(path,*a,**kw)
    with monkeypatch.context() as context:
        context.setattr(Path,'unlink',fail)
        result=c.request('DELETE','/api/v1/files/'+fid,json={'confirmation':'DELETE'},headers=h);job=result.json()['data']['id']
        c.portal.call(app.state.deletion.wait_for_job,job)
        assert c.get('/api/v1/deletions/'+job).json()['data']['status']=='failed'
        assert c.get('/api/v1/files/'+fid+'/preview?version=1&paragraph=1').status_code==410
    async def recover():app.state.deletion.recover()
    c.portal.call(recover);c.portal.call(app.state.deletion.wait_for_job,job)
    assert c.get('/api/v1/deletions/'+job).json()['data']['status']=='completed'
    assert c.request('DELETE','/api/v1/files/'+fid,json={'confirmation':'DELETE'},headers=h).json()['data']['id']==job
    deletion_key=str(uuid.uuid4());token=secrets.token_urlsafe(32);body=dict(confirmation='DELETE',current_password='wrong-password123',deletion_key=deletion_key,receipt_token=token)
    assert c.request('DELETE','/api/v1/account/data',json=body,headers=h).status_code==401
    # Wrong password must never create a deletion task.
    assert app.state.services[0].one('SELECT id FROM deletion_jobs WHERE id=?',(deletion_key,)) is None
    me=c.get('/api/v1/auth/me').json()['data'];h['X-CSRF-Token']=me['csrf_token'];body['current_password']='synthetic-pass-123'
    assert c.request('DELETE','/api/v1/account/data',json=body,headers=h).status_code==202
    c.portal.call(app.state.deletion.wait_for_job,deletion_key)
    assert c.request('DELETE','/api/v1/account/data',json=body,headers=h).status_code==202
    app.state.services[0].run('UPDATE deletion_jobs SET expires_at=? WHERE id=?',(time.time()-1,deletion_key))
    assert c.get('/api/v1/deletions/'+deletion_key,headers={'X-Deletion-Receipt':token}).status_code==404

def test_graceful_parse_restart_and_expired_lease_are_recoverable(env,monkeypatch):
    import server.documents as module
    c,h,cid,app,cfg=env;docs=app.state.documents;db=app.state.services[0];real=module.parse_in_job
    async def waiting(*args):await asyncio.Event().wait()
    monkeypatch.setattr(module,'parse_in_job',waiting)
    response,_=upload(env);fid=response.json()['data']['file']['id']
    c.portal.call(docs.close)
    assert db.one('SELECT status FROM parse_jobs WHERE file_id=?',(fid,))['status'] in ('running','queued')
    monkeypatch.setattr(module,'parse_in_job',real);docs.stopping=False
    async def recover():docs.recover()
    c.portal.call(recover);c.portal.call(docs.wait_for_file,fid)
    assert c.get('/api/v1/files/'+fid+'/status').json()['data']['status']=='ready'
    assert db.one('SELECT restart_recoveries FROM parse_jobs WHERE file_id=?',(fid,))['restart_recoveries']==1
    # Owner-scoped orphan reservation cleared without walking any other folder.
    owner=db.one('SELECT owner_id FROM conversations WHERE id=?',(cid,))['owner_id'];tmp='staging/'+str(uuid.uuid4());docs.path(tmp).write_bytes(b'partial')
    db.run('INSERT INTO upload_reservations VALUES(?,?,?,?,?,?,?,?,?)',(str(uuid.uuid4()),owner,cid,str(uuid.uuid4()),20,7,tmp,time.time()-1,'unit'))
    docs.clean_reservations();assert not docs.path(tmp).exists()

def test_bad_model_claim_closes_upstream_immediately(env,monkeypatch):
    import server.generation as module
    closed=[]
    async def bad(*args):
        try:yield '{"invalid":"record"}\n';await asyncio.Event().wait()
        finally:closed.append(True)
    monkeypatch.setattr(module,'stream_model',bad)
    c,h,cid,app,cfg=env;fid,_=ready(env)
    result=c.post('/api/v1/conversations/'+cid+'/messages',headers={**h,'Idempotency-Key':str(uuid.uuid4())},json=dict(content='预算',mode='document',intent='question',file_refs=[{'file_id':fid,'version':1}])).json()['data']
    c.get(result['events_url']);snapshot=c.get('/api/v1/requests/'+result['generation']['id']).json()['data']
    assert closed and snapshot['status']=='failed' and snapshot['error']['code']=='MODEL_FORMAT_ERROR'
    assert snapshot['content']=='' and snapshot['claims']==[] and snapshot['citations']==[]
