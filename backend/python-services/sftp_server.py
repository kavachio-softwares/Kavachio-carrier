"""Kavachio's own SFTP server — brokers sign in with the login emailed to them
and upload into a folder only they can see.

WHY IT EXISTS (6 Oct 2026). The SFTP channel is Kavachio's server, not the
broker's: the carrier makes the channel, Kavachio makes the broker a login
(sftp_accounts) and emails it, the broker uploads, and sftp_poller collects —
the same collector, folders and checks as before. Until now nothing actually
served SFTP in front of those folders, so nobody could sign in to them.

WHAT A LOGIN CAN DO. Two folders of its own channel, and nothing else:
  /incoming   where it starts. Upload — a new file, an overwrite, a resumed
              upload — and rename or delete its own files until they are
              collected (careful clients upload under a temporary name and
              rename when done; a wrong file can be taken back before anybody
              reads it). Never read back: a stolen password cannot be used to
              download a broker's bordereaux.
  /outbound   Kavachio's answer to each file — the receipt, the result and any
              exceptions (submission_service.write_sftp). Download, and delete
              once read; nothing can be written there.
The channel's other folders (processed, rejected, held, quarantine) are not
shown. No folders can be made, no links, no permission changes, no shell, no
commands, no port forwarding. Every path is resolved inside the channel's
folder and refused if it would leave it, so another broker's folder — or
another carrier's — cannot even be named. Collected files are moved out of
`incoming` by sftp_poller, so they disappear from it once taken.

SECURITY, in the order it applies to a connection:
  * at most SFTP_SERVER_MAX_CONNECTIONS at once, of which at most
    SFTP_SERVER_MAX_PENDING may still be signing in;
  * modern algorithms only — no CBC ciphers, no SHA-1 or MD5 MACs, strict key
    exchange (the Terrapin fix) — and the library version is not announced;
  * 30 seconds to sign in, three attempts per connection, a second added to
    every failure, and a USER NAME with 10 failures in 10 minutes is locked
    for 15 minutes — that login only, never anybody else's;
  * a sign-in is sftp_accounts.verify: user name and password fingerprint, the
    channel switched on, the login not revoked, its IP allow-list;
  * at most SFTP_SERVER_MAX_PER_LOGIN sessions open at once per login.

WHO IS WHO (9 Oct 2026). Every limit after the handshake counts per LOGIN —
one broker's channel — never per internet address. Behind a proxy or load
balancer that hides where connections come from, every broker would share one
address: an address limit would then cap the whole server at 8 connections,
and one broker's mistyped password would lock every broker out. Before
sign-in nobody is known yet, so that stage is capped for the server as a
whole (SFTP_SERVER_MAX_PENDING), with 30 seconds to sign in. The address is
still logged, and a login's own IP allow-list (off unless a carrier sets one)
needs brokers to connect directly.
  * a session idle for SFTP_SERVER_IDLE_SECONDS is closed, and switching the
    channel off or making a new password closes its sessions at once;
  * one file at most INTAKE_MAX_FILE_MB (an oversized upload is deleted), at
    most 100 files waiting in a folder, and SFTP_SERVER_QUOTA_MB in all;
  * an upload the client never finished — the connection dropped, idled out
    or was cut off — is deleted, never collected as if it were whole.

HOST KEYS. Ed25519 and RSA-3072, made on first start in SFTP_SERVER_HOST_KEY_DIR
(default <SFTP_ROOT>/.host-keys, mode 0700, files 0600) and kept: a key that
changed on every restart would train brokers to click through the one warning
that protects them. The fingerprints are shown on the channel and in the login
email so a broker can check them the first time they connect.

WHERE IT RUNS. Inside the API process, started like the collectors (main.py),
which is also how the collector knows an upload is still open
(sftp_watch.being_written). With several API workers on one host the first
binds the port and the others keep retrying quietly. To serve these folders
from somewhere else later — AWS Transfer Family, Azure Blob SFTP, OpenSSH
chrooted at SFTP_ROOT — set SFTP_SERVER_ENABLED=0 and point that server at the
same folders; the logins and the collector do not change.

Configuration:
  SFTP_SERVER_ENABLED          0/1   (default 1)
  SFTP_SERVER_BIND             addr  (default 0.0.0.0)
  SFTP_SERVER_PORT             int   (default SFTP_PORT, else 2022)
  SFTP_HOST / SFTP_PORT        what brokers are TOLD (intake_service) — set
                                     them when a router or load balancer sits
                                     in front and the outside differs
  SFTP_SERVER_HOST_KEY_DIR     path  (default <SFTP_ROOT>/.host-keys)
  SFTP_SERVER_MAX_CONNECTIONS  int   (default 50)
  SFTP_SERVER_MAX_PENDING      int   (default 20)  not yet signed in
  SFTP_SERVER_MAX_PER_LOGIN    int   (default 8)   one login's sessions
  SFTP_SERVER_IDLE_SECONDS     int   (default 600)
  SFTP_SERVER_QUOTA_MB         int   (default 2048) waiting in one folder
  INTAKE_MAX_FILE_MB           int   (default 200)  one file, see intake_safety
"""
from __future__ import annotations

import base64
import errno
import hashlib
import logging
import os
import posixpath
import re
import secrets
import socket
import stat as st
import threading
import time
from collections import deque
from pathlib import Path
from typing import Callable, Optional

import paramiko
from paramiko import (
    AUTH_FAILED, AUTH_SUCCESSFUL, OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED,
    OPEN_SUCCEEDED, SFTP_FAILURE, SFTP_NO_SUCH_FILE, SFTP_OK,
    SFTP_PERMISSION_DENIED, SFTPAttributes, SFTPHandle, SFTPServer,
    SFTPServerInterface, ServerInterface,
)
from paramiko.server import InteractiveQuery

import sftp_watch

log = logging.getLogger("kavachio.sftp_server")

# Nothing older than this is offered. Paramiko 5 already dropped SHA-1 key
# exchange and ssh-rsa signatures; these are what it still lists by default.
_DISABLED_ALGORITHMS = {
    "ciphers": ["aes128-cbc", "aes192-cbc", "aes256-cbc", "3des-cbc"],
    "macs": ["hmac-sha1", "hmac-sha1-96", "hmac-md5", "hmac-md5-96"],
}
_BANNER = "Kavachio SFTP - authorised users only. Activity is logged.\r\n"

_AUTH_SECONDS = 30            # to finish the handshake AND sign in
_MAX_AUTH_ATTEMPTS = 3        # per connection
_MAX_CHANNELS = 4             # per connection; a client opens one
_MAX_PER_LOGIN = 8           # sessions one login may have open at once
_MAX_PENDING = 20             # connections still signing in, whole server
_MAX_PENDING_FILES = 100      # waiting in one folder at once
_RETRY_BIND_SECONDS = 10

_HOST_KEYS = (("ssh_host_ed25519_key", "ed25519"), ("ssh_host_rsa_key", "rsa"))


# ── configuration ───────────────────────────────────────────────────────────
# Read at call time: main.py loads .env after this module is imported.

def _flag(name: str, default: str) -> bool:
    return os.getenv(name, default).strip().lower() in ("1", "true", "yes", "on")


def _int(name: str, default: int, floor: int = 1) -> int:
    try:
        return max(floor, int(os.getenv(name, str(default))))
    except ValueError:
        return default


def enabled() -> bool:
    return _flag("SFTP_SERVER_ENABLED", "1")


def listen_port() -> int:
    for name in ("SFTP_SERVER_PORT", "SFTP_PORT"):
        raw = (os.getenv(name) or "").strip()
        if raw:
            try:
                return int(raw)
            except ValueError:
                log.warning("%s=%r is not a port number", name, raw)
    return 2022


def bind_address() -> str:
    return (os.getenv("SFTP_SERVER_BIND") or "0.0.0.0").strip()


def host_key_dir() -> Path:
    raw = (os.getenv("SFTP_SERVER_HOST_KEY_DIR") or "").strip()
    if raw:
        return Path(raw).expanduser().resolve()
    import intake_service as svc
    return svc.sftp_root() / ".host-keys"


# ── host keys ───────────────────────────────────────────────────────────────

def _generate(path: Path, kind: str) -> None:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ed25519, rsa
    key = (ed25519.Ed25519PrivateKey.generate() if kind == "ed25519"
           else rsa.generate_private_key(public_exponent=65537, key_size=3072))
    pem = key.private_bytes(serialization.Encoding.PEM,
                            serialization.PrivateFormat.OpenSSH,
                            serialization.NoEncryption())
    # Written whole under a private name, then LINKED into place: a key is never
    # seen half-written, and one another worker made first is never replaced.
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(4)}")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(pem)
    try:
        os.link(tmp, path)
    except FileExistsError:
        pass
    finally:
        os.unlink(tmp)


def load_host_keys() -> list:
    d = host_key_dir()
    d.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(d, 0o700)
    except OSError:
        pass
    keys = []
    for name, kind in _HOST_KEYS:
        p = d / name
        if not p.exists():
            log.info("sftp server: making its %s host key in %s", kind, d)
            _generate(p, kind)
        cls = paramiko.Ed25519Key if kind == "ed25519" else paramiko.RSAKey
        keys.append(cls(filename=str(p)))
    return keys


def key_fingerprint(key) -> str:
    """"SHA256:…" — the form OpenSSH, FileZilla and WinSCP show on first connect."""
    digest = hashlib.sha256(key.asbytes()).digest()
    return "SHA256:" + base64.b64encode(digest).decode().rstrip("=")


# ── failed sign-ins ─────────────────────────────────────────────────────────

class _Throttle:
    """Failed sign-ins per USER NAME: `limit` in `window` seconds locks that
    name for `block` seconds. Per process, which is where the server is.
    Keyed by name, not address, so brokers who reach us through one proxy
    address never lock each other out (see WHO IS WHO above)."""

    def __init__(self, limit: int = 10, window: float = 600, block: float = 900):
        self.limit, self.window, self.block = limit, window, block
        self._fails: dict[str, deque] = {}
        self._until: dict[str, float] = {}
        self._lock = threading.Lock()

    def blocked(self, key: str) -> bool:
        now = time.monotonic()
        with self._lock:
            until = self._until.get(key)
            if until is not None and now >= until:
                del self._until[key]
                until = None
            return until is not None

    def failed(self, key: str) -> bool:
        """Record one; True when the name is locked from now."""
        now = time.monotonic()
        with self._lock:
            q = self._fails.setdefault(key, deque())
            q.append(now)
            while q and now - q[0] > self.window:
                q.popleft()
            if len(q) >= self.limit:
                self._until[key] = now + self.block
                q.clear()
                return True
            return False

    def succeeded(self, key: Optional[str]) -> None:
        with self._lock:
            self._fails.pop(key, None)

    def prune(self) -> None:
        now = time.monotonic()
        with self._lock:
            for ip in [ip for ip, q in self._fails.items()
                       if not q or now - q[-1] > self.window]:
                del self._fails[ip]
            for ip in [ip for ip, until in self._until.items() if now >= until]:
                del self._until[ip]


# The shape of every login's name (sftp_accounts._USER_RE). Only such names
# are counted: anything else can never sign in, so there is nothing to lock.
_NAME_RE = re.compile(r"^[a-z][a-z0-9-]{2,31}$")


def _throttle_key(username: str) -> Optional[str]:
    """The name failed sign-ins are counted against, as verify() reads it."""
    name = (username or "").strip().lower()
    return name if _NAME_RE.match(name) else None


def _shown(username: str) -> str:
    """A typed user name, safe to put in a log line."""
    return repr((username or "")[:64])


# ── one connection: sign-in, and the only thing it may open ────────────────

class _Gate(ServerInterface):
    def __init__(self, owner: "SftpServer", ip: str):
        self.owner = owner
        self.ip = ip
        self.transport: Optional[paramiko.Transport] = None
        self.login = None                  # sftp_accounts.Login once signed in
        self.failures = 0
        self.channels = 0
        self.last_activity = time.monotonic()
        self._ki_user = ""

    def touch(self) -> None:
        self.last_activity = time.monotonic()

    def _close_soon(self) -> None:
        # Not from this thread: the auth handler runs on the transport's own.
        t = self.transport
        if t is not None:
            threading.Timer(0.2, t.close).start()

    # sign-in — password, or the same password asked as keyboard-interactive,
    # which is all some clients (and some versions of WinSCP) will use.
    def get_allowed_auths(self, username):
        return "password,keyboard-interactive"

    def check_auth_none(self, username):
        return AUTH_FAILED

    def check_auth_publickey(self, username, key):
        return AUTH_FAILED

    def check_auth_password(self, username, password):
        return self._check(username, password)

    def check_auth_interactive(self, username, submethods):
        self._ki_user = username
        return InteractiveQuery("", "", ("Password: ", False))

    def check_auth_interactive_response(self, responses):
        return self._check(self._ki_user, responses[0] if len(responses) == 1 else "")

    def _check(self, username: str, password: str):
        owner = self.owner
        who = _throttle_key(username)
        if who and owner.throttle.blocked(who):
            # Refused the same way as a wrong password — no database lookup,
            # and nothing that tells a guesser the name is locked.
            log.warning("sftp: refused %s from %s — locked after too many failed "
                        "sign-ins", _shown(username), self.ip)
            self.failures += 1
            time.sleep(owner.fail_delay)
            if self.failures >= _MAX_AUTH_ATTEMPTS:
                self._close_soon()
            return AUTH_FAILED
        try:
            login = owner.authenticate(username, password, self.ip)
        except Exception:  # noqa: BLE001 — the database, not the broker
            log.exception("sftp: could not check a sign-in for %s", _shown(username))
            return AUTH_FAILED
        if login is None:
            self.failures += 1
            now_blocked = owner.throttle.failed(who) if who else False
            log.warning("sftp: failed sign-in for %s from %s", _shown(username), self.ip)
            if now_blocked:
                log.warning("sftp: %s locked for %ds after %d failed sign-ins",
                            _shown(username), int(owner.throttle.block),
                            owner.throttle.limit)
            time.sleep(owner.fail_delay)
            if now_blocked or self.failures >= _MAX_AUTH_ATTEMPTS:
                self._close_soon()
            return AUTH_FAILED
        owner.throttle.succeeded(who)
        self.login = login
        self.touch()
        log.info("sftp: %s signed in from %s (route %s)", login.username, self.ip, login.route_id)
        return AUTH_SUCCESSFUL

    # what a signed-in connection may open: one SFTP session, nothing else
    def check_channel_request(self, kind, chanid):
        if kind != "session" or self.login is None or self.channels >= _MAX_CHANNELS:
            return OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED
        if self.channels == 0:
            n = self.owner.open_sessions_of(self.login.username, but=self)
            if n >= self.owner.max_per_login:
                # This one login already has its share open — say so to the
                # client ("administratively prohibited"), rather than a
                # misleading "wrong password".
                log.warning("sftp: %s already has %d sessions open — refusing another",
                            self.login.username, n)
                self._close_soon()
                return OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED
        self.channels += 1
        return OPEN_SUCCEEDED

    def check_channel_subsystem_request(self, channel, name):
        if name != "sftp" or self.login is None:
            return False
        return super().check_channel_subsystem_request(channel, name)

    def check_channel_shell_request(self, channel):
        return False

    def check_channel_exec_request(self, channel, command):
        return False

    def check_channel_pty_request(self, channel, term, width, height,
                                  pixelwidth, pixelheight, modes):
        return False

    def check_channel_env_request(self, channel, name, value):
        return False

    def check_channel_x11_request(self, channel, single_connection, auth_protocol,
                                  auth_cookie, screen_number):
        return False

    def check_channel_forward_agent_request(self, channel):
        return False

    def check_channel_direct_tcpip_request(self, chanid, origin, destination):
        return OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED

    def check_port_forward_request(self, address, port):
        return False

    def check_global_request(self, kind, msg):
        return False

    def get_banner(self):
        return (_BANNER, "en-US")


# ── the folder ──────────────────────────────────────────────────────────────

# What a login sees of its channel's folder: where it uploads, and where
# Kavachio answers. The channel's other folders are never shown.
_INCOMING, _OUTBOUND = "incoming", "outbound"
_AREAS = (_INCOMING, _OUTBOUND)


def _ok_name(name: str) -> bool:
    return (0 < len(name.encode("utf-8", "surrogateescape")) <= 255
            and name not in (".", "..")
            and not any(ord(c) < 32 or ord(c) == 127 for c in name))


def _attrs(stat_result, filename: Optional[str] = None) -> SFTPAttributes:
    a = SFTPAttributes.from_stat(stat_result, filename)
    a.st_uid = a.st_gid = 0          # the server's own account is nobody's business
    return a


class _Upload(SFTPHandle):
    """A file being written. Read back: never."""

    def __init__(self, flags: int, jail: "_Jail", real: str, f):
        super().__init__(flags)
        self.writefile = f
        self.jail = jail
        self.real = real
        self.too_big = False
        self.closed_once = False
        jail.uploads.add(self)
        sftp_watch.writing_started(real)

    def read(self, offset, length):
        return SFTP_PERMISSION_DENIED

    def write(self, offset, data):
        self.jail.gate.touch()
        if self.too_big:
            return SFTP_FAILURE
        if offset + len(data) > self.jail.max_file:
            self.too_big = True
            log.warning("sftp: %s's %s is over %d MB — refused", self.jail.login.username,
                        os.path.basename(self.real), self.jail.max_file // (1024 * 1024))
            return SFTP_FAILURE
        return super().write(offset, data)

    def stat(self):
        try:
            return _attrs(os.fstat(self.writefile.fileno()))
        except (OSError, ValueError):
            return SFTP_FAILURE

    def chattr(self, attr):
        return SFTP_OK            # accepted and ignored — see _Jail.chattr

    def close(self):
        if self.closed_once:
            return
        self.closed_once = True
        self.jail.uploads.discard(self)
        try:
            super().close()
        finally:
            name = os.path.basename(self.real)
            # The client never said "done": the connection dropped, went idle
            # past the limit, or was cut off (new password, channel switched
            # off). Whatever is on disk is the part that got through — and a
            # cut-off CSV, JSON or XML still READS, just with rows missing, so
            # it would be processed as if it were the whole bordereau. Deleted;
            # the broker's client reports the upload failed and they send it
            # again. Deleted BEFORE writing_ended, so the collector can never
            # see it as finished in between.
            cut_off = self.jail.ending and not self.too_big
            if self.too_big or cut_off:
                # Half a bordereau is worse than none: it would be collected,
                # refused as unreadable, and reported as if the broker sent it.
                try:
                    os.unlink(self.real)
                except OSError:
                    pass
            sftp_watch.writing_ended(self.real)
            if cut_off:
                log.warning("sftp: %s's upload of %s was cut off before it finished "
                            "— the part received was deleted", self.jail.login.username,
                            name)
            elif not self.too_big:
                try:
                    size = os.path.getsize(self.real)
                except OSError:
                    size = -1
                log.info("sftp: %s uploaded %s (%d bytes)", self.jail.login.username,
                         name, size)


class _Download(SFTPHandle):
    """One of Kavachio's replies in /outbound, being read. Written: never."""

    def __init__(self, flags: int, jail: "_Jail", real: str, f):
        super().__init__(flags)
        self.readfile = f
        self.jail = jail

    def read(self, offset, length):
        self.jail.gate.touch()
        return super().read(offset, length)

    def write(self, offset, data):
        return SFTP_PERMISSION_DENIED

    def stat(self):
        try:
            return _attrs(os.fstat(self.readfile.fileno()))
        except (OSError, ValueError):
            return SFTP_FAILURE

    def chattr(self, attr):
        return SFTP_OK


class _Jail(SFTPServerInterface):
    """One login's view of its channel's folder:

        /             the two folders below, and nothing else
        /incoming     upload — where a login starts
        /outbound     Kavachio's replies, to download

    A path names the top, one of the two folders, or a file directly inside
    one of them. Anything else is refused."""

    def __init__(self, server: _Gate, *args, **kwargs):
        super().__init__(server, *args, **kwargs)
        self.gate = server
        self.login = server.login
        self.base = os.path.realpath(str(self.login.folder))
        for area in _AREAS:
            os.makedirs(os.path.join(self.base, area), exist_ok=True)
        self.max_file = server.owner.max_file_bytes
        self.quota = server.owner.quota_bytes
        self.ending = False
        # Uploads open right now, so a rename can tell the one it moved.
        self.uploads: set = set()

    def session_ended(self):
        # Called before paramiko closes the handles a dropped connection left
        # open, so _Upload.close can tell "done" from "cut off".
        self.ending = True

    # paths
    def canonicalize(self, path):
        # A relative path is relative to where a login starts: /incoming.
        p = (path or "").replace("\\", "/")
        if not p.startswith("/"):
            p = f"/{_INCOMING}/{p}"
        return "/" + posixpath.normpath(p).lstrip("/")

    def _where(self, path) -> Optional[tuple]:
        """("", "") the top · (area, "") one of the folders · (area, name) a
        file in it · None for anything a login may not name."""
        parts = [p for p in self.canonicalize(path).split("/") if p]
        if not parts:
            return ("", "")
        if parts[0] not in _AREAS or len(parts) > 2:
            return None
        if len(parts) == 1:
            return (parts[0], "")
        if not _ok_name(parts[1]):
            return None
        # A reply still being written is a hidden temporary file; not yet one.
        if parts[0] == _OUTBOUND and parts[1].startswith("."):
            return None
        return (parts[0], parts[1])

    def _real(self, area: str, name: str = "") -> str:
        if not area:
            return self.base
        return os.path.join(self.base, area, name) if name else os.path.join(self.base, area)

    def _file(self, where) -> Optional[str]:
        """The real path of a regular file a login may name — never a link,
        never a folder — or None."""
        if where is None or not where[1]:
            return None
        real = self._real(*where)
        try:
            return real if st.S_ISREG(os.lstat(real).st_mode) else None
        except OSError:
            return None

    # reading folders
    def list_folder(self, path):
        self.gate.touch()
        where = self._where(path)
        if where is None or where[1]:
            return SFTP_NO_SUCH_FILE
        area = where[0]
        try:
            if not area:
                return [_attrs(os.stat(self._real(a)), a) for a in _AREAS]
            out = []
            with os.scandir(self._real(area)) as it:
                for e in it:
                    if area == _OUTBOUND and e.name.startswith("."):
                        continue
                    try:
                        if e.is_file(follow_symlinks=False):
                            out.append(_attrs(e.stat(follow_symlinks=False), e.name))
                    except OSError:
                        continue
            return out
        except OSError as exc:
            return SFTPServer.convert_errno(exc.errno)

    def stat(self, path):
        self.gate.touch()
        where = self._where(path)
        if where is None:
            return SFTP_NO_SUCH_FILE
        try:
            if not where[1]:
                return _attrs(os.stat(self._real(where[0])))
            real = self._file(where)
            return _attrs(os.lstat(real)) if real else SFTP_NO_SUCH_FILE
        except OSError as exc:
            return SFTPServer.convert_errno(exc.errno)

    lstat = stat

    # files
    def _full(self) -> Optional[str]:
        n, total = 0, 0
        try:
            with os.scandir(self._real(_INCOMING)) as it:
                for e in it:
                    if e.is_file(follow_symlinks=False):
                        n += 1
                        total += e.stat(follow_symlinks=False).st_size
        except OSError:
            return "the folder cannot be read"
        if n >= _MAX_PENDING_FILES:
            return f"{n} files are already waiting to be collected"
        if total >= self.quota:
            return "the files waiting to be collected already fill the folder's quota"
        return None

    def open(self, path, flags, attr):
        self.gate.touch()
        where = self._where(path)
        if where is None or not where[1]:
            return SFTP_PERMISSION_DENIED
        access = flags & (os.O_RDONLY | os.O_WRONLY | os.O_RDWR)
        if where[0] == _OUTBOUND:
            return self._open_reply(where, flags, access)
        if access == os.O_RDONLY:
            # Upload only: a stolen password must not read a bordereau back out.
            return SFTP_PERMISSION_DENIED
        real = self._real(*where)
        if os.path.lexists(real):
            if self._file(where) is None:
                return SFTP_PERMISSION_DENIED
        else:
            if not flags & os.O_CREAT:
                return SFTP_NO_SUCH_FILE
            refused = self._full()
            if refused:
                log.warning("sftp: %s cannot upload %s — %s", self.login.username,
                            where[1], refused)
                return SFTP_FAILURE
        try:
            fd = os.open(real, flags | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0), 0o640)
        except OSError as exc:
            return SFTPServer.convert_errno(exc.errno)
        try:
            if not st.S_ISREG(os.fstat(fd).st_mode):
                os.close(fd)
                return SFTP_PERMISSION_DENIED
            f = os.fdopen(fd, "ab" if flags & os.O_APPEND
                          else "r+b" if access == os.O_RDWR else "wb")
        except OSError as exc:
            try:
                os.close(fd)
            except OSError:
                pass
            return SFTPServer.convert_errno(exc.errno)
        return _Upload(flags, self, real, f)

    def _open_reply(self, where, flags: int, access: int):
        if access != os.O_RDONLY or flags & (os.O_CREAT | os.O_TRUNC | os.O_APPEND):
            return SFTP_PERMISSION_DENIED
        real = self._file(where)
        if real is None:
            return SFTP_NO_SUCH_FILE
        try:
            fd = os.open(real, os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0))
        except OSError as exc:
            return SFTPServer.convert_errno(exc.errno)
        try:
            if not st.S_ISREG(os.fstat(fd).st_mode):
                os.close(fd)
                return SFTP_PERMISSION_DENIED
            f = os.fdopen(fd, "rb")
        except OSError as exc:
            try:
                os.close(fd)
            except OSError:
                pass
            return SFTPServer.convert_errno(exc.errno)
        log.info("sftp: %s downloaded %s/%s", self.login.username, _OUTBOUND, where[1])
        return _Download(flags, self, real, f)

    def remove(self, path):
        # A pending upload taken back, or a reply cleared away once read.
        self.gate.touch()
        where = self._where(path)
        real = self._file(where)
        if real is None:
            return (SFTP_PERMISSION_DENIED if where is None or not where[1]
                    else SFTP_NO_SUCH_FILE)
        try:
            os.unlink(real)
        except OSError as exc:
            return SFTPServer.convert_errno(exc.errno)
        log.info("sftp: %s deleted %s/%s", self.login.username, where[0], where[1])
        return SFTP_OK

    def rename(self, oldpath, newpath):
        return self._rename(oldpath, newpath, replace=False)

    def posix_rename(self, oldpath, newpath):
        return self._rename(oldpath, newpath, replace=True)

    def _rename(self, oldpath, newpath, *, replace: bool):
        # Inside /incoming only: a temporary upload name becoming the real one.
        self.gate.touch()
        old_w, new_w = self._where(oldpath), self._where(newpath)
        if (old_w is None or new_w is None or old_w[0] != _INCOMING
                or new_w[0] != _INCOMING or not new_w[1]):
            return SFTP_PERMISSION_DENIED
        old = self._file(old_w)
        if old is None:
            return SFTP_NO_SUCH_FILE
        new = self._real(*new_w)
        try:
            if os.path.lexists(new):
                # Plain SFTP rename must not replace (what OpenSSH does too);
                # posix-rename may, but only a file with a file.
                if not replace:
                    return SFTP_FAILURE
                if self._file(new_w) is None:
                    return SFTP_PERMISSION_DENIED
            os.replace(old, new)
        except OSError as exc:
            return SFTPServer.convert_errno(exc.errno)
        sftp_watch.writing_renamed(old, new)
        # Still open under its old name: its close must find it under the new
        # one, or a cut-off upload would survive the rename.
        for up in list(self.uploads):
            if up.real == old:
                up.real = new
        return SFTP_OK

    def chattr(self, path, attr):
        # Times and permissions a client asks for are accepted and ignored. The
        # collector judges "finished" by when WE last wrote a file, and a
        # back-dated time would make a half-written one look finished.
        where = self._where(path)
        if where is not None and (not where[1] or self._file(where)):
            return SFTP_OK
        return SFTP_NO_SUCH_FILE

    def mkdir(self, path, attr):
        return SFTP_PERMISSION_DENIED

    def rmdir(self, path):
        return SFTP_PERMISSION_DENIED

    def symlink(self, target_path, path):
        return SFTP_PERMISSION_DENIED

    def readlink(self, path):
        return SFTP_PERMISSION_DENIED


# ── the server ──────────────────────────────────────────────────────────────

class _Session:
    def __init__(self, ip: str):
        self.ip = ip
        self.started = time.monotonic()
        self.transport: Optional[paramiko.Transport] = None
        self.gate: Optional[_Gate] = None


class SftpServer:
    """Listens, hands each connection to paramiko, and keeps an eye on every
    session: unfinished sign-ins and idle sessions are closed by `_reap`."""

    def __init__(self, host: str, port: int, *,
                 authenticate: Callable[[str, str, Optional[str]], object],
                 host_keys: Optional[list] = None,
                 max_connections: int = 50, max_per_login: int = _MAX_PER_LOGIN,
                 max_pending: int = _MAX_PENDING,
                 idle_seconds: int = 600,
                 max_file_bytes: int = 200 * 1024 * 1024,
                 quota_bytes: int = 2048 * 1024 * 1024,
                 fail_delay: float = 1.0,
                 throttle: Optional[_Throttle] = None):
        self.host, self.port = host, port
        self.authenticate = authenticate
        self.host_keys = host_keys
        self.max_connections, self.max_per_login = max_connections, max_per_login
        self.max_pending = max_pending
        self.idle_seconds = idle_seconds
        self.max_file_bytes, self.quota_bytes = max_file_bytes, quota_bytes
        self.fail_delay = fail_delay
        self.throttle = throttle or _Throttle()
        self.state = "starting"            # starting | listening | waiting | failed | stopped
        self.error: Optional[str] = None
        self.ready = threading.Event()     # set once listening (or given up)
        self._sessions: set[_Session] = set()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._sock: Optional[socket.socket] = None

    # lifecycle
    def start(self) -> None:
        threading.Thread(target=self._serve, name="sftp-server", daemon=True).start()

    def stop(self) -> None:
        self._stop.set()
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
        with self._lock:
            sessions = list(self._sessions)
        for s in sessions:
            if s.transport is not None:
                s.transport.close()
        self.state = "stopped"

    def _bind(self) -> socket.socket:
        family, kind, proto, _, addr = socket.getaddrinfo(
            self.host, self.port, type=socket.SOCK_STREAM, flags=socket.AI_PASSIVE)[0]
        sock = socket.socket(family, kind, proto)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(addr)
            sock.listen(64)
        except OSError:
            sock.close()
            raise
        sock.settimeout(1.0)
        self.port = sock.getsockname()[1]
        return sock

    def _serve(self) -> None:
        try:
            if self.host_keys is None:
                self.host_keys = load_host_keys()
        except Exception as exc:  # noqa: BLE001
            self.state, self.error = "failed", f"host keys: {exc}"
            log.error("sftp server cannot start — %s", self.error)
            self.ready.set()
            return
        warned = False
        while not self._stop.is_set():
            try:
                self._sock = self._bind()
                break
            except OSError as exc:
                # The port is taken — usually another worker of this app, or the
                # previous process still letting go after a reload. Keep trying.
                self.state, self.error = "waiting", f"cannot listen on port {self.port}: {exc}"
                if not warned:
                    log.warning("sftp server: %s — retrying every %ss", self.error,
                                _RETRY_BIND_SECONDS)
                    warned = True
                self.ready.set()
                self._stop.wait(_RETRY_BIND_SECONDS)
        if self._stop.is_set():
            return
        self.state, self.error = "listening", None
        log.info("sftp server: listening on %s:%s", self.host, self.port)
        self.ready.set()
        threading.Thread(target=self._reap, name="sftp-reaper", daemon=True).start()

        while not self._stop.is_set():
            try:
                conn, addr = self._sock.accept()
            except socket.timeout:
                continue
            except OSError:
                if self._stop.is_set():
                    break
                log.exception("sftp server: accept failed")
                time.sleep(0.5)
                continue
            ip = addr[0]
            if ip.startswith("::ffff:"):
                ip = ip[7:]
            sess = self._admit(ip)
            if sess is None:
                try:
                    conn.close()
                except OSError:
                    pass
                continue
            threading.Thread(target=self._handle, args=(conn, sess),
                             name=f"sftp-{ip}", daemon=True).start()

    def _admit(self, ip: str) -> Optional[_Session]:
        """A new connection, before anybody is known: only server-wide limits.
        Nothing here is per address (see WHO IS WHO above)."""
        with self._lock:
            # A connection that has already ended no longer counts — without
            # waiting for the reaper's next round.
            for s in [s for s in self._sessions
                      if s.transport is not None and not s.transport.is_active()]:
                self._sessions.discard(s)
            if len(self._sessions) >= self.max_connections:
                log.warning("sftp server: at its %d-connection limit — refusing %s",
                            self.max_connections, ip)
                return None
            pending = sum(1 for s in self._sessions
                          if s.gate is None or s.gate.login is None)
            if pending >= self.max_pending:
                log.warning("sftp server: %d connections still signing in — refusing %s",
                            pending, ip)
                return None
            sess = _Session(ip)
            self._sessions.add(sess)
            return sess

    def open_sessions_of(self, username: str, but=None) -> int:
        """Live sessions this login has open, besides `but` (a _Gate)."""
        with self._lock:
            sessions = list(self._sessions)
        return sum(1 for s in sessions
                   if s.gate is not None and s.gate is not but
                   and s.gate.login is not None and s.gate.channels > 0
                   and s.gate.login.username == username
                   and s.transport is not None and s.transport.is_active())

    def _forget(self, sess: _Session) -> None:
        with self._lock:
            self._sessions.discard(sess)

    def _handle(self, conn: socket.socket, sess: _Session) -> None:
        t = None
        try:
            t = paramiko.Transport(conn, disabled_algorithms=_DISABLED_ALGORITHMS)
            t.local_version = "SSH-2.0-Kavachio"
            t.banner_timeout = 15
            for key in self.host_keys:
                t.add_server_key(key)
            t.set_subsystem_handler("sftp", SFTPServer, _Jail)
            gate = _Gate(self, sess.ip)
            gate.transport = t
            sess.transport, sess.gate = t, gate
            t.start_server(server=gate)
        except Exception as exc:  # noqa: BLE001 — a scanner, an old client, a hang-up
            log.info("sftp: %s did not finish the handshake: %s", sess.ip, exc)
            try:
                (t or conn).close()
            except Exception:  # noqa: BLE001
                pass
            self._forget(sess)

    def _reap(self) -> None:
        while not self._stop.wait(2.0):
            now = time.monotonic()
            with self._lock:
                sessions = list(self._sessions)
            for s in sessions:
                t, g = s.transport, s.gate
                if t is not None and not t.is_active():
                    self._forget(s)
                elif g is None or g.login is None:
                    if now - s.started > _AUTH_SECONDS:
                        if t is not None:
                            t.close()
                        self._forget(s)
                elif now - g.last_activity > self.idle_seconds:
                    log.info("sftp: closing %s's session — idle %ds", g.login.username,
                             self.idle_seconds)
                    t.close()
            self.throttle.prune()

    # used by the API
    def drop_route(self, route_id: int) -> int:
        """Close every session signed in to this channel. Returns how many."""
        n = 0
        with self._lock:
            sessions = list(self._sessions)
        for s in sessions:
            g = s.gate
            if g is not None and g.login is not None and g.login.route_id == route_id:
                s.transport.close()
                n += 1
        if n:
            log.info("sftp: closed %d session(s) on route %s", n, route_id)
        return n

    def session_count(self) -> int:
        with self._lock:
            return len(self._sessions)


# ── the app's one server ────────────────────────────────────────────────────

_server: Optional[SftpServer] = None
_server_lock = threading.Lock()


def ensure_running() -> SftpServer:
    global _server
    with _server_lock:
        if _server is None or _server.state == "stopped":
            import intake_safety
            import sftp_accounts
            _server = SftpServer(
                bind_address(), listen_port(),
                authenticate=sftp_accounts.verify,
                max_connections=_int("SFTP_SERVER_MAX_CONNECTIONS", 50),
                max_pending=_int("SFTP_SERVER_MAX_PENDING", _MAX_PENDING),
                max_per_login=_int("SFTP_SERVER_MAX_PER_LOGIN", _MAX_PER_LOGIN),
                idle_seconds=_int("SFTP_SERVER_IDLE_SECONDS", 600, floor=30),
                max_file_bytes=intake_safety.max_bytes(),
                quota_bytes=_int("SFTP_SERVER_QUOTA_MB", 2048) * 1024 * 1024)
            _server.start()
        return _server


def status() -> dict:
    """For the screen: is the server brokers are told about actually up?"""
    if not enabled():
        return {"mode": "off"}
    if _server is None:
        return {"mode": "not_started"}
    return {"mode": _server.state, "port": _server.port, "error": _server.error}


def drop_sessions(route_id: int) -> int:
    return _server.drop_route(route_id) if _server is not None else 0


def fingerprints() -> list[dict]:
    """[{type, fingerprint}] of the host keys — what a broker checks on first
    connect. Empty when this app does not serve SFTP itself."""
    if not enabled():
        return []
    keys = _server.host_keys if _server is not None and _server.host_keys else None
    if keys is None:
        try:
            keys = load_host_keys()
        except Exception:  # noqa: BLE001
            log.exception("sftp server: cannot read its host keys")
            return []
    return [{"type": k.get_name(), "fingerprint": key_fingerprint(k)} for k in keys]


def start(app) -> None:
    """Attach the server to the app's startup, the same way the collectors are."""
    if not enabled():
        log.info("sftp server off (SFTP_SERVER_ENABLED=0) — brokers cannot sign in "
                 "to upload unless another server fronts SFTP_ROOT")
        return

    @app.on_event("startup")
    async def _start_sftp_server() -> None:    # pragma: no cover - wiring
        # Returns at once: keys, binding and serving all happen on its thread.
        # Never takes the API down with it — status() says what went wrong.
        try:
            ensure_running()
        except Exception:  # noqa: BLE001
            log.exception("sftp server could not start")

    @app.on_event("shutdown")
    async def _stop_sftp_server() -> None:     # pragma: no cover - wiring
        if _server is not None:
            _server.stop()
