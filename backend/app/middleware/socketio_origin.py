"""Apply the shared browser-origin policy before Engine.IO's synchronous check."""

from starlette.datastructures import Headers
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from app.core.cors import SOCKET_ORIGIN_DECISION_KEY, is_origin_allowed


class SocketIOOriginMiddleware:
    def __init__(self, app: ASGIApp):
        self.app = app
        self.other_asgi_app = getattr(app, "other_asgi_app", app)

    async def __call__(self, scope: Scope, receive: Receive, send: Send):
        if (
            scope["type"] in {"http", "websocket"}
            and scope.get("path", "").rstrip("/") == "/sio"
        ):
            origin = Headers(scope=scope).get("origin")
            if origin is not None:
                if not await is_origin_allowed(origin):
                    if scope["type"] == "websocket":
                        await send({"type": "websocket.close", "code": 1008})
                    else:
                        await JSONResponse(
                            {"detail": "Origin is not allowed"}, status_code=400
                        )(scope, receive, send)
                    return
                scope = {**scope, SOCKET_ORIGIN_DECISION_KEY: (origin, True)}
        await self.app(scope, receive, send)
