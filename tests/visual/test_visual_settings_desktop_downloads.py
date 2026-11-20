"""The shipped desktop download links keep their full content inside the card."""
from __future__ import annotations

import pytest

from tests._jsdom import require_shipped_graph

pytestmark = pytest.mark.visual


@pytest.mark.parametrize(('lang', 'recommend', 'download'), [
    ('zh', '推荐', '下载'),
    ('fr', 'Recommandé', 'Télécharger'),
])
def test_macos_download_labels_and_actions_fit(browser, live_server, tmp_path, lang, recommend, download):
    desktop_url = 'https://example.invalid/desktop'
    installers = [
        {'os': 'macos', 'arch': arch, 'label': f'macOS {arch} DMG',
         'version': '0.19.2', 'size': size * 1048576, 'url': f'{desktop_url}/{arch}.dmg'}
        for arch, size in [('arm64', 182), ('x86_64', 193)]
    ]
    require_shipped_graph()
    with browser.new_context() as context:
        context.add_init_script(f"""localStorage.setItem('tofu_ui_lang', '{lang}');
          localStorage.setItem('tofu_onboarding_v1_done', '1');
          Object.defineProperty(navigator, 'userAgentData', {{
            value: {{getHighEntropyValues: async () => ({{architecture: 'arm'}})}}
          }});""")
        page = context.new_page()
        page.route('**/api/v4/desktop/status', lambda route: route.fulfill(json={
            'data': {'downloads': installers, 'download_url': desktop_url}, 'meta': {},
        }))
        page.set_viewport_size({'width': 880, 'height': 920})
        page.goto(live_server)
        page.wait_for_function('window.TofuModules?.version === 3')
        page.evaluate("() => window.TofuModules.invokeFeature('openSettings', [], () => {})")
        card = page.locator('#settingsDesktopCard')
        links = card.locator('a[href$=".dmg"]')
        links.last.wait_for(state='visible', timeout=10000)
        assert links.count() == 2
        assert card.locator(f'a[href="{desktop_url}"]').is_visible()
        assert links.first.get_by_text(recommend, exact=True).is_visible()
        for link, installer, size in zip(links.all(), installers, (182, 193)):
            assert link.get_attribute('href') == installer['url']
            assert link.get_by_text(installer['label'], exact=True).is_visible()
            assert link.get_by_text(f'v0.19.2 · {size} MB', exact=True).is_visible()
            assert link.get_by_text(download, exact=True).is_visible()

        page.evaluate('async () => { await document.fonts.ready; }')
        for width in (880, 650, 390):
            page.set_viewport_size({'width': width, 'height': 920})
            geometry = links.evaluate_all('''links => links.map(link => {
              const inside = (inner, outer) => inner.left >= outer.left - 1 &&
                inner.right <= outer.right + 1 && inner.top >= outer.top - 1 &&
                inner.bottom <= outer.bottom + 1;
              const bounds = link.getBoundingClientRect();
              const card = link.closest('#settingsDesktopCard').getBoundingClientRect();
              const overflowing = [...link.querySelectorAll('svg, span')].flatMap(element => {
                const range = document.createRange();
                range.selectNodeContents(element);
                return [...element.getClientRects(), ...range.getClientRects()]
                  .some(rect => !inside(rect, bounds)) ? [element.textContent || 'icon'] : [];
              });
              const fitsCard = inside(bounds, card) && bounds.left >= 0 && bounds.right <= innerWidth;
              return {label: link.textContent, fitsCard, overflowing};
            })''')
            assert len(geometry) == 2
            assert all(item['fitsCard'] and not item['overflowing'] for item in geometry), (
                lang, width, geometry)
            if width in (880, 390):
                card.screenshot(path=tmp_path / f'desktop-downloads-{lang}-{width}.png')
