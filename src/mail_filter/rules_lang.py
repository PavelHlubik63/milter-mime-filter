"""The rules DSL: lexer, parser, and AST nodes.

Turns the text of a rules file into a tree of IfRule/Cond*/Test*/Action*
objects. Does not evaluate anything against a real message -- see
rules_engine.py for that.
"""

import os
import re
from typing import List, Optional

from .logging_setup import _mail_log_safe

# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

class RuleError(Exception):
    """Error whilst parsing the rules."""
    def __init__(self, msg: str, line: int = 0):
        super().__init__(msg)
        self.line = line

    def __str__(self):
        if self.line:
            return f"line {self.line}: {super().__str__()}"
        return super().__str__()


# ---------------------------------------------------------------------------
# AST nodes -- conditions (tests + logical operators)
# ---------------------------------------------------------------------------

class CondAnyOf:
    __slots__ = ("children",)
    def __init__(self, children): self.children = children

class CondAllOf:
    __slots__ = ("children",)
    def __init__(self, children): self.children = children

class CondNot:
    __slots__ = ("child",)
    def __init__(self, child): self.child = child

class RegexEntry:
    __slots__ = ("regex", "raw", "flags_str", "source_path", "source_line")
    def __init__(self, regex, raw, flags_str, source_path, source_line):
        self.regex = regex
        self.raw = raw
        self.flags_str = flags_str
        self.source_path = source_path
        self.source_line = source_line


class RegexSource:
    __slots__ = ("entries", "display", "is_refile", "source_path")
    def __init__(self, entries, display, is_refile=False, source_path=None):
        self.entries = entries
        self.display = display
        self.is_refile = is_refile
        self.source_path = source_path


class NumericSource:
    """A numeric comparison test, e.g. `header "X-Spam-Score-Total" > 15`."""
    __slots__ = ("op", "value", "display", "source_path", "source_line")
    def __init__(self, op, value, source_path, source_line):
        self.op = op
        self.value = value
        self.display = f"{op} {value}"
        self.source_path = source_path
        self.source_line = source_line


class _RegexTest:
    __slots__ = ("source",)
    def __init__(self, source):
        self.source = source


class TestSubject(_RegexTest):
    pass

class TestBody(_RegexTest):
    pass

class TestEnvelopeFrom(_RegexTest):
    pass

class TestEnvelopeTo(_RegexTest):
    pass

class TestFromHeader(_RegexTest):
    pass

class TestToHeader(_RegexTest):
    pass

class TestCcHeader(_RegexTest):
    pass

class TestReplyTo(_RegexTest):
    pass

class TestHeader:
    __slots__ = ("name", "source")
    def __init__(self, name, source):
        self.name = name.lower()
        self.source = source


def _truncate_for_log(text: str, max_chars: int) -> str:
    """Truncates an already-safe (control-character-escaped) string to at
    most max_chars characters, appending " ..." if it was cut. max_chars <= 0
    means no limit."""
    if max_chars <= 0 or len(text) <= max_chars:
        return text
    return f"{text[:max_chars]} ..."


def _strip_line_breaks(text: str) -> str:
    """Strips characters that would break a single-line protocol field this
    text gets embedded into (a folded header, an SMTP reply line) -- the
    rules DSL's string escapes let a rule author put a literal \\r/\\n/\\t
    into a warn/reject message, and $N substitution pulls in sender-
    controlled text from a regex match."""
    return text.replace("\r", " ").replace("\n", " ").replace("\t", " ")


class MatchInfo:
    __slots__ = ("source_path", "source_line", "raw", "flags_str", "matched_text", "groups")
    def __init__(self, source_path, source_line, raw, flags_str, matched_text, groups=()):
        self.source_path = source_path
        self.source_line = source_line
        self.raw = raw
        self.flags_str = flags_str
        self.matched_text = matched_text
        self.groups = groups

    def regex_literal(self):
        return f"/{self.raw}/{self.flags_str}"

    def log_text(self, max_chars: int = 0) -> str:
        """Like pcre2grep --only-matching: shows the text that actually
        matched (e.g. the one emoji that tripped an emoji rule) instead of
        the regex that matched it, which for a rule like a large emoji
        character class would otherwise dwarf the rest of the log line.
        matched_text comes straight from the message the sender controls,
        so it is run through the same control-character escaping as the
        rest of the log (_mail_log_safe) before being placed on a single
        log line, and only then truncated to max_chars (0 = no limit)."""
        snippet = _truncate_for_log(_mail_log_safe(self.matched_text), max_chars)
        return f"{self.source_path}:{self.source_line}:{snippet}"


class NumericMatchInfo:
    __slots__ = ("source_path", "source_line", "op", "threshold", "actual")
    def __init__(self, source_path, source_line, op, threshold, actual):
        self.source_path = source_path
        self.source_line = source_line
        self.op = op
        self.threshold = threshold
        self.actual = actual

    def regex_literal(self):
        return f"{self.op} {self.threshold} (actual={self.actual})"

    def log_text(self, max_chars: int = 0) -> str:
        # Always short (a comparison operator and two numbers) -- max_chars
        # is accepted only so callers can treat every *MatchInfo the same way.
        return f"{self.source_path}:{self.source_line}:{self.regex_literal()}"


# ---------------------------------------------------------------------------
# AST nodes -- actions
# ---------------------------------------------------------------------------

class ActionReject:
    __slots__ = ("msg", "line")
    def __init__(self, msg="Message rejected by mail filter", line=0):
        self.msg = msg; self.line = line

class ActionAccept:
    __slots__ = ("line",)
    def __init__(self, line=0): self.line = line

class ActionWarn:
    __slots__ = ("msg", "line")
    def __init__(self, msg, line=0): self.msg = msg; self.line = line

class ActionDunno:
    __slots__ = ("line",)
    def __init__(self, line=0): self.line = line

# ---------------------------------------------------------------------------
# AST nodes -- rules
# ---------------------------------------------------------------------------

class IfRule:
    """if condition { body } [ else if condition { body } ]* [ else { body } ]"""
    __slots__ = ("branches", "else_body", "line")
    def __init__(self, branches, else_body, line=0):
        # branches: list of (condition, body, line) where body: list of IfRule|Action
        self.branches = branches
        self.else_body = else_body   # list of IfRule|Action or None
        self.line = line             # line of the 'if' keyword that opened this rule


# ---------------------------------------------------------------------------
# Lexer
# ---------------------------------------------------------------------------

_TK_KW      = "KW"       # keyword
_TK_REGEX   = "REGEX"    # /pattern/flags
_TK_REFILE  = "REFILE"   # refile:/path/to/file
_TK_STRING  = "STRING"   # "..." or '...'
_TK_LBRACE  = "LBRACE"   # {
_TK_RBRACE  = "RBRACE"   # }
_TK_LPAREN  = "LPAREN"   # (
_TK_RPAREN  = "RPAREN"   # )
_TK_COMMA   = "COMMA"    # ,
_TK_SEMI    = "SEMI"     # ;
_TK_CMP     = "CMP"      # > < >= <= == !=
_TK_NUMBER  = "NUMBER"   # 15, 15.5, -3.2
_TK_EOF     = "EOF"

_KEYWORDS = {
    "if", "else", "anyof", "allof", "not", "and", "or",
    "reject", "accept", "warn", "dunno",
    "subject", "body", "envelope_from", "envelope_to", "rcpt_to",
    "from", "to", "cc", "reply_to", "header",
}

_UNICODE_ESC_RE = re.compile(r"\\U[0-9A-Fa-f]{8}|\\u[0-9A-Fa-f]{4}")
_FLAG_MAP = {"i": re.IGNORECASE, "m": re.MULTILINE, "s": re.DOTALL, "x": re.VERBOSE}


def _unescape_pattern(pat: str) -> str:
    def replace(m):
        return chr(int(m.group(0)[2:], 16))
    return _UNICODE_ESC_RE.sub(replace, pat)


class Token:
    __slots__ = ("kind", "value", "line")
    def __init__(self, kind, value, line): self.kind = kind; self.value = value; self.line = line
    def __repr__(self): return f"Token({self.kind}, {self.value!r}, line={self.line})"


def tokenize(src: str) -> List[Token]:
    tokens: List[Token] = []
    i = 0
    n = len(src)
    line = 1

    while i < n:
        # Whitespace
        if src[i] in " \t\r":
            i += 1
            continue
        if src[i] == "\n":
            line += 1
            i += 1
            continue
        # Comment
        if src[i] == "#":
            while i < n and src[i] != "\n":
                i += 1
            continue
        # Single-char tokens
        if src[i] == "{":
            tokens.append(Token(_TK_LBRACE, "{", line)); i += 1; continue
        if src[i] == "}":
            tokens.append(Token(_TK_RBRACE, "}", line)); i += 1; continue
        if src[i] == "(":
            tokens.append(Token(_TK_LPAREN, "(", line)); i += 1; continue
        if src[i] == ")":
            tokens.append(Token(_TK_RPAREN, ")", line)); i += 1; continue
        if src[i] == ",":
            tokens.append(Token(_TK_COMMA, ",", line)); i += 1; continue
        if src[i] == ";":
            tokens.append(Token(_TK_SEMI, ";", line)); i += 1; continue
        # Comparison operators: > < >= <= == !=
        if src[i] in "<>=!":
            two = src[i:i+2]
            if two in (">=", "<=", "==", "!="):
                tokens.append(Token(_TK_CMP, two, line)); i += 2; continue
            if src[i] in "<>":
                tokens.append(Token(_TK_CMP, src[i], line)); i += 1; continue
            raise RuleError(f"Unexpected character {src[i]!r} at line {line}", line)
        # Numeric literal: 15, 15.5, -3.2 (leading '-' only when directly
        # followed by a digit, so it never collides with anything else)
        if src[i].isdigit() or (src[i] == "-" and i + 1 < n and src[i+1].isdigit()):
            j = i + 1 if src[i] == "-" else i
            while j < n and src[j].isdigit():
                j += 1
            if j < n and src[j] == "." and j + 1 < n and src[j+1].isdigit():
                j += 1
                while j < n and src[j].isdigit():
                    j += 1
            tokens.append(Token(_TK_NUMBER, float(src[i:j]), line))
            i = j
            continue
        # Regex file reference: refile:/path/to/file
        if src.startswith("refile:", i):
            j = i + len("refile:")
            path_start = j
            while j < n and src[j] not in " \t\r\n,(){};":
                j += 1
            if j == path_start:
                raise RuleError("Empty refile path", line)
            tokens.append(Token(_TK_REFILE, src[path_start:j], line))
            i = j
            continue
        # Regex literal /pattern/flags
        if src[i] == "/":
            j = i + 1
            while j < n:
                if src[j] == "\\" and j + 1 < n:
                    j += 2  # escaped char
                elif src[j] == "/":
                    break
                else:
                    if src[j] == "\n":
                        line += 1
                    j += 1
            if j >= n:
                raise RuleError(f"Unterminated regex literal starting at line {line}")
            pattern = src[i+1:j]
            j += 1  # skip closing /
            # collect flags
            flags_start = j
            while j < n and src[j] in "imsxIMSX":
                j += 1
            flags_str = src[flags_start:j]
            tokens.append(Token(_TK_REGEX, (pattern, flags_str), line))
            i = j
            continue
        # String literal
        if src[i] in ('"', "'"):
            q = src[i]
            j = i + 1
            buf = []
            while j < n and src[j] != q:
                if src[j] == "\\" and j + 1 < n:
                    esc = src[j+1]
                    buf.append({"n": "\n", "t": "\t", "r": "\r"}.get(esc, esc))
                    j += 2
                else:
                    if src[j] == "\n":
                        line += 1
                    buf.append(src[j])
                    j += 1
            if j >= n:
                raise RuleError(f"Unterminated string literal starting at line {line}")
            tokens.append(Token(_TK_STRING, "".join(buf), line))
            i = j + 1
            continue
        # Identifier / keyword
        if src[i].isalpha() or src[i] == "_":
            j = i
            while j < n and (src[j].isalnum() or src[j] == "_"):
                j += 1
            word = src[i:j]
            kind = _TK_KW if word in _KEYWORDS else _TK_STRING
            tokens.append(Token(kind, word, line))
            i = j
            continue
        raise RuleError(f"Unexpected character {src[i]!r} at line {line}", line)

    tokens.append(Token(_TK_EOF, "", line))
    return tokens


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------

class Parser:
    def __init__(self, tokens: List[Token], rules_path: str = "<rules>"):
        self._t = tokens
        self._pos = 0
        self._rules_path = rules_path
        self._rules_dir = os.path.dirname(os.path.abspath(rules_path)) if rules_path != "<rules>" else os.getcwd()

    def _cur(self) -> Token:
        return self._t[self._pos]

    def _peek(self, offset=1) -> Token:
        idx = self._pos + offset
        return self._t[idx] if idx < len(self._t) else self._t[-1]

    def _expect(self, kind, value=None) -> Token:
        tok = self._cur()
        if tok.kind != kind or (value is not None and tok.value != value):
            exp = f"{kind}" + (f"={value!r}" if value else "")
            raise RuleError(f"Expected {exp}, got {tok.kind}={tok.value!r}", tok.line)
        self._pos += 1
        return tok

    def _consume(self, kind, value=None) -> bool:
        tok = self._cur()
        if tok.kind == kind and (value is None or tok.value == value):
            self._pos += 1
            return True
        return False

    def parse_program(self):
        rules = []
        while self._cur().kind != _TK_EOF:
            rules.append(self._parse_if_stmt())
        return rules

    def _parse_if_stmt(self) -> IfRule:
        if_line = self._cur().line
        self._expect(_TK_KW, "if")
        branches = []
        cond = self._parse_condition()
        body = self._parse_block()
        branches.append((cond, body, if_line))

        else_body = None
        while self._cur().kind == _TK_KW and self._cur().value == "else":
            self._pos += 1  # consume 'else'
            if self._cur().kind == _TK_KW and self._cur().value == "if":
                branch_line = self._cur().line
                self._pos += 1  # consume 'if'
                cond2 = self._parse_condition()
                body2 = self._parse_block()
                branches.append((cond2, body2, branch_line))
            else:
                else_body = self._parse_block()
                break

        return IfRule(branches, else_body, line=if_line)

    def _parse_block(self):
        self._expect(_TK_LBRACE)
        stmts = []
        while self._cur().kind != _TK_RBRACE:
            if self._cur().kind == _TK_EOF:
                raise RuleError("Unexpected EOF inside block", self._cur().line)
            if self._cur().kind == _TK_KW and self._cur().value == "if":
                stmts.append(self._parse_if_stmt())
            else:
                stmts.append(self._parse_action())
        self._expect(_TK_RBRACE)
        return stmts

    def _parse_action(self):
        tok = self._cur()
        if tok.kind != _TK_KW or tok.value not in ("reject", "accept", "warn", "dunno"):
            raise RuleError(
                f"Expected action (reject/accept/warn/dunno), got {tok.value!r}", tok.line
            )
        act_line = tok.line
        self._pos += 1
        if tok.value == "reject":
            msg = None
            if self._cur().kind == _TK_STRING:
                msg = self._cur().value; self._pos += 1
            self._expect(_TK_SEMI)
            return ActionReject(msg or "Message rejected by subject filter", line=act_line)
        if tok.value == "accept":
            self._expect(_TK_SEMI)
            return ActionAccept(line=act_line)
        if tok.value == "warn":
            msg = self._expect(_TK_STRING).value
            self._expect(_TK_SEMI)
            return ActionWarn(msg, line=act_line)
        if tok.value == "dunno":
            self._expect(_TK_SEMI)
            return ActionDunno(line=act_line)

    # Condition grammar (precedence, highest to lowest): NOT > AND > OR.
    # This mirrors the usual convention (e.g. Python's own "not"/"and"/"or"),
    # so "a or b and not c" parses as "a or (b and (not c))".
    # Parentheses can always be used to force a different grouping.
    #
    #   or_expr   := and_expr ( "or"  and_expr )*
    #   and_expr  := not_expr ( "and" not_expr )*
    #   not_expr  := "not" not_expr | primary
    #   primary   := "(" or_expr ")"
    #              | "anyof" "(" or_expr ("," or_expr)* ")"   # legacy function form, still supported
    #              | "allof" "(" or_expr ("," or_expr)* ")"   # legacy function form, still supported
    #              | test
    #   test      := test-keyword ( regex-source | numeric-source )
    #   regex-source  := "/" pattern "/" flags | "refile:" path
    #   numeric-source := ( ">" | "<" | ">=" | "<=" | "==" | "!=" ) number
    #
    # Note this grammar keeps the old "not(cond)" / "allof(...)" / "anyof(...)"
    # syntax fully valid: "not(cond)" is simply "not" applied to a
    # parenthesized primary, and allof/anyof remain available as an
    # alternative to writing a long chain of "and"/"or".
    #
    # Every test (subject, body, header "...", from, to, cc, reply_to,
    # envelope_from, envelope_to, rcpt_to) accepts either a regex source
    # (/pattern/flags or refile:/path) OR a numeric comparison
    # (>, <, >=, <=, ==, != followed by a number). The numeric form parses a
    # leading number out of the field's value and compares it -- e.g.
    # `header "X-Spam-Score-Total" > 15` rejects on a spam score above 15,
    # without having to hand-write a regex that covers every possible
    # digit count (2-digit scores, 3-digit scores such as GTUBE's 1000, ...).
    # A field whose value has no leading number simply does not match.

    def _parse_condition(self):
        return self._parse_or_expr()

    def _parse_or_expr(self):
        children = [self._parse_and_expr()]
        while self._consume(_TK_KW, "or"):
            children.append(self._parse_and_expr())
        return children[0] if len(children) == 1 else CondAnyOf(children)

    def _parse_and_expr(self):
        children = [self._parse_not_expr()]
        while self._consume(_TK_KW, "and"):
            children.append(self._parse_not_expr())
        return children[0] if len(children) == 1 else CondAllOf(children)

    def _parse_not_expr(self):
        if self._cur().kind == _TK_KW and self._cur().value == "not":
            self._pos += 1
            return CondNot(self._parse_not_expr())
        return self._parse_primary()

    def _parse_primary(self):
        tok = self._cur()
        if tok.kind == _TK_LPAREN:
            self._pos += 1
            cond = self._parse_or_expr()
            self._expect(_TK_RPAREN)
            return cond
        if tok.kind == _TK_KW and tok.value == "anyof":
            return self._parse_combinator(CondAnyOf)
        if tok.kind == _TK_KW and tok.value == "allof":
            return self._parse_combinator(CondAllOf)
        return self._parse_test()

    def _parse_combinator(self, cls):
        self._pos += 1  # consume anyof/allof
        self._expect(_TK_LPAREN)
        children = [self._parse_condition()]
        while self._consume(_TK_COMMA):
            children.append(self._parse_condition())
        self._expect(_TK_RPAREN)
        return cls(children)

    def _parse_test(self):
        tok = self._cur()
        if tok.kind != _TK_KW:
            raise RuleError(f"Expected test keyword, got {tok.value!r}", tok.line)
        kw = tok.value
        self._pos += 1

        if kw == "header":
            name_tok = self._cur()
            if name_tok.kind not in (_TK_STRING, _TK_KW):
                raise RuleError(f"Expected header name string, got {name_tok.value!r}", name_tok.line)
            name = name_tok.value
            self._pos += 1
            source = self._parse_test_source(name_tok.line)
            return TestHeader(name, source)

        _MAP = {
            "subject":       TestSubject,
            "body":          TestBody,
            "envelope_from": TestEnvelopeFrom,
            "envelope_to":   TestEnvelopeTo,
            "rcpt_to":       TestEnvelopeTo,
            "from":          TestFromHeader,
            "to":            TestToHeader,
            "cc":            TestCcHeader,
            "reply_to":      TestReplyTo,
        }
        if kw not in _MAP:
            raise RuleError(f"Unknown test keyword {kw!r}", tok.line)
        source = self._parse_test_source(tok.line)
        return _MAP[kw](source)

    def _parse_test_source(self, ref_line: int):
        """Dispatch to a numeric comparison or a regex source, whichever follows."""
        if self._cur().kind == _TK_CMP:
            return self._parse_numeric_source(ref_line)
        return self._parse_regex_source(ref_line)

    def _parse_numeric_source(self, ref_line: int) -> NumericSource:
        op_tok = self._expect(_TK_CMP)
        num_tok = self._expect(_TK_NUMBER)
        return NumericSource(op_tok.value, num_tok.value, self._rules_path, op_tok.line)

    def _compile_regex(self, pattern: str, flags_str: str, error_line: int, source_desc: str):
        flags_int = 0
        for ch in flags_str:
            flags_int |= _FLAG_MAP.get(ch.lower(), 0)
        try:
            return re.compile(_unescape_pattern(pattern), flags_int)
        except re.error as exc:
            raise RuleError(f"Invalid regex /{pattern}/{flags_str} in {source_desc}: {exc}", error_line)

    def _load_refile(self, ref_path: str, rule_line: int) -> RegexSource:
        resolved = ref_path if os.path.isabs(ref_path) else os.path.join(self._rules_dir, ref_path)
        resolved = os.path.normpath(resolved)
        try:
            with open(resolved, encoding="utf-8") as fh:
                lines = fh.read().splitlines()
        except OSError as exc:
            raise RuleError(f"Cannot read refile {ref_path!r}: {exc}", rule_line) from exc

        entries = []
        for lineno, line_text in enumerate(lines, 1):
            text = line_text.strip()
            if not text or text.startswith("#"):
                continue
            try:
                tokens = tokenize(text)
            except RuleError as exc:
                raise RuleError(f"Invalid refile entry {resolved}:{lineno}: {exc}", rule_line) from exc
            if len(tokens) != 2 or tokens[0].kind != _TK_REGEX or tokens[1].kind != _TK_EOF:
                raise RuleError(
                    f"Invalid refile entry {resolved}:{lineno}: expected exactly /regex/flags",
                    rule_line,
                )
            pattern, flags_str = tokens[0].value
            regex = self._compile_regex(pattern, flags_str, rule_line, f"{resolved}:{lineno}")
            entries.append(RegexEntry(regex, pattern, flags_str, resolved, lineno))

        return RegexSource(entries, f"refile:{ref_path}", is_refile=True, source_path=resolved)

    def _parse_regex_source(self, ref_line: int) -> RegexSource:
        tok = self._cur()
        if tok.kind == _TK_REFILE:
            self._pos += 1
            return self._load_refile(tok.value, tok.line)
        if tok.kind != _TK_REGEX:
            raise RuleError(
                f"Expected /regex/, refile:/path, or a comparison (>, <, >=, <=, ==, !=) "
                f"followed by a number, got {tok.value!r}", tok.line
            )
        pattern, flags_str = tok.value
        self._pos += 1
        regex = self._compile_regex(pattern, flags_str, tok.line, self._rules_path)
        entry = RegexEntry(regex, pattern, flags_str, self._rules_path, tok.line)
        return RegexSource([entry], f"/{pattern}/{flags_str}")


# ---------------------------------------------------------------------------
# $N backreferences in reject/warn messages
# ---------------------------------------------------------------------------
#
# reject "$1 attachment"; -- like Postfix pcre_table(5): $1, $2, ... pull in
# the matched regex's own capture groups, ${1} disambiguates when not
# followed by whitespace, $$ produces a literal $. Only meaningful when
# there is exactly one regex match to pull groups from, so this is only
# allowed directly inside a bare regex test or an anyof(...) of such tests
# (mirroring Postfix's own restriction: "substitutions are not available
# for negated patterns") -- never allof(...) (multiple conditions could
# each contribute groups -- which one's $1 would that be?) and never
# not(...) (a negated condition has no match to take groups from at all).

_BACKREF_RE = re.compile(r"\$\$|\$\{(\d+)\}|\$(\d+)")


def _max_backref_index(msg: str) -> Optional[int]:
    indices = [
        int(m.group(1) or m.group(2))
        for m in _BACKREF_RE.finditer(msg)
        if m.group(0) != "$$"
    ]
    return max(indices) if indices else None


def _collect_regex_entries_for_backref(cond, action_line: int) -> List["RegexEntry"]:
    """Collects every RegexEntry a $N in the action directly inside `cond`
    could ever pull groups from. Raises RuleError for any condition shape
    where that is ambiguous or impossible."""
    if isinstance(cond, CondNot):
        raise RuleError(
            "$N in a reject/warn message needs a regex match to take capture "
            "groups from, but the enclosing condition is not(...) -- a negated "
            "condition never has a match", action_line,
        )
    if isinstance(cond, CondAllOf):
        raise RuleError(
            "$N in a reject/warn message is ambiguous inside allof(...) -- "
            "more than one condition could each contribute capture groups. "
            "Use a single condition, or anyof(...), instead", action_line,
        )
    if isinstance(cond, CondAnyOf):
        entries = []
        for child in cond.children:
            entries.extend(_collect_regex_entries_for_backref(child, action_line))
        return entries
    source = getattr(cond, "source", None)
    if not isinstance(source, RegexSource):
        raise RuleError(
            "$N in a reject/warn message needs a regex-based condition "
            "(subject/body/envelope_from/envelope_to/from/to/cc/reply_to/header), "
            "not a numeric comparison", action_line,
        )
    return list(source.entries)


def _validate_message_backrefs(msg: str, cond, action_line: int) -> None:
    max_n = _max_backref_index(msg)
    if max_n is None or max_n == 0:
        return  # no $N, or only $0 (the whole match -- always available once matched)
    if cond is None:
        raise RuleError(
            "$N in a reject/warn message has no enclosing if-condition to take "
            "capture groups from (e.g. inside an else branch)", action_line,
        )
    for entry in _collect_regex_entries_for_backref(cond, action_line):
        if entry.regex.groups < max_n:
            raise RuleError(
                f"reject/warn message references ${max_n}, but the regex at "
                f"{entry.source_path}:{entry.source_line} (/{entry.raw}/{entry.flags_str}) "
                f"only has {entry.regex.groups} capture group(s)", action_line,
            )


def _walk_body_for_backrefs(body, enclosing_cond) -> None:
    for stmt in body:
        if isinstance(stmt, IfRule):
            for cond, nested_body, _branch_line in stmt.branches:
                _walk_body_for_backrefs(nested_body, cond)
            if stmt.else_body is not None:
                _walk_body_for_backrefs(stmt.else_body, None)
        elif isinstance(stmt, (ActionReject, ActionWarn)):
            _validate_message_backrefs(stmt.msg, enclosing_cond, stmt.line)


def _validate_backrefs(rules: list, path: str) -> None:
    try:
        for rule in rules:
            for cond, body, _branch_line in rule.branches:
                _walk_body_for_backrefs(body, cond)
            if rule.else_body is not None:
                _walk_body_for_backrefs(rule.else_body, None)
    except RuleError as exc:
        raise RuleError(f"{path}: {exc}") from exc


def parse_rules_src(src: str, path: str = "<rules>") -> list:
    """Parses the source text of the rules and returns a list of IfRule objects."""
    try:
        tokens = tokenize(src)
    except RuleError as exc:
        raise RuleError(f"{path}: {exc}") from exc
    try:
        rules = Parser(tokens, path).parse_program()
    except RuleError as exc:
        raise RuleError(f"{path}: {exc}") from exc
    _validate_backrefs(rules, path)
    return rules
