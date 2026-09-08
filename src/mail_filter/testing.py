"""Everything that powers --configtest / --test / --test-eml."""

import configparser
import email
import ipaddress
import logging
import os
import sys
from typing import Optional, Tuple

from . import state
from .logging_setup import _MAIL_LOG_PRIORITIES, _mail_log_format, _mail_log_format_fields, validate_log_timestamp
from .mime import _count_entities, _decode_body_for_log, _qp_soft_unfold, decode_header_value, decode_subject
from .rules_engine import MailContext, apply_rules, load_rules
from .rules_lang import RuleError, parse_rules_src


# ---------------------------------------------------------------------------
# --configtest
# ---------------------------------------------------------------------------

def run_configtest(config_path: Optional[str] = None) -> int:
    errors = 0
    config_path = config_path or state._CONFIG_FILE

    print("==> Checking config file")
    cfg = configparser.ConfigParser(interpolation=None)
    if not os.path.exists(config_path):
        if config_path != state._CONFIG_FILE:
            print(f"    FAIL: not found: {config_path}")
            return 1
        print(f"    WARN: not found: {config_path} -- using defaults")
    else:
        try:
            state.read_config(cfg, config_path)
        except state.ConfigError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 2
        print(f"    OK:   {config_path}")

    rules_value = cfg.get("files", "rules", fallback=state._RULES_FALLBACK)
    whitelist_value = cfg.get("files", "whitelist", fallback=state._WHITELIST_FALLBACK)
    rules_path = state.resolve_config_path(config_path, rules_value) if config_path != state._CONFIG_FILE or not os.path.isabs(rules_value) else rules_value
    whitelist_path = state.resolve_config_path(config_path, whitelist_value) if config_path != state._CONFIG_FILE or not os.path.isabs(whitelist_value) else whitelist_value

    print("==> Checking milter logging settings")
    timestamp_format = cfg.get("milter", "log_timestamp", fallback="bsd").strip()
    try:
        validate_log_timestamp(timestamp_format)
        print(f"    OK:   log_timestamp={timestamp_format}")
    except ValueError as exc:
        print(f"    FAIL: {exc}")
        errors += 1

    print("==> Checking body-size limit")
    try:
        max_body_kb = cfg.getint("milter", "max_body_kb", fallback=state._DEFAULT_MAX_BODY_KB)
        if max_body_kb < 0:
            print(f"    FAIL: max_body_kb must be >= 0 (0 disables the limit), got {max_body_kb}")
            errors += 1
        else:
            print(f"    OK:   max_body_kb={max_body_kb}")
    except ValueError as exc:
        print(f"    FAIL: max_body_kb: {exc}")
        errors += 1

    print("==> Checking log_match_max_chars")
    try:
        log_match_max_chars = cfg.getint(
            "milter", "log_match_max_chars", fallback=state._DEFAULT_LOG_MATCH_MAX_CHARS
        )
        if log_match_max_chars < 0:
            print(f"    FAIL: log_match_max_chars must be >= 0 (0 disables truncation), got {log_match_max_chars}")
            errors += 1
        else:
            print(f"    OK:   log_match_max_chars={log_match_max_chars}")
    except ValueError as exc:
        print(f"    FAIL: log_match_max_chars: {exc}")
        errors += 1

    print("==> Checking add_warn_header")
    try:
        add_warn_header = cfg.getboolean("milter", "add_warn_header", fallback=False)
        print(f"    OK:   add_warn_header={add_warn_header}")
    except ValueError as exc:
        print(f"    FAIL: add_warn_header: {exc}")
        errors += 1

    print(f"==> Checking rules file: {rules_path}")
    try:
        with open(rules_path, encoding="utf-8") as fh:
            src = fh.read()
        rules = parse_rules_src(src, rules_path)
        print(f"    OK:   {len(rules)} rule(s) parsed and validated")
    except FileNotFoundError:
        print(f"    FAIL: not found: {rules_path}")
        errors += 1
    except OSError as exc:
        print(f"    FAIL: cannot read {rules_path}: {exc}")
        errors += 1
    except RuleError as exc:
        print(f"    FAIL: {exc}")
        errors += 1

    print(f"==> Checking whitelist file: {whitelist_path}")
    try:
        with open(whitelist_path, encoding="utf-8") as fh:
            wl_errors = 0
            wl_count  = 0
            for lineno, line in enumerate(fh, 1):
                line = line.split("#")[0].strip()
                if not line:
                    continue
                try:
                    ipaddress.ip_network(line, strict=False)
                    wl_count += 1
                except ValueError:
                    print(f"    FAIL: line {lineno}: invalid address: {line!r}")
                    wl_errors += 1
        if wl_errors:
            errors += wl_errors
        else:
            print(f"    OK:   {wl_count} entry/entries validated")
    except FileNotFoundError:
        print(f"    WARN: not found: {whitelist_path} -- no IP exceptions active")
    except OSError as exc:
        print(f"    FAIL: cannot read {whitelist_path}: {exc}")
        errors += 1

    print("==> Checking mail_log settings")
    try:
        enabled = cfg.getboolean("mail_log", "enabled", fallback=False)
    except ValueError as exc:
        print(f"    FAIL: enabled: {exc}")
        errors += 1
        enabled = False
    priority_name = cfg.get("mail_log", "priority", fallback="warning").strip().lower()
    settings_ok = True
    if priority_name not in _MAIL_LOG_PRIORITIES:
        print(f"    FAIL: unknown priority: {priority_name!r}")
        errors += 1
        settings_ok = False
    mail_format = cfg.get("mail_log", "format", fallback=_mail_log_format)
    try:
        fields = _mail_log_format_fields(mail_format)
        if enabled and not fields:
            print("    FAIL: enabled mail_log format contains no fields")
            errors += 1
            settings_ok = False
    except (ValueError, KeyError) as exc:
        print(f"    FAIL: format: {exc}")
        errors += 1
        settings_ok = False
        fields = []
    if settings_ok:
        state_str = "enabled" if enabled else "disabled"
        print(f"    OK:   {state_str}; {len(fields)} field reference(s); facility=mail; priority={priority_name}")

    print()
    if errors == 0:
        print("Configuration OK.")
    else:
        print(f"Configuration has {errors} error(s).")
    return errors


# ---------------------------------------------------------------------------
# --test (interactive rule testing)
# ---------------------------------------------------------------------------

def _build_test_ctx(args) -> Tuple[MailContext, str, str, str]:
    """Builds a MailContext from command-line arguments or a JSON file."""
    if args.test_context:
        import json
        with open(args.test_context, encoding="utf-8") as fh:
            data = json.load(fh)
        headers = {}
        for k, v in (data.get("headers") or {}).items():
            values = v if isinstance(v, list) else [v]
            headers[k.lower()] = [decode_header_value(str(value)) for value in values]
        subject_raw = data.get("subject", "")
        subject_dec = decode_subject(subject_raw)
        body_raw = data.get("body", "")
        body_proc = _qp_soft_unfold(body_raw)
        return MailContext(
            subject=subject_dec,
            body=body_proc,
            envelope_from=data.get("envelope_from", ""),
            envelope_to=data.get("envelope_to", []),
            headers=headers,
        ), subject_raw, subject_dec, body_raw
    else:
        subject_raw = args.subject or ""
        try:
            subject_dec = decode_subject(subject_raw)
        except Exception:
            subject_dec = subject_raw
        body_raw = ""
        if args.body:
            body_raw = args.body
        elif args.body_file:
            try:
                with open(args.body_file, encoding="utf-8") as fh:
                    body_raw = fh.read()
            except OSError as exc:
                print(f"ERROR: cannot read body file: {exc}", file=sys.stderr)
                sys.exit(2)
        body_proc = _qp_soft_unfold(body_raw)
        headers = {}
        for hstr in (args.header or []):
            if ":" in hstr:
                hname, _, hval = hstr.partition(":")
                headers.setdefault(hname.strip().lower(), []).append(decode_header_value(hval.strip()))
        # From/To headers from explicit switches
        if args.from_header:
            headers.setdefault("from", []).append(decode_header_value(args.from_header))
        if args.to_header:
            headers.setdefault("to", []).append(decode_header_value(args.to_header))
        return MailContext(
            subject=subject_dec,
            body=body_proc,
            envelope_from=args.envelope_from or "",
            envelope_to=args.envelope_to or [],
            headers=headers,
        ), subject_raw, subject_dec, body_raw


def run_test(args) -> int:
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s",
                        stream=sys.stderr)
    cfg = configparser.ConfigParser(interpolation=None)
    config_path = args.test_config or state._CONFIG_FILE
    if args.test_config and not os.path.exists(config_path):
        print(f"ERROR: test config file not found: {config_path}", file=sys.stderr)
        return 2
    try:
        state.read_config(cfg, config_path)
    except state.ConfigError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    rules_value = cfg.get("files", "rules", fallback=state._RULES_FALLBACK)
    rules_path = state.resolve_config_path(config_path, rules_value) if args.test_config or not os.path.isabs(rules_value) else rules_value

    try:
        rules = load_rules(rules_path)
    except Exception as exc:
        print(f"ERROR loading rules: {exc}", file=sys.stderr)
        return 2

    if not rules:
        print("WARNING: no rules loaded", file=sys.stderr)

    ctx, subject_raw, subject_dec, body_raw = _build_test_ctx(args)

    print(f"Subject(raw):     {subject_raw!r}")
    print(f"Subject(decoded): {subject_dec!r}")
    print(f"envelope_from:    {ctx.envelope_from!r}")
    print(f"envelope_to:      {ctx.envelope_to!r}")
    if ctx.headers:
        for k, vs in ctx.headers.items():
            for v in vs:
                print(f"header:           {k}: {v!r}")
    entity_count = _count_entities(ctx.body)
    print(f"body_size:        {len(ctx.body)} chars")
    print(f"entity_count:     {entity_count}")
    if ctx.body:
        preview = _decode_body_for_log(ctx.body)
        print(f"body_preview:     {preview[:200]!r}")
    print()

    # Source lines of the rules file, used to look up the rule text by line number.
    try:
        with open(rules_path, encoding="utf-8") as fh:
            src_lines = fh.read().splitlines()
    except OSError:
        src_lines = []

    def _fmt_path(path):
        """Formats the matched-rule path, e.g.:
             line 3: if subject /pattern/i {
             line 4:   if not(envelope_from /pattern2/i) {
             line 5:     reject;
        """
        out = []
        for ln, match_infos in path:
            text = src_lines[ln - 1].strip() if 0 < ln <= len(src_lines) else "?"
            out.append(f"    line {ln}: {text}")
            for info in match_infos:
                if os.path.abspath(info.source_path) != os.path.abspath(rules_path) or info.source_line != ln:
                    out.append(f"        matched {info.source_path}:{info.source_line}: {info.regex_literal()}")
                else:
                    out.append(f"        matched {info.regex_literal()}")
        return "\n".join(out)

    result, warns, path = apply_rules(rules, ctx)

    for wmsg, wpath in warns:
        print(f"WARN: {wmsg}  (rules {rules_path})")
        print(_fmt_path(wpath))

    if result is None:
        print("No rule matched --> ACCEPT")
        return 0
    if result[0] == "ACCEPT":
        print(f"Rule matched --> ACCEPT  (rules {rules_path})")
        print(_fmt_path(path))
        return 0
    if result[0] == "REJECT":
        print(f"Rule matched --> REJECT: {result[1]}  (rules {rules_path})")
        print(_fmt_path(path))
        return 1

    print("No terminal rule --> ACCEPT")
    return 0


# ---------------------------------------------------------------------------
# --test-eml (EML file testing)
# ---------------------------------------------------------------------------

def run_test_eml(eml_path: str, config_path: Optional[str] = None) -> int:
    """Load an EML file (RFC 2822 or mbox), extract subject/body, and apply rules."""
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s",
                        stream=sys.stderr)

    cfg = configparser.ConfigParser(interpolation=None)
    config_path = config_path or state._CONFIG_FILE
    try:
        state.read_config(cfg, config_path)
    except state.ConfigError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    rules_value = cfg.get("files", "rules", fallback=state._RULES_FALLBACK)
    rules_path = state.resolve_config_path(config_path, rules_value) if config_path != state._CONFIG_FILE or not os.path.isabs(rules_value) else rules_value
    rules = load_rules(rules_path)

    try:
        with open(eml_path, "rb") as fh:
            raw_bytes = fh.read()
    except OSError as exc:
        print(f"ERROR: cannot read {eml_path}: {exc}", file=sys.stderr)
        return 2

    if raw_bytes.startswith(b"From "):
        raw_bytes = raw_bytes.split(b"\n", 1)[1] if b"\n" in raw_bytes else raw_bytes

    msg = email.message_from_bytes(raw_bytes)

    # Extract the body (everything after the headers) -- simulates what the milter actually receives.
    for sep in (b"\r\n\r\n", b"\n\n"):
        if sep in raw_bytes:
            _, _, body_bytes = raw_bytes.partition(sep)
            break
    else:
        body_bytes = b""

    try:
        raw_text = body_bytes[:state._DEFAULT_MAX_BODY_KB * 1024].decode("utf-8", errors="replace")
    except Exception:
        raw_text = ""
    body_text = _qp_soft_unfold(raw_text)

    subject_raw = msg.get("Subject", "")
    try:
        subject_dec = decode_subject(subject_raw)
    except Exception:
        subject_dec = subject_raw

    print(f"=== EML: {eml_path} ===")
    print(f"From:             {msg.get('From', '(none)')}")
    print(f"To:               {msg.get('To', '(none)')}")
    print(f"Subject(raw):     {subject_raw!r}")
    print(f"Subject(decoded): {subject_dec!r}")
    print(f"Date:             {msg.get('Date', '(none)')}")
    entity_count = _count_entities(body_text)
    print(f"body_size:        {len(body_text)} chars")
    print(f"entity_count:     {entity_count}")
    if body_text:
        preview = _decode_body_for_log(body_text)
        print(f"body_preview:     {preview[:200]!r}")
    print()

    parts_found = 0
    for part in msg.walk():
        ct = part.get_content_type()
        if ct not in ("text/plain", "text/html"):
            continue
        payload_bytes = part.get_payload(decode=True)
        if not payload_bytes:
            continue
        parts_found += 1
        charset = part.get_content_charset() or "utf-8"
        cte = part.get("Content-Transfer-Encoding", "none")
        try:
            part_text = payload_bytes.decode(charset, errors="replace")
        except LookupError:
            part_text = payload_bytes.decode("latin-1", errors="replace")
        print(f"--- MIME part {parts_found}: {ct} (charset={charset}, cte={cte}, "
              f"{len(part_text)} chars) ---")

    if parts_found:
        print()

    headers = {}
    for hname in ("From", "To", "Cc", "Reply-To"):
        hval = msg.get(hname)
        if hval:
            headers.setdefault(hname.lower(), []).append(decode_header_value(hval))

    ctx = MailContext(
        subject=subject_dec,
        body=body_text,
        envelope_from="",
        envelope_to=[],
        headers=headers,
    )

    result, warns, path = apply_rules(rules, ctx)

    for wmsg, _wpath in warns:
        print(f"WARN: {wmsg}")

    if result is None:
        print("No rule matched --> ACCEPT")
        return 0
    if result[0] == "ACCEPT":
        print("Rule matched --> ACCEPT")
        return 0
    if result[0] == "REJECT":
        print(f"Rule matched --> REJECT: {result[1]}")
        return 1

    print("No terminal rule --> ACCEPT")
    return 0
