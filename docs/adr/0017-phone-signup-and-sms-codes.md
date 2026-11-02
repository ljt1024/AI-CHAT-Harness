# ADR-0017: Phone-number sign-up and SMS verification codes

Date: 2026-11-02
Status: Accepted
Builds on: ADR-0014 (accounts and ownership), ADR-0015 (anonymous trial)

## Context

ADR-0014 created accounts from `ADMIN_USER`/`ADMIN_PASSWORD` only. There was a
login form but no way for a person to create an account, which is fine for a
single-operator deployment and useless for a product. The requested change was
self-service sign-up bound to a mobile number, with a real verification-code
system rather than a stub.

Two constraints shaped the design. First, the deployment server builds its
virtualenv without a package proxy, so a new dependency for one HTTP call is a
real cost (the same reason passwords use `hashlib.scrypt`). Second, a public
"send me a code" endpoint is an open door: it spends the operator's SMS budget
and can be used to enumerate accounts, so the send side needs its own limits
independent of whether the login succeeds.

## Decision

**There is no separate registration step. Verifying a new number creates the
account.**

- Three endpoints: `POST /api/auth/sms/code` (send), `POST /api/auth/sms/verify`
  (sign in), `POST /api/auth/phone` (bind a number to the signed-in account).
  The first two are in `PUBLIC_API`; binding is not, and answers 401 for a
  guest. Registration and sign-in share the send endpoint on purpose — asking
  whether a number is registered before sending would turn it into an account
  enumeration oracle.
- `purpose` is `login` or `bind`, and a code is scoped to its purpose: a binding
  code cannot sign a different account in. `bind` is refused unless the caller
  has a session, and a number already bound to another account answers 409.
- **Code lifecycle** (`auth.py`, collection `sms_codes`): six random digits,
  stored only as a salted SHA-256 digest, 5-minute `expire_at` enforced by a
  Mongo TTL index, single use, and at most 5 wrong answers before the row is
  deleted and the code is dead. A wrong answer is counted atomically. A send
  writes the row *before* calling the provider so two racing requests cannot both
  pass the cooldown; a failed send rolls back both the row and the daily
  counters, so a provider outage does not lock the person out.
- **Send-side limits**: 60-second cooldown per number+purpose, a per-number daily
  cap (10) and a per-IP daily cap (30) in `sms_sends`, plus `SIGNUP_OPEN=false`
  to refuse unregistered numbers without spending a message. Limits answer 429
  with a `retryAfter` the UI turns into a countdown. The IP comes from
  `X-Forwarded-For` and is a budget key only, never an authorisation.
- **Identity**: `phone.py` normalises `+86 138-0013-8000` to E.164 so one person
  cannot end up with two accounts; a sparse unique index on `users.phone` is the
  race guard. A phone account has no password at all; `authenticate()` still
  spends the same dummy scrypt hash, so timing does not reveal which accounts
  have passwords. `/api/auth/me` and every log line carry only `138****8000`.
  Binding deliberately does not overwrite an existing display name.
- **Transport** (`sms.py`): `SmsSender` implementations for Tencent Cloud
  (`SendSms`, TC3-HMAC-SHA256) and Aliyun (`Dysmsapi`, RPC HMAC-SHA1), both
  signed with the standard library over the existing `httpx` dependency. Both
  clouds answer HTTP 200 with an error inside the body, which `_check` turns
  into `SmsError`. A `console` channel logs the code for development;
  `SMS_DEBUG_ECHO=true` additionally returns it to the browser so the flow can be
  tested without a handset. `build_sender()` returns `None` for a named but
  misconfigured provider — the endpoints then answer 503 rather than quietly
  printing codes to a log while the operator believes messages were delivered.
- **Login CSRF**: ADR-0014's Origin check ran only for requests that already had
  an identity. It now applies to *every* unsafe method, including the anonymous
  code endpoints, so a foreign form cannot sign a visitor into an account the
  attacker controls. All login paths converge on one `finish_login()` helper,
  which also adopts the guest trial transcript (ADR-0015) — a phone sign-up
  keeps the conversation the visitor was already looking at.

## Frontend

- The sign-in dialog defaults to a phone tab and keeps a username/password tab
  for the operator account. It shows the send cooldown, maps error codes
  (`cooldown`, `daily_limit`, `invalid`, `too_many`, `signup_closed`,
  `phone_taken`, `sms_unavailable`) to localised sentences, and displays the
  dev-echo code when present.
- The sidebar account row shows the masked number or a "bind phone" action;
  `AuthContext` gained `signInWithCode` and `refreshUser` so a binding updates
  the displayed identity without a reload.

## Consequences

- The console channel with echo enabled is equivalent to disabling
  authentication for any number. It is warned about loudly at startup, gated on
  `SMS_PROVIDER=console`, and must never be enabled on a public deployment;
  production needs the five cloud credentials in `.env` (see `.env.example`).
- A phone number is now an identity, so losing control of a number means losing
  the account. There is no recovery path yet, and nothing prevents a carrier
  from reassigning one — a future ADR should add a second factor or a recovery
  code for accounts holding valuable data.
- Number normalisation is mainland-China-only (11 digits, `1[3-9]…`). Overseas
  numbers, landlines and short codes are refused at the schema/validation edge
  rather than being half-supported.
- Code rows live in Mongo with a TTL index; a deployment whose Mongo has no TTL
  monitor would accumulate expired rows. They are never trusted past
  `expire_at` in code, so the TTL is cleanup, not the security boundary.
- Password accounts are unchanged and can bind a number afterwards; the two
  login paths are not merged at the storage level.
