"""DES003 upstream SSE adapter. Only real content is yielded; stop AND DONE required."""
import asyncio
import json
import httpx
from .errors import DomainError
from .model_cleanup import close_upstream,stop_producer


def model_error(code):
    codes={
        'OUTPUT_LIMIT':(502,'回答达到输出上限，内容尚未完整',False),
        'UPSTREAM_INCOMPLETE':(502,'模型流未完整结束，请显式重试',True),
        'MODEL_FORMAT_ERROR':(502,'模型返回格式无效，请联系开发排查',False),
        'MODEL_AUTH_ERROR':(502,'模型认证失败，请检查服务端配置',False),
        'MODEL_RATE_LIMIT':(429,'模型限流，请稍后显式重试',True),
        'MODEL_TIMEOUT':(504,'模型响应超时，请显式重试',True),
        'MODEL_UNAVAILABLE':(503,'模型服务暂时不可用，请检查连接后重试',True),
        'PROCESS_INTERRUPTED':(503,'服务重启中断了生成，请显式重试',True),
        'CONTEXT_LIMIT':(413,'模型上下文预算不足，请新建会话',False),
    }
    status,message,retry=codes[code]
    return DomainError(code,status,message,retry,{'retry_after_seconds':2} if status==429 else {})


async def stream_model(cfg, messages, client=None):
    queue=asyncio.Queue(maxsize=4);holder={}
    async def produce():
        try:
            async for content in decode_model(cfg,messages,client,holder):
                await queue.put(('content',content))
            await queue.put(('end',None))
        except Exception as error:
            await queue.put(('error',error))
    task=asyncio.create_task(produce())
    try:
        while True:
            kind,value=await queue.get()
            if kind=='end':
                return
            if kind=='error':
                raise value
            yield value
    finally:
        await stop_producer(task,holder)


async def decode_model(cfg, messages, client=None, holder=None):
    own=client is None
    if own:
        client=httpx.AsyncClient(timeout=httpx.Timeout(cfg.read_timeout,connect=cfg.connect_timeout),follow_redirects=False,trust_env=False)
    r=None
    try:
        async with asyncio.timeout(cfg.total_timeout):
            headers={'Authorization':'Bearer '+cfg.api_key} if cfg.api_key else {}
            request=client.build_request('POST',cfg.model_base_url+'/chat/completions',headers=headers,json={
                    'model':cfg.model_name,'stream':True,'messages':messages,'max_tokens':cfg.output_tokens,'temperature':0.2})
            r=await client.send(request,stream=True)
            if holder is not None:
                holder['response']=r
            if r is not None:
                if r.status_code in (401,403):
                    raise model_error('MODEL_AUTH_ERROR')
                if r.status_code==429:
                    raise model_error('MODEL_RATE_LIMIT')
                if r.status_code != 200:
                    if r.status_code==400:
                        raw=bytearray()
                        async for part in r.aiter_bytes():
                            raw.extend(part)
                            if len(raw)>32768:
                                break
                        try:
                            obj=json.loads(raw)
                            if obj.get('error',{}).get('code')=='context_length_exceeded':
                                raise model_error('CONTEXT_LIMIT')
                        except (ValueError,AttributeError):
                            pass
                    raise model_error('MODEL_UNAVAILABLE')
                lines=r.aiter_lines().__aiter__()
                frame=[]; size=0; finish=None; done=False
                deadline=asyncio.get_running_loop().time()+cfg.read_timeout
                while True:
                    try:
                        async with asyncio.timeout_at(deadline):
                            line=await anext(lines)
                    except StopAsyncIteration:
                        break
                    size+=len(line.encode())
                    if size>1048576:
                        raise model_error('MODEL_FORMAT_ERROR')
                    if line:
                        if line.startswith('data:'):
                            frame.append(line[5:].lstrip(' '))
                        continue
                    if not frame:
                        size=0
                        continue
                    data='\n'.join(frame); frame=[]; size=0
                    if data=='[DONE]':
                        done=True
                        break
                    try:
                        obj=json.loads(data)
                        choices=obj.get('choices',[])
                        if not isinstance(choices,list):
                            raise ValueError()
                        if not choices:
                            continue
                        c=choices[0]; delta=c.get('delta',{})
                        content=delta.get('content')
                        if content is not None and not isinstance(content,str):
                            raise ValueError()
                        if content:
                            if finish:
                                raise ValueError()
                            deadline=asyncio.get_running_loop().time()+cfg.read_timeout
                            yield content
                        if c.get('finish_reason'):
                            finish=c['finish_reason']
                    except (ValueError,TypeError,KeyError,AttributeError):
                        raise model_error('MODEL_FORMAT_ERROR')
                if finish=='length':
                    raise model_error('OUTPUT_LIMIT')
                if not done or finish!='stop':
                    raise model_error('UPSTREAM_INCOMPLETE')
    except (TimeoutError,httpx.TimeoutException):
        raise model_error('MODEL_TIMEOUT')
    except httpx.RequestError:
        raise model_error('MODEL_UNAVAILABLE')
    finally:
        await close_upstream(r,client,own)
