"""Test type: structural.

Scope: SDK chat error-envelope mapping, safe diagnostics and unchanged empty
response contract. Payloads are synthetic contract cases, not recorded model
answers or evidence of successful real dialogue. No provider or database calls.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from openai.types.chat import ChatCompletion

from app.ai.adapters.openai_compatible.support.protocol_chat_completions_helpers import (
    convert_chat_response,
)
from app.ai.engine.failure_classifier import FailureClassifier
from app.ai.exceptions import ProviderError, is_retryable
from app.core.i18n import _


class _UnusedUsageExtractor:
    def _extract_usage_tokens(self, _usage: Any) -> tuple[None, None, None]:
        raise AssertionError("Error envelopes and empty responses have no usage")


@pytest.mark.parametrize(
    "shape",
    ["sdk_none_choices", "sdk_empty_choices", "object_without_choices", "dict"],
)
def test_error_envelope_keeps_http_200_and_omits_raw_payload(shape: str) -> None:
    secret = "synthetic-api-key-and-private-provider-message"
    raw_error = {
        "type": "custom_unclassified_type",
        "code": "custom_unclassified_code",
        "message": secret,
        "body": {"token": secret},
    }
    if shape == "dict":
        payload = {"error": raw_error}
    elif shape == "object_without_choices":
        payload = SimpleNamespace(error=raw_error)
    else:
        payload = ChatCompletion.model_construct(
            choices=None if shape == "sdk_none_choices" else [],
            error=raw_error,
        )

    with pytest.raises(ProviderError) as raised:
        convert_chat_response(
            adapter=_UnusedUsageExtractor(), response=payload, model="fixture-model"
        )

    error = raised.value
    assert type(error) is ProviderError
    assert error.status_code == 200
    assert error.upstream_status_code == 200
    assert error.error_code == "provider_error_envelope"
    assert error.provider_code == "openai"
    assert error.model_code == "fixture-model"
    assert error.message == _("ai.request_failed")
    assert error.original_error is None
    assert error.__cause__ is None
    assert is_retryable(error) is False
    kind, details = FailureClassifier.classify_exception(error)
    assert kind == "provider_bad_response"
    assert details == {
        "kind": "provider_bad_response",
        "error": _("ai.request_failed"),
        "status_code": 200,
    }
    projected = repr((str(error), error.to_dict(), details, error.__dict__))
    assert secret not in projected
    assert "custom_unclassified_code" not in projected
    assert "custom_unclassified_type" not in projected


def test_error_envelope_is_checked_before_any_choice_content() -> None:
    payload = ChatCompletion.model_construct(
        choices=[object()], error={"message": "synthetic-private-message"}
    )

    with pytest.raises(ProviderError) as raised:
        convert_chat_response(
            adapter=_UnusedUsageExtractor(), response=payload, model="fixture-model"
        )

    assert raised.value.error_code == "provider_error_envelope"
    assert raised.value.status_code == 200


@pytest.mark.parametrize("choices", [None, []])
@pytest.mark.parametrize("include_null_error", [False, True])
def test_legitimate_empty_response_contract_is_unchanged(
    choices: list | None, include_null_error: bool
) -> None:
    fields: dict[str, Any] = {"choices": choices}
    if include_null_error:
        fields["error"] = None
    payload = ChatCompletion.model_construct(**fields)

    response = convert_chat_response(
        adapter=_UnusedUsageExtractor(), response=payload, model="fixture-model"
    )

    assert response.message.role == "assistant"
    assert response.message.content == ""
    assert response.model == "fixture-model"
    assert response.finish_reason == "stop"
    assert response.metadata == {"protocol_path": "chat_completions"}
    assert response.raw_response is None
    assert response.input_tokens is None
    assert response.output_tokens is None
    assert response.total_tokens is None
