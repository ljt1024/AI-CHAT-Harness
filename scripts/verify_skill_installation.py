"""Real model -> installation approval -> public GitHub import -> live skill catalog."""
import argparse
import json
from pathlib import Path
import time
from uuid import uuid4

from playwright.sync_api import sync_playwright


parser = argparse.ArgumentParser()
parser.add_argument('--url', default='http://127.0.0.1:5196/ai')
args = parser.parse_args()
output = Path(__file__).resolve().parents[1] / 'out/skill-installation'
output.mkdir(parents=True, exist_ok=True)

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True, channel='chrome')
    for approved in (False, True):
        context = browser.new_context(viewport={'width': 1440, 'height': 1000}, base_url=args.url)
        context.add_init_script("localStorage.setItem('chat.agentMode','true')")
        page = context.new_page()
        responses = []
        page.on('response', lambda response: responses.append(response)
                if response.url.endswith('/api/agents/run') else None)
        page.goto(args.url)
        page.wait_for_load_state('networkidle')
        name = 'github-verify-' + uuid4().hex[:8]
        prompt = (f'Install the skill from https://github.com/anthropics/skills/tree/main/skills/frontend-design '
                  f'with the name {name}. Use install_skill source github. Install only, do not load or execute. '
                  'Do not create substitute inline content. If approval is denied, stop and report it.')
        page.locator('textarea').first.fill(prompt)
        page.locator('textarea').first.press('Enter')
        buttons = page.locator('.approval-actions button')
        buttons.first.wait_for(timeout=60000)
        assert page.locator('.approval-card code').first.inner_text() == 'install_skill'
        before = page.request.get('/api/skills').json()['data']
        assert not any(skill['name'] == name for skill in before)
        page.screenshot(path=str(output / f'approval-{approved}-desktop.png'), full_page=True)
        page.set_viewport_size({'width': 390, 'height': 844})
        page.screenshot(path=str(output / f'approval-{approved}-mobile.png'), full_page=True)
        assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth')
        buttons.nth(1 if approved else 0).click()
        deadline = time.monotonic() + 100
        while time.monotonic() < deadline:
            if page.locator('.agent-trace--failed').count():
                raise AssertionError(page.locator('.message.bot').last.inner_text())
            if page.locator('.agent-trace--completed').count():
                break
            assert not page.locator('.approval-actions button:enabled').count(), 'Unexpected repeated approval'
            page.wait_for_timeout(250)
        else:
            raise AssertionError('Installation run timed out')
        catalog = page.request.get('/api/skills').json()['data']
        installed = [skill for skill in catalog if skill['name'] == name]
        assert bool(installed) is approved
        events = [json.loads(line[6:]) for line in responses[-1].text().splitlines() if line.startswith('data: ')]
        assert any(e['type'] == 'skill_installed' for e in events) is approved
        if approved:
            assert len(installed[0]['provenance']['commit']) == 40
            assert installed[0]['provenance']['source'] == 'github'
            assert 'SKILL.md' in installed[0]['files']
            page.locator('.capabilities-panel summary').click()
            row = page.locator('.cap-item').filter(has_text=name)
            row.wait_for()
            row.scroll_into_view_if_needed()
            composer = page.locator('.input-area').bounding_box()
            assert composer['y'] >= 0 and composer['y'] + composer['height'] <= 845
            page.screenshot(path=str(output / 'installed-catalog-mobile.png'), full_page=True)
            # Remove only this script's own verification package after validating live refresh.
            assert page.request.delete('/api/skills/' + installed[0]['id']).ok
        print(f'PASS: real-model GitHub install approved={approved}, catalog mutation={bool(installed)}', flush=True)
        context.close()
    browser.close()
