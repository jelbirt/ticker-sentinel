"""Delivery config handling — no SMTP connection is ever made in tests."""
from __future__ import annotations

from sentinel.deliver import send_report


def test_missing_credentials_skips_gracefully(monkeypatch):
    for var in ("SMTP_HOST", "SMTP_PORT", "SMTP_USER", "SMTP_PASS", "RECIPIENT_EMAILS"):
        monkeypatch.delenv(var, raising=False)
    sent, status = send_report("<p>hi</p>", "subject")
    assert not sent
    assert "skipped" in status


def _offline_run(monkeypatch, sent: bool):
    """Drive main() past the email tail with every network call stubbed out.

    A ticker subset is used so the run never touches data/cache (no run-history
    write, no cache prune), and every fetch returns empty: the report still gets
    written, which is the point of the degrade-never-crash rule.
    """
    from sentinel.run import main

    monkeypatch.setattr("sentinel.data.fundamentals.get_fundamentals", lambda ticker: (None, []))
    monkeypatch.setattr("sentinel.data.signals.fetch_signals", lambda ticker: (None, []))
    monkeypatch.setattr(
        "sentinel.data.prices.fetch_prices", lambda universe, period="1y": (None, None, [])
    )
    monkeypatch.setattr("sentinel.news.pipeline.collect_news", lambda *a, **kw: ([], {}, []))
    monkeypatch.setattr(
        "sentinel.deliver.send_report",
        lambda *a, **kw: (sent, "sent to 1 recipient" if sent else "smtp refused"),
    )
    return main


def test_unsent_email_fails_the_run(tmp_path, monkeypatch):
    # a silent green run with no email is the failure this exit code exists to surface
    main = _offline_run(monkeypatch, sent=False)
    assert main(["--tickers", "ALFA", "--out-dir", str(tmp_path)]) == 1
    assert (tmp_path / "report.html").exists()   # the report still landed


def test_sent_email_keeps_the_run_green(tmp_path, monkeypatch):
    main = _offline_run(monkeypatch, sent=True)
    assert main(["--tickers", "ALFA", "--out-dir", str(tmp_path)]) == 0


def test_empty_env_vars_treated_as_unset(monkeypatch):
    # GitHub Actions passes unset secrets as empty strings — must not crash on int("")
    for var in ("SMTP_HOST", "SMTP_PORT", "SMTP_USER", "SMTP_PASS", "RECIPIENT_EMAILS"):
        monkeypatch.setenv(var, "")
    sent, status = send_report("<p>hi</p>", "subject")
    assert not sent
    assert "skipped" in status
