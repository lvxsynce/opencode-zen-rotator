import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime

import httpx
import pytest

from zen_rotator.config import Settings
from zen_rotator.gateway import clean_headers, create_app, is_daily_free_limit, retry_delay

TOKEN = 'local-test-token-' + 'x' * 32
BODY = {'model': 'example-free', 'messages': [{'role': 'user', 'content': 'OK'}]}


@asynccontextmanager
async def gateway(handlers, **kwargs):
    settings = Settings(tuple(['direct'] + [f'socks5://proxy-{i}.example:1080' for i in range(1, len(handlers))]), TOKEN, **kwargs)
    clients = iter([httpx.AsyncClient(transport=httpx.MockTransport(h)) for h in handlers])
    now = [100.0]
    app = create_app(settings, lambda route: next(clients), clock=lambda: now[0])
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://gateway', headers={'x-zen-rotator-token': TOKEN}) as client:
            yield client, app.state.pool, now


def reply(code, **kwargs):
    return lambda request: httpx.Response(code, json={'result': code}, **kwargs)


async def test_429_rotates_and_sticks_to_successful_route():
    async with gateway([reply(429, headers={'retry-after': '30'}), reply(200), reply(200)]) as (c, pool, now):
        r = await c.post('/v1/chat/completions', json=BODY)
        assert r.status_code == 200
        assert r.headers['x-zen-route-index'] == '1'
        h = (await c.get('/health')).json()
        assert h['requests'] == [1, 1, 0]
        assert h['rotations_429'] == h['rate_limits_429'] == h['retries'] == 1
        assert h['cooldown_seconds'] == [30, 0, 0]
        assert h['active'] == 0
        await c.post('/v1/chat/completions', json=BODY)
        assert pool.requests == [1, 2, 0]


async def test_daily_free_message_in_success_response_rotates():
    message = "You've used today's free messages. They reset at 8:00 AM."
    async with gateway([
        lambda request: httpx.Response(200, text=message),
        reply(200),
    ]) as (c, pool, now):
        response = await c.post('/v1/chat/completions', json=BODY)
        assert response.status_code == 200
        assert response.headers['x-zen-route-index'] == '1'
        assert pool.requests == [1, 1]
        assert pool.rate_limits_429 == 0
        assert pool.daily_free_limits == 1
        assert pool.rotations_429 == 0


def test_daily_free_message_detection_is_narrow():
    assert is_daily_free_limit(200, b"You've used today's free messages")
    assert is_daily_free_limit(200, b"You have used today's free messages")
    assert not is_daily_free_limit(429, b"You've used today's free messages")
    assert not is_daily_free_limit(200, b"The model says you've used today's free messages")


async def test_every_route_limited_and_cooldown_does_not_hammer_upstream():
    async with gateway([reply(429), reply(429), reply(429)]) as (c, pool, now):
        r = await c.post('/v1/chat/completions', json=BODY)
        assert r.status_code == 429 and r.headers['retry-after'] == '60'
        assert pool.rotations_429 == 2 and pool.rate_limits_429 == 3
        await c.post('/v1/chat/completions', json=BODY)
        assert pool.requests == [1, 1, 1]
        now[0] += 61
        await c.post('/v1/chat/completions', json=BODY)
        assert pool.requests == [2, 2, 2]


async def test_skips_route_on_cooldown():
    async with gateway([reply(200), reply(200)]) as (c, pool, now):
        pool.blocked[0] = 200
        r = await c.post('/v1/chat/completions', json=BODY)
        assert r.status_code == 200 and pool.requests == [0, 1]


async def test_transport_failure_is_not_a_429_rotation():
    def failure(request):
        raise httpx.ConnectError('do not leak proxy-password', request=request)
    async with gateway([failure, reply(200)]) as (c, pool, now):
        assert (await c.post('/v1/chat/completions', json=BODY)).status_code == 200
        assert pool.retries == 1 and pool.rotations_429 == 0
    async with gateway([failure, failure]) as (c, pool, now):
        r = await c.post('/v1/chat/completions', json=BODY)
        assert r.status_code == 502 and 'proxy-password' not in r.text
        assert pool.active == 0


@pytest.mark.parametrize('path', ['chat/completions', 'responses', 'messages'])
async def test_native_headers_body_and_duplicate_query_parameters(path):
    def handler(request):
        assert request.headers['authorization'] == 'Bearer native-key'
        assert request.headers['x-opencode-client'] == 'opencode'
        assert 'x-zen-rotator-token' not in request.headers
        assert 'x-remove' not in request.headers
        assert request.url.params.get_list('tag') == ['a', 'b']
        assert request.url.path == '/zen/v1/' + path
        assert b'example-free' in request.content
        return httpx.Response(200, json={'ok': True}, headers={'connection': 'x-private', 'x-private': 'remove'})
    async with gateway([handler]) as (c, pool, now):
        r = await c.post('/v1/' + path + '?tag=a&tag=b', json=BODY, headers={'authorization': 'Bearer native-key', 'x-opencode-client': 'opencode', 'connection': 'x-remove', 'x-remove': 'hidden'})
        assert r.status_code == 200 and 'x-private' not in r.headers


@pytest.mark.parametrize('status', [400, 401, 403, 404, 500])
async def test_other_errors_not_retried(status):
    async with gateway([reply(status), reply(200)]) as (c, pool, now):
        assert (await c.post('/v1/chat/completions', json=BODY)).status_code == status
        assert pool.requests == [1, 0] and pool.retries == 0


async def test_validation_and_access_control():
    async with gateway([reply(200)], max_body_bytes=100) as (c, pool, now):
        assert (await c.post('/v1/chat/completions', json=BODY, headers={'x-zen-rotator-token': ''})).status_code == 401
        assert (await c.post('/v1/anything', json=BODY)).status_code == 404
        assert (await c.post('/v1/chat/completions', content='{}', headers={'content-encoding': 'gzip'})).status_code == 415
        for data in ['bad json', '[]', 'null']:
            assert (await c.post('/v1/chat/completions', content=data)).status_code == 400
        for model in ['paid', '', None, 42]:
            assert (await c.post('/v1/chat/completions', json={'model': model})).status_code == 403
        assert (await c.post('/v1/chat/completions', content='x' * 101)).status_code == 413
        assert pool.requests == [0]


class Stream(httpx.AsyncByteStream):
    def __init__(self, pool_ref, fail=False):
        self.pool_ref, self.fail, self.closed = pool_ref, fail, False

    async def __aiter__(self):
        assert self.pool_ref[0].active == 1
        yield b'data: first\n\n'
        if self.fail:
            raise httpx.ReadError('stream interrupted')
        yield b'data: [DONE]\n\n'

    async def aclose(self):
        self.closed = True


async def test_stream_passes_through_and_closes():
    ref = [None]
    stream = Stream(ref)
    async with gateway([lambda request: httpx.Response(200, stream=stream, headers={'content-type': 'text/event-stream'})]) as (c, pool, now):
        ref[0] = pool
        r = await c.post('/v1/chat/completions', json=BODY)
        assert b'[DONE]' in r.content
        assert stream.closed and pool.active == 0


async def test_stream_failure_never_replays_request():
    ref = [None]
    stream = Stream(ref, fail=True)
    async with gateway([lambda request: httpx.Response(200, stream=stream, headers={'content-type': 'text/event-stream'}), reply(200)]) as (c, pool, now):
        ref[0] = pool
        with pytest.raises(Exception):
            await c.post('/v1/chat/completions', json=BODY)
        assert pool.requests == [1, 0]
        assert pool.retries == 0 and stream.closed and pool.active == 0


async def test_nonstream_read_failure_closes_upstream():
    ref = [None]
    stream = Stream(ref, fail=True)
    async with gateway([lambda request: httpx.Response(200, stream=stream)]) as (c, pool, now):
        ref[0] = pool
        with pytest.raises(httpx.ReadError):
            await c.post('/v1/chat/completions', json=BODY)
        assert stream.closed and pool.active == 0


async def test_concurrent_requests_recheck_cooldown():
    entered = asyncio.Event()
    release = asyncio.Event()
    async def first(request):
        entered.set()
        await release.wait()
        return httpx.Response(429)
    async with gateway([first, reply(200)]) as (c, pool, now):
        task = asyncio.create_task(c.post('/v1/chat/completions', json=BODY))
        await entered.wait()
        pool.blocked[1] = 200
        release.set()
        assert (await task).status_code == 429
        assert pool.rotations_429 == 0 and pool.requests == [1, 0]


def test_header_rules_and_retry_after():
    headers = httpx.Headers({'connection': 'x-hidden', 'x-hidden': 'value', 'content-encoding': 'gzip', 'x-normal': 'yes'})
    assert clean_headers(headers, decoded=True) == {'x-normal': 'yes'}
    assert retry_delay('20', 60, 3600) == 20
    assert retry_delay('-1', 60, 3600) == 1
    assert retry_delay('99999', 60, 3600) == 3600
    for value in [None, 'bad', 'nan', 'inf']:
        assert retry_delay(value, 60, 3600) == 60
    future = format_datetime(datetime.now(timezone.utc) + timedelta(seconds=60))
    assert 58 <= retry_delay(future, 60, 3600) <= 60
    naive = 'Wed, 21 Oct 2037 07:28:00'
    assert retry_delay(naive, 60, 3600) == 3600


async def test_default_client_setup_and_shutdown():
    app = create_app(Settings(('direct', 'socks5://proxy.example:1080'), TOKEN))
    async with app.router.lifespan_context(app):
        clients = app.state.pool.clients
        assert len(clients) == 2
        assert not any(client.is_closed for client in clients)
    assert all(client.is_closed for client in clients)
