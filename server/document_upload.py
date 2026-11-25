import asyncio
import errno
import hashlib
import json
import os
import re
import time
import unicodedata
from urllib.parse import unquote
from pathlib import Path
from .db import uid,now
from .conversations import valid_uuid
from .document_errors import doc_error
from .errors import DomainError

MIMES={'pdf':{'application/pdf'},'docx':{'application/vnd.openxmlformats-officedocument.wordprocessingml.document'},'txt':{'text/plain'},'md':{'text/plain','text/markdown'}}


def upload_headers(request):
    encoded=request.headers.get('x-file-name','')
    try:
        if len(encoded.encode('ascii'))>4096 or re.search(r'%(?![0-9a-fA-F]{2})',encoded):
            raise ValueError()
        name=unquote(encoded,encoding='utf-8',errors='strict')
        if not 1<=len(name)<=180 or any(unicodedata.category(c).startswith('C') or c in '/\\' for c in name):
            raise ValueError()
        raw=request.headers.get('x-file-size','')
        if not re.fullmatch(r'[0-9]{1,10}',raw):
            raise ValueError()
        size=int(raw)
    except (ValueError,UnicodeError):
        raise doc_error('INVALID_UPLOAD_HEADERS')
    if size==0:
        raise doc_error('EMPTY_FILE')
    if size>20000000:
        raise doc_error('FILE_TOO_LARGE',{'limit':20000000,'actual':size,'unit':'bytes'})
    kind=Path(name).suffix.lower().lstrip('.')
    if kind not in MIMES:
        raise doc_error('UNSUPPORTED_FORMAT')
    mime=request.headers.get('content-type','').split(';')[0].strip().lower()
    if mime not in MIMES[kind]|{'','application/octet-stream'}:
        raise doc_error('MIME_MISMATCH')
    return name,size,kind,mime or 'application/octet-stream'


async def upload(documents,request,actor,cid):
    db=documents.db;owner=actor['user_id'];documents.convs.owned(cid,owner)
    name,size,kind,mime=upload_headers(request);key=valid_uuid(request.headers.get('idempotency-key'))
    token=uid();temporary='staging/'+token;path=documents.path(temporary);target=None;committed=False
    documents.clean_reservations()
    with db.transaction():
        if db.one('SELECT count(*) AS n FROM upload_reservations')['n']>=2:
            raise doc_error('UPLOAD_CAPACITY',{'retry_after_seconds':2})
        if db.one('SELECT id FROM upload_reservations WHERE owner_id=? AND conversation_id=? AND idempotency_key=?',(owner,cid,key)):
            raise DomainError('IDEMPOTENCY_IN_PROGRESS',409,'同请求仍在上传，请稍后重试',True,{'retry_after_seconds':1})
        db.run('INSERT INTO upload_reservations VALUES(?,?,?,?,?,?,?,?,?)',(token,owner,cid,key,size,0,temporary,time.time()+120,now()))
    try:
        received=0;hashed=hashlib.sha256();signature=bytearray()
        async with asyncio.timeout(120):
            with path.open('xb') as stream:
                async for part in request.stream():
                    received+=len(part)
                    if received>20000000 or received>size:
                        raise doc_error('UPLOAD_SIZE_MISMATCH')
                    if len(signature)<1024:
                        signature.extend(part[:1024-len(signature)])
                    stream.write(part);hashed.update(part)
                    db.run('UPDATE upload_reservations SET received_size=? WHERE id=?',(received,token))
        if received!=size:
            raise doc_error('UPLOAD_SIZE_MISMATCH')
        if kind=='pdf' and not signature.startswith(b'%PDF-') or kind=='docx' and not signature.startswith(b'PK\x03\x04'):
            raise doc_error('MIME_MISMATCH')
        async with documents.gates[owner].work():
            if not documents.auth.valid(actor):
                raise DomainError('AUTH_REQUIRED',401,'上传期间登录已失效')
            documents.convs.owned(cid,owner)
            with db.transaction():
                existing,fields=documents.convs.lookup_key(request,owner,dict(sha256=hashed.hexdigest(),name=name,mime=mime,size=size))
                if existing:
                    row=documents.owned(existing['resource_id'],owner,True)
                    if row['deleted_at']:
                        raise DomainError('RESOURCE_DELETED',410,'原请求的资料已删除')
                    return {'file':documents.dto(row),'job':documents.job(row['id']),'deduplicated':True}
                duplicate=db.one('SELECT id FROM files WHERE owner_id=? AND conversation_id=? AND sha256=? AND deleted_at IS NULL',(owner,cid,hashed.hexdigest()))
                if duplicate:
                    documents.convs.save_key(fields,'file',duplicate['id'])
                    return {'file':documents.dto(documents.owned(duplicate['id'],owner)),'job':documents.job(duplicate['id']),'deduplicated':True}
                quota=db.one('SELECT count(*) AS n,COALESCE(sum(size),0) AS bytes FROM files WHERE conversation_id=? AND owner_id=? AND deleted_at IS NULL',(cid,owner))
                if quota['n']>=5:
                    raise doc_error('FILE_COUNT_LIMIT',{'limit':5,'actual':quota['n']+1,'unit':'count'})
                if quota['bytes']+size>50000000:
                    raise doc_error('CONVERSATION_BYTES_LIMIT',{'limit':50000000,'actual':quota['bytes']+size,'unit':'bytes'})
                fid=uid();object_key='objects/'+fid;target=documents.path(object_key);os.replace(path,target)
                db.run('''INSERT INTO files(id,owner_id,conversation_id,sha256,name,mime,size,version,parser_revision,status,object_key,created_at)
 VALUES(?,?,?,?,?,?,?,1,'pending','security_check',?,?)''',(fid,owner,cid,hashed.hexdigest(),name,mime,size,object_key,now()))
                db.run('INSERT INTO conversation_files VALUES(?,?,?,1,1,1,?)',(cid,fid,owner,now()))
                job_id=documents.new_job(fid,owner)
                documents.convs.save_key(fields,'file',fid)
            committed=True;documents.schedule(job_id)
            return {'file':documents.dto(documents.owned(fid,owner)),'job':documents.job(fid),'deduplicated':False}
    except TimeoutError:
        raise doc_error('UPLOAD_TIMEOUT')
    except OSError as error:
        raise doc_error('STORAGE_FULL' if error.errno==errno.ENOSPC else 'STORAGE_ERROR')
    finally:
        db.run('DELETE FROM upload_reservations WHERE id=?',(token,))
        path.unlink(missing_ok=True)
        if target is not None and not committed:
            target.unlink(missing_ok=True)
