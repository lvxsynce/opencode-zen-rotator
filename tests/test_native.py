import asyncio
import base64
import json
from contextlib import asynccontextmanager

import httpx
import pytest

from zen_rotator import native


@asynccontextmanager
async def control(tmp_path, monkeypatch=None):
    config = {"routes": ["direct", "socks5h://proxy.example:1080"],
              "keys": ["test-account-a", "test-account-b"], "token": "t" * 40,
              "port": 19000, "tunnel_port": 0}
    path = tmp_path / "private.json"
    path.write_text(json.dumps(config))
    app = native.create_native_app(path)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://local",
                                     headers={"x-zen-rotator-token": config["token"]}) as c:
            yield c, app, config


async def test_control_auth_plan_and_redaction(tmp_path):
    async with control(tmp_path) as (c, app, config):
        health = await c.get('/health', headers={'x-zen-rotator-token': ''})
        assert health.status_code == 200
        assert all(value not in health.text for value in config['keys'] + config['routes'][1:] + [config['token']])
        assert (await c.get('/plan', headers={'x-zen-rotator-token': ''})).status_code == 401
        plan = (await c.get('/plan')).json()
        assert plan['pairs'] == [[1, 0], [0, 0], [1, 1], [0, 1]]
        assert plan['tunnel_port'] > 0
        assert (await c.post('/complete')).status_code == 204


async def test_report_cooldown_and_success(tmp_path):
    async with control(tmp_path) as (c, app, config):
        assert (await c.post('/report', json={'route': 1, 'key': 0, 'status': 429, 'retry_after': '20000'})).status_code == 200
        assert [1, 0] not in (await c.get('/plan')).json()['pairs']
        await c.post('/report', json={'route': 0, 'key': 1, 'status': 200, 'rotation': True})
        health = (await c.get('/health')).json()
        assert health['current'] == [0, 1]
        assert health['successes'] == 1 and health['rotations_429'] == 1
        assert health['rate_limits_429'] == 1
        assert health['cooldowns'][0]['seconds'] > 19000


@pytest.mark.parametrize('status', [401, 403, 0])
async def test_bad_key_or_transport_cooldown(tmp_path, status):
    async with control(tmp_path) as (c, app, config):
        await c.post('/report', json={'route': 1, 'key': 0, 'status': status})
        pairs = (await c.get('/plan')).json()['pairs']
        if status in [401, 403]:
            assert all(k != 0 for r, k in pairs)
        else:
            assert all(r != 1 for r, k in pairs)


@pytest.mark.parametrize('payload', [
    {'route': -1, 'key': 0, 'status': 200},
    {'route': 0, 'key': 4, 'status': 200},
    {'route': True, 'key': 0, 'status': 200},
    {'route': 0, 'key': 0, 'status': '200'},
])
async def test_report_rejects_invalid_input(tmp_path, payload):
    async with control(tmp_path) as (c, app, config):
        assert (await c.post('/report', json=payload)).status_code == 400


async def connect_request(port, target, auth):
    r, w = await asyncio.open_connection('127.0.0.1', port)
    w.write(f'CONNECT {target} HTTP/1.1\r\n{auth}\r\n\r\n'.encode())
    await w.drain()
    response = await r.readuntil(b'\r\n\r\n')
    return r, w, response


async def test_connect_auth_and_destination_allowlist(tmp_path):
    async with control(tmp_path) as (c, app, config):
        port = app.state.tunnel_port
        _, w, response = await connect_request(port, 'opencode.ai:443', '')
        assert b'407' in response
        w.close()
        auth = 'Proxy-Authorization: Basic ' + base64.b64encode(('0:' + config['token']).encode()).decode()
        _, w, response = await connect_request(port, 'other.example:443', auth)
        assert b'403' in response
        w.close()
        _, w, response = await connect_request(port, 'opencode.ai:443', 'Proxy-Authorization: Basic invalid')
        assert b'502' in response
        w.close()


async def test_connect_is_opaque_bidirectional_tunnel(tmp_path, monkeypatch):
    async def echo(reader, writer):
        try:
            while data := await reader.read(65536):
                writer.write(data)
                await writer.drain()
        finally:
            writer.close()
    server = await asyncio.start_server(echo, '127.0.0.1', 0)
    port = server.sockets[0].getsockname()[1]
    calls = []
    async def fake_route(route, host, target_port):
        calls.append((route, host, target_port))
        return await asyncio.open_connection('127.0.0.1', port)
    monkeypatch.setattr(native, 'open_route', fake_route)
    try:
        async with control(tmp_path) as (c, app, config):
            auth = 'Proxy-Authorization: Basic ' + base64.b64encode(('1:' + config['token']).encode()).decode()
            r, w, response = await connect_request(app.state.tunnel_port, 'opencode.ai:443', auth)
            assert b'200 Connection Established' in response
            opaque = b'\x16\x03\x01\x00\x04TEST'
            w.write(opaque)
            await w.drain()
            assert await r.readexactly(len(opaque)) == opaque
            assert calls == [(config['routes'][1], 'opencode.ai', 443)]
            w.close()
            await w.wait_closed()
    finally:
        server.close()
        await server.wait_closed()


async def test_socks_handshake_uses_remote_dns():
    observations = []
    async def socks(reader, writer):
        try:
            observations.append(await reader.readexactly(3))
            writer.write(b'\x05\x02'); await writer.drain()
            assert await reader.readexactly(1) == b'\x01'
            user = await reader.readexactly((await reader.readexactly(1))[0])
            password = await reader.readexactly((await reader.readexactly(1))[0])
            observations.append((user, password))
            writer.write(b'\x01\x00'); await writer.drain()
            observations.append(await reader.readexactly(4))
            observations.append(await reader.readexactly((await reader.readexactly(1))[0]))
            assert await reader.readexactly(2) == b'\x01\xbb'
            writer.write(b'\x05\x00\x00\x01\x00\x00\x00\x00\x00\x00'); await writer.drain()
        finally:
            writer.close()
    server = await asyncio.start_server(socks, '127.0.0.1', 0)
    try:
        port = server.sockets[0].getsockname()[1]
        _, w = await native.open_route(f'socks5h://user:pass@127.0.0.1:{port}', 'opencode.ai', 443)
        assert observations == [b'\x05\x01\x02', (b'user', b'pass'), b'\x05\x01\x00\x03', b'opencode.ai']
        w.close()
        await w.wait_closed()
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.parametrize('reply', [
    b'\x05\xff',
    b'\x05\x00\x05\x01\x00\x01',
    b'\x05\x00\x05\x00\x00\x09',
])
async def test_socks_rejection_closes_socket(reply):
    closed = asyncio.Event()
    async def fake(reader, writer):
        await reader.readexactly(3)
        writer.write(reply)
        await writer.drain()
        await reader.read()
        writer.close()
        closed.set()
    server = await asyncio.start_server(fake, '127.0.0.1', 0)
    try:
        port = server.sockets[0].getsockname()[1]
        with pytest.raises(ConnectionError):
            await native.open_route(f'socks5h://127.0.0.1:{port}', 'opencode.ai', 443)
        await asyncio.wait_for(closed.wait(), 2)
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.parametrize('address', [b'\x04' + b'\x00' * 16, b'\x03\x03abc'])
async def test_socks_ipv6_and_domain_reply(address):
    async def fake(reader, writer):
        await reader.readexactly(3)
        writer.write(b'\x05\x00'); await writer.drain()
        await reader.readexactly(4)
        await reader.readexactly((await reader.readexactly(1))[0])
        await reader.readexactly(2)
        writer.write(b'\x05\x00\x00' + address + b'\x00\x00'); await writer.drain()
        writer.close()
    server = await asyncio.start_server(fake, '127.0.0.1', 0)
    try:
        port = server.sockets[0].getsockname()[1]
        _, writer = await native.open_route(f'socks5://127.0.0.1:{port}', 'opencode.ai', 443)
        writer.close(); await writer.wait_closed()
    finally:
        server.close(); await server.wait_closed()


async def test_direct_route():
    async def close(reader, writer):
        writer.close()
    server = await asyncio.start_server(close, '127.0.0.1', 0)
    try:
        _, w = await native.open_route('direct', '127.0.0.1', server.sockets[0].getsockname()[1])
        w.close(); await w.wait_closed()
    finally:
        server.close(); await server.wait_closed()


@pytest.mark.parametrize('routes,keys,token', [([], ['key'], 'x' * 40), (['direct'], [], 'x' * 40), (['direct'], ['key'], ''), (['http://proxy.example:80'], ['key'], 'x' * 40)])
def test_bad_native_config(tmp_path, routes, keys, token):
    p = tmp_path / 'private.json'
    p.write_text(json.dumps({'routes': routes, 'keys': keys, 'token': token, 'port': 19000}))
    with pytest.raises(ValueError):
        native.create_native_app(p)
