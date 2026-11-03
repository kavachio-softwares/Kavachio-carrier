"""Tests for sftp_server — Kavachio's own SFTP server, where brokers upload.

No database and no mail. The server runs in this process on a free port over
temporary folders; sign-in is a stub standing in for sftp_accounts.verify, and
each "broker" is a login on a channel folder of its own. Every test talks to it
with a real SSH client (paramiko's), the way FileZilla or a script would.

What is proved, in the order a broker meets it:
  * a login starts in /incoming, sees only /incoming and /outbound, and can
    upload, rename and delete its own uploads;
  * it can read Kavachio's replies in /outbound, and write nothing there;
  * it cannot read an upload back, make folders or links, run commands,
    forward ports, or name any path outside its own channel folder — another
    broker's included;
  * wrong passwords: three per connection, then the USER NAME is locked —
    never anybody else's, even from the same address;
  * limits count per login, never per address (a proxy shares one address);
  * old ciphers and MACs are refused at the handshake;
  * size and quota limits; idle and "switched off" sessions are closed;
  * the collector is told while an upload is still open (sftp_watch).

Run:  .venv/bin/python -m pytest -q test_sftp_server.py
"""
import os

# Never a real database: nothing here needs one, and an import must not find one.
os.environ.setdefault("DATABASE_URL", "postgresql+psycopg2://nobody@127.0.0.1:1/none")

import io
import stat
import time
import types

import paramiko
import pytest

import sftp_server
import sftp_watch

PASSWORDS = {"broker-a": "Pass-A-12345", "broker-b": "Pass-B-67890"}


@pytest.fixture(scope="module")
def host_keys(tmp_path_factory):
    d = tmp_path_factory.mktemp("keys")
    os.environ["SFTP_SERVER_HOST_KEY_DIR"] = str(d)
    try:
        yield sftp_server.load_host_keys()
    finally:
        os.environ.pop("SFTP_SERVER_HOST_KEY_DIR", None)


def _make_server(tmp_path, host_keys, **kw):
    bases = {user: tmp_path / user for user in PASSWORDS}
    for base in bases.values():
        (base / "incoming").mkdir(parents=True)
        # Folders a login must never see.
        for hidden in ("processed", "rejected", "held", "quarantine"):
            (base / hidden).mkdir()
            (base / hidden / f"{hidden}.xlsx").write_bytes(b"not for the broker")

    def authenticate(username, password, ip):
        if PASSWORDS.get(username) != password:
            return None
        return types.SimpleNamespace(
            username=username, route_id=list(PASSWORDS).index(username) + 1,
            folder=bases[username])

    kw.setdefault("fail_delay", 0)
    srv = sftp_server.SftpServer("127.0.0.1", 0, authenticate=authenticate,
                                 host_keys=host_keys, **kw)
    srv.start()
    assert srv.ready.wait(10) and srv.state == "listening", srv.error
    srv.incoming = {u: b / "incoming" for u, b in bases.items()}
    srv.outbound = {u: b / "outbound" for u, b in bases.items()}
    srv.bases = bases
    return srv


@pytest.fixture
def server(tmp_path, host_keys):
    srv = _make_server(tmp_path, host_keys)
    yield srv
    srv.stop()


def _connect(srv, user="broker-a", password=None, **transport_kw):
    t = paramiko.Transport(("127.0.0.1", srv.port), **transport_kw)
    t.connect(username=user, password=password or PASSWORDS[user])
    return t, paramiko.SFTPClient.from_transport(t)


def _put(sftp, name, data=b"PK\x03\x04 a bordereau"):
    with sftp.open(name, "wb") as f:
        f.write(data)


# ── a login and its two folders ─────────────────────────────────────────────

def test_a_login_starts_in_incoming_and_sees_two_folders(server):
    t, sftp = _connect(server)
    try:
        assert sftp.normalize(".") == "/incoming"
        assert sorted(sftp.listdir("/")) == ["incoming", "outbound"]
        assert sftp.listdir("..") == sftp.listdir("/")
        # A bare name lands in /incoming, like an absolute one.
        _put(sftp, "PRG-ABC123_2026-09.xlsx", b"x" * 5000)
        _put(sftp, "/incoming/second.xlsx", b"y")
        assert sorted(sftp.listdir()) == ["PRG-ABC123_2026-09.xlsx", "second.xlsx"]
        assert sftp.stat("PRG-ABC123_2026-09.xlsx").st_size == 5000
        assert stat.S_ISDIR(sftp.stat("/outbound").st_mode)
        # The server's own account is not shown.
        assert sftp.stat("/").st_uid == 0
        # The channel's other folders cannot be named at all.
        for hidden in ("processed", "rejected", "held", "quarantine"):
            with pytest.raises(IOError):
                sftp.listdir(f"/{hidden}")
            with pytest.raises(IOError):
                sftp.stat(f"/{hidden}/{hidden}.xlsx")
        # Nothing can be put at the top, beside the two folders.
        with pytest.raises(IOError):
            _put(sftp, "/top.xlsx")
    finally:
        t.close()
    landed = server.incoming["broker-a"] / "PRG-ABC123_2026-09.xlsx"
    assert landed.read_bytes() == b"x" * 5000
    assert stat.S_IMODE(landed.stat().st_mode) & 0o007 == 0      # not world-readable
    assert not (server.bases["broker-a"] / "top.xlsx").exists()


def test_an_upload_cannot_be_read_back(server):
    (server.incoming["broker-a"] / "earlier.xlsx").write_bytes(b"secret rows")
    t, sftp = _connect(server)
    try:
        with pytest.raises(IOError):
            sftp.open("earlier.xlsx", "rb")
        with pytest.raises(IOError):
            sftp.getfo("earlier.xlsx", io.BytesIO())
        # Opened read-write for an upload: writing works, reading does not.
        with sftp.open("rw.xlsx", "w+") as f:
            f.write(b"abc")
            f.seek(0)
            with pytest.raises(IOError):
                f.read(3)
    finally:
        t.close()


def test_replies_in_outbound_can_be_read_never_written(server):
    out = server.outbound["broker-a"]
    out.mkdir(exist_ok=True)
    (out / "SUB-1.status.json").write_text('{"status": "accepted"}')
    (out / ".SUB-2.status.json.tmp").write_text("still being written")
    t, sftp = _connect(server)
    try:
        assert sftp.listdir("/outbound") == ["SUB-1.status.json"]
        buf = io.BytesIO()
        sftp.getfo("/outbound/SUB-1.status.json", buf)
        assert buf.getvalue() == b'{"status": "accepted"}'
        with pytest.raises(IOError):
            sftp.stat("/outbound/.SUB-2.status.json.tmp")
        for mode in ("wb", "ab", "r+"):
            with pytest.raises(IOError):
                sftp.open("/outbound/SUB-1.status.json", mode)
        with pytest.raises(IOError):
            _put(sftp, "/outbound/forged.json")
        _put(sftp, "move-me.xlsx")
        with pytest.raises(IOError):
            sftp.rename("move-me.xlsx", "/outbound/move-me.xlsx")
        # Read, then cleared away — what a broker's pick-up job does.
        sftp.remove("/outbound/SUB-1.status.json")
        assert sftp.listdir("/outbound") == []
    finally:
        t.close()
    assert not (out / "forged.json").exists()
    assert (out / ".SUB-2.status.json.tmp").exists()


def test_another_broker_cannot_see_or_name_my_files(server):
    server.outbound["broker-a"].mkdir(exist_ok=True)
    (server.outbound["broker-a"] / "a-reply.json").write_text("{}")
    ta, a = _connect(server, "broker-a")
    tb, b = _connect(server, "broker-b")
    try:
        _put(a, "mine.xlsx")
        assert b.listdir("/incoming") == [] and b.listdir("/outbound") == []
        with pytest.raises(IOError):
            b.stat("mine.xlsx")
        # Every way of spelling a path out of the folder.
        mine = str(server.incoming["broker-a"] / "mine.xlsx")
        for path in ("../broker-a/incoming/mine.xlsx", "/../../broker-a/incoming/mine.xlsx",
                     "../../broker-a/outbound/a-reply.json", mine,
                     "..\\broker-a\\incoming\\mine.xlsx", "/incoming/../../broker-a/incoming/mine.xlsx"):
            with pytest.raises(IOError):
                b.stat(path)
            with pytest.raises(IOError):
                b.remove(path)
            with pytest.raises(IOError):
                b.rename(path, "stolen.xlsx")
            with pytest.raises(IOError):
                b.getfo(path, io.BytesIO())
            with pytest.raises(IOError):
                _put(b, path, b"overwrite")
        # ".." at the top is the top: broker B's own two folders.
        assert b.normalize("../../..") == "/"
        assert sorted(b.listdir("../..")) == ["incoming", "outbound"]
        with pytest.raises(IOError):
            b.listdir("/broker-a")
        with pytest.raises(IOError):
            _put(b, "../escape.xlsx/x")
    finally:
        ta.close()
        tb.close()
    assert sorted(p.name for p in server.incoming["broker-a"].iterdir()) == ["mine.xlsx"]
    assert (server.incoming["broker-a"] / "mine.xlsx").read_bytes().startswith(b"PK")
    assert list(server.incoming["broker-b"].iterdir()) == []
    assert not (server.bases["broker-b"] / "escape.xlsx").exists()


def test_no_folders_links_or_permission_changes(server):
    (server.incoming["broker-a"] / "real.xlsx").write_bytes(b"1")
    t, sftp = _connect(server)
    try:
        for make in ("sub", "/new", "/incoming/sub"):
            with pytest.raises(IOError):
                sftp.mkdir(make)
        for gone in ("/", "/incoming", "/outbound"):
            with pytest.raises(IOError):
                sftp.rmdir(gone)
        with pytest.raises(IOError):
            sftp.symlink("/etc/passwd", "pw.xlsx")
        with pytest.raises(IOError):
            sftp.readlink("real.xlsx")
        # Times and modes are accepted and ignored, never applied.
        before = os.stat(server.incoming["broker-a"] / "real.xlsx")
        sftp.utime("real.xlsx", (1, 1))
        sftp.chmod("real.xlsx", 0o777)
        after = os.stat(server.incoming["broker-a"] / "real.xlsx")
        assert (after.st_mtime, after.st_mode) == (before.st_mtime, before.st_mode)
    finally:
        t.close()
    assert not (server.incoming["broker-a"] / "pw.xlsx").exists()


def test_a_link_already_in_a_folder_is_never_followed(server, tmp_path):
    outside = tmp_path / "outside.txt"
    outside.write_text("not yours")
    server.outbound["broker-a"].mkdir(exist_ok=True)
    os.symlink(outside, server.incoming["broker-a"] / "link.xlsx")
    os.symlink(outside, server.outbound["broker-a"] / "link.json")
    t, sftp = _connect(server)
    try:
        assert "link.xlsx" not in sftp.listdir("/incoming")
        assert "link.json" not in sftp.listdir("/outbound")
        with pytest.raises(IOError):
            _put(sftp, "link.xlsx", b"overwrite")
        with pytest.raises(IOError):
            sftp.getfo("/outbound/link.json", io.BytesIO())
        with pytest.raises(IOError):
            sftp.remove("link.xlsx")
    finally:
        t.close()
    assert outside.read_text() == "not yours"


def test_rename_and_take_back_before_collection(server):
    t, sftp = _connect(server)
    try:
        _put(sftp, "bdx.xlsx.filepart", b"done")
        sftp.rename("bdx.xlsx.filepart", "bdx.xlsx")
        _put(sftp, "other.xlsx")
        with pytest.raises(IOError):           # plain rename never replaces
            sftp.rename("other.xlsx", "bdx.xlsx")
        sftp.posix_rename("other.xlsx", "bdx.xlsx")
        with pytest.raises(IOError):
            sftp.rename("bdx.xlsx", "/bdx.xlsx")
        assert sftp.listdir() == ["bdx.xlsx"]
        sftp.remove("bdx.xlsx")
        assert sftp.listdir() == []
    finally:
        t.close()


# ── sign-in ─────────────────────────────────────────────────────────────────

def test_keyboard_interactive_works_like_the_password(server):
    t = paramiko.Transport(("127.0.0.1", server.port))
    t.start_client()
    t.auth_interactive("broker-b", lambda title, instr, prompts: [PASSWORDS["broker-b"]])
    assert t.is_authenticated()
    t.close()


def test_three_wrong_passwords_end_the_connection(server):
    t = paramiko.Transport(("127.0.0.1", server.port))
    t.start_client()
    for _ in range(3):
        with pytest.raises(paramiko.AuthenticationException):
            t.auth_password("broker-a", "wrong")
    time.sleep(0.6)
    assert not t.is_active()


def test_unknown_user_is_refused_like_a_wrong_password(server):
    for user in ("nobody", "broker-a"):
        t = paramiko.Transport(("127.0.0.1", server.port))
        t.start_client()
        with pytest.raises(paramiko.AuthenticationException):
            t.auth_password(user, "wrong")
        t.close()


def test_a_user_name_that_keeps_failing_is_locked(tmp_path, host_keys):
    srv = _make_server(tmp_path, host_keys,
                       throttle=sftp_server._Throttle(limit=4, window=60, block=1.5))
    try:
        for _ in range(2):
            t = paramiko.Transport(("127.0.0.1", srv.port))
            t.start_client()
            for _ in range(2):
                with pytest.raises(paramiko.AuthenticationException):
                    t.auth_password("broker-a", "wrong")
            t.close()
        # Locked now — even with the right password, on a new connection.
        with pytest.raises((paramiko.SSHException, EOFError, OSError)):
            _connect(srv)
        time.sleep(1.6)
        t, sftp = _connect(srv)
        assert sftp.listdir() == []
        t.close()
    finally:
        srv.stop()


# ── who is who: logins, never addresses (9 Oct 2026) ───────────────────────
# Behind a proxy or load balancer every broker arrives from ONE address. Here
# every test connection is from 127.0.0.1 — exactly that situation.

def test_one_brokers_wrong_passwords_never_lock_out_another(tmp_path, host_keys):
    srv = _make_server(tmp_path, host_keys,
                       throttle=sftp_server._Throttle(limit=3, window=60, block=60))
    try:
        t = paramiko.Transport(("127.0.0.1", srv.port))
        t.start_client()
        for _ in range(3):
            try:
                t.auth_password("broker-a", "wrong")
            except (paramiko.AuthenticationException, paramiko.SSHException, EOFError):
                pass
        t.close()
        with pytest.raises((paramiko.SSHException, EOFError, OSError)):
            _connect(srv, "broker-a")                  # locked: that login only
        tb, b = _connect(srv, "broker-b")              # same address, still in
        assert b.listdir() == []
        tb.close()
    finally:
        srv.stop()


def test_many_brokers_from_one_address_are_not_capped_at_eight(server):
    opened = []
    try:
        for user in ["broker-a"] * 6 + ["broker-b"] * 6:
            t, sftp = _connect(server, user)
            assert sftp.listdir() == []
            opened.append(t)
        assert all(t.is_active() for t in opened)      # 12 from 127.0.0.1
    finally:
        for t in opened:
            t.close()


def test_one_login_is_limited_but_not_the_others(tmp_path, host_keys):
    srv = _make_server(tmp_path, host_keys, max_per_login=2)
    opened = []
    try:
        for _ in range(2):
            opened.append(_connect(srv, "broker-a")[0])
        with pytest.raises((paramiko.SSHException, EOFError, OSError)):
            t3, s3 = _connect(srv, "broker-a")
            opened.append(t3)
            s3.listdir()
        tb, b = _connect(srv, "broker-b")
        opened.append(tb)
        assert b.listdir() == []
        # One of broker-a's sessions ends: the next one is let in at once.
        opened[0].close()
        time.sleep(0.3)
        t4, s4 = _connect(srv, "broker-a")
        opened.append(t4)
        assert s4.listdir() == []
    finally:
        for t in opened:
            t.close()
        srv.stop()


def test_connections_still_signing_in_are_capped_for_the_server(tmp_path, host_keys):
    srv = _make_server(tmp_path, host_keys, max_pending=1)
    try:
        lurker = paramiko.Transport(("127.0.0.1", srv.port))
        lurker.start_client()                          # connected, never signs in
        with pytest.raises((paramiko.SSHException, EOFError, OSError)):
            _connect(srv, "broker-b")
        lurker.close()
        time.sleep(0.3)
        tb, b = _connect(srv, "broker-b")              # room again at once
        assert b.listdir() == []
        tb.close()
    finally:
        srv.stop()


def test_closed_connections_stop_counting_at_once(tmp_path, host_keys):
    # A client that connects, uploads and disconnects in a quick loop is never
    # refused for connections it has already closed.
    srv = _make_server(tmp_path, host_keys, max_connections=3)
    try:
        for i in range(10):
            t, sftp = _connect(srv, "broker-a")
            _put(sftp, f"file-{i}.csv", b"a,b\n1,2\n")
            t.close()
            time.sleep(0.05)
        assert len(os.listdir(srv.incoming["broker-a"])) == 10
    finally:
        srv.stop()


def test_no_shell_commands_or_forwarding(server):
    t, _ = _connect(server)
    try:
        ch = t.open_session()
        with pytest.raises(paramiko.SSHException):
            ch.exec_command("cat /etc/passwd")
        ch = t.open_session()
        with pytest.raises(paramiko.SSHException):
            ch.invoke_shell()
        with pytest.raises(paramiko.ChannelException):
            t.open_channel("direct-tcpip", ("127.0.0.1", 22), ("127.0.0.1", 0))
        with pytest.raises(paramiko.SSHException):
            t.request_port_forward("127.0.0.1", 0)
    finally:
        t.close()


@pytest.mark.parametrize("disabled", [
    # A client that can only speak a CBC cipher, then one that only has SHA-1 MACs.
    {"ciphers": ["aes128-ctr", "aes192-ctr", "aes256-ctr",
                 "aes128-gcm@openssh.com", "aes256-gcm@openssh.com"]},
    {"macs": ["hmac-sha2-256", "hmac-sha2-512", "hmac-sha2-256-etm@openssh.com",
              "hmac-sha2-512-etm@openssh.com"],
     # GCM carries its own MAC, so it is taken away too or it would be chosen.
     "ciphers": ["aes128-gcm@openssh.com", "aes256-gcm@openssh.com"]},
])
def test_old_algorithms_are_refused(server, disabled):
    with pytest.raises(paramiko.SSHException):
        _connect(server, disabled_algorithms=disabled)


def test_host_keys_are_kept_and_private(host_keys):
    d = os.environ["SFTP_SERVER_HOST_KEY_DIR"]
    again = sftp_server.load_host_keys()
    assert [k.asbytes() for k in again] == [k.asbytes() for k in host_keys]
    assert stat.S_IMODE(os.stat(d).st_mode) == 0o700
    for name in os.listdir(d):
        assert stat.S_IMODE(os.stat(os.path.join(d, name)).st_mode) == 0o600
    fps = [sftp_server.key_fingerprint(k) for k in host_keys]
    assert all(fp.startswith("SHA256:") for fp in fps) and len(set(fps)) == 2
    assert [k.get_name() for k in host_keys] == ["ssh-ed25519", "ssh-rsa"]


# ── limits, and sessions ending ─────────────────────────────────────────────

def test_a_file_over_the_limit_is_refused_and_removed(tmp_path, host_keys):
    srv = _make_server(tmp_path, host_keys, max_file_bytes=1000)
    try:
        t, sftp = _connect(srv)
        with pytest.raises(IOError):
            _put(sftp, "huge.xlsx", b"x" * 5000)
        _put(sftp, "fine.xlsx", b"x" * 900)
        t.close()
        time.sleep(0.3)
        assert sorted(p.name for p in srv.incoming["broker-a"].iterdir()) == ["fine.xlsx"]
    finally:
        srv.stop()


def test_a_full_folder_takes_no_new_files(tmp_path, host_keys, monkeypatch):
    monkeypatch.setattr(sftp_server, "_MAX_PENDING_FILES", 2)
    srv = _make_server(tmp_path, host_keys)
    try:
        t, sftp = _connect(srv)
        _put(sftp, "1.xlsx")
        _put(sftp, "2.xlsx")
        with pytest.raises(IOError):
            _put(sftp, "3.xlsx")
        _put(sftp, "2.xlsx", b"an overwrite is not a new file")
        t.close()
    finally:
        srv.stop()


def test_collector_is_told_while_an_upload_is_open(server):
    path = server.incoming["broker-a"] / "slow.xlsx"
    t, sftp = _connect(server)
    try:
        f = sftp.open("slow.xlsx", "wb")
        f.write(b"first half")
        f.flush()
        time.sleep(0.2)
        assert sftp_watch.being_written(path)
        f.close()
        time.sleep(0.2)
        assert not sftp_watch.being_written(path)
    finally:
        t.close()


def test_switching_a_channel_off_closes_its_sessions(server):
    ta, a = _connect(server, "broker-a")
    tb, b = _connect(server, "broker-b")
    try:
        assert server.drop_route(1) == 1
        time.sleep(0.3)
        assert not ta.is_active()
        assert tb.is_active() and b.listdir() == []
    finally:
        ta.close()
        tb.close()


def test_an_idle_session_is_closed(tmp_path, host_keys):
    srv = _make_server(tmp_path, host_keys, idle_seconds=1)
    try:
        t, _ = _connect(srv)
        deadline = time.time() + 6
        while t.is_active() and time.time() < deadline:
            time.sleep(0.2)
        assert not t.is_active()
    finally:
        srv.stop()


def test_a_connection_that_never_signs_in_is_dropped(tmp_path, host_keys, monkeypatch):
    monkeypatch.setattr(sftp_server, "_AUTH_SECONDS", 1)
    srv = _make_server(tmp_path, host_keys)
    try:
        t = paramiko.Transport(("127.0.0.1", srv.port))
        t.start_client()
        deadline = time.time() + 6
        while t.is_active() and time.time() < deadline:
            time.sleep(0.2)
        assert not t.is_active()
        assert srv.session_count() == 0
    finally:
        srv.stop()


# ── an upload the client never finished (9 Oct 2026) ───────────────────────

def _wait_gone(path, seconds=5):
    deadline = time.time() + seconds
    while path.exists() and time.time() < deadline:
        time.sleep(0.1)
    return not path.exists()


def _wait_not_written(path, seconds=5):
    # The part is deleted first, THEN the collector is told the upload has
    # ended (so it can never see it as finished in between): a moment apart.
    deadline = time.time() + seconds
    while sftp_watch.being_written(path) and time.time() < deadline:
        time.sleep(0.05)
    return not sftp_watch.being_written(path)


def test_a_dropped_connection_leaves_no_half_file(server):
    # A CSV cut off mid-upload still reads — with rows missing. It must never
    # be left for the collector to take as the whole bordereau.
    path = server.incoming["broker-a"] / "PRG-ABC123_2026-09.csv"
    t, sftp = _connect(server)
    f = sftp.open("PRG-ABC123_2026-09.csv", "wb")
    f.write(b"policy,premium\n" + b"P1,100\n" * 500)
    f.flush()
    time.sleep(0.2)
    assert path.exists() and sftp_watch.being_written(path)
    t.close()                                   # the line drops; no close()
    assert _wait_gone(path)
    assert _wait_not_written(path)


def test_a_cut_off_session_leaves_no_half_file(server):
    # A new password or the channel switched off, mid-upload.
    path = server.incoming["broker-a"] / "half.xlsx"
    t, sftp = _connect(server)
    try:
        f = sftp.open("half.xlsx", "wb")
        f.write(b"PK\x03\x04 first half")
        f.flush()
        time.sleep(0.2)
        assert server.drop_route(1) == 1
        assert _wait_gone(path)
        assert _wait_not_written(path)
    finally:
        t.close()


def test_an_idle_upload_is_closed_and_removed(tmp_path, host_keys):
    srv = _make_server(tmp_path, host_keys, idle_seconds=1)
    path = srv.incoming["broker-a"] / "stalled.csv"
    try:
        t, sftp = _connect(srv)
        f = sftp.open("stalled.csv", "wb")
        f.write(b"policy,premium\nP1,100\n")
        f.flush()
        assert _wait_gone(path, 8)
        assert _wait_not_written(path)
        t.close()
    finally:
        srv.stop()


def test_a_cut_off_upload_renamed_while_open_is_still_removed(server):
    old = server.incoming["broker-a"] / "part.tmp"
    new = server.incoming["broker-a"] / "renamed.csv"
    t, sftp = _connect(server)
    f = sftp.open("part.tmp", "wb")
    f.write(b"policy,premium\nP1,100\n")
    f.flush()
    sftp.posix_rename("part.tmp", "renamed.csv")
    assert new.exists() and sftp_watch.being_written(new)
    t.close()
    assert _wait_gone(new) and not old.exists()
    assert _wait_not_written(new)


def test_a_finished_upload_is_kept_when_the_connection_then_drops(server):
    path = server.incoming["broker-a"] / "whole.csv"
    t, sftp = _connect(server)
    _put(sftp, "whole.csv", b"policy,premium\nP1,100\n")
    t.close()
    time.sleep(0.5)
    assert path.read_bytes() == b"policy,premium\nP1,100\n"
    assert _wait_not_written(path)
