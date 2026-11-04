# ADR-0023: The pdf.js worker is addressed by version, not by content hash

Date: 2026-11-04
Status: Accepted
Refines: ADR-0022 (pdf.js draws the pages), ADR-0021 (the header fix that started it)
Related: ADR-0012 (workspace writes and HTML delivery), ADR-0020 (conversation file system)

## Context

ADR-0022 brought pdf.js back and Vite fingerprinted its worker:
`assets/pdf.worker.min-Dswkl-cV.mjs`, served `immutable` for 30 days because
everything under `/assets/` is.

The first night that URL was wrong. nginx did not know `.mjs`, answered
`application/octet-stream`, and a module script served as an octet stream is
refused — `Setting up fake worker failed`. The type was fixed within the hour.
The URL was not, and could not be: a content hash follows the file's **bytes**,
and the bytes had not changed, only the header that carried them. So the
corrected response went out under a URL that every affected browser already had
stored, with `immutable` forbidding even a revalidation. Their copy stayed
`octet-stream`, and the preview kept failing for a month — a reload, a hard
reload, a cleared *site data* entry all did nothing, because the browser was
never asked.

A `?rev=2` query got that round through. Then ADR-0021 removed pdf.js, ADR-0022
brought it back, and the worker returned to the plain fingerprinted path. The
same failure reappeared on the same evening it shipped: the file on the server
was 200 `application/javascript`, 1,265,413 bytes, correct to the last character,
and a clean browser loaded it without complaint — while the reader's browser
served itself the poisoned copy from cache. That is the whole trap: **a hash is a
cache key, and a header fix changes no bytes.**

## Decision

**The worker is served from `public/pdfjs/` like the CMap tables, and its URL
carries the pdf.js version as a query string:**

```
/pdfjs/pdf.worker.min.mjs?v=6.3.289
```

`vite.config.ts` copies `build/pdf.worker.min.mjs` into `public/pdfjs` alongside
the fonts, cmaps and wasm decoders, and injects `__PDFJS_VERSION__` from
`node_modules/pdfjs-dist/package.json`. `PdfPreview.tsx` builds the URL from
those two. Vite no longer emits the worker as a hashed asset.

The version query is a better key than the content hash ever was:

* It changes exactly when the library changes, which is the only time the worker
  must change — pdf.js refuses a worker from another version, so a stale one is
  not a subtle bug but a hard failure.
* It changes when we need it to, which a hash never does: any future fix to how
  the worker is *served* can bump the query without pretending the file differs.
* The URL is ours to write, so it can be reasoned about. A path that Vite chooses
  cannot be corrected by anyone who does not control the build.

nginx follows the same logic, in both server blocks:

* `location ~ \.mjs$` keeps `default_type application/javascript` and keeps the
  30-day `immutable` — legitimate now that the URL is versioned. Its comment says
  never to point at an `.mjs` here without a version query.
* `location /pdfjs/` serves the unversioned assets (cmaps, fonts, wasm) with
  `expires 7d` and `try_files $uri =404`. Seven days so a library upgrade heals
  itself; a hard 404 so a missing table cannot fall back to `index.html` and hand
  pdf.js an HTML page to parse.
* `location = /index.html` answers `Cache-Control: no-cache`. The entry document
  is what names the hashed chunks; served from cache, a deploy reaches nobody.

## Consequences

* **No one has to clear their cache.** The fix reaches a browser that holds the
  poisoned copy because the chunk it loads now asks for a different URL.
* A pdf.js upgrade is one `npm install` and a rebuild: the query moves with the
  version, the old worker is orphaned, the new one is fetched. Nobody has to
  remember a manual revision number.
* One more file lives outside the asset pipeline, so one more thing can be
  forgotten by a deploy — already covered: the platform ships `dist/` whole, and
  the README says so.
* The rule this repository keeps relearning is now written down twice, in the
  README and in `NOTES.md`: **fixing a response header means changing a URL.**
  A cache entry is `(URL, headers, body)`; a hash protects the body and says
  nothing about the other two.
* Not fixed by this: a client that cached the *HTML document* still needs one
  reload to pick up the new chunk names. `no-cache` on `/index.html` makes that
  reload sufficient rather than optional, which is the best a static deploy can
  promise.
