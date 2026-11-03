"""External SFTP pull — Kavachio collects bordereaux from SOMEBODY ELSE'S server.

sftp_poller watches folders on Kavachio's own disk: a broker logs in to US and
drops a file. This is the other way round. The carrier types in the details of
an SFTP server that already exists — the broker's, or their own — and Kavachio
logs in to it as an ordinary SFTP client every few minutes, downloads the
finished files from one folder, and hands each one to intake_service.land_file:
the same landing, the same checks and the same Files Received row as a file
that came by email or API. The remote file is then moved to a "processed"
folder (or deleted), so it is never collected twice.

WHERE THE SETTINGS LIVE. intake_route.route_sftp_config (JSONB, migration 35).
That column does NOT exist on a database until migration 35 is run, and every
ORM SELECT of intake_route would break the moment the model mapped it — so it
is never mapped. It is read and written here with raw SQL, behind a check of
information_schema that is cached. Without the column this module does nothing
at all: the scheduler idles and creating a pull route says why it cannot.

A pull route is recognised WITHOUT the column, by its address: the route's
`address` is "sftp://user@host:port/folder". The old local-folder routes store a
bare path ("insurisk/corvin"), so sftp_poller can skip pull routes with no
database change, and the two never collect each other's routes.

SECURITY, in the order it is applied on every connection:
  * the address must resolve to somewhere we are willing to connect to — never
    a link-local / cloud-metadata address, and no loopback or private network
    unless SFTP_PULL_ALLOW_PRIVATE=1 (otherwise a carrier could use the Test
    button to probe hosts inside our own network);
  * the server's host key is PINNED when the route is created (the carrier
    tests, sees the fingerprint, saves). Every later connection checks it before
    a password is sent, and a different key is refused (error_code host_key) —
    there is no "accept new key" path after create;
  * no SSH agent, no ~/.ssh keys: only the secret saved on the route is used,
    and ONE login attempt per connection (Go-based servers such as SFTPGo hang
    up on a second attempt, so a password the server only takes as
    "keyboard-interactive" is tried on a fresh connection, key checked again);
  * the password / private key is Fernet-encrypted at rest (intake_secrets) and
    is never returned by the API.

SERVERS DIFFER, and the code below is written for the awkward ones:
  OpenSSH            the reference; posix-rename@openssh.com is used when there.
  FileZilla Server   Windows drives shown as /C:/…; no posix-rename; a rename
                     onto an existing name fails; modification times can be
                     coarse — so a file is "finished" when its SIZE and time are
                     unchanged across two listings, not by time alone.
  Azure Blob SFTP    no posix-rename; home directory = container; no chmod
                     (mkdir is retried without attributes); listings may carry
                     no permission bits, so the long listing is read instead.
  AWS Transfer (S3)  folders are virtual; rename may be refused — the fallback
                     is copy + verify size + delete.
  GoAnywhere, Cerberus, Bitvise, …  generic SFTP v3; nothing assumed.

Paramiko 5 has dropped SHA-1 (ssh-rsa signatures, diffie-hellman-*-sha1). A
server that offers nothing newer is refused with error_code "protocol" and a
message that says so — that is a server to be updated, not worked around.

Measured against nine local servers (OpenSSH 8.9, SFTPGo 2.7.6, paramiko
stubs for Windows-like and SHA-1-only servers, a tarpit); see the compatibility
notes the test-server work produced. Every rule below that looks odd is there
because one of them needed it.

Configuration:
  SFTP_PULL_ENABLED        0/1  (default 1) the scheduler; POST /poll works either way
  SFTP_PULL_ALLOW_PRIVATE  0/1  (default 0) allow loopback / private-network hosts —
                                for development and tests only. Link-local
                                (169.254.x.x, cloud metadata) is refused whatever
                                this says.
  SFTP_PULL_MAX_FILES      int  (default 50) files landed per route per check
  SFTP_QUIET_SECONDS       int  (default 10) see intake_service.quiet_seconds
  INTAKE_MAX_FILE_MB       int  (default 200) see intake_safety.max_bytes
  INTAKE_SECRET_KEY        Fernet key, see intake_secrets
"""
from __future__ import annotations

import base64
import errno
import hashlib
import io
import ipaddress
import json
import logging
import os
import posixpath
import re
import secrets
import socket
import stat as statmod
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

import intake_secrets
import sftp_watch

log = logging.getLogger("kavachio.sftp_pull")

# paramiko's transport thread logs a full traceback at ERROR for every failed
# handshake — a wrong port typed into the Test form fills the log with them.
# Every failure is translated and logged here in plain words instead. Only
# touched when nobody has configured that logger.
if logging.getLogger("paramiko.transport").level == logging.NOTSET:
    logging.getLogger("paramiko.transport").setLevel(logging.CRITICAL)

# ── fixed behaviour ─────────────────────────────────────────────────────────

CONNECT_TIMEOUT = 15        # TCP connect
BANNER_TIMEOUT = 15         # the server's first "SSH-2.0-…" line
# The whole key exchange. Kept ABOVE the banner timeout: equal, the two timers
# race and a silent server is reported as "No existing session".
HANDSHAKE_TIMEOUT = 20
AUTH_TIMEOUT = 20           # how long the server may take to say yes or no
IO_TIMEOUT = 60             # any one SFTP request (list, read, rename)
KEEPALIVE_SECONDS = 30      # Azure drops a connection idle for 2 minutes
# Outstanding read requests while downloading. Unbounded (paramiko's default)
# can swamp some servers (the Maverick engine behind GoAnywhere); a small limit
# is ruinous — paramiko then busy-waits between requests: measured 5 MB/s at
# 64 against 60-90 MB/s at 256.
PREFETCH_REQUESTS = 256
CHUNK = 1 << 20

# Upload names to leave alone, on top of sftp_watch's (.filepart, .part,
# .partial, .tmp, .crdownload, .upload): CrushFTP lets each server choose one.
_EXTRA_TEMP_SUFFIXES = (".temp", ".uploading", ".partial", ".upload")

# Words in a keyboard-interactive prompt that mean "a one-time code", which a
# stored password cannot answer.
_SECOND_FACTOR_WORDS = ("code", "token", "verification", "one-time", "otp",
                        "authenticator")

INTERVALS = (5, 15, 60)                       # minutes, the only choices offered
DEFAULT_INTERVAL = 15
AFTER_CHOICES = ("move", "delete")
ERROR_CODES = ("dns", "refused", "timeout", "auth", "host_key", "no_dir",
               "permission", "protocol")

# An EMPTY file is usually an upload that has only just started (the server
# creates the file, then the bytes follow). One this old is genuinely empty and
# is landed — and refused as empty — rather than skipped for ever.
EMPTY_GRACE_SECONDS = 600

# Advisory-lock namespace — different from sftp_poller's 0x5F7B, so a pull
# route and a local route with the same id never block each other.
_LOCK_NAMESPACE = 0x5F7C

_TICK_SECONDS = 60          # how often the scheduler looks for routes that are due
_COLUMN_RECHECK_SECONDS = 300

# The test file the Test button writes to prove files can be moved / deleted.
# A dotfile AND a temporary suffix, so neither we nor most other collectors
# would ever pick it up in the second it exists.
_PROBE_PREFIX = ".kavachio-check-"


class PullError(Exception):
    """A connection or folder problem, in plain words, with one of ERROR_CODES."""

    def __init__(self, code: Optional[str], message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class SettingsError(ValueError):
    """What the carrier typed cannot be used as it is."""


class MoveFailed(Exception):
    """A landed file could not be moved / deleted on the remote server."""


# ── switches ────────────────────────────────────────────────────────────────

def _flag(name: str, default: str) -> bool:
    return os.getenv(name, default).strip().lower() in ("1", "true", "yes", "on")


def _enabled() -> bool:
    return _flag("SFTP_PULL_ENABLED", "1")


def _allow_private() -> bool:
    return _flag("SFTP_PULL_ALLOW_PRIVATE", "0")


def _max_files() -> int:
    try:
        return max(1, int(os.getenv("SFTP_PULL_MAX_FILES", "50")))
    except ValueError:
        return 50


def _paramiko():
    """Imported lazily: the app must still boot where paramiko is missing."""
    try:
        import paramiko
        return paramiko
    except ImportError as exc:  # pragma: no cover - depends on the environment
        raise PullError("protocol", "SFTP collection is not installed on this server "
                                    "(the 'paramiko' package is missing). Ask your "
                                    "administrator to install requirements.txt.") from exc


# ── the stored settings (raw SQL; see the module note) ──────────────────────

_column = {"ok": False, "checked": 0.0}


def _sa_text(sql: str):
    from sqlalchemy import text
    return text(sql)


def column_ready(session=None, *, fresh: bool = False) -> bool:
    """True once migration 35 has added intake_route.route_sftp_config.

    A yes is remembered for the life of the process. A no is re-asked every few
    minutes (or at once with `fresh`, which creating a route uses), so running
    the migration takes effect without a restart. Asked on a connection of its
    own, so a failure can never poison the caller's transaction.
    """
    if _column["ok"]:
        return True
    now = time.monotonic()
    if not fresh and _column["checked"] and now - _column["checked"] < _COLUMN_RECHECK_SECONDS:
        return False
    found = False
    try:
        if session is not None:
            bind = session.get_bind()
        else:
            from db import engine as bind
        from sqlalchemy.engine import Connection
        sql = _sa_text(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_name = 'intake_route' AND column_name = 'route_sftp_config' "
            "AND table_schema = ANY (current_schemas(false)) LIMIT 1")
        if isinstance(bind, Connection):
            found = bind.execute(sql).first() is not None
        else:
            with bind.connect() as conn:
                found = conn.execute(sql).first() is not None
    except Exception as exc:  # noqa: BLE001 — "not ready" is the safe answer
        log.debug("route_sftp_config check failed: %s", exc)
        found = False
    _column["ok"], _column["checked"] = found, now
    return found


def _as_dict(value) -> Optional[dict]:
    if value is None:
        return None
    if isinstance(value, dict):
        return value
    try:
        out = json.loads(value)
        return out if isinstance(out, dict) else None
    except (TypeError, ValueError):
        return None


def load_config(session, route_id: int) -> Optional[dict]:
    """The stored settings of one route, or None (none saved, or no column)."""
    if not column_ready(session):
        return None
    try:
        with session.begin_nested():
            row = session.execute(
                _sa_text("SELECT route_sftp_config FROM intake_route WHERE route_id = :id"),
                {"id": route_id}).first()
    except Exception as exc:  # noqa: BLE001
        log.warning("could not read the SFTP settings of route %s: %s", route_id, exc)
        return None
    return _as_dict(row[0]) if row else None


def configs_for_tenant(session, tenant_id: int) -> dict[int, dict]:
    """{route_id: settings} for every pull route of one carrier — one query for
    the whole "How Files Arrive" list."""
    if not column_ready(session):
        return {}
    try:
        with session.begin_nested():
            rows = session.execute(
                _sa_text("SELECT route_id, route_sftp_config FROM intake_route "
                         "WHERE tenant_id = :t AND route_sftp_config IS NOT NULL"),
                {"t": tenant_id}).all()
    except Exception as exc:  # noqa: BLE001
        log.warning("could not read SFTP settings for tenant %s: %s", tenant_id, exc)
        return {}
    out = {}
    for rid, cfg in rows:
        d = _as_dict(cfg)
        if d:
            out[int(rid)] = d
    return out


def save_config(session, route_id: int, cfg: dict) -> None:
    """Write a route's settings inside the caller's transaction (create)."""
    session.execute(
        _sa_text("UPDATE intake_route SET route_sftp_config = CAST(:c AS jsonb) "
                 "WHERE route_id = :id"),
        {"c": json.dumps(cfg), "id": route_id})


def _save_status(session, route_id: int, patch: dict) -> None:
    """Merge last_checked_at / last_error / last_collected into the settings.

    Its own short transaction on its own connection: it must be recorded even
    when the collection failed half way, and must not be lost to — or lose —
    whatever the caller's session is doing. A merge (||), not a rewrite, so it
    can never put back a secret that was changed meanwhile.
    """
    try:
        bind = session.get_bind()
        from sqlalchemy.engine import Connection
        sql = _sa_text("UPDATE intake_route SET route_sftp_config = "
                       "COALESCE(route_sftp_config, CAST('{}' AS jsonb)) || CAST(:p AS jsonb) "
                       "WHERE route_id = :id AND route_sftp_config IS NOT NULL")
        params = {"p": json.dumps(patch), "id": route_id}
        if isinstance(bind, Connection):
            bind.execute(sql, params)
        else:
            with bind.begin() as conn:
                conn.execute(sql, params)
    except Exception as exc:  # noqa: BLE001
        log.warning("could not record the check of route %s: %s", route_id, exc)


def public_view(cfg: Optional[dict]) -> Optional[dict]:
    """What the screen may see. Never the secret, only that there is one."""
    if not cfg:
        return None
    hk = cfg.get("host_key") or {}
    return {
        "host": cfg.get("host"),
        "port": cfg.get("port"),
        "username": cfg.get("username"),
        "auth": cfg.get("auth"),
        "remote_dir": cfg.get("remote_dir"),
        "after": cfg.get("after"),
        "processed_dir": cfg.get("processed_dir"),
        "interval_minutes": cfg.get("interval_minutes"),
        "fingerprint": hk.get("fingerprint_sha256"),
        "last_checked_at": cfg.get("last_checked_at"),
        "last_error": cfg.get("last_error"),
        "last_collected": cfg.get("last_collected"),
        "has_secret": bool(cfg.get("secret_enc")),
    }


# ── what the carrier typed ──────────────────────────────────────────────────

_HOST_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?$")


def _clean_path(value) -> str:
    """A remote folder as typed: trimmed, Windows backslashes turned round.
    "" (and ".", "~", "~/") mean the folder the login starts in."""
    p = str(value or "").strip().replace("\\", "/")
    if p in (".", "~", "~/", "./"):
        return ""
    if p.startswith("~/"):
        p = p[2:]
    while "//" in p:
        p = p.replace("//", "/")
    if len(p) > 1:
        p = p.rstrip("/")
    return p


def default_processed_dir(remote_dir: str) -> str:
    """"<remote_dir>/processed" — inside the folder we collect from, which is
    the one place the login is certain to be able to reach."""
    r = _clean_path(remote_dir)
    if r == "":
        return "processed"
    if r == "/":
        return "/processed"
    return r + "/processed"


def clean_settings(raw: dict, *, for_create: bool = False) -> dict:
    """Validate and tidy the connection details. Raises SettingsError."""
    raw = raw or {}
    host = str(raw.get("host") or "").strip()
    # People paste a URL into the host box: "sftp://files.broker.com/".
    if "://" in host:
        host = host.split("://", 1)[1]
    host = host.strip().strip("/")
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]
    if not host:
        raise SettingsError("Enter the server's host name, for example sftp.broker.com.")
    if "@" in host:
        raise SettingsError("Put only the server name in Host — the user name has its own box.")
    if "/" in host:
        raise SettingsError("Put only the server name in Host — the folder has its own box.")
    if ":" in host:
        try:
            ipaddress.IPv6Address(host.split("%")[0])
        except ValueError:
            raise SettingsError("Put only the server name in Host — the port has its own box.")
    elif not _HOST_RE.match(host) or len(host) > 253:
        raise SettingsError(f"“{host}” is not a valid host name.")

    try:
        port = int(raw.get("port") or 22)
    except (TypeError, ValueError):
        raise SettingsError("The port must be a number (SFTP is usually 22).")
    if not 1 <= port <= 65535:
        raise SettingsError("The port must be between 1 and 65535 (SFTP is usually 22).")

    username = str(raw.get("username") or "").strip()
    if not username:
        raise SettingsError("Enter the user name Kavachio should log in as.")

    auth = str(raw.get("auth") or "password").strip().lower()
    if auth not in ("password", "key"):
        raise SettingsError("Choose how to log in: password or key.")
    password = raw.get("password")
    private_key = raw.get("private_key")
    passphrase = raw.get("passphrase") or None
    if auth == "password":
        # Never trimmed: a space can be part of a password.
        if password is None or str(password) == "":
            raise SettingsError("Enter the password.")
        secret = str(password)
    else:
        if private_key is None or not str(private_key).strip():
            raise SettingsError("Paste the private key.")
        secret = str(private_key)

    remote_dir = _clean_path(raw.get("remote_dir"))
    after = str(raw.get("after") or "move").strip().lower()
    if after not in AFTER_CHOICES:
        raise SettingsError("Choose what happens to a file after it is collected: move or delete.")
    processed_dir = _clean_path(raw.get("processed_dir")) or None
    if after == "move":
        processed_dir = processed_dir or default_processed_dir(remote_dir)
        if processed_dir.rstrip("/") == (remote_dir or "").rstrip("/"):
            raise SettingsError("The processed folder must be different from the folder "
                                "files are collected from.")
    else:
        processed_dir = None

    out = {"host": host, "port": port, "username": username, "auth": auth,
           "secret": secret, "passphrase": str(passphrase) if passphrase else None,
           "remote_dir": remote_dir, "after": after, "processed_dir": processed_dir}
    if for_create:
        try:
            interval = int(raw.get("interval_minutes") or DEFAULT_INTERVAL)
        except (TypeError, ValueError):
            interval = -1
        if interval not in INTERVALS:
            raise SettingsError("Check every 5, 15 or 60 minutes.")
        out["interval_minutes"] = interval
        fp = str(raw.get("fingerprint") or "").strip()
        if not fp:
            raise SettingsError("Test the connection first — the server's fingerprint "
                                "from the test is what this channel trusts.")
        out["fingerprint"] = fp
    return out


def build_address(settings: dict) -> str:
    """"sftp://user@host:port/folder" — the route's address. Display, and the
    marker that tells a pull route from a local-folder one."""
    host = settings["host"]
    if ":" in host:
        host = f"[{host}]"
    folder = settings.get("remote_dir") or ""
    # A folder relative to the login's home ("outgoing") is shown as "~/outgoing"
    # so it is not mistaken for — or collide with — the absolute "/outgoing".
    path = folder if folder.startswith("/") else f"/~/{folder}"
    return f"sftp://{settings['username']}@{host}:{settings['port']}{path}"


def is_pull_route(route) -> bool:
    return (getattr(route, "channel", None) == "sftp"
            and str(getattr(route, "address", "") or "").startswith("sftp://"))


# ── keys ────────────────────────────────────────────────────────────────────

def host_key_info(key) -> dict:
    """{type, fingerprint_sha256, key_b64} — the fingerprint in the form
    `ssh-keygen -l` prints, so a carrier can compare it with the server owner's."""
    blob = key.asbytes()
    fp = "SHA256:" + base64.b64encode(hashlib.sha256(blob).digest()).decode("ascii").rstrip("=")
    return {"type": key.get_name(), "fingerprint_sha256": fp,
            "key_b64": base64.b64encode(blob).decode("ascii")}


def _same_fingerprint(a: Optional[str], b: Optional[str]) -> bool:
    """"SHA256:abc" and "abc" (and stray padding / spaces) are the same print."""
    def norm(x):
        x = (x or "").strip()
        if x.upper().startswith("SHA256:"):
            x = x[7:]
        return x.rstrip("=").strip()
    return bool(norm(a)) and norm(a) == norm(b)


_PEM_RE = re.compile(r"-----BEGIN ([A-Z0-9 ]+)-----(.*?)-----END \1-----", re.S)


def _repair_pem(text: str) -> str:
    """Undo what pasting does to a key: CRLF, indentation, and a body squashed
    onto one line. A PEM with headers (Proc-Type:) is only line-ended."""
    text = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    m = _PEM_RE.search(text)
    if not m or ":" in m.group(2):
        return "\n".join(line.strip() for line in text.split("\n")) + "\n"
    label, body = m.group(1), re.sub(r"\s+", "", m.group(2))
    lines = [body[i:i + 64] for i in range(0, len(body), 64)]
    return f"-----BEGIN {label}-----\n" + "\n".join(lines) + f"\n-----END {label}-----\n"


def load_private_key(text: str, passphrase: Optional[str] = None):
    """A pasted private key -> a paramiko key. Raises PullError("auth", …).

    Accepts the OpenSSH format and PEM — PKCS#1 ("BEGIN RSA PRIVATE KEY"),
    PKCS#8 ("BEGIN PRIVATE KEY", encrypted or not) and EC — for RSA, Ed25519 and
    ECDSA keys. Everything is read with `cryptography` and handed to paramiko as
    an unencrypted OpenSSH key in memory, because paramiko alone cannot read
    PKCS#8.
    """
    paramiko = _paramiko()
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import dsa, ec, ed25519, rsa

    raw = str(text or "").strip()
    if raw.startswith("PuTTY-User-Key-File"):
        raise PullError("auth", "This is a PuTTY key (.ppk). Open it in PuTTYgen, choose "
                                "Conversions → Export OpenSSH key, and paste that instead.")
    if (raw.startswith(("ssh-rsa ", "ssh-ed25519 ", "ecdsa-sha2-", "ssh-dss "))
            or "BEGIN SSH2 PUBLIC KEY" in raw or "BEGIN PUBLIC KEY" in raw):
        raise PullError("auth", "This is a PUBLIC key. Kavachio needs the PRIVATE key — the "
                                "file without .pub, starting “-----BEGIN … PRIVATE KEY-----”. "
                                "The public key is the one that goes on the server.")
    if "PRIVATE KEY" not in raw:
        raise PullError("auth", "This does not look like a private key. Paste the whole key, "
                                "including its BEGIN and END lines.")
    data = _repair_pem(raw).encode("utf-8")
    pw = passphrase.encode("utf-8") if passphrase else None

    def _load(password):
        import warnings
        with warnings.catch_warnings():
            # DSA loads with a deprecation warning; it is refused below anyway.
            warnings.simplefilter("ignore")
            if b"OPENSSH PRIVATE KEY" in data:
                return serialization.load_ssh_private_key(data, password=password)
            return serialization.load_pem_private_key(data, password=password)

    try:
        try:
            key = _load(pw)
        except TypeError as exc:
            msg = str(exc).lower()
            if "not encrypted" in msg and pw is not None:
                key = _load(None)          # a passphrase given for a plain key: forgive it
            elif "encrypted" in msg or "password" in msg:
                raise PullError("auth", "This key is protected by a passphrase. "
                                        "Enter the passphrase as well.")
            else:
                raise
    except PullError:
        raise
    except ValueError as exc:
        if pw is not None or "password" in str(exc).lower() or "decrypt" in str(exc).lower():
            raise PullError("auth", "The key could not be opened — the passphrase may be "
                                    "wrong, or the key was damaged when it was copied.")
        raise PullError("auth", "This key could not be read. Paste the whole private key in "
                                "OpenSSH or PEM format, including its BEGIN and END lines.")
    except Exception as exc:  # noqa: BLE001 — UnsupportedAlgorithm and friends
        raise PullError("auth", f"This kind of key is not supported ({exc}). "
                                "Use an Ed25519, ECDSA or RSA key.")

    if isinstance(key, dsa.DSAPrivateKey):
        raise PullError("auth", "DSA keys are no longer accepted by modern SFTP software. "
                                "Use an Ed25519 or RSA key.")
    if isinstance(key, rsa.RSAPrivateKey):
        cls = paramiko.RSAKey
    elif isinstance(key, ed25519.Ed25519PrivateKey):
        cls = paramiko.Ed25519Key
    elif isinstance(key, ec.EllipticCurvePrivateKey):
        if key.curve.name not in ("secp256r1", "secp384r1", "secp521r1"):
            raise PullError("auth", f"ECDSA keys on the {key.curve.name} curve are not "
                                    "supported. Use Ed25519, RSA or ECDSA P-256/384/521.")
        cls = paramiko.ECDSAKey
    else:
        raise PullError("auth", "This kind of key is not supported. Use an Ed25519, ECDSA "
                                "or RSA key.")
    pem = key.private_bytes(serialization.Encoding.PEM,
                            serialization.PrivateFormat.OpenSSH,
                            serialization.NoEncryption()).decode("ascii")
    try:
        return cls.from_private_key(io.StringIO(pem))
    except Exception as exc:  # noqa: BLE001
        raise PullError("auth", f"This key could not be used ({exc}).")


# ── connecting ──────────────────────────────────────────────────────────────

def _address_allowed(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip.split("%")[0])
    except ValueError:
        return False
    if getattr(addr, "ipv4_mapped", None):
        addr = addr.ipv4_mapped
    # Link-local is where cloud metadata services live (169.254.169.254); a
    # connection test aimed there is never a broker's server.
    if addr.is_link_local or addr.is_multicast or addr.is_unspecified or addr.is_reserved:
        return False
    if (addr.is_loopback or addr.is_private) and not _allow_private():
        return False
    return True


def _resolve(host: str, port: int) -> list:
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except (socket.gaierror, UnicodeError):
        raise PullError("dns", f"We could not find a server called {host}. "
                               "Check the host name for typing mistakes.")
    allowed = [ai for ai in infos if _address_allowed(ai[4][0])]
    if not allowed:
        ip = infos[0][4][0] if infos else host
        raise PullError("refused", f"{host} points to a private or internal address ({ip}). "
                                   "Kavachio only connects to servers it can reach over the "
                                   "internet.")
    return allowed


def _socket_problem(host: str, port: int, exc: Optional[BaseException]) -> PullError:
    if isinstance(exc, ConnectionRefusedError):
        return PullError("refused", f"{host} refused the connection on port {port}. Check the "
                                    "port number (SFTP is usually 22) and that the server is "
                                    "running.")
    if isinstance(exc, (socket.timeout, TimeoutError)):
        return PullError("timeout", f"{host} did not answer on port {port} within "
                                    f"{CONNECT_TIMEOUT} seconds. A firewall is probably "
                                    "blocking Kavachio — ask the server's owner to allow "
                                    "Kavachio's address.")
    if isinstance(exc, OSError) and exc.errno in (errno.ENETUNREACH, errno.EHOSTUNREACH):
        return PullError("timeout", f"We could not reach {host} — there is no network path to "
                                    "it from Kavachio. Check the host name, and that the "
                                    "server accepts connections from the internet.")
    return PullError("refused", f"We could not connect to {host} on port {port}"
                                + (f" ({exc})." if exc else "."))


def _connect_socket(host: str, port: int) -> socket.socket:
    last: Optional[BaseException] = None
    for family, socktype, proto, _, sockaddr in _resolve(host, port):
        sock = socket.socket(family, socktype, proto)
        sock.settimeout(CONNECT_TIMEOUT)
        try:
            sock.connect(sockaddr)
            return sock
        except OSError as exc:
            sock.close()
            last = exc
    raise _socket_problem(host, port, last)


def _greeting(sock: socket.socket, host: str, port: int) -> bytes:
    """Peek at the server's first words without consuming them (paramiko then
    reads the same bytes). Separates three failures that otherwise all look
    like "SSH banner error": a silent port, a server that hangs up at once (an
    address allow-list), and a server that is not SFTP at all (FTP says "220")."""
    sock.settimeout(BANNER_TIMEOUT)
    try:
        data = sock.recv(256, socket.MSG_PEEK)
    except (socket.timeout, TimeoutError):
        raise PullError("timeout", f"We connected to {host} on port {port}, but no SFTP "
                                   f"greeting came within {BANNER_TIMEOUT} seconds. The port "
                                   "may be wrong (SFTP is usually 22), or the server is "
                                   "overloaded.")
    except OSError:
        data = b""
    if not data:
        raise PullError("refused", f"{host} closed the connection straight away. It "
                                   "probably only accepts connections from addresses it "
                                   "knows — ask its owner to allow Kavachio's address.")
    return data


def _handshake_problem(exc: BaseException, host: str, port: int,
                       greeting: bytes = b"") -> PullError:
    paramiko = _paramiko()
    text = str(exc)
    if isinstance(exc, paramiko.IncompatiblePeer):
        return PullError("protocol", "This server only offers old SHA-1 security (ssh-rsa / "
                                     "SHA-1 key exchange), which is no longer considered "
                                     "safe. Ask its owner to enable an Ed25519, ECDSA or "
                                     "rsa-sha2-256/512 host key and a SHA-2 key exchange.")
    if "banner" in text.lower() or "Indecipherable" in text or "Invalid SSH banner" in text \
            or "Incompatible version" in text:
        first = greeting.split(b"\n", 1)[0].strip()[:60].decode("latin-1", "replace")
        if greeting[:3] == b"220" or b"FTP" in greeting[:200].upper():
            return PullError("protocol", f"This is an FTP server (it answered “{first}”), not "
                                         "SFTP. Kavachio collects over SFTP only — usually "
                                         "port 22.")
        if greeting and not greeting.startswith(b"SSH-"):
            return PullError("protocol", f"Something answered on port {port}, but it is not "
                                         "an SFTP server. Check the port — SFTP is usually 22.")
        cause = exc.__cause__ or exc.__context__
        if isinstance(cause, (EOFError, ConnectionResetError, BrokenPipeError)):
            return PullError("refused", f"{host} closed the connection during the greeting. "
                                        "It may only accept known addresses.")
        return PullError("timeout", f"{host} stopped answering during the greeting.")
    if "No existing session" in text or isinstance(exc, (socket.timeout, TimeoutError)):
        return PullError("timeout", f"{host} did not finish the secure handshake within "
                                    f"{HANDSHAKE_TIMEOUT} seconds.")
    if isinstance(exc, (EOFError, ConnectionResetError, BrokenPipeError)):
        return PullError("refused", f"{host} closed the connection during the secure "
                                    "handshake. It may only accept known addresses.")
    return PullError("protocol", f"The secure handshake with {host} failed: "
                                 f"{text or type(exc).__name__}.")


def _pin_key_type(transport, key_type: Optional[str]) -> None:
    """Accept ONLY the pinned key's algorithm. Servers have several host keys
    (OpenSSH and SFTPGo: one each of Ed25519, ECDSA, RSA) and paramiko picks by
    its own preference, so a server that adds a key — or a paramiko upgrade —
    would otherwise present a different, equally genuine key and raise a false
    "the key changed" alarm. RSA is pinned as its SHA-2 signature names."""
    if not key_type:
        return
    names = ("rsa-sha2-512", "rsa-sha2-256") if key_type == "ssh-rsa" else (key_type,)
    try:
        opts = transport.get_security_options()
        usable = tuple(n for n in names if n in transport._key_info)
        if usable:
            opts.key_types = usable
    except Exception:  # noqa: BLE001 — then the fingerprint check alone decides
        pass


class _Conn:
    """An authenticated SFTP session. Always closed — `with _open(...) as c:`."""

    def __init__(self, transport, sftp, host_key: dict):
        self.transport, self.sftp, self.host_key = transport, sftp, host_key

    def close(self) -> None:
        for thing in (self.sftp, self.transport):
            try:
                if thing is not None:
                    thing.close()
            except Exception:  # noqa: BLE001
                pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


def _handshake(settings: dict, *, pinned: Optional[dict] = None,
               expect_fingerprint: Optional[str] = None,
               seen: Optional[Callable[[dict], None]] = None):
    """TCP, the SSH key exchange and the host-key check -> (transport, key info).
    Nothing secret has been sent when this returns — or raises."""
    paramiko = _paramiko()
    host, port = settings["host"], int(settings["port"])
    sock = _connect_socket(host, port)
    try:
        greeting = _greeting(sock, host, port)
        transport = paramiko.Transport(sock)
    except PullError:
        sock.close()
        raise
    except Exception as exc:  # noqa: BLE001
        sock.close()
        raise _handshake_problem(exc, host, port)
    transport.banner_timeout = BANNER_TIMEOUT
    transport.handshake_timeout = HANDSHAKE_TIMEOUT
    transport.auth_timeout = AUTH_TIMEOUT
    try:
        if pinned:
            _pin_key_type(transport, pinned.get("type"))
        try:
            transport.start_client(timeout=HANDSHAKE_TIMEOUT)
        except paramiko.IncompatiblePeer as exc:
            if pinned and pinned.get("type") and "host key" in str(exc).lower():
                raise PullError(
                    "host_key",
                    f"The server no longer offers the kind of identity key this channel "
                    f"trusts ({pinned.get('type')}, {pinned.get('fingerprint_sha256')}). "
                    "This happens when a server is rebuilt or reconfigured — or when "
                    "something is pretending to be it. Nothing was sent to it. Confirm with "
                    "the server's owner, then set the channel up again.")
            raise _handshake_problem(exc, host, port, greeting)
        except Exception as exc:  # noqa: BLE001
            raise _handshake_problem(exc, host, port, greeting)
        if not transport.is_active() or not getattr(transport, "initial_kex_done", True):
            raise PullError("timeout", f"{host} did not finish the secure handshake within "
                                       f"{HANDSHAKE_TIMEOUT} seconds.")
        info = host_key_info(transport.get_remote_server_key())
        if seen is not None:
            seen(info)
        if pinned and pinned.get("key_b64") != info["key_b64"]:
            raise PullError(
                "host_key",
                f"The server's identity key has changed since this channel was set up "
                f"(it was {pinned.get('fingerprint_sha256')}, now {info['fingerprint_sha256']}). "
                "This happens when a server is rebuilt or moved — or when something is "
                "pretending to be it. Nothing was sent to it. Confirm the new fingerprint "
                "with the server's owner, then set the channel up again.")
        if expect_fingerprint and not _same_fingerprint(expect_fingerprint,
                                                        info["fingerprint_sha256"]):
            raise PullError(
                "host_key",
                f"The server's identity key is not the one you tested "
                f"(tested {expect_fingerprint}, now {info['fingerprint_sha256']}). "
                "Test the connection again before saving.")
        try:
            transport.set_keepalive(KEEPALIVE_SECONDS)
        except Exception:  # noqa: BLE001
            pass
        return transport, info
    except BaseException:
        transport.close()
        raise


def _auth_problem(exc: BaseException, settings: dict, key_type: Optional[str] = None) -> PullError:
    paramiko = _paramiko()
    user = settings["username"]
    if isinstance(exc, paramiko.BadAuthenticationType):
        allowed = list(getattr(exc, "allowed_types", []) or [])
        if settings["auth"] == "password" and "publickey" in allowed:
            return PullError("auth", f"The server does not accept a password for {user} — it "
                                     "wants a key. Choose Key and paste the private key.")
        if settings["auth"] == "key" and ({"password", "keyboard-interactive"} & set(allowed)):
            return PullError("auth", f"The server does not accept a key for {user} — it wants "
                                     "a password.")
        return PullError("auth", f"The server does not allow this way of logging in for {user}"
                                 + (f" (it accepts: {', '.join(allowed)})." if allowed else "."))
    if "timeout" in str(exc).lower():
        return PullError("timeout", "The server took too long to check the login.")
    if settings["auth"] == "key":
        extra = ""
        if key_type == "ssh-ed25519" and str(settings.get("host", "")).lower().endswith(
                ".blob.core.windows.net"):
            extra = " Azure Blob SFTP accepts RSA and ECDSA keys only, not Ed25519."
        return PullError("auth", f"The server did not accept this key for {user}. Check the "
                                 "user name, and that the matching public key is installed "
                                 f"for this user on the server.{extra}")
    return PullError("auth", f"The server did not accept the user name and password for "
                             f"{user}. Check both — passwords are case-sensitive.")


def _answer_with(password: str, second_factor: list):
    """Keyboard-interactive: answer every prompt with the password — unless it
    asks for a one-time code, which a saved password cannot give."""
    def handler(title, instructions, prompts):
        answers = []
        for prompt in prompts:
            text = (prompt[0] if isinstance(prompt, (tuple, list)) else str(prompt)) or ""
            if any(w in text.lower() for w in _SECOND_FACTOR_WORDS):
                second_factor.append(text)
                answers.append("")
            else:
                answers.append(password)
        return answers
    return handler


def _open(settings: dict, *, secret: str, passphrase: Optional[str] = None,
          pinned: Optional[dict] = None, expect_fingerprint: Optional[str] = None,
          seen: Optional[Callable[[dict], None]] = None) -> _Conn:
    """Connect, check the server is who it should be, log in, start SFTP.

    The host key is checked BEFORE the password or key is offered: a server
    that is not the pinned one never sees the secret.

    ONE login attempt per connection. Go-based servers (SFTPGo, many cloud
    gateways) drop the connection on a second attempt, which is why paramiko's
    own password -> keyboard-interactive fallback fails against them. A server
    that takes the password only as keyboard-interactive gets it on a FRESH
    connection whose key is checked again — it must be the very same key.
    """
    paramiko = _paramiko()
    user = settings["username"]
    # Parse the key first: a key that cannot be read is the carrier's to fix,
    # and there is no reason to touch the network to say so.
    pkey = load_private_key(secret, passphrase) if settings["auth"] == "key" else None

    transport, info = _handshake(settings, pinned=pinned,
                                 expect_fingerprint=expect_fingerprint, seen=seen)
    conn = _Conn(transport, None, info)
    try:
        second_factor: list = []
        try:
            if pkey is not None:
                remaining = transport.auth_publickey(user, pkey)
            else:
                try:
                    remaining = transport.auth_password(user, secret, fallback=False)
                except paramiko.BadAuthenticationType as exc:
                    if "keyboard-interactive" not in (exc.allowed_types or []):
                        raise
                    transport.close()
                    transport, _ = _handshake(settings, pinned=pinned or info)
                    conn.transport = transport
                    remaining = transport.auth_interactive(
                        user, _answer_with(secret, second_factor))
        except paramiko.AuthenticationException as exc:
            if second_factor:
                raise PullError("auth", "The server asks for a one-time code as well as the "
                                        "password. Kavachio collects unattended and cannot "
                                        "answer that — ask for a login without two-step "
                                        "verification for this user.")
            raise _auth_problem(exc, settings, pkey.get_name() if pkey is not None else None)
        except PullError:
            raise
        except (paramiko.SSHException, EOFError, OSError) as exc:
            if "timeout" in str(exc).lower() or isinstance(exc, (socket.timeout, TimeoutError)):
                raise PullError("timeout", "The server took too long to check the login.")
            raise PullError("auth", f"The login was interrupted: {exc or type(exc).__name__}.")
        if remaining:
            raise PullError("auth", "The server asks for a second login step after this one "
                                    f"(it wants: {', '.join(remaining)}). Kavachio logs in "
                                    "with one method — ask the server's owner to allow a "
                                    "single method for this user.")
        if not conn.transport.is_authenticated():
            raise _auth_problem(paramiko.AuthenticationException("rejected"), settings)

        try:
            sftp = paramiko.SFTPClient.from_transport(conn.transport)
        except Exception as exc:  # noqa: BLE001
            sftp = None
            log.info("sftp subsystem refused on %s: %s", settings["host"], exc)
        if sftp is None:
            raise PullError("protocol", "We logged in, but the server does not offer SFTP to "
                                        "this user (it may allow only a shell or SCP). Ask "
                                        "its owner to enable SFTP for this user.")
        # Without this a server that stalls mid-transfer would hold the one
        # scheduler thread for ever.
        sftp.get_channel().settimeout(IO_TIMEOUT)
        conn.sftp = sftp
        return conn
    except BaseException:
        conn.close()
        raise


# ── folders and files on the remote server ──────────────────────────────────

def _io_code(exc: BaseException) -> Optional[int]:
    return exc.errno if isinstance(exc, (IOError, OSError)) else None


def _io_words(exc: BaseException) -> str:
    """An SFTP error as a short phrase."""
    code = _io_code(exc)
    if code == errno.ENOENT:
        return "it does not exist"
    if code == errno.EACCES:
        return "permission denied"
    if isinstance(exc, (socket.timeout, TimeoutError)):
        return "the server stopped answering"
    text = (exc.args[-1] if getattr(exc, "args", None) else "") or str(exc)
    return str(text).strip() or type(exc).__name__


def _rjoin(base: str, name: str) -> str:
    if base in ("", "."):
        return name
    if base.endswith("/"):
        return base + name
    return base + "/" + name


def _shown(path: str) -> str:
    return path or "the login folder"


def _no_dir(sftp, p: str) -> PullError:
    """"No such folder", with where the login actually starts — the usual cause
    is "/outgoing" typed for a server whose login starts in /home/<user>."""
    msg = f"We logged in, but there is no folder called “{_shown(p)}” on the server."
    try:
        home = sftp.normalize(".")
    except Exception:  # noqa: BLE001
        home = None
    if home:
        msg += f" Your login starts in “{home}”"
        if p.startswith("/") and home.rstrip("/") not in ("", p.rstrip("/")):
            msg += f" — try “{p.lstrip('/')}”"
        msg += "."
    return PullError("no_dir", msg + " Folder names are case-sensitive on most servers.")


def resolve_dir(sftp, path: str) -> str:
    """The folder as the server names it (absolute when the server says), or
    PullError no_dir / permission. Relative paths are relative to the folder the
    login starts in — on Azure that is the container, on AWS Transfer the user's
    home mapping, on FileZilla Server the virtual root."""
    p = _clean_path(path)
    try:
        resolved = sftp.normalize(p or ".")
    except UnicodeDecodeError:
        resolved = p or "."
    except (IOError, OSError) as exc:
        code = _io_code(exc)
        if code == errno.ENOENT:
            raise _no_dir(sftp, p)
        if code == errno.EACCES:
            raise PullError("permission", f"We logged in, but this user is not allowed to "
                                          f"open “{_shown(p)}”.")
        # A server that cannot resolve paths: use the folder as typed.
        resolved = p or "."
    return resolved or "."


def _kind(attr) -> str:
    """file | dir | link | other | unknown — from the mode bits when the server
    sends them, else from the `ls -l` style long name (Azure sends no mode)."""
    mode = getattr(attr, "st_mode", None)
    if mode is not None and statmod.S_IFMT(mode):
        if statmod.S_ISREG(mode):
            return "file"
        if statmod.S_ISDIR(mode):
            return "dir"
        if statmod.S_ISLNK(mode):
            return "link"
        return "other"
    first = (getattr(attr, "longname", None) or "")[:1]
    return {"-": "file", "d": "dir", "l": "link"}.get(first, "unknown")


def is_skipped_name(name: str) -> bool:
    """Hidden files, uploads still wearing a temporary name, Office lock files."""
    return (not name or name in (".", "..") or sftp_watch.is_temp_name(name)
            or name.lower().endswith(_EXTRA_TEMP_SUFFIXES) or name.startswith("~$"))


def _list_attrs(sftp, folder: str) -> tuple[list, int]:
    """listdir_attr, but one file name that is not UTF-8 costs that one file,
    not the whole folder -> (attributes, names that could not be read).

    paramiko decodes every name as UTF-8 and gives up on the entire listing at
    the first that is not (a Windows server's "café.csv" in Latin-1). On that
    error the listing is read again here with the same three requests
    listdir_attr makes, leaving out only the unreadable names."""
    try:
        return sftp.listdir_attr(folder), 0
    except UnicodeDecodeError:
        pass
    import paramiko
    from paramiko.sftp import CMD_CLOSE, CMD_HANDLE, CMD_NAME, CMD_OPENDIR, CMD_READDIR
    t, msg = sftp._request(CMD_OPENDIR, folder)
    if t != CMD_HANDLE:
        raise IOError("the server gave an unexpected answer to a folder listing")
    handle = msg.get_binary()
    out, bad = [], 0
    try:
        while True:
            try:
                t, msg = sftp._request(CMD_READDIR, handle)
            except EOFError:
                break
            if t != CMD_NAME:
                raise IOError("the server gave an unexpected answer to a folder listing")
            for _ in range(msg.get_int()):
                raw, long_raw = msg.get_string(), msg.get_string()
                attr = paramiko.SFTPAttributes._from_msg(msg)
                try:
                    name = raw.decode("utf-8")
                except UnicodeDecodeError:
                    bad += 1
                    continue
                if name in (".", ".."):
                    continue
                attr.filename = name
                attr.longname = long_raw.decode("utf-8", "replace")
                out.append(attr)
    finally:
        try:
            sftp._request(CMD_CLOSE, handle)
        except Exception:  # noqa: BLE001
            pass
    return out, bad


def snapshot(sftp, folder: str, notes: Optional[list] = None) -> dict[str, dict]:
    """{name: {size, mtime, kind}} for the files we might collect in `folder`.
    Directories, links and skipped names are left out. Anything worth telling
    the carrier (unreadable names) is appended to `notes`."""
    try:
        attrs, bad = _list_attrs(sftp, folder)
    except (IOError, OSError) as exc:
        code = _io_code(exc)
        if code == errno.ENOENT:
            raise _no_dir(sftp, folder)
        if code == errno.EACCES:
            raise PullError("permission", f"We logged in, but this user is not allowed to "
                                          f"list the files in “{folder}”.")
        raise PullError("no_dir", f"The server would not list “{folder}” "
                                  f"({_io_words(exc)}). Check that it is a folder.")
    if bad and notes is not None:
        notes.append(f"{bad} file name(s) in this folder are not UTF-8 text, so they "
                     "cannot be collected — ask the sender to rename them.")
    out: dict[str, dict] = {}
    for a in attrs:
        name = a.filename
        if is_skipped_name(name):
            continue
        kind = _kind(a)
        if kind == "unknown":
            kind = _probe_kind(sftp, _rjoin(folder, name))
        if kind not in ("file", "unknown"):
            continue
        out[name] = {"size": a.st_size, "mtime": a.st_mtime, "kind": kind}
    return out


def _probe_kind(sftp, path: str) -> str:
    """For a listing that carried no mode bits and no long name: ask once more,
    then try opening it as a folder. A folder (our own `processed`, say) must
    never be mistaken for a file to download."""
    try:
        kind = _kind(sftp.stat(path))
        if kind != "unknown":
            return kind
    except (IOError, OSError):
        pass
    try:
        sftp.listdir(path)
        return "dir"
    except (IOError, OSError, UnicodeDecodeError):
        return "unknown"


def settled(first: dict, second: dict, now_ts: float, quiet: int) -> tuple[list[str], int]:
    """Which files are finished: in BOTH listings with the same size and time,
    and not modified within `quiet` seconds of now (either way — a server clock
    running ahead must not make a file look old). Returns (names, still_writing).

    Size AND time, because some servers keep only minutes (FileZilla) or keep
    the time the file had on the sender's PC (WinSCP preserves it): a growing
    file is caught by its size even when its time says nothing.
    """
    ready, waiting = [], 0
    for name, now_ in second.items():
        before = first.get(name)
        if before is None or (before["size"], before["mtime"]) != (now_["size"], now_["mtime"]):
            waiting += 1
            continue
        mtime = now_["mtime"]
        if mtime is not None and abs(now_ts - mtime) < quiet:
            waiting += 1
            continue
        if now_["size"] == 0 and (mtime is None or now_ts - mtime < EMPTY_GRACE_SECONDS):
            waiting += 1
            continue
        ready.append(name)
    # Oldest first, so versions land in order; files with no time at all last.
    ready.sort(key=lambda n: (second[n]["mtime"] is None, second[n]["mtime"] or 0, n))
    return ready, waiting


def idempotency_key(route_id: int, path: str, size, mtime) -> str:
    """sha256(route_id|remote path|size|mtime). The same file, unchanged, gets
    the same key — so a file that was landed but could not be moved away is
    never landed a second time."""
    return hashlib.sha256(f"{route_id}|{path}|{size}|{mtime}".encode("utf-8")).hexdigest()


def _exists(sftp, path: str) -> bool:
    """True unless the server says plainly that nothing is there. Any other
    answer counts as "there", so we never write over a file we could not see."""
    try:
        sftp.stat(path)
        return True
    except (IOError, OSError) as exc:
        return _io_code(exc) != errno.ENOENT


def _is_dir(sftp, path: str) -> Optional[bool]:
    try:
        a = sftp.stat(path)
    except (IOError, OSError) as exc:
        if _io_code(exc) == errno.ENOENT:
            return None
        raise
    kind = _kind(a)
    return True if kind in ("dir", "unknown") else False


def _mkdir(sftp, path: str) -> None:
    try:
        sftp.mkdir(path)
        return
    except (IOError, OSError) as first:
        # Azure Blob SFTP supports no chmod, and some servers refuse a MKDIR
        # that carries permission bits. Retry with no attributes at all.
        try:
            import paramiko
            sftp._request(paramiko.sftp.CMD_MKDIR, path, paramiko.SFTPAttributes())
            return
        except (IOError, OSError, AttributeError):
            pass
        # Somebody (another worker) made it meanwhile.
        if _is_dir(sftp, path):
            return
        raise first


def ensure_dir(sftp, path: str) -> tuple[str, bool]:
    """Make `path` (and its parents) if missing -> (resolved path, created?)."""
    p = _clean_path(path)
    if not p:
        return resolve_dir(sftp, ""), False
    state = _is_dir(sftp, p)
    if state is True:
        return resolve_dir(sftp, p), False
    if state is False:
        raise MoveFailed(f"“{p}” exists on the server but is a file, not a folder")
    parent = posixpath.dirname(p)
    if parent and parent not in ("/", ".") and parent != p:
        ensure_dir(sftp, parent)
    try:
        _mkdir(sftp, p)
    except (IOError, OSError) as exc:
        raise MoveFailed(f"could not create the folder “{p}” ({_io_words(exc)})")
    return resolve_dir(sftp, p), True


def _free_target(sftp, folder: str, name: str) -> str:
    """folder/name, or folder/name_<UTC time>.ext when that is taken. FileZilla
    Server and several MFT products refuse to rename onto an existing name, and
    posix-rename would silently replace last month's file."""
    target = _rjoin(folder, name)
    if not _exists(sftp, target):
        return target
    stem, ext = posixpath.splitext(name)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    for i in range(20):
        cand = _rjoin(folder, f"{stem}_{stamp}{'' if i == 0 else f'_{i}'}{ext}")
        if not _exists(sftp, cand):
            return cand
    return _rjoin(folder, f"{stem}_{stamp}_{secrets.token_hex(3)}{ext}")


def _copy(sftp, src: str, dst: str, size: Optional[int]) -> None:
    """Server-side copy, by reading and writing through this session. The last
    resort for servers that refuse to rename (AWS Transfer on S3)."""
    with sftp.open(src, "rb") as fr:
        if size:
            fr.prefetch(size, PREFETCH_REQUESTS)
        with sftp.open(dst, "wb") as fw:
            fw.set_pipelined(True)
            while True:
                chunk = fr.read(CHUNK)
                if not chunk:
                    break
                fw.write(chunk)
    if size is not None:
        got = sftp.stat(dst).st_size
        if got is not None and got != size:
            raise IOError(f"the copy is {got} bytes, the original {size}")


def move_file(sftp, src: str, folder: str, name: str, size: Optional[int] = None) -> str:
    """Move src into folder. posix-rename → rename → copy + delete. Returns the
    new path; raises MoveFailed in plain words."""
    target = _free_target(sftp, folder, name)
    tried: list[BaseException] = []
    for how in (sftp.posix_rename, sftp.rename):
        try:
            how(src, target)
            return target
        except (IOError, OSError) as exc:
            tried.append(exc)
            if _io_code(exc) == errno.ENOENT and not _exists(sftp, src):
                raise MoveFailed("the file disappeared from the server before it could be moved")
    if all(_io_code(e) == errno.EACCES for e in tried):
        # A login refused renames is refused uploads too — copying would only
        # leave a half-written file behind.
        raise MoveFailed("permission denied — this login may not move files there")
    try:
        _copy(sftp, src, target, size)
    except (IOError, OSError, EOFError) as exc:
        try:
            sftp.remove(target)        # only ever our own half-written copy
        except (IOError, OSError):
            pass
        raise MoveFailed(f"the server would not rename it ({_io_words(tried[-1])}) "
                         f"or let us copy it ({_io_words(exc)})")
    try:
        sftp.remove(src)
    except (IOError, OSError) as exc:
        raise MoveFailed(f"it was copied to the processed folder but the original could not "
                         f"be removed ({_io_words(exc)})")
    return target


def _remove_quietly(sftp, path: str) -> bool:
    try:
        sftp.remove(path)
        return True
    except (IOError, OSError):
        return False


class NotReady(Exception):
    """The file changed between listing and reading, or is locked by its
    uploader (Cerberus locks a file while it is written). Next time."""


def download(sftp, path: str, size_hint: Optional[int], cap: int) -> tuple[bytes, bool]:
    """Read a file, never more than cap+1 bytes -> (bytes, over the cap?).

    The bytes read must match the size the listing gave: paramiko does not
    compare with the remote size itself, and an interrupted S3 upload can leave
    a short object behind."""
    want = cap + 1
    buf = bytearray()
    with sftp.open(path, "rb") as f:
        if size_hint:
            f.prefetch(min(int(size_hint), want), PREFETCH_REQUESTS)
        while len(buf) < want:
            chunk = f.read(min(CHUNK, want - len(buf)))
            if not chunk:
                break
            buf += chunk
    data = bytes(buf)
    if len(data) <= cap and size_hint is not None and len(data) != size_hint:
        raise NotReady(f"it was {size_hint} bytes when listed and {len(data)} when read")
    return data, len(data) > cap


# ── Test (POST /intake/sftp/test) ───────────────────────────────────────────

def _probe_write(sftp, base: str, settings: dict, warnings: list) -> Optional[bool]:
    """Prove a collected file can be moved (or deleted) — with a tiny file of
    our own, never a real one. True / False, or None when we could not tell."""
    name = f"{_PROBE_PREFIX}{secrets.token_hex(4)}.tmp"
    probe = _rjoin(base, name)
    verb = "moved" if settings["after"] == "move" else "deleted"
    try:
        with sftp.open(probe, "wb") as f:
            f.write(b"Kavachio connection check - safe to delete.\n")
    except (IOError, OSError, EOFError) as exc:
        # Pick-up logins are often allowed to read, rename and delete but not
        # to upload — so on its own this says nothing either way. Unless the
        # processed folder is missing and cannot be made either: then nothing
        # can ever be moved there.
        if settings["after"] == "move" and _io_code(exc) == errno.EACCES:
            try:
                folder, created = ensure_dir(sftp, settings["processed_dir"])
            except (MoveFailed, IOError, OSError) as mexc:
                why = str(mexc) if isinstance(mexc, MoveFailed) else _io_words(mexc)
                warnings.append(
                    "This login can read the folder but may not create files or folders "
                    f"there ({why}), so collected files cannot be moved to "
                    f"“{settings['processed_dir']}”. Each file would be collected once and "
                    "then left on the server. Ask the server's owner to allow this, or "
                    "choose to delete files after collecting.")
                return False
            if created:
                warnings.append(f"We created the folder “{folder}” on the server for "
                                "collected files.")
        warnings.append(f"We could not put a test file in the folder, so we could not "
                        f"check that collected files can be {verb}. If they cannot, the "
                        "channel will say so after its first collection.")
        return None

    if settings["after"] == "delete":
        if _remove_quietly(sftp, probe):
            return True
        warnings.append("We can read the folder but this user cannot delete files from it, "
                        "so collected files would stay where they are. Ask the server's "
                        "owner to allow deleting, or choose to move files to a processed "
                        f"folder. (A test file {name} was left behind; it is safe to delete.)")
        return False

    try:
        folder, created = ensure_dir(sftp, settings["processed_dir"])
        if folder.rstrip("/") == base.rstrip("/"):
            raise MoveFailed("it is the same folder files are collected from")
        if created:
            warnings.append(f"We created the folder “{folder}” on the server for "
                            "collected files.")
        moved = move_file(sftp, probe, folder, name, size=None)
    except (MoveFailed, IOError, OSError, EOFError) as exc:
        _remove_quietly(sftp, probe)
        why = str(exc) if isinstance(exc, MoveFailed) else _io_words(exc)
        warnings.append(f"We can read the folder but could not move a file into "
                        f"“{settings['processed_dir']}”: {why}. Ask the server's owner to "
                        "let this user rename files and create folders there, or choose to "
                        "delete files after collecting.")
        return False
    if not _remove_quietly(sftp, moved):
        warnings.append(f"A small test file was left in “{folder}” ({name}); it is safe "
                        "to delete.")
    return True


def test_connection(raw: dict) -> dict:
    """The Test button. Never raises: every problem comes back as ok=False with
    plain words and one of ERROR_CODES (null for a form problem)."""
    out = {"ok": False, "fingerprint": None, "key_type": None, "files_found": None,
           "sample": [], "can_write": None, "warnings": [], "error": None,
           "error_code": None}
    try:
        st = clean_settings(raw)
    except SettingsError as exc:
        out["error"] = str(exc)
        return out

    def _seen(info: dict) -> None:
        out["fingerprint"], out["key_type"] = info["fingerprint_sha256"], info["type"]

    conn = None
    try:
        conn = _open(st, secret=st["secret"], passphrase=st["passphrase"], seen=_seen)
        base = resolve_dir(conn.sftp, st["remote_dir"])
        files = snapshot(conn.sftp, base, out["warnings"])
        out["files_found"] = len(files)
        out["sample"] = sorted(files)[:10]
        out["ok"] = True
        if len(files) > 10:
            out["warnings"].append(
                f"There are {len(files)} files in this folder now. All of them will be "
                f"collected at the first check, then "
                f"{'moved to the processed folder' if st['after'] == 'move' else 'deleted'}. "
                "If some are old, move them out first, or choose a folder that only "
                "receives new bordereaux.")
        out["can_write"] = _probe_write(conn.sftp, base, st, out["warnings"])
    except PullError as exc:
        out["error"], out["error_code"] = exc.message, exc.code
    except Exception as exc:  # noqa: BLE001 — the Test button must always answer
        log.warning("sftp test of %s failed unexpectedly", st.get("host"), exc_info=True)
        out["error"] = f"The test stopped unexpectedly: {exc or type(exc).__name__}."
        out["error_code"] = "protocol"
    finally:
        if conn is not None:
            conn.close()
    return out


# ── Create (POST /intake/routes, channel sftp) ─────────────────────────────

class SetupRefused(Exception):
    """Plain words for a 400 — the route is not created."""


def prepare_route(raw: dict) -> tuple[str, dict]:
    """Validate, test again against the fingerprint the carrier saw, and build
    the settings to store -> (address, config). Raises SetupRefused.

    Testing again is not a formality: the fingerprint is what is pinned, so it
    has to be the key this server presents now, and the folder has to be there.
    """
    try:
        st = clean_settings(raw, for_create=True)
    except SettingsError as exc:
        raise SetupRefused(str(exc))
    try:
        with _open(st, secret=st["secret"], passphrase=st["passphrase"],
                   expect_fingerprint=st["fingerprint"]) as conn:
            base = resolve_dir(conn.sftp, st["remote_dir"])
            snapshot(conn.sftp, base)
            host_key = conn.host_key
    except PullError as exc:
        raise SetupRefused(exc.message)
    except Exception as exc:  # noqa: BLE001
        log.warning("sftp setup check of %s failed unexpectedly", st["host"], exc_info=True)
        raise SetupRefused(f"The connection check failed: {exc or type(exc).__name__}.")

    cfg = {
        "host": st["host"], "port": st["port"], "username": st["username"],
        "auth": st["auth"],
        "secret_enc": intake_secrets.encrypt(st["secret"]),
        "passphrase_enc": intake_secrets.encrypt(st["passphrase"]) if st["passphrase"] else None,
        "remote_dir": st["remote_dir"], "after": st["after"],
        "processed_dir": st["processed_dir"],
        "interval_minutes": st["interval_minutes"],
        "host_key": host_key,
        "last_checked_at": None, "last_error": None, "last_collected": None,
    }
    return build_address(st), cfg


# ── Collect (the scheduler, and POST /intake/routes/{id}/poll) ──────────────

def _try_lock(session, route_id: int):
    """One worker per route, across every replica — a transaction-scoped
    advisory lock on a DEDICATED connection (sftp_poller explains both
    choices). Returns a release function, or None when somebody else has it."""
    lock_key = (_LOCK_NAMESPACE << 32) | (int(route_id) & 0xFFFFFFFF)
    conn = session.get_bind().connect()
    try:
        txn = conn.begin()
        got = conn.execute(_sa_text("SELECT pg_try_advisory_xact_lock(:k)"),
                           {"k": lock_key}).scalar()
    except Exception:
        conn.close()
        raise
    if not got:
        txn.rollback()
        conn.close()
        return None

    def release() -> None:
        try:
            txn.rollback()
        finally:
            conn.close()
    return release


def _already_landed(session, route, key: str) -> bool:
    from intake_models import FileArrival
    return session.query(FileArrival.id).filter(
        FileArrival.tenant_id == route.tenant_id,
        FileArrival.route_id == route.id,
        FileArrival.idempotency_key == key).first() is not None


def _land(session, route, cfg: dict, name: str, data: bytes, key: str, *,
          oversize: bool, real_size: Optional[int], cap: int):
    """Hand one file to the landing every channel shares."""
    import intake_service as svc
    import storage
    claimed = f"sftp://{cfg.get('username')}@{cfg.get('host')}"
    if oversize:
        # Never held in memory, never stored: refused on its size alone, with
        # the size it really is.
        arrival = svc.land_file(
            session, tenant_id=route.tenant_id, filename=name, file_bytes=b"",
            declared_size=real_size or (cap + 1), route=route, claimed_sender=claimed,
            idempotency_key=key, max_bytes=cap)
        arrival.file_hash_sha256 = None       # nothing was read, so nothing was hashed
        session.flush()
        return arrival
    blob_ref = None
    try:
        # Our own copy before anything is decided — what Files Received's
        # download reads, and what survives the remote file being moved on.
        blob_ref, _ = storage.store_or_keep("intake", route.tenant_id, name, data)
    except Exception as exc:  # noqa: BLE001 — losing the copy must not lose the record
        log.error("could not store %s: %s", name, exc)
    return svc.land_file(
        session, tenant_id=route.tenant_id, filename=name, file_bytes=data, route=route,
        blob_ref=blob_ref, claimed_sender=claimed, idempotency_key=key, max_bytes=cap)


_stop = threading.Event()


def _collect(session, route, cfg: dict, summary: dict) -> tuple[int, Optional[str]]:
    import intake_safety as safety
    import intake_service as svc

    try:
        secret = intake_secrets.decrypt(cfg.get("secret_enc") or "")
        passphrase = (intake_secrets.decrypt(cfg["passphrase_enc"])
                      if cfg.get("passphrase_enc") else None)
    except intake_secrets.SecretUnreadable as exc:
        raise PullError("auth", str(exc))

    pinned = cfg.get("host_key") or {}
    if not pinned.get("key_b64"):
        # Never connect to a pull route without a pinned key: that would be
        # trusting whatever answers at that address with the password.
        raise PullError("host_key", "No server identity key was saved for this channel, so "
                                    "Kavachio will not log in to it. Set the channel up again.")

    landed, problems, notes = 0, [], []
    settings = {"host": cfg.get("host"), "port": cfg.get("port") or 22,
                "username": cfg.get("username"), "auth": cfg.get("auth") or "password"}
    with _open(settings, secret=secret, passphrase=passphrase, pinned=pinned) as conn:
        sftp = conn.sftp
        base = resolve_dir(sftp, cfg.get("remote_dir") or "")
        where = base if base.startswith("/") else "/" + base
        summary["looked_in"] = (f"sftp://{settings['username']}@{settings['host']}:"
                                f"{settings['port']}{where}")
        first = snapshot(sftp, base, notes)
        if not first:
            return 0, (" ".join(notes) or None)
        # A file being uploaded looks exactly like a finished one, only
        # shorter. Look twice, `quiet` seconds apart, and take only what did
        # not change in between.
        quiet = svc.quiet_seconds()
        if _stop.wait(quiet):
            return 0, None
        second = snapshot(sftp, base)
        names, waiting = settled(first, second, time.time(), quiet)
        summary["skipped_still_writing"] = waiting
        not_ready: list[str] = []
        cap = safety.max_bytes()
        after = cfg.get("after") or "move"
        folder: Optional[str] = None
        folder_problem: Optional[str] = None

        for name in names[:_max_files()]:
            entry = second[name]
            path = _rjoin(base, name)
            key = idempotency_key(route.id, path, entry["size"], entry["mtime"])
            if _already_landed(session, route, key):
                # Landed on an earlier pass whose move / delete failed. Not
                # landed again — only retired.
                summary["already_seen"] += 1
            else:
                oversize = entry["size"] is not None and entry["size"] > cap
                data = b""
                if not oversize:
                    try:
                        data, oversize = download(sftp, path, entry["size"], cap)
                    except NotReady as exc:
                        # Changed between the listing and the read: looked at
                        # again next time, under its new size / time.
                        log.info("route %s: %s not ready: %s", route.id, name, exc)
                        not_ready.append(name)
                        continue
                    except (IOError, OSError, EOFError) as exc:
                        if _io_code(exc) == errno.EACCES:
                            problems.append(f"could not read {name} (permission denied)")
                        else:
                            # Locked by its uploader, or gone: next time.
                            log.info("route %s: could not read %s yet: %s",
                                     route.id, name, _io_words(exc))
                            not_ready.append(name)
                        continue
                try:
                    arrival = _land(session, route, cfg, name, data, key,
                                    oversize=oversize, real_size=entry["size"], cap=cap)
                    session.commit()
                except Exception:
                    session.rollback()
                    raise
                landed += 1
                summary[arrival.outcome] = summary.get(arrival.outcome, 0) + 1
                summary["files"].append({"filename": name, "outcome": arrival.outcome,
                                         "reason": arrival.turned_away_reason,
                                         "arrival_id": arrival.id})

            # The row is committed, so the file is accounted for whatever
            # happens next — the order sftp_poller keeps for the same reason.
            try:
                if after == "delete":
                    try:
                        sftp.remove(path)
                    except (IOError, OSError) as exc:
                        raise MoveFailed(f"could not delete it ({_io_words(exc)})")
                else:
                    if folder is None and folder_problem is None:
                        try:
                            folder, _ = ensure_dir(sftp, cfg.get("processed_dir")
                                                   or default_processed_dir(base))
                        except (MoveFailed, IOError, OSError) as exc:
                            folder_problem = str(exc) if isinstance(exc, MoveFailed) else _io_words(exc)
                    if folder_problem:
                        raise MoveFailed(folder_problem)
                    move_file(sftp, path, folder, name, size=entry["size"])
            except (MoveFailed, IOError, OSError, EOFError) as exc:
                why = str(exc) if isinstance(exc, MoveFailed) else _io_words(exc)
                log.warning("route %s: landed %s but could not %s it: %s",
                            route.id, name, after, why)
                problems.append(f"{name}: {why}")

        if len(names) > _max_files():
            summary["more_waiting"] = len(names) - _max_files()
        if not_ready:
            summary["skipped_still_writing"] += len(not_ready)
            summary["not_ready"] = not_ready

    parts = []
    if problems:
        verb = "move" if (cfg.get("after") or "move") == "move" else "delete"
        parts.append(f"Collected, but {len(problems)} file(s) could not be dealt with on "
                     f"the server — first: {problems[0]}. A file that was collected is "
                     f"never collected twice, but it stays in the folder until we can "
                     f"{verb} it.")
    parts.extend(notes)
    return landed, (" ".join(parts) or None)


def collect_route(session, route) -> dict:
    """Collect one pull route once. The same summary shape as
    sftp_poller.collect_route, plus `already_seen`, `error_code` and `more_waiting`.
    Never raises for a server problem: it is the summary's `error`, and the
    route's last_error."""
    summary = {"route_id": route.id, "address": route.address, "looked_in": None,
               "accepted": 0, "held": 0, "turned_away": 0, "skipped_still_writing": 0,
               "already_seen": 0, "files": []}
    cfg = load_config(session, route.id)
    if not cfg:
        summary["error"] = ("This channel has no saved server settings (or database update "
                            "35 has not been run), so there is nothing to collect from.")
        return summary
    if not route.is_enabled:
        summary["error"] = "This channel is switched off, so nothing was collected."
        return summary
    release = _try_lock(session, route.id)
    if release is None:
        summary["skipped"] = "another worker is already collecting from this server"
        return summary

    landed, error = 0, None
    try:
        landed, error = _collect(session, route, cfg, summary)
    except PullError as exc:
        error = exc.message
        summary["error_code"] = exc.code
    except Exception as exc:  # noqa: BLE001 — one broken server must not stop the rest
        log.exception("collecting pull route %s failed", route.id)
        error = f"Collection stopped unexpectedly: {exc or type(exc).__name__}."
    finally:
        release()
    if error:
        summary["error"] = error
    _save_status(session, route.id, {
        "last_checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds")
                           .replace("+00:00", "Z"),
        "last_error": error,
        "last_collected": landed,
    })
    return summary


# ── the scheduler ───────────────────────────────────────────────────────────

_thread: Optional[threading.Thread] = None
_mode = "off"


def _parse_iso(value) -> Optional[datetime]:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def is_due(cfg: dict, now: datetime) -> bool:
    last = _parse_iso(cfg.get("last_checked_at"))
    if last is None:
        return True
    try:
        minutes = int(cfg.get("interval_minutes") or DEFAULT_INTERVAL)
    except (TypeError, ValueError):
        minutes = DEFAULT_INTERVAL
    # A few seconds' slack, so a route on 5 minutes is not pushed to 6 by the
    # time the previous pass itself took.
    return now - last >= timedelta(minutes=minutes) - timedelta(seconds=30)


def run_due() -> dict:
    """Collect every enabled pull route whose interval has passed."""
    totals = {"routes": 0, "accepted": 0, "held": 0, "turned_away": 0, "errors": 0}
    if not column_ready():
        return totals
    from db import SessionLocal
    from intake_models import IntakeRoute
    with SessionLocal() as s:
        rows = s.execute(_sa_text(
            "SELECT route_id, route_sftp_config FROM intake_route "
            "WHERE channel = 'sftp' AND is_enabled AND route_sftp_config IS NOT NULL "
            "ORDER BY route_id")).all()
    now = datetime.now(timezone.utc)
    for rid, cfg in rows:
        if _stop.is_set():
            break
        cfg = _as_dict(cfg)
        if not cfg or not is_due(cfg, now):
            continue
        with SessionLocal() as s:
            route = s.get(IntakeRoute, rid)
            if route is None or not route.is_enabled:
                continue
            try:
                result = collect_route(s, route)
                s.commit()
            except Exception:  # noqa: BLE001
                s.rollback()
                log.exception("pull route %s failed", rid)
                totals["errors"] += 1
                continue
        totals["routes"] += 1
        for k in ("accepted", "held", "turned_away"):
            totals[k] += result.get(k, 0)
        if result.get("error"):
            totals["errors"] += 1
    return totals


def status() -> dict:
    """How pull routes are being checked in this process, for the Ways in panel."""
    if not _enabled():
        mode = "off"
    elif not column_ready():
        mode = "not_ready"
    else:
        mode = _mode if _mode != "off" else "timer"
    return {"mode": mode, "check_seconds": _TICK_SECONDS}


def _run() -> None:
    global _mode
    _mode = "timer"
    try:
        # Let the app finish starting before the first look.
        if _stop.wait(15):
            return
        while not _stop.is_set():
            try:
                totals = run_due()
                if totals["accepted"] or totals["held"] or totals["turned_away"]:
                    log.info("sftp pull: %s", totals)
            except Exception:  # noqa: BLE001 — never let a bad pass end the thread
                log.exception("sftp pull pass failed")
            if _stop.wait(_TICK_SECONDS):
                break
    finally:
        _mode = "off"


def start(app) -> None:
    """Attach the scheduler to the app's startup, the way sftp_poller does."""
    if not _enabled():
        log.info("sftp pull off (SFTP_PULL_ENABLED=0) — remote servers are only "
                 "collected from on request")
        return

    @app.on_event("startup")
    async def _start_sftp_pull() -> None:      # pragma: no cover - wiring
        global _thread
        if _thread is None or not _thread.is_alive():
            _stop.clear()
            _thread = threading.Thread(target=_run, name="sftp-pull", daemon=True)
            _thread.start()

    @app.on_event("shutdown")
    async def _stop_sftp_pull() -> None:       # pragma: no cover - wiring
        _stop.set()
