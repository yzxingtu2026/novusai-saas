"""中文: 使用虚构 SMTP 边界验证提交安全性。 EN: Verify submission safety through a fictional SMTP boundary."""

import smtplib
import socket
from email import message_from_string

import pytest

from app.services.common import email_service
from app.services.common.email_service import (
    EmailMessage,
    EmailService,
    SmtpConfig,
    send_prepared_email,
)

pytestmark = pytest.mark.behavioral


@pytest.fixture(autouse=True)
def reject_unfaked_network(monkeypatch):
    def reject(*_args, **_kwargs):
        raise AssertionError("SMTP must use the fictional transport")

    monkeypatch.setattr(socket, "create_connection", reject)
    monkeypatch.setattr(socket, "getaddrinfo", reject)


class FictionalSMTP:
    def __init__(self, failures=None):
        self.failures = failures or {}
        self.events = []
        self.submissions = []
        self.connections = []

    def _phase(self, phase):
        self.events.append(phase)
        if phase in self.failures:
            raise self.failures[phase]

    def connect(self, transport, host, port, **kwargs):
        self.connections.append((transport, host, port, kwargs))
        self._phase("connect")
        return self

    def starttls(self, *, context):
        assert context.verify_mode.name == "CERT_REQUIRED"
        self._phase("tls")

    def login(self, username, password):
        assert (username, password) == ("fictional-user", "fictional-password")
        self._phase("auth")

    def sendmail(self, sender, recipients, message):
        self.submissions.append((sender, recipients, message))
        self._phase("sendmail")
        return {}

    def quit(self):
        self._phase("quit")

    def close(self):
        self._phase("close")


def _arrange(monkeypatch, *, failures=None, encryption="tls", credentials=True):
    smtp = FictionalSMTP(failures)
    monkeypatch.setattr(
        email_service.smtplib,
        "SMTP",
        lambda host, port, **kwargs: smtp.connect("plain", host, port, **kwargs),
    )
    monkeypatch.setattr(
        email_service.smtplib,
        "SMTP_SSL",
        lambda host, port, **kwargs: smtp.connect("ssl", host, port, **kwargs),
    )
    config = SmtpConfig(
        host="smtp.example.invalid",
        port=465 if encryption == "ssl" else 587,
        encryption=encryption,
        username="fictional-user" if credentials else "",
        password="fictional-password" if credentials else "",
        from_address="sender@example.invalid",
        from_name="Fictional sender",
        enabled=True,
    )
    recipients = ["recipient@example.invalid"]
    mime = EmailService._build_mime_message(
        EmailMessage(
            to=recipients, subject="Fictional subject", text_body="Frozen body"
        ),
        config,
    )

    def before_submit():
        smtp._phase("commit_hook")

    return smtp, lambda: send_prepared_email(config, mime, recipients, before_submit)


def _assert_result(result, *, state, retryable, success=False):
    assert result.success is success
    assert result.submission_state == state
    assert result.retryable is retryable
    assert result.recipients == ["recipient@example.invalid"]
    assert result.error is None


@pytest.mark.parametrize(
    ("encryption", "credentials", "expected"),
    [
        ("tls", True, ["connect", "tls", "auth", "commit_hook", "sendmail", "quit"]),
        ("ssl", True, ["connect", "auth", "commit_hook", "sendmail", "quit"]),
        ("none", False, ["connect", "commit_hook", "sendmail", "quit"]),
    ],
)
def test_commit_hook_runs_after_connection_security_before_smtp_submission(
    monkeypatch, encryption, credentials, expected
):
    smtp, send = _arrange(monkeypatch, encryption=encryption, credentials=credentials)

    result = send()

    _assert_result(result, state="accepted", retryable=False, success=True)
    assert result.message == "sent"
    assert smtp.events == expected
    transport, host, port, options = smtp.connections[0]
    assert (transport, host, port) == (
        "ssl" if encryption == "ssl" else "plain",
        "smtp.example.invalid",
        465 if encryption == "ssl" else 587,
    )
    assert options["timeout"] == 30
    if encryption == "ssl":
        assert options["context"].verify_mode.name == "CERT_REQUIRED"
    assert len(smtp.submissions) == 1
    sender, recipients, raw_message = smtp.submissions[0]
    assert (sender, recipients) == (
        "sender@example.invalid",
        ["recipient@example.invalid"],
    )
    message = message_from_string(raw_message)
    assert message["Subject"] == "Fictional subject"
    assert message.get_payload(0).get_payload(decode=True) == b"Frozen body"


def test_commit_hook_failure_prevents_any_smtp_submission(monkeypatch):
    smtp, send = _arrange(
        monkeypatch,
        failures={
            "commit_hook": RuntimeError("fictional fence failure private detail")
        },
    )

    result = send()

    _assert_result(result, state="not_submitted", retryable=False)
    assert result.message == "smtp_pre_submit_failed"
    assert smtp.events == ["connect", "tls", "auth", "commit_hook", "quit"]
    assert smtp.submissions == []


@pytest.mark.parametrize(
    ("phase", "error", "retryable", "events"),
    [
        ("connect", ConnectionRefusedError("private peer"), True, ["connect"]),
        ("connect", smtplib.SMTPConnectError(421, b"private peer"), True, ["connect"]),
        ("connect", smtplib.SMTPConnectError(554, b"private peer"), False, ["connect"]),
        ("tls", TimeoutError("private peer"), True, ["connect", "tls", "quit"]),
        (
            "tls",
            smtplib.SMTPResponseException(454, b"private peer"),
            True,
            ["connect", "tls", "quit"],
        ),
        (
            "tls",
            smtplib.SMTPResponseException(550, b"private peer"),
            False,
            ["connect", "tls", "quit"],
        ),
        (
            "tls",
            smtplib.SMTPNotSupportedError("private peer"),
            False,
            ["connect", "tls", "quit"],
        ),
        (
            "auth",
            smtplib.SMTPServerDisconnected("private peer"),
            True,
            ["connect", "tls", "auth", "quit"],
        ),
        (
            "auth",
            smtplib.SMTPAuthenticationError(454, b"private peer"),
            True,
            ["connect", "tls", "auth", "quit"],
        ),
        (
            "auth",
            smtplib.SMTPAuthenticationError(535, b"private peer"),
            False,
            ["connect", "tls", "auth", "quit"],
        ),
    ],
)
def test_pre_submission_failures_never_send_and_classify_temporary_rejections(
    monkeypatch, phase, error, retryable, events
):
    smtp, send = _arrange(monkeypatch, failures={phase: error})

    result = send()

    _assert_result(result, state="not_submitted", retryable=retryable)
    assert result.message in {"smtp_auth_rejected", "smtp_pre_submit_failed"}
    assert smtp.events == events
    assert smtp.submissions == []


@pytest.mark.parametrize(
    ("error", "retryable", "message"),
    [
        (
            smtplib.SMTPSenderRefused(451, b"private peer", "sender@example.invalid"),
            True,
            "smtp_rejected",
        ),
        (
            smtplib.SMTPSenderRefused(550, b"private peer", "sender@example.invalid"),
            False,
            "smtp_rejected",
        ),
        (smtplib.SMTPDataError(451, b"private peer"), True, "smtp_rejected"),
        (smtplib.SMTPDataError(554, b"private peer"), False, "smtp_rejected"),
        (
            smtplib.SMTPRecipientsRefused(
                {"recipient@example.invalid": (450, b"private peer")}
            ),
            True,
            "smtp_recipients_rejected",
        ),
        (
            smtplib.SMTPRecipientsRefused(
                {"recipient@example.invalid": (550, b"private peer")}
            ),
            False,
            "smtp_recipients_rejected",
        ),
        (
            smtplib.SMTPRecipientsRefused({}),
            False,
            "smtp_recipients_rejected",
        ),
    ],
)
def test_only_proven_temporary_smtp_rejection_can_retry(
    monkeypatch, error, retryable, message
):
    smtp, send = _arrange(monkeypatch, failures={"sendmail": error})

    result = send()

    _assert_result(result, state="rejected", retryable=retryable)
    assert result.message == message
    assert smtp.events == ["connect", "tls", "auth", "commit_hook", "sendmail", "quit"]
    assert len(smtp.submissions) == 1


@pytest.mark.parametrize(
    "error",
    [
        smtplib.SMTPServerDisconnected("private peer"),
        TimeoutError("private peer"),
        OSError("private peer"),
        RuntimeError("private peer"),
    ],
)
def test_submission_connection_loss_is_uncertain_and_never_retryable(
    monkeypatch, error
):
    smtp, send = _arrange(monkeypatch, failures={"sendmail": error})

    result = send()

    _assert_result(result, state="uncertain", retryable=False)
    assert result.message == "smtp_acceptance_unknown"
    assert smtp.events == ["connect", "tls", "auth", "commit_hook", "sendmail", "quit"]
    assert len(smtp.submissions) == 1


@pytest.mark.parametrize("close_fails", [False, True])
def test_quit_failure_cannot_overturn_accepted_submission(monkeypatch, close_fails):
    failures = {"quit": smtplib.SMTPServerDisconnected("private cleanup detail")}
    if close_fails:
        failures["close"] = OSError("private close detail")
    smtp, send = _arrange(monkeypatch, failures=failures)

    result = send()

    _assert_result(result, state="accepted", retryable=False, success=True)
    assert result.message == "sent"
    assert smtp.events == [
        "connect",
        "tls",
        "auth",
        "commit_hook",
        "sendmail",
        "quit",
        "close",
    ]


def test_cleanup_failure_cannot_mask_original_definite_rejection(monkeypatch):
    smtp, send = _arrange(
        monkeypatch,
        failures={
            "sendmail": smtplib.SMTPDataError(451, b"private rejection"),
            "quit": smtplib.SMTPServerDisconnected("private cleanup detail"),
            "close": OSError("private close detail"),
        },
    )

    result = send()

    _assert_result(result, state="rejected", retryable=True)
    assert result.message == "smtp_rejected"
    assert smtp.events[-3:] == ["sendmail", "quit", "close"]
