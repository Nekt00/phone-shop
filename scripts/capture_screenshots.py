"""Capture the local demo only. Requires an already migrated, seeded SQLite database."""
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')

import django
django.setup()

from django.contrib.auth import get_user_model
from django.test import Client
from playwright.sync_api import sync_playwright


def main():
    from django.conf import settings
    if settings.DATABASES['default']['ENGINE'] != 'django.db.backends.sqlite3':
        raise SystemExit('Screenshots require a separate local SQLite demo database.')
    user, _ = get_user_model().objects.get_or_create(username='portfolio_preview')
    user.set_unusable_password()
    user.save()
    client = Client()
    client.force_login(user)
    directory = Path(__file__).resolve().parents[1] / 'docs/screenshots'
    directory.mkdir(parents=True, exist_ok=True)
    errors = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        context = browser.new_context(viewport={'width': 1440, 'height': 1000}, device_scale_factor=1, reduced_motion='reduce')
        page = context.new_page()
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.goto('http://127.0.0.1:8011/login/', wait_until='networkidle')
        page.screenshot(path=str(directory / 'login.png'), full_page=True)
        context.add_cookies([{'name': settings.SESSION_COOKIE_NAME, 'value': client.cookies[settings.SESSION_COOKIE_NAME].value,
                             'domain': '127.0.0.1', 'path': '/'}])
        pages = [('dashboard', '/operators/'), ('import', '/operators/upload/'), ('catalog', '/operators/upload/?mode=manual'),
                 ('comparison', '/operators/comparison/'), ('telegram-price', '/operators/telegram-price/'),
                 ('requests', '/operators/telegram-requests/')]
        for name, path in pages:
            response = page.goto('http://127.0.0.1:8011' + path, wait_until='networkidle')
            assert response.status == 200, (name, response.status)
            for close in page.locator('.live-toast__close').all():
                close.click()
            if name in {'import', 'catalog', 'telegram-price'}:
                buttons = page.locator('.mode-switch .btn, .builder-action-btn').evaluate_all('''buttons => buttons.map(button => ({
                    height: button.getBoundingClientRect().height,
                    gap: parseFloat(getComputedStyle(button).columnGap)
                }))''')
                assert all(35 <= button['height'] <= 38 and button['gap'] >= 6 for button in buttons), buttons
            if name == 'requests':
                page.locator('.telegram-request-accordion summary').first.click()
                field = page.locator('textarea[name="reply_text"]').first
                field.fill('Здравствуйте! Проверяем наличие на складе.')
                page.wait_for_timeout(5200)
                assert field.input_value() == 'Здравствуйте! Проверяем наличие на складе.', 'Draft was reset by live refresh'
            if name == 'telegram-price':
                heights = page.locator('.builder-action-btn').evaluate_all('(buttons) => buttons.map(button => button.getBoundingClientRect().height)')
                assert all(30 <= height <= 38 for height in heights), heights
                page.evaluate('generateTelegramText()')
                text = page.locator('#telegramOutput').input_value()
                assert 'Galaxy S25' in text and '17 Pro Max' in text, text
                page.evaluate("window.scrollTo({top: document.getElementById('telegramOutputCard').getBoundingClientRect().top + scrollY - 80, behavior: 'instant'})")
                page.screenshot(path=str(directory / 'telegram-output.png'), full_page=False)
                page.evaluate('window.scrollTo(0, 0)')
            page.screenshot(path=str(directory / f'{name}.png'), full_page=False)
            print(f'OK {name}')
        page.set_viewport_size({'width': 390, 'height': 844})
        page.goto('http://127.0.0.1:8011/operators/', wait_until='networkidle')
        for close in page.locator('.live-toast__close').all():
            close.click()
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), 'Mobile page overflow'
        page.screenshot(path=str(directory / 'mobile.png'), full_page=True)
        page.goto('http://127.0.0.1:8011/operators/telegram-price/', wait_until='networkidle')
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), 'Mobile builder overflow'
        for close in page.locator('.live-toast__close').all():
            close.click()
        page.locator('.builder-settings-toolbar').scroll_into_view_if_needed()
        page.screenshot(path=str(directory / 'telegram-mobile.png'))
        assert not errors, errors
        browser.close()


if __name__ == '__main__':
    main()
