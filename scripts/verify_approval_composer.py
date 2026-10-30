"""UI regression against tests.browser_approval_app, not the user's live service."""
import argparse
from pathlib import Path

from playwright.sync_api import expect, sync_playwright


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:5198/ai")
    args = parser.parse_args()
    output = Path(__file__).resolve().parents[1] / "out/approval-composer"
    output.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, channel="chrome")
        for scenario in ("approve", "deny", "error", "stop", "timeout", "queue", "dark"):
            context = browser.new_context(viewport={"width": 1440, "height": 1000})
            context.add_init_script("localStorage.setItem('chat.agentMode', 'true')")
            if scenario == "dark":
                context.add_init_script("localStorage.setItem('theme', 'dark')")
            page = context.new_page()
            page.goto(args.url)
            page.wait_for_load_state("networkidle")
            page.locator("textarea").fill(f"测试确认流程 {scenario}")
            page.locator("textarea").press("Enter")
            card = page.locator(".approval-composer .approval-card")
            card.wait_for()
            assert page.locator("textarea").count() == 0
            assert page.locator(".chat-workspace > .approval-card").count() == 0
            assert page.locator(".typing-indicator").count() == 0
            assert card.evaluate("node => document.activeElement === node")
            # Long parameters must scroll, never push the decision buttons off screen.
            assert card.locator("pre").evaluate("node => node.scrollHeight > node.clientHeight")
            for width, height, label in ((1440, 1000, "desktop"), (390, 844, "mobile")):
                page.set_viewport_size({"width": width, "height": height})
                bounds = card.bounding_box()
                assert bounds and bounds["y"] > height * 0.4
                assert bounds["y"] + bounds["height"] <= height
                assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
                if scenario in ("approve", "dark"):
                    page.screenshot(path=str(output / f"pending-{scenario}-{label}.png"), full_page=True)
            if scenario == "error":
                page.route("**/api/approvals/*", lambda route: route.fulfill(
                    status=503, content_type="application/json", body='{"detail":"暂时无法提交，请重试"}'), times=1)
                card.get_by_role("button", name="批准执行", exact=True).click()
                expect(card.get_by_role("alert")).to_have_text("暂时无法提交，请重试")
                assert card.get_by_role("button", name="批准执行", exact=True).is_enabled()
                page.screenshot(path=str(output / "retry-mobile.png"), full_page=True)
            if scenario == "stop":
                card.get_by_role("button", name="停止生成", exact=True).click()
                expect(page.locator(".agent-trace--cancelled")).to_be_visible()
            elif scenario == "timeout":
                page.locator(".agent-trace--completed").wait_for(timeout=20000)
                expect(page.locator(".approval-history")).to_contain_text("已拒绝")
            else:
                card.get_by_role("button", name="拒绝执行" if scenario == "deny" else "批准执行", exact=True).click()
                if scenario == "queue":
                    expect(card.locator("pre")).to_contain_text("result-1.txt")
                    assert page.locator(".approval-history .approval-card").count() == 1
                    card.get_by_role("button", name="拒绝执行", exact=True).click()
                page.locator(".agent-trace--completed").wait_for(timeout=10000)
                expect(page.locator(".approval-history")).to_contain_text("已拒绝" if scenario in ("deny", "queue") else "已批准")
            expect(page.locator(".approval-composer")).to_have_count(0)
            expect(page.locator("textarea")).to_be_enabled()
            expect(page.locator("textarea")).to_be_focused()
            assert page.locator(".approval-actions button").count() == 0
            page.locator("textarea").fill("可以继续提问")
            assert page.locator("textarea").input_value() == "可以继续提问"
            print("PASS", scenario, flush=True)
            context.close()
        browser.close()
    print("PASS: composer approval, allow/deny/retry/stop/timeout/queue, focus, long arguments, desktop/mobile")


if __name__ == "__main__":
    main()
