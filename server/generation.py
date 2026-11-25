import asyncio
from contextlib import aclosing
import json
import logging
from .db import now,uid
from .errors import DomainError
from .context import build_context
from .model import stream_model,model_error
from .document_claims import claim_stream,persist_claim,claim_dtos,citation_dtos
from .retrieval import prepare_retrieval

ACTIVE=('queued','running')


class Generations:
    def __init__(self, db, cfg, auth, conversations, gates):
        self.db,self.cfg,self.auth,self.convs,self.gates=db,cfg,auth,conversations,gates
        self.tasks={}
        self.documents=None

    def owned(self, rid, owner):
        from .conversations import valid_uuid
        valid_uuid(rid)
        row=self.db.one('SELECT * FROM generation_requests WHERE id=? AND owner_id=?',(rid,owner))
        if not row:
            raise DomainError('NOT_FOUND',404,'请求不存在或不可访问')
        self.convs.owned(row['conversation_id'],owner)
        return row

    def dto(self, row):
        msg=self.db.one('SELECT content FROM messages WHERE id=? AND owner_id=?',(row['assistant_message_id'],row['owner_id']))
        keys=('id','conversation_id','user_message_id','assistant_message_id','status','mode','intent','last_seq','created_at','updated_at')
        return {k:row[k] for k in keys} | {'events_invalidated':bool(row['events_invalidated']),'content':msg['content'],
             'scope':json.loads(row['scope_json']),'scope_exclusions':json.loads(row['scope_exclusions_json']),
             'coverage':json.loads(row['coverage_json']) if row['coverage_json'] else None,'claims':claim_dtos(self.db,row['assistant_message_id']),'citations':citation_dtos(self.db,row['assistant_message_id']),'error':model_error(row['error_code']).dto() if row['error_code'] else None}

    def result(self, row):
        return dict(user_message_id=row['user_message_id'],assistant_message_id=row['assistant_message_id'],generation=self.dto(row),events_url='/api/v1/requests/'+row['id']+'/events')

    def create(self, request, actor, cid, question, retry=None):
        owner=actor['user_id'];conv=self.convs.owned(cid,owner)
        body=retry.model_dump() if retry else question.model_dump()
        with self.db.transaction():
            existing,key=self.convs.lookup_key(request,owner,body)
            if existing:
                return self.result(self.owned(existing['resource_id'],owner))
            if question.mode=='general' and question.file_refs:
                raise DomainError('VALIDATION_ERROR')
            if self.db.one("SELECT id FROM generation_requests WHERE conversation_id=? AND status IN ('queued','running')",(cid,)):
                raise DomainError('GENERATION_ACTIVE',409,'当前会话仍在生成，请先等待或中止')
            if self.db.one("SELECT count(*) AS n FROM generation_requests WHERE status IN ('queued','running')")['n']>=2:
                raise DomainError('GENERATION_CAPACITY',429,'生成容量已满，请稍后重试',True,{'retry_after_seconds':2})
            previous=None
            if retry:
                previous=self.owned(retry.failed_request_id,owner)
                if previous['conversation_id']!=cid or previous['status'] not in ('failed','cancelled','interrupted'):
                    raise DomainError('REQUEST_NOT_RETRYABLE',409,'只能重试失败、中止或中断的回答')
                if question.mode=='document':
                    original=json.loads(previous['scope_json']);available=[];unavailable=[]
                    for ref in original:
                        file=self.documents.owned(ref['file_id'],owner,True)
                        reason='deleted' if file['deleted_at'] else 'disabled' if not file['enabled'] else 'not_ready' if file['status']!='ready' else None
                        if reason:
                            unavailable.append({'file_id':file['id'],'reason':reason})
                        else:
                            available.append({'file_id':file['id'],'version':file['version']})
                    confirmed=retry.confirmed_file_refs
                    if confirmed:
                        if any(r['file_id'] not in {x['file_id'] for x in original} for r in confirmed):
                            raise DomainError('VALIDATION_ERROR')
                        question.file_refs=confirmed
                    elif unavailable:
                        raise DomainError('SCOPE_CHANGED',409,'资料范围已变更，请确认当前仍可用范围',details={'available_file_refs':available,'unavailable':unavailable})
                    else:
                        question.file_refs=available
            messages,history,budget,exclusions,dependencies=build_context(self.db,self.cfg,cid,owner,question.content,previous['user_message_id'] if previous else None,True)
            coverage=None;source_map={};scope=[]
            if question.mode=='document':
                messages,coverage,source_map,files,budget=prepare_retrieval(self.documents,self.cfg,owner,cid,question,messages)
                scope=[{'file_id':f['id'],'version':f['version']} for f in files]
                dependencies=sorted(set(dependencies)|{f['id'] for f in files})
            rid,aid,t=uid(),uid(),now()
            if previous:
                user_id=previous['user_message_id']
                version=self.db.one('SELECT MAX(answer_version) AS v FROM messages WHERE parent_user_message_id=?',(user_id,))['v']+1
            else:
                user_id=uid();version=1
                self.db.run('INSERT INTO messages VALUES(?,?,?,?,?,?,?,?,?,?,?)',(user_id,cid,owner,'user',question.content,'accepted',None,None,None,t,t))
            self.db.run('INSERT INTO messages VALUES(?,?,?,?,?,?,?,?,?,?,?)',(aid,cid,owner,'assistant','','queued',user_id,version,self.cfg.model_name,now(),now()))
            self.db.run('''INSERT INTO generation_requests(id,owner_id,conversation_id,user_message_id,assistant_message_id,status,mode,intent,model,
 history_ids_json,budget_json,snapshot_epoch,created_at,updated_at) VALUES(?,?,?,?,?,'queued',?,?,?,?,?,?,?,?)''',
                (rid,owner,cid,user_id,aid,question.mode,question.intent,self.cfg.model_name,json.dumps(history),json.dumps(budget),conv['access_epoch'],t,t))
            self.db.run('UPDATE generation_requests SET scope_json=?,scope_exclusions_json=?,coverage_json=?,source_token_map_json=?,dependency_file_ids_json=? WHERE id=?',
                (json.dumps(scope),json.dumps(exclusions),json.dumps(coverage),json.dumps(source_map,ensure_ascii=False),json.dumps(dependencies),rid))
            self.db.conn.executemany('INSERT INTO message_source_dependencies VALUES(?,?,?,?)',[(aid,fid,cid,owner) for fid in dependencies])
            self.db.event(rid,'meta',dict(request_id=rid,user_message_id=user_id,assistant_message_id=aid,answer_version=version,mode=question.mode,scope=scope,scope_exclusions=exclusions,coverage=coverage,budget=budget,retrieval_no_match=question.mode=='document' and not source_map))
            self.convs.save_key(key,'generation',rid)
            if conv['title_source']=='default':
                self.db.run("UPDATE conversations SET title=?,title_source='auto',revision=revision+1 WHERE id=? AND owner_id=?",(question.content[:24],cid,owner))
            self.db.run('UPDATE conversations SET updated_at=? WHERE id=?',(now(),cid))
        task=asyncio.create_task(self.run(rid,actor,messages))
        self.tasks[rid]=task
        def finished(task):
            self.tasks.pop(rid,None)
            if task.cancelled():
                self.finish(rid,'cancelled')
        task.add_done_callback(finished)
        return self.result(self.owned(rid,owner))

    def finish(self, rid, status, error=None):
        row=self.db.one('SELECT * FROM generation_requests WHERE id=?',(rid,))
        if not row or row['status'] not in ACTIVE:
            return
        with self.db.transaction():
            self.db.run('UPDATE generation_requests SET status=?,error_code=?,cancel_requested=0,updated_at=? WHERE id=?',(status,error.code if error else None,now(),rid))
            self.db.run('UPDATE messages SET status=?,updated_at=? WHERE id=?',(status,now(),row['assistant_message_id']))
            if error:
                self.db.event(rid,'error',error.dto()|{'status':status})
            elif status=='cancelled':
                self.db.event(rid,'cancelled',{'status':status,'reason':'user' if self.dependencies_valid(row) else 'source_removed'})
            else:
                claims=claim_dtos(self.db,row['assistant_message_id'])
                self.db.event(rid,'done',{'status':'completed','finish_reason':'stop','coverage':json.loads(row['coverage_json']) if row['coverage_json'] else None,'no_answer':bool(claims) and all(c['kind']=='no_answer' for c in claims)})

    def dependencies_valid(self,row):
        for fid in json.loads(row['dependency_file_ids_json']):
            file=self.db.one('SELECT f.deleted_at,cf.enabled FROM files f JOIN conversation_files cf ON cf.file_id=f.id WHERE f.id=? AND f.owner_id=? AND f.conversation_id=?',(fid,row['owner_id'],row['conversation_id']))
            if not file or file['deleted_at'] or not file['enabled']:
                return False
        return True

    async def output(self,row,messages):
        if row['mode']=='general':
            async with aclosing(stream_model(self.cfg,messages)) as upstream:
                async for text in upstream:
                    yield text
        elif not json.loads(row['source_token_map_json']):
            yield ({'kind':'no_answer','text':'资料中未找到能够回答该问题的依据。'},[])
            coverage=json.loads(row['coverage_json'])
            for file in coverage['files']:
                for warning in file['warnings']:
                    if warning['code']=='PARTIAL_TEXT':
                        pages='、'.join(str(p) for p in warning['pages'])
                        yield ({'kind':'no_answer','text':'解析范围限制：第'+pages+'页无可提取文字，无法依据这些页面作答。'},[])
        else:
            async with aclosing(stream_model(self.cfg,messages)) as upstream:
                async with aclosing(claim_stream(upstream,json.loads(row['source_token_map_json']))) as claims:
                    async for item in claims:
                        yield item

    async def run(self, rid, actor, messages):
        owner=actor['user_id']
        try:
            async with self.gates[owner].work():
                if not self.auth.valid(actor):
                    self.finish(rid,'interrupted',model_error('PROCESS_INTERRUPTED'));return
                with self.db.transaction():
                    self.db.run("UPDATE generation_requests SET status='running' WHERE id=?",(rid,))
                    self.db.run("UPDATE messages SET status='running' WHERE id=(SELECT assistant_message_id FROM generation_requests WHERE id=?)",(rid,))
                    self.db.event(rid,'status',{'status':'running'})
            initial=self.owned(rid,owner)
            async with aclosing(self.output(initial,messages)) as output:
                async for text in output:
                    async with self.gates[owner].work():
                        row=self.owned(rid,owner)
                        if not self.auth.valid(actor) or row['cancel_requested'] or not self.dependencies_valid(row):
                            raise asyncio.CancelledError()
                        with self.db.transaction():
                            if row['mode']=='document':
                                persist_claim(self.db,row,*text)
                            else:
                                self.db.run('UPDATE messages SET content=content||?,updated_at=? WHERE id=?',(text,now(),row['assistant_message_id']))
                                self.db.event(rid,'delta',{'append':text,'claim_index':None,'claim':None})
            async with self.gates[owner].work():
                if not self.auth.valid(actor):
                    self.finish(rid,'interrupted',model_error('PROCESS_INTERRUPTED'))
                else:
                    self.finish(rid,'completed')
        except asyncio.CancelledError:
            async with self.gates[owner].work():
                self.finish(rid,'cancelled')
        except DomainError as e:
            async with self.gates[owner].work():
                self.finish(rid,'failed',e)
        except Exception:
            async with self.gates[owner].work():
                self.finish(rid,'failed',model_error('MODEL_UNAVAILABLE'))

    def cancel(self,rid,owner):
        row=self.owned(rid,owner)
        if row['status'] in ACTIVE:
            self.db.run('UPDATE generation_requests SET cancel_requested=1 WHERE id=?',(rid,))
            if rid in self.tasks:
                self.tasks[rid].cancel()
        current=self.owned(rid,owner)
        return dict(id=rid,status=current['status'],cancel_requested=bool(current['cancel_requested']))

    def recover(self):
        for row in self.db.all("SELECT id FROM generation_requests WHERE status IN ('queued','running')"):
            self.finish(row['id'],'interrupted',model_error('PROCESS_INTERRUPTED'))

    async def close(self):
        tasks=list(self.tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            _,pending=await asyncio.wait(tasks,timeout=2)
            for rid,task in list(self.tasks.items()):
                if task in pending:
                    task.cancel()
                    self.finish(rid,'interrupted',model_error('PROCESS_INTERRUPTED'))
                    logging.getLogger('zhixu.generation').warning('GENERATION_SHUTDOWN_TIMEOUT request_id=%s',rid)
