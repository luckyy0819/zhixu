import json,uuid
from pathlib import Path
import pytest
from server.retrieval import document_prompt,prepare_retrieval
from server.schemas import Question
from test_document_boundaries import env,ready

@pytest.mark.parametrize('text',['A\u0301','A\U0001d165','😀和e\u0301与"引号"及\\反斜杠'])
def test_all_combining_and_json_text_preserve_exact_codepoints(text):
    chunk=dict(id='one',text=text,start=0,end=len(text))
    messages=document_prompt([dict(role='system',content='unit'),dict(role='user',content=text)],[chunk],{'one':'safe'})
    data=json.loads(messages[-1]['content'])
    assert data['question']==text
    assert data['untrusted_document_evidence'][0]['spans'][0]['quote']==text

def test_all_runtime_unicode_combining_characters_roundtrip_without_normalization():
    import unicodedata
    text=''.join(chr(n) for n in range(0x110000) if unicodedata.combining(chr(n)))
    assert len(text)>500 and any(ord(c)>0xffff for c in text)
    prompt=document_prompt([dict(role='system',content='unit'),dict(role='user',content=text)],[dict(id='all',text=text,start=0,end=len(text))],{'all':'safe'})
    data=json.loads(prompt[-1]['content'])
    assert data['question']==text
    assert ''.join(s['quote'] for s in data['untrusted_document_evidence'][0]['spans'])==text

def test_actual_partial_pdf_parse_limits_reach_final_model_prompt(env):
    c,h,cid,app,cfg=env;p=Path(__file__).resolve().parents[2]/'tests/fixtures/zhixu-p0-v1/ZHIXU-TC-G050.pdf'
    fid,_=ready(env,raw=p.read_bytes(),name=p.name,extra={'Content-Type':'application/pdf'})
    owner=app.state.services[0].one('SELECT owner_id FROM conversations WHERE id=?',(cid,))['owner_id']
    q=Question(content='操作窗口及缺页范围？',mode='document',intent='question',file_refs=[dict(file_id=fid,version=1)])
    messages,coverage,sources,files,budget=prepare_retrieval(app.state.documents,cfg,owner,cid,q,[dict(role='system',content='unit'),dict(role='user',content=q.content)])
    assert len([m for m in messages if m['role']=='system'])==1
    assert json.loads(messages[-1]['content'])['trusted_parse_limits']==[dict(file_id=fid,version=1,unread_pages=[2])]
    assert sources and coverage['files'][0]['warnings'][0]['pages']==[2]
    assert budget['estimated_tokens']==len(json.dumps(messages,ensure_ascii=False,separators=(',',':')).encode())+512+cfg.output_tokens

def test_no_match_still_reports_verified_partial_page_limit_without_model(env):
    c,h,cid,app,cfg=env;p=Path(__file__).resolve().parents[2]/'tests/fixtures/zhixu-p0-v1/ZHIXU-TC-G050.pdf'
    fid,_=ready(env,raw=p.read_bytes(),name=p.name,extra={'Content-Type':'application/pdf'})
    response=c.post('/api/v1/conversations/'+cid+'/messages',headers={**h,'Idempotency-Key':str(uuid.uuid4())},json=dict(content='unmatched_qxznohit',mode='document',intent='question',file_refs=[dict(file_id=fid,version=1)]))
    assert response.status_code==202,response.text
    data=response.json()['data'];events=c.get(data['events_url']);snapshot=c.get('/api/v1/requests/'+data['generation']['id']).json()['data']
    assert snapshot['status']=='completed' and 'event: done' in events.text
    assert all(c['kind']=='no_answer' for c in snapshot['claims']) and snapshot['citations']==[]
    assert '第2页无可提取文字' in snapshot['content']
