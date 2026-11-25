"""ZHIXU DES002 R7: suspended base interpreter -> nested Job -> ResumeThread."""
import asyncio
import json
import os
import subprocess
import sys
import sysconfig
import logging
from uuid import uuid4
from pathlib import Path
from .document_errors import doc_error


async def parse_in_job(source,kind,target,on_phase=None):
    job=process=thread=None;waiter=None
    try:
        import win32job,win32process,win32event
        job=win32job.CreateJobObject(None,'ZhixuParser-'+uuid4().hex)
        limits=win32job.QueryInformationJobObject(job,win32job.JobObjectExtendedLimitInformation)
        flags=(win32job.JOB_OBJECT_LIMIT_ACTIVE_PROCESS|win32job.JOB_OBJECT_LIMIT_PROCESS_MEMORY|win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE)
        limits['BasicLimitInformation']['LimitFlags']=flags
        limits['BasicLimitInformation']['ActiveProcessLimit']=1
        limits['ProcessMemoryLimit']=536870912
        win32job.SetInformationJobObject(job,win32job.JobObjectExtendedLimitInformation,limits)
        configured=win32job.QueryInformationJobObject(job,win32job.JobObjectExtendedLimitInformation)
        if configured['ProcessMemoryLimit']!=536870912 or configured['BasicLimitInformation']['ActiveProcessLimit']!=1 or configured['BasicLimitInformation']['LimitFlags']&flags!=flags:
            raise RuntimeError('Job limits readback mismatch')
        # Windows venv launchers create another process: use the base interpreter
        # directly, with -I -S and only this venv's explicit dependency directory.
        executable=str(Path(sys._base_executable).resolve())
        arguments=[executable,'-I','-S',str(Path(__file__).with_name('parser_worker.py')),str(Path(source).resolve()),kind,str(Path(target).resolve()),sysconfig.get_paths()['purelib']]
        environment={k:os.environ[k] for k in ('SystemRoot','TEMP','TMP') if k in os.environ}
        process,thread,pid,tid=win32process.CreateProcess(executable,subprocess.list2cmdline(arguments),None,None,False,
            win32process.CREATE_SUSPENDED|win32process.CREATE_NO_WINDOW,environment,str(Path(target).parent),win32process.STARTUPINFO())
        win32job.AssignProcessToJobObject(job,process)
        win32process.ResumeThread(thread)
    except Exception as error:
        logging.getLogger('zhixu.parser').warning('PARSER_ISOLATION_UNAVAILABLE exception=%s winerror=%s',type(error).__name__,getattr(error,'winerror',None))
        if process is not None:
            try:
                win32process.TerminateProcess(process,1)
            except Exception:
                pass
        for handle in (thread,process,job):
            if handle is not None:
                handle.Close()
        raise doc_error('PARSER_ISOLATION_UNAVAILABLE')
    try:
        waiter=asyncio.create_task(asyncio.to_thread(win32event.WaitForSingleObject,process,60000))
        last_phase=None
        while not waiter.done():
            await asyncio.wait([waiter],timeout=.1)
            marker=Path(str(target)+'.phase')
            if on_phase is not None and marker.is_file():
                value=marker.read_text(encoding='ascii')
                if value in ('security_check','parsing') and value!=last_phase:
                    last_phase=value;await on_phase(value)
        result=waiter.result()
        if result==win32event.WAIT_TIMEOUT:
            raise doc_error('PARSER_TIMEOUT')
        if win32process.GetExitCodeProcess(process)!=0 or not Path(target).is_file():
            raise doc_error('PARSER_FAILED')
        if Path(target).stat().st_size>16777216:
            raise doc_error('WORKER_OUTPUT_LIMIT')
        with Path(target).open('rb') as stream:
            raw=stream.read(16777217)
        if len(raw)>16777216:
            raise doc_error('WORKER_OUTPUT_LIMIT')
        value=json.loads(raw)
        if 'error' in value:
            code=value['error']
            allowed={'PARSER_RESOURCE_LIMIT','TEXT_ENCODING','BINARY_TEXT','EMPTY_DOCUMENT','PAGE_LIMIT','EXPANSION_LIMIT','TEXT_LIMIT','WORKER_OUTPUT_LIMIT','ENCRYPTED_DOCUMENT','UNSAFE_DOCUMENT','CORRUPT_DOCUMENT','MIME_MISMATCH','SCAN_ONLY','NO_EXTRACTABLE_TEXT'}
            raise doc_error(code if code in allowed else 'PARSER_FAILED')
        if set(value)!={'data'} or not isinstance(value['data'],dict):
            raise doc_error('PARSER_FAILED')
        data=value['data']
        if set(data)!={'paragraphs','page_count','warnings','parser_revision'} or data['parser_revision']!='pypdf7.1.1-defusedxml0.7.1-r2':
            raise doc_error('PARSER_FAILED')
        if kind=='pdf' and (type(data['page_count']) is not int or not 1<=data['page_count']<=200) or kind!='pdf' and data['page_count'] is not None:
            raise doc_error('PARSER_FAILED')
        if not isinstance(data['warnings'],list) or len(data['warnings'])>1:
            raise doc_error('PARSER_FAILED')
        for warning in data['warnings']:
            if set(warning)!={'code','pages','message'} or warning['code']!='PARTIAL_TEXT' or not isinstance(warning['message'],str) or len(warning['message'])>200 or not isinstance(warning['pages'],list) or any(type(p) is not int or not 1<=p<=data['page_count'] for p in warning['pages']):
                raise doc_error('PARSER_FAILED')
        paragraphs=data['paragraphs']
        if not isinstance(paragraphs,list) or not 1<=len(paragraphs)<=50000:
            raise doc_error('WORKER_OUTPUT_LIMIT')
        total=0
        for number,row in enumerate(paragraphs,1):
            if set(row)!={'paragraph_no','page','text'} or row['paragraph_no']!=number or not isinstance(row['text'],str):
                raise doc_error('PARSER_FAILED')
            if row['page'] is not None and (type(row['page']) is not int or not 1<=row['page']<=200):
                raise doc_error('PARSER_FAILED')
            if any(ord(c)<32 and c not in '\n\t' for c in row['text']):
                raise doc_error('BINARY_TEXT')
            total+=len(row['text'].encode())
        if total>5242880:
            raise doc_error('TEXT_LIMIT')
        data['isolation']={'assigned_before_resume':True,'memory_bytes':536870912,'active_process_limit':1,'kill_on_job_close':True,'wall_timeout_seconds':60,'pid':pid}
        return data
    except (ValueError,KeyError,TypeError):
        raise doc_error('PARSER_FAILED')
    finally:
        if win32process.GetExitCodeProcess(process)==259:
            win32job.TerminateJobObject(job,1)
        if waiter is not None and not waiter.done():
            try:
                await asyncio.wait_for(asyncio.shield(waiter),2)
            except (TimeoutError,asyncio.CancelledError):
                pass
        for handle in (thread,process,job):
            handle.Close()
        Path(target).unlink(missing_ok=True)
        Path(str(target)+'.phase').unlink(missing_ok=True)
