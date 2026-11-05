# ADR-0018: Email sign-up replaces the phone door

Date: 2026-11-03
Status: Accepted
Supersedes in part: ADR-0017 (phone sign-up and SMS codes)
Builds on: ADR-0014 (accounts and ownership), ADR-0015 (anonymous trial)

## Context

ADR-0017 shipped sign-up by mobile number with a real verification-code system.
Two things made it a poor fit for this stage of the product:

- **It cannot be exercised locally.** A Chinese SMS provider needs a reviewed
  signature and template before a single message leaves, so a developer with no
  account can only test the console channel — a code printed into a log is not
  the flow the user sees.
- **The provider is a procurement step.** Whether codes arrive is decided by an
  approval queue outside the repository, which makes the auth path the part of
  the product nobody can demo on a fresh machine.

Email has none of that. SMTP is a protocol every mailbox speaks, the standard
library can sign nothing, and a local sink accepts mail with no account at all.
The verification-code lifecycle from ADR-0017 — hashed, single use, expiring,
rate-limited, killed after five wrong answers — is exactly what email needs, so
the change is a new identifier and a new transport, not a new subsystem.

## Decision

**Email is the default door; verifying a new address creates the account.**

- `POST /api/auth/email/code`, `POST /api/auth/email/verify`, and the
  signed-in-only `POST /api/auth/email` for binding. The first two join
  `PUBLIC_API`; binding is refused by the middleware for anyone without a
  session. An unknown address becomes an account unless `SIGNUP_OPEN=false`,
  in which case the send endpoint refuses before spending a message.
- **One code lifecycle for every channel.** `verification_codes` stores
  `identifier`, `channel`, `purpose`, a salted SHA-256 digest, an attempt count
  and an `expire_at`. The identifier is an email address or a phone number;
  `channel` keeps a code issued for one from answering the other. Everything
  else — five-minute TTL row, five wrong answers kill it, per-address cooldown,
  per-address and per-IP daily budgets in separate `mail_sends` / `sms_sends`
  counters, rollback when the provider refuses — is shared.
- **A consumed code is marked, not deleted.** ADR-0017 deleted the row, which
  also deleted the cooldown record: a successful sign-in could immediately ask
  for another message. `consumed_at` keeps the row until the TTL index removes
  it, so the cooldown covers the whole window and verification only matches a
  row that has not been used.
- **Transport**: `mail.py` speaks SMTP through `smtplib` (STARTTLS 587, implicit
  TLS 465, or plain for a local sink) in a worker thread, and a console channel
  logs the code for development. `MAIL_DEBUG_ECHO=true` returns it to the
  browser, only on the console channel. A named-but-misconfigured provider makes
  `build_mailer()` return `None` and the endpoints answer 503 — the same
  fail-loud rule as SMS, because printing codes into a log while the operator
  believes mail is going out is worse than a visible outage.
- **Identity**: `email_address.py` lower-cases and shape-checks the address
  before it reaches the database, `users.email` has a sparse unique index, and
  the username is derived (`e` + a digest prefix) because this door has no field
  to choose one. The display name is the mask (`a***@example.com`), which is
  also all `/api/auth/me`, the sidebar and every log line ever see. Binding an
  address another account owns answers 409; binding never renames an account.
- **The SMS endpoints stay** (`/api/auth/sms/*`, `/api/auth/phone`) as a backend
  capability, but the interface no longer offers a phone tab. Someone who wants
  it back has the whole implementation behind one endpoint.

## Frontend

- The sign-in dialog defaults to an email tab, keeps the username/password tab
  for the account `bootstrap()` creates, and reuses the countdown, error-code
  mapping and development echo from `verification.tsx` (the SMS-specific module
  it replaced).
- The bind dialog is now `EmailBindDialog`; the sidebar shows the masked address
  and offers "bind email" when the account has none.

## Consequences

- **Accounts created through the phone door have no entry point in the
  interface.** Their endpoint still works, but a person cannot reach it from the
  UI. On a fresh deployment this is empty; on an existing one it needs a
  migration story (bind an address, then sign in with it) before this ADR ships
  to real users. Recorded in NOTES Backlog rather than solved here.
- Deliverability is now the operator's problem: SPF, DKIM and a sane `From`
  address decide whether the code reaches an inbox or a spam folder, and a
  provider that throttles silently looks exactly like a wrong address. The
  endpoint reports `send_failed`, but it cannot tell the user *why* the mailbox
  did not receive it. On the consumer providers this ADR was built against, the
  address is not negotiable either: QQ and 163 answer 553 when `From` differs
  from the authenticated account, so a separate `SMTP_FROM_NAME` is the only
  freedom left, and moving off a personal mailbox means moving to another
  account rather than relabelling this one.
- An address can be reassigned or forwarded, so it is a weaker identity than a
  phone number for account recovery. There is still no second factor and no
  recovery code; that remains a future ADR.
- The per-address and per-IP counters live in Mongo and are per-process for the
  in-memory password-failure tracker, so a multi-instance deployment wants a
  shared rate-limit store. `SIGNUP_OPEN=false` remains the blunt instrument for
  closing the door entirely.
- The daily budget is charged per channel, so switching a deployment from mail
  to SMS does not inherit yesterday's count. That is deliberate: the two have
  different costs.
