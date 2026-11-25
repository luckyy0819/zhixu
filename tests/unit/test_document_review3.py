"""D-F02/03 and legal G050 regressions; no exploit/resource-exhaustion inputs."""
import asyncio,uuid
from pathlib import Path
import pytest
from server.errors import DomainError
from server.process_limits import parse_in_job
from test_document_boundaries import env,ready,upload
from test_claim_stream_boundaries import collect

@pytest.mark.parametrize('suffix',['','\n'])
def test_duplicate_json_keys_rejected_in_complete_and_last_records(suffix):
    with pytest.raises(DomainError) as error:
        collect(['{"kind":"no_answer","text":"资料不足","text":"未提供","sources":[]}'+suffix])
    assert error.value.code=='MODEL_FORMAT_ERROR'

def test_legal_partial_pdf_does_not_require_image_decode(tmp_path):
    source=Path(__file__).resolve().parents[2]/'tests/fixtures/zhixu-p0-v1/ZHIXU-TC-G050.pdf'
    result=asyncio.run(parse_in_job(source,'pdf',tmp_path/'result.json'))
    assert result['page_count']==2 and result['warnings'][0]['pages']==[2]
    assert result['paragraphs'][0]['page']==1

def test_nested_duplicate_and_nonstandard_numbers_fail_json_decode():
    from server.document_claims import decode_claim
    for value in ['{"sources":[{"quote":"x","quote":"y"}]}','{"text":NaN}','{"text":Infinity}','{"text":-Infinity}']:
        with pytest.raises(ValueError):decode_claim(value)

def test_model_evidence_json_escapes_preserve_original_combining_codepoints():
    import json
    from server.retrieval import document_prompt
    text='标记😀及e\u0301与字母A\u030a。'
    chunk=dict(id='unit',text=text,start=0,end=len(text))
    prompt=document_prompt([{'role':'system','content':'unit'},{'role':'user','content':'问题'}],[chunk],{'unit':'source'})[-1]['content']
    assert '\\u0301' in prompt and '\\u030a' in prompt
    assert json.loads(prompt)['untrusted_document_evidence'][0]['spans'][0]['quote']==text

def test_plain_image_pdf_classifies_without_pillow_or_pixel_decode(tmp_path):
    from pypdf import PdfWriter
    from pypdf.generic import DictionaryObject,NameObject,NumberObject,DecodedStreamObject,ArrayObject
    writer=PdfWriter();page=writer.add_blank_page(100,100);image=DecodedStreamObject();image.set_data(b'\x00')
    image.update({NameObject('/Type'):NameObject('/XObject'),NameObject('/Subtype'):NameObject('/Image'),NameObject('/Width'):NumberObject(1),NameObject('/Height'):NumberObject(1),NameObject('/ColorSpace'):NameObject('/DeviceGray'),NameObject('/BitsPerComponent'):NumberObject(8)})
    page[NameObject('/Resources')]=DictionaryObject({NameObject('/XObject'):DictionaryObject({NameObject('/Im1'):writer._add_object(image)}),NameObject('/ProcSet'):ArrayObject([NameObject('/PDF'),NameObject('/ImageB')])})
    source=tmp_path/'image.pdf'
    with source.open('wb') as stream:writer.write(stream)
    with pytest.raises(DomainError) as error:asyncio.run(parse_in_job(source,'pdf',tmp_path/'image.json'))
    assert error.value.code=='SCAN_ONLY'
    # Unit coverage of the same classifier; the production route above already
    # exercised the real Job and never decodes pixels or falls back in-process.
    from server.parser_worker import parse,ParseError
    with pytest.raises(ParseError,match='SCAN_ONLY'):parse(source,'pdf')

@pytest.mark.parametrize('operation',['cancel','retry'])
def test_revoked_actor_cannot_mutate_parse_job_after_waiting_gate(env,monkeypatch,operation):
    import server.documents as module
    c,h,cid,app,cfg=env;docs=app.state.documents;db=app.state.services[0]
    async def boundary(*args):
        if operation=='retry':raise DomainError('PARSER_TIMEOUT')
        await asyncio.Event().wait()
    monkeypatch.setattr(module,'parse_in_job',boundary)
    response,_=upload(env);fid=response.json()['data']['file']['id']
    if operation=='retry':c.portal.call(docs.wait_for_file,fid)
    actor=dict(db.one('SELECT * FROM auth_sessions'));before=[dict(r) for r in db.all('SELECT * FROM parse_jobs WHERE file_id=?',(fid,))]
    async def scenario():
        gate=docs.gates[actor['user_id']];await gate.lock.acquire();entered=asyncio.Event()
        async def write():
            entered.set()
            async with gate.work():
                if operation=='cancel':docs.cancel(fid,actor)
                else:docs.retry(None,fid,actor)
        task=asyncio.create_task(write());await entered.wait()
        revoked=gate.revoke(lambda:db.run('DELETE FROM auth_sessions WHERE id=?',(actor['id'],)))
        gate.lock.release();await revoked
        with pytest.raises(DomainError) as error:await task
        assert error.value.code=='AUTH_REQUIRED'
    c.portal.call(scenario)
    assert [dict(r) for r in db.all('SELECT * FROM parse_jobs WHERE file_id=?',(fid,))]==before
