"""Evaluates already-parsed rules (see rules_lang.py) against one message."""

import ipaddress
import re
from typing import Dict, List, Optional

from .logging_setup import log
from .rules_lang import (
    ActionAccept,
    ActionDunno,
    ActionReject,
    ActionWarn,
    CondAllOf,
    CondAnyOf,
    CondNot,
    IfRule,
    MatchInfo,
    NumericMatchInfo,
    NumericSource,
    RuleError,
    TestBody,
    TestCcHeader,
    TestEnvelopeFrom,
    TestEnvelopeTo,
    TestFromHeader,
    TestHeader,
    TestReplyTo,
    TestSubject,
    TestToHeader,
    _BACKREF_RE,
    _strip_line_breaks,
    parse_rules_src,
)


class MailContext:
    """The context of the message passed to the condition evaluator."""
    __slots__ = ("subject", "body", "envelope_from", "envelope_to", "headers")

    def __init__(
        self,
        subject: str = "",
        body: str = "",
        envelope_from: str = "",
        envelope_to: Optional[List[str]] = None,
        headers: Optional[Dict[str, List[str]]] = None,
    ):
        self.subject       = subject
        self.body          = body
        self.envelope_from = envelope_from
        self.envelope_to   = envelope_to or []
        self.headers       = headers or {}  # lowercase name -> list of values

    def get_header(self, name: str) -> List[str]:
        return self.headers.get(name.lower(), [])


def _match_source(source, values: List[str]):
    for value in values:
        for entry in source.entries:
            m = entry.regex.search(value)
            if m:
                return True, [MatchInfo(entry.source_path, entry.source_line, entry.raw, entry.flags_str,
                                         m.group(0), m.groups())]
    return False, []


_NUMBER_PREFIX_RE = re.compile(r"^\s*[-+]?\d+(?:\.\d+)?")

_CMP_FUNCS = {
    ">":  lambda a, b: a >  b,
    "<":  lambda a, b: a <  b,
    ">=": lambda a, b: a >= b,
    "<=": lambda a, b: a <= b,
    "==": lambda a, b: a == b,
    "!=": lambda a, b: a != b,
}


def _leading_number(raw: str) -> Optional[float]:
    """Parses a leading (optionally signed, optionally fractional) number out
    of a header/field value, e.g. "17.20" or "-100.2 required=5.0 ...". Returns
    None if the value does not start with a number."""
    m = _NUMBER_PREFIX_RE.match(raw)
    if not m:
        return None
    try:
        return float(m.group(0))
    except ValueError:
        return None


def _match_numeric(source: NumericSource, values: List[str]):
    cmp_func = _CMP_FUNCS[source.op]
    for value in values:
        actual = _leading_number(value)
        if actual is None:
            continue
        if cmp_func(actual, source.value):
            return True, [NumericMatchInfo(source.source_path, source.source_line,
                                            source.op, source.value, actual)]
    return False, []


def _match_test_source(source, values: List[str]):
    if isinstance(source, NumericSource):
        return _match_numeric(source, values)
    return _match_source(source, values)


def _eval_cond(cond, ctx: MailContext):
    """Return (boolean_result, matched_regexes)."""
    if isinstance(cond, CondAnyOf):
        for child in cond.children:
            matched, infos = _eval_cond(child, ctx)
            if matched:
                return True, infos
        return False, []
    if isinstance(cond, CondAllOf):
        all_infos = []
        for child in cond.children:
            matched, infos = _eval_cond(child, ctx)
            if not matched:
                return False, []
            all_infos.extend(infos)
        return True, all_infos
    if isinstance(cond, CondNot):
        matched, _infos = _eval_cond(cond.child, ctx)
        return (not matched), []
    if isinstance(cond, TestSubject):
        return _match_test_source(cond.source, [ctx.subject])
    if isinstance(cond, TestBody):
        return _match_test_source(cond.source, [ctx.body])
    if isinstance(cond, TestEnvelopeFrom):
        return _match_test_source(cond.source, [ctx.envelope_from])
    if isinstance(cond, TestEnvelopeTo):
        return _match_test_source(cond.source, ctx.envelope_to)
    if isinstance(cond, TestFromHeader):
        return _match_test_source(cond.source, ctx.get_header("from"))
    if isinstance(cond, TestToHeader):
        return _match_test_source(cond.source, ctx.get_header("to"))
    if isinstance(cond, TestCcHeader):
        return _match_test_source(cond.source, ctx.get_header("cc"))
    if isinstance(cond, TestReplyTo):
        return _match_test_source(cond.source, ctx.get_header("reply-to"))
    if isinstance(cond, TestHeader):
        return _match_test_source(cond.source, ctx.get_header(cond.name))
    raise TypeError(f"Unknown condition type: {type(cond)}")


def _render_action_message(msg: str, match_infos: list) -> str:
    """Substitutes $N/${N}/$$ in a reject/warn message using the single
    MatchInfo that matched the action's directly enclosing condition (rules_
    lang.py's parse-time validation guarantees match_infos holds at most one
    such MatchInfo whenever the message actually contains a backreference).
    Substituted values come straight from the sender's own message, so they
    are stripped of embedded CR/LF/tab before being placed on what will
    become either a single SMTP reply line or a folded header line."""
    if "$" not in msg:
        return msg
    info = next((i for i in match_infos if isinstance(i, MatchInfo)), None)

    def _replace(m):
        if m.group(0) == "$$":
            return "$"
        n = int(m.group(1) or m.group(2))
        if info is None:
            return ""
        if n == 0:
            value = info.matched_text
        else:
            value = info.groups[n - 1] if n - 1 < len(info.groups) else None
        return _strip_line_breaks(value) if value is not None else ""

    return _BACKREF_RE.sub(_replace, msg)


# Return value of _eval_rules: ("REJECT", msg) | ("ACCEPT",) | ("WARN", msg) | None
_CONTINUE = None


def _eval_body(body: list, ctx: MailContext, path: list):
    for stmt in body:
        result = _eval_stmt(stmt, ctx, path)
        if result is not None:
            return result
    return None


def _eval_stmt(stmt, ctx: MailContext, path: list):
    """ `path` is a shared list to which the line numbers of every
    `if` statement that evaluated to true are added together with regex match details, followed
    by the line number of the action that terminated the evaluation (or continued past `dunno`)."""
    if isinstance(stmt, IfRule):
        for cond, body, branch_line in stmt.branches:
            matched, match_infos = _eval_cond(cond, ctx)
            if matched:
                path.append((branch_line, match_infos))
                return _eval_body(body, ctx, path)
        if stmt.else_body is not None:
            path.append((stmt.line, []))
            return _eval_body(stmt.else_body, ctx, path)
        return None
    if isinstance(stmt, ActionReject):
        msg = _render_action_message(stmt.msg, path[-1][1] if path else [])
        path.append((stmt.line, []))
        return ("REJECT", msg)
    if isinstance(stmt, ActionAccept):
        path.append((stmt.line, []))
        return ("ACCEPT",)
    if isinstance(stmt, ActionWarn):
        msg = _render_action_message(stmt.msg, path[-1][1] if path else [])
        path.append((stmt.line, []))
        return ("WARN", msg)
    if isinstance(stmt, ActionDunno):
        path.append((stmt.line, []))
        return None  # continue
    raise TypeError(f"Unknown statement type: {type(stmt)}")


def apply_rules(rules: list, ctx: MailContext):
    """Evaluation loop.

    Returns (result, warn_entries, path), where:
      result       = ('REJECT', msg) | ('ACCEPT',) | None
      warn_entries = a list of (msg, path) for each warning that was triggered
      path         = a list of (line, match_infos) entries (if -> ... -> action)
                     leading to the 'result'; None if result is None
    """
    warn_entries = []
    for rule in rules:
        path = []
        result = _eval_stmt(rule, ctx, path)
        if result is None:
            continue
        if result[0] == "WARN":
            warn_entries.append((result[1], path))
            continue
        if result[0] in ("REJECT", "ACCEPT"):
            return result, warn_entries, path
    return None, warn_entries, None


def _path_lines(path):
    return [entry[0] for entry in (path or [])]


def _path_match_infos(path):
    infos = []
    for _line, entry_infos in (path or []):
        infos.extend(entry_infos)
    return infos


def _format_matches_for_log(path, max_chars: int = 0):
    infos = _path_match_infos(path)
    return ";".join(info.log_text(max_chars) for info in infos) if infos else "-"


# ---------------------------------------------------------------------------
# Loading rules
# ---------------------------------------------------------------------------

def load_rules(path: str) -> list:
    try:
        with open(path, encoding="utf-8") as fh:
            src = fh.read()
    except FileNotFoundError:
        log.error("Rules file not found: %s", path)
        return []
    except OSError as exc:
        log.error("Cannot read rules file %s: %s", path, exc)
        return []
    try:
        rules = parse_rules_src(src, path)
    except RuleError as exc:
        log.error("Rules parse error in %s: %s", path, exc)
        return []
    log.info("Loaded %d rule(s) from %s", len(rules), path)
    return rules


# ---------------------------------------------------------------------------
# Whitelist IP/CIDR
# ---------------------------------------------------------------------------

def load_whitelist(path: str) -> list:
    nets = []
    try:
        with open(path, encoding="utf-8") as fh:
            for lineno, line in enumerate(fh, 1):
                line = line.split("#")[0].strip()
                if not line:
                    continue
                try:
                    nets.append(ipaddress.ip_network(line, strict=False))
                    log.debug("Whitelist [%d]: %s", lineno, line)
                except ValueError:
                    log.warning("whitelist.cidr:%d: invalid address: %r", lineno, line)
    except FileNotFoundError:
        log.warning("Whitelist not found: %s -- no exceptions active", path)
    log.info("Loaded %d whitelist entries from %s", len(nets), path)
    return nets


def in_whitelist(ip_str: str, nets: list) -> bool:
    try:
        ip = ipaddress.ip_address(ip_str)
        return any(ip in net for net in nets)
    except ValueError:
        return False
