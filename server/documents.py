import asyncio
import json
import time
from pathlib import Path
from .db import uid,now
from .document_store import DocumentStore
from .document_errors import doc_error,RETRY
from .errors import DomainError
from .process_limits import parse_in_job
from .document_index import publish_index


class Documents(DocumentStore):
    def __init__(self,db,cfg,convs,auth,gates):
        super().__init__(db,cfg,convs)
        self.auth,self.gates=auth,gates
        self.tasks={};self.slot=asyncio.Semaphore(1);self.stopping=False

    def new_job(self,fid,owner,attempt=1):
        job_id=uid();t=now()
        self.db.run('''INSERT INTO parse_jobs(id,file_id,owner_id,attempt,status,phase,progress_completed,created_at,updated_at)
 VALUES(?,?,?,?,'queued','security_check',0,?,?)''',(job_id,fid,owner,attempt,t,t))
        return job_id

    def schedule(self,job_id):
        task=asyncio.create_task(self.run(job_id));self.tasks[job_id]=task
        def done(task):
            self.tasks.pop(job_id,None)
            if task.cancelled() and not self.stopping:
                self.failed(job_id,'cancelled',None)
        task.add_done_callback(done)

    async def wait_for_file(self,fid):
        job=self.db.one("SELECT id FROM parse_jobs WHERE file_id=? AND status IN ('queued','running')",(fid,))
        if job and job['id'] in self.tasks:
            await asyncio.shield(self.tasks[job['id']])

    def failed(self,job_id,status,code):
        row=self.db.one('SELECT * FROM parse_jobs WHERE id=?',(job_id,))
        if not row or row['status'] not in ('queued','running'):
            return
        with self.db.transaction():
            self.db.run('UPDATE parse_jobs SET status=?,error_code=?,cancel_requested=0,lease_until=NULL,updated_at=? WHERE id=?',(status,code,now(),job_id))
            self.db.run('UPDATE files SET status=? WHERE id=? AND deleted_at IS NULL',(status,row['file_id']))

    async def run(self,job_id):
        row=self.db.one('SELECT * FROM parse_jobs WHERE id=?',(job_id,))
        owner=row['owner_id'];fid=row['file_id']
        try:
            async with self.slot:
                async with self.gates[owner].work():
                    file=self.owned(fid,owner)
                    if row['cancel_requested']:
                        raise asyncio.CancelledError()
                    self.db.run("UPDATE parse_jobs SET status='running',lease_until=?,updated_at=? WHERE id=?",(time.time()+60,now(),job_id))
                async def progress(phase):
                    async with self.gates[owner].work():
                        self.owned(fid,owner)
                        self.db.run('UPDATE parse_jobs SET phase=?,updated_at=? WHERE id=?',(phase,now(),job_id))
                        self.db.run('UPDATE files SET status=? WHERE id=? AND deleted_at IS NULL',(phase,fid))
                result=await parse_in_job(self.path(file['object_key']),Path(file['name']).suffix.lower()[1:],self.path('parsed/'+job_id+'.json'),progress)
                async with self.gates[owner].work():
                    file=self.owned(fid,owner)
                    current=self.db.one('SELECT * FROM parse_jobs WHERE id=?',(job_id,))
                    if current['cancel_requested']:
                        raise asyncio.CancelledError()
                    self.db.run("UPDATE parse_jobs SET phase='indexing',progress_total=? WHERE id=?",(len(result['paragraphs']),job_id))
                    self.db.run("UPDATE files SET status='indexing' WHERE id=?",(fid,))
                    with self.db.transaction():
                        publish_index(self.db,file,result)
                        self.db.run("UPDATE files SET status='ready',parser_revision=?,page_count=?,paragraph_count=?,warnings_json=? WHERE id=?",(result['parser_revision'],result['page_count'],len(result['paragraphs']),json.dumps(result['warnings']),fid))
                        self.db.run("UPDATE parse_jobs SET status='completed',progress_completed=progress_total,cancel_requested=0,lease_until=NULL,updated_at=? WHERE id=?",(now(),job_id))
        except asyncio.CancelledError:
            if not self.stopping:
                self.failed(job_id,'cancelled',None)
        except DomainError as error:
            self.failed(job_id,'failed',error.code)
        except Exception:
            self.failed(job_id,'failed','PARSER_FAILED')

    def authorize(self,actor):
        if not self.auth.valid(actor):
            raise DomainError('AUTH_REQUIRED',401,'登录状态已撤销')
        return actor['user_id']

    def cancel(self,fid,actor):
        owner=self.authorize(actor)
        file=self.owned(fid,owner)
        job=self.db.one("SELECT * FROM parse_jobs WHERE file_id=? AND status IN ('queued','running')",(fid,))
        if job:
            self.db.run('UPDATE parse_jobs SET cancel_requested=1 WHERE id=?',(job['id'],))
            if job['id'] in self.tasks:
                self.tasks[job['id']].cancel()
        return {'file':self.dto(file),'job':self.job(fid),'cancel_requested':job is not None}

    def retry(self,request,fid,actor):
        owner=self.authorize(actor)
        file=self.owned(fid,owner);old=self.db.one('SELECT * FROM parse_jobs WHERE file_id=? ORDER BY attempt DESC LIMIT 1',(fid,))
        with self.db.transaction():
            existing,key=self.convs.lookup_key(request,owner,{})
            if existing:
                return {'file':self.dto(file),'job':self.job(fid)}
            if old['status']!='failed' or old['error_code'] not in RETRY:
                raise doc_error('FILE_NOT_RETRYABLE')
            if old['attempt']>=3:
                raise doc_error('ATTEMPTS_EXHAUSTED')
            job_id=self.new_job(fid,owner,old['attempt']+1)
            self.db.run("UPDATE files SET status='security_check' WHERE id=?",(fid,))
            self.convs.save_key(key,'file',fid)
        self.schedule(job_id)
        return {'file':self.dto(self.owned(fid,owner)),'job':self.job(fid)}

    def clean_reservations(self,all_entries=False):
        entries=self.db.all('SELECT * FROM upload_reservations'+('' if all_entries else ' WHERE expires_at<?'),() if all_entries else (time.time(),))
        for row in entries:
            self.path(row['temp_key']).unlink(missing_ok=True)
            self.db.run('DELETE FROM upload_reservations WHERE id=?',(row['id'],))

    def recover(self):
        self.clean_reservations(True)
        # Only private object folders; never walk user directories. All old Job
        # handles died with the previous parent, so stale worker output is inert.
        referenced={r['object_key'] for r in self.db.all('SELECT object_key FROM files WHERE object_key IS NOT NULL')}
        for path in (self.cfg.data_dir/'objects').iterdir():
            if path.is_file() and 'objects/'+path.name not in referenced:
                path.unlink()
        for path in (self.cfg.data_dir/'parsed').iterdir():
            if path.is_file():
                path.unlink()
        for row in self.db.all("SELECT * FROM parse_jobs WHERE status IN ('queued','running')"):
            if row['cancel_requested']:
                self.failed(row['id'],'cancelled',None)
            elif row['restart_recoveries']>=1:
                self.failed(row['id'],'failed','PROCESS_INTERRUPTED')
            else:
                self.db.run("UPDATE parse_jobs SET status='queued',restart_recoveries=restart_recoveries+1,lease_until=NULL WHERE id=?",(row['id'],))
                self.schedule(row['id'])

    async def close(self):
        self.stopping=True
        tasks=list(self.tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.wait(tasks,timeout=3)
