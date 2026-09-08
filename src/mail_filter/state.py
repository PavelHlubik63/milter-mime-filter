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


def readable_by_user(path, username):
    """Could ``username`` open ``path`` for reading?

    Answered from stat() data rather than by changing identity, so it is
    safe to call from a root process -- and it gives the honest answer for
    somebody else, which is the point. Root can read a root:wheel 0640
    file; the postfix user cannot, and a --configtest run as root that only
    asks "can I read this?" reports success on a configuration that will
    take the daemon down at start.

    Every directory along the way must be searchable, then the file itself
    readable. Returns True, False, or None when the question cannot be
    answered -- there is no such user on this system, or something on the
    path is missing.
    """
    try:
        pw = pwd.getpwnam(username)
    except KeyError:
        return None

    uid = pw.pw_uid
    gids = {pw.pw_gid}
    for group in grp.getgrall():
        if username in group.gr_mem:
            gids.add(group.gr_gid)

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

    last = len(components) - 1
    for index, component in enumerate(components):
        try:
            st = os.stat(component)
        except OSError:
            return None
        if index == last:
            ok = _granted(st, uid, gids,
                          stat.S_IRUSR, stat.S_IRGRP, stat.S_IROTH)
        else:
            ok = _granted(st, uid, gids,
                          stat.S_IXUSR, stat.S_IXGRP, stat.S_IXOTH)
        if not ok:
            return False

    return True


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
