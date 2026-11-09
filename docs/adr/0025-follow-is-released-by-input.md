# ADR-0025: The follow is released by input, not by scroll distance

Date: 2026-11-09
Status: Accepted
Related: ADR-0020 (conversation file system), ADR-0024 (web tools expand what a step can be)

## Context

`useStickToBottom` pinned the transcript to the bottom while it grew, and
decided when to stop by measuring the distance after every `scroll` event:

```ts
const onScroll = () => { stickRef.current = distanceToBottom() <= 72 }
```

That reads correctly and is wrong, because pinning the container also fires
`scroll` — and the event is dispatched after the next frame, not at the moment
`scrollTop` was assigned. Between the two, React renders whatever the stream
just produced.

For plain chat that gap is a few characters. For an agent run it is a tool step
card, an artifact row, or a markdown table — hundreds of pixels at once. The
event then arrived with a distance well past 72px, the hook concluded the
reader had scrolled away, and the follow switched off for good. A 38-step run
ended with the view stranded mid-transcript, and the way back was missing too:
the jump button only recomputed on `scroll`, so a transcript that grew without
scrolling never turned it on. Reproduced by appending 400px to the message list
while pinned — `gap` went 0 → 400 → 800, button stayed absent.

## Decision

The follow is released by **input**, never by distance:

- `wheel` with `deltaY < 0`, `touchmove`, an upward key (`ArrowUp`, `PageUp`,
  `Home`) inside the container, or a `pointerdown` on the scrollbar strip.
- `scroll` never clears the follow. It either re-arms it (`distance <= 72`) or,
  if the follow is still armed, treats the event as our own pin arriving late
  and pins again.
- A `ResizeObserver` on the content box pins after height changes that never
  produce a message — a table reflowing when the preview pane narrows, an image
  landing, a step group expanding.
- The jump button is recomputed on content change as well as on scroll,
  appearing at 2× the stick threshold so a deliberate detour earns it and a
  nudge does not.

## Consequences

Scrolling down is not input: a reader can never reach the bottom while the
stream grows unless the follow pins them there, so a downward wheel must not
release anything. Conversely a programmatic scroll can no longer be mistaken
for a reader's decision, which was the whole failure.

Cost: the hook now owns five listeners plus two observers, and it must not
assume `firstElementChild` is stable — the welcome panel is replaced by the
message list, so the resize target is rebound from a `MutationObserver`.

The threshold pair (72px stick, 144px button) is the contract the UI reads;
changing one without the other brings back the gap where the view has visibly
left the bottom but offers no way back.
