"""Delivery config handling — no SMTP connection is ever made in tests."""
from __future__ import annotations

from sentinel.deliver import send_report


def test_missing_credentials_skips_gracefully(monkeypatch):
    for var in ("SMTP_HOST", "SMTP_PORT", "SMTP_USER", "SMTP_PASS", "RECIPIENT_EMAILS"):
        monkeypatch.delenv(var, raising=False)
    sent, status = send_report("<p>hi</p>", "subject")
    assert not sent
    assert "skipped" in status


def test_empty_env_vars_treated_as_unset(monkeypatch):
    # GitHub Actions passes unset secrets as empty strings — must not crash on int("")
    for var in ("SMTP_HOST", "SMTP_PORT", "SMTP_USER", "SMTP_PASS", "RECIPIENT_EMAILS"):
        monkeypatch.setenv(var, "")
    sent, status = send_report("<p>hi</p>", "subject")
    assert not sent
    assert "skipped" in status
