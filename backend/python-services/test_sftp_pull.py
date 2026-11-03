"""Tests for sftp_pull — collecting bordereaux from an EXTERNAL SFTP server.

No database and no Docker. An SFTP server runs in this process (paramiko's
server side) over a temporary folder, with switches that reproduce what real
servers do differently:

  posix_rename=False      FileZilla Server, Azure Blob SFTP (no extension)
  rename="unsupported"    AWS Transfer on S3 refusing rename -> copy + delete
  no_mode=True            Azure: listings with no mode bits at all
  mkdir_no_attrs=True     Azure: MKDIR refused when it carries permissions
  deny_write/delete/mkdir a pick-up login with narrower rights
  rename="denied"         renames refused outright (no copy must be attempted)
  lock={names}            a file its uploader still holds (Cerberus): open fails
  methods="keyboard-interactive"   servers that only offer that for passwords
  one_attempt=True        a Go server (SFTPGo): hangs up on a 2nd login attempt
  otp=True                keyboard-interactive asks for a one-time code
  host_keys=[...]         several host keys, changeable between connections

collect_route is driven with its database seams (load_config, the advisory
lock, the "already landed?" query, the landing itself) replaced by recorders.

Run:  .venv/bin/python -m pytest -q test_sftp_pull.py
"""
import os

# Never a real database: nothing here needs one, and an import must not find one.
os.environ.setdefault("DATABASE_URL", "postgresql+psycopg2://nobody@127.0.0.1:1/none")

import io
import posixpath
import socket
import threading
import time
import types

import paramiko
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed25519, rsa
from paramiko import (AUTH_FAILED, AUTH_SUCCESSFUL, OPEN_SUCCEEDED, SFTPAttributes,
                      SFTPHandle, SFTPServer, SFTPServerInterface, ServerInterface)
from paramiko.sftp import (SFTP_FAILURE, SFTP_OK, SFTP_OP_UNSUPPORTED,
                           SFTP_PERMISSION_DENIED)

import intake_secrets
import sftp_pull


# ── an SFTP server in this process ──────────────────────────────────────────

def _status(exc):
    return SFTPServer.convert_errno(exc.errno)


class _Handle(SFTPHandle):
    def stat(self):
        try:
            return SFTPAttributes.from_stat(os.fstat(self.readfile.fileno()))
        except OSError as exc:
            return _status(exc)

    def chattr(self, attr):
        return SFTP_OK


class _FS(SFTPServerInterface):
    def __init__(self, server, root, quirks, *a, **kw):
        super().__init__(server, *a, **kw)
        self.root, self.q = root, quirks

    def canonicalize(self, path):
        if not path.startswith("/"):
            path = posixpath.join(self.q.get("home", "/"), path)
        out = posixpath.normpath(path)
        return "/" if out in ("", ".") else out

    def _real(self, path):
        return os.path.join(self.root, self.canonicalize(path).lstrip("/"))

    def _attr(self, st, name=None):
        a = SFTPAttributes.from_stat(st, name)
        if self.q.get("no_mode"):
            a.st_mode = None
        return a

    def list_folder(self, path):
        real = self._real(path)
        try:
            out = []
            for n in os.listdir(real):
                a = self._attr(os.lstat(os.path.join(real, n)), n)
                try:
                    n.encode("utf-8")
                except UnicodeEncodeError:          # a Latin-1 name on disk
                    a.filename = os.fsencode(n)     # sent as the raw bytes
                out.append(a)
            return out
        except OSError as exc:
            return _status(exc)

    def stat(self, path):
        try:
            return self._attr(os.stat(self._real(path)))
        except OSError as exc:
            return _status(exc)

    def lstat(self, path):
        try:
            return self._attr(os.lstat(self._real(path)))
        except OSError as exc:
            return _status(exc)

    def open(self, path, flags, attr):
        if (flags & (os.O_WRONLY | os.O_RDWR)) and self.q.get("deny_write"):
            return SFTP_PERMISSION_DENIED
        if posixpath.basename(path) in self.q.get("lock", ()):
            return SFTP_FAILURE                       # held by its uploader
        real = self._real(path)
        try:
            fd = os.open(real, flags, 0o644)
        except OSError as exc:
            return _status(exc)
        if flags & os.O_WRONLY:
            fstr = "ab" if flags & os.O_APPEND else "wb"
        elif flags & os.O_RDWR:
            fstr = "a+b" if flags & os.O_APPEND else "r+b"
        else:
            fstr = "rb"
        f = os.fdopen(fd, fstr)
        h = _Handle(flags)
        h.filename, h.readfile, h.writefile = real, f, f
        return h

    def remove(self, path):
        if self.q.get("deny_delete"):
            return SFTP_PERMISSION_DENIED
        try:
            os.remove(self._real(path))
        except OSError as exc:
            return _status(exc)
        return SFTP_OK

    def rename(self, old, new):
        if self.q.get("rename") == "unsupported":
            return SFTP_OP_UNSUPPORTED
        if self.q.get("rename") == "denied":
            return SFTP_PERMISSION_DENIED
        if os.path.exists(self._real(new)):       # plain SFTP rename never replaces
            return SFTP_FAILURE
        try:
            os.rename(self._real(old), self._real(new))
        except OSError as exc:
            return _status(exc)
        return SFTP_OK

    def posix_rename(self, old, new):
        if self.q.get("rename") == "denied":
            return SFTP_PERMISSION_DENIED
        if not self.q.get("posix_rename", True):
            return SFTP_OP_UNSUPPORTED
        try:
            os.replace(self._real(old), self._real(new))
        except OSError as exc:
            return _status(exc)
        return SFTP_OK

    def mkdir(self, path, attr):
        if self.q.get("deny_mkdir"):
            return SFTP_PERMISSION_DENIED
        if self.q.get("mkdir_no_attrs") and attr is not None and attr.st_mode is not None:
            return SFTP_FAILURE
        try:
            os.mkdir(self._real(path))
        except OSError as exc:
            return _status(exc)
        return SFTP_OK

    def rmdir(self, path):
        try:
            os.rmdir(self._real(path))
        except OSError as exc:
            return _status(exc)
        return SFTP_OK


class _Auth(ServerInterface):
    def __init__(self, srv, transport=None):
        self.srv, self.t, self.attempts = srv, transport, 0

    def _attempt(self):
        """A Go SSH server hangs up on the second attempt of a connection."""
        self.attempts += 1
        if self.srv.one_attempt and self.attempts > 1:
            self.srv.dropped += 1
            self.t.close()
            return False
        return True

    def get_allowed_auths(self, username):
        return self.srv.methods

    def check_auth_password(self, username, password):
        self.srv.auth_log.append(("password", username))
        if not self._attempt():
            return AUTH_FAILED
        if "password" in self.srv.methods and username == "bob" \
                and password == self.srv.password:
            return AUTH_SUCCESSFUL
        return AUTH_FAILED

    def check_auth_publickey(self, username, key):
        self.srv.auth_log.append(("publickey", username))
        if not self._attempt():
            return AUTH_FAILED
        if (self.srv.pubkey is not None and username == "bob"
                and key.get_base64() == self.srv.pubkey.get_base64()):
            return AUTH_SUCCESSFUL
        return AUTH_FAILED

    def check_auth_interactive(self, username, submethods):
        self.srv.auth_log.append(("keyboard-interactive", username))
        if not self._attempt() or "keyboard-interactive" not in self.srv.methods:
            return AUTH_FAILED
        if self.srv.otp:
            return paramiko.InteractiveQuery("", "", ("Password: ", False),
                                             ("Verification code: ", True))
        return paramiko.InteractiveQuery("", "", ("Password: ", False))

    def check_auth_interactive_response(self, responses):
        want = [self.srv.password, "123456"] if self.srv.otp else [self.srv.password]
        return AUTH_SUCCESSFUL if list(responses) == want else AUTH_FAILED

    def check_channel_request(self, kind, chanid):
        return OPEN_SUCCEEDED


def _ed25519_paramiko(key=None):
    key = key or ed25519.Ed25519PrivateKey.generate()
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.OpenSSH,
                            serialization.NoEncryption()).decode()
    return paramiko.Ed25519Key.from_private_key(io.StringIO(pem)), key


_HOST_KEYS = {}


def _host_key(kind):
    if kind not in _HOST_KEYS:
        _HOST_KEYS[kind] = (paramiko.RSAKey.generate(2048) if kind == "rsa"
                            else _ed25519_paramiko()[0])
    return _HOST_KEYS[kind]


class StubSFTP:
    def __init__(self, root, *, host_key="ed25519", host_keys=None, quirks=None,
                 password="s3cret", pubkey=None, methods="password,publickey",
                 one_attempt=False, otp=False):
        self.root, self.quirks = root, dict(quirks or {})
        self.host_key = _host_key(host_key) if isinstance(host_key, str) else host_key
        self.host_keys = list(host_keys) if host_keys else [self.host_key]
        self.password, self.pubkey, self.methods = password, pubkey, methods
        self.one_attempt, self.otp, self.dropped = one_attempt, otp, 0
        self.auth_log, self.transports = [], []
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(8)
        self.port = self.sock.getsockname()[1]
        self._stop = False
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self):
        while not self._stop:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def _serve(self, conn):
        t = paramiko.Transport(conn)
        for k in self.host_keys:
            t.add_server_key(k)
        t.set_subsystem_handler("sftp", SFTPServer, _FS, self.root, self.quirks)
        self.transports.append(t)
        try:
            t.start_server(server=_Auth(self, t))
        except Exception:  # noqa: BLE001
            pass

    def path(self, rel):
        return os.path.join(self.root, rel.lstrip("/"))

    def put(self, rel, data=b"policy,premium\n1,100\n", age=120):
        p = self.path(rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "wb") as f:
            f.write(data)
        t = time.time() - age
        os.utime(p, (t, t))
        return p

    def listing(self, rel):
        p = self.path(rel)
        return sorted(os.listdir(p)) if os.path.isdir(p) else None

    def close(self):
        self._stop = True
        self.sock.close()
        for t in self.transports:
            try:
                t.close()
            except Exception:  # noqa: BLE001
                pass


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    from cryptography.fernet import Fernet
    monkeypatch.setenv("INTAKE_SECRET_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("SFTP_QUIET_SECONDS", "1")
    monkeypatch.setenv("SFTP_PULL_ALLOW_PRIVATE", "1")
    monkeypatch.delenv("SFTP_PULL_MAX_FILES", raising=False)
    monkeypatch.delenv("INTAKE_MAX_FILE_MB", raising=False)


@pytest.fixture
def make_server(tmp_path):
    made = []

    def make(**kw):
        root = tmp_path / f"srv{len(made)}"
        root.mkdir()
        srv = StubSFTP(str(root), **kw)
        made.append(srv)
        return srv
    yield make
    for srv in made:
        srv.close()


def settings(srv, **over):
    s = {"host": "127.0.0.1", "port": srv.port, "username": "bob", "auth": "password",
         "password": "s3cret", "remote_dir": "/outgoing", "after": "move"}
    s.update(over)
    return s


# ── the collect harness: every database seam replaced by a recorder ─────────

class Harness:
    def __init__(self, monkeypatch, cfg, *, outcome="accepted"):
        self.cfg, self.landed, self.status = cfg, {}, []
        monkeypatch.setattr(sftp_pull, "load_config", lambda s, rid: self.cfg)
        monkeypatch.setattr(sftp_pull, "_try_lock", lambda s, rid: (lambda: None))
        monkeypatch.setattr(sftp_pull, "_save_status",
                            lambda s, rid, patch: self.status.append(patch))
        monkeypatch.setattr(sftp_pull, "_already_landed",
                            lambda s, route, key: key in self.landed)

        def land(session, route, cfg, name, data, key, *, oversize, real_size, cap):
            self.landed[key] = types.SimpleNamespace(name=name, data=data,
                                                     oversize=oversize, real_size=real_size)
            return types.SimpleNamespace(outcome=outcome, turned_away_reason=None,
                                         id=len(self.landed))
        monkeypatch.setattr(sftp_pull, "_land", land)
        self.session = types.SimpleNamespace(commit=lambda: None, rollback=lambda: None)
        self.route = types.SimpleNamespace(id=7, tenant_id=1, channel="sftp",
                                           address="sftp://bob@127.0.0.1:1/outgoing",
                                           is_enabled=True)

    def run(self):
        return sftp_pull.collect_route(self.session, self.route)

    def names(self):
        return sorted(v.name for v in self.landed.values())


def pull_config(srv, **over):
    """A stored config exactly as create would save it: tested, pinned, encrypted."""
    s = settings(srv, **over)
    test = sftp_pull.test_connection(s)
    assert test["ok"], test
    s["fingerprint"] = test["fingerprint"]
    s.setdefault("interval_minutes", 15)
    _, cfg = sftp_pull.prepare_route(s)
    return cfg


# ── pure functions ──────────────────────────────────────────────────────────

def test_clean_settings_tidies_and_refuses_in_plain_words():
    st = sftp_pull.clean_settings({"host": " sftp://files.broker.com/ ", "port": "2222",
                                   "username": " bob ", "password": " pw ",
                                   "remote_dir": "out\\bdx\\", "after": "move"})
    assert st["host"] == "files.broker.com" and st["port"] == 2222
    assert st["username"] == "bob" and st["secret"] == " pw "        # passwords untouched
    assert st["remote_dir"] == "out/bdx" and st["processed_dir"] == "out/bdx/processed"
    assert sftp_pull.clean_settings({"host": "h", "username": "u", "password": "p",
                                     "remote_dir": "", "after": "move"}
                                    )["processed_dir"] == "processed"
    assert sftp_pull.clean_settings({"host": "h", "username": "u", "password": "p",
                                     "after": "delete", "processed_dir": "x"}
                                    )["processed_dir"] is None
    assert sftp_pull.clean_settings({"host": "[2001:db8::1]", "username": "u",
                                     "password": "p"})["host"] == "2001:db8::1"
    for bad, words in [({"host": ""}, "host name"),
                       ({"host": "h:22"}, "port has its own box"),
                       ({"host": "bob@h"}, "user name has its own box"),
                       ({"host": "h", "port": 70000}, "between 1 and 65535"),
                       ({"host": "h", "username": ""}, "user name"),
                       ({"host": "h", "username": "u"}, "Enter the password"),
                       ({"host": "h", "username": "u", "auth": "key"}, "private key"),
                       ({"host": "h", "username": "u", "password": "p",
                         "remote_dir": "/in", "processed_dir": "/in/"}, "must be different")]:
        with pytest.raises(sftp_pull.SettingsError) as e:
            sftp_pull.clean_settings(bad)
        assert words in str(e.value)
    with pytest.raises(sftp_pull.SettingsError, match="5, 15 or 60"):
        sftp_pull.clean_settings({"host": "h", "username": "u", "password": "p",
                                  "interval_minutes": 7, "fingerprint": "x"}, for_create=True)
    with pytest.raises(sftp_pull.SettingsError, match="Test the connection first"):
        sftp_pull.clean_settings({"host": "h", "username": "u", "password": "p"},
                                 for_create=True)


def test_address_and_route_marker():
    st = {"host": "files.broker.com", "port": 22, "username": "bob", "remote_dir": "/out"}
    assert sftp_pull.build_address(st) == "sftp://bob@files.broker.com:22/out"
    st6 = dict(st, host="2001:db8::1", remote_dir="out")
    # A relative folder keeps its "~/" so it never reads as (or collides with) "/out".
    assert sftp_pull.build_address(st6) == "sftp://bob@[2001:db8::1]:22/~/out"
    pull = types.SimpleNamespace(channel="sftp", address="sftp://bob@h:22/out")
    local = types.SimpleNamespace(channel="sftp", address="insurisk/corvin")
    assert sftp_pull.is_pull_route(pull) and not sftp_pull.is_pull_route(local)
    import intake_service
    assert intake_service.is_external_sftp(pull) and not intake_service.is_external_sftp(local)
    assert intake_service.display_address(pull) == "sftp://bob@h:22/out"


def test_skipped_names():
    for n in (".hidden", "a.xlsx.filepart", "a.part", "a.tmp", "a.crdownload", "~$a.xlsx",
              "a.csv.TEMP", "a.csv.uploading", "a.partial", "A.XLSX.FILEPART"):
        assert sftp_pull.is_skipped_name(n), n
    for n in ("a.xlsx", "July 2026 bdx é.csv", "a.partner.csv"):
        assert not sftp_pull.is_skipped_name(n), n


def test_settled_needs_two_equal_listings_and_quiet():
    now = 1_000_000.0
    first = {"old.csv": {"size": 10, "mtime": now - 100},
             "grew.csv": {"size": 10, "mtime": now - 100},     # coarse/preserved mtime
             "new.csv": {"size": 10, "mtime": now - 2},
             "future.csv": {"size": 10, "mtime": now + 3},
             "empty.csv": {"size": 0, "mtime": now - 100},
             "oldempty.csv": {"size": 0, "mtime": now - 5000},
             "nomtime.csv": {"size": 5, "mtime": None}}
    second = dict(first)
    second["grew.csv"] = {"size": 20, "mtime": now - 100}
    second["late.csv"] = {"size": 1, "mtime": now - 100}       # not in the first listing
    ready, waiting = sftp_pull.settled(first, second, now, 10)
    assert ready == ["oldempty.csv", "old.csv", "nomtime.csv"]     # oldest first, no time last
    assert waiting == 5


def test_idempotency_key_and_fingerprints():
    k = sftp_pull.idempotency_key(7, "/out/a.csv", 10, 123)
    assert k == sftp_pull.idempotency_key(7, "/out/a.csv", 10, 123)
    assert k != sftp_pull.idempotency_key(7, "/out/a.csv", 11, 123)
    assert k != sftp_pull.idempotency_key(8, "/out/a.csv", 10, 123)
    assert sftp_pull._same_fingerprint("SHA256:abc=", "abc")
    assert not sftp_pull._same_fingerprint("", "")
    key, _ = _ed25519_paramiko()
    info = sftp_pull.host_key_info(key)
    assert info["type"] == "ssh-ed25519" and info["fingerprint_sha256"].startswith("SHA256:")
    assert "=" not in info["fingerprint_sha256"]
    assert info["fingerprint_sha256"] == key.fingerprint      # what ssh-keygen -l prints


def test_is_due():
    from datetime import datetime, timedelta, timezone
    now = datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc)
    assert sftp_pull.is_due({}, now)
    iso = lambda dt: dt.isoformat().replace("+00:00", "Z")   # noqa: E731
    assert not sftp_pull.is_due({"last_checked_at": iso(now - timedelta(minutes=3)),
                                 "interval_minutes": 5}, now)
    assert sftp_pull.is_due({"last_checked_at": iso(now - timedelta(minutes=5)),
                             "interval_minutes": 5}, now)
    assert not sftp_pull.is_due({"last_checked_at": iso(now - timedelta(minutes=50)),
                                 "interval_minutes": 60}, now)


def test_secrets_round_trip_rotation_and_key_change(monkeypatch):
    from cryptography.fernet import Fernet
    tok = intake_secrets.encrypt("pä55 word")
    assert tok != "pä55 word" and intake_secrets.decrypt(tok) == "pä55 word"
    old = os.environ["INTAKE_SECRET_KEY"]
    monkeypatch.setenv("INTAKE_SECRET_KEY", Fernet.generate_key().decode() + "," + old)
    assert intake_secrets.decrypt(tok) == "pä55 word"           # rotation: old key still reads
    monkeypatch.setenv("INTAKE_SECRET_KEY", Fernet.generate_key().decode())
    with pytest.raises(intake_secrets.SecretUnreadable, match="Set this channel up again"):
        intake_secrets.decrypt(tok)
    monkeypatch.setenv("INTAKE_SECRET_KEY", "just a long passphrase, not a fernet key")
    assert intake_secrets.decrypt(intake_secrets.encrypt("x")) == "x"
    monkeypatch.delenv("INTAKE_SECRET_KEY")
    monkeypatch.setenv("JWT_SECRET", "jwt-one")
    tok2 = intake_secrets.encrypt("y")
    assert intake_secrets.decrypt(tok2) == "y"
    monkeypatch.setenv("JWT_SECRET", "jwt-two")
    with pytest.raises(intake_secrets.SecretUnreadable):
        intake_secrets.decrypt(tok2)


def test_address_policy(monkeypatch):
    monkeypatch.delenv("SFTP_PULL_ALLOW_PRIVATE")
    assert not sftp_pull._address_allowed("127.0.0.1")          # refused unless allowed
    assert not sftp_pull._address_allowed("192.168.2.11")
    monkeypatch.setenv("SFTP_PULL_ALLOW_PRIVATE", "1")
    assert not sftp_pull._address_allowed("169.254.169.254")    # cloud metadata, always
    assert not sftp_pull._address_allowed("0.0.0.0")
    assert sftp_pull._address_allowed("127.0.0.1")
    assert sftp_pull._address_allowed("93.184.216.34")
    monkeypatch.setenv("SFTP_PULL_ALLOW_PRIVATE", "0")
    assert not sftp_pull._address_allowed("127.0.0.1")
    assert not sftp_pull._address_allowed("10.1.2.3")
    assert not sftp_pull._address_allowed("::ffff:192.168.1.1")
    assert sftp_pull._address_allowed("93.184.216.34")


# ── private keys ────────────────────────────────────────────────────────────

def _pem(key, fmt, password=None):
    enc = (serialization.BestAvailableEncryption(password.encode()) if password
           else serialization.NoEncryption())
    return key.private_bytes(serialization.Encoding.PEM, fmt, enc).decode()


@pytest.mark.parametrize("make,fmt,pw,cls", [
    (lambda: rsa.generate_private_key(65537, 2048), serialization.PrivateFormat.TraditionalOpenSSL, None, "RSAKey"),
    (lambda: rsa.generate_private_key(65537, 2048), serialization.PrivateFormat.PKCS8, None, "RSAKey"),
    (lambda: rsa.generate_private_key(65537, 2048), serialization.PrivateFormat.PKCS8, "pass phrase", "RSAKey"),
    (lambda: rsa.generate_private_key(65537, 2048), serialization.PrivateFormat.OpenSSH, "pass phrase", "RSAKey"),
    (lambda: ed25519.Ed25519PrivateKey.generate(), serialization.PrivateFormat.OpenSSH, None, "Ed25519Key"),
    (lambda: ed25519.Ed25519PrivateKey.generate(), serialization.PrivateFormat.PKCS8, None, "Ed25519Key"),
    (lambda: ec.generate_private_key(ec.SECP256R1()), serialization.PrivateFormat.TraditionalOpenSSL, None, "ECDSAKey"),
    (lambda: ec.generate_private_key(ec.SECP384R1()), serialization.PrivateFormat.OpenSSH, None, "ECDSAKey"),
])
def test_private_key_formats(make, fmt, pw, cls):
    k = sftp_pull.load_private_key(_pem(make(), fmt, pw), pw)
    assert type(k).__name__ == cls


def test_private_key_problems_in_plain_words():
    key = ed25519.Ed25519PrivateKey.generate()
    pem = _pem(key, serialization.PrivateFormat.OpenSSH)
    # Pasting squashes newlines and adds Windows line ends: still read.
    squashed = pem.replace("\n", " ").replace("-----END", "\r\n-----END")
    assert type(sftp_pull.load_private_key(squashed)).__name__ == "Ed25519Key"
    # A passphrase given for a key that has none is forgiven.
    assert sftp_pull.load_private_key(pem, "unneeded")
    locked = _pem(key, serialization.PrivateFormat.OpenSSH, "right")
    cases = [
        ("PuTTY-User-Key-File-3: ssh-ed25519\nEncryption: none\n", None, "PuTTYgen"),
        ("ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIA bob@pc", None, "PUBLIC key"),
        ("hello", None, "does not look like a private key"),
        (locked, None, "passphrase"),
        (locked, "wrong", "passphrase may be"),
        ("-----BEGIN OPENSSH PRIVATE KEY-----\nAAAA\n-----END OPENSSH PRIVATE KEY-----", None,
         "could not be read"),
    ]
    for text, pw, words in cases:
        with pytest.raises(sftp_pull.PullError) as e:
            sftp_pull.load_private_key(text, pw)
        assert e.value.code == "auth" and words in e.value.message, (words, e.value.message)


# ── Test button against a live server ───────────────────────────────────────

def test_test_connection_happy_path_leaves_nothing_behind(make_server):
    srv = make_server()
    srv.put("outgoing/a.xlsx")
    srv.put("outgoing/b report é.csv")
    srv.put("outgoing/.hidden")
    srv.put("outgoing/c.xlsx.filepart")
    os.makedirs(srv.path("outgoing/sub"))
    r = sftp_pull.test_connection(settings(srv))
    assert r["ok"] and r["error"] is None and r["error_code"] is None, r
    assert r["fingerprint"] == srv.host_key.fingerprint and r["key_type"] == "ssh-ed25519"
    assert r["files_found"] == 2 and r["sample"] == ["a.xlsx", "b report é.csv"]
    assert r["can_write"] is True
    assert any("created the folder" in w for w in r["warnings"])
    # The probe file is gone from both folders; real files untouched.
    assert srv.listing("outgoing") == sorted([".hidden", "a.xlsx", "b report é.csv",
                                              "c.xlsx.filepart", "processed", "sub"])
    assert srv.listing("outgoing/processed") == []


@pytest.mark.parametrize("quirks", [
    {"posix_rename": False},                                   # FileZilla / Azure
    {"posix_rename": False, "rename": "unsupported"},          # AWS Transfer on S3
    {"posix_rename": False, "no_mode": True, "mkdir_no_attrs": True},  # Azure Blob
])
def test_test_connection_on_awkward_servers(make_server, quirks):
    srv = make_server(quirks=quirks)
    srv.put("outgoing/a.xlsx")
    r = sftp_pull.test_connection(settings(srv))
    assert r["ok"] and r["can_write"] is True, r
    assert r["files_found"] == 1
    assert srv.listing("outgoing/processed") == []


def test_test_connection_relative_folder_and_rsa_host_key(make_server):
    srv = make_server(host_key="rsa", quirks={"home": "/home/bob"})
    srv.put("home/bob/out/a.csv")
    r = sftp_pull.test_connection(settings(srv, remote_dir="out", processed_dir="done"))
    assert r["ok"] and r["key_type"] == "ssh-rsa" and r["can_write"] is True, r
    assert os.path.isdir(srv.path("home/bob/done"))            # relative to the login folder


def test_test_connection_write_checks(make_server):
    srv = make_server(quirks={"deny_write": True})
    srv.put("outgoing/a.xlsx")
    r = sftp_pull.test_connection(settings(srv))
    assert r["ok"] and r["can_write"] is None                  # processed could be made
    assert any("could not put a test file" in w for w in r["warnings"])

    # Neither a file nor the processed folder can be created (read-only box):
    # nothing can ever be moved, so that is a definite no.
    srv0 = make_server(quirks={"deny_write": True, "deny_mkdir": True})
    srv0.put("outgoing/a.xlsx")
    r = sftp_pull.test_connection(settings(srv0))
    assert r["ok"] and r["can_write"] is False, r
    assert any("left on the server" in w for w in r["warnings"])

    srv2 = make_server(quirks={"deny_mkdir": True})
    os.makedirs(srv2.path("outgoing"))
    r = sftp_pull.test_connection(settings(srv2))
    assert r["ok"] and r["can_write"] is False
    assert any("could not move a file" in w for w in r["warnings"])
    assert srv2.listing("outgoing") == []                      # probe cleaned up

    srv3 = make_server(quirks={"deny_delete": True})
    os.makedirs(srv3.path("outgoing"))
    r = sftp_pull.test_connection(settings(srv3, after="delete"))
    assert r["ok"] and r["can_write"] is False
    assert any("cannot delete" in w for w in r["warnings"])


def test_test_connection_many_files_warns(make_server):
    srv = make_server()
    for i in range(12):
        srv.put(f"outgoing/f{i:02}.csv")
    r = sftp_pull.test_connection(settings(srv))
    assert r["files_found"] == 12 and len(r["sample"]) == 10
    assert any("12 files" in w for w in r["warnings"])


def test_key_and_keyboard_interactive_logins(make_server):
    pkey, raw = _ed25519_paramiko()
    srv = make_server(pubkey=pkey, methods="publickey")
    os.makedirs(srv.path("outgoing"))
    pem = _pem(raw, serialization.PrivateFormat.OpenSSH, "pp")
    r = sftp_pull.test_connection(settings(srv, auth="key", password=None,
                                           private_key=pem, passphrase="pp"))
    assert r["ok"], r
    r = sftp_pull.test_connection(settings(srv))               # password to a key-only server
    assert r["error_code"] == "auth" and "wants a key" in r["error"]
    assert r["fingerprint"]                                    # still shown when login fails

    srv2 = make_server(methods="keyboard-interactive")
    os.makedirs(srv2.path("outgoing"))
    assert sftp_pull.test_connection(settings(srv2))["ok"]
    srv3 = make_server(methods="password")
    os.makedirs(srv3.path("outgoing"))
    r = sftp_pull.test_connection(settings(srv3, auth="key", password=None, private_key=pem,
                                           passphrase="pp"))
    assert r["error_code"] == "auth" and "wants a password" in r["error"]


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _fake_server(behaviour):
    """A TCP listener that is not SFTP: 'ftp' greets like FTP, 'close' hangs up."""
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen(4)

    def run():
        while True:
            try:
                c, _ = sock.accept()
            except OSError:
                return
            if behaviour == "ftp":
                c.sendall(b"220 FileZilla Server ready\r\n")
                time.sleep(5)
            elif behaviour == "silent":
                time.sleep(4)
            c.close()
    threading.Thread(target=run, daemon=True).start()
    return sock


@pytest.mark.parametrize("over,code,words", [
    ({"password": "wrong"}, "auth", "did not accept the user name and password"),
    ({"remote_dir": "/nope"}, "no_dir", "no folder called"),
    ({"host": "no-such-host.invalid"}, "dns", "could not find a server"),
    ({"host": "169.254.169.254"}, "refused", "private or internal address"),
])
def test_test_connection_errors(make_server, over, code, words):
    srv = make_server()
    os.makedirs(srv.path("outgoing"))
    r = sftp_pull.test_connection(settings(srv, **over))
    assert not r["ok"] and r["error_code"] == code and words in r["error"], r


def test_network_errors_in_plain_words(monkeypatch):
    r = sftp_pull.test_connection({"host": "127.0.0.1", "port": _free_port(),
                                   "username": "u", "password": "p"})
    assert r["error_code"] == "refused" and "refused the connection" in r["error"]
    ftp = _fake_server("ftp")
    r = sftp_pull.test_connection({"host": "127.0.0.1", "port": ftp.getsockname()[1],
                                   "username": "u", "password": "p"})
    assert r["error_code"] == "protocol" and "This is an FTP server" in r["error"], r
    ftp.close()
    closer = _fake_server("close")
    r = sftp_pull.test_connection({"host": "127.0.0.1", "port": closer.getsockname()[1],
                                   "username": "u", "password": "p"})
    assert r["error_code"] == "refused" and "closed the connection" in r["error"], r
    closer.close()
    monkeypatch.setenv("SFTP_PULL_ALLOW_PRIVATE", "0")
    r = sftp_pull.test_connection({"host": "127.0.0.1", "username": "u", "password": "p"})
    assert r["error_code"] == "refused" and "private" in r["error"]
    r = sftp_pull.test_connection({"host": "", "username": "u", "password": "p"})
    assert r["error_code"] is None and "host name" in r["error"]


def test_prepare_route_pins_and_encrypts(make_server):
    srv = make_server()
    os.makedirs(srv.path("outgoing"))
    s = settings(srv, interval_minutes=5)
    with pytest.raises(sftp_pull.SetupRefused, match="not the one you tested"):
        sftp_pull.prepare_route(dict(s, fingerprint="SHA256:somethingelse"))
    s["fingerprint"] = sftp_pull.test_connection(s)["fingerprint"]
    address, cfg = sftp_pull.prepare_route(s)
    assert address == f"sftp://bob@127.0.0.1:{srv.port}/outgoing"
    assert cfg["host_key"]["fingerprint_sha256"] == srv.host_key.fingerprint
    assert cfg["secret_enc"] != "s3cret" and intake_secrets.decrypt(cfg["secret_enc"]) == "s3cret"
    assert cfg["interval_minutes"] == 5 and cfg["processed_dir"] == "/outgoing/processed"
    view = sftp_pull.public_view(cfg)
    assert view["has_secret"] is True and view["fingerprint"].startswith("SHA256:")
    assert "secret_enc" not in view and "s3cret" not in str(view)
    assert set(view) == {"host", "port", "username", "auth", "remote_dir", "after",
                         "processed_dir", "interval_minutes", "fingerprint",
                         "last_checked_at", "last_error", "last_collected", "has_secret"}


# ── collecting ──────────────────────────────────────────────────────────────

def test_collect_lands_finished_files_and_moves_them(make_server, monkeypatch):
    srv = make_server()
    srv.put("outgoing/a.xlsx", b"A" * 10)
    srv.put("outgoing/b report é.csv", b"B" * 20, age=300)
    srv.put("outgoing/.hidden")
    srv.put("outgoing/c.xlsx.filepart")
    srv.put("outgoing/~$a.xlsx")
    os.makedirs(srv.path("outgoing/sub"))
    os.symlink(srv.path("outgoing/a.xlsx"), srv.path("outgoing/link.xlsx"))
    srv.put("outgoing/processed/a.xlsx", b"last month")       # a name clash
    h = Harness(monkeypatch, pull_config(srv))
    # (The "touched within the quiet window" clock rule is timing-bound here,
    # so it is tested on settled() directly; the size rule further down.)
    out = h.run()
    assert out.get("error") is None, out
    assert out["accepted"] == 2 and out["skipped_still_writing"] == 0
    assert h.names() == ["a.xlsx", "b report é.csv"]
    assert [v.data for v in h.landed.values()] == [b"B" * 20, b"A" * 10]   # oldest first
    left = srv.listing("outgoing")
    assert "a.xlsx" not in left and "b report é.csv" not in left
    assert {".hidden", "c.xlsx.filepart", "~$a.xlsx", "sub", "link.xlsx"} <= set(left)
    done = srv.listing("outgoing/processed")
    assert "b report é.csv" in done and "a.xlsx" in done
    stamped = [n for n in done if n.startswith("a_") and n.endswith(".xlsx")]
    assert len(stamped) == 1                                   # clash -> a_<UTC time>.xlsx
    assert open(srv.path("outgoing/processed/a.xlsx"), "rb").read() == b"last month"
    assert h.status[-1]["last_error"] is None and h.status[-1]["last_collected"] == 2
    assert h.status[-1]["last_checked_at"].endswith("Z")
    assert out["looked_in"] == f"sftp://bob@127.0.0.1:{srv.port}/outgoing"


@pytest.mark.parametrize("quirks", [
    {"posix_rename": False},                                         # FileZilla
    {"posix_rename": False, "rename": "unsupported"},                # AWS S3: copy + delete
    {"posix_rename": False, "no_mode": True, "mkdir_no_attrs": True},  # Azure
])
def test_collect_on_awkward_servers(make_server, monkeypatch, quirks):
    srv = make_server(quirks=quirks)
    srv.put("outgoing/a.xlsx", b"A" * 70000)
    srv.put("outgoing/processed/a.xlsx", b"old")                # rename onto it would fail
    h = Harness(monkeypatch, pull_config(srv))
    out = h.run()
    assert out.get("error") is None and out["accepted"] == 1, out
    assert h.names() == ["a.xlsx"]                               # the folder was not a "file"
    done = srv.listing("outgoing/processed")
    assert len(done) == 2 and srv.listing("outgoing") == ["processed"]
    moved = [n for n in done if n != "a.xlsx"][0]
    assert open(srv.path(f"outgoing/processed/{moved}"), "rb").read() == b"A" * 70000


def test_collect_creates_processed_folder_on_azure(make_server, monkeypatch):
    srv = make_server(quirks={"posix_rename": False, "no_mode": True, "mkdir_no_attrs": True})
    os.makedirs(srv.path("outgoing"))
    cfg = pull_config(srv)
    os.rmdir(srv.path("outgoing/processed"))                    # made by the Test; gone again
    srv.put("outgoing/a.xlsx")
    h = Harness(monkeypatch, cfg)
    out = h.run()
    assert out.get("error") is None and out["accepted"] == 1, out
    assert srv.listing("outgoing/processed") == ["a.xlsx"]


def test_collect_a_file_that_cannot_be_moved_is_not_landed_twice(make_server, monkeypatch):
    srv = make_server(quirks={"posix_rename": False, "rename": "unsupported",
                              "deny_write": True})
    srv.put("outgoing/a.xlsx")
    os.makedirs(srv.path("outgoing/processed"))
    h = Harness(monkeypatch, pull_config(srv))
    out = h.run()
    assert out["accepted"] == 1 and "could not be dealt with" in out["error"], out
    assert h.status[-1]["last_error"] == out["error"]
    out2 = h.run()
    assert out2["accepted"] == 0 and out2["already_seen"] == 1
    assert len(h.landed) == 1                                    # never landed twice
    assert srv.listing("outgoing") == ["a.xlsx", "processed"]


def test_collect_delete_mode(make_server, monkeypatch):
    srv = make_server()
    srv.put("outgoing/a.xlsx")
    h = Harness(monkeypatch, pull_config(srv, after="delete"))
    out = h.run()
    assert out["accepted"] == 1 and out.get("error") is None
    assert srv.listing("outgoing") == []


def test_collect_skips_a_file_still_growing(make_server, monkeypatch):
    srv = make_server()
    p = srv.put("outgoing/big.csv", b"x" * 100)
    srv.put("outgoing/done.csv")
    h = Harness(monkeypatch, pull_config(srv))

    def grow():
        time.sleep(0.3)
        st = os.stat(p)
        with open(p, "ab") as f:
            f.write(b"y" * 100)
        os.utime(p, (st.st_atime, st.st_mtime))    # the time says nothing (preserved/coarse)
    threading.Thread(target=grow).start()
    out = h.run()
    assert h.names() == ["done.csv"] and out["skipped_still_writing"] == 1
    assert "big.csv" in srv.listing("outgoing")


def test_collect_oversize_is_refused_without_downloading(make_server, monkeypatch):
    monkeypatch.setenv("INTAKE_MAX_FILE_MB", "1")
    srv = make_server()
    srv.put("outgoing/huge.csv", b"x" * (1024 * 1024 + 10))
    h = Harness(monkeypatch, pull_config(srv), outcome="turned_away")
    monkeypatch.setattr(sftp_pull, "download",
                        lambda *a, **k: pytest.fail("an oversize file must not be read"))
    out = h.run()
    (rec,) = h.landed.values()
    assert rec.oversize and rec.data == b"" and rec.real_size == 1024 * 1024 + 10
    assert out["turned_away"] == 1 and srv.listing("outgoing/processed") == ["huge.csv"]


def test_collect_refuses_a_changed_host_key_before_logging_in(make_server, monkeypatch):
    srv = make_server()
    srv.put("outgoing/a.xlsx")
    cfg = pull_config(srv)
    srv.auth_log.clear()
    cfg["host_key"] = sftp_pull.host_key_info(_ed25519_paramiko()[0])   # someone else's key
    h = Harness(monkeypatch, cfg)
    out = h.run()
    assert out["error_code"] == "host_key" and "identity key has changed" in out["error"]
    assert srv.auth_log == []                                    # the password was never sent
    assert h.landed == {} and srv.listing("outgoing") == ["a.xlsx", "processed"]
    cfg.pop("host_key")
    out = h.run()
    assert out["error_code"] == "host_key" and "No server identity key" in out["error"]


def test_collect_on_rsa_server_with_pinned_key(make_server, monkeypatch):
    srv = make_server(host_key="rsa")
    srv.put("outgoing/a.xlsx")
    cfg = pull_config(srv)
    assert cfg["host_key"]["type"] == "ssh-rsa"
    out = Harness(monkeypatch, cfg).run()
    assert out["accepted"] == 1 and out.get("error") is None, out


def test_collect_wrong_password_and_switched_off(make_server, monkeypatch):
    srv = make_server()
    srv.put("outgoing/a.xlsx")
    cfg = pull_config(srv)
    srv.password = "rotated"
    h = Harness(monkeypatch, cfg)
    out = h.run()
    assert out["error_code"] == "auth" and h.status[-1]["last_error"] == out["error"]
    h.route.is_enabled = False
    srv.auth_log.clear()
    out = h.run()
    assert "switched off" in out["error"] and srv.auth_log == []


def test_collect_caps_files_per_pass(make_server, monkeypatch):
    monkeypatch.setenv("SFTP_PULL_MAX_FILES", "2")
    srv = make_server()
    for i in range(3):
        srv.put(f"outgoing/f{i}.csv", age=300 - i)
    h = Harness(monkeypatch, pull_config(srv))
    out = h.run()
    assert out["accepted"] == 2 and out["more_waiting"] == 1
    assert h.names() == ["f0.csv", "f1.csv"]
    assert Harness.run(h)["accepted"] == 1


def test_collect_empty_folder_does_not_wait(make_server, monkeypatch):
    srv = make_server()
    os.makedirs(srv.path("outgoing"))
    h = Harness(monkeypatch, pull_config(srv))
    t0 = time.monotonic()
    out = h.run()
    assert out["accepted"] == 0 and out.get("error") is None
    assert time.monotonic() - t0 < 1.0                           # no quiet wait for nothing


# ── what the real test servers taught (OpenSSH 8.9, SFTPGo 2.7.6, stubs) ────

def test_go_server_one_login_attempt_per_connection(make_server):
    # SFTPGo takes this password only as keyboard-interactive and hangs up on
    # a second attempt; paramiko's own fallback would fail against it.
    srv = make_server(methods="publickey,keyboard-interactive", one_attempt=True)
    srv.put("outgoing/a.xlsx")
    r = sftp_pull.test_connection(settings(srv))
    assert r["ok"], r
    assert srv.dropped == 0                                   # never a 2nd attempt on one connection
    assert [m for m, _ in srv.auth_log] == ["password", "keyboard-interactive"]


def test_one_time_code_prompt_is_refused_in_plain_words(make_server):
    srv = make_server(methods="keyboard-interactive", otp=True)
    os.makedirs(srv.path("outgoing"))
    r = sftp_pull.test_connection(settings(srv))
    assert r["error_code"] == "auth" and "one-time code" in r["error"], r


def test_key_type_is_pinned_so_a_new_host_key_is_no_false_alarm(make_server, monkeypatch):
    rsa_key, ed_key = _host_key("rsa"), _ed25519_paramiko()[0]
    srv = make_server(host_keys=[rsa_key])
    srv.put("outgoing/a.xlsx")
    cfg = pull_config(srv)
    assert cfg["host_key"]["type"] == "ssh-rsa"
    srv.host_keys = [ed_key, rsa_key]       # the server adds an Ed25519 key later
    out = Harness(monkeypatch, cfg).run()
    assert out.get("error") is None and out["accepted"] == 1, out
    srv.put("outgoing/b.xlsx")
    srv.host_keys = [ed_key]                # ... and drops the RSA one
    out = Harness(monkeypatch, cfg).run()
    assert out["error_code"] == "host_key" and "no longer offers" in out["error"], out


def test_silent_port_is_a_timeout(monkeypatch):
    monkeypatch.setattr(sftp_pull, "BANNER_TIMEOUT", 1)
    tarpit = _fake_server("silent")
    t0 = time.monotonic()
    r = sftp_pull.test_connection({"host": "127.0.0.1", "port": tarpit.getsockname()[1],
                                   "username": "u", "password": "p"})
    assert r["error_code"] == "timeout" and "no SFTP greeting" in r["error"], r
    assert time.monotonic() - t0 < 3
    tarpit.close()


def test_no_dir_says_where_the_login_starts(make_server):
    srv = make_server(quirks={"home": "/home/bob"})
    os.makedirs(srv.path("home/bob/outgoing"))
    r = sftp_pull.test_connection(settings(srv, remote_dir="/outgoing"))
    assert r["error_code"] == "no_dir", r
    assert "starts in “/home/bob”" in r["error"] and "try “outgoing”" in r["error"]


def test_a_non_utf8_name_costs_one_file_not_the_folder(make_server, monkeypatch):
    srv = make_server()
    srv.put("outgoing/a.xlsx")
    bad = os.path.join(os.fsencode(srv.path("outgoing")), b"caf\xe9_latin1.csv")
    with open(bad, "wb") as f:
        f.write(b"x")
    r = sftp_pull.test_connection(settings(srv))
    assert r["ok"] and r["files_found"] == 1, r
    assert any("not UTF-8" in w for w in r["warnings"])
    h = Harness(monkeypatch, pull_config(srv))
    out = h.run()
    assert h.names() == ["a.xlsx"] and "not UTF-8" in out["error"]
    assert os.path.exists(bad)                                 # left alone


def test_a_locked_file_is_not_ready_not_an_error(make_server, monkeypatch):
    srv = make_server(quirks={"lock": {"busy.xlsx"}})
    srv.put("outgoing/busy.xlsx")
    srv.put("outgoing/a.xlsx")
    h = Harness(monkeypatch, pull_config(srv))
    out = h.run()
    assert h.names() == ["a.xlsx"] and out.get("error") is None, out
    assert out["not_ready"] == ["busy.xlsx"] and out["skipped_still_writing"] == 1


def test_refused_renames_never_attempt_a_copy(make_server, monkeypatch):
    srv = make_server(quirks={"rename": "denied"})
    srv.put("outgoing/a.xlsx")
    os.makedirs(srv.path("outgoing/processed"))
    h = Harness(monkeypatch, pull_config(srv))
    out = h.run()
    assert out["accepted"] == 1 and "permission denied" in out["error"], out
    assert srv.listing("outgoing/processed") == []             # no half-copy left behind


def test_download_checks_the_size_it_was_told():
    class _F(io.BytesIO):
        def prefetch(self, *a, **k):
            pass

    class _S:
        def __init__(self, data):
            self.data = data

        def open(self, path, mode):
            return _F(self.data)

    assert sftp_pull.download(_S(b"abcde"), "x", 5, 100) == (b"abcde", False)
    with pytest.raises(sftp_pull.NotReady):
        sftp_pull.download(_S(b"abc"), "x", 5, 100)            # short: an interrupted upload
    data, over = sftp_pull.download(_S(b"x" * 50), "x", 50, 10)
    assert over and len(data) == 11                            # never more than cap + 1


def test_land_file_refuses_an_oversize_file_on_its_declared_size(monkeypatch):
    import intake_safety
    import intake_service
    import submission_service
    monkeypatch.setattr(submission_service, "on_land", lambda *a, **k: None)
    added = []
    session = types.SimpleNamespace(add=added.append, flush=lambda: None)
    route = types.SimpleNamespace(id=7, tenant_id=1, broker_party_id=3, is_enabled=True,
                                  channel="sftp", program_id=None, address="sftp://x@h:22/")
    mb = 1024 * 1024
    a = intake_service.land_file(session, tenant_id=1, filename="big.xlsx", file_bytes=b"",
                                 declared_size=350 * mb, route=route, max_bytes=200 * mb)
    assert a.outcome == "turned_away" and a.file_size_bytes == 350 * mb
    assert a.turned_away_reason.startswith("The file is 350 MB. We can accept files up to 200 MB")
    assert intake_safety.check_size(b"abc", 10) is None        # existing callers unchanged
    assert "empty" in intake_safety.check_size(b"", 10)
