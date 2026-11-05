"""Exercise real skill and MCP workflows through the browser and configured model."""
import argparse
import json
from pathlib import Path
import time

from playwright.sync_api import sync_playwright


parser = argparse.ArgumentParser()
parser.add_argument('--url', default='http://127.0.0.1:5196/ai')
parser.add_argument('--autonomous', action='store_true')
args = parser.parse_args()
root = Path(__file__).resolve().parents[1]
output = root / 'out/capabilities'
output.mkdir(parents=True, exist_ok=True)


def complete(page, expect_approval):
    deadline = time.monotonic() + 100
    approvals = 0
    while time.monotonic() < deadline:
        if page.locator('.agent-trace--failed').count():
            raise AssertionError(page.locator('.message.bot').last.inner_text())
        buttons = page.locator('.approval-actions button:last-child:enabled')
        if buttons.count():
            assert expect_approval, 'Routine sandbox work unexpectedly requested approval'
            buttons.first.click()
            approvals += 1
        if page.locator('.agent-trace--completed').count():
            assert (approvals > 0) is expect_approval
            return page.locator('.message.bot').last.inner_text()
        page.wait_for_timeout(250)
    raise AssertionError('Run did not finish')


with sync_playwright() as p:
    browser = p.chromium.launch(headless=True, channel='chrome')
    for mode in ('skill', 'mcp'):
        context = browser.new_context(viewport={'width': 1440, 'height': 1000})
        context.add_init_script("localStorage.setItem('chat.agentMode','true')")
        page = context.new_page()
        responses = []
        page.on('response', lambda response: responses.append(response)
                if response.url.endswith('/api/agents/run') else None)
        page.goto(args.url)
        page.wait_for_load_state('networkidle')
        page.locator('.capabilities-panel summary').click()
        if mode == 'skill':
            if args.autonomous:
                assert page.locator('.sandbox-control input').is_checked()
            else:
                page.locator('.sandbox-control input').check()
            page.locator('.cap-install input').set_input_files(str(root / 'examples/skills/csv-summary'))
            row = page.locator('.cap-item').filter(has_text='csv-summary')
            if not args.autonomous:
                row.locator('input[type=checkbox]').check()
            prompt = 'Use a suitable installed skill to summarize the bundled CSV sample. Execute its script and report row count and numeric totals.'
        else:
            row = page.locator('.cap-item').filter(has_text='mcp_local__write_file')
            if not row.count():
                page.locator('.cap-connect input').first.fill('local')
                page.locator('.cap-connect button').click()
            row.locator('input[type=checkbox]').check()
            prompt = 'Use only mcp_local__write_file to write exactly MCP_BROWSER_VERIFIED to browser-verification.txt in the external MCP workspace. Do not use builtin tools. Then confirm completion.'
        page.screenshot(path=str(output / f'{mode}-desktop.png'), full_page=True)
        page.set_viewport_size({'width': 390, 'height': 844})
        page.screenshot(path=str(output / f'{mode}-mobile.png'), full_page=True)
        assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth')
        page.locator('.capabilities-panel summary').click()
        page.locator('textarea').first.fill(prompt)
        page.locator('textarea').first.press('Enter')
        result = complete(page, expect_approval=mode == 'mcp')
        if mode == 'skill':
            assert '78' in result and '15' in result, result
            if args.autonomous:
                assert responses[-1].request.post_data_json['skills'] == []
                events = [json.loads(line[6:]) for line in responses[-1].text().splitlines()
                          if line.startswith('data: ')]
                actions = [e['step']['output'].split(' ', 1)[0] for e in events
                           if e['type'] == 'step' and e['step']['phase'] == 'action']
                assert 'list_skills' in actions and 'load_skill' in actions, actions
                assert any(e['type'] == 'skill_loaded' for e in events)
                print('Verified autonomous skill calls:', actions, flush=True)
        else:
            assert (root / '.agentforge/mcp-workspace/browser-verification.txt').read_text() == 'MCP_BROWSER_VERIFIED'
        page.screenshot(path=str(output / f'{mode}-completed.png'), full_page=True)
        page.reload()
        page.wait_for_load_state('networkidle')
        page.locator('.capabilities-panel summary').click()
        assert page.locator('.cap-row input:checked').count() == (0 if mode == 'skill' and args.autonomous else 1)
        if mode == 'skill' and args.autonomous:
            assert page.locator('.cap-row [role=status]').count() == 1
        context.close()
    browser.close()
print('PASS: real model, skill install/script, MCP discovery/write, approvals, persistence and responsive layout')
