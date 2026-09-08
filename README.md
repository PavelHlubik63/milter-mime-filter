# milter-mime-filter

**MIME-aware Postfix milter that filters mail on *decoded* Subject, headers, and body — not on the raw `=?UTF-8?B?...?=` garbage most filters see.**

## The problem

Almost every spam and phishing subject today arrives RFC 2047-encoded:

```
Subject: =?UTF-8?B?WW91IGhhdmUgYmVlbiBoYWNrZWQ=?=
```

Postfix's built-in `header_checks` and most lightweight milters (`milter-regex`, `header_checks`) match this **raw, still-encoded string** — a regex like `/hacked/i` simply never fires, because the word "hacked" doesn't appear anywhere in the wire format. Spammers don't even need to try; encoding the subject is enough to slip past a huge share of naive filters.

`milter-mime-filter` decodes the header (and reconstructs QP soft-broken body text) **before** any rule runs, so your regexes see what a human reading the mail would see.

## Features

- **RFC 2047 decoding** (Q-encoding and Base64, any advertised charset) of `Subject`, `From`, `To`, `Cc`, `Reply-To`, and arbitrary headers — before matching, not after.
- **Body matching** on raw UTF-8 text, with quoted-printable soft line-breaks merged back together so patterns spanning `=\r\n` boundaries still match.
- **One rules DSL** that freely combines subject, body, envelope (`envelope_from`/`envelope_to`), and arbitrary-header conditions in a single rule, with `and` / `or` / `not` (plus legacy `anyof()` / `allof()` / `not()`).
- **External regex lists** via `refile:/path/to/list.inc` (e.g. a maintained emoji-subject list).
- **CIDR/IP whitelist**, evaluated independently of Postfix `mynetworks`.
- **Live reload** — `service mail_filter reload` / `systemctl reload mail_filter` — no restart, no dropped connections.
- **Built-in test tooling**: `--configtest`, `--test` (feed subject/body/headers on the command line or as JSON context), `--test-eml message.eml`, plus a standalone `check_regex.py` for batch-testing a regex list against a corpus of sample lines.
- **Structured logging**: plain application log (BSD or ISO-8601 timestamps, or any custom Python `time.strftime()` format) and an optional structured `mail_log` record over `syslog(3)` for SIEM ingestion.
- Packaged for **FreeBSD** (`pkg`, `rc.d`, `newsyslog.conf.d`) and **Debian/Ubuntu** (`.deb`, `systemd`) with proper `configtest`/`reload` service integration.

## How it compares

| Tool | Decodes before matching? | Scope |
|---|---|---|
| Postfix `header_checks` / `milter-regex` | No — matches raw encoded header | header only |
| [`milter-regex-too`](https://github.com/douzzer/milter-regex-too) | Yes (GMIME, C) | Subject/From/To only, no body/envelope/whitelist bundled |
| [`milter-decode-headers`](https://github.com/mikenye/milter-decode-headers) | Decodes only, doesn't filter — needs chaining with another milter | headers only |
| Rspamd | Yes, but as part of a full antispam engine | everything, but heavyweight to run just for this |
| **milter-mime-filter** | Yes — Subject/From/To/Cc/Reply-To/body, one rule DSL | single-purpose, lightweight, one Python process |

## Requirements

- Python ≥ 3.8
- [`pymilter`](https://pypi.org/project/pymilter/) (`py3XX-pymilter` on FreeBSD, `python3-milter` on Debian/Ubuntu)
- Postfix with milter support enabled

## Installation

### FreeBSD

Download the `.pkg` for the version you want from the [Releases page](../../releases), then:

```sh
pkg add mail_filter-<version>.pkg
```

The post-install step creates the working config from the `.sample` files (without overwriting an existing config), creates `/var/run/mail_filter` (for the PID file) and `/var/log/mail_filter.log`, and checks that `pymilter` is importable.

```sh
sysrc mail_filter_enable="YES"
service mail_filter start
```

### Debian / Ubuntu

Download the `.deb` for the version you want from the [Releases page](../../releases), then:

```sh
apt install ./mail-filter_<version>_all.deb
systemctl enable --now mail_filter
```

### Postfix integration

Add the milter to `main.cf`, the same way on both platforms:

```
smtpd_milters     = unix:private/mail_filter.sock
non_smtpd_milters = unix:private/mail_filter.sock
milter_default_action = accept
milter_protocol = 6
```

`non_smtpd_milters` covers locally injected mail, which reaches Postfix through `cleanup` rather than `smtpd`; leave it out and `sendmail`-submitted mail passes unfiltered. `milter_protocol = 6` is what libmilter speaks here.

`private/…` is Postfix's own convention: it resolves the name relative to `queue_directory` (`postconf -h queue_directory`, normally `/var/spool/postfix`), so the socket lives at `/var/spool/postfix/private/mail_filter.sock`. That is the matching `socket` value in `mail_filter.conf`, and it is what the post-install step writes there when it creates the file — so the two sides agree out of the box.

This one location is correct **whether or not** `smtpd` and `cleanup` are chrooted, on FreeBSD as much as on Linux. A chrooted process can reach nothing outside `queue_directory`, and a non-chrooted one reaches that path exactly as easily as any other. It also stays correct if the chroot setting is flipped later.

An absolute path somewhere else — `unix:/var/run/mail_filter/mail_filter.sock`, say — works too, but only for as long as nothing is chrooted (`postconf -M`, a `y` in the chroot column). If you prefer one, set the identical path on both sides: `smtpd_milters` in `main.cf` and `socket` in `mail_filter.conf`.

This package never edits `main.cf`. If `mail_filter.conf` is created fresh by the install (first install, or the file was deleted) and Postfix's `smtpd_milters`/`non_smtpd_milters` already name this socket, the post-install scripts take Postfix's own directive as the source of truth and write the resolved path into the new file. If an existing `mail_filter.conf` is preserved instead, or Postfix is not wired up yet, they change nothing and print the exact fix when they can see a mismatch.

## Configuration

Three files, all live-reloadable without restarting the daemon:

- `mail_filter.conf` — socket path, log level/format, `max_body_kb`, optional structured `mail_log`.
- `mail_filter_rules.conf` — the rules, in the DSL described below.
- `mail_filter_whitelist.cidr` — IPs/CIDRs that bypass filtering entirely.

### Rules DSL

```
if subject /YOU\s+PERVERT/i {
    reject "Spam: email blackmail rejected";
}

if envelope_from /@yourcompany\.com>/i {
    accept;
}

if subject refile:emoji_regex.inc {
    reject "Emoji not allowed";
}

if subject /invoice/i and body /wire transfer/i {
    reject "Suspicious BEC pattern";
}
```

Conditions: `subject`, `body`, `envelope_from`, `envelope_to` (alias `rcpt_to`), `from`, `to`, `cc`, `reply_to`, `header "Name"`.
Actions: `reject "text";`, `accept;`, `warn "text";`, `dunno;`. Rules run top to bottom; the first `reject`/`accept` stops evaluation. With `add_warn_header = yes` in `mail_filter.conf`, a `warn` also adds an `X-Mail-Filter-Status` header to the delivered message (SpamAssassin-style) — see `mail_filter.conf(5)`.

That header is plain text a mail filter downstream of delivery can act on. For example, a Sieve (Pigeonhole) rule that sends security a copy of anything flagged:

```sieve
if header :contains "X-Mail-Filter-Status" "WARN" {
    redirect :copy "security@your-domain.foo";
}
```

Put `accept` rules for trusted senders (your own domain, transactional ESPs) **before** the `reject` sections.

A `reject`/`warn` message can pull in the regex's own capture groups (`$1`, `$2`, ..., `$0` for the whole match, `$$` for a literal `$`), like Postfix's `pcre_table(5)`:

```
if body /name\s*=\s*"?([^;]*\.(zip|rar|exe))"?/i {
    reject "$1 attachment not allowed";
}
```

Only works directly inside a single regex condition or an `anyof(...)` of them — `allof(...)` and `not(...)` are rejected at load time (ambiguous or nonexistent match). See `mail_filter_rules.conf(5)` for the full rules.

### Testing rules before you rely on them

```sh
mail_filter --configtest
mail_filter --test --subject "You have been hacked" --body "..."
mail_filter --test-eml suspicious_sample.eml
check_regex.py corpus.txt mail_filter_rules.conf --show-found
```

## Reloading and stopping

```sh
service mail_filter reload      # FreeBSD — sends SIGUSR1
systemctl reload mail_filter    # Linux
```

`SIGHUP` is intentionally **not** used for reload — `libmilter` intercepts and terminates on it, so this is why a separate `SIGUSR1` handler exists.

## What it records

The log holds the Subject line and the envelope addresses of every message
the milter sees. How much is recorded is configurable down to a bare
`{qid}` and `{result}`, and nothing is ever sent off the machine. See
[PRIVACY.md](PRIVACY.md).

## License

ISC License — see [LICENSE](LICENSE). Free software, provided "as is," no warranty. Use at your own risk.

## Author

Pavel Hlubík <phlubik@seznam.cz>
