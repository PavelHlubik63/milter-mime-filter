"""The client_name test: the SMTP client's hostname as Postfix reported it."""

import pytest

from mail_filter.rules_engine import MailContext, apply_rules
from mail_filter.rules_lang import RuleError, parse_rules_src

GOOGLE_VM = "166.255.116.136.bc.googleusercontent.com"

SUPPORT_FROM_GOOGLE_VM = r'''
if client_name /\.googleusercontent\.com$/i
   and (envelope_from /^<support@/i or from /(^|<)support@/i) {
    reject "Rejected: support@ sender from a Google Cloud VM";
}
'''


def _result(rules_src, **ctx):
    result, _warns, _path = apply_rules(parse_rules_src(rules_src), MailContext(**ctx))
    return result


def test_client_name_matches_the_hostname():
    rules = r'if client_name /\.googleusercontent\.com$/i { reject "VM"; }'
    assert _result(rules, client_name=GOOGLE_VM) == ("REJECT", "VM")


def test_client_name_does_not_match_another_host():
    rules = r'if client_name /\.googleusercontent\.com$/i { reject "VM"; }'
    assert _result(rules, client_name="mail-wr1-f41.google.com") is None


def test_client_name_anchor_rejects_lookalike_domain():
    rules = r'if client_name /\.googleusercontent\.com$/i { reject "VM"; }'
    assert _result(rules, client_name="googleusercontent.com.example.net") is None


def test_unknown_client_is_the_bracketed_address():
    rules = r'if client_name /^\[/ { warn "no verified client name"; }'
    _result_value, warns, _path = apply_rules(
        parse_rules_src(rules), MailContext(client_name="[192.0.2.1]")
    )
    assert [w[0] for w in warns] == ["no verified client name"]


def test_empty_client_name_never_matches_an_unanchored_pattern():
    rules = r'if client_name /googleusercontent/i { reject "VM"; }'
    assert _result(rules) is None


@pytest.mark.parametrize("ctx", [
    {"envelope_from": "<support@ecommercepartners.net>"},
    {"headers": {"from": ["support@ecommercepartners.net"]}},
    {"headers": {"from": ["Customer Care <support@huckmagazine.com>"]}},
    {"envelope_from": "<Support@Example.COM>"},
])
def test_combined_rule_rejects_support_sender_from_google_vm(ctx):
    assert _result(SUPPORT_FROM_GOOGLE_VM, client_name=GOOGLE_VM, **ctx) == (
        "REJECT", "Rejected: support@ sender from a Google Cloud VM")


@pytest.mark.parametrize("ctx", [
    # the same sender from anywhere else
    {"client_name": "mail.example.net", "envelope_from": "<support@example.net>"},
    {"client_name": "[192.0.2.1]", "headers": {"from": ["support@example.net"]}},
    # a Google VM, but not a support@ sender
    {"client_name": GOOGLE_VM, "envelope_from": "<newsletter@example.net>",
     "headers": {"from": ["Newsletter <newsletter@example.net>"]}},
    # support@ only somewhere inside the address, not at its start
    {"client_name": GOOGLE_VM, "envelope_from": "<techsupport@example.net>",
     "headers": {"from": ["Tech <techsupport@example.net>"]}},
])
def test_combined_rule_leaves_other_mail_alone(ctx):
    assert _result(SUPPORT_FROM_GOOGLE_VM, **ctx) is None


def test_client_name_backreference_in_message():
    rules = r'if client_name /^([^.]+)\.example\.net$/i { reject "host $1"; }'
    assert _result(rules, client_name="smtp7.example.net") == ("REJECT", "host smtp7")


def test_client_name_refile(tmp_path):
    refile = tmp_path / "cloud_vms.regex"
    refile.write_text("# default PTR names of rented VMs\n"
                      r"/\.googleusercontent\.com$/i" "\n", encoding="utf-8")
    rules_path = tmp_path / "rules.conf"
    rules_path.write_text(f'if client_name refile:{refile.name} {{ reject "VM"; }}\n',
                          encoding="utf-8")
    rules = parse_rules_src(rules_path.read_text(encoding="utf-8"), str(rules_path))
    result, _warns, _path = apply_rules(rules, MailContext(client_name=GOOGLE_VM))
    assert result == ("REJECT", "VM")


def test_client_name_needs_a_source():
    with pytest.raises(RuleError):
        parse_rules_src('if client_name { reject "VM"; }')
