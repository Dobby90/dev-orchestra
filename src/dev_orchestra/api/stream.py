"""Bounded SSE transport; disconnect affects only the subscription."""

import asyncio
import json
import time

from fastapi import APIRouter, HTTPException, Query, Request
from starlette.concurrency import run_in_threadpool
from starlette.requests import ClientDisconnect
from starlette.responses import StreamingResponse

from dev_orchestra.api.security import COOKIE
from dev_orchestra.application.errors import ApplicationError


class BoundedEventResponse(StreamingResponse):
    media_type = "text/event-stream"
    send_timeout = 5

    async def __call__(self, scope, receive, send):
        async def bounded(message):
            await asyncio.wait_for(send(message), self.send_timeout)

        try:
            await super().__call__(scope, receive, bounded)
        except (TimeoutError, ClientDisconnect):
            # The browser reconnects from its last fully received event ID.
            pass
        finally:
            close = getattr(self.body_iterator, "aclose", None)
            if close is not None:
                await close()


def routes(runs, security):
    router = APIRouter()

    @router.get("/api/runs/{rid}/events", response_class=BoundedEventResponse)
    async def events(rid: str, request: Request, after: int = Query(0, ge=0)):
        await run_in_threadpool(runs.queries.get, rid)
        header = request.headers.get("last-event-id")
        try:
            cursor = int(header) if header is not None else after
        except ValueError:
            raise HTTPException(422, "invalid event cursor") from None
        high = await run_in_threadpool(runs.events.high_water)
        if cursor < 0 or cursor > high:
            raise HTTPException(409, "event cursor beyond history")

        async def generate():
            nonlocal cursor
            heartbeat = time.monotonic()
            try:
                while not await request.is_disconnected():
                    try:
                        await run_in_threadpool(security.authenticate, request.cookies.get(COOKIE))
                    except ApplicationError:
                        return
                    page = await run_in_threadpool(runs.events.stream_page, rid, cursor)
                    for event in page["items"]:
                        # JSON escapes embedded newlines; persisted kind is a single SSE field.
                        kind = event["kind"].replace("\r", "").replace("\n", "")
                        data = json.dumps(event, ensure_ascii=False)
                        yield f"id: {event['id']}\nevent: {kind}\nretry: 3000\ndata: {data}\n\n".encode()
                        cursor = event["id"]
                    if time.monotonic() - heartbeat >= 15:
                        yield b": heartbeat\n\n"
                        heartbeat = time.monotonic()
                    if not page["has_more"]:
                        await asyncio.sleep(0.3)
            finally:
                pass  # No Worker signal, control mutation, or process cleanup here.

        return BoundedEventResponse(
            generate(),
            headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"},
        )

    return router
