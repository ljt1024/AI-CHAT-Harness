"""Browser integration check against the deterministic WriteThenFinish test backend."""
from pathlib import Path

from playwright.sync_api import sync_playwright

output = Path('out/browser-approvals')
output.mkdir(parents=True, exist_ok=True)
with sync_playwright() as p:
    browser = p.chromium.launch(headless=True, channel='chrome')
    for approved in (True, False):
        context = browser.new_context(viewport={'width': 1440, 'height': 1000})
        context.add_init_script("localStorage.setItem('chat.agentMode', 'true')")
        page = context.new_page()
        page.goto('http://127.0.0.1:5197/ai')
        page.wait_for_load_state('networkidle')
        page.locator('.sandbox-control input').check()
        page.locator('textarea').first.fill('Write result')
        page.locator('textarea').first.press('Enter')
        card = page.locator('.approval-card')
        card.wait_for()
        page.screenshot(path=str(output / f'desktop-{approved}.png'), full_page=True)
        page.set_viewport_size({'width': 390, 'height': 844})
        page.screenshot(path=str(output / f'mobile-{approved}.png'), full_page=True)
        assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth')
        card.locator('button').nth(1 if approved else 0).click()
        page.get_by_text('Task finished.', exact=False).first.wait_for()
        assert card.locator('button').count() == 0
        page.reload()
        page.wait_for_load_state('networkidle')
        assert page.locator('.sandbox-control input').is_checked()
        context.close()
    browser.close()
print('PASS: browser approve/reject, sandbox persistence, desktop/mobile layout')
