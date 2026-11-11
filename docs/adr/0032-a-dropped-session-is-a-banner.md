# 0032 — A dropped session is a banner, not an eviction

Date: 2026-11-11
Status: Accepted

## Context

The marketplace body was rendered as `{user && <>…</>}`. The catalogue it shows
is public: `/api/mcp/catalog` answers without an account. Only the writes —
install, enable, remove — need one. Tying the whole body to `user` therefore did
more than it said: `user` goes `null` on **any** single 401 (`AuthProvider`
listens on `UNAUTHORIZED_EVENT`), so one background read that came back 401 —
an expired cookie, a restarted backend with a fresh signing key, a stale install
click — took the catalogue off the screen. Visiting the page again re-read the
session, found it healthy, and drew the list, which is why the report was
"refresh and it is there".

Two smaller holes sat on the same path:

- the account-change branch read the answer back from `sessionStorage` only. A
  profile that cannot write storage (private mode, full quota) fell through to
  `setCatalog([])` even though the answer was still in memory, one line above.
- `load` treated a fulfilled request as an array. A 200 that is not one — a
  proxy's error page, a half-deployed server — threw outside every `catch`, so
  the promise rejected quietly and the panel kept whatever it had.

## Decision

1. **What is on screen outlives the session.** The body renders when there is an
   account *or* a catalogue to show. A dropped session puts the sign-in sentence
   above the shelf; it does not remove the shelf. Writes still ask for an
   account, and the manual registration form stays behind `user` because it
   would only earn a 401.
2. **An answer already in memory beats a copy of it.** The account-change branch
   falls back to the in-memory answer before it clears anything.
3. **"Nothing" is not an answer worth remembering.** The cache write skips an
   empty catalogue when the account already had a non-empty one, and an effect
   restores the in-memory answer if the state is ever emptied while that answer
   exists. This is deliberately blunt: several unrelated paths end in the same
   empty screen and the reader cannot tell them apart.
4. **Trust the type, not the status.** A fulfilled request whose value is not an
   array is read as "no rows", which keeps the shelf and reports.

## Consequences

A guest who returns to a warm page sees the catalogue instead of a sentence.
That is allowed — the catalogue is public — and the install button already asks
for an account. A genuine removal of every catalogue entry no longer repaints
during the same visit; it lands on reload, which is a fair price for never
blanking a shelf that was full a second ago.

The page also carries a development-only readout (`目录 / 显示 / DOM / 网格 /
首卡 / 块 / 滚 / 登录`) written through a ref rather than state, because "the list
disappeared" has four causes that look identical on screen and only one of them
is this component's.
