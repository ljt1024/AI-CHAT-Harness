# ADR-0021: The PDF viewer is the browser's, not ours

Date: 2026-11-04
Status: Accepted
Supersedes: the canvas renderer introduced with the artifact preview panel
Related: ADR-0012 (workspace writes and HTML delivery), ADR-0020 (account scope)

## Context

A PDF artifact was previewed by drawing it ourselves: `pdfjs-dist`, a module
worker, one `<canvas>` per page, and a small toolbar of our own. It worked on
the PDFs we happened to test with — which were written by our own generator and
used base-14 fonts — and produced a **blank page** for the first PDF a reader
actually cared about: a three-page Chinese report from ReportLab.

The file was not broken. Chrome's own viewer drew all three pages, with the
Chinese text, from the same bytes.

The reason is a gap in what we shipped, not a bug in pdf.js: that document sets
its Chinese text in **STSong-Light, a CID font it does not embed** (`FontFile`
appears zero times in the file). Drawing those glyphs requires the CMap tables
that map character ids to Unicode — `GB1-EUC-H` and friends — which pdf.js
fetches from a `cMapUrl` we never configured. Without them it silently draws
nothing: no exception, no error state, just white. Embedding the font is the
document's job and we do not control the documents readers bring.

Two ways to fix it:

* Ship the CMap tables and the standard-14 font data (~3 MB of `.bcmap` files
  copied out of `pdfjs-dist`), set `cMapUrl` / `standardFontDataUrl`, and keep
  maintaining a renderer, a worker, a page cache and a toolbar. The renderer
  would then handle the fonts we remembered to ship and stay silent about the
  rest — forms, annotations, scanned images with unusual filters, and any
  future gap of the same shape.
* Ask the browser to draw it.

## Decision

**A PDF is served `inline` and rendered by the browser's own PDF viewer.**

A new endpoint, `GET /api/files/{file_id}/view`, is the one place a file may be
rendered as a document of its own, and it serves **only** `application/pdf`:
anything else answers `415` rather than quietly falling back to an attachment
under a different promise. The panel embeds it in an `<object>` with an
`<iframe>` inside for a browser whose `object` path fails.

`GET /api/files/{file_id}/download` is unchanged and still answers `attachment`
for every file. The two doors differ only in `Content-Disposition`, which is
exactly the difference that decides whether a browser shows a PDF or saves it.

`pdfjs-dist` is removed. What went with it, all of which had already produced
one incident each: the 1.26 MB worker asset, its `.mjs` Content-Type in nginx,
its `immutable` cache entry that outlived the header fix (and the `?rev=`
cache-buster added to work around it), and 96 KB of gzip in the main bundle.

## Consequences

* Arbitrary PDFs render as well as the reader's browser can render PDFs —
  unembedded CJK fonts, forms, annotations, scans. We no longer decide which
  documents are previewable.
* The preview looks like the browser: its own toolbar, its own zoom, its own
  dark-mode chrome. We gave up the page/zoom controls we used to draw. On iOS
  Safari an embedded PDF shows the first page only, which is worse than a
  canvas for a long document and better than a white page for every document.
* One more endpoint has to hold the same two locks as the download path
  (owner scope, then the file's session), and it is the only route in the
  codebase that serves a stored file with `inline`. A future file type must not
  be added to it without thinking about what rendering a document of unknown
  origin on our own origin means.
* The old failure mode is gone in a way that cannot regress silently: there is
  no font, cmap or worker to forget to ship. If a PDF does not render now, the
  browser declined it, and the panel's fallback offers the download.
