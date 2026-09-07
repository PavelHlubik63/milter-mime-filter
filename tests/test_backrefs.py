"""$N/${N}/$$ backreferences in reject/warn messages -- pulling regex
capture groups into the action text, like Postfix's pcre_table(5)."""

import pytest

from mail_filter.rules_engine import MailContext, apply_rules
from mail_filter.rules_lang import RuleError, parse_rules_src


def test_single_group_substituted_into_reject_message():
    rules = parse_rules_src('if subject /invoice-(\\d+)/ { reject "case $1 blocked"; }')
    ctx = MailContext(subject="Re: invoice-42 due")
    result, _warns, _path = apply_rules(rules, ctx)
    assert result == ("REJECT", "case 42 blocked")


def test_second_group_like_the_postfix_attachment_example():
    rules = parse_rules_src(
        r'if subject /name\s*=\s*"?([^;]*(invoice)[^;]*\.(zip|rar))"?/i '
        '{ reject "$2 attachment"; }'
    )
    ctx = MailContext(subject='name="my_invoice_file.zip"')
    result, _warns, _path = apply_rules(rules, ctx)
    assert result == ("REJECT", "invoice attachment")


def test_dollar_zero_is_the_whole_match():
    rules = parse_rules_src('if subject /invoice-\\d+/ { reject "$0 blocked"; }')
    ctx = MailContext(subject="Re: invoice-42 due")
    result, _warns, _path = apply_rules(rules, ctx)
    assert result == ("REJECT", "invoice-42 blocked")


def test_double_dollar_is_a_literal_dollar():
    rules = parse_rules_src('if subject /invoice-(\\d+)/ { reject "fee $$5 for $1"; }')
    ctx = MailContext(subject="invoice-42")
    result, _warns, _path = apply_rules(rules, ctx)
    assert result == ("REJECT", "fee $5 for 42")


def test_braced_form_disambiguates_from_following_digit():
    rules = parse_rules_src('if subject /invoice-(\\d+)/ { reject "id${1}9"; }')
    ctx = MailContext(subject="invoice-42")
    result, _warns, _path = apply_rules(rules, ctx)
    assert result == ("REJECT", "id429")


def test_literal_dollar_not_followed_by_digit_is_untouched():
    rules = parse_rules_src('if subject /invoice-(\\d+)/ { reject "cost: $ $1"; }')
    ctx = MailContext(subject="invoice-42")
    result, _warns, _path = apply_rules(rules, ctx)
    assert result == ("REJECT", "cost: $ 42")


def test_warn_action_also_supports_backrefs():
    rules = parse_rules_src('if subject /invoice-(\\d+)/ { warn "case $1 flagged"; }')
    ctx = MailContext(subject="invoice-42")
    _result, warns, _path = apply_rules(rules, ctx)
    assert warns == [("case 42 flagged", warns[0][1])]


def test_anyof_uses_the_branch_that_actually_matched():
    rules = parse_rules_src(
        'if anyof(subject /aaa-(\\d+)/, subject /bbb-(\\d+)/) { reject "id $1"; }'
    )
    ctx = MailContext(subject="bbb-99")
    result, _warns, _path = apply_rules(rules, ctx)
    assert result == ("REJECT", "id 99")


def test_embedded_newline_in_matched_group_is_stripped():
    rules = parse_rules_src('if body /name="([^"]*)"/s { reject "file: $1"; }')
    ctx = MailContext(subject="s", body='name="evil\nname.zip"')
    result, _warns, _path = apply_rules(rules, ctx)
    assert result[0] == "REJECT"
    assert "\n" not in result[1]
    assert result[1] == "file: evil name.zip"


def test_allof_is_rejected_at_parse_time():
    with pytest.raises(RuleError, match="allof"):
        parse_rules_src(
            'if allof(subject /a-(\\d+)/, body /b-(\\d+)/) { reject "$1"; }'
        )


def test_not_is_rejected_at_parse_time():
    with pytest.raises(RuleError, match="not"):
        parse_rules_src('if not(subject /a-(\\d+)/) { reject "$1"; }')


def test_numeric_condition_is_rejected_at_parse_time():
    with pytest.raises(RuleError, match="numeric"):
        parse_rules_src('if header "X-Score" > 5 { reject "$1"; }')


def test_insufficient_capture_groups_is_rejected_at_parse_time():
    with pytest.raises(RuleError, match="capture group"):
        parse_rules_src('if subject /invoice-(\\d+)/ { reject "$2"; }')


def test_refile_entry_with_too_few_groups_is_rejected_at_parse_time(tmp_path):
    refile = tmp_path / "patterns.inc"
    refile.write_text("/one-(\\d+)-(\\d+)/\n/two-(\\d+)/\n", encoding="utf-8")
    rules_path = tmp_path / "rules.conf"
    rules_path.write_text(
        f'if subject refile:{refile.name} {{ reject "$2"; }}\n', encoding="utf-8"
    )
    with pytest.raises(RuleError, match="capture group"):
        parse_rules_src(rules_path.read_text(encoding="utf-8"), str(rules_path))


def test_nonparticipating_optional_group_substitutes_empty_string():
    rules = parse_rules_src('if subject /(a)|(b)/ { reject "[$1][$2]"; }')
    ctx = MailContext(subject="b")
    result, _warns, _path = apply_rules(rules, ctx)
    assert result == ("REJECT", "[][b]")
