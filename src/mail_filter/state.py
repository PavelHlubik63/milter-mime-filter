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
import os
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
max_body_bytes: int = _DEFAULT_MAX_BODY_KB * 1024
log_match_max_chars: int = _DEFAULT_LOG_MATCH_MAX_CHARS
add_warn_header: bool = False


def resolve_config_path(config_path: str, value: str) -> str:
    """Resolve a path from a config file. Relative paths are relative to that config file."""
    if os.path.isabs(value):
        return os.path.normpath(value)
    return os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(config_path)), value))
