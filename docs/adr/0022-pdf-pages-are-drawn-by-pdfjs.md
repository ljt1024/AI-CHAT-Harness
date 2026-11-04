# ADR-0022: PDF pages are drawn by pdf.js, inside our own panel

Date: 2026-11-04
Status: Accepted
Supersedes: ADR-0021 (the browser's viewer)
Refined by: ADR-0023 (the worker's URL, which this one got wrong)
Related: ADR-0012 (workspace writes and HTML delivery), ADR-0020 (conversation file system)

## Context

ADR-0021 handed PDFs to the browser's own viewer because pdf.js had drawn a
blank page for a Chinese report. That decision fixed the blank page and created
a different one: the preview panel is 680px wide and the native viewer brings its
own grey chrome — a toolbar, a sidebar button, a dark-mode skin — that no CSS in
this application can reach. Next to the Excel and Word previews, which use the
panel's own header, colours and radius, a PDF looked like a different
application pasted into a sidebar. On iOS Safari it is worse: an embedded PDF
shows the first page and nothing else.

The blank page was never a limit of pdf.js. It was a missing `cMapUrl`. The
report sets its Chinese in `STSong-Light`, a CID font it does not embed, and
pdf.js needs the Adobe CMap tables to turn character ids into glyphs. Ship those
tables and the same file renders.

## Decision

**PDFs are parsed and drawn by pdf.js (`react-pdf`), with a 42px toolbar of our
own inside the preview panel.**

The gap ADR-0021 was afraid of is now closed by configuration rather than by
hoping: `cMapUrl`, `standardFontDataUrl` and `wasmUrl` point at
`/pdfjs/{cmaps,standard_fonts,wasm}/`, which `vite.config.ts` copies out of
`node_modules/pdfjs-dist` at startup — 4.3 MB of static files, gitignored, fetched
on demand. The three-page report that started all this pulls exactly two of them,
`UniGB-UCS2-H.bcmap` and `Adobe-GB1-UCS2.bcmap`, both 200, and renders all three
pages with selectable text.

What the panel keeps: the same header, the same select and download row, the same
border and radius as every other preview; a page counter that follows the scroll,
`‹ n / N ›`, zoom in 10% steps, and a click on the percentage to fit the width
again. Every page is drawn at once and they scroll as one column.

What ADR-0021 decided still holds: `GET /api/files/{id}/view` is the only route
that serves a stored file `inline`, still `application/pdf` only, still behind the
same owner-and-session locks; `download` still answers `attachment`. The escape
hatch is explicit now — if pdf.js cannot parse a file, the panel says so, offers a
retry, and links to that same URL so the browser's viewer can try.

## Consequences

* Appearance is ours again: one toolbar in the panel's own idiom, identical in
  dark mode, and the same on every browser instead of whichever skin that browser
  ships.
* Cost is deferred, not paid up front: `react-pdf` + `pdfjs-dist` are a separate
  chunk (212 KB gzip) loaded the first time a PDF is opened, and the worker is a
  1.26 MB asset fetched only then. The main bundle is untouched.
* Every page of a PDF is rendered immediately. That is right for the artifacts
  this platform produces — reports, a handful of pages — and wrong for a
  100-page upload; if that ever appears, page rendering has to become windowed
  rather than the whole document being dropped back to the native viewer.
* pdf.js 6 needs `Promise.withResolvers` (Chrome 119+, Safari 17.4+, Firefox
  121+). Older browsers land on the parse error and its open-in-new-tab link
  rather than a blank panel.
* We are maintaining a renderer again: a worker URL, an asset copy step, a text
  layer, and a ninth thing that can regress silently. The difference from before
  ADR-0021 is that the assets are configured in one place, and the failure is
  visible: a page that does not draw now raises `onRenderError` instead of leaving
  white paper.
