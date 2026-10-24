"""Local route control and opaque CONNECT tunnels; never terminate upstream TLS."""
import asyncio
import base64
import hmac
import json
import struct
import time
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import unquote, urlsplit

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

from .gateway import retry_delay


async def open_route(route, host, port):
    if route == "direct":
        return await asyncio.open_connection(host, port)
    proxy = urlsplit(route)
    reader, writer = await asyncio.open_connection(proxy.hostname, proxy.port)
    try:
        if proxy.scheme not in {"socks5", "socks5h"}:
            raise ValueError("Native tunnels require SOCKS5 routes")
        username = unquote(proxy.username or "").encode()
        password = unquote(proxy.password or "").encode()
        writer.write(b"\x05\x01" + (b"\x02" if username else b"\x00"))
        await writer.drain()
        method = await reader.readexactly(2)
        if method != b"\x05" + (b"\x02" if username else b"\x00"):
            raise ConnectionError("SOCKS method rejected")
        if username:
            if len(username) > 255 or len(password) > 255:
                raise ValueError("SOCKS credentials too long")
            writer.write(b"\x01" + bytes([len(username)]) + username + bytes([len(password)]) + password)
            await writer.drain()
            if await reader.readexactly(2) != b"\x01\x00":
                raise ConnectionError("SOCKS authentication rejected")
        domain = host.encode("idna")
        writer.write(b"\x05\x01\x00\x03" + bytes([len(domain)]) + domain + struct.pack("!H", port))
        await writer.drain()
        reply = await reader.readexactly(4)
        if reply[0] != 5 or reply[1] != 0:
            raise ConnectionError("SOCKS connection rejected")
        if reply[3] == 1:
            await reader.readexactly(4)
        elif reply[3] == 4:
            await reader.readexactly(16)
        elif reply[3] == 3:
            await reader.readexactly((await reader.readexactly(1))[0])
        else:
            raise ConnectionError("Invalid SOCKS address")
        await reader.readexactly(2)
        return reader, writer
    except BaseException:
        writer.close()
        await writer.wait_closed()
        raise


def create_native_app(config_path):
    config = json.loads(Path(config_path).read_text())
    routes, keys, token = config["routes"], config["keys"], config["token"]
    if (not isinstance(routes, list) or not routes or not isinstance(keys, list) or not keys
            or not all(isinstance(key, str) and key for key in keys)
            or not isinstance(token, str) or len(token) < 32 or len(set(keys)) != len(keys)):
        raise ValueError("Invalid private native configuration")
    for route in routes:
        if route != "direct" and urlsplit(route).scheme not in {"socks5", "socks5h"}:
            raise ValueError("Native tunnels require SOCKS5 routes")
    blocked = {}
    state = {"current": [len(routes) - 1, 0], "requests": 0, "rotations_429": 0,
             "rate_limits_429": 0, "active_tunnels": 0, "successes": 0,
             "transport_errors": 0}
    tasks = set()

    async def tunnel(reader, writer):
        upstream = None
        active = False
        try:
            raw = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 10)
            if len(raw) > 8192:
                raise ValueError("Oversized request")
            lines = raw.decode("ascii").split("\r\n")
            method, target, _ = lines[0].split(" ", 2)
            headers = dict(line.split(":", 1) for line in lines[1:] if ":" in line)
            auth = next((v.strip() for k, v in headers.items() if k.lower() == "proxy-authorization"), "")
            if not auth.startswith("Basic "):
                writer.write(b'HTTP/1.1 407 Proxy Authentication Required\r\nProxy-Authenticate: Basic realm="Zen"\r\nContent-Length: 0\r\n\r\n')
                await writer.drain()
                return
            user, supplied = base64.b64decode(auth[6:], validate=True).decode().split(":", 1)
            index = int(user)
            if not hmac.compare_digest(supplied, token) or not 0 <= index < len(routes):
                raise ValueError("Invalid tunnel credentials")
            if method != "CONNECT" or target.lower() != "opencode.ai:443":
                writer.write(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n")
                await writer.drain()
                return
            ur, upstream = await asyncio.wait_for(open_route(routes[index], "opencode.ai", 443), 15)
            writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            await writer.drain()
            state["active_tunnels"] += 1
            active = True

            async def pump(src, dst):
                while True:
                    data = await src.read(65536)
                    if not data:
                        return
                    dst.write(data)
                    await dst.drain()

            pipes = [asyncio.create_task(pump(reader, upstream)), asyncio.create_task(pump(ur, writer))]
            try:
                await asyncio.wait(pipes, return_when=asyncio.FIRST_COMPLETED)
            finally:
                for task in pipes:
                    task.cancel()
                await asyncio.gather(*pipes, return_exceptions=True)
        except (OSError, ValueError, UnicodeError, asyncio.IncompleteReadError, asyncio.LimitOverrunError, TimeoutError):
            if not active:
                writer.write(b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\n\r\n")
                try:
                    await writer.drain()
                except OSError:
                    pass
        finally:
            if active:
                state["active_tunnels"] -= 1
            writer.close()
            if upstream:
                upstream.close()

    def accept(reader, writer):
        task = asyncio.create_task(tunnel(reader, writer))
        tasks.add(task)
        task.add_done_callback(tasks.discard)

    @asynccontextmanager
    async def lifespan(app):
        server = await asyncio.start_server(accept, "127.0.0.1", config.get("tunnel_port", config["port"] + 1))
        app.state.tunnel_port = server.sockets[0].getsockname()[1]
        try:
            yield
        finally:
            server.close()
            await server.wait_closed()
            for task in list(tasks):
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware("http")
    async def authenticate(request, call_next):
        if request.url.path != "/health" and not hmac.compare_digest(request.headers.get("x-zen-rotator-token", ""), token):
            return JSONResponse({"error": "Local token required"}, status_code=401)
        return await call_next(request)

    @app.get("/health")
    async def health():
        return {"ok": True, "mode": "native-connect", "routes": len(routes), "keys": len(keys), **state,
                "cooldowns": [{"route": r, "key": k, "seconds": max(0, int(t - time.monotonic()))}
                              for (r, k), t in blocked.items() if t > time.monotonic()]}

    @app.get("/plan")
    async def plan():
        r0, k0 = state["current"]
        pairs = [[(r0 + r) % len(routes), (k0 + k) % len(keys)]
                 for k in range(len(keys)) for r in range(len(routes))]
        return {"pairs": [p for p in pairs if blocked.get(tuple(p), 0) <= time.monotonic()],
                "tunnel_port": app.state.tunnel_port}

    @app.post("/report")
    async def report(request: Request):
        data = await request.json()
        r, k, status = data.get("route"), data.get("key"), data.get("status")
        if type(r) is not int or type(k) is not int or not 0 <= r < len(routes) or not 0 <= k < len(keys):
            return JSONResponse({"error": "Invalid pair"}, status_code=400)
        if type(status) is not int or status not in range(600):
            return JSONResponse({"error": "Invalid status"}, status_code=400)
        state["requests"] += 1
        if data.get("rotation") is True:
            state["rotations_429"] += 1
        if status == 429:
            state["rate_limits_429"] += 1
            blocked[r, k] = time.monotonic() + retry_delay(data.get("retry_after"), 60, 86400)
        elif status in {401, 403}:
            for ri in range(len(routes)):
                blocked[ri, k] = time.monotonic() + 60
        elif status == 0:
            state["transport_errors"] += 1
            for ki in range(len(keys)):
                blocked[r, ki] = time.monotonic() + 30
        elif 200 <= status < 300:
            state["successes"] += 1
            state["current"] = [r, k]
        return {"ok": True}

    @app.post("/complete")
    async def complete():
        return Response(status_code=204)

    return app


def main():
    import argparse
    import uvicorn
    parser = argparse.ArgumentParser(description="Native OpenCode Zen tunnel control")
    parser.add_argument("--config", required=True)
    args = parser.parse_args()
    config = json.loads(Path(args.config).read_text())
    uvicorn.run(create_native_app(args.config), host="127.0.0.1", port=config["port"], access_log=False)


if __name__ == "__main__":
    main()
