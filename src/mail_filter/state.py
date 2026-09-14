"""Shared mutable runtime state and configuration-path defaults.

Kept in one place -- instead of living as module-level globals inside
whichever file happens to run first -- so the milter daemon (milter_core.py)
and the SIGUSR1 reload handler can both reach the current rules/whitelist/
config without importing each other.

Other modules must access the mutable attributes below through the module
itself (``from . import state`` then ``state.rules``), not via
``from .state import rules``: a reload replaces the list/int objects these
names point to, and a "from" import would keep pointing at the old object.
"""

import configparser
import grp
import os
import pwd
import stat
import sys

_DEFAULT_MAX_BODY_KB = 512
_DEFAULT_LOG_MATCH_MAX_CHARS = 80

_CONFIG_FILE = (
    "/usr/local/etc/mail_filter/mail_filter.conf"
    if sys.platform.startswith("freebsd")
    else "/etc/mail_filter/mail_filter.conf"
)
_RULES_FALLBACK = (
    "/usr/local/etc/mail_filter/mail_filter_rules.conf"
    if sys.platform.startswith("freebsd")
    else "/etc/mail_filter/mail_filter_rules.conf"
)
_WHITELIST_FALLBACK = (
    "/usr/local/etc/mail_filter/mail_filter_whitelist.cidr"
    if sys.platform.startswith("freebsd")
    else "/etc/mail_filter/mail_filter_whitelist.cidr"
)

# The user both packages run the daemon as: the User= in the systemd unit
# and mail_filter_runas in the rc.d script. Kept here so --configtest can
# ask "can *that* user read this?" on either platform, where no rc.d script
# is around to answer it.
DAEMON_USER = "postfix"

rules: list = []
whitelist: list = []
cfg = configparser.ConfigParser(interpolation=None)


class ConfigError(Exception):
    """The configuration file exists but could not be used."""


def read_config(parser, path):
    """Read the configuration file, and say so when that fails.

    configparser.read() takes a list of candidate files and quietly skips
    any it cannot open, returning only the ones it managed to parse. That is
    sensible for "read whichever of these exist" and dangerous for "read my
    configuration": a file the daemon has no permission to open leaves every
    setting at its built-in default with nothing logged and nothing raised.

    That is not hypothetical. The daemon runs as the postfix user; a
    mail_filter.conf left as root:wheel mode 640 is readable by root -- so
    --configtest passes -- and unreadable by the daemon, which then runs
    with a default socket, default limits, no [mail_log] and loglevel INFO
    while the file on disk says WARNING. It appears to work, because the
    defaults are reasonable.

    Returns True when the file was read, False when it does not exist (the
    caller decides whether running on defaults is acceptable), and raises
    ConfigError when it exists but could not be used.
    """
    if not os.path.exists(path):
        return False

    try:
        parsed = parser.read(path)
    except configparser.Error as exc:
        raise ConfigError("%s: %s" % (path, exc))

    if not parsed:
        raise ConfigError(
            "%s exists but could not be read -- check its ownership and mode. "
            "The daemon runs as the user named in the rc.d script or the "
            "systemd unit, not as root, and needs read access to this file."
            % path
        )

    return True


def _granted(st, uid, gids, user_bit, group_bit, other_bit):
    """Which of the three permission triads applies, and does it grant?

    POSIX picks exactly one triad -- owner, else group, else other -- and
    does not fall through to a more permissive one. A file owned by the
    user with mode 0044 is therefore *not* readable by that user.
    """
    if st.st_uid == uid:
        return bool(st.st_mode & user_bit)
    if st.st_gid in gids:
        return bool(st.st_mode & group_bit)
    return bool(st.st_mode & other_bit)


class Readability:
    """The answer to "can this user read that file?", and how it was reached.

    verdict   True, False, or None when the question could not be asked at
              all (no such user here, or something on the path is missing)
    blocker   when verdict is False: the file itself, or the directory on the
              way to it that the user cannot search; otherwise None
    exact     True when the kernel answered, by opening the file as that
              user; False when it was worked out from owner, group and mode,
              which knows nothing about ACLs or MAC policies
    by_mode   when verdict is False: True when owner, group and mode alone
              deny access too; False when they would allow it, so an ACL or
              a MAC policy is what says no and changing the mode will not help
    """
    __slots__ = ("verdict", "blocker", "exact", "by_mode")

    def __init__(self, verdict, blocker=None, exact=True, by_mode=None):
        self.verdict = verdict
        self.blocker = blocker
        self.exact = exact
        self.by_mode = by_mode


# Exit codes of the probe child. Denial on the n-th path component is
# reported as _PROBE_DENIED_BASE + n, so the parent can name it; an exit
# status has eight bits, which caps how deep a path can be told apart.
_PROBE_READABLE = 0
_PROBE_MISSING = 1
_PROBE_SETUP_FAILED = 2
_PROBE_DENIED_BASE = 10
_PROBE_MAX_COMPONENTS = 255 - _PROBE_DENIED_BASE


def _path_components(path):
    """"/a/b/c" -> ["/", "/a", "/a/b", "/a/b/c"]."""
    path = os.path.abspath(path)
    components = []
    head = path
    while True:
        components.append(head)
        parent = os.path.dirname(head)
        if parent == head:
            break
        head = parent
    components.reverse()
    return components


def _probe(components):
    """Walk to the file and open it, as whoever this process is now.

    Returns a _PROBE_* code. Each component is stat()ed in turn: that fails
    with EACCES exactly when the directory above it may not be searched, so
    the denial can be pinned to that directory. Then the file is opened,
    which is the question itself, answered by the kernel with everything it
    knows -- mode, ACLs, MAC policy.
    """
    for index in range(1, len(components)):
        try:
            os.stat(components[index])
        except PermissionError:
            return _PROBE_DENIED_BASE + index - 1
        except OSError:
            return _PROBE_MISSING
    try:
        fd = os.open(components[-1], os.O_RDONLY | getattr(os, "O_NONBLOCK", 0))
    except PermissionError:
        return _PROBE_DENIED_BASE + len(components) - 1
    except OSError:
        return _PROBE_MISSING
    os.close(fd)
    return _PROBE_READABLE


def _probe_as(components, pw):
    """Run _probe() in a child process that has become ``pw``.

    Only root can change identity; the parent keeps its own. The child
    leaves with os._exit(), so nothing buffered in the parent (the
    --configtest report) is written twice.
    """
    pid = os.fork()
    if pid == 0:
        code = _PROBE_SETUP_FAILED
        try:
            os.initgroups(pw.pw_name, pw.pw_gid)
            os.setgid(pw.pw_gid)
            os.setuid(pw.pw_uid)
            code = _probe(components)
        except BaseException:
            pass
        os._exit(code)
    _pid, status = os.waitpid(pid, 0)
    if os.WIFEXITED(status):
        return os.WEXITSTATUS(status)
    return _PROBE_SETUP_FAILED


def _user_gids(pw):
    gids = {pw.pw_gid}
    for group in grp.getgrall():
        if pw.pw_name in group.gr_mem:
            gids.add(group.gr_gid)
    return gids


def _mode_grants(path, pw, is_file, gids=None):
    """Do owner, group and mode alone let ``pw`` read this file (is_file)
    or search this directory? Stat errors count as no."""
    try:
        st = os.stat(path)
    except OSError:
        return False
    if gids is None:
        gids = _user_gids(pw)
    if is_file:
        return _granted(st, pw.pw_uid, gids,
                        stat.S_IRUSR, stat.S_IRGRP, stat.S_IROTH)
    return _granted(st, pw.pw_uid, gids,
                    stat.S_IXUSR, stat.S_IXGRP, stat.S_IXOTH)


def _readable_from_stat(components, pw):
    """The fallback: work the answer out from owner, group and mode.

    Used when the caller can neither become the user nor already is it. It
    is right for plain permissions and blind to ACLs and MAC policies, in
    both directions: it can call a file unreadable that an ACL opens up, and
    readable when an ACL denies it.
    """
    gids = _user_gids(pw)
    last = len(components) - 1
    for index, component in enumerate(components):
        if not os.path.exists(component):
            return Readability(None, exact=False)
        if not _mode_grants(component, pw, index == last, gids):
            return Readability(False, component, exact=False, by_mode=True)

    return Readability(True, exact=False)


def daemon_user_exists():
    try:
        pwd.getpwnam(DAEMON_USER)
    except KeyError:
        return False
    return True


def can_ask_as_user(username):
    """Will readable_by_user() get the kernel's answer for this user?

    It will when this process is root, or already runs as that user --
    "service mail_filter configtest" on FreeBSD does.
    """
    try:
        pw = pwd.getpwnam(username)
    except KeyError:
        return False
    return os.geteuid() in (0, pw.pw_uid)


def readable_by_user(path, username):
    """Could ``username`` open ``path`` for reading?

    Asked of the kernel, as that user: root runs the check in a child
    process that has changed identity, and a process that already is that
    user simply tries. Root can read a root:wheel 0640 file; the postfix
    user cannot, and a --configtest run as root that only asks "can I read
    this?" reports success on a configuration that will take the daemon
    down at start.

    Earlier versions worked the answer out from stat() instead, which also
    sees owner, group and mode -- and nothing else. A directory opened to
    postfix by an NFSv4 or POSIX ACL was reported unreadable, and a file
    closed to it by one was reported readable. Anyone else still gets that
    approximation, marked as such (Readability.exact is False).

    Returns a Readability.
    """
    try:
        pw = pwd.getpwnam(username)
    except KeyError:
        return Readability(None)

    components = _path_components(path)
    euid = os.geteuid()

    if len(components) > _PROBE_MAX_COMPONENTS:
        return _readable_from_stat(components, pw)
    if euid == pw.pw_uid:
        code = _probe(components)
    elif euid == 0:
        code = _probe_as(components, pw)
    else:
        return _readable_from_stat(components, pw)

    if code == _PROBE_READABLE:
        return Readability(True)
    if code == _PROBE_MISSING:
        return Readability(None)
    if _PROBE_DENIED_BASE <= code < _PROBE_DENIED_BASE + len(components):
        index = code - _PROBE_DENIED_BASE
        blocker = components[index]
        return Readability(False, blocker,
                           by_mode=not _mode_grants(blocker, pw,
                                                    index == len(components) - 1))
    # the child could not become the user; fall back rather than guess
    return _readable_from_stat(components, pw)


def describe_owner(path):
    """"root:postfix 0644", for an error message that says what to change."""
    try:
        st = os.stat(path)
    except OSError as exc:
        return str(exc)
    try:
        owner = pwd.getpwuid(st.st_uid).pw_name
    except KeyError:
        owner = str(st.st_uid)
    try:
        group = grp.getgrgid(st.st_gid).gr_name
    except KeyError:
        group = str(st.st_gid)
    return "%s:%s %04o" % (owner, group, stat.S_IMODE(st.st_mode))


max_body_bytes: int = _DEFAULT_MAX_BODY_KB * 1024
log_match_max_chars: int = _DEFAULT_LOG_MATCH_MAX_CHARS
add_warn_header: bool = False


def resolve_config_path(config_path: str, value: str) -> str:
    """Resolve a path from a config file. Relative paths are relative to that config file."""
    if os.path.isabs(value):
        return os.path.normpath(value)
    return os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(config_path)), value))
