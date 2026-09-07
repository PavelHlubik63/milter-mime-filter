#!/usr/bin/env python3
"""
mail_filter -- A Postfix milter for decoding and filtering email messages.

Evaluates rules with logical conditions (and/or/not, plus the legacy
anyof/allof/not combinators) against the decoded Subject, the message
body, SMTP envelope addresses, and arbitrary headers.

Configuration: /etc/mail_filter/mail_filter.conf (Linux)
               /usr/local/etc/mail_filter/mail_filter.conf (FreeBSD)

This file is intentionally a thin launcher; the implementation lives in
the mail_filter/ package next to it.
"""

from mail_filter.cli import main

if __name__ == "__main__":
    main()
