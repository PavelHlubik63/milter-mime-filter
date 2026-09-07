"""Tests the CIDR matching mechanism used to bypass all filtering for
trusted source addresses (load_whitelist + in_whitelist). The wiring that
actually skips rule evaluation for a whitelisted connection lives in
MailFilter.connect()/eom() in milter_core.py, which requires pymilter and
a live SMTP session to exercise end-to-end -- out of scope for a unit test."""

from mail_filter.rules_engine import in_whitelist, load_whitelist


def test_whitelist_matches_cidr_range(tmp_path):
    whitelist_path = tmp_path / "whitelist.cidr"
    whitelist_path.write_text("192.168.1.0/24\n::1\n", encoding="utf-8")

    nets = load_whitelist(str(whitelist_path))

    assert in_whitelist("192.168.1.42", nets) is True
    assert in_whitelist("::1", nets) is True
    assert in_whitelist("203.0.113.5", nets) is False


def test_whitelist_ignores_comments_and_blank_lines(tmp_path):
    whitelist_path = tmp_path / "whitelist.cidr"
    whitelist_path.write_text(
        "# trusted relay\n192.0.2.1\n\n# another comment\n", encoding="utf-8"
    )

    nets = load_whitelist(str(whitelist_path))

    assert in_whitelist("192.0.2.1", nets) is True
    assert in_whitelist("192.0.2.2", nets) is False


def test_missing_whitelist_file_matches_nothing(tmp_path):
    nets = load_whitelist(str(tmp_path / "does-not-exist.cidr"))
    assert nets == []
    assert in_whitelist("192.0.2.1", nets) is False
