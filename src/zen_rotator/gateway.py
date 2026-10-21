"""Forward native requests, switching routes before response streaming only."""
import asyncio
import hmac
import json
import logging
import math
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

log = logging.getLogger("zen_rotator")
UPSTREAM = "https://opencode.ai/zen/v1/"
DROP = {"host", "connection", "keep-alive", "proxy-authenticate",
        "proxy-authorization", "te", "trailer", "transfer-encoding",
        "upgrade", "content-length", "x-zen-rotator-token"}


def clean_headers(headers, decoded=False):
    excluded = DROP | {p.strip().lower() for p in headers.get("connection", "").split(",")}
    if decoded:
        excluded = excluded | {"content-encoding"}
    return {k: v for k, v in headers.items() if k.lower() not in excluded}


def retry_delay(value, default, maximum):
    try:
        delay = float(value)
        if not math.isfinite(delay):
            raise ValueError()
    except (ValueError, TypeError):
        try:
            date = parsedate_to_datetime(value)
            if date.tzinfo is None:
                date = date.replace(tzinfo=timezone.utc)
            delay = (date - datetime.now(timezone.utc)).total_seconds()
        except (ValueError, TypeError, OverflowError, AttributeError):
            delay = default
    return max(1, min(maximum, delay))


def is_daily_free_limit(status, body):
    """Detect Zen's daily free-quota message, which may not use HTTP 429."""
    if status != 200:
        return False
    text = body.decode("utf-8", errors="replace").lower()
    return "you've used today's free messages" in text or "you have used today's free messages" in text


class Pool:
    def __init__(self, settings, clients, clock):
        self.settings, self.clients, self.clock = settings, clients, clock
        self.current = 0
        self.blocked = [0.0] * len(clients)
        self.requests = [0] * len(clients)
        self.retries = self.rotations_429 = self.rate_limits_429 = self.daily_free_limits = self.active = 0

    def order(self):
        return [(self.current + n) % len(self.clients) for n in range(len(self.clients))]

    def available(self, index):
        return self.blocked[index] <= self.clock()

    def limited(self, index, retry_after, daily_free=False):
        if daily_free:
            self.daily_free_limits += 1
        else:
            self.rate_limits_429 += 1
        delay = retry_delay(retry_after, self.settings.cooldown, self.settings.max_cooldown)
        self.blocked[index] = max(self.blocked[index], self.clock() + delay)
        self.current = (index + 1) % len(self.clients)

    def health(self):
        return {"ok": True, "requests": self.requests, "current": self.current,
                "retries": self.retries, "rotations_429": self.rotations_429,
                "rate_limits_429": self.rate_limits_429,
                "daily_free_limits": self.daily_free_limits, "active": self.active,
                "cooldown_seconds": [max(0, math.ceil(t - self.clock())) for t in self.blocked]}


def create_app(settings, client_factory=None, clock=time.monotonic):
    def default_client(route):
        return httpx.AsyncClient(proxy=None if route == "direct" else route, trust_env=False,
                                 timeout=httpx.Timeout(600, connect=15), follow_redirects=False)

    @asynccontextmanager
    async def lifespan(app):
        clients = []
        try:
            for route in settings.routes:
                clients.append((client_factory or default_client)(route))
            app.state.pool = Pool(settings, clients, clock)
            yield
        finally:
            await asyncio.gather(*(client.aclose() for client in clients))

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    @app.get("/health")
    async def health():
        return app.state.pool.health()

    @app.post("/v1/{path:path}")
    async def forward(path: str, request: Request):
        supplied = request.headers.get("x-zen-rotator-token", "")
        if not hmac.compare_digest(supplied, settings.token):
            return JSONResponse({"error": "Local access token required"}, status_code=401)
        if path not in {"chat/completions", "responses", "messages"}:
            return JSONResponse({"error": "Unsupported endpoint"}, status_code=404)
        if request.headers.get("content-encoding", "identity").lower() != "identity":
            return JSONResponse({"error": "Send uncompressed JSON"}, status_code=415)
        chunks, size = [], 0
        async for chunk in request.stream():
            size += len(chunk)
            if size > settings.max_body_bytes:
                return JSONResponse({"error": "Request body is too large"}, status_code=413)
            chunks.append(chunk)
        body = b"".join(chunks)
        try:
            model = json.loads(body).get("model")
        except (ValueError, AttributeError):
            return JSONResponse({"error": "Invalid JSON"}, status_code=400)
        if not isinstance(model, str) or not model.endswith("-free"):
            return JSONResponse({"error": "Only free models are accepted"}, status_code=403)
        headers = clean_headers(request.headers)
        headers["accept-encoding"] = "identity"
        pool = app.state.pool
        pool.active += 1
        streaming = False
        try:
            previous = None
            for index in pool.order():
                if not pool.available(index):
                    continue
                if previous:
                    pool.retries += 1
                    if previous == "429":
                        pool.rotations_429 += 1
                previous = None
                pool.requests[index] += 1
                client = pool.clients[index]
                try:
                    req = client.build_request("POST", UPSTREAM + path, headers=headers,
                                               content=body, params=request.query_params.multi_items())
                    upstream = await client.send(req, stream=True)
                except httpx.RequestError:
                    pool.blocked[index] = pool.clock() + settings.transport_cooldown
                    pool.current = (index + 1) % len(pool.clients)
                    previous = "transport"
                    continue
                log.info("Zen route_index=%d status=%d", index, upstream.status_code)
                daily_free = False
                if upstream.status_code == 429 or (upstream.status_code == 200 and
                        "text/event-stream" not in upstream.headers.get("content-type", "")):
                    if upstream.status_code == 200:
                        try:
                            body = await upstream.aread()
                        finally:
                            await upstream.aclose()
                        if not is_daily_free_limit(200, body):
                            response_headers = clean_headers(upstream.headers, decoded=True)
                            response_headers["x-zen-route-index"] = str(index)
                            return Response(body, status_code=200,
                                            headers=response_headers)
                        daily_free = True
                        log.info("Zen route_index=%d daily_free_limit=true", index)
                    else:
                        await upstream.aread()
                    pool.limited(index, upstream.headers.get("retry-after"), daily_free=daily_free)
                    if not upstream.is_closed:
                        await upstream.aclose()
                    previous = "daily" if daily_free else "429"
                    continue
                if upstream.is_success and pool.available(index):
                    pool.current = index
                response_headers = clean_headers(upstream.headers, decoded=True)
                response_headers["x-zen-route-index"] = str(index)
                if "text/event-stream" in upstream.headers.get("content-type", ""):
                    async def stream(response=upstream):
                        try:
                            async for chunk in response.aiter_bytes():
                                yield chunk
                        finally:
                            pool.active -= 1
                            await response.aclose()
                    streaming = True
                    return StreamingResponse(stream(), status_code=upstream.status_code, headers=response_headers)
                try:
                    raw = await upstream.aread()
                finally:
                    await upstream.aclose()
                return Response(raw, status_code=upstream.status_code, headers=response_headers)
            if previous == "transport":
                return JSONResponse({"error": {"type": "transport_error", "message": "Routes unavailable"}}, status_code=502)
            delay = max(1, math.ceil(min(pool.blocked) - pool.clock()))
            return JSONResponse({"error": {"type": "FreeUsageLimitError",
                                           "message": "All routes are cooling down. Try again later."}},
                                status_code=429, headers={"retry-after": str(delay)})
        finally:
            if not streaming:
                pool.active -= 1

    return app
