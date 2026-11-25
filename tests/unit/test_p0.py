"""ZHIXU-TASK-005 W1/W2 regression: real SQLite and HTTP, no fake model success."""
import uuid
import pytest
from fastapi.testclient import TestClient


def test_health_security_and_persistence(tmp_path, monkeypatch):
    from server.main import create_app
    from server.config import Settings
    cfg = Settings(data_dir=tmp_path, model_base_url='http://127.0.0.1:1/v1', model_name='test')
    with TestClient(create_app(cfg), base_url=cfg.origin) as c:
        assert c.get('/api/v1/health').json()['data']['status'] == 'ok'
        assert c.get('/api/v1/health').headers['x-zhixu-version']=='0.1.0-p0.2'
        runtime_audit=(tmp_path/'runtime-audit.jsonl').read_text(encoding='utf-8')
        assert 'ZHIXU-TASK-005' in runtime_audit and 'password' not in runtime_audit
        assert c.get('/api/v1/conversations').status_code == 401
        assert c.get('/.env').status_code == 404
        assert c.get('/docs/engineering/design/02-LLD.md').status_code == 404
        nonce = c.get('/api/v1/auth/csrf').json()['data']['csrf_token']
        h = {'Origin': cfg.origin, 'X-CSRF-Token': nonce}
        assert c.post('/api/v1/auth/register', json={'username':'user_a','display_name':'A','password':'synthetic-password-123'}, headers=h).status_code == 201
        me = c.get('/api/v1/auth/me').json()['data']
        h['X-CSRF-Token'] = me['csrf_token']
        key = str(uuid.uuid4()); h['Idempotency-Key'] = key
        r = c.post('/api/v1/conversations',json={},headers=h)
        assert r.status_code == 201, r.text
        cid = r.json()['data']['id']
        assert c.post('/api/v1/conversations',json={},headers=h).json()['data']['id'] == cid
        assert c.post('/api/v1/conversations',json={'title':'different'},headers=h).status_code == 409
        assert c.patch('/api/v1/conversations/'+cid,json={'title':'changed','revision':1},headers={**h,'Origin':'https://evil.invalid'}).status_code == 403
        cookies = dict(c.cookies)
    with TestClient(create_app(cfg), base_url=cfg.origin) as c:
        c.cookies.update(cookies)
        assert c.get('/api/v1/conversations/'+cid).status_code == 200
        c.cookies.clear()
        nonce = c.get('/api/v1/auth/csrf').json()['data']['csrf_token']
        r=c.post('/api/v1/auth/register',json={'username':'user_b','display_name':'B','password':'synthetic-password-456'},headers={'Origin':cfg.origin,'X-CSRF-Token':nonce})
        assert r.status_code==201
        assert c.get('/api/v1/conversations/'+cid).status_code == 404
        assert c.get('/api/v1/conversations/'+cid+'/messages').status_code == 404
        assert c.get('/api/v1/conversations').json()['data']['items'] == []


def test_config_rejects_unapproved_listen(tmp_path):
    from server.config import Settings
    with pytest.raises(ValueError):
        Settings(data_dir=tmp_path, model_base_url='http://localhost:1/v1',model_name='x',host='0.0.0.0')
