"""Browser smoke check against the explicitly opt-in protocol stub."""
import argparse
from pathlib import Path

from playwright.sync_api import sync_playwright

parser = argparse.ArgumentParser()
parser.add_argument("--url", default="http://127.0.0.1:5186")
parser.add_argument("--output", default="out/browser")
parser.add_argument("--channel", default=None)
args = parser.parse_args()
output = Path(args.output)
output.mkdir(parents=True, exist_ok=True)

with sync_playwright() as playwright:
    browser = playwright.chromium.launch(headless=True, channel=args.channel)
    page = browser.new_page(viewport={"width": 1440, "height": 1000})
    page.add_init_script("localStorage.setItem('chat.agentMode', 'true')")
    page.goto(args.url)
    page.wait_for_load_state("networkidle")
    field = page.locator("textarea").first
    field.fill("Verify the AgentForge connection")
    field.press("Enter")
    page.get_by_text("AgentForge protocol connection verified.", exact=False).first.wait_for()
    page.screenshot(path=str(output / "desktop.png"), full_page=True, animations="disabled")
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    page.set_viewport_size({"width": 390, "height": 844})
    page.screenshot(path=str(output / "mobile.png"), full_page=True, animations="disabled")
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    browser.close()
print("PASS: browser -> Vite proxy -> Harness SSE -> rendered answer, desktop and mobile")
