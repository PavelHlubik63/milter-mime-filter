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
max_body_bytes: int = _DEFAULT_MAX_BODY_KB * 1024
log_match_max_chars: int = _DEFAULT_LOG_MATCH_MAX_CHARS
add_warn_header: bool = False


def resolve_config_path(config_path: str, value: str) -> str:
    """Resolve a path from a config file. Relative paths are relative to that config file."""
    if os.path.isabs(value):
        return os.path.normpath(value)
    return os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(config_path)), value))
