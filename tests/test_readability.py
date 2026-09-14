"""state.readable_by_user(): can the daemon user read a file?

The answer comes from the kernel whenever the process is root (a child
changes identity and opens the file) or already the user in question, and
from owner/group/mode otherwise. The root-only tests use "nobody", which
exists on both FreeBSD and Debian; the ACL tests also need setfacl and a
file system with ACLs enabled, and skip themselves when either is missing.
"""

import os
import pwd
import shutil
import subprocess
import sys
import tempfile

import pytest

from mail_filter import state

needs_root = pytest.mark.skipif(os.geteuid() != 0, reason="needs root")
needs_non_root = pytest.mark.skipif(os.geteuid() == 0, reason="root reads everything")


@pytest.fixture
def open_dir():
    """A directory "nobody" can reach: /tmp itself is 1777, pytest's
    tmp_path lives under a 0700 directory and would block every test."""
    path = tempfile.mkdtemp(prefix="mf-readable-", dir="/tmp")
    os.chmod(path, 0o755)
    yield path
    shutil.rmtree(path, ignore_errors=True)


def make_file(directory, mode, name="file.conf"):
    path = os.path.join(directory, name)
    with open(path, "w") as fh:
        fh.write("x\n")
    os.chmod(path, mode)
    return path


def current_user():
    return pwd.getpwuid(os.geteuid()).pw_name


def test_unknown_user_cannot_be_asked(open_dir):
    path = make_file(open_dir, 0o644)
    result = state.readable_by_user(path, "no-such-user-mf-test")
    assert result.verdict is None
    assert not state.can_ask_as_user("no-such-user-mf-test")


def test_fallback_is_marked_inexact(open_dir, monkeypatch):
    path = make_file(open_dir, 0o644)
    user = current_user()
    uid = pwd.getpwnam(user).pw_uid
    monkeypatch.setattr(os, "geteuid", lambda: uid + 100000)

    assert not state.can_ask_as_user(user)
    result = state.readable_by_user(path, user)
    assert result.verdict is True
    assert result.exact is False


def test_fallback_names_the_file(open_dir, monkeypatch):
    path = make_file(open_dir, 0o000)
    user = current_user()
    uid = pwd.getpwnam(user).pw_uid
    # owned by the user, mode 000: the owner triad applies and denies
    monkeypatch.setattr(os, "geteuid", lambda: uid + 100000)
    monkeypatch.setattr(os, "getuid", lambda: uid + 100000)

    result = state.readable_by_user(path, user)
    assert result.verdict is False
    assert result.blocker == path
    assert result.exact is False


@needs_non_root
def test_same_user_is_answered_by_the_kernel(open_dir):
    readable = make_file(open_dir, 0o644, "readable.conf")
    unreadable = make_file(open_dir, 0o000, "unreadable.conf")
    user = current_user()

    assert state.can_ask_as_user(user)
    ok = state.readable_by_user(readable, user)
    assert ok.verdict is True and ok.exact is True

    denied = state.readable_by_user(unreadable, user)
    assert denied.verdict is False and denied.exact is True
    assert denied.blocker == unreadable


@needs_non_root
def test_same_user_names_the_blocking_directory(open_dir):
    inner = os.path.join(open_dir, "closed")
    os.mkdir(inner)
    path = make_file(inner, 0o644)
    os.chmod(inner, 0o000)
    try:
        result = state.readable_by_user(path, current_user())
    finally:
        os.chmod(inner, 0o755)
    assert result.verdict is False
    assert result.blocker == inner


@needs_root
def test_root_asks_as_the_user(open_dir):
    readable = make_file(open_dir, 0o644, "readable.conf")
    unreadable = make_file(open_dir, 0o600, "unreadable.conf")

    ok = state.readable_by_user(readable, "nobody")
    assert ok.verdict is True and ok.exact is True

    # root could read it; nobody cannot -- and the mode is why
    denied = state.readable_by_user(unreadable, "nobody")
    assert denied.verdict is False and denied.exact is True
    assert denied.blocker == unreadable
    assert denied.by_mode is True


@needs_root
def test_root_names_the_blocking_directory(open_dir):
    inner = os.path.join(open_dir, "private")
    os.mkdir(inner, 0o700)
    path = make_file(inner, 0o644)

    result = state.readable_by_user(path, "nobody")
    assert result.verdict is False
    assert result.blocker == inner


@needs_root
def test_root_probe_does_not_duplicate_output(open_dir, capfd):
    path = make_file(open_dir, 0o644)
    sys.stdout.write("before\n")
    state.readable_by_user(path, "nobody")
    sys.stdout.flush()
    assert capfd.readouterr().out.count("before") == 1


def _set_read_acl(path, user, allow):
    """Allow or deny ``user`` read access through an ACL; False when
    setfacl or ACL support is missing."""
    setfacl = shutil.which("setfacl")
    if setfacl is None:
        return False
    if sys.platform.startswith("freebsd"):
        # NFSv4 ACL; a deny entry must come first to take effect
        if allow:
            args = ["-m", "u:%s:r::allow" % user]
        else:
            args = ["-a", "0", "u:%s:r::deny" % user]
    else:
        # POSIX ACL; a named-user entry overrides "other"
        args = ["-m", "u:%s:%s" % (user, "r" if allow else "-")]
    done = subprocess.run([setfacl, *args, path],
                          capture_output=True, text=True)
    return done.returncode == 0


@needs_root
def test_acl_that_grants_read_is_seen(open_dir):
    path = make_file(open_dir, 0o600)
    if not _set_read_acl(path, "nobody", allow=True):
        pytest.skip("setfacl or ACL support not available here")

    result = state.readable_by_user(path, "nobody")
    assert result.verdict is True
    assert result.exact is True

    # the owner/group/mode approximation would have got this wrong
    pw = pwd.getpwnam("nobody")
    approx = state._readable_from_stat(state._path_components(path), pw)
    assert approx.verdict is False


@needs_root
def test_acl_that_denies_read_is_seen_and_blamed(open_dir):
    path = make_file(open_dir, 0o644)
    if not _set_read_acl(path, "nobody", allow=False):
        pytest.skip("setfacl or ACL support not available here")

    result = state.readable_by_user(path, "nobody")
    assert result.verdict is False
    assert result.blocker == path
    # the mode says yes: an ACL is what says no
    assert result.by_mode is False


@needs_root
def test_acl_denial_is_blamed_behind_an_acl_opened_directory(open_dir):
    # The directory is reachable only through an ACL, the file closed by
    # one. Whether the mode is to blame must be decided at the file, not by
    # replaying the whole path from the mode bits, which stop at the
    # directory.
    inner = os.path.join(open_dir, "acl-only")
    os.mkdir(inner, 0o700)
    path = make_file(inner, 0o644)
    setfacl = shutil.which("setfacl")
    if setfacl is None:
        pytest.skip("setfacl not available here")
    if sys.platform.startswith("freebsd"):
        entry = "u:nobody:x::allow"
    else:
        entry = "u:nobody:x"
    if subprocess.run([setfacl, "-m", entry, inner], capture_output=True).returncode != 0 \
            or not _set_read_acl(path, "nobody", allow=False):
        pytest.skip("ACL support not available here")

    result = state.readable_by_user(path, "nobody")
    assert result.verdict is False
    assert result.blocker == path
    assert result.by_mode is False


def write(path, text, mode):
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.chmod(path, mode)
    return path


def test_refile_paths_lists_each_refile_once(open_dir):
    from mail_filter.rules_lang import parse_rules_src, refile_paths

    one = write(os.path.join(open_dir, "one.inc"), "/spam/i\n", 0o644)
    two = write(os.path.join(open_dir, "two.inc"), "/scam/i\n", 0o644)
    rules = os.path.join(open_dir, "rules.conf")
    src = (
        'if subject refile:one.inc { reject; }\n'
        'if anyof(subject refile:two.inc, body refile:one.inc) { warn "x"; }\n'
        'if subject /plain/i { reject; }\n'
    )
    parsed = parse_rules_src(src, rules)
    assert refile_paths(parsed) == [one, two]


@needs_root
def test_configtest_checks_refiles_too(open_dir, capsys, monkeypatch):
    # The daemon reads refile: lists as its own user; one it cannot open
    # fails the whole rules file, so --configtest must ask about it.
    from mail_filter import testing

    monkeypatch.setattr(state, "DAEMON_USER", "nobody")
    inc = write(os.path.join(open_dir, "list.inc"), "/spam/i\n", 0o600)
    write(os.path.join(open_dir, "rules.conf"),
          'if subject refile:list.inc { reject; }\n', 0o644)
    write(os.path.join(open_dir, "wl.cidr"), "127.0.0.1\n", 0o644)
    conf = write(os.path.join(open_dir, "mail_filter.conf"),
                 "[files]\nrules = rules.conf\nwhitelist = wl.cidr\n", 0o644)

    errors = testing.run_configtest(conf)
    out = capsys.readouterr().out
    assert errors >= 1
    assert "FAIL: %s is" % inc in out
    assert "not readable by nobody" in out
