# What mail_filter records

The record holds the Subject line and the envelope addresses of every
message the milter sees, and it only becomes useful once it covers weeks or
months of traffic. Enable it if your site's policy on retaining message
metadata allows it. On most mail servers a record of this kind already
exists for operational reasons — answering "did they really send it, and
when" long after the fact — and this one is no different in kind. What it is
kept for, how it is protected and how long it lives are decisions for the
site, not for this software.

## Where it is written

Two places, configured separately in `mail_filter.conf`:

- **`logfile`** — the application log, `/var/log/mail_filter.log` by default.
  At `loglevel = INFO` it records every message that passes through, with
  the decoded Subject and the envelope addresses. At `WARNING` it records
  only the mail that was rejected or warned about. `DEBUG` adds the decoding
  steps and every rule tested.
- **`[mail_log]`** — an optional structured record written through
  `syslog(3)` with facility `mail`, so it lands wherever the system sends
  mail logs. Disabled by default, and independent of `loglevel`.

Nothing else is written anywhere. mail_filter keeps no database, no queue
and no state between messages, and never sends anything off the machine.

## Recording less

Neither switch is all-or-nothing.

The `format` line under `[mail_log]` decides field by field what the record
contains, and every field in it is optional. A record of `{qid}` and
`{result}` alone says that a message was filtered without saying anything
about who sent what to whom. See `mail_filter.conf(5)` for the full field
list.

For the application log, `loglevel = WARNING` narrows it to the mail that
was acted on. `log_match_max_chars` caps how much of the matched text is
quoted in that line.

Rotation is not mail_filter's business: on FreeBSD the package installs
`/etc/newsyslog.conf.d/mail_filter.conf`, on Debian `/etc/logrotate.d/mail_filter`,
and both are ordinary configuration you can change.

## A note on the delivered message

`add_warn_header = yes` adds an `X-Mail-Filter-Status` header to messages a
`warn` rule matched. That header travels with the message: it is visible to
the recipient, and to anything the message is later forwarded to. It is off
by default because it changes delivered mail rather than only the log.
