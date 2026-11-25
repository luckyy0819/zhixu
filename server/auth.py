import asyncio
import hashlib
import hmac
import secrets
import sqlite3
import time
from collections import defaultdict, deque
from fastapi import Request
from .db import now, uid
from .errors import DomainError


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def password_hash(password, salt):
    return hashlib.scrypt(password.encode(),salt=bytes.fromhex(salt),n=32768,r=8,p=1,maxmem=67108864,dklen=32).hex()


def cookie(response, name, value, seconds):
    response.set_cookie(name,value,max_age=seconds,httponly=True,samesite='strict',secure=False,path='/')


class Auth:
    def __init__(self, db, gates):
        self.db,self.gates = db,gates
        self.hash_slots=asyncio.Semaphore(2)
        self.attempts=defaultdict(deque)
        self.dummy_salt=secrets.token_hex(16)

    def rate(self, request, username):
        at=time.time()
        keys=[('source', request.client.host),('name',username.lower())]
        for key in keys:
            q=self.attempts[key]
            while q and q[0] <= at-60:
                q.popleft()
            if len(q)>=5:
                raise DomainError('AUTH_RATE_LIMIT',429,'登录尝试过多，请一分钟后重试',True,{'retry_after_seconds':60})
        for key in keys:
            self.attempts[key].append(at)

    def preauth(self, response):
        token,csrf=secrets.token_urlsafe(32),secrets.token_urlsafe(32)
        self.db.run('DELETE FROM preauth_nonces WHERE expires_at<?',(time.time(),))
        self.db.run('INSERT INTO preauth_nonces VALUES(?,?,?)',(digest(token),digest(csrf),time.time()+600))
        cookie(response,'zhixu_preauth',token,600)
        return {'csrf_token':csrf,'expires_at':time.time()+600}

    def consume_nonce(self, request):
        token=request.cookies.get('zhixu_preauth','')
        row=self.db.one('SELECT * FROM preauth_nonces WHERE token_hash=?',(digest(token),))
        if not row or row['expires_at']<=time.time() or not hmac.compare_digest(row['csrf_hash'],digest(request.headers.get('x-csrf-token',''))):
            raise DomainError('CSRF_INVALID',403,'登录凭据已过期，请重新获取')
        self.db.run('DELETE FROM preauth_nonces WHERE token_hash=?',(digest(token),))

    async def credentials(self, request, data, register=False):
        self.rate(request,data.username)
        self.consume_nonce(request)
        row=self.db.one('SELECT * FROM users WHERE username_norm=? AND deleted_at IS NULL',(data.username.lower(),))
        salt=secrets.token_hex(16) if register or not row else row['salt']
        async with self.hash_slots:
            hashed=await asyncio.to_thread(password_hash,data.password,salt)
        if register:
            if row:
                raise DomainError('USERNAME_UNAVAILABLE',409,'用户名不可用')
            user_id=uid()
            try:
                self.db.run('INSERT INTO users VALUES(?,?,?,?,?,?,?,?,?,NULL)',(user_id,data.username.lower(),data.display_name,hashed,salt,32768,8,1,now()))
            except sqlite3.IntegrityError:
                raise DomainError('USERNAME_UNAVAILABLE',409,'用户名不可用')
            row=self.db.one('SELECT * FROM users WHERE id=?',(user_id,))
        elif not row or not hmac.compare_digest(row['password_hash'],hashed):
            raise DomainError('AUTH_INVALID',401,'用户名或密码不正确')
        token,csrf=secrets.token_urlsafe(32),secrets.token_urlsafe(32)
        expires=time.time()+86400
        # Rotate the login currently held by this browser, without affecting other devices.
        old=digest(request.cookies.get('zhixu_session',''))
        previous=self.db.one('SELECT user_id FROM auth_sessions WHERE token_hash=?',(old,))
        if previous:
            await asyncio.shield(self.gates[previous['user_id']].revoke(
                lambda:self.db.run('DELETE FROM auth_sessions WHERE token_hash=?',(old,))))
        self.db.run('INSERT INTO auth_sessions VALUES(?,?,?,?,?,?,?)',(uid(),row['id'],digest(token),digest(csrf),now(),expires,now()))
        return {'user':self.user(row),'csrf_token':csrf,'expires_at':expires},token

    @staticmethod
    def user(row):
        return dict(id=row['id'],username=row['username_norm'],display_name=row['display_name'])

    def actor(self, request, write=False):
        token_hash=digest(request.cookies.get('zhixu_session',''))
        row=self.db.one('''SELECT a.*,u.username_norm,u.display_name FROM auth_sessions a JOIN users u ON u.id=a.user_id
 WHERE a.token_hash=? AND u.deleted_at IS NULL''',(token_hash,))
        if not row:
            raise DomainError('AUTH_REQUIRED',401,'请先登录')
        if row['expires_at'] <= time.time():
            raise DomainError('SESSION_EXPIRED',401,'登录已过期，请重新登录')
        if write and not hmac.compare_digest(row['csrf_hash'],digest(request.headers.get('x-csrf-token',''))):
            raise DomainError('CSRF_INVALID',403,'页面令牌已更新，请重新获取后重试')
        request.state.actor=row
        return row

    def me(self, request):
        actor=self.actor(request)
        csrf=secrets.token_urlsafe(32)
        self.db.run('UPDATE auth_sessions SET csrf_hash=?,last_seen_at=? WHERE id=?',(digest(csrf),now(),actor['id']))
        return dict(user={'id':actor['user_id'],'username':actor['username_norm'],'display_name':actor['display_name']},csrf_token=csrf,expires_at=actor['expires_at'])

    def valid(self, actor):
        return self.db.one('''SELECT a.id FROM auth_sessions a JOIN users u ON a.user_id=u.id
 WHERE a.id=? AND a.expires_at>? AND u.deleted_at IS NULL''',(actor['id'],time.time())) is not None
