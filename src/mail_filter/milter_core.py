"""The milter daemon itself: the MailFilter class, and its reload/shutdown
signal handlers."""

import email.header
import logging
import os
import signal
from typing import Dict, List, Optional

import Milter

from . import __version__, state
from .logging_setup import _log_always, _write_mail_log, log
from .mime import _count_entities, _qp_soft_unfold, decode_header_value, decode_subject, unfold_header_value
from .rules_engine import MailContext, _format_matches_for_log, _path_lines, apply_rules, in_whitelist, load_rules, load_whitelist
from .rules_lang import _strip_line_breaks

_WARN_HEADER_NAME = "X-Mail-Filter-Status"


def _build_warn_header_value(warns) -> str:
    """"WARN \"text\"" for a single warn. For more than one, folds like
    SpamAssassin's X-Spam-Status: one tab-indented, quoted warn per
    continuation line, so grepping for one warn's text still finds exactly
    one line. Each warn's text is RFC 2047-encoded (a no-op for plain
    ASCII) since it comes straight from a rules.conf warn "..."; string,
    which may contain non-ASCII text (and may already have had $N
    backreferences substituted in by rules_engine._render_action_message)."""
    texts = [
        email.header.Header(_strip_line_breaks(wmsg)).encode()
        for wmsg, _wpath in warns
    ]
    if len(texts) == 1:
        return f'WARN "{texts[0]}"'
    return "\n".join(["WARN"] + [f'\t"{t}"' for t in texts])


def _reload(signum=None, frame=None) -> None:
    log.info("SIGUSR1: reloading rules and whitelist")
    rules_path = state.resolve_config_path(
        state._CONFIG_FILE, state.cfg.get("files", "rules", fallback=state._RULES_FALLBACK)
    )
    whitelist_path = state.resolve_config_path(
        state._CONFIG_FILE, state.cfg.get("files", "whitelist", fallback=state._WHITELIST_FALLBACK)
    )
    state.rules = load_rules(rules_path)
    state.whitelist = load_whitelist(whitelist_path)


def _shutdown(signum, frame) -> None:
    sig_name = {signal.SIGTERM: "SIGTERM", signal.SIGINT: "SIGINT"}.get(signum, str(signum))
    _log_always("Signal %s received -- shutting down mail_filter %s", sig_name, __version__)
    logging.shutdown()
    os._exit(0)


class MailFilter(Milter.Base):

    def __init__(self):
        self._id:            int            = Milter.uniqueID()
        self._ip:            str            = "unknown"
        self._whitelisted:   bool           = False
        self._env_from:      str            = ""
        self._env_rcpt:      List[str]      = []
        self._headers:       Dict[str, List[str]] = {}   # lowercase name -> decoded/unfolded values
        self._headers_raw:   Dict[str, List[str]] = {}   # lowercase name -> original values
        self._subject_raw:   Optional[str]  = None
        self._msgid:         Optional[str]  = None
        self._body_chunks:   List[bytes]    = []
        self._body_truncated: bool          = False
        self._body_size:     int            = 0

    def _store_header(self, name: str, val: str) -> None:
        key = name.lower()
        self._headers_raw.setdefault(key, []).append(val)
        try:
            decoded = decode_header_value(val)
        except Exception as exc:
            log.warning("[%d] Header decode failed name=%s error=%s", self._id, name, exc)
            decoded = unfold_header_value(val)
        self._headers.setdefault(key, []).append(decoded)

    def _add_warn_header(self, warns) -> None:
        if not state.add_warn_header or not warns:
            return
        self.addheader(_WARN_HEADER_NAME, _build_warn_header_value(warns))

    @Milter.noreply
    def connect(self, hostname, family, hostaddr):
        self._ip = hostaddr[0] if hostaddr else "unknown"
        self._whitelisted = in_whitelist(self._ip, state.whitelist)
        log.debug("[%d] CONNECT ip=%s host=%s%s",
                  self._id, self._ip, hostname,
                  " WHITELISTED" if self._whitelisted else "")
        return Milter.CONTINUE

    @Milter.noreply
    def hello(self, heloname):
        log.debug("[%d] HELO/EHLO %s", self._id, heloname)
        return Milter.CONTINUE

    @Milter.noreply
    def envfrom(self, f, *args):
        self._env_from        = f
        self._env_rcpt        = []
        self._headers          = {}
        self._headers_raw      = {}
        self._subject_raw      = None
        self._msgid           = None
        self._body_chunks     = []
        self._body_truncated  = False
        self._body_size       = 0
        log.debug("[%d] MAIL FROM %s", self._id, f)
        return Milter.CONTINUE

    @Milter.noreply
    def envrcpt(self, to, *args):
        self._env_rcpt.append(to)
        log.debug("[%d] RCPT TO %s", self._id, to)
        return Milter.CONTINUE

    @Milter.noreply
    def header(self, name, hval):
        lname = name.lower()
        self._store_header(name, hval)
        if lname == "subject":
            self._subject_raw = hval
            log.debug("[%d] Subject(raw): %r", self._id, hval)
        elif lname == "message-id":
            self._msgid = hval.strip()
        else:
            log.debug("[%d] Header: %s: %s", self._id, name, hval)
        return Milter.CONTINUE

    @Milter.noreply
    def body(self, chunk: bytes):
        if self._body_truncated:
            return Milter.CONTINUE
        if self._body_size + len(chunk) > state.max_body_bytes:
            remaining = state.max_body_bytes - self._body_size
            if remaining > 0:
                self._body_chunks.append(chunk[:remaining])
                self._body_size += remaining
            self._body_truncated = True
            log.debug("[%d] Body truncated at %d bytes (limit %d KB)",
                      self._id, self._body_size, state.max_body_bytes // 1024)
        else:
            self._body_chunks.append(chunk)
            self._body_size += len(chunk)
        return Milter.CONTINUE

    def eom(self):
        envelope_to     = ",".join(self._env_rcpt) or "-"
        header_from     = " | ".join(self._headers.get("from", [])) or "-"
        header_from_raw = " | ".join(self._headers_raw.get("from", [])) or "-"
        header_to       = " | ".join(self._headers.get("to", [])) or "-"
        header_to_raw   = " | ".join(self._headers_raw.get("to", [])) or "-"
        header_cc       = " | ".join(self._headers.get("cc", [])) or "-"
        header_cc_raw   = " | ".join(self._headers_raw.get("cc", [])) or "-"
        reply_to        = " | ".join(self._headers.get("reply-to", [])) or "-"
        reply_to_raw    = " | ".join(self._headers_raw.get("reply-to", [])) or "-"
        x_original_to   = " | ".join(self._headers.get("x-original-to", [])) or "-"
        delivered_to    = " | ".join(self._headers.get("delivered-to", [])) or "-"
        qid           = self.getsymval("{i}") or "-"
        msgid         = self._msgid or "-"

        decode_error = "-"
        if self._subject_raw is None:
            decoded = "-"
            ctx_subject = ""
        else:
            try:
                decoded = decode_subject(self._subject_raw)
                ctx_subject = decoded
            except Exception as exc:
                decode_error = str(exc)
                decoded = self._subject_raw
                ctx_subject = self._subject_raw

        # Decode the accumulated body: QP soft-breaks are merged first so that
        # entities split across a line boundary (e.g. &#22=\r\n5;) are rejoined.
        raw_body_bytes = b"".join(self._body_chunks)
        try:
            body_text = raw_body_bytes.decode("utf-8", errors="replace")
        except Exception as exc:
            log.warning("[%d] Body decode error (%s) -- treating body as empty", self._id, exc)
            body_text = ""
        body_text = _qp_soft_unfold(body_text)
        entity_count = _count_entities(body_text)

        base_log = {
            "qid": qid,
            "message_id": msgid,
            "client_ip": self._ip,
            "envelope_from": self._env_from or "-",
            "envelope_to": envelope_to,
            "header_from": header_from,
            "header_from_raw": header_from_raw,
            "header_to": header_to,
            "header_to_raw": header_to_raw,
            "header_cc": header_cc,
            "header_cc_raw": header_cc_raw,
            "reply_to": reply_to,
            "reply_to_raw": reply_to_raw,
            "x_original_to": x_original_to,
            "delivered_to": delivered_to,
            "subject_raw": self._subject_raw if self._subject_raw is not None else "-",
            "subject_decoded": decoded,
            "decode_error": decode_error,
            "body_size": self._body_size,
            "body_size_bytes": self._body_size,
            "body_truncated": "yes" if self._body_truncated else "no",
            "entity_count": entity_count,
            "result": "ACCEPT",
            "reason": "-",
            "warn": "-",
            "warn_rules": "-",
            "rule_line": "-",
            "matched": "-",
        }

        if self._whitelisted:
            log.info("[%d] qid=%s msgid=%s result=ACCEPT reason=whitelisted ip=%s envelope_from=%r envelope_to=%r header_from=%r header_to=%r x_original_to=%r delivered_to=%r subject_raw=%r subject_decoded=%r body_size=%d entity_count=%d",
                     self._id, qid, msgid, self._ip, self._env_from or "-", envelope_to,
                     header_from, header_to, x_original_to, delivered_to,
                     self._subject_raw if self._subject_raw is not None else "-", decoded,
                     self._body_size, entity_count)
            _write_mail_log({**base_log, "reason": "whitelisted"})
            return Milter.ACCEPT

        # Note: unlike the historical subject-only milter, a missing Subject
        # header (or an empty body) no longer short-circuits evaluation --
        # rules that only test the body, envelope, or other headers must
        # still run even when the Subject header is absent.
        ctx = MailContext(
            subject=ctx_subject,
            body=body_text,
            envelope_from=self._env_from,
            envelope_to=list(self._env_rcpt),
            headers=dict(self._headers),
        )

        result, warns, path = apply_rules(state.rules, ctx)

        warn_text = " | ".join(wmsg for wmsg, _wpath in warns) or "-"
        warn_rules = " | ".join(
            f"{wmsg}:line={'->'.join(str(l) for l in _path_lines(wpath))}:matched={_format_matches_for_log(wpath, state.log_match_max_chars)}"
            for wmsg, wpath in warns
        ) or "-"

        if result is None:
            level = log.warning if warns or decode_error != "-" else log.info
            level("[%d] qid=%s msgid=%s result=ACCEPT reason=no_rule_matched ip=%s envelope_from=%r envelope_to=%r header_from=%r header_to=%r x_original_to=%r delivered_to=%r subject_raw=%r subject_decoded=%r decode_error=%r body_size=%d entity_count=%d warn=%r warn_rules=%r",
                  self._id, qid, msgid, self._ip, self._env_from or "-", envelope_to,
                  header_from, header_to, x_original_to, delivered_to, self._subject_raw, decoded,
                  decode_error, self._body_size, entity_count, warn_text, warn_rules)
            _write_mail_log({**base_log, "reason": "no_rule_matched", "warn": warn_text, "warn_rules": warn_rules})
            self._add_warn_header(warns)
            return Milter.ACCEPT

        rule_line = "->".join(str(l) for l in _path_lines(path)) if path else "?"
        matched = _format_matches_for_log(path, state.log_match_max_chars)

        if result[0] == "REJECT":
            msg = result[1]
            log.warning("[%d] qid=%s msgid=%s result=REJECT ip=%s envelope_from=%r envelope_to=%r header_from=%r header_to=%r x_original_to=%r delivered_to=%r subject_raw=%r subject_decoded=%r decode_error=%r body_size=%d entity_count=%d msg=%r rule_line=%s matched=%s warn=%r warn_rules=%r",
                        self._id, qid, msgid, self._ip, self._env_from or "-", envelope_to,
                        header_from, header_to, x_original_to, delivered_to, self._subject_raw, decoded,
                        decode_error, self._body_size, entity_count, msg, rule_line, matched, warn_text, warn_rules)
            _write_mail_log({**base_log, "result": "REJECT", "reason": msg, "warn": warn_text,
                "warn_rules": warn_rules, "rule_line": rule_line, "matched": matched})
            self.setreply("550", "5.7.1", msg)
            return Milter.REJECT

        if result[0] == "ACCEPT":
            level = log.warning if warns or decode_error != "-" else log.info
            level("[%d] qid=%s msgid=%s result=ACCEPT reason=rule ip=%s envelope_from=%r envelope_to=%r header_from=%r header_to=%r x_original_to=%r delivered_to=%r subject_raw=%r subject_decoded=%r decode_error=%r body_size=%d entity_count=%d rule_line=%s matched=%s warn=%r warn_rules=%r",
                  self._id, qid, msgid, self._ip, self._env_from or "-", envelope_to,
                  header_from, header_to, x_original_to, delivered_to, self._subject_raw, decoded,
                  decode_error, self._body_size, entity_count, rule_line, matched, warn_text, warn_rules)
            _write_mail_log({**base_log, "reason": "rule", "warn": warn_text, "warn_rules": warn_rules,
                "rule_line": rule_line, "matched": matched})
            self._add_warn_header(warns)
            return Milter.ACCEPT

        log.info("[%d] qid=%s msgid=%s result=ACCEPT reason=fallthrough ip=%s envelope_from=%r envelope_to=%r header_from=%r header_to=%r x_original_to=%r delivered_to=%r subject_raw=%r subject_decoded=%r decode_error=%r body_size=%d entity_count=%d warn=%r warn_rules=%r",
                 self._id, qid, msgid, self._ip, self._env_from or "-", envelope_to,
                 header_from, header_to, x_original_to, delivered_to, self._subject_raw, decoded,
                 decode_error, self._body_size, entity_count, warn_text, warn_rules)
        _write_mail_log({**base_log, "reason": "fallthrough", "warn": warn_text, "warn_rules": warn_rules})
        self._add_warn_header(warns)
        return Milter.ACCEPT

    def close(self):
        log.debug("[%d] close", self._id)
        return Milter.CONTINUE

    def abort(self):
        log.debug("[%d] abort", self._id)
        return Milter.CONTINUE
