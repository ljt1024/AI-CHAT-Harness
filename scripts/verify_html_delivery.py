"""Real-model browser check: Chinese request -> Docker writes -> preview/download/reload."""
import argparse
import json
from pathlib import Path

from playwright.sync_api import sync_playwright


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--sandbox-checks", action="store_true")
    args = parser.parse_args()
    output = Path(__file__).resolve().parents[1] / "out/html-delivery"
    output.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, channel="chrome")
        page = browser.new_page(viewport={"width": 1440, "height": 1000}, accept_downloads=True)
        responses = []
        page.on("response", lambda response: responses.append(response)
                if response.url.endswith("/api/agents/run") else None)
        page.goto("http://127.0.0.1:5196/ai")
        page.wait_for_load_state("networkidle")
        page.evaluate("""() => {
            localStorage.setItem('chat.agentMode', 'true');
            localStorage.setItem('isNewCov', 'true');
        }""")
        page.reload()
        page.wait_for_load_state("networkidle")
        assert page.locator(".sandbox-control input").is_checked()
        checks = ("写完后必须用 run_command 执行 Python 标准库检查：确认三个文件存在且 HTML "
                  "引用了 style.css 和 app.js。不要安装或加载技能，不要运行 Node 或浏览器。"
                  if args.sandbox_checks else "请直接用 write_file 写文件，不运行命令，不安装技能，不需要额外验证。")
        page.locator("textarea").first.fill(
            "生成一个简单的中文 HTML 计数器页面，标题为‘点击计数’，初始数字为0，"
            "有一个‘加一’按钮。使用三个独立文件 index.html、style.css、app.js。"
            + checks)
        page.locator("textarea").first.press("Enter")
        page.locator(".agent-artifact").first.wait_for(timeout=240000)
        assert page.locator(".approval-card").count() == 0
        page.locator(".agent-trace--completed").wait_for(timeout=10000)
        events = [json.loads(line[6:]) for line in responses[-1].text().splitlines()
                  if line.startswith("data: ")]
        assert not any(event["type"] == "await_approval" for event in events)
        if args.sandbox_checks:
            commands = [event for event in events if event["type"] == "step"
                        and event.get("call", {}).get("name") == "run_command"]
            assert commands, "Model did not run the requested sandbox checks"
            print("SANDBOX COMMANDS", len(commands))
        page.locator(".artifact-html-frame").wait_for()
        frame = page.frame_locator(".artifact-html-frame")
        frame.get_by_text("点击计数", exact=True).first.wait_for()
        # The app preview must stay script-free, even for an interactive download.
        assert frame.locator("script").count() == 0
        page.screenshot(path=str(output / "desktop.png"), full_page=True)
        with page.expect_download() as download_info:
            page.locator(".artifact-preview-toolbar a").click()
        download = download_info.value
        path = output / download.suggested_filename
        download.save_as(path)
        html = path.read_text()
        assert "<style" in html and "<script" in html
        assert 'src="app.js"' not in html and 'href="style.css"' not in html
        # The locally opened download must actually run its bundled JavaScript.
        delivered = browser.new_page()
        errors = []
        delivered.on("pageerror", lambda error: errors.append(str(error)))
        delivered.goto(path.as_uri())
        delivered.get_by_role("button", name="加一", exact=True).click()
        assert "1" in delivered.locator("body").inner_text()
        assert not errors, errors
        delivered.close()
        page.reload()
        page.wait_for_load_state("networkidle")
        page.locator(".agent-artifact").first.wait_for()
        page.locator(".agent-artifact button").first.click()
        page.frame_locator(".artifact-html-frame").get_by_text("点击计数", exact=True).first.wait_for()
        page.set_viewport_size({"width": 390, "height": 844})
        page.screenshot(path=str(output / "mobile.png"), full_page=True)
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        conversations = page.evaluate("JSON.parse(localStorage.getItem('messages'))")
        assistant = conversations[-1]["data"][-1]
        assert assistant["artifacts"] and assistant["agentStatus"] == "completed"
        print("PASS: real model, Docker, bundled download/interaction, script-free preview, reload, desktop/mobile")
        print("ARTIFACT", assistant["artifacts"][0]["downloadPath"])
        print("OUTPUT", output)
        browser.close()


if __name__ == "__main__":
    main()
