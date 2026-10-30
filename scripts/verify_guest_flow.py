"""Browser check of the anonymous trial: walk in, run out, sign in, keep the log.

Needs a live frontend and backend, as described in verify_auth_flow.py. A guest
cookie whose budget is already spent can be supplied with --guest so the dialog
path is reachable without burning four real model calls:

    /usr/bin/python3 scripts/verify_guest_flow.py --password dev-password-123 \
        --guest guest_xxx

Screenshots land in harness/out/guest/ (ignored by git).
"""

import argparse
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
SHOTS = ROOT / "harness" / "out" / "guest"


def shot(page, name):
    try:
        page.screenshot(path=str(SHOTS / f"{name}.png"), timeout=4000)
    except Exception as exc:  # remote fonts can stall a full-page capture
        print(f"  (screenshot {name} skipped: {type(exc).__name__})")


def text(locator):
    return locator.inner_text().replace("\n", " | ")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:5174/ai")
    parser.add_argument("--user", default="admin")
    parser.add_argument("--password", default="dev-password-123")
    parser.add_argument("--guest", default="", help="a guest id with no turns left")
    args = parser.parse_args()
    SHOTS.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel="chrome", args=["--no-proxy-server"])
        context = browser.new_context(viewport={"width": 1440, "height": 950})
        if args.guest:
            context.add_cookies([{"name": "agentforge_guest", "value": args.guest,
                                  "domain": "127.0.0.1", "path": "/"}])
        page = context.new_page()
        failures = []
        page.on("pageerror", lambda error: failures.append(str(error)[:200]))

        # 1. No session, no cookie: the product itself, not a login page.
        page.goto(args.url, wait_until="commit")
        page.wait_for_selector(".input-area textarea", timeout=25000)
        assert page.locator(".login-card").count() == 0, "a visitor was walled out"
        print("1 ok  entry:", text(page.locator(".sidebar-account")))
        shot(page, "01-anonymous-entry")

        # 2. Sending with no budget opens the dialog and keeps the draft.
        box = page.locator(".input-area textarea").first
        box.fill("我还想再试一次")
        page.locator(".input-area button[type=submit]").first.click()
        page.wait_for_selector(".login-dialog", timeout=10000)
        print("2 ok  dialog:", text(page.locator("#login-dialog-title")),
              "| draft kept:", repr(box.input_value()))
        assert box.input_value().strip(), "the draft must survive the dialog"
        shot(page, "02-limit-dialog")

        # 3. Signing in closes the dialog and puts the account in the sidebar.
        page.locator(".login-dialog input").nth(0).fill(args.user)
        page.locator(".login-dialog input").nth(1).fill(args.password)
        page.locator(".login-dialog .login-submit").click()
        page.wait_for_selector(".login-dialog", state="detached", timeout=15000)
        page.wait_for_timeout(800)
        print("3 ok  signed in:", text(page.locator(".sidebar-account")))
        shot(page, "03-signed-in")

        print("page errors:", failures or "none")
        browser.close()


if __name__ == "__main__":
    main()
