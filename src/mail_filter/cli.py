"""Argument parsing and the main() entrypoint that ties everything together."""

import argparse
import configparser
import os
import signal
import sys
import threading
import time

import Milter

from . import __version__, state
from .logging_setup import _log_always, log, setup_logging, setup_mail_log
from .milter_core import MailFilter, _reload, _shutdown
from .rules_engine import load_rules, load_whitelist
from .testing import run_configtest, run_test, run_test_eml


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="mail_filter",
        description=f"Postfix milter for filtering email messages by Subject, body, envelope, and headers (version {__version__})",
    )
    parser.add_argument("--version", action="version",
                        version=f"%(prog)s {__version__}")
    parser.add_argument("--configtest", nargs="?", const=state._CONFIG_FILE, metavar="FILE",
                        help="Verify configuration and rules; optionally use an alternative config file")

    # --test / --test-eml switches, split into clearly separated groups so
    # it's obvious at a glance which options feed the Subject/header side of
    # a rule and which feed the body side -- both can be combined freely in
    # a single --test invocation (e.g. to exercise a rule that requires both
    # "subject ..." and "body ..." to match), so these are documentation
    # groups, not mutually exclusive argparse groups.
    tg_ctl = parser.add_argument_group("rule testing -- control (--test / --test-eml)")
    tg_ctl.add_argument("--test", action="store_true",
                        help="Check the message against the rules (0=ACCEPT, 1=REJECT)")
    tg_ctl.add_argument("--test-config", metavar="FILE",
                        dest="test_config",
                        help="Use an alternative mail_filter.conf for --test / --test-eml")
    tg_ctl.add_argument("--test-context", metavar="FILE",
                        help="JSON file containing the full message context (subject, body, "
                             "envelope, headers) for --test. Takes precedence over --subject/"
                             "--body/--envelope-*/--from/--to/--header if both are given.")
    tg_ctl.add_argument("--test-eml", metavar="EML_FILE", dest="test_eml",
                        help="Test an EML file (0=ACCEPT, 1=REJECT)")

    tg_subj = parser.add_argument_group(
        "rule testing -- Subject/envelope/header options",
        "Feed the \"subject\", \"envelope_from\", \"envelope_to\", \"from\", \"to\", "
        "\"cc\", \"reply_to\", and \"header\" tests. Combine freely with the body "
        "options below in the same --test invocation.",
    )
    tg_subj.add_argument("--subject",       metavar="SUBJECT",  help="Subject header (raw)")
    tg_subj.add_argument("--envelope-from", metavar="ADDR",     dest="envelope_from")
    tg_subj.add_argument("--envelope-to",   metavar="ADDR",     dest="envelope_to", action="append")
    tg_subj.add_argument("--from",          metavar="ADDR",     dest="from_header")
    tg_subj.add_argument("--to",            metavar="ADDR",     dest="to_header")
    tg_subj.add_argument("--header",        metavar="NAME:VAL", action="append",
                        help='Add a header, e.g., --header "X-Spam-Score: 8.5"')

    tg_body = parser.add_argument_group(
        "rule testing -- body options",
        "Feed the \"body\" test. --body and --body-file are mutually exclusive.",
    )
    tg_body_mx = tg_body.add_mutually_exclusive_group()
    tg_body_mx.add_argument("--body",      metavar="TEXT", help="Message body for --test")
    tg_body_mx.add_argument("--body-file", metavar="FILE", dest="body_file",
                        help="File containing the message body for --test")

    args = parser.parse_args()

    _test_switches_used = (
        args.subject or args.body or args.body_file or args.envelope_from
        or args.envelope_to or args.from_header or args.to_header or args.header
    )
    _any_test_mode = args.test or args.test_context or args.test_eml or _test_switches_used

    if args.configtest is not None and _any_test_mode:
        print(
            "WARN: --configtest was given together with --test/--test-eml/--test-context/"
            "--subject/--body/... -- --configtest runs alone and the test switches are "
            "ignored.",
            file=sys.stderr,
        )
    if args.configtest is not None:
        sys.exit(run_configtest(args.configtest))

    if args.test_eml and (args.test_context or _test_switches_used):
        print(
            "WARN: --test-eml was given together with --test-context/--subject/--body/"
            "--envelope-*/--from/--to/--header -- --test-eml builds its own message "
            "context from the EML file and the other test switches are ignored.",
            file=sys.stderr,
        )
    if args.test_eml:
        sys.exit(run_test_eml(args.test_eml, args.test_config))

    if args.test_context and _test_switches_used:
        print(
            "WARN: --test-context was given together with --subject/--body/--envelope-*/"
            "--from/--to/--header -- --test-context takes precedence and the other "
            "test switches are ignored.",
            file=sys.stderr,
        )

    if args.test or args.test_config or args.test_context or _test_switches_used:
        sys.exit(run_test(args))

    try:
        if not state.read_config(state.cfg, state._CONFIG_FILE):
            print(f"WARN: config file not found: {state._CONFIG_FILE} -- using defaults",
                  file=sys.stderr)
    except state.ConfigError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)

    logfile = state.resolve_config_path(
        state._CONFIG_FILE, state.cfg.get("files", "logfile", fallback="/var/log/mail_filter.log")
    )
    loglevel = state.cfg.get("milter", "loglevel", fallback="INFO")
    log_timestamp = state.cfg.get("milter", "log_timestamp", fallback="bsd")
    sockaddr = state.cfg.get("milter", "socket",
                        fallback="unix:/var/run/mail_filter/mail_filter.sock")
    timeout  = state.cfg.getint("milter", "timeout", fallback=600)
    max_body_kb = state.cfg.getint("milter", "max_body_kb", fallback=state._DEFAULT_MAX_BODY_KB)
    state.max_body_bytes = max_body_kb * 1024
    log_match_max_chars = state.cfg.getint(
        "milter", "log_match_max_chars", fallback=state._DEFAULT_LOG_MATCH_MAX_CHARS
    )
    if log_match_max_chars < 0:
        print(
            f"ERROR: [milter] log_match_max_chars must be >= 0 (0 disables truncation), "
            f"got {log_match_max_chars}",
            file=sys.stderr,
        )
        sys.exit(1)
    state.log_match_max_chars = log_match_max_chars
    state.add_warn_header = state.cfg.getboolean("milter", "add_warn_header", fallback=False)

    try:
        setup_logging(logfile, loglevel, log_timestamp)
    except ValueError as exc:
        print(f"ERROR: invalid [milter] logging configuration: {exc}", file=sys.stderr)
        sys.exit(1)
    try:
        setup_mail_log(state.cfg)
    except (ValueError, configparser.Error) as exc:
        log.error("Invalid [mail_log] configuration: %s -- aborting", exc)
        sys.exit(1)
    _log_always("mail_filter %s starting socket=%s max_body_kb=%d", __version__, sockaddr, max_body_kb)

    if sockaddr.startswith("unix:"):
        sock_path = sockaddr[len("unix:"):]
        sock_dir  = os.path.dirname(sock_path)
        if not os.path.isdir(sock_dir):
            log.error("Socket directory does not exist: %s -- aborting", sock_dir)
            sys.exit(1)
        if not os.access(sock_dir, os.W_OK):
            log.error("Socket directory not writable for uid=%d: %s -- aborting",
                      os.getuid(), sock_dir)
            sys.exit(1)

    rules_path = state.resolve_config_path(
        state._CONFIG_FILE, state.cfg.get("files", "rules", fallback=state._RULES_FALLBACK)
    )
    whitelist_path = state.resolve_config_path(
        state._CONFIG_FILE, state.cfg.get("files", "whitelist", fallback=state._WHITELIST_FALLBACK)
    )
    state.rules = load_rules(rules_path)
    state.whitelist = load_whitelist(whitelist_path)

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT,  _shutdown)
    signal.signal(signal.SIGUSR1, _reload)

    os.umask(0o007)

    Milter.factory = MailFilter
    Milter.set_flags(Milter.ADDHDRS if state.add_warn_header else 0)

    milter_thread = threading.Thread(
        target=Milter.runmilter,
        args=("mail_filter", sockaddr, timeout),
        daemon=True,
    )
    milter_thread.start()
    _log_always("mail_filter %s running, milter thread started", __version__)

    while milter_thread.is_alive():
        time.sleep(1)

    _log_always("mail_filter stopped")
