# Security policy

## Reporting a vulnerability

Please report privately, not in a public issue:

- **GitHub:** [Report a vulnerability](https://github.com/PavelHlubik63/milter-mime-filter/security/advisories/new)
- **E-mail:** phlubik@seznam.cz

Include the mail_filter version, the platform, and — most usefully — the
message or the rule that triggers it. A `.eml` file that reproduces the
problem is worth more than a description of it; `mail_filter --test-eml`
takes one directly.

This is a spare-time project maintained by one person. Expect an
acknowledgement within a week. If a report needs a fix, the fix and the
advisory go out together.

## Supported versions

The latest release. There is no separate maintenance branch: fixes go to
`main` and are published as a release.

## What is in scope

mail_filter sits between Postfix and every message the server accepts, and
it runs as a single process shared by all concurrent SMTP connections. That
shapes what matters here:

- **A crafted message that stops the milter or makes it hang.** Postfix then
  falls back to `milter_default_action` — commonly `accept`, which lets mail
  through unfiltered, or `tempfail`, which stops mail flowing at all. Either
  is a real outage, so a regex that backtracks catastrophically on a
  constructed body is a security bug, not just a performance one.
- **Evading the decoder.** The point of this milter is that rules match the
  decoded Subject and the unfolded body rather than the wire format. An
  encoding, charset or MIME construction that gets a Subject past a rule
  which should have matched it defeats that purpose, and is in scope.
- **Anything a rules file can do that its author did not intend** —
  `refile:` path handling, backreference substitution into reject messages,
  and the rest of the rule language.
- **The packaging**, on either platform: file ownership and permissions,
  what an upgrade or a purge touches, what the postinstall scripts write.

## What is not in scope

- Spam that gets through because no rule covers it. That is a rule you have
  not written yet, not a defect in the program.
- False positives from rules you wrote or generated. `check_regex.py`
  exists to find those before deployment; see
  `mail_filter_rules.conf(5)`.
- Anything requiring write access to `/etc/mail_filter` or
  `/usr/local/etc/mail_filter`. Whoever can edit the rules already decides
  what the milter does.
