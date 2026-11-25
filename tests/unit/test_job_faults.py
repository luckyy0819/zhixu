"""Fault injection of Win32 API seam. This does not replace actual Job tests."""
import asyncio,json
from pathlib import Path
import pytest
import win32job,win32process,win32event
from server.process_limits import parse_in_job
from server.errors import DomainError

def test_unavailable_job_fails_closed_before_resume(tmp_path,monkeypatch):
    source=tmp_path/'normal.txt';source.write_text('安全合成资料')
    resumed=[]
    monkeypatch.setattr(win32job,'CreateJobObject',lambda *args:(_ for _ in ()).throw(OSError('unit API unavailable')))
    monkeypatch.setattr(win32process,'ResumeThread',lambda *args:resumed.append(True))
    with pytest.raises(DomainError) as e:asyncio.run(parse_in_job(source,'txt',tmp_path/'out.json'))
    assert e.value.code=='PARSER_ISOLATION_UNAVAILABLE' and not resumed

@pytest.mark.parametrize('result,exit_code,content,expected',[
 ('timeout',0,None,'PARSER_TIMEOUT'),('done',1,None,'PARSER_FAILED'),
 ('done',0,{'error':'PARSER_RESOURCE_LIMIT'},'PARSER_RESOURCE_LIMIT'),('done',0,{'error':'unknown'},'PARSER_FAILED'),
 ('done',0,{},'PARSER_FAILED'),
 ('done',0,{'data':{}},'PARSER_FAILED'),
 ('done',0,{'data':{'paragraphs':[],'page_count':None,'warnings':[],'parser_revision':'pypdf7.1.1-defusedxml0.7.1-r2'}},'WORKER_OUTPUT_LIMIT'),
 *[('done',0,{'data':dict(paragraphs=[{'paragraph_no':1,'page':None,'text':'合成'}],page_count=None,warnings=[],parser_revision='pypdf7.1.1-defusedxml0.7.1-r2',**{})|changes},code) for changes,code in [
   ({'parser_revision':'wrong'},'PARSER_FAILED'),({'page_count':2},'PARSER_FAILED'),({'warnings':{}},'PARSER_FAILED'),
   ({'warnings':[{'code':'wrong','pages':[],'message':'unit'}]},'PARSER_FAILED'),
   ({'paragraphs':[{'paragraph_no':2,'page':None,'text':'合成'}]},'PARSER_FAILED'),
   ({'paragraphs':[{'paragraph_no':1,'page':201,'text':'合成'}]},'PARSER_FAILED'),
   ({'paragraphs':[{'paragraph_no':1,'page':None,'text':'unit\0'}]},'BINARY_TEXT'),
   ({'paragraphs':[{'paragraph_no':1,'page':None,'text':'a'*5242881}]},'TEXT_LIMIT'),
 ]],
])
def test_job_transport_faults_are_bounded_and_cleanup_all_handles(tmp_path,monkeypatch,result,exit_code,content,expected):
    source=tmp_path/'normal.txt';source.write_text('合成');target=tmp_path/'out.json';calls=[]
    class Handle:
        def Close(self):calls.append('close')
    limits={'BasicLimitInformation':{'LimitFlags':0,'ActiveProcessLimit':0},'ProcessMemoryLimit':0}
    monkeypatch.setattr(win32job,'CreateJobObject',lambda *a:Handle())
    monkeypatch.setattr(win32job,'QueryInformationJobObject',lambda *a:limits)
    monkeypatch.setattr(win32job,'SetInformationJobObject',lambda *a:None)
    monkeypatch.setattr(win32process,'CreateProcess',lambda *a:(Handle(),Handle(),1,2))
    monkeypatch.setattr(win32job,'AssignProcessToJobObject',lambda *a:calls.append('assign'))
    def resume(*args):
        calls.append('resume')
        if content is not None:target.write_text(json.dumps(content),encoding='utf-8')
    monkeypatch.setattr(win32process,'ResumeThread',resume)
    monkeypatch.setattr(win32event,'WaitForSingleObject',lambda *a:win32event.WAIT_TIMEOUT if result=='timeout' else win32event.WAIT_OBJECT_0)
    monkeypatch.setattr(win32process,'GetExitCodeProcess',lambda *a:exit_code)
    with pytest.raises(DomainError) as e:asyncio.run(parse_in_job(source,'txt',target))
    assert e.value.code==expected and calls[:2]==['assign','resume'] and calls.count('close')==3
    assert not target.exists()

def test_assignment_refusal_terminates_suspended_process(tmp_path,monkeypatch):
    source=tmp_path/'normal.txt';source.write_text('合成');stopped=[]
    monkeypatch.setattr(win32job,'AssignProcessToJobObject',lambda *a:(_ for _ in ()).throw(OSError('unit assignment refusal')))
    original=win32process.TerminateProcess
    def terminate(*args):stopped.append(True);return original(*args)
    monkeypatch.setattr(win32process,'TerminateProcess',terminate)
    with pytest.raises(DomainError) as e:asyncio.run(parse_in_job(source,'txt',tmp_path/'out.json'))
    assert e.value.code=='PARSER_ISOLATION_UNAVAILABLE' and stopped
