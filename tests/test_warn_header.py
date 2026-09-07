"""_build_warn_header_value -- the X-Mail-Filter-Status header content,
independent of the actual Milter.addheader() call (which needs a live
milter session)."""

import pytest

pytest.importorskip("Milter")

from mail_filter.milter_core import _build_warn_header_value
from mail_filter.rules_lang import _strip_line_breaks


def test_single_warn_is_one_line():
    value = _build_warn_header_value([("Possible BEC -- bank account change notification", [])])
    assert value == 'WARN "Possible BEC -- bank account change notification"'


def test_multiple_warns_fold_one_per_line():
    value = _build_warn_header_value([
        ("Possible BEC -- bank account change notification", []),
        ("Emoji not allowed", []),
    ])
    assert value == (
        'WARN\n'
        '\t"Possible BEC -- bank account change notification"\n'
        '\t"Emoji not allowed"'
    )


def test_non_ascii_warn_text_is_rfc2047_encoded():
    value = _build_warn_header_value([("Možný podvod s bankovním účtem", [])])
    assert value.startswith('WARN "=?utf-8?')
    assert "Možný" not in value


def test_strip_line_breaks_strips_embedded_newlines_and_tabs():
    assert _strip_line_breaks("line1\nline2\r\nline3\ttabbed") == "line1 line2  line3 tabbed"
