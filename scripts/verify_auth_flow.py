"""Browser check of the sign-in gate, session persistence and sign-out.

Runs against a live frontend and backend:

    cd harness && ADMIN_USER=admin ADMIN_PASSWORD=dev-password-123 \\
        PYTHONPATH=src .venv/bin/python -m uvicorn agentforge.server:app --port 8000
    npm run dev
    /usr/bin/python3 scripts/verify_auth_flow.py --password dev-password-123

Screenshots land in harness/out/auth/ (ignored by git).
"""

import argparse
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
SHOTS = ROOT / "harness" / "out" / "auth"


def shot(page, name):
    try:
        page.screenshot(path=str(SHOTS / f"{name}.png"), timeout=4000)
    except Exception as exc:  # remote fonts can stall a full-page capture
        print(f"  (screenshot {name} skipped: {type(exc).__name__})")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:5174/ai")
    parser.add_argument("--user", default="admin")
    parser.add_argument("--password", default="dev-password-123")
    args = parser.parse_args()
    SHOTS.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as playwright:
        # The host exports HTTP_PROXY, and without this the browser proxies even
        # 127.0.0.1 and every navigation times out.
        browser = playwright.chromium.launch(channel="chrome", args=["--no-proxy-server"])
        page = browser.new_page(viewport={"width": 1440, "height": 1000})
        page.goto(args.url, wait_until="commit")

        page.wait_for_selector(".login-card", timeout=20000)
        assert page.locator(".chat-container").count() == 0, "chat must not render before sign-in"
        shot(page, "01-login")
        print("login form shown, chat not rendered")

        fields = page.locator(".login-field input")
        fields.nth(0).fill(args.user)
        fields.nth(1).fill("definitely-not-the-password")
        page.locator(".login-submit").click()
        page.wait_for_selector(".login-error", timeout=10000)
        assert "Wrong username or password" in page.locator(".login-error").inner_text()
        shot(page, "02-wrong-password")
        print("wrong password rejected in place")

        fields.nth(1).fill(args.password)
        page.locator(".login-submit").click()
        page.wait_for_selector(".chat-container", timeout=20000)
        page.wait_for_selector(".sidebar-account", timeout=20000)
        assert args.user in page.locator(".sidebar-account").inner_text()
        shot(page, "03-signed-in")
        print("signed in, sidebar shows the account")

        # A reload must keep the session: the cookie is HttpOnly and server-side.
        page.reload(wait_until="commit")
        page.wait_for_selector(".chat-container", timeout=20000)
        assert page.locator(".login-card").count() == 0, "reload must not ask again"
        print("session survives a reload")

        page.locator(".sidebar-signout").click()
        page.wait_for_selector(".login-card", timeout=20000)
        assert page.locator(".chat-container").count() == 0
        shot(page, "04-signed-out")
        print("sign-out returns to the login form")

        # And the cookie really is gone: a reload stays on the login form.
        page.reload(wait_until="commit")
        page.wait_for_selector(".login-card", timeout=20000)
        print("reload after sign-out stays signed out")

        mobile = browser.new_page(viewport={"width": 390, "height": 844})
        mobile.goto(args.url, wait_until="commit")
        mobile.wait_for_selector(".login-card", timeout=20000)
        shot(mobile, "05-mobile-login")
        print("mobile layout renders the login form")

        browser.close()
    print("OK")


if __name__ == "__main__":
    sys.exit(main())
