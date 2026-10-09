"""Tests for the switches in front of every broker email, and the plain reply
to a refused emailed file.

  BROKER_NOTIFY_ENABLED    the broker loop's emails and the channel guide
                           (submission_service.notifications_enabled)
  EMAIL_REPLY_ON_REFUSAL   the plain "We could not use …" reply
                           (email_poller.notify_sender)
  MAIL_ALLOWED_RECIPIENTS  the test-only list every email is kept to

Both switches stay OFF unless set, so a developer's machine on the shared
database never emails a real broker; a server meant to email brokers sets them.

No database, no mail server: send_email is a recorder, and smtplib.SMTP_SSL
fails the test if anything ever reaches it.

A file uploaded by hand in the web portal is never emailed; files sent by
email, SFTP or API are.

Run:  python -m pytest test_broker_email_defaults.py
"""
import os

# Never a real database: nothing here queries one, and an import must not
# find one (db.py builds its engine from DATABASE_URL when imported).
os.environ.setdefault("DATABASE_URL", "sqlite://")

import logging
from types import SimpleNamespace

import pytest

import audit
import email_poller
import email_utils
import intake_guide
import settings
import submission_service

_SWITCHES = [
    pytest.param("BROKER_NOTIFY_ENABLED", submission_service.notifications_enabled,
                 id="broker-emails"),
    pytest.param("EMAIL_REPLY_ON_REFUSAL", email_poller._reply_on_refusal,
                 id="refusal-reply"),
]


@pytest.fixture(autouse=True)
def sent(monkeypatch):
    """Every switch unset, and every email caught before it can leave."""
    for var in ("BROKER_NOTIFY_ENABLED", "EMAIL_REPLY_ON_REFUSAL",
                "MAIL_ALLOWED_RECIPIENTS", "EMAIL_REPLY_MAX_PER_DAY",
                "SUBMISSION_DEADLINE_SWEEP_SECONDS"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(email_utils.smtplib, "SMTP_SSL",
                        lambda *a, **k: pytest.fail("an email reached SMTP"))
    out = []
    monkeypatch.setattr(email_utils, "send_email", lambda *a, **k: out.append((a, k)))
    return out


# ── the defaults ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("var, on", _SWITCHES)
def test_off_when_unset(var, on):
    assert on() is False


@pytest.mark.parametrize("var, on", _SWITCHES)
@pytest.mark.parametrize("value", ["1", "true", "yes", "on", "TRUE", " On "])
def test_on_for_the_on_words(monkeypatch, var, on, value):
    monkeypatch.setenv(var, value)
    assert on() is True


@pytest.mark.parametrize("var, on", _SWITCHES)
@pytest.mark.parametrize("value", ["0", "false", "no", "off", "", "enabled"])
def test_anything_else_is_off(monkeypatch, var, on, value):
    # A mistyped value switches it OFF, never on.
    monkeypatch.setenv(var, value)
    assert on() is False


# ── the broker loop's emails (_send_one) ────────────────────────────────────

def _notice(channel="email"):
    return {"channel": channel, "recipient": "b@example.com",
            "payload": {"subject": "s", "html": "h", "text": "t"}}


def test_notice_skipped_when_broker_emails_are_unset(sent):
    assert submission_service._send_one(_notice()) == (
        "skipped", "broker emails are switched off (BROKER_NOTIFY_ENABLED)")
    assert sent == []


def test_notice_skipped_with_0(monkeypatch, sent):
    monkeypatch.setenv("BROKER_NOTIFY_ENABLED", "0")
    assert submission_service._send_one(_notice())[0] == "skipped"
    assert sent == []


def test_notice_sent_on_the_notify_account_when_on(monkeypatch, sent):
    monkeypatch.setenv("BROKER_NOTIFY_ENABLED", "1")
    assert submission_service._send_one(_notice()) == ("sent", None)
    (args, kw), = sent
    assert args[:3] == ("b@example.com", "s", "h")
    assert kw["account"] == "NOTIFY" and kw["text"] == "t"


def test_notice_skipped_when_the_allowlist_leaves_the_broker_out(monkeypatch, sent):
    monkeypatch.setenv("BROKER_NOTIFY_ENABLED", "1")
    monkeypatch.setenv("MAIL_ALLOWED_RECIPIENTS", "qa@example.com")
    assert submission_service._send_one(_notice()) == (
        "skipped", "test mode: address not in MAIL_ALLOWED_RECIPIENTS")
    assert sent == []


def test_notice_sent_when_the_allowlist_names_the_broker(monkeypatch, sent):
    monkeypatch.setenv("BROKER_NOTIFY_ENABLED", "1")
    monkeypatch.setenv("MAIL_ALLOWED_RECIPIENTS", "qa@example.com; B@Example.com")
    assert submission_service._send_one(_notice()) == ("sent", None)
    assert len(sent) == 1


def test_sftp_folder_files_ignore_the_switch(monkeypatch, sent):
    written = []
    monkeypatch.setattr(submission_service, "write_sftp", written.append)
    assert submission_service._send_one(_notice("sftp_file")) == ("sent", None)
    assert len(written) == 1 and sent == []


# ── the channel guide (intake_guide.send) ───────────────────────────────────

@pytest.fixture
def guide(monkeypatch):
    """recipients/build stand-ins, and threads that only start when told to."""
    threads = []

    class _Thread:
        def __init__(self, target, name=None, daemon=None):
            self.target = target

        def start(self):
            threads.append(self)

    monkeypatch.setattr(intake_guide, "threading", SimpleNamespace(Thread=_Thread))
    monkeypatch.setattr(intake_guide, "recipients", lambda s, route: ["b@example.com"])
    monkeypatch.setattr(intake_guide, "build", lambda s, route, **k: {
        "subject": "How to send", "html": "h", "text": "t", "reply_to": None})
    logged = []
    monkeypatch.setattr(audit, "log_activity", lambda *a, **k: logged.append(k["details"]))
    route = SimpleNamespace(id=7, tenant_id=1, broker_party_id=3, channel="email",
                            address="ops@broker.example", program_id=None)
    return SimpleNamespace(route=route, threads=threads, logged=logged)


def _send_guide(g):
    return intake_guide.send(None, g.route, carrier="Acme Re", send_to="in@kavachio.example")


def test_guide_not_sent_when_broker_emails_are_unset(guide, sent):
    assert _send_guide(guide) == {"recipients": ["b@example.com"], "sending": False}
    assert guide.threads == [] and sent == []


def test_guide_not_sent_with_0(monkeypatch, guide, sent):
    monkeypatch.setenv("BROKER_NOTIFY_ENABLED", "0")
    assert _send_guide(guide)["sending"] is False
    assert guide.threads == []


def test_guide_sent_on_the_notify_account_when_on(monkeypatch, guide, sent):
    monkeypatch.setenv("BROKER_NOTIFY_ENABLED", "1")
    assert _send_guide(guide)["sending"] is True
    guide.threads[0].target()
    (args, kw), = sent
    assert args[:2] == ("b@example.com", "How to send") and kw["account"] == "NOTIFY"
    assert guide.logged == [{"recipient": "b@example.com", "status": "sent", "error": None}]


def test_guide_skipped_and_recorded_under_the_allowlist(monkeypatch, guide, sent):
    monkeypatch.setenv("BROKER_NOTIFY_ENABLED", "1")
    monkeypatch.setenv("MAIL_ALLOWED_RECIPIENTS", "qa@example.com")
    assert _send_guide(guide)["sending"] is True
    guide.threads[0].target()
    assert sent == []
    assert guide.logged == [{"recipient": "b@example.com", "status": "skipped",
                             "error": "test mode: address not in MAIL_ALLOWED_RECIPIENTS"}]


# ── the plain reply to a refused emailed file (notify_sender) ───────────────

@pytest.fixture
def told_today(monkeypatch):
    """How many replies the sender already had today — nobody, unless set."""
    count = {"n": 0}
    monkeypatch.setattr(email_poller, "_already_told_today", lambda s, sender: count["n"])
    return count


def _arrival(outcome="turned_away", broker=None):
    return SimpleNamespace(outcome=outcome, filename="Motor Sept 2026.xlsx",
                           turned_away_reason="We do not recognise the sender.",
                           matched_broker_party_id=broker,
                           sender_notified_at=None, sender_notified_via=None)


def _from(addr="x@broker.example", automated=False):
    return SimpleNamespace(from_addr=addr, is_automated=automated)


def test_no_reply_when_unset(told_today, sent):
    arrival = _arrival()
    assert email_poller.notify_sender(None, arrival, _from()) is False
    assert sent == [] and arrival.sender_notified_at is None


def test_no_reply_with_0(monkeypatch, told_today, sent):
    monkeypatch.setenv("EMAIL_REPLY_ON_REFUSAL", "0")
    assert email_poller.notify_sender(None, _arrival(), _from()) is False
    assert sent == []


def test_unknown_sender_gets_one_reply_when_on(monkeypatch, told_today, sent):
    monkeypatch.setenv("EMAIL_REPLY_ON_REFUSAL", "1")
    arrival = _arrival()
    assert email_poller.notify_sender(None, arrival, _from()) is True
    (args, kw), = sent
    assert kw["to"] == "x@broker.example"
    assert kw["subject"] == "We could not use “Motor Sept 2026.xlsx”"
    assert "We do not recognise the sender." in kw["text"]
    assert "account" not in kw                  # the default account, not NOTIFY
    assert arrival.sender_notified_at is not None and arrival.sender_notified_via == "email"


def test_no_reply_for_a_held_file(monkeypatch, told_today, sent):
    monkeypatch.setenv("EMAIL_REPLY_ON_REFUSAL", "1")
    assert email_poller.notify_sender(None, _arrival(outcome="held"), _from()) is False
    assert sent == []


def test_no_reply_to_an_automated_sender(monkeypatch, told_today, sent):
    monkeypatch.setenv("EMAIL_REPLY_ON_REFUSAL", "1")
    assert email_poller.notify_sender(None, _arrival(), _from(automated=True)) is False
    assert sent == []


def test_no_reply_once_the_daily_cap_is_reached(monkeypatch, told_today, sent):
    monkeypatch.setenv("EMAIL_REPLY_ON_REFUSAL", "1")
    told_today["n"] = 3
    assert email_poller.notify_sender(None, _arrival(), _from()) is False
    assert sent == []


def test_known_broker_is_not_told_twice_when_broker_emails_are_on(monkeypatch, told_today, sent):
    # They already get 'File rejected' with the reason from the broker loop.
    monkeypatch.setenv("EMAIL_REPLY_ON_REFUSAL", "1")
    monkeypatch.setenv("BROKER_NOTIFY_ENABLED", "1")
    arrival = _arrival(broker=42)
    assert email_poller.notify_sender(None, arrival, _from()) is False
    assert sent == [] and arrival.sender_notified_at is None


def test_known_broker_gets_the_reply_when_broker_emails_are_off(monkeypatch, told_today, sent):
    # Nobody else tells them, so the plain reply is the only message.
    monkeypatch.setenv("EMAIL_REPLY_ON_REFUSAL", "1")
    assert email_poller.notify_sender(None, _arrival(broker=42), _from()) is True
    assert len(sent) == 1


def test_not_stamped_told_when_the_allowlist_skips_the_sender(monkeypatch, told_today, sent):
    monkeypatch.setenv("EMAIL_REPLY_ON_REFUSAL", "1")
    monkeypatch.setenv("MAIL_ALLOWED_RECIPIENTS", "qa@example.com")
    arrival = _arrival()
    assert email_poller.notify_sender(None, arrival, _from()) is False
    assert sent == []
    assert arrival.sender_notified_at is None and arrival.sender_notified_via is None


def test_allowlisted_sender_still_gets_the_reply(monkeypatch, told_today, sent):
    monkeypatch.setenv("EMAIL_REPLY_ON_REFUSAL", "1")
    monkeypatch.setenv("MAIL_ALLOWED_RECIPIENTS", "qa@example.com, x@broker.example")
    arrival = _arrival()
    assert email_poller.notify_sender(None, arrival, _from()) is True
    assert len(sent) == 1 and arrival.sender_notified_at is not None


# ── the start-up line ───────────────────────────────────────────────────────

class _App:
    """Just enough of FastAPI for start(): handlers are kept, never run."""

    def __init__(self):
        self.handlers = {}

    def on_event(self, name):
        def keep(fn):
            self.handlers[name] = fn
            return fn
        return keep


def _start_line(caplog):
    """The one record start() logs about email, and the whole log text."""
    caplog.clear()
    with caplog.at_level(logging.INFO, logger="kavachio.submissions"):
        submission_service.start(_App())
    lines = [r for r in caplog.records if "broker emails" in r.getMessage()]
    assert len(lines) == 1
    return lines[0], caplog.text


@pytest.fixture
def real_secret(monkeypatch):
    secret = "prod-secret-7f3a9c2e"
    monkeypatch.setattr(settings.SETTINGS, "JWT_SECRET", secret)
    return secret


def test_start_line_everything_off(caplog, real_secret):
    rec, text = _start_line(caplog)
    assert rec.levelno == logging.INFO
    assert rec.getMessage() == ("broker emails OFF · refusal replies OFF · "
                                "MAIL_ALLOWED_RECIPIENTS: NOT SET (real recipients)")
    assert real_secret not in text


def test_start_line_on_with_the_allowlist_counts_addresses_only(monkeypatch, caplog,
                                                                real_secret):
    monkeypatch.setenv("BROKER_NOTIFY_ENABLED", "1")
    monkeypatch.setenv("EMAIL_REPLY_ON_REFUSAL", "yes")
    monkeypatch.setenv("MAIL_ALLOWED_RECIPIENTS",
                       "qa@example.com, dev@example.com; QA@example.com\nops@example.com")
    rec, text = _start_line(caplog)
    assert rec.getMessage() == ("broker emails ON · refusal replies ON · "
                                "MAIL_ALLOWED_RECIPIENTS: 3 addresses")
    assert "@" not in text and real_secret not in text


def test_start_line_one_address(monkeypatch, caplog, real_secret):
    monkeypatch.setenv("MAIL_ALLOWED_RECIPIENTS", "qa@example.com")
    rec, _ = _start_line(caplog)
    assert rec.getMessage().endswith("MAIL_ALLOWED_RECIPIENTS: 1 address")


def test_start_line_warns_on_the_development_secret(monkeypatch, caplog):
    monkeypatch.setattr(settings.SETTINGS, "JWT_SECRET", settings._DEV_SECRET)
    monkeypatch.setenv("BROKER_NOTIFY_ENABLED", "1")
    rec, text = _start_line(caplog)
    assert rec.levelno == logging.WARNING
    assert rec.getMessage() == (
        "broker emails ON · refusal replies OFF · "
        "MAIL_ALLOWED_RECIPIENTS: NOT SET (real recipients) · "
        "fix links and logins are signed with the development secret — set JWT_SECRET")
    assert settings._DEV_SECRET not in text


def test_start_line_logged_even_with_the_deadline_sweep_off(monkeypatch, caplog,
                                                           real_secret):
    monkeypatch.setenv("SUBMISSION_DEADLINE_SWEEP_SECONDS", "0")
    rec, _ = _start_line(caplog)
    assert rec.getMessage().startswith("broker emails OFF")


# ── a portal hand upload is never emailed ───────────────────────────────────
#
# The broker watched a hand upload run and read the result on screen. Files
# sent by email, SFTP or API are emailed; later carrier actions (delivery, a
# deadline hold) carry no arrival and are still emailed.

@pytest.fixture
def asked(monkeypatch):
    """Who the result email would go to is asked only when it will be sent."""
    calls = []
    monkeypatch.setattr(submission_service, "recipients",
                        lambda s, th, arrival: calls.append(getattr(arrival, "channel", None)) or [])
    return calls


def _thread():
    return SimpleNamespace(ref="SUB-1", status="rejected",
                           current=SimpleNamespace(status="rejected", no=1))


def test_portal_upload_is_not_emailed(monkeypatch, asked):
    monkeypatch.setenv("BROKER_NOTIFY_ENABLED", "1")
    submission_service._queue_result_email(
        None, _thread(), None, SimpleNamespace(channel="upload"), event="rejected")
    assert asked == []


@pytest.mark.parametrize("channel", ["email", "sftp", "api"])
def test_files_sent_by_a_channel_are_emailed(monkeypatch, asked, channel):
    monkeypatch.setenv("BROKER_NOTIFY_ENABLED", "1")
    submission_service._queue_result_email(
        None, _thread(), None, SimpleNamespace(channel=channel), event="rejected")
    assert asked == [channel]


def test_a_later_carrier_action_is_still_emailed(monkeypatch, asked):
    monkeypatch.setenv("BROKER_NOTIFY_ENABLED", "1")
    submission_service._queue_result_email(None, _thread(), None, None, event="rejected")
    assert asked == [None]
