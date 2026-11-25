import asyncio
import hmac
import json
import time
from collections import deque
from .auth import digest,password_hash
from .db import uid,now
from .document_errors import doc_error
from .document_purge import purge_conversation
from .errors import DomainError
from .idempotency import retire_resource,forget_owner


class Deletion:
    def __init__(self,documents,generations):
        self.docs,self.gens=documents,generations;self.db=documents.db;self.tasks={};self.receipt_attempts=deque()

    def rate_receipt(self):
        at=time.monotonic()
        while self.receipt_attempts and self.receipt_attempts[0]<=at-60:
            self.receipt_attempts.popleft()
        if len(self.receipt_attempts)>=60:
            raise DomainError('RATE_LIMITED',429,'删除状态查询过于频繁，请一分钟后重试',True,{'retry_after_seconds':60})
        self.receipt_attempts.append(at)

    @staticmethod
    def dto(row):
        return {k:row[k] for k in ('id','status','created_at','updated_at')}|{'error_code':row['last_error_code']}

    def existing(self,owner,kind,resource):
        return self.db.one('SELECT * FROM deletion_jobs WHERE owner_ref_hash=? AND resource_type=? AND resource_id=?',(digest(owner),kind,resource))

    def job(self,owner,kind,resource,keys,job_id=None,receipt=None):
        job_id=job_id or uid();t=now()
        self.db.run('INSERT INTO deletion_jobs VALUES(?,?,?,?,?,?,?,?,?,0,NULL,?,?)',(job_id,owner,digest(owner),kind,resource,digest(receipt) if receipt else None,time.time()+604800,json.dumps(keys),'pending',t,t))
        return job_id

    def object_keys(self,owner,cid=None):
        filter='owner_id=?'+(' AND conversation_id=?' if cid else '')
        args=(owner,cid) if cid else (owner,)
        keys=[r['object_key'] for r in self.db.all('SELECT object_key FROM files WHERE '+filter+' AND object_key IS NOT NULL',args)]
        keys += [r['temp_key'] for r in self.db.all('SELECT temp_key FROM upload_reservations WHERE '+filter,args)]
        for row in self.db.all('SELECT id FROM parse_jobs WHERE owner_id=? AND file_id IN (SELECT id FROM files WHERE '+filter+')',(owner,*args)):
            keys += ['parsed/'+row['id']+'.json','parsed/'+row['id']+'.json.phase']
        return keys

    def signal(self,owner,cid=None,fid=None):
        params=[owner];sql="SELECT * FROM generation_requests WHERE owner_id=?"
        if cid:
            sql+=' AND conversation_id=?';params.append(cid)
        for row in self.db.all(sql,params):
            if fid and fid not in json.loads(row['dependency_file_ids_json']):
                continue
            self.db.run("UPDATE generation_requests SET events_invalidated=1,source_token_map_json='{}',cancel_requested=CASE WHEN status IN ('queued','running') THEN 1 ELSE 0 END WHERE id=?",(row['id'],))
            self.db.run('DELETE FROM generation_events WHERE request_id=?',(row['id'],))
            if row['id'] in self.gens.tasks:
                self.gens.tasks[row['id']].cancel()
        rows=self.db.all('SELECT id,file_id FROM parse_jobs WHERE owner_id=? AND status IN (\'queued\',\'running\')',(owner,))
        for row in rows:
            file=self.db.one('SELECT conversation_id FROM files WHERE id=?',(row['file_id'],))
            if (fid and row['file_id']!=fid) or (cid and file['conversation_id']!=cid):
                continue
            self.db.run('UPDATE parse_jobs SET cancel_requested=1 WHERE id=?',(row['id'],))
            if row['id'] in self.docs.tasks:
                self.docs.tasks[row['id']].cancel()

    async def enable(self,actor,cid,fid,data):
        owner=actor['user_id'];self.docs.owned(fid,owner)
        def action():
            if not self.docs.auth.valid(actor):
                raise DomainError('AUTH_REQUIRED',401)
            file=self.docs.owned(fid,owner)
            if file['conversation_id']!=cid:
                raise DomainError('NOT_FOUND',404)
            if bool(file['enabled'])==data.enabled:
                return self.docs.dto(file)
            if file['enabled_revision']!=data.revision:
                raise DomainError('REVISION_CONFLICT',409,'资料状态已更新，请重新读取',details={'current':self.docs.dto(file)})
            with self.db.transaction():
                self.db.run('UPDATE conversation_files SET enabled=?,revision=revision+1 WHERE file_id=?',(int(data.enabled),fid))
                self.db.run('UPDATE conversations SET access_epoch=access_epoch+1 WHERE id=?',(cid,))
                if not data.enabled:
                    self.signal(owner,cid,fid)
            return self.docs.dto(self.docs.owned(fid,owner))
        return await asyncio.shield(self.docs.gates[owner].revoke(action))

    async def delete_file(self,actor,fid):
        owner=actor['user_id'];old=self.existing(owner,'file',fid)
        if old:
            return self.dto(old)
        self.docs.owned(fid,owner)
        def action():
            if not self.docs.auth.valid(actor):
                raise DomainError('AUTH_REQUIRED',401)
            old=self.existing(owner,'file',fid)
            if old:
                return self.dto(old)
            file=self.docs.owned(fid,owner)
            keys=[file['object_key']] if file['object_key'] else []
            for job in self.db.all('SELECT id FROM parse_jobs WHERE file_id=?',(fid,)):
                keys+=['parsed/'+job['id']+'.json','parsed/'+job['id']+'.json.phase']
            with self.db.transaction():
                job_id=self.job(owner,'file',fid,keys)
                retire_resource(self.db,owner,fid)
                self.db.run("UPDATE files SET deleted_at=?,status='deleted',name=NULL,mime=NULL,sha256=NULL WHERE id=?",(now(),fid))
                self.db.run('UPDATE conversation_files SET enabled=0,revision=revision+1 WHERE file_id=?',(fid,))
                self.db.run('UPDATE conversations SET access_epoch=access_epoch+1 WHERE id=?',(file['conversation_id'],))
                self.signal(owner,file['conversation_id'],fid)
                self.db.run("UPDATE citations SET source_status='deleted',quote='',chunk_id=NULL WHERE file_id=?",(fid,))
                self.db.run('DELETE FROM chunk_terms WHERE chunk_id IN (SELECT id FROM chunks WHERE file_id=?)',(fid,))
                self.db.run('DELETE FROM chunks WHERE file_id=?',(fid,));self.db.run('DELETE FROM paragraphs WHERE file_id=?',(fid,))
            self.schedule(job_id)
            return self.dto(self.db.one('SELECT * FROM deletion_jobs WHERE id=?',(job_id,)))
        return await asyncio.shield(self.docs.gates[owner].revoke(action))

    async def delete_conversation(self,actor,cid):
        owner=actor['user_id'];old=self.existing(owner,'conversation',cid)
        if old:
            return self.dto(old)
        self.docs.convs.owned(cid,owner)
        def action():
            if not self.docs.auth.valid(actor):
                raise DomainError('AUTH_REQUIRED',401)
            old=self.existing(owner,'conversation',cid)
            if old:
                return self.dto(old)
            self.docs.convs.owned(cid,owner)
            with self.db.transaction():
                job_id=self.job(owner,'conversation',cid,self.object_keys(owner,cid))
                self.signal(owner,cid)
                self.db.run('UPDATE conversations SET deleted_at=?,access_epoch=access_epoch+1 WHERE id=?',(now(),cid))
                purge_conversation(self.db,cid,owner)
            self.schedule(job_id)
            return self.dto(self.db.one('SELECT * FROM deletion_jobs WHERE id=?',(job_id,)))
        return await asyncio.shield(self.docs.gates[owner].revoke(action))

    def receipt(self,job_id,token,owner=None):
        row=self.db.one('SELECT * FROM deletion_jobs WHERE id=?',(job_id,))
        if not row or row['expires_at']<time.time():
            raise DomainError('NOT_FOUND',404)
        authorized=owner is not None and row['owner_id']==owner
        if token and row['receipt_hash']:
            authorized=authorized or hmac.compare_digest(row['receipt_hash'],digest(token))
        if not authorized:
            raise DomainError('NOT_FOUND',404)
        return self.dto(row)

    async def delete_account(self,request,data):
        try:
            actor=self.docs.auth.actor(request,True)
        except DomainError as error:
            if error.code not in ('AUTH_REQUIRED','SESSION_EXPIRED'):
                raise
            return self.receipt(data.deletion_key,data.receipt_token)
        owner=actor['user_id']
        existing=self.db.one('SELECT * FROM deletion_jobs WHERE id=?',(data.deletion_key,))
        if existing:
            return self.receipt(data.deletion_key,data.receipt_token)
        user=self.db.one('SELECT * FROM users WHERE id=? AND deleted_at IS NULL',(owner,))
        async with self.docs.auth.hash_slots:
            value=await asyncio.to_thread(password_hash,data.current_password,user['salt'])
        if not hmac.compare_digest(value,user['password_hash']):
            raise DomainError('AUTH_INVALID',401,'当前密码不正确')
        def action():
            if not self.docs.auth.valid(actor):
                raise DomainError('AUTH_REQUIRED',401)
            with self.db.transaction():
                job_id=self.job(owner,'account',owner,self.object_keys(owner),data.deletion_key,data.receipt_token)
                self.db.run('UPDATE users SET deleted_at=? WHERE id=?',(now(),owner))
                self.signal(owner)
                for conv in self.db.all('SELECT id FROM conversations WHERE owner_id=?',(owner,)):
                    purge_conversation(self.db,conv['id'],owner)
                self.db.run('DELETE FROM idempotency_keys WHERE owner_id=?',(owner,))
                forget_owner(self.db,owner)
                self.db.run('DELETE FROM auth_sessions WHERE user_id=?',(owner,))
                self.db.run('DELETE FROM users WHERE id=?',(owner,))
            self.schedule(job_id)
            return self.dto(self.db.one('SELECT * FROM deletion_jobs WHERE id=?',(job_id,)))
        return await asyncio.shield(self.docs.gates[owner].revoke(action))

    def schedule(self,job_id):
        task=asyncio.create_task(self.gc(job_id));self.tasks[job_id]=task
        task.add_done_callback(lambda _:self.tasks.pop(job_id,None))

    async def wait_for_job(self,job_id):
        if job_id in self.tasks:
            await asyncio.shield(self.tasks[job_id])

    async def gc(self,job_id):
        row=self.db.one('SELECT * FROM deletion_jobs WHERE id=?',(job_id,))
        try:
            for key in json.loads(row['object_keys_json']):
                await asyncio.to_thread(self.docs.path(key).unlink,missing_ok=True)
            with self.db.transaction():
                if row['resource_type']=='file':
                    self.db.run('UPDATE files SET object_key=NULL,size=0 WHERE id=? AND deleted_at IS NOT NULL',(row['resource_id'],))
                self.db.run("UPDATE deletion_jobs SET status='completed',object_keys_json='[]',last_error_code=NULL,expires_at=min(expires_at,?),updated_at=? WHERE id=?",(time.time()+86400,now(),job_id))
        except OSError:
            self.db.run("UPDATE deletion_jobs SET status='failed',attempt=attempt+1,last_error_code='STORAGE_ERROR',updated_at=? WHERE id=?",(now(),job_id))

    def recover(self):
        for row in self.db.all("SELECT id FROM deletion_jobs WHERE status IN ('pending','failed')"):
            self.schedule(row['id'])

    async def close(self):
        if self.tasks:
            tasks=list(self.tasks.values())
            _,pending=await asyncio.wait(tasks,timeout=2)
            for task in pending:
                task.cancel()
            await asyncio.gather(*tasks,return_exceptions=True)
