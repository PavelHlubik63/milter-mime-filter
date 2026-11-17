import pytest

from mail_filter.logging_setup import validate_log_timestamp


def test_bsd_and_iso8601_presets_are_valid():
    validate_log_timestamp("bsd")
    validate_log_timestamp("BSD")
    validate_log_timestamp("iso8601")
    validate_log_timestamp("ISO8601")


def test_custom_strftime_format_is_valid():
    validate_log_timestamp("%Y-%m-%d %H:%M:%S")
    validate_log_timestamp("%s")


def test_percent_f_is_rejected_with_a_helpful_message():
    with pytest.raises(ValueError, match="iso8601"):
        validate_log_timestamp("%Y-%m-%d %H:%M:%S.%f")


# --- [mail_log] default template -------------------------------------------

import configparser
import re
from pathlib import Path

from mail_filter import logging_setup
from mail_filter.logging_setup import (
    _DEFAULT_MAIL_LOG_FORMAT, _MAIL_LOG_FIELDS, _mail_log_format_fields,
)

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_default_template_ends_with_client_name():
    assert _DEFAULT_MAIL_LOG_FORMAT.endswith(r"\tclient_name={client_name}")
    assert set(_mail_log_format_fields(_DEFAULT_MAIL_LOG_FORMAT)) <= _MAIL_LOG_FIELDS


def test_default_template_is_the_one_the_man_page_documents():
    man = (REPO_ROOT / "man" / "man5" / "mail_filter.conf.5").read_text(encoding="utf-8")
    m = re.search(r"The default template is:\n\.PP\n\.nf\n\.RS\nformat = (.*)\n", man)
    assert m, "default template paragraph not found in mail_filter.conf(5)"
    # roff writes a literal backslash as \
    assert m.group(1).replace("\\\\", "\\") == _DEFAULT_MAIL_LOG_FORMAT


def test_sample_config_format_ends_with_client_name():
    cfg = configparser.ConfigParser(interpolation=None)
    cfg.read(REPO_ROOT / "conf" / "mail_filter.conf", encoding="utf-8")
    fmt = cfg.get("mail_log", "format")
    assert fmt.endswith(r"\tclient_name={client_name}")
    assert set(_mail_log_format_fields(fmt)) <= _MAIL_LOG_FIELDS


def test_record_without_a_format_line_carries_client_name_last(monkeypatch):
    written = []
    monkeypatch.setattr(logging_setup.syslog, "openlog", lambda **kw: None)
    monkeypatch.setattr(logging_setup.syslog, "syslog", lambda prio, line: written.append(line))
    cfg = configparser.ConfigParser(interpolation=None)
    cfg.read_string("[mail_log]\nenabled = yes\n")
    try:
        logging_setup.setup_mail_log(cfg)
        logging_setup._write_mail_log({
            "qid": "ABC123", "subject_decoded": "Hello", "envelope_from": "<a@b.example>",
            "header_from": "a@b.example", "body_size_bytes": 10, "entity_count": 0,
            "client_name": "1.2.3.4.bc.googleusercontent.com",
        })
    finally:
        monkeypatch.setattr(logging_setup, "_mail_log_enabled", False)
        monkeypatch.setattr(logging_setup, "_mail_log_format", _DEFAULT_MAIL_LOG_FORMAT)
    assert written == ["ABC123:\theader Subject: 'Hello'\teFrom: '<a@b.example>'\thFrom: "
                       "'a@b.example'\tbody_size=10\tentity_count=0\t"
                       "client_name=1.2.3.4.bc.googleusercontent.com"]
