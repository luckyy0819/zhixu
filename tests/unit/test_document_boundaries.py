"""Synthetic boundary tests; mocked fault seams here are NOT real-model evidence."""
import asyncio,json,uuid,secrets,time
from pathlib import Path
import pytest
from fastapi.testclient import TestClient
from server.config import Settings
from server.main import create_app
from server.errors import DomainError
from test_lifecycle import sign_in

@pytest.fixture
def env(tmp_path):
    cfg=Settings(data_dir=tmp_path,port=8788,origin='http://127.0.0.1:8788',model_base_url='http://localhost:1/v1',model_name='unit')
    app=create_app(cfg)
    with TestClient(app,base_url=cfg.origin) as c:
        h=sign_in(c,cfg,'boundary_a');cid=c.post('/api/v1/conversations',json={},headers=h).json()['data']['id']
        yield c,h,cid,app,cfg

def upload(env,raw='培训预算7,600元。'.encode(),name='budget.txt',extra=None):
    c,h,cid,app,cfg=env
    heads={**h,'Idempotency-Key':str(uuid.uuid4()),'Content-Type':'text/plain','X-File-Name':name,'X-File-Size':str(len(raw)),**(extra or {})}
    return c.post('/api/v1/conversations/'+cid+'/files',content=raw,headers=heads),heads

def ready(env,**args):
    c,h,cid,app,cfg=env;r,heads=upload(env,**args);assert r.status_code==202,r.text
    fid=r.json()['data']['file']['id'];c.portal.call(app.state.documents.wait_for_file,fid)
    return fid,heads

def test_upload_header_size_and_idempotency_failures_are_atomic(env):
    c,h,cid,app,cfg=env
    for extra in [{'X-File-Name':'%'},{'X-File-Name':'bad/name.txt'},{'X-File-Size':'bad'},{'X-File-Size':'0'},{'X-File-Size':'20000001'},{'X-File-Name':'x.csv'},{'Content-Type':'image/png'},{'X-File-Size':'2'},{'X-File-Size':'100'},{'X-File-Name':'x.pdf','Content-Type':'application/pdf'}]:
        r,_=upload(env,extra=extra);assert r.status_code in (400,413,415,422),r.text
    assert app.state.services[0].all('SELECT * FROM upload_reservations')==[]
    assert not list((cfg.data_dir/'staging').iterdir())
    fid,heads=ready(env)
    same=c.post('/api/v1/conversations/'+cid+'/files',content='培训预算7,600元。'.encode(),headers=heads)
    assert same.json()['data']['file']['id']==fid
    bad=c.post('/api/v1/conversations/'+cid+'/files',content=b'changed',headers={**heads,'X-File-Size':'7'})
    assert bad.status_code==409
    for n in range(4):ready(env,raw=('其他资料'+str(n)).encode(),name='other.txt')
    assert upload(env,raw=b'sixth')[0].status_code==413

def test_preview_revision_selection_and_deletion_receipt(env):
    c,h,cid,app,cfg=env;fid,heads=ready(env)
    docs=app.state.documents
    for query in ['paragraph=99','paragraph=1&start=0','paragraph=1&start=5&end=2','paragraph=1&window_start=0&window_end=999']:
        assert c.get('/api/v1/files/'+fid+'/preview?version=1&'+query).status_code==422
    with pytest.raises(DomainError):docs.path('../outside')
    assert c.patch('/api/v1/conversations/'+cid+'/files/'+fid,json={'enabled':False,'revision':1},headers=h).status_code==200
    assert c.patch('/api/v1/conversations/'+cid+'/files/'+fid,json={'enabled':False,'revision':1},headers=h).status_code==200
    assert c.patch('/api/v1/conversations/'+cid+'/files/'+fid,json={'enabled':True,'revision':1},headers=h).status_code==409
    assert c.patch('/api/v1/conversations/'+cid+'/files/'+fid,json={'enabled':True,'revision':2},headers=h).status_code==200
    def ask(refs,intent='question',content='预算'):
        return c.post('/api/v1/conversations/'+cid+'/messages',json=dict(content=content,mode='document',file_refs=refs,intent=intent),headers={**h,'Idempotency-Key':str(uuid.uuid4())})
    assert ask([{'file_id':fid,'version':2}]).status_code==409
    assert ask([{'file_id':fid,'version':1}]*2).status_code==422
    assert c.post('/api/v1/files/'+fid+'/retry',json={},headers={**h,'Idempotency-Key':str(uuid.uuid4())}).status_code==409
    assert c.post('/api/v1/files/'+fid+'/cancel',json={},headers=h).json()['data']['cancel_requested'] is False
    first=c.request('DELETE','/api/v1/conversations/'+cid,headers=h,json={'confirmation':'DELETE','include_files':True});assert first.status_code==202,first.text
    assert c.request('DELETE','/api/v1/conversations/'+cid,headers=h,json={'confirmation':'DELETE','include_files':True}).json()['data']['id']==first.json()['data']['id']
    c.portal.call(app.state.deletion.wait_for_job,first.json()['data']['id'])
    assert c.get('/api/v1/conversations/'+cid).status_code==404
    assert app.state.services[0].all('PRAGMA foreign_key_check')==[]
    for _ in range(60):app.state.deletion.rate_receipt()
    assert c.get('/api/v1/deletions/'+first.json()['data']['id']).status_code==429
    app.state.deletion.receipt_attempts.clear();app.state.deletion.receipt_attempts.append(time.monotonic()-61)
    app.state.deletion.rate_receipt();assert len(app.state.deletion.receipt_attempts)==1

def test_database_refuses_cross_owner_parser_and_chunk_rows(env):
    import sqlite3
    c,h,cid,app,cfg=env;fid,_=ready(env);db=app.state.services[0]
    c.cookies.clear();sign_in(c,cfg,'other_owner');other=db.one("SELECT id FROM users WHERE username_norm='other_owner'")['id']
    job=dict(db.one('SELECT * FROM parse_jobs WHERE file_id=?',(fid,)));job.update(id=str(uuid.uuid4()),owner_id=other,attempt=99)
    chunk=dict(db.one('SELECT * FROM chunks WHERE file_id=?',(fid,)));chunk.update(id=str(uuid.uuid4()),owner_id=other,start=1)
    for table,row in [('parse_jobs',job),('chunks',chunk)]:
        with pytest.raises(sqlite3.IntegrityError,match='FOREIGN KEY'):
            db.run('INSERT INTO '+table+'('+','.join(row)+') VALUES('+','.join('?' for _ in row)+')',tuple(row.values()))
    assert db.all('PRAGMA foreign_key_check')==[]

def test_parse_failure_retry_cancel_and_restart_budget(env,monkeypatch):
    import server.documents as module
    c,h,cid,app,cfg=env;docs=app.state.documents;db=app.state.services[0]
    async def failure(*args):raise DomainError('PARSER_TIMEOUT')
    monkeypatch.setattr(module,'parse_in_job',failure)
    fid,_=ready(env)
    assert c.get('/api/v1/files/'+fid+'/status').json()['data']['status']=='failed'
    for _ in range(2):
        r=c.post('/api/v1/files/'+fid+'/retry',json={},headers={**h,'Idempotency-Key':str(uuid.uuid4())});assert r.status_code==202,r.text
        c.portal.call(docs.wait_for_file,fid)
    assert c.post('/api/v1/files/'+fid+'/retry',json={},headers={**h,'Idempotency-Key':str(uuid.uuid4())}).status_code==409
    async def waiting(*args):await asyncio.Event().wait()
    monkeypatch.setattr(module,'parse_in_job',waiting)
    second,_=upload(env,raw=b'second');fid2=second.json()['data']['file']['id']
    assert c.post('/api/v1/files/'+fid2+'/cancel',json={},headers=h).status_code==200
    c.portal.call(docs.wait_for_file,fid2)
    assert c.get('/api/v1/files/'+fid2+'/status').json()['data']['status']=='cancelled'
    # Crash-state rows are controlled unit fixtures, not a real restart claim.
    db.run("UPDATE parse_jobs SET status='running',restart_recoveries=1 WHERE file_id=?",(fid2,))
    (cfg.data_dir/'objects/orphan').write_text('orphan');(cfg.data_dir/'parsed/stale').write_text('stale')
    c.portal.call(asyncio.to_thread,lambda:None)
    async def recover():docs.recover()
    c.portal.call(recover)
    assert db.one('SELECT error_code FROM parse_jobs WHERE file_id=?',(fid2,))['error_code']=='PROCESS_INTERRUPTED'
    assert not (cfg.data_dir/'objects/orphan').exists() and not list((cfg.data_dir/'parsed').iterdir())

def test_summary_coverage_scope_budget_and_no_match(env,monkeypatch):
    from server.retrieval import prepare_retrieval,uniform
    from server.schemas import Question
    c,h,cid,app,cfg=env;fid,_=ready(env,raw=('培训预算7,600元。\n\n'+'别的段落。'*200).encode());docs=app.state.documents;owner=app.state.services[0].one('SELECT owner_id FROM conversations WHERE id=?',(cid,))['owner_id']
    question=Question(content='概述',mode='document',intent='summary',file_refs=[{'file_id':fid,'version':1}])
    messages,coverage,sources,files,budget=prepare_retrieval(docs,cfg,owner,cid,question,[{'role':'system','content':'unit'},{'role':'user','content':'概述'}])
    assert coverage['selection']=='sampled_summary' and coverage['selected_chunks']>=1
    assert coverage['selected_chunks']+coverage['omitted_chunks']==coverage['eligible_chunks']
    assert coverage['rank_omitted']+coverage['budget_omitted']==coverage['omitted_chunks']
    assert all(f['positive_chunks'] is None and f['no_match'] is None for f in coverage['files'])
    assert len(uniform([{'id':i} for i in range(9)]))==9
    cfg.context_tokens=1100
    with pytest.raises(DomainError) as error:prepare_retrieval(docs,cfg,owner,cid,question,messages)
    assert error.value.code=='SCOPE_TOO_LARGE'
    cfg.context_tokens=8192
    question.intent='question';question.content='zzzz_unknown'
    _,cov,source,_,_=prepare_retrieval(docs,cfg,owner,cid,question,[{'role':'system','content':'unit'},{'role':'user','content':'zzzz_unknown'}])
    assert not source and cov['selected_chunks']==0

def test_retrieval_rejects_disabled_and_missing_index_without_scope_substitution(env):
    from server.retrieval import prepare_retrieval
    from server.schemas import Question
    c,h,cid,app,cfg=env;fid,_=ready(env);docs=app.state.documents;db=app.state.services[0];owner=db.one('SELECT owner_id FROM conversations WHERE id=?',(cid,))['owner_id']
    question=Question(content='预算',mode='document',intent='question',file_refs=[{'file_id':fid,'version':1}]);history=[{'role':'system','content':'unit'},{'role':'user','content':'预算'}]
    other=c.post('/api/v1/conversations',json={},headers={**h,'Idempotency-Key':str(uuid.uuid4())}).json()['data']['id']
    with pytest.raises(DomainError):prepare_retrieval(docs,cfg,owner,other,question,history)
    c.patch('/api/v1/conversations/'+cid+'/files/'+fid,json=dict(enabled=False,revision=1),headers=h)
    with pytest.raises(DomainError) as e:prepare_retrieval(docs,cfg,owner,cid,question,history)
    assert e.value.code=='FILE_DISABLED'
    c.patch('/api/v1/conversations/'+cid+'/files/'+fid,json=dict(enabled=True,revision=2),headers=h)
    db.run("UPDATE files SET status='parsing' WHERE id=?",(fid,))
    with pytest.raises(DomainError) as e:prepare_retrieval(docs,cfg,owner,cid,question,history)
    assert e.value.code=='FILE_NOT_READY'
    db.run("UPDATE files SET status='ready' WHERE id=?",(fid,));db.run('DELETE FROM chunk_terms');db.run('DELETE FROM chunks')
    with pytest.raises(DomainError) as e:prepare_retrieval(docs,cfg,owner,cid,question,history)
    assert e.value.code=='INDEX_INCONSISTENT'

def test_upload_capacity_lease_and_byte_quota(env):
    c,h,cid,app,cfg=env;db=app.state.services[0];owner=db.one('SELECT owner_id FROM conversations WHERE id=?',(cid,))['owner_id']
    keys=[]
    for n in range(2):
        key=str(uuid.uuid4());keys.append(key);db.run('INSERT INTO upload_reservations VALUES(?,?,?,?,?,?,?,?,?)',(str(uuid.uuid4()),owner,cid,key,100,0,'staging/unit'+str(n),time.time()+60,'unit'))
    assert upload(env)[0].status_code==429
    db.run('DELETE FROM upload_reservations WHERE idempotency_key=?',(keys[1],))
    assert upload(env,extra={'Idempotency-Key':keys[0]})[0].status_code==409
    db.run('DELETE FROM upload_reservations')
    fid,_=ready(env);db.run('UPDATE files SET size=49999999 WHERE id=?',(fid,))
    assert upload(env,raw=b'other unique')[0].status_code==413
