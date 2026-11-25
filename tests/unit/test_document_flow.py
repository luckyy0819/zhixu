import asyncio
import uuid
from fastapi.testclient import TestClient
from server.config import Settings
from server.main import create_app
from test_lifecycle import sign_in


def test_text_upload_real_worker_duplicate_and_authorized_preview(tmp_path):
    cfg=Settings(data_dir=tmp_path,port=8788,origin='http://127.0.0.1:8788',model_base_url='http://localhost:1/v1',model_name='test')
    with TestClient(create_app(cfg),base_url=cfg.origin) as c:
        h=sign_in(c,cfg,'upload_a');cid=c.post('/api/v1/conversations',json={},headers=h).json()['data']['id']
        raw='培训预算7,600元。\n\n联系人：合成甲。'.encode()
        h.update({'Idempotency-Key':str(uuid.uuid4()),'Content-Type':'text/plain','X-File-Name':'budget.txt','X-File-Size':str(len(raw))})
        r=c.post('/api/v1/conversations/'+cid+'/files',content=raw,headers=h)
        assert r.status_code==202,r.text
        fid=r.json()['data']['file']['id']
        # Use the actual worker task synchronization, not fabricated ready state.
        c.portal.call(c.app.state.documents.wait_for_file,fid)
        item=c.get('/api/v1/files/'+fid+'/status').json()['data']
        assert item['status']=='ready',item
        assert item['paragraph_count']==2
        assert c.get('/api/v1/files/'+fid+'/preview?version=1&paragraph=1&start=4&end=9').status_code==200
        assert c.get('/api/v1/files/'+fid+'/preview?version=2&paragraph=1').status_code==409
        assert c.post('/api/v1/conversations/'+cid+'/files',content=raw,headers=h).json()['data']['deduplicated'] is True
        c.cookies.clear();hb=sign_in(c,cfg,'upload_b')
        assert c.get('/api/v1/files/'+fid+'/status').status_code==404
        assert c.get('/api/v1/files/'+fid+'/preview?version=1&paragraph=1').status_code==404
