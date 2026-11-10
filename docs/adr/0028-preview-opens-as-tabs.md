# ADR-0028: A preview opens as a tab, not as an overlay

Date: 2026-11-10
Status: Accepted
Related: ADR-0022 / ADR-0023 (pdf.js preview), ADR-0025 (follow is released by input)

## Context

The preview panel started as one slot holding one file. Opening a second file
replaced the first, and the only way back to the first was a `<select>` at the
top of the panel — a control that hides everything it can offer behind a click.
A run that produces `report.html`, `style.css` and `data.html` reads as "one
report and two attachments you cannot compare".

Below 1100px the panel was worse than a one-slot viewer: it was an absolutely
positioned overlay with `.messages-content` set to `visibility: hidden`. Opening
a file to check one number blanked the conversation that produced it, and the
only way out was the × — losing the file you were reading. The reader who asked
for this change was on that side of the breakpoint, which is why they described
the current behaviour as "covering".

## Decision

The panel holds a **set** of opened files, one tab each.

- `openedFiles` in `chat/index.tsx` keeps `{ sessionId, tabs, activeId }`.
  Opening a file that already has a tab focuses it instead of stacking a second
  copy, because the same report is linked from several messages.
- Closing a tab closes that file only. Closing the active one hands the body to
  its right neighbour, or to the last tab when it was at the end; the panel
  only disappears when the last tab goes.
- The strip's `+` lists files the run produced but that have no tab, so a closed
  file stays reachable without hunting through the transcript.

Below 1100px the preview becomes **the workspace's other view** rather than an
overlay. `.chat-panels` turns into a column, the strip is lifted to the top of
it carrying an extra 「聊天」 tab, and the chat is `display: none` rather than
hidden-but-present. Switching views is one click in the same strip, in both
directions; nothing is ever covered.

Two copies of the strip therefore exist — one inside the pane (wide layout), one
above the panels (narrow layout) — and CSS shows exactly one of them at a time.
Duplicating the element is cheaper than re-parenting it on resize, which would
unmount the preview body and re-parse a PDF on every breakpoint crossing.

## Consequences

Auto-open stays wide-only (`canAutoOpenPreview`, ≥1101px). On narrow widths a
file arriving mid-answer would swap the view out from under someone still
reading it, and the tab is one click away anyway.

Switching tabs remounts the body (the panel is keyed by `fileId`), so a PDF is
parsed again when you come back to it. Keeping per-tab scroll and page position
would mean either one live instance per tab — several pdf.js parsers resident —
or caching blobs; neither is worth it until someone switches files often enough
to notice. Drafts are unaffected: their `fileId` is stable, so streaming rewrites
the same tab without remounting.

The `<select>` is gone. It was the only way to reach a file that had never been
opened, which the `+` menu now covers; the transcript's artifact cards remain the
primary entry point.
