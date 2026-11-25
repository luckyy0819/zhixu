import asyncio
import sqlite3
from contextlib import asynccontextmanager
from fastapi import FastAPI,Request,Query
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse
from starlette.exceptions import HTTPException
from .config import Settings,ROOT
from .db import Database
from .auth import Auth,cookie,digest
from .gates import Gates
from .conversations import Conversations,valid_uuid
from .generation import Generations
from .schemas import Register,Login,Empty,NewConversation,Rename,Question,Retry,EnableFile,DeleteFile,DeleteConversation,DeleteAccount
from .security import Security
from .errors import DomainError,response,error_response
from .events import events
from .documents import Documents
from .document_upload import upload
from .deletion import Deletion


def create_app(cfg=None):
    cfg=cfg or Settings.load()
    db=Database(cfg.data_dir/'zhixu.sqlite3');gates=Gates()
    auth=Auth(db,gates);convs=Conversations(db);gens=Generations(db,cfg,auth,convs,gates)
    documents=Documents(db,cfg,convs,auth,gates)
    gens.documents=documents
    deletion=Deletion(documents,gens)
    @asynccontextmanager
    async def lifespan(app):
        gens.recover()
        documents.recover()
        deletion.recover()
        yield
        await gens.close()
        await documents.close()
        await deletion.close()
        db.conn.close()
    app=FastAPI(lifespan=lifespan,docs_url=None,redoc_url=None,openapi_url=None)
    app.state.services=(db,auth,convs,gens,gates)
    app.state.documents=documents
    app.state.deletion=deletion
    app.add_middleware(Security,cfg=cfg,auth=auth,convs=convs,gates=gates)

    @app.exception_handler(DomainError)
    async def domain_error(request,error):
        return error_response(error)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request,error):
        return error_response(DomainError('VALIDATION_ERROR',422,'输入格式或字段无效，请检查后重试'))

    @app.exception_handler(HTTPException)
    async def http_error(request,error):
        return error_response(DomainError('NOT_FOUND',404,'接口不存在或尚未实现'))

    @app.exception_handler(sqlite3.Error)
    async def storage_error(request,error):
        return error_response(DomainError('STORAGE_ERROR',503,'存储操作失败，请恢复环境后重试',True))

    def bind(request,cid,owner):
        row=convs.owned(cid,owner)
        request.state.cid=cid;request.state.epoch=row['access_epoch']
        return row

    async def write_actor(request):
        actor=auth.actor(request,True)
        return actor

    @app.get('/api/v1/health')
    async def health():
        return response({'status':'ok'})

    @app.get('/api/v1/auth/csrf')
    async def csrf():
        r=response(None);data=auth.preauth(r)
        import json
        r.body=json.dumps({'request_id':__import__('uuid').uuid4().hex,'data':data}).encode()
        r.headers['content-length']=str(len(r.body))
        return r

    @app.post('/api/v1/auth/register')
    async def register(request:Request,data:Register):
        info,token=await auth.credentials(request,data,True)
        r=response(info,201);cookie(r,'zhixu_session',token,86400);r.delete_cookie('zhixu_preauth')
        return r

    @app.post('/api/v1/auth/login')
    async def login(request:Request,data:Login):
        info,token=await auth.credentials(request,data)
        r=response(info);cookie(r,'zhixu_session',token,86400);r.delete_cookie('zhixu_preauth')
        return r

    @app.get('/api/v1/auth/me')
    async def me(request:Request):
        return response(auth.me(request))

    @app.post('/api/v1/auth/logout')
    async def logout(request:Request,data:Empty):
        try:
            actor=auth.actor(request,True)
        except DomainError as e:
            if e.code not in ('AUTH_REQUIRED','SESSION_EXPIRED'):
                raise
            r=response({'logged_out':True});r.delete_cookie('zhixu_session');return r
        def revoke():
            db.run('DELETE FROM auth_sessions WHERE id=?',(actor['id'],))
            for row in db.all("SELECT id FROM generation_requests WHERE owner_id=? AND status IN ('queued','running')",(actor['user_id'],)):
                gens.cancel(row['id'],actor['user_id'])
        # shield the independently supervised revocation from HTTP disconnects.
        await asyncio.shield(gates[actor['user_id']].revoke(revoke))
        request.state.public_response=True
        r=response({'logged_out':True});r.delete_cookie('zhixu_session');return r

    @app.get('/api/v1/capabilities')
    async def capabilities(request:Request):
        auth.actor(request)
        return response(dict(file_types=['pdf','docx','txt','md'],max_file_bytes=20000000,max_conversation_files=5,max_conversation_bytes=50000000,
            max_pdf_pages=200,max_question_chars=6000,context_budget_tokens=cfg.context_tokens,max_output_tokens=cfg.output_tokens,
            features={'ocr':False,'web':False,'documents':True},runtime_mode='local'))

    @app.post('/api/v1/conversations')
    async def create_conversation(request:Request,data:NewConversation):
        actor=await write_actor(request)
        async with gates[actor['user_id']].work():
            auth.actor(request,True)
            return response(convs.create(request,actor['user_id'],data),201)

    @app.get('/api/v1/conversations')
    async def list_conversations(request:Request,q:str='',cursor:str|None=None,limit:int=20):
        actor=auth.actor(request)
        return response(convs.listing(actor['user_id'],q,cursor,limit))

    @app.get('/api/v1/conversations/{cid}')
    async def conversation(request:Request,cid:str):
        actor=auth.actor(request);row=bind(request,cid,actor['user_id'])
        return response({'conversation':convs.dto(row),'files':documents.listing(cid,actor['user_id'])})

    @app.patch('/api/v1/conversations/{cid}')
    async def rename(request:Request,cid:str,data:Rename):
        actor=await write_actor(request)
        async with gates[actor['user_id']].work():
            auth.actor(request,True);bind(request,cid,actor['user_id'])
            return response(convs.rename(cid,actor['user_id'],data))

    @app.get('/api/v1/conversations/{cid}/messages')
    async def messages(request:Request,cid:str,cursor:str|None=None,limit:int=20):
        actor=auth.actor(request);bind(request,cid,actor['user_id'])
        return response(convs.messages(cid,actor['user_id'],cursor,limit))

    @app.get('/api/v1/conversations/{cid}/files')
    async def files(request:Request,cid:str):
        actor=auth.actor(request);bind(request,cid,actor['user_id'])
        return response({'items':documents.listing(cid,actor['user_id'])})

    @app.post('/api/v1/conversations/{cid}/messages')
    async def question(request:Request,cid:str,data:Question):
        actor=await write_actor(request)
        async with gates[actor['user_id']].work():
            auth.actor(request,True);bind(request,cid,actor['user_id'])
            return response(gens.create(request,actor,cid,data),202)

    @app.get('/api/v1/requests/{rid}')
    async def generation(request:Request,rid:str):
        actor=auth.actor(request);row=gens.owned(rid,actor['user_id']);bind(request,row['conversation_id'],actor['user_id'])
        return response(gens.dto(row))

    @app.get('/api/v1/requests/{rid}/events')
    async def stream(request:Request,rid:str):
        actor=auth.actor(request);row=gens.owned(rid,actor['user_id']);bind(request,row['conversation_id'],actor['user_id'])
        return events(request,gens,rid,actor['user_id'])

    @app.post('/api/v1/requests/{rid}/cancel')
    async def cancel(request:Request,rid:str,data:Empty):
        actor=await write_actor(request)
        async with gates[actor['user_id']].work():
            auth.actor(request,True);row=gens.owned(rid,actor['user_id']);bind(request,row['conversation_id'],actor['user_id'])
            return response(gens.cancel(rid,actor['user_id']))

    @app.post('/api/v1/messages/{mid}/retry')
    async def retry(request:Request,mid:str,data:Retry):
        actor=await write_actor(request)
        async with gates[actor['user_id']].work():
            auth.actor(request,True);valid_uuid(mid)
            row=db.one("SELECT * FROM messages WHERE id=? AND owner_id=? AND role='user'",(mid,actor['user_id']))
            if not row:
                raise DomainError('NOT_FOUND',404,'问题不存在或不可访问')
            previous=gens.owned(data.failed_request_id,actor['user_id'])
            if previous['user_message_id']!=mid or previous['mode']=='general' and data.confirmed_file_refs:
                raise DomainError('REQUEST_NOT_RETRYABLE',409,'重试范围与原问题不一致')
            bind(request,row['conversation_id'],actor['user_id'])
            question=Question(content=row['content'],mode=previous['mode'],file_refs=[],intent=previous['intent'])
            return response(gens.create(request,actor,row['conversation_id'],question,data),202)

    @app.post('/api/v1/conversations/{cid}/files')
    async def upload_file(request:Request,cid:str):
        actor=auth.actor(request,True);bind(request,cid,actor['user_id'])
        data=await upload(documents,request,actor,cid)
        return response(data,200 if data['deduplicated'] else 202)

    @app.get('/api/v1/files/{fid}/status')
    async def file_status(request:Request,fid:str):
        actor=auth.actor(request);row=documents.owned(fid,actor['user_id']);bind(request,row['conversation_id'],actor['user_id'])
        return response(documents.dto(row))

    @app.get('/api/v1/files/{fid}/preview')
    async def file_preview(request:Request,fid:str,version:int,paragraph:int,start:int|None=None,end:int|None=None,window_start:int|None=None,window_end:int|None=None):
        actor=auth.actor(request);row=documents.owned(fid,actor['user_id'],True);bind(request,row['conversation_id'],actor['user_id'])
        return response(documents.preview(fid,actor['user_id'],version,paragraph,start,end,window_start,window_end))

    @app.post('/api/v1/files/{fid}/cancel')
    async def file_cancel(request:Request,fid:str,data:Empty):
        actor=auth.actor(request,True)
        async with gates[actor['user_id']].work():
            actor=auth.actor(request,True)
            row=documents.owned(fid,actor['user_id']);bind(request,row['conversation_id'],actor['user_id'])
            return response(documents.cancel(fid,actor))

    @app.post('/api/v1/files/{fid}/retry')
    async def file_retry(request:Request,fid:str,data:Empty):
        actor=auth.actor(request,True)
        async with gates[actor['user_id']].work():
            actor=auth.actor(request,True)
            row=documents.owned(fid,actor['user_id']);bind(request,row['conversation_id'],actor['user_id'])
            return response(documents.retry(request,fid,actor),202)

    @app.patch('/api/v1/conversations/{cid}/files/{fid}')
    async def enable_file(request:Request,cid:str,fid:str,data:EnableFile):
        actor=auth.actor(request,True)
        result=await deletion.enable(actor,cid,fid,data)
        bind(request,cid,actor['user_id'])
        return response(result)

    @app.delete('/api/v1/files/{fid}')
    async def delete_file(request:Request,fid:str,data:DeleteFile):
        actor=auth.actor(request,True)
        result=await deletion.delete_file(actor,fid)
        request.state.public_response=True
        return response(result,202)

    @app.delete('/api/v1/conversations/{cid}')
    async def delete_conversation(request:Request,cid:str,data:DeleteConversation):
        actor=auth.actor(request,True)
        result=await deletion.delete_conversation(actor,cid)
        request.state.public_response=True
        return response(result,202)

    @app.delete('/api/v1/account/data')
    async def delete_account(request:Request,data:DeleteAccount):
        result=await deletion.delete_account(request,data)
        request.state.public_response=True
        r=response(result,202);r.delete_cookie('zhixu_session');return r

    @app.get('/api/v1/deletions/{job_id}')
    async def deletion_status(request:Request,job_id:str):
        deletion.rate_receipt()
        valid_uuid(job_id)
        try:
            actor=auth.actor(request)
            owner=actor['user_id']
        except DomainError:
            owner=None
        return response(deletion.receipt(job_id,request.headers.get('x-deletion-receipt',''),owner))

    @app.get('/api/v1/citations/{citation_id}/preview')
    async def citation_preview(request:Request,citation_id:str):
        actor=auth.actor(request);valid_uuid(citation_id)
        row=db.one('SELECT * FROM citations WHERE id=? AND owner_id=?',(citation_id,actor['user_id']))
        if not row:
            raise DomainError('NOT_FOUND',404)
        bind(request,row['conversation_id'],actor['user_id'])
        return response(documents.preview(row['file_id'],actor['user_id'],row['version'],row['paragraph_no'],row['start'],row['end'])|{'citation_id':citation_id})

    @app.get('/{path:path}')
    async def static(path:str):
        path=path or 'index.html'
        if path not in ('index.html','app.js','document-ui.js','styles.css','client-core.js','vendor/marked.js','vendor/purify.js') or not (ROOT/path).is_file():
            raise DomainError('NOT_FOUND',404,'页面不存在')
        return FileResponse(ROOT/path)
    return app
