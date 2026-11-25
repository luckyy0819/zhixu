"""E01 author integration probe: actual Uvicorn/h11 + TCP backpressure.

Test-only route and tracing subclasses, not mounted by production main.py.
No mock ASGI send, no Browser, no shared 8787/8765 process manipulation.
"""
import asyncio
import contextvars
import json
import socket
import tempfile
import time
import hashlib
from datetime import datetime,timezone
from pathlib import Path
from urllib.parse import parse_qs
import httpx
import uvicorn
from uvicorn.protocols.http.h11_impl import H11Protocol
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route
from server.config import Settings
from server.main import create_app
from server.gates import Gate

label=contextvars.ContextVar('probe_label',default='')
traces=[];signals=None


def emit(event,**data):
    name=label.get()
    if name in ('s0','s1','s2','logout') or 'name' in data:
        item={'order':len(traces),'at':time.monotonic(),'event':event,'name':name,**data}
        traces.append(item);signals.put_nowait(item)


class TraceTransport:
    def __init__(self,transport):
        self.transport=transport;self.name=''
    def __getattr__(self,key):
        return getattr(self.transport,key)
    def write(self,data):
        self.name=label.get() or self.name
        emit('transport_write',name=self.name,bytes=len(data),synthetic_body=b'SYNTHETIC-PROBE-' in data[:80],
             status_line=data.split(b'\r\n',1)[0].decode('ascii','replace') if data.startswith(b'HTTP/') else None)
        self.transport.write(data)
    def close(self):
        self.transport.close()
        emit('transport_close',name=self.name,is_closing=self.transport.is_closing())
    def abort(self):
        self.transport.abort()
        emit('transport_abort',name=self.name,is_closing=self.transport.is_closing())


class Protocol(H11Protocol):
    def connection_made(self,transport):
        super().connection_made(TraceTransport(transport))


class TraceGate(Gate):
    async def deliver(self,verify,send,message):
        emit('delivery_enter',kind=message['type'])
        async def actual_send(data):
            emit('send_begin',kind=data['type'],bytes=len(data.get('body',b'')))
            try:
                await send(data)
            except asyncio.CancelledError:
                emit('send_cancelled',kind=data['type']);raise
            else:
                emit('send_end',kind=data['type'])
        try:
            result=await super().deliver(verify,actual_send,message)
        except TimeoutError:
            emit('send_timeout');raise
        emit('delivery_result',allowed=result)
        return result
    def revoke(self,action):
        def committed():
            result=action();emit('T1_session_revoked');return result
        task=super().revoke(committed)
        emit('pending_registered',tokens=len(self.tokens))
        return task


class ProbeBody(Response):
    async def __call__(self,scope,receive,send):
        await send({'type':'http.response.start','status':200,'headers':[(b'content-type',b'application/octet-stream')]})
        chunk=b'SYNTHETIC-PROBE-'*(512*1024)
        for _ in range(16):
            await send({'type':'http.response.body','body':chunk,'more_body':True})
        await send({'type':'http.response.body','body':b'','more_body':False})


async def main():
    global signals
    signals=asyncio.Queue();ready=asyncio.Event();sockets=[]
    listener=socket.socket();listener.bind(('127.0.0.1',0));listener.listen(128);listener.setblocking(False)
    port=listener.getsockname()[1]
    with tempfile.TemporaryDirectory(prefix='zhixu-E01-') as tmp:
        cfg=Settings(data_dir=Path(tmp),port=port,origin=f'http://127.0.0.1:{port}',model_base_url='http://127.0.0.1:1/v1',model_name='not-used')
        app=create_app(cfg);db,auth,convs,gens,gates=app.state.services;gates.default_factory=TraceGate
        async def probe(request:Request):
            actor=auth.actor(request);conv=convs.owned(request.query_params['cid'],actor['user_id'])
            request.state.cid=conv['id'];request.state.epoch=conv['access_epoch']
            return ProbeBody()
        app.router.routes.insert(0,Route('/_probe/stream',probe))
        async def tagged(scope,receive,send):
            name=parse_qs(scope.get('query_string',b'').decode()).get('name',[''])[0]
            if scope.get('path')=='/api/v1/auth/logout':
                name='logout'
            token=label.set(name)
            try:
                await app(scope,receive,send)
            finally:
                label.reset(token)
        class Server(uvicorn.Server):
            async def startup(self,sockets=None):
                await super().startup(sockets);ready.set()
        server=Server(uvicorn.Config(tagged,host=cfg.host,port=port,http=Protocol,access_log=False,log_level='critical'))
        running=asyncio.create_task(server.serve(sockets=[listener]))
        try:
            await asyncio.wait_for(ready.wait(),5)
            async with httpx.AsyncClient(base_url=cfg.origin,trust_env=False,timeout=5) as client:
                nonce=(await client.get('/api/v1/auth/csrf')).json()['data']['csrf_token']
                headers={'Origin':cfg.origin,'X-CSRF-Token':nonce}
                result=await client.post('/api/v1/auth/register',headers=headers,json={'username':'probe_account','display_name':'synthetic','password':'synthetic-password-123'})
                headers['X-CSRF-Token']=result.json()['data']['csrf_token']
                import uuid
                headers['Idempotency-Key']=str(uuid.uuid4())
                cid=(await client.post('/api/v1/conversations',headers=headers,json={})).json()['data']['id']
                cookie='; '.join(k+'='+v for k,v in client.cookies.items())
                async def connect(name):
                    s=socket.socket();s.setsockopt(socket.SOL_SOCKET,socket.SO_RCVBUF,4096);s.setblocking(False)
                    await asyncio.get_running_loop().sock_connect(s,('127.0.0.1',port));sockets.append(s)
                    request=f'GET /_probe/stream?name={name}&cid={cid} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nCookie: {cookie}\r\n\r\n'
                    await asyncio.get_running_loop().sock_sendall(s,request.encode());return s
                await connect('s0')
                seen_body=0
                while seen_body<2:
                    event=await asyncio.wait_for(signals.get(),5)
                    if event['name']=='s0' and event['event']=='send_begin' and event.get('kind')=='http.response.body':
                        seen_body+=1
                await connect('s1');await connect('s2');queued=set()
                while len(queued)<2:
                    event=await asyncio.wait_for(signals.get(),2)
                    if event['event']=='delivery_enter' and event['name'] in ('s1','s2'):
                        queued.add(event['name'])
                logout=await client.post('/api/v1/auth/logout',headers=headers,json={})
                assert logout.status_code==200
                assert (await client.get('/api/v1/auth/me')).status_code==401
                t1=next(e['at'] for e in traces if e['event']=='T1_session_revoked')
                pending=next(e['at'] for e in traces if e['event']=='pending_registered')
                t1_order=next(e['order'] for e in traces if e['event']=='T1_session_revoked')
                pending_order=next(e['order'] for e in traces if e['event']=='pending_registered')
                diagnostic=Path('docs/engineering/audit/AI-DEV/2026-11-25-p0.2')
                diagnostic.mkdir(exist_ok=True,parents=True)
                (diagnostic/'real-asgi-trace-diagnostic.json').write_text(json.dumps(traces,indent=2),encoding='utf-8')
                assert any(e['event']=='send_timeout' and e['name']=='s0' for e in traces)
                assert any(e['event']=='send_cancelled' and e['name']=='s0' for e in traces)
                for name in ('s1','s2'):
                    assert any(e['event']=='delivery_result' and e['name']==name and e['allowed'] is False for e in traces)
                    assert not any(e['event']=='send_begin' and e['name']==name and e['order']>pending_order for e in traces)
                assert not any(e['event']=='transport_write' and e['name'] in ('s0','s1','s2') and e.get('synthetic_body') and e['order']>t1_order for e in traces)
                assert all(any(e['event']=='transport_close' and e['name']==name and e['is_closing'] for e in traces) for name in ('s0','s1','s2'))
                report={'result':'passed','stack':{'uvicorn':uvicorn.__version__,'httpx':httpx.__version__,'http':'actual h11/TCP'},'port':port,
                    'T1':t1,'T1_order':t1_order,'pending':pending,'pending_order':pending_order,'trace':traces,'proof':'S0 real transport backpressure times out/cancels; queued S1/S2 skip; T1 revokes session; transports close; no new private synthetic body transport.write after T1',
                    'boundary':'Windows coarse clock ties are resolved by synchronous trace order. Uvicorn may emit generic 500 when rejected headers never started; no private payload. Bytes accepted by transport before T1 are not retracted. Test-only route absent from production app.'}
                report['at']=datetime.now(timezone.utc).isoformat()
                source_root=Path(__file__).resolve().parents[2]
                report['production_hashes']={str(p.relative_to(source_root)).replace('\\','/'):hashlib.sha256(p.read_bytes()).hexdigest()
                    for p in [source_root/'server'/name for name in ['gates.py','security.py','auth.py','main.py','db.py','__init__.py']]}
        finally:
            for s in sockets:
                s.close()
            server.should_exit=True
            await asyncio.wait_for(running,5)
    out=Path('docs/engineering/audit/AI-DEV/2026-11-25-p0.2');out.mkdir(exist_ok=True,parents=True)
    (out/'real-asgi-probe.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
    print(json.dumps({k:v for k,v in report.items() if k!='trace'}))


if __name__=='__main__':
    asyncio.run(main())
