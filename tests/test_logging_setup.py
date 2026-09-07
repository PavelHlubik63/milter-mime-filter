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
