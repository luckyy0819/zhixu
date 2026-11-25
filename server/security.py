import json
from datetime import datetime,timezone
from starlette.responses import RedirectResponse
from .errors import DomainError,error_response
from . import __version__

CSP="default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self'; object-src 'none'; frame-src 'none'; base-uri 'none'; form-action 'self'"


class Security:
    def __init__(self, app, cfg, auth, convs, gates):
        self.app,self.cfg,self.auth,self.convs,self.gates=app,cfg,auth,convs,gates

    async def __call__(self, scope, receive, send):
        if scope['type']!='http':
            await self.app(scope,receive,send);return
        headers={k.decode().lower():v.decode() for k,v in scope['headers']}
        state=scope.setdefault('state',{})
        host=headers.get('host','')
        async def secure_send(message):
            if message['type']=='http.response.start':
                # Audit routing/status only: never log body, query, cookies or headers.
                route=scope.get('route')
                audit={'at':datetime.now(timezone.utc).isoformat(),'task':'ZHIXU-TASK-005',
                       'action':scope['method'],'route':getattr(route,'path','unmatched'),
                       'status':message['status']}
                with (self.cfg.data_dir/'runtime-audit.jsonl').open('a',encoding='utf-8') as log:
                    log.write(json.dumps(audit)+'\n')
                message['headers']+= [(b'cache-control',b'no-store'),(b'x-content-type-options',b'nosniff'),
                    (b'content-security-policy',CSP.encode()),(b'referrer-policy',b'no-referrer'),(b'x-zhixu-version',__version__.encode())]
            actor=state.get('actor')
            if actor and not state.get('public_response'):
                def verify():
                    if not self.auth.valid(actor):
                        return False
                    cid=state.get('cid')
                    if cid:
                        row=self.auth.db.one('SELECT access_epoch FROM conversations WHERE id=? AND owner_id=? AND deleted_at IS NULL',(cid,actor['user_id']))
                        return row is not None and row['access_epoch']==state['epoch']
                    return True
                if not await self.gates[actor['user_id']].deliver(verify,send,message):
                    raise ConnectionAbortedError('delivery revoked')
            else:
                await send(message)
        if host==f'localhost:{self.cfg.port}':
            await RedirectResponse(self.cfg.origin+scope['path'],status_code=307)(scope,receive,secure_send);return
        if host!=f'127.0.0.1:{self.cfg.port}':
            await error_response(DomainError('ORIGIN_DENIED',403,'请求主机无效'))(scope,receive,secure_send);return
        if scope['method'] not in ('GET','HEAD','OPTIONS'):
            if headers.get('origin')!=self.cfg.origin:
                await error_response(DomainError('ORIGIN_DENIED',403,'仅允许本地同源操作'))(scope,receive,secure_send);return
            if not headers.get('content-type','').lower().startswith('application/json'):
                await error_response(DomainError('VALIDATION_ERROR',422,'请使用JSON请求'))(scope,receive,secure_send);return
            body=bytearray()
            while True:
                chunk=await receive()
                if chunk['type']=='http.disconnect':
                    return
                body.extend(chunk.get('body',b''))
                if len(body)>32768:
                    await error_response(DomainError('VALIDATION_ERROR',422,'请求正文不得超过32KiB'))(scope,receive,secure_send);return
                if not chunk.get('more_body',False):
                    break
            delivered=False
            original_receive=receive
            async def bounded_receive():
                nonlocal delivered
                if not delivered:
                    delivered=True
                    return {'type':'http.request','body':bytes(body),'more_body':False}
                return await original_receive()
            receive=bounded_receive
        await self.app(scope,receive,secure_send)
