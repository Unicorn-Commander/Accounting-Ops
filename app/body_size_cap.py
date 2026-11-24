"""ASGI body-size cap middleware (AO-UP-5).

The upload cap (``document_upload_max_bytes``) used to be checked only in the
upload routes, AFTER Starlette had already spooled the whole body into memory:
a multi-GB upload exhausted memory before the 413 could fire. The FastAPI
constructor's ``max_body_size`` kwarg was the intended fix, but on the locked
pins (fastapi 0.136.3 / starlette 1.3.1, installed ``--frozen`` from uv.lock)
FastAPI swallows it as an **extra and installs nothing: a 1000-byte POST to
``FastAPI(max_body_size=10)`` returns 200. So the cap is enforced here, in a
pure-ASGI middleware that wraps ``receive`` and ``send``:

* a declared ``Content-Length`` over the cap is rejected with 413 at once,
  BEFORE the downstream app runs and before any body is read (fail fast);
* otherwise the running total of streamed body bytes is counted as the
  downstream app reads them, and the moment it exceeds the cap the middleware
  sends the 413 itself and RAISES the private abort exception immediately
  (R5-P2-1): the chunk that crosses the cap is never returned to the app, so
  a handler never runs on an over-cap body and no handler side effects happen
  on partial data. For a chunked over-cap body the app IS entered (Starlette
  hands it a request), but it is aborted inside the body read; any later
  ``receive()`` returns ``http.disconnect``.

Because Starlette's outermost ``ServerErrorMiddleware`` would otherwise catch
the abort exception and render a 500 on top of the 413, the middleware also
wraps ``send``: once the 413 has gone out, any response the app (or its error
middleware) still tries to start is dropped. The client sees exactly one
response: the 413.

Registered on ``app.main.app`` (see app/main.py); the cap shares the same env
var the web edge reads (client_max_body_size), so edge + app stay in lockstep.
"""

from __future__ import annotations

from typing import Any

_CAP_BODY = b"Request body exceeds the maximum allowed size."


class _BodyCapExceeded(Exception):
    """Raised by the wrapped receive() once the body crossed the cap.

    Private to this module: it is the middleware's own abort signal, raised by
    the wrapped ``receive()`` the moment the running total crosses the cap (so
    the crossing chunk is never returned to the app) and caught in ``__call__``
    so the app never sees it. The app's request is abandoned and the client got
    the 413 from the middleware.

    Starlette's outermost ``ServerErrorMiddleware`` would otherwise catch this
    and turn it into a 500, so ``__call__`` also drops any response the app
    (or its error middleware) starts after the 413 has gone out (see the
    ``capped_send`` below). The 413 is the only thing the client sees.
    """


async def _send_413(send: Any) -> None:
    await send(
        {
            "type": "http.response.start",
            "status": 413,
            "headers": [
                (b"content-type", b"text/plain; charset=utf-8"),
                (b"content-length", str(len(_CAP_BODY)).encode("ascii")),
            ],
        }
    )
    await send({"type": "http.response.body", "body": _CAP_BODY, "more_body": False})


def _carries_cap_abort(exc: BaseException) -> bool:
    """True if ``exc`` is (or nests) the private abort exception.

    The abort can surface as a bare ``_BodyCapExceeded`` or wrapped in one or
    more ``ExceptionGroup`` layers (the BaseHTTPMiddleware task group and the
    router's ServerErrorMiddleware each wrap it). Search the group tree.
    """
    if isinstance(exc, _BodyCapExceeded):
        return True
    if isinstance(exc, BaseExceptionGroup):
        return any(_carries_cap_abort(sub) for sub in exc.exceptions)
    return False


def _declared_length(scope: dict[str, Any]) -> int | None:
    for key, value in scope.get("headers") or []:
        if key.lower() == b"content-length":
            try:
                return int(value)
            except (TypeError, ValueError):
                return None
    return None


class BodySizeCapMiddleware:
    """Reject request bodies over ``max_bytes`` with 413, before they buffer.

    A declared ``Content-Length`` over the cap is rejected up front, at once,
    before the app runs and before any body is read. Otherwise the streamed
    ``http.request`` body bytes are counted as the downstream app reads them,
    and the moment the running total crosses the cap the middleware sends the
    413 itself and RAISES the abort exception at once (R5-P2-1): the chunk
    that crosses the cap is never returned to the app, so a handler never runs
    on an over-cap body and no handler side effects run on partial data. For a
    body without a declared length (chunked) the app IS entered (Starlette
    hands it a request) but is aborted inside the body read; any later
    ``receive()`` reports disconnect. Any 500 the app's error middleware would
    emit on top of the 413 is dropped by the wrapped send.
    """

    def __init__(self, app: Any, *, max_bytes: int) -> None:
        self._app = app
        self._max_bytes = int(max_bytes)

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self._app(scope, receive, send)
            return

        content_length = _declared_length(scope)
        if content_length is not None and content_length > self._max_bytes:
            # Fail fast: reject at once, before the app runs and before any
            # body is read (draining the over-cap body here would buffer the
            # very bytes this middleware exists to keep out of memory).
            await _send_413(send)
            return

        state = {"total": 0, "exceeded": False, "sent_413": False}

        async def capped_receive() -> dict[str, Any]:
            if state["exceeded"]:
                # The body crossed the cap on an earlier read: the 413 is on
                # the wire and the app must be stopped. Report disconnect so
                # any other reader stops too (the first raise already aborted
                # the app's own read loop).
                return {"type": "http.disconnect"}
            message = await receive()
            if message["type"] != "http.request":
                return message
            state["total"] += len(message.get("body", b"") or b"")
            if state["total"] > self._max_bytes:
                state["exceeded"] = True
                # The body crossed the cap: send the 413 ourselves and RAISE
                # at once (R5-P2-1). The chunk that crossed the cap is never
                # returned to the app, so a handler never runs on an over-cap
                # body and no handler side effects run on partial data.
                state["sent_413"] = True
                await _send_413(send)
                raise _BodyCapExceeded()
            return message

        async def capped_send(message: dict[str, Any]) -> None:
            # Once the 413 is on the wire, the request is over: drop any
            # response the app (or its error middleware) still tries to start,
            # so the client sees exactly one response (the 413) and never a
            # spurious 500 on top of it.
            if state["sent_413"]:
                return
            await send(message)

        try:
            await self._app(scope, capped_receive, capped_send)
        except _BodyCapExceeded:
            # The app was aborted after the body crossed the cap; the 413 was
            # already sent, so nothing more goes to the client.
            return
        except BaseException as exc:  # noqa: BLE001 - must catch the group form too
            # SecurityHeadersMiddleware is a BaseHTTPMiddleware: its anyio task
            # group re-raises our abort as an ExceptionGroup (sometimes nested,
            # wrapped by the router's ServerErrorMiddleware). If the exception
            # -- or anything inside its group -- is _BodyCapExceeded and the
            # 413 is on the wire, swallow it: the client already got the 413.
            # Anything else is re-raised so a real error is never hidden.
            if state["sent_413"] and _carries_cap_abort(exc):
                return
            raise
        # Under the cap: the app's response passed through capped_send
        # untouched. On an over-cap abort the 413 was already sent and any
        # spurious 500 was dropped by capped_send.
