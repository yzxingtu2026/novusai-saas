"""Construct requests for behavior tests of permission-decorated routes."""

from starlette.requests import Request


def make_authenticated_request(
    *permissions: str,
    method: str = "GET",
    path: str = "/",
    query_string: str = "",
) -> Request:
    """Build a real request for direct route-function behavioral tests.

    Permission decorators intentionally fail closed when a route is invoked
    outside FastAPI. Keeping the request state in one helper prevents tests
    from accidentally reintroducing MagicMock-based permission bypasses.
    """

    return Request(
        {
            "type": "http",
            "method": method,
            "path": path,
            "headers": [],
            "query_string": query_string.encode("ascii"),
            "state": {"user_permissions": set(permissions)},
        }
    )
