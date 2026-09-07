from mail_filter.rules_engine import MailContext, _format_matches_for_log, apply_rules
from mail_filter.rules_lang import parse_rules_src


def test_simple_subject_reject_rule():
    rules = parse_rules_src('if subject /hacked/i { reject "Spam"; }')
    ctx = MailContext(subject="You have been HACKED")
    result, warns, path = apply_rules(rules, ctx)
    assert result == ("REJECT", "Spam")
    assert warns == []


def test_simple_subject_reject_rule_no_match_accepts():
    rules = parse_rules_src('if subject /hacked/i { reject "Spam"; }')
    ctx = MailContext(subject="A perfectly normal subject")
    result, warns, path = apply_rules(rules, ctx)
    assert result is None


def test_refile_include(tmp_path):
    refile = tmp_path / "patterns.inc"
    refile.write_text("/hacked/i\n/phishing/i\n", encoding="utf-8")

    rules_path = tmp_path / "rules.conf"
    rules_path.write_text(
        f'if subject refile:{refile.name} {{ reject "Spam"; }}\n', encoding="utf-8"
    )

    rules = parse_rules_src(rules_path.read_text(encoding="utf-8"), str(rules_path))

    ctx_match = MailContext(subject="This looks like PHISHING")
    result, _warns, _path = apply_rules(rules, ctx_match)
    assert result == ("REJECT", "Spam")

    ctx_no_match = MailContext(subject="Unrelated subject")
    result, _warns, _path = apply_rules(rules, ctx_no_match)
    assert result is None


def test_matched_log_shows_matched_text_not_the_pattern():
    rules = parse_rules_src('if subject /hacked/i { reject "Spam"; }')
    ctx = MailContext(subject="You have been HACKED")
    _result, _warns, path = apply_rules(rules, ctx)
    matched = _format_matches_for_log(path)
    assert matched.endswith(":HACKED")
    assert "/hacked/i" not in matched


def test_matched_log_shows_only_the_emoji_that_matched_a_character_class():
    rules = parse_rules_src(r'if subject /[\U0001F300-\U0001FAFF]/ { reject "Emoji"; }')
    ctx = MailContext(subject="Upozorneni \U0001F514 nove produkty")
    _result, _warns, path = apply_rules(rules, ctx)
    matched = _format_matches_for_log(path)
    assert matched.endswith(":\U0001F514")


def test_matched_log_truncates_long_matched_text_with_ellipsis():
    rules = parse_rules_src('if body /x+/ { reject "Long match"; }')
    ctx = MailContext(subject="s", body="x" * 200)
    _result, _warns, path = apply_rules(rules, ctx)
    matched = _format_matches_for_log(path, max_chars=10)
    assert matched.endswith(":" + "x" * 10 + " ...")


def test_matched_log_max_chars_zero_means_unlimited():
    rules = parse_rules_src('if body /x+/ { reject "Long match"; }')
    ctx = MailContext(subject="s", body="x" * 200)
    _result, _warns, path = apply_rules(rules, ctx)
    matched = _format_matches_for_log(path, max_chars=0)
    assert matched.endswith(":" + "x" * 200)


def test_matched_log_escapes_embedded_newline_in_matched_text():
    rules = parse_rules_src('if body /line1.*line2/s { reject "Multiline"; }')
    ctx = MailContext(subject="s", body="line1\nline2")
    _result, _warns, path = apply_rules(rules, ctx)
    matched = _format_matches_for_log(path)
    assert "\n" not in matched
    assert r"line1\nline2" in matched
