"""中文: 为任务日志生成脱敏的读取投影，保留重试所需的原始输入。

EN: Project stored task payloads for display while preserving retry inputs.
"""

from __future__ import annotations

import json
import re
from typing import Any

from app.middleware.audit_log import is_sensitive_field, normalize_audit_field_name

REDACTED = "***"
_DIAGNOSTIC_KEYS = {"idempotency_key", "run_key"}
_HEADER_KEYS = {"headers", "request_headers", "response_headers"}
_CONTENT_KEYS = {
    "html_body",
    "text_body",
    "email_body",
    "mail_body",
    "payload_ciphertext",
    "traceback",
    "traceback_internal",
    "error_message_internal",
}
# 中文: 两种邮件签名都将邮件内容放在索引 1 至 3，按位置读取可避免导入任务运行时。
# EN: Both mail signatures place content at positions 1–3. Read these positions
# directly to avoid importing the task runtimes.
_MAIL_TASKS = {
    "app.tasks.email.send_email_task",
    "app.tasks.notification.send_notification_email",
}
_BEARER = re.compile(r"(?i)\bbearer\s+[a-z0-9\-._~+/]+=*")
_AUTHORIZATION = re.compile(
    r"(?i)\b(?:proxy[-_]authorization|authorization)\s*[:=]\s*[^\r\n]+"
)
_USERINFO = re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://)[^/\s?#]*@")
_COOKIE = re.compile(r"(?i)\b(?:set-cookie|cookies?)\s*[:=]\s*[^\r\n]+")
_KEY_PREFIX = re.compile(
    r"(?P<quote>['\"]?)(?P<key>[a-zA-Z][a-zA-Z0-9_-]*)(?P=quote)\s*[:=]\s*"
)
_TEXT_VALUE = re.compile(r"""\"(?:\\.|[^\"\\])*\"|'(?:\\.|[^'\\])*'|[^\s,;{}\[\]"']+""")
_OTP = re.compile(
    r"(?i)\b((?:otp(?:[\s_-]*code)?|one[\s_-]*time[\s_-]*(?:code|password)|verification[\s_-]*code|"
    r"login[\s_-]*code|sms[\s_-]*code|password[\s_-]*reset[\s_-]*code)"
    r"\s*(?:is|[:=])?\s*)['\"]?\d{4,10}\b"
)
_OTP_CJK = re.compile(
    r"((?:验证码|校验码|动态码|一次性密码)\s*(?:为|是|[:：=])?\s*)['\"]?\d{4,10}(?!\d)"
)


def _hide_key(key: object, *, mail_content: bool = False) -> bool:
    normalized = normalize_audit_field_name(key)
    if normalized in _DIAGNOSTIC_KEYS:
        return False
    return (
        is_sensitive_field(normalized)
        or normalized in _CONTENT_KEYS
        or (mail_content and normalized == "subject")
    )


def _redact_text(value: str) -> str:
    if "Traceback (most recent call last):" in value:
        return REDACTED
    # 中文: 字符串内的 JSON 仍可按字段名识别敏感内容。
    # EN: JSON encoded inside a string still has structured credential fields.
    if value.lstrip().startswith(("{", "[")):
        try:
            decoded = json.loads(value)
        except (ValueError, RecursionError):
            pass
        else:
            return json.dumps(redact_task_payload(decoded), ensure_ascii=False)

    value = _AUTHORIZATION.sub(f"Authorization: {REDACTED}", value)
    value = _BEARER.sub(f"Bearer {REDACTED}", value)
    value = _USERINFO.sub(lambda match: f"{match[1]}{REDACTED}@", value)
    value = _COOKIE.sub(f"Cookie: {REDACTED}", value)
    chunks = []
    cursor = 0
    # 中文: 扫描字段名前缀才能识别 message="token=..." 中嵌套的凭证。
    # EN: Scan field prefixes to find nested credentials in text such as
    # message="token=...".
    for match in _KEY_PREFIX.finditer(value):
        if match.start() < cursor or not _hide_key(match["key"]):
            continue
        start = match.end()
        quoted = ""
        if value[start : start + 1] in {"{", "["}:
            try:
                _, length = json.JSONDecoder().raw_decode(value[start:])
            except (ValueError, RecursionError):
                end = len(value)
            else:
                end = start + length
        else:
            raw_value = _TEXT_VALUE.match(value, start)
            if raw_value is None:
                continue
            end = raw_value.end()
            quoted = value[start] if value[start] in {"'", '"'} else ""
        chunks.append(f"{value[cursor:start]}{quoted}{REDACTED}{quoted}")
        cursor = end
    chunks.append(value[cursor:])
    value = "".join(chunks)
    value = _OTP.sub(lambda match: f"{match[1]}{REDACTED}", value)
    return _OTP_CJK.sub(lambda match: f"{match[1]}{REDACTED}", value)


def _redact_headers(value: Any, *, mail_content: bool) -> Any:
    if not isinstance(value, (list, tuple)):
        return redact_task_payload(value, mail_content=mail_content)
    return [
        [item[0], REDACTED]
        if (
            isinstance(item, (list, tuple))
            and len(item) == 2
            and isinstance(item[0], str)
            and _hide_key(item[0])
        )
        else redact_task_payload(item, mail_content=mail_content)
        for item in value
    ]


def redact_task_payload(value: Any, *, mail_content: bool = False) -> Any:
    """中文: 复制任务载荷，遮蔽可识别的敏感内容。

    EN: Copy a JSON-like task payload while hiding recognized secret content.
    """
    if isinstance(value, dict):
        projected = {}
        for key, item in value.items():
            if _hide_key(key, mail_content=mail_content):
                projected[key] = REDACTED
            elif normalize_audit_field_name(key) in _HEADER_KEYS:
                projected[key] = _redact_headers(item, mail_content=mail_content)
            else:
                projected[key] = redact_task_payload(item, mail_content=mail_content)
        return projected
    if isinstance(value, (list, tuple)):
        return [redact_task_payload(item, mail_content=mail_content) for item in value]
    return _redact_text(value) if isinstance(value, str) else value


def redact_task_arguments(
    handler_path: str | None,
    args: list | None,
    kwargs: dict | None,
) -> tuple[list | None, dict | None]:
    """中文: 根据已知邮件签名遮蔽位置参数中的邮件内容。

    EN: Mask mail content at the argument positions used by known handlers.
    """
    is_mail = handler_path in _MAIL_TASKS
    projected_args = (
        [
            REDACTED if is_mail and index in {1, 2, 3} else redact_task_payload(item)
            for index, item in enumerate(args)
        ]
        if args is not None
        else None
    )
    return projected_args, redact_task_payload(kwargs, mail_content=is_mail)


__all__ = ["redact_task_arguments", "redact_task_payload"]
