"""Clock-skew regression check against a running local preview.py (Playwright)."""
from playwright.sync_api import sync_playwright, expect


URL = 'http://127.0.0.1:8789/preview-hidden-path-0123456789'
with sync_playwright() as pw:
    browser = pw.chromium.launch()
    for offset in (0, -7 * 86400, 7 * 86400):
        context = browser.new_context()
        context.add_init_script("""(() => {
            const RealDate = Date;
            window.clockOffset = OFFSET * 1000;
            window.Date = class extends RealDate {
                constructor(...args) { super(...(args.length ? args : [RealDate.now() + window.clockOffset])); }
                static now() { return RealDate.now() + window.clockOffset; }
            };
        })();""".replace('OFFSET', str(offset)))
        page = context.new_page()
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.goto(URL)
        page.locator('#password').fill('preview-password-123')
        page.locator('#login-form button').click()
        expect(page.locator('#state-badge')).to_have_text('Учёт активен')
        expect(page.locator('#pause')).to_be_enabled()
        expect(page.locator('#peak-rx')).not_to_have_text('—')
        page.wait_for_function('chartState && chartState.points.length > 1000')
        assert page.evaluate('Math.abs(chartState.now-statusData.server_time)<10')
        assert page.evaluate('Math.abs(serverNow()-statusData.server_time)<10')
        assert page.evaluate("(() => {const [h,m,s]=document.querySelector('#reset-countdown').textContent.split(':').map(Number);return Math.abs(h*3600+m*60+s-(86400-serverNow()%86400))<3;})()")
        # Changing the workstation clock while the page is open must also be harmless.
        page.evaluate('window.clockOffset += 123456789; refreshStatus()')
        expect(page.locator('#state-badge')).to_have_text('Учёт активен')
        page.locator('#pause').click()
        expect(page.locator('#node-state')).to_have_text('Остановлена', timeout=10000)
        page.locator('#resume').click()
        expect(page.locator('#node-state')).to_have_text('Работает', timeout=10000)
        # A genuinely stale controller still disables management regardless of local time.
        def stale_status(route):
            response = route.fetch()
            data = response.json()
            data['controller_fresh'] = False
            route.fulfill(response=response, json=data)
        page.route('**/api/status', stale_status)
        page.evaluate('refreshStatus()')
        expect(page.locator('#state-badge')).to_have_text('Нет связи')
        expect(page.locator('#pause')).to_be_disabled()
        expect(page.locator('#resume')).to_be_disabled()
        assert not errors, errors
        context.close()
    browser.close()
print('Clock QA passed: normal and +/-7 day clocks, live clock change, chart, countdown, stop/start, stale controller; no JS errors.')
