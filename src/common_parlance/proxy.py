"""Transparent HTTP proxy that forwards requests to a local model engine."""

import asyncio
import logging
import uuid
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request, Response
from fastapi.responses import StreamingResponse
from starlette.background import BackgroundTask

logger = logging.getLogger(__name__)

CHAT_PATHS = {
    "/v1/chat/completions",
    "/api/chat",
    "/api/generate",
}

_HOP_BY_HOP = frozenset(
    {
        "host",
        "content-length",
        "transfer-encoding",
        "content-encoding",
    }
)


def is_chat_endpoint(path: str) -> bool:
    """Whether the request path is a chat/completion endpoint we should log.

    Suffix match, not arbitrary substring: each CHAT_PATHS entry starts with
    "/", so endswith requires a path-segment boundary. This keeps support for
    reverse-proxy prefixes ("/prefix/v1/chat/completions") while rejecting a
    chat path that is merely embedded — trailing garbage
    ("/v1/chat/completions/extra") or, once the query is stripped by the
    caller, a value smuggled into the query string. Over-matching here means
    silently logging a non-chat request to the staging DB.

    A trailing slash is normalized away first so a client that posts to
    "/v1/chat/completions/" (FastAPI/redirect_slashes style) is still logged.
    """
    path = path.rstrip("/")
    return any(path.endswith(chat_path) for chat_path in CHAT_PATHS)


def _forward_headers(raw_headers: list[tuple[str, str]]) -> dict[str, str]:
    """Filter out hop-by-hop headers."""
    return {k: v for k, v in raw_headers if k.lower() not in _HOP_BY_HOP}


def _log_exchange_sync(
    db_path: str, session_id: str, request_json: str, response_json: str
) -> str:
    """Log an exchange using a short-lived SQLite connection.

    Each call creates and closes its own connection. This is safe for
    concurrent use from asyncio.to_thread() because no connection is
    shared across threads. WAL mode allows concurrent readers/writers,
    and busy_timeout handles write contention gracefully.

    The ~1ms connection overhead per call is negligible compared to
    model inference time (seconds).
    """
    from common_parlance.db import ConversationStore

    with ConversationStore(db_path) as store:
        return store.log_exchange(session_id, request_json, response_json)


def create_app(
    upstream: str,
    db_path: str | None = None,
) -> FastAPI:
    """Create the proxy FastAPI application.

    Args:
        upstream: URL of the local model engine to proxy to.
        db_path: Path to SQLite database for logging exchanges.
            If None, logging is disabled. Each log call creates a
            short-lived connection (no shared state across threads).
    """
    client = httpx.AsyncClient(
        base_url=upstream,
        timeout=httpx.Timeout(connect=5.0, read=300.0, write=10.0, pool=5.0),
        limits=httpx.Limits(
            max_connections=100,
            max_keepalive_connections=20,
            keepalive_expiry=30,
        ),
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
        yield
        await client.aclose()

    app = FastAPI(
        title="Common Parlance Proxy",
        docs_url=None,
        redoc_url=None,
        lifespan=lifespan,
    )

    @app.api_route(
        "/{path:path}",
        methods=["GET", "HEAD", "POST", "PUT", "DELETE", "PATCH"],
    )
    async def proxy(request: Request, path: str) -> Response:
        clean_path = f"/{path}"
        full_path = clean_path
        if request.url.query:
            full_path = f"{full_path}?{request.url.query}"

        # Match on the path only — a chat path smuggled into the query string
        # must not flip a request into a logged chat exchange.
        is_chat = is_chat_endpoint(clean_path)
        body = await request.body()
        headers = _forward_headers(list(request.headers.items()))

        # Check if client wants streaming (SSE)
        is_streaming = request.headers.get("accept") == "text/event-stream"

        try:
            if is_streaming:
                # Always stream to the client live so time-to-first-token is
                # preserved (this proxy sits in front of the user's LLM). When
                # the exchange also needs logging, TEE the bytes into a buffer
                # and log from it in a background task after the stream closes —
                # logging must never gate first-byte latency. (Previously a
                # chat+logging request fell through to the fully-buffered path,
                # which withheld the whole response until generation finished.)
                req = client.build_request(
                    method=request.method,
                    url=full_path,
                    content=body,
                    headers=headers,
                )
                upstream_resp = await client.send(req, stream=True)
                resp_headers = _forward_headers(list(upstream_resp.headers.items()))
                media_type = upstream_resp.headers.get("content-type")
                should_log = (
                    is_chat and db_path is not None and upstream_resp.is_success
                )

                if not should_log:
                    return StreamingResponse(
                        upstream_resp.aiter_bytes(),
                        status_code=upstream_resp.status_code,
                        headers=resp_headers,
                        media_type=media_type,
                        background=BackgroundTask(upstream_resp.aclose),
                    )

                captured: list[bytes] = []

                async def _tee() -> AsyncGenerator[bytes]:
                    async for chunk in upstream_resp.aiter_bytes():
                        captured.append(chunk)
                        yield chunk

                async def _close_and_log() -> None:
                    await upstream_resp.aclose()
                    response_text = b"".join(captured).decode("utf-8", errors="replace")
                    try:
                        exchange_id = await asyncio.to_thread(
                            _log_exchange_sync,
                            db_path,
                            str(uuid.uuid4()),
                            body.decode("utf-8", errors="replace"),
                            response_text,
                        )
                        logger.info("Logged streamed exchange %s", exchange_id)
                    except Exception as exc:
                        logger.warning("Failed to log exchange: %s", type(exc).__name__)

                return StreamingResponse(
                    _tee(),
                    status_code=upstream_resp.status_code,
                    headers=resp_headers,
                    media_type=media_type,
                    background=BackgroundTask(_close_and_log),
                )

            # Non-streaming: buffer the response (logged below if it's a chat).
            upstream_resp = await client.request(
                method=request.method,
                url=full_path,
                content=body,
                headers=headers,
            )
        except httpx.ConnectError:
            logger.error("Cannot connect to upstream: %s", upstream)
            return Response(
                content=f"Cannot connect to upstream model at {upstream}",
                status_code=502,
            )
        except (httpx.TimeoutException, httpx.RemoteProtocolError) as exc:
            logger.error("Upstream error: %s", type(exc).__name__)
            return Response(
                content=f"Upstream error: {type(exc).__name__}",
                status_code=502,
            )

        # Log chat exchanges off the event loop via a thread pool.
        # Each call creates its own short-lived SQLite connection —
        # no shared connection state, no thread safety concerns.
        if is_chat and upstream_resp.is_success and db_path is not None:
            try:
                exchange_id = await asyncio.to_thread(
                    _log_exchange_sync,
                    db_path,
                    str(uuid.uuid4()),
                    body.decode("utf-8", errors="replace"),
                    upstream_resp.text,
                )
                logger.info("Logged exchange %s", exchange_id)
            except Exception as exc:
                # Log error type only — exc_info could leak conversation PII
                logger.warning("Failed to log exchange: %s", type(exc).__name__)

        return Response(
            content=upstream_resp.content,
            status_code=upstream_resp.status_code,
            headers=_forward_headers(list(upstream_resp.headers.items())),
        )

    return app
