"""Application logging and the optional structured mail_log syslog record."""

import configparser
import logging
import logging.handlers
import string
import syslog
import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

log = logging.getLogger("mail_filter")

_MAIL_LOG_FIELDS = {
    "qid", "message_id", "client_ip", "envelope_from", "envelope_to",
    "header_from", "header_from_raw", "header_to", "header_to_raw",
    "header_cc", "header_cc_raw", "reply_to", "reply_to_raw", "x_original_to",
    "delivered_to", "subject_raw", "subject_decoded", "decode_error",
    "body_size", "body_size_bytes", "body_truncated", "entity_count",
    "result", "reason", "warn", "warn_rules", "rule_line", "matched",
}
_MAIL_LOG_PRIORITIES = {
    "emerg": syslog.LOG_EMERG,
    "alert": syslog.LOG_ALERT,
    "crit": syslog.LOG_CRIT,
    "err": syslog.LOG_ERR,
    "error": syslog.LOG_ERR,
    "warning": syslog.LOG_WARNING,
    "warn": syslog.LOG_WARNING,
    "notice": syslog.LOG_NOTICE,
    "info": syslog.LOG_INFO,
    "debug": syslog.LOG_DEBUG,
}
_mail_log_enabled = False
_mail_log_format = "{qid}:\\theader Subject: '{subject_decoded}'\\teFrom: '{envelope_from}'\\thFrom: '{header_from}'\\tbody_size={body_size_bytes}"
_mail_log_priority = syslog.LOG_WARNING


def _mail_log_format_fields(fmt: str) -> List[str]:
    fields = []
    for _literal, field_name, format_spec, conversion in string.Formatter().parse(fmt):
        if field_name is None:
            continue
        if field_name not in _MAIL_LOG_FIELDS:
            raise ValueError(f"unknown field {{{field_name}}}")
        if format_spec:
            raise ValueError(f"format specifiers are not supported for {{{field_name}}}")
        if conversion:
            raise ValueError(f"conversions are not supported for {{{field_name}}}")
        fields.append(field_name)
    return fields


def _expand_mail_log_escapes(fmt: str) -> str:
    return fmt.replace(r"\t", "\t").replace(r"\r", "\r").replace(r"\n", "\n")


def _mail_log_safe(value: Any) -> str:
    if value is None or value == "":
        return "-"
    return str(value).replace("\\", "\\\\").replace("\r", r"\r").replace("\n", r"\n").replace("\t", r"\t")


def setup_mail_log(cfg: configparser.ConfigParser) -> None:
    global _mail_log_enabled, _mail_log_format, _mail_log_priority
    _mail_log_enabled = cfg.getboolean("mail_log", "enabled", fallback=False)
    _mail_log_format = cfg.get(
        "mail_log", "format",
        fallback="{qid}:\\theader Subject: '{subject_decoded}'\\teFrom: '{envelope_from}'\\thFrom: '{header_from}'\\tbody_size={body_size_bytes}",
    )
    priority_name = cfg.get("mail_log", "priority", fallback="warning").strip().lower()
    if priority_name not in _MAIL_LOG_PRIORITIES:
        raise ValueError(f"unknown mail_log priority: {priority_name!r}")
    _mail_log_priority = _MAIL_LOG_PRIORITIES[priority_name]
    if not _mail_log_enabled:
        return
    fields = _mail_log_format_fields(_mail_log_format)
    if not fields:
        raise ValueError("enabled mail_log format contains no fields")
    tag = cfg.get("mail_log", "tag", fallback="postfix/mail_filter").strip() or "postfix/mail_filter"
    syslog.openlog(ident=tag, logoption=syslog.LOG_PID, facility=syslog.LOG_MAIL)
    log.info("Mail log output enabled facility=mail priority=%s tag=%s", priority_name, tag)


def _write_mail_log(values: Dict[str, Any]) -> None:
    if not _mail_log_enabled:
        return
    safe = {name: _mail_log_safe(values.get(name, "-")) for name in _MAIL_LOG_FIELDS}
    line = _expand_mail_log_escapes(_mail_log_format).format_map(safe)
    # A syslog record must remain one physical line. Tabs introduced by the
    # configured format are preserved; CR/LF from the format are escaped.
    line = line.replace("\r", r"\r").replace("\n", r"\n")
    syslog.syslog(_mail_log_priority, line)


# "bsd" and "iso8601" are named presets kept for backward compatibility.
# Anything else is used verbatim as a Python time.strftime() format string:
# https://docs.python.org/3/library/time.html#time.strftime
_LOG_TIMESTAMP_PRESETS = {
    "bsd": "%b %d %H:%M:%S",
}


class _ISO8601Formatter(logging.Formatter):
    def formatTime(self, record, datefmt=None):
        return datetime.fromtimestamp(record.created).astimezone().isoformat(timespec="microseconds")


def _resolve_log_timestamp(timestamp_format: str) -> Tuple[bool, Optional[str]]:
    """Returns (is_iso8601, datefmt) for a [milter] log_timestamp value.
    "bsd"/"iso8601" are recognized case-insensitively as presets; any other
    value is a literal Python time.strftime() format string, used exactly as given
    (directives are case-sensitive, e.g. %Y vs %y)."""
    key = timestamp_format.strip().lower()
    if key == "iso8601":
        return True, None
    if key in _LOG_TIMESTAMP_PRESETS:
        return False, _LOG_TIMESTAMP_PRESETS[key]
    return False, timestamp_format.strip()


def validate_log_timestamp(timestamp_format: str) -> None:
    """Raises ValueError if timestamp_format is not usable -- either because
    it looks like an intended preset but is spelled wrong, or because
    time.strftime() itself rejects it (it also silently passes through
    unrecognized %-directives on most platforms, so this cannot catch every
    typo; run `mail_filter --test` or check the log to eyeball the result).

    %f (sub-second digits) is rejected explicitly rather than left to fail
    silently: it works on datetime.strftime() but NOT on time.strftime(),
    which is what logging.Formatter.formatTime() actually calls here -- it
    would otherwise render as a literal "f" with no error at all. Use the
    "iso8601" preset for microsecond precision instead."""
    is_iso8601, datefmt = _resolve_log_timestamp(timestamp_format)
    if is_iso8601:
        return
    if "%f" in datefmt:
        raise ValueError(
            f"invalid log_timestamp {timestamp_format!r}: %f (sub-second digits) is not "
            "supported here -- use the \"iso8601\" preset instead, which always includes "
            "microseconds"
        )
    try:
        time.strftime(datefmt)
    except (ValueError, TypeError) as exc:
        raise ValueError(f"invalid log_timestamp {timestamp_format!r}: {exc}") from exc


def setup_logging(logfile: str, level: str, timestamp_format: str = "bsd") -> None:
    validate_log_timestamp(timestamp_format)
    is_iso8601, datefmt = _resolve_log_timestamp(timestamp_format)
    handler = logging.handlers.WatchedFileHandler(logfile, encoding="utf-8")
    if is_iso8601:
        formatter = _ISO8601Formatter(
            "%(asctime)s mail_filter[%(process)d] %(levelname)s %(message)s"
        )
    else:
        formatter = logging.Formatter(
            "%(asctime)s mail_filter[%(process)d] %(levelname)s %(message)s",
            datefmt=datefmt,
        )
    handler.setFormatter(formatter)
    log.addHandler(handler)
    log.setLevel(getattr(logging, level.upper(), logging.INFO))
    _log_always("Logging initialized level=%s file=%s", level.upper(), logfile)


def _log_always(msg: str, *args) -> None:
    record = log.makeRecord(log.name, logging.INFO, "", 0, msg, args, None)
    for handler in log.handlers:
        handler.emit(record)
