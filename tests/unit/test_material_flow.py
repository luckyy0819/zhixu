import json
import secrets
import uuid
from fastapi.testclient import TestClient
from server.config import Settings
from server.main import create_app
from test_lifecycle import sign_in


def test_claim_citation_disable_delete_history_and_account_receipt(tmp_path,monkeypatch):
    import server.generation as gen
    prompts=[]
    async def upstream(cfg,messages):
        prompts.append(messages)
        try:
            data=json.loads(messages[-1]['content']);source=data['untrusted_document_evidence'][0];span=source['spans'][0]
            yield json.dumps({'kind':'fact','text':'培训预算为7,600元。','sources':[{'source_token':source['source_token'],**span}]},ensure_ascii=False)+'\n'
        except (ValueError,KeyError):
            yield '独立通用回答'
    monkeypatch.setattr(gen,'stream_model',upstream)
    cfg=Settings(data_dir=tmp_path,port=8788,origin='http://127.0.0.1:8788',model_base_url='http://localhost:1/v1',model_name='test')
    app=create_app(cfg)
    with TestClient(app,base_url=cfg.origin) as c:
        h=sign_in(c,cfg,'material_a');cid=c.post('/api/v1/conversations',json={},headers=h).json()['data']['id']
        raw='培训预算7,600元。'.encode();h['Idempotency-Key']=str(uuid.uuid4())
        file=c.post('/api/v1/conversations/'+cid+'/files',content=raw,headers={**h,'Content-Type':'text/plain','X-File-Name':'budget.txt','X-File-Size':str(len(raw))}).json()['data']['file'];fid=file['id']
        c.portal.call(app.state.documents.wait_for_file,fid)
        h['Idempotency-Key']=str(uuid.uuid4())
        result=c.post('/api/v1/conversations/'+cid+'/messages',headers=h,json={'content':'培训预算多少？','mode':'document','intent':'question','file_refs':[{'file_id':fid,'version':1}]}).json()['data']
        events=c.get(result['events_url']).text
        assert 'event: citation' in events,events
        rid=result['generation']['id'];snapshot=c.get('/api/v1/requests/'+rid).json()['data']
        assert snapshot['status']=='completed' and snapshot['claims'][0]['kind']=='fact'
        citation=snapshot['citations'][0]
        preview=c.get('/api/v1/citations/'+citation['id']+'/preview').json()['data']
        assert preview['text'][preview['highlight']['start']:preview['highlight']['end']]==citation['quote']
        assert c.patch('/api/v1/conversations/'+cid+'/files/'+fid,json={'enabled':False,'revision':1},headers=h).status_code==200
        h['Idempotency-Key']=str(uuid.uuid4())
        general=c.post('/api/v1/conversations/'+cid+'/messages',headers=h,json={'content':'继续一般问题','mode':'general','intent':'question','file_refs':[]}).json()['data']
        c.get(general['events_url'])
        assert not any('7,600' in m['content'] for m in prompts[-1])
        assert c.get('/api/v1/requests/'+general['generation']['id']).json()['data']['scope_exclusions']
        deletion=c.request('DELETE','/api/v1/files/'+fid,headers=h,json={'confirmation':'DELETE'})
        assert deletion.status_code==202,deletion.text
        c.portal.call(app.state.deletion.wait_for_job,deletion.json()['data']['id'])
        assert c.get('/api/v1/files/'+fid+'/preview?version=1&paragraph=1').status_code==410
        assert c.get('/api/v1/citations/'+citation['id']+'/preview').status_code==410
        assert c.get('/api/v1/requests/'+rid+'/events').status_code==410
        snap=c.get('/api/v1/requests/'+rid).json()['data']
        assert snap['citations'][0]['quote']=='' and snap['citations'][0]['source_status']=='deleted'
        assert not list((tmp_path/'objects').iterdir())
        assert app.state.services[0].all('PRAGMA foreign_key_check')==[]
        job_id=str(uuid.uuid4());token=secrets.token_urlsafe(32)
        body={'confirmation':'DELETE','current_password':'synthetic-pass-123','deletion_key':job_id,'receipt_token':token}
        removed=c.request('DELETE','/api/v1/account/data',headers=h,json=body)
        assert removed.status_code==202,removed.text
        c.portal.call(app.state.deletion.wait_for_job,job_id)
        assert c.get('/api/v1/auth/me').status_code==401
        assert c.get('/api/v1/deletions/'+job_id,headers={'X-Deletion-Receipt':token}).json()['data']['status']=='completed'
        assert c.get('/api/v1/deletions/'+job_id,headers={'X-Deletion-Receipt':'wrong'}).status_code==404
        assert app.state.services[0].all('PRAGMA foreign_key_check')==[]
