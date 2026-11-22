"""The apps shipped with the robot (mac_server/assets/apps), as they ship: each is a page the builder would accept
(the scan, then one run in a real Chrome, its own tests included), asks for its manette, keeps the robot silent, and
keeps its French signs with their words. Between them they ask for every body a manette lends; the bench asks for
whichever profile the human picks in it, the ear among them."""

from __future__ import annotations

import re
from html.parser import HTMLParser
from typing import get_args

import pytest

from mac_server import app_build, app_check, apps
from mac_server.contracts import ManetteProfile

ASSETS = apps.ASSETS  # where they ship: read before conftest.py points the Mac at an empty directory
EVERY = '*'  # the bench: each profile of the toolbox's own list, as the human picks it
PROFILES = {  # the manette each one asks for at « Jouer »
    'slalom': 'volant',
    'vol-de-nuit': 'tete',
    'tam-tam': 'gachettes',
    'marionnette': 'tete',
    'manhattan': 'tete',
    'avant-la-nuit': 'paume',
    'bille-en-tete': 'tete',
    'banc-de-manette': EVERY,
    'jardin-abyssal': 'tete',
}
BROKEN = re.compile(r'« | »| [:;!?](?=\s|$|[»"\')])')  # tests/mac/test_french.py


def shipped(key: str) -> str:
    return (ASSETS / f'{key}.html').read_text(encoding='utf-8')


class _Words(HTMLParser):
    """What a page says before any script runs: its text, outside its style and its scripts."""

    def __init__(self) -> None:
        super().__init__()
        self.said: list[str] = []
        self._code = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._code += tag in ('script', 'style')

    def handle_endtag(self, tag: str) -> None:
        self._code -= tag in ('script', 'style')

    def handle_data(self, data: str) -> None:
        if not self._code and data.strip():
            self.said.append(data.strip())


def test_each_shipped_app_has_its_page_and_each_page_its_app() -> None:
    assert sorted(path.stem for path in ASSETS.glob('*.html')) == sorted(apps.BUILTINS)
    assert sorted(PROFILES) == sorted(apps.BUILTINS)
    assert set(PROFILES.values()) - {EVERY} <= set(get_args(ManetteProfile))
    assert {'gachettes', 'volant', 'paume', 'tete'} <= set(PROFILES.values())


@pytest.mark.parametrize('key', list(apps.BUILTINS))
def test_a_shipped_page_is_one_the_builder_would_accept(key: str) -> None:
    page = shipped(key)
    assert app_build.check(page) == []
    assert app_build.extract(page) == page.strip()  # whole, nothing around it
    assert app_build.title_of(page) == apps.BUILTINS[key][0]
    assert app_build.quiet_of(page)  # a game played in front of the robot
    if PROFILES[key] == EVERY:
        assert 'reachy.PROFILS' in page and 'reachy.lancer(profil,' in page
    else:
        assert f"const PROFIL = '{PROFILES[key]}';" in page  # the manette it asks for at « Jouer »
        assert 'reachy.lancer(PROFIL' in page  # through the toolbox every page is served with (assets/kit.js)
    assert 'postMessage' not in page  # and no channel of its own: a change of protocol is made in the toolbox
    assert 'window.__selfTest' in page


@pytest.mark.usefixtures('chrome')
@pytest.mark.parametrize('key', list(apps.BUILTINS))
def test_a_shipped_page_runs_in_chrome_and_passes_its_own_tests(key: str) -> None:
    assert app_check.run(shipped(key), app_build.CSP, kit=app_build.kit()) == []


# The toolbox's canvas in three frames: one the page gave no size (it fills its parent), one the page sized (kept),
# one in a frame with nothing to measure when the toolbox gets it, as in the dashboard's frame (no size either: it
# fills its parent at the first frame; the check's Chrome renders one).
CANVASES = """<!DOCTYPE html><html lang="fr"><head><meta charset="utf-8"><title>Toile</title><style>
html, body { height: 100%; margin: 0; }
body { display: flex; flex-direction: column; }
#cadre { flex: 1 1 auto; min-height: 0; }
#boite { width: 300px; height: 200px; }
#fixe { width: 200px; height: 100px; }
#tard { width: 120px; height: 80px; display: none; }
</style></head><body>
<div id="cadre"><canvas id="libre"></canvas></div>
<div id="boite"><canvas id="fixe"></canvas></div>
<div id="tard"><canvas id="cache"></canvas></div>
<script>
const $ = (id) => document.getElementById(id);
const libre = reachy.toile($('libre')), fixe = reachy.toile($('fixe')), cache = reachy.toile($('cache'));
$('tard').style.display = 'block';
window.__selfTest = () => {
  const cadre = $('cadre');
  return [
    ['le cadre a sa taille', +(cadre.clientWidth > 300 && cadre.clientHeight > 300), 1, 0],
    ['sans taille : la largeur du cadre', $('libre').clientWidth, cadre.clientWidth, 0],
    ['sans taille : la hauteur du cadre', $('libre').clientHeight, cadre.clientHeight, 0],
    ['sans taille : ses pixels', $('libre').width, Math.round(cadre.clientWidth * libre.dpr), 1],
    ['sans taille : ce que la toile rend', libre.hauteur, cadre.clientHeight, 0],
    ['dimensionnée par la page : sa largeur', $('fixe').clientWidth, 200, 0],
    ['dimensionnée par la page : sa hauteur', $('fixe').clientHeight, 100, 0],
    ['dimensionnée par la page : ce que la toile rend', fixe.largeur, 200, 0],
    ['cachée puis montrée : la largeur du cadre', $('cache').clientWidth, 120, 0],
    ['cachée puis montrée : la hauteur du cadre', $('cache').clientHeight, 80, 0],
    ['cachée puis montrée : ce que la toile rend', cache.largeur, 120, 0],
  ];
};
</script></body></html>"""


@pytest.mark.usefixtures('chrome')
def test_the_toolbox_canvas_fills_its_parent_unless_the_page_sized_it() -> None:
    assert app_check.run(CANVASES, app_build.CSP, kit=app_build.kit()) == []


@pytest.mark.parametrize('key', list(apps.BUILTINS))
def test_a_shipped_page_keeps_its_french_words_with_their_signs(key: str) -> None:
    words = _Words()
    words.feed(shipped(key))
    assert len(words.said) > 10
    assert [text for text in words.said if BROKEN.search(text)] == []
