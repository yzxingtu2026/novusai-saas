"""Test type: behavioral. Check the real tenant JWT and lifecycle validator."""

import pytest

from app.core.security import (
    TENANT_SESSION_EPOCH_CLAIM,
    TOKEN_SCOPE_TENANT_ADMIN,
    TOKEN_SCOPE_TENANT_USER,
    _decode_token_no_blacklist,
    create_token_pair,
    tenant_session_epoch_matches,
)

pytestmark = pytest.mark.behavioral


@pytest.mark.parametrize(
    ("payload", "current_epoch", "accepted"),
    [
        ({}, 0, False),
        ({}, 2, False),
        ({TENANT_SESSION_EPOCH_CLAIM: None}, 0, False),
        ({TENANT_SESSION_EPOCH_CLAIM: False}, 0, False),
        ({TENANT_SESSION_EPOCH_CLAIM: " 2"}, 2, False),
        ({TENANT_SESSION_EPOCH_CLAIM: -1}, 0, False),
        ({TENANT_SESSION_EPOCH_CLAIM: 0}, 0, True),
        ({TENANT_SESSION_EPOCH_CLAIM: "2"}, 2, True),
        ({TENANT_SESSION_EPOCH_CLAIM: 2}, 3, False),
    ],
)
def test_tenant_session_epoch_requires_an_explicit_match(
    payload, current_epoch, accepted
):
    assert tenant_session_epoch_matches(payload, current_epoch) is accepted


@pytest.mark.parametrize("scope", [TOKEN_SCOPE_TENANT_ADMIN, TOKEN_SCOPE_TENANT_USER])
@pytest.mark.parametrize("epoch", [0, 2])
def test_tenant_token_pair_carries_the_epoch_on_each_token(scope, epoch):
    pair = create_token_pair(
        subject=19,
        scope=scope,
        extra_claims={"tenant_id": 7, TENANT_SESSION_EPOCH_CLAIM: epoch},
    )

    for token_type in ("access", "refresh"):
        claims = _decode_token_no_blacklist(pair[f"{token_type}_token"])
        assert (claims["sub"], claims["scope"], claims["type"]) == (
            "19",
            scope,
            token_type,
        )
        assert claims[TENANT_SESSION_EPOCH_CLAIM] == epoch
        assert tenant_session_epoch_matches(claims, epoch)
        assert not tenant_session_epoch_matches(claims, epoch + 1)
