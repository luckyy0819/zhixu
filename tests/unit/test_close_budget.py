import asyncio
import socket
import time
import httpx


def test_F02_close_deadline_forces_socket_shutdown(caplog):
    from server.model import close_upstream
    async def run():
        local,peer=socket.socketpair();peer.settimeout(.2)
        class StreamInfo:
            def get_extra_info(self,name):
                return local if name=='socket' else None
        class Response:
            extensions={'network_stream':StreamInfo()}
            async def aclose(self):
                await asyncio.Event().wait()
        class Client:
            async def aclose(self):
                await asyncio.Event().wait()
        started=time.monotonic()
        await close_upstream(Response(),Client(),True)
        elapsed=time.monotonic()-started
        assert 1.8<=elapsed<2.8
        assert peer.recv(1)==b''
        peer.close();local.close()
    asyncio.run(run())
    assert 'MODEL_CLOSE_TIMEOUT' in caplog.text


def test_F02_producer_finalizers_cannot_hold_generation_slot(caplog):
    from server.model import stream_model
    from server.config import Settings
    from pathlib import Path
    import tempfile
    async def run(tmp):
        local,peer=socket.socketpair();peer.settimeout(.2)
        class Info:
            def get_extra_info(self,name):
                return local if name=='socket' else None
        class Blocking(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield b'data: {"choices":[{"delta":{"content":"partial"}}]}\n\n'
                await asyncio.Event().wait()
            async def aclose(self):
                await asyncio.Event().wait()
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda req:httpx.Response(200,stream=Blocking(),extensions={'network_stream':Info()}))) as client:
            cfg=Settings(data_dir=Path(tmp),model_base_url='http://localhost:1/v1',model_name='test')
            iterator=stream_model(cfg,[],client)
            assert await anext(iterator)=='partial'
            started=time.monotonic();await iterator.aclose()
            assert time.monotonic()-started<2.8
            assert peer.recv(1)==b''
        peer.close();local.close()
    with tempfile.TemporaryDirectory() as tmp:
        asyncio.run(run(tmp))
    assert 'CLOSE_TIMEOUT' in caplog.text


def test_cleanup_failures_and_no_socket_are_sanitized(caplog):
    from server.model_cleanup import close_upstream,terminate_socket,stop_producer
    class Failed:
        extensions={}
        async def aclose(self):
            raise OSError('synthetic secret must not be logged')
    async def run():
        await close_upstream(Failed(),None,False)
        task=asyncio.create_task(asyncio.sleep(0));await task
        await stop_producer(task,{})
    asyncio.run(run())
    assert 'MODEL_CLOSE_FAILED' in caplog.text and 'secret' not in caplog.text
    assert terminate_socket(None) is False
    assert terminate_socket(Failed()) is False
