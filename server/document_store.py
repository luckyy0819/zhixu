import json
import re
import unicodedata
from pathlib import Path
from .conversations import valid_uuid
from .document_errors import doc_error
from .errors import DomainError


class DocumentStore:
    def __init__(self,db,cfg,convs):
        self.db,self.cfg,self.convs=db,cfg,convs
        for folder in ('objects','staging','parsed'):
            (cfg.data_dir/folder).mkdir(exist_ok=True)

    def path(self,key):
        candidate=(self.cfg.data_dir/key).resolve()
        if not candidate.is_relative_to(self.cfg.data_dir) or candidate==self.cfg.data_dir:
            raise doc_error('STORAGE_ERROR')
        return candidate

    def owned(self,fid,owner,deleted=False):
        valid_uuid(fid)
        row=self.db.one('''SELECT f.*,cf.enabled,cf.revision AS enabled_revision FROM files f JOIN conversation_files cf
 ON cf.file_id=f.id AND cf.version=f.version WHERE f.id=? AND f.owner_id=?''',(fid,owner))
        if not row:
            raise DomainError('NOT_FOUND',404,'资料不存在或不可访问')
        self.convs.owned(row['conversation_id'],owner)
        if row['deleted_at'] and not deleted:
            raise DomainError('NOT_FOUND',404,'资料已删除')
        return row

    def job(self,fid):
        row=self.db.one('SELECT * FROM parse_jobs WHERE file_id=? ORDER BY attempt DESC LIMIT 1',(fid,))
        if not row:
            return None
        return {k:row[k] for k in ('id','status','phase','attempt')} | {'progress':{'completed':row['progress_completed'],'total':row['progress_total']},
            'error':doc_error(row['error_code']).dto() if row['error_code'] else None}

    def dto(self,row):
        keys=('id','conversation_id','name','mime','size','sha256','version','parser_revision','status','enabled_revision','page_count','paragraph_count','created_at')
        return {k:row[k] for k in keys}|{'enabled':bool(row['enabled']),'warnings':json.loads(row['warnings_json']),'job':self.job(row['id'])}

    def listing(self,cid,owner):
        self.convs.owned(cid,owner)
        rows=self.db.all('SELECT id FROM files WHERE conversation_id=? AND owner_id=? AND deleted_at IS NULL ORDER BY created_at,id',(cid,owner))
        return [self.dto(self.owned(r['id'],owner)) for r in rows]

    def preview(self,fid,owner,version,paragraph,start=None,end=None,window_start=None,window_end=None):
        row=self.owned(fid,owner,True)
        if row['deleted_at']:
            raise doc_error('SOURCE_DELETED')
        if row['version']!=version:
            raise doc_error('VERSION_MISMATCH')
        if row['status']!='ready':
            raise doc_error('FILE_NOT_READY')
        p=self.db.one('SELECT * FROM paragraphs WHERE file_id=? AND version=? AND paragraph_no=?',(fid,version,paragraph))
        if not p or (start is None)!=(end is None) or (window_start is None)!=(window_end is None):
            raise DomainError('VALIDATION_ERROR')
        text=p['text'];n=len(text)
        if start is not None and not 0<=start<end<=n:
            raise DomainError('VALIDATION_ERROR')
        if window_start is None:
            left=max(0,start-500) if start is not None else 0
            right=min(n,end+500) if end is not None else min(n,1000)
        else:
            left,right=window_start,window_end
        if not 0<=left<right<=n or start is not None and not left<=start<end<=right or len(text[left:right].encode())>20480:
            raise DomainError('VALIDATION_ERROR')
        return dict(file_id=fid,version=version,name=row['name'],page=p['page'],paragraph=paragraph,text=text[left:right],text_start=left,text_end=right,
            highlight={'start':start,'end':end} if start is not None else None,previous_paragraph=paragraph-1 if paragraph>1 else None,
            next_paragraph=paragraph+1 if paragraph<row['paragraph_count'] else None,parser_revision=row['parser_revision'],warnings=json.loads(row['warnings_json']))
