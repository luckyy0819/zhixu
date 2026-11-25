import asyncio,json,uuid,runpy,errno
import pytest
from test_document_boundaries import env,ready,upload

def test_disabling_mid_generation_and_deleting_mid_parse_never_publish_old_source(env,monkeypatch):
    import server.generation as gen
    import server.documents as parsers
    continued=asyncio.Event()
    async def source(cfg,messages):
        data=json.loads(messages[-1]['content']);evidence=data['untrusted_document_evidence'][0];span=evidence['spans'][0]
        yield json.dumps(dict(kind='fact',text='培训预算7,600元。',sources=[dict(source_token=evidence['source_token'],**span)]))+'\n'
        continued.set();await asyncio.Event().wait()
    monkeypatch.setattr(gen,'stream_model',source)
    c,h,cid,app,cfg=env;fid,_=ready(env)
    result=c.post('/api/v1/conversations/'+cid+'/messages',headers={**h,'Idempotency-Key':str(uuid.uuid4())},json=dict(content='预算',mode='document',intent='question',file_refs=[{'file_id':fid,'version':1}])).json()['data'];rid=result['generation']['id']
    c.portal.call(continued.wait)
    assert c.patch('/api/v1/conversations/'+cid+'/files/'+fid,headers=h,json=dict(enabled=False,revision=1)).status_code==200
    async def settle():
        task=app.state.services[3].tasks.get(rid)
        if task:await task
    c.portal.call(settle)
    snap=c.get('/api/v1/requests/'+rid).json()['data'];assert snap['status']=='cancelled' and snap['events_invalidated']
    assert snap['claims'][0]['text']=='培训预算7,600元。'
    assert c.get(result['events_url']).status_code==410
    async def parse_wait(*a):await asyncio.Event().wait()
    monkeypatch.setattr(parsers,'parse_in_job',parse_wait)
    response,_=upload(env,raw=b'pending parse');fid2=response.json()['data']['file']['id']
    deleted=c.request('DELETE','/api/v1/files/'+fid2,headers=h,json={'confirmation':'DELETE'});assert deleted.status_code==202
    c.portal.call(app.state.deletion.wait_for_job,deleted.json()['data']['id'])
    assert c.get('/api/v1/files/'+fid2+'/status').status_code==404
    assert app.state.services[0].all('SELECT * FROM chunks WHERE file_id=?',(fid2,))==[]

def test_launch_entry_and_metadata_consistency(tmp_path,monkeypatch):
    from server.config import Settings
    import server.main,uvicorn,server,json
    cfg=Settings(data_dir=tmp_path,port=8788,origin='http://127.0.0.1:8788',model_base_url='http://localhost:1/v1',model_name='test')
    monkeypatch.setattr(Settings,'load',lambda:cfg);monkeypatch.setattr(server.main,'create_app',lambda c:'unit-app')
    called=[];monkeypatch.setattr(uvicorn,'run',lambda *args,**kwargs:called.append((args,kwargs)))
    runpy.run_module('server',run_name='__main__');assert called[0][1]==dict(host='127.0.0.1',port=8788,workers=1,access_log=False)
    from pathlib import Path
    root=Path(__file__).resolve().parents[2]
    assert server.__version__==json.loads((root/'package.json').read_text())['version']==json.loads((root/'package-lock.json').read_text())['packages']['']['version']

def test_upload_storage_full_is_typed_and_cleans_reservation(env,monkeypatch):
    import server.document_upload as module
    def full(*a):raise OSError(errno.ENOSPC,'controlled unit storage full')
    monkeypatch.setattr(module.os,'replace',full)
    response,_=upload(env);assert response.status_code==503 and response.json()['error']['code']=='STORAGE_FULL'
    assert env[3].state.services[0].all('SELECT * FROM upload_reservations')==[]
    assert not list((env[4].data_dir/'staging').iterdir())
