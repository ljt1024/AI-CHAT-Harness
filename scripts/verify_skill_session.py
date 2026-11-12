"""Regression for selecting a skill from an existing host conversation."""
import json
from pathlib import Path

from playwright.sync_api import sync_playwright


with sync_playwright() as p:
    browser = p.chromium.launch(headless=True, channel='chrome')
    page = browser.new_page(viewport={'width': 390, 'height': 844})
    page.goto('http://127.0.0.1:5196/ai')
    page.wait_for_load_state('networkidle')
    page.evaluate('''() => {
      localStorage.setItem('chat.agentMode', 'true');
      localStorage.setItem('isNewCov', 'false');
      localStorage.setItem('selectCovId', 'host-regression');
      localStorage.setItem('messages', JSON.stringify([{covId:'host-regression',
        modelId:'deepseek-chat', data:[{content:'Preserve this conversation',role:'user',
        isBot:false,timestamp:'2026-10-20T00:00:00Z'}]}]));
    }''')
    page.reload()
    page.wait_for_load_state('networkidle')
    sandbox = page.locator('.sandbox-control input')
    assert sandbox.is_disabled() and not sandbox.is_checked()
    page.locator('.capabilities-panel summary').click()
    skill = page.locator('.cap-item').filter(has_text='csv-summary').first.locator('input')
    assert skill.is_enabled()
    skill.click()
    page.locator('.dialog-overlay').wait_for()
    page.locator('.dialog-button.cancel').click()
    assert page.evaluate("localStorage.getItem('selectCovId')") == 'host-regression'
    assert not skill.is_checked()
    skill.click()
    page.locator('.dialog-button.confirm').click()
    new_id = page.evaluate("localStorage.getItem('selectCovId')")
    assert new_id != 'host-regression'
    assert sandbox.is_checked()
    assert page.locator('.message.user').count() == 0
    page.reload()
    page.wait_for_load_state('networkidle')
    assert sandbox.is_checked()
    page.locator('.capabilities-panel summary').click()
    assert page.locator('.cap-row input:checked').count() == 1
    page.locator('.capabilities-panel summary').click()
    requests = []

    def respond(route):
        body = route.request.post_data_json
        requests.append(body)
        events = [{'type': 'start', 'sessionId': body['sessionId'], 'memoryMessages': 0},
                  {'type': 'done', 'result': {'output': 'UI_VERIFIED', 'steps': []}}]
        route.fulfill(status=200, content_type='text/event-stream',
                      body=''.join(f'data: {json.dumps(e)}\n\n' for e in events))

    page.route('**/api/agents/run', respond)
    page.locator('textarea').first.fill('Verify skill routing')
    page.locator('textarea').first.press('Enter')
    page.locator('.agent-trace--completed').wait_for()
    assert requests[0]['sessionId'] == new_id
    assert requests[0]['sandbox'] is True and len(requests[0]['skills']) == 1
    conversations = page.evaluate("JSON.parse(localStorage.getItem('messages'))")
    assert len(conversations) == 2
    old = next(item for item in conversations if item['covId'] == 'host-regression')
    assert old['data'][0]['content'] == 'Preserve this conversation'
    output = Path(__file__).resolve().parents[1] / 'out/capabilities'
    output.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(output / 'host-to-skill.png'), full_page=True)
    browser.close()
print('PASS: cancel/confirm, old conversation preserved, sandbox and skill persisted, no duplicate draft')
