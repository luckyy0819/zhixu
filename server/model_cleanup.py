"""DES002 R2 P3: supervised cleanup, bounded wait, synchronous socket termination."""
import asyncio
import logging
import socket

log=logging.getLogger('zhixu.model')
_cleanup_tasks=set()


def terminate_socket(response):
    if response is None:
        return False
    stream=response.extensions.get('network_stream')
    if stream is None:
        return False
    try:
        sock=stream.get_extra_info('socket')
        if sock is None:
            return False
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass  # An already closed TCP socket has the desired terminal state.
        close=getattr(sock,'close',None)
        if close:
            close()
        return True
    except (OSError,AttributeError,RuntimeError):
        return False


def consume(task):
    _cleanup_tasks.discard(task)
    if not task.cancelled():
        task.exception()


async def close_upstream(response,client,own):
    async def cleanup():
        try:
            if response is not None:
                await response.aclose()
        finally:
            if own:
                await client.aclose()
    task=asyncio.create_task(cleanup())
    _cleanup_tasks.add(task);task.add_done_callback(consume)
    try:
        done,_=await asyncio.wait([task],timeout=2)
    except BaseException:
        terminated=terminate_socket(response)
        task.cancel()
        log.warning('MODEL_CLOSE_CANCELLED socket_terminated=%s',terminated)
        raise
    if not done:
        terminated=terminate_socket(response)
        task.cancel()
        log.warning('MODEL_CLOSE_TIMEOUT socket_terminated=%s',terminated)
        # No await on a non-cooperating close after the deadline; the local TCP
        # connection is shut down and its abandoned cleanup remains supervised.
        return
    if not task.cancelled() and task.exception() is not None:
        terminated=terminate_socket(response)
        log.warning('MODEL_CLOSE_FAILED socket_terminated=%s',terminated)


async def stop_producer(task,holder):
    """A transport's own exception finalizers cannot hold a generation slot forever."""
    if task.done():
        if not task.cancelled():
            task.exception()
        return
    task.cancel()
    _cleanup_tasks.add(task);task.add_done_callback(consume)
    try:
        done,_=await asyncio.wait([task],timeout=2)
    except BaseException:
        terminate_socket(holder.get('response'));task.cancel()
        raise
    if not done:
        terminated=terminate_socket(holder.get('response'))
        task.cancel()
        log.warning('MODEL_PRODUCER_CLOSE_TIMEOUT socket_terminated=%s',terminated)
