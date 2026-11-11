# 0031: A shelf the reader has seen is never emptied by a refresh

Status: Accepted

Context: the tool marketplace answers two requests — the catalogue and this
account's installations — and repaints from whatever arrives. Three separate
reports of "the list was there, then it was gone" traced back to that repaint
rather than to the data:

- A remount (tab switch, a session re-read, an error boundary reset) rebuilt the
  component with empty arrays and read the cached answer in an effect. An effect
  runs after the first paint, so the remount rendered an empty shelf for one
  commit — 「正在加载……」 and no cards — even though the answer was already in
  hand. `.market-card` nodes were absent from the DOM during that window.
- `load` awaited the catalogue and "my services" as a pair (`Promise.all`).
  "My services" runs a live discovery per installed row, which can take the full
  `MCP_DISCOVER_TIMEOUT` (30 s) and fails outright when one installed service is
  unreachable. A slow or broken row therefore held back — or threw away — a
  catalogue that had answered in milliseconds, which in the UI is a full shelf
  turning into nothing.
- A refresh that answered "no services at all" replaced the rows on screen. The
  catalogue is almost never empty in a running deployment, so that answer is
  usually a half-started server; the reader saw an empty page and no reason.

Decisions:
- **Seed the first render, not the first effect.** The last answer for the
  current account is held at module scope and read inside the `useState`
  initialisers, with `sessionStorage` as the fallback for a page that just
  loaded. A remount paints the catalogue and the installations in the same frame
  it appears; the refetch then corrects it silently. Only a change of account
  clears the rows.
- **Settle the two requests separately.** `Promise.allSettled`; the catalogue is
  applied as soon as it answers, and a failure in either half is reported without
  cancelling the other. "Loaded" is set when at least one half answered, so two
  failures leave the panel saying it is fetching next to the reason, instead of
  implying the deployment offers nothing.
- **Never empty a full shelf.** An empty catalogue answer while rows are on
  screen keeps the rows and says so (「这次刷新没有取到服务列表，下面仍是上一次
  的结果。」) rather than repainting as empty. The next non-empty answer clears
  the notice; the retry button is already next to it when the request failed.
- **Depend on the account id, not the `user` object.** A session re-read hands
  back a fresh object with the same id; that used to re-run the fetch for no
  reason, which is how a harmless re-render became a network round trip and a
  repaint that could blank the page.

Consequences:
- A stale shelf is possible in exactly one case: the operator really removed
  every entry, and the reader sees the previous list with the notice until the
  next answer arrives or the page reloads. Saying so is preferable to a page that
  blinks between "14 services" and "nothing here" while the operator edits a file.
- Nothing in the browser can now turn a loaded market back into an empty one
  except a sign-out or a switch to an account that has no cached answer — both of
  which mean the rows are genuinely no longer this reader's.
- The module-level answer is per tab and dies with it; `sessionStorage` still
  covers a reload, and remains keyed by account so two logins never share a shelf.
