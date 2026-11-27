"""用户上传规则路由匹配 / User attachment upload-rules routing.

Test type: structural.
"""

from __future__ import annotations

import pytest
from starlette.routing import Match

from app.api.user.attachments import get_attachment, get_upload_rules, router


@pytest.mark.parametrize(
    ("path", "endpoint"),
    [
        ("/attachments/upload-rules", get_upload_rules),
        ("/attachments/7", get_attachment),
    ],
)
def test_user_attachment_paths_match_their_endpoints(path, endpoint) -> None:
    scope = {"type": "http", "path": path, "method": "GET"}
    matched = next(
        route for route in router.routes if route.matches(scope)[0] == Match.FULL
    )
    assert matched.endpoint is endpoint
