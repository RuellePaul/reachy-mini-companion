"""An app's page: what Claude is asked (a creation writes the whole page, a retouch edits a copy of it with Claude
Code's own Read and Edit tools), how the page is checked (the scan, then one run in headless Chrome:
mac_server/app_check.py), one repair round, and the build's progress as Claude works (the `app_build` channel,
latest only). Blocking for seconds to minutes: a build runs in its own thread."""

from __future__ import annotations

import html
import json
import logging
import re
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import get_args

from mac_server import app_check, claude
from mac_server.claude import Cancelled, ClaudeError
from mac_server.contracts import APP_TITLE_MAX, AppBuild, AppEdit, ManetteProfile
from robot_agent import animation_names as anim

log = logging.getLogger(__name__)

# The model and the effort are settings (Opus at high by default); a retouch edits a copy
# with the same ones, in seconds whatever the effort (10 s at medium as at high).
CREATE_TIMEOUT_S, RETOUCH_TIMEOUT_S = 1200.0, 600.0
PAGE_MAX = 400_000  # characters; a generated game is 25-35 KB
PUBLISH_S = 0.25  # the progress goes out at most this often, the latest only
TAIL_LINES, TAIL_MAX = 40, 3000  # the code being written, as shown
EDITS_SHOWN, EDIT_MAX = 5, 1500

CSP = (
    "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src data: blob:; "
    "media-src data: blob:; font-src data:; connect-src 'none'"
)
KIT = Path(__file__).resolve().parent / 'assets' / 'kit.js'

# What the sandbox or the CSP forbids, as a page would name it: calls, APIs, and any address outside the page.
FORBIDDEN = re.compile(
    r'(?:(?<![\w$.])|\b(?:window|self|globalThis)\s*\.\s*)(?P<call>fetch|alert|confirm|prompt)\s*\(|\[\s*[\'"`](?P<key>fetch)'
    r'|\b(?P<api>XMLHttpRequest|WebSocket|EventSource|(?:webkit|moz)?RTCPeerConnection|RTCDataChannel|WebTransport'
    r'|sendBeacon|localStorage|sessionStorage|indexedDB|document\s*\.\s*cookie|window\s*\.\s*open)\b'
    r'|(?i:(?:\b(?:src|href|action|poster)\s*=\s*[\'"`]?\s*|url\(\s*[\'"]?\s*|@import\s+[\'"]?)(?:https?:)?//)'
)
# A page that reloads or leaves: the dashboard cuts the bridge as the frame's document goes (AppFrame).
# A `location` declared (`let location`), compared, or an arrow's parameter is the page's own.
LEAVING = re.compile(
    r'(?:(?<![\w$.])(?<!let )(?<!const )(?<!var )|\b(?:window|document|self|globalThis)\s*\.\s*)(?P<way>'
    r'location(?:\s*\.\s*(?:reload|replace|assign)\b'
    r'|(?:\s*\.\s*(?:href|search|pathname|host|hostname|port|protocol))?\s*=(?![=>]))'
    r'|history\s*\.\s*(?:go|back|forward)\b)'
)


# A page's own channel to the dashboard: the toolbox is the only one (assets/kit.js, served before the page).
OWN_CHANNEL = re.compile(r'\bpostMessage\b')


def _listed(names: dict[str, tuple[str, str]]) -> str:
    return ', '.join(f'{key} ({label.lower()})' for key, (label, _) in names.items())


PROMPT = f"""Tu codes de petites applications web pour un robot de bureau, un Reachy Mini. Quelqu'un lui a demandé un jeu, une animation ou un petit outil, et il te transmet la demande. Ta page s'affiche dans le tableau de bord du robot, sous les yeux de cette personne, qui attend : va droit au but.

=== CE QUE TU RENDS ===
- Un seul fichier HTML complet et autonome, de `<!DOCTYPE html>` à `</html>` : `<html lang="fr">`, `<meta charset="utf-8">`, `<meta name="viewport" content="width=device-width, initial-scale=1">`, un `<title>` de deux à cinq mots (le nom de l'app), tout le CSS dans un `<style>`, tout le JavaScript dans un `<script>` en ligne.
- Réponds uniquement par ce fichier : aucun texte avant ni après, pas de bloc markdown.

=== OÙ LA PAGE TOURNE (contraintes strictes) ===
- Dans une iframe `sandbox="allow-scripts"` (sans allow-same-origin), sous cette CSP : `{CSP}`
- Donc aucune ressource externe : pas de `<script src>`, pas de CDN, pas de police web, aucune image ni aucun lien vers une adresse http(s). Polices système seulement (`system-ui, -apple-system, "Segoe UI", Roboto, sans-serif`). Les images se dessinent en SVG, en canvas ou en `data:`. Le son passe par WebAudio, lancé par un clic.
- Aucune requête réseau : fetch, XMLHttpRequest, WebSocket, EventSource, sendBeacon, RTCPeerConnection et WebTransport sont bloqués et ne doivent apparaître nulle part dans le code, pas même en commentaire.
- localStorage, sessionStorage, indexedDB et document.cookie lèvent une exception dans cette sandbox : ne les nomme nulle part ; l'état reste en mémoire.
- alert, confirm, prompt, window.open, l'envoi de formulaires, les téléchargements et les liens vers une autre page sont bloqués : n'en mets aucun, les messages s'affichent dans la page. eval et new Function sont bloqués par la CSP.
- La page ne se recharge jamais et ne quitte jamais son adresse : ni location.reload, ni location.href, location.replace ou location.assign. Le tableau de bord arrête une app qui le fait ; « Rejouer » remet la partie à zéro dans la page.
- La fenêtre fait entre 360 x 520 (téléphone) et 1100 x 850 (grand écran), et sa taille change en cours de route. Tout tient sans défilement. Un canvas suit la taille de son conteneur (ResizeObserver) et reste net (devicePixelRatio).

=== LE ROBOT : LA BOÎTE À OUTILS `reachy` ===
La page est servie avec une boîte à outils déjà chargée, `window.reachy`, qui existe avant tes scripts : elle parle au robot, tient la manette, joue le son. Sers-t'en. N'écris ni `postMessage`, ni écoute de `message`, ni ta propre couche par-dessus : une page qui le fait est refusée.
Faire bouger le robot (chaque appel rend false quand rien n'est parti) :
- `reachy.emotion(nom)` — une émotion que le robot joue pendant quelques secondes (tête, antennes, son). nom parmi : {_listed(anim.EMOTIONS)}.
- `reachy.geste(nom)` — un geste court. nom parmi : {_listed(anim.GESTURES)}.
- `reachy.regard(lacet, tangage, roulis)` — le robot regarde un point de la page, en degrés : lacet de -30 (bord gauche de la page) à 30 (bord droit), tangage de -20 (bas) à 20 (haut), roulis facultatif de -20 à 20 (la tête penchée sur le côté) ; 0, 0 = le centre.
- `reachy.antennes(gauche, droite)` — l'antenne à gauche de l'écran puis celle à droite, en degrés de -60 à 60 : 0 = au repos, positif = vers l'avant.
Savoir où il en est :
- `reachy.etat.eveille` vaut true (réveillé), false (endormi) ou null (rien n'est encore dit : la page tourne sans robot). `reachy.quand('robot', (eveille) => …)` prévient à chaque changement.
- `reachy.pastilles({{robot, manette}})` tient à jour deux éléments de ta page, à mettre discrètement dans un coin : « Robot réveillé », « Robot endormi » ou « Robot : en attente », et « Manette : … ».
Le reste de la boîte, pour ne pas le réécrire :
- `reachy.son` : `demarrer()` (dans un clic ou une touche), `note(frequence, duree, {{forme, volume, apres, vers}})`, `basculer()` pour couper ou remettre, `ctx` et `sortie` pour brancher tes propres instruments WebAudio (la sortie passe par `limiteur`, un compresseur que tu peux régler).
- `reachy.toile(canvas, {{surTaille}})` : un canvas net (devicePixelRatio) qui remplit son parent et suit sa taille (donne sa taille au parent en CSS, aucune au canvas) ; rend `{{ctx, largeur, hauteur}}` tenu à jour, et appelle `surTaille(largeur, hauteur)` à chaque changement.
- `reachy.boucle({{avancer, dessiner}})` : `avancer(pas)` à pas fixes de 1/120 s, `dessiner(dt)` une fois par image ; rend `{{arreter}}`.
- `reachy.hasard(graine)` (un hasard qui se rejoue : la même graine donne la même suite), `reachy.borne(v, min, max)`, `reachy.mele(a, b, t)`, `reachy.lisse(t)`.
- Les couleurs du tableau de bord, déjà en variables CSS : `--page`, `--plaque`, `--relief`, `--creux`, `--trait`, `--trait-fort`, `--encre`, `--encre-2`, `--encre-3`, `--ecoute`, `--pense`, `--parle`, `--sommeil`, `--attention`, `--faute` ; et dans `reachy.couleurs` pour un canvas.
Règles :
- Un appel par moment qui compte (un point, un coup joué, une victoire, une défaite), jamais dans la boucle d'animation ni à chaque image. Le tableau de bord regroupe et cadence lui-même : n'ajoute dans la page ni limite de débit ni file d'attente.
- La page reste entièrement utilisable sans robot ; appelle quand même.

=== LE ROBOT COMME MANETTE (seulement si la demande parle de manette, de boutons, d'antennes, de volant, de tourner le robot, de sa tête comme joystick, de sa voix, de son micro, de faire du bruit, de souffler ou de chanter, de lui faire refaire un geste) ===
Le corps du robot peut servir de manette, et son micro aussi. Tout passe par `reachy.lancer(profil, {{attendre, partir}})`, appelé dans le gestionnaire même du clic ou de la touche « Jouer » (jamais au chargement ni sur une minuterie : le clic de la personne dans l'app vaut accord, une demande venue sans lui attend son prochain clic). Que le robot soit réveillé ou endormi : endormi, le tableau de bord le réveille.
- `attendre(titre)` est appelé tant que le robot se met en place (1 à 6 secondes) : montre un écran d'attente avec ce titre (« Reachy se réveille… » s'il dormait, puis « Reachy se met en place… ») et un bouton « Jouer sans Reachy ».
- `partir(avecManette, raison)` est appelé une seule fois : true, la manette marche ; false, la partie se joue sans lui, et raison dit pourquoi : 'aucun' (pas de robot), 'absent' (il n'est pas venu : dis-le d'un mot, « Sans manette : au clavier ») ou 'sans' (« Jouer sans Reachy »). Lance la partie là.
- L'appel rend `{{sans}}` : branche `sans()` sur le bouton « Jouer sans Reachy ».
profil :
- 'gachettes' : ses deux antennes sont des boutons (on les pousse du doigt) ; le robot reste immobile.
- 'volant' : en plus, sa base tourne à la main comme un volant.
- 'paume' : comme 'volant', mais ses antennes sont baissées comme quand il dort : la paume posée sur sa tête tourne la base, deux doigts posés sur les antennes appuient en même temps. Une antenne baissée se lit par `gauche`, `droite` ou la valeur absolue de `antennes.gauche`, `antennes.droite` (le doigt la pousse dans un sens ou dans l'autre). Sous la paume, le sens est celui de la main, l'inverse de celui du visage du robot : la main tournée vers la gauche donne un `corps` positif, donc va du côté de `-corps`.
- 'tete' : en plus, sa tête se bouge à la main comme un joystick ou une caméra.
- 'voix' : son micro seul. Rien de son corps n'est prêté : il continue de bouger quand la page le demande (c'est le seul profil où regards, antennes, gestes et émotions partent pendant que la manette marche). Pour le micro en plus d'un corps : 'gachettes+voix', 'volant+voix' ou 'tete+voix'.
Tant que la manette marche, `reachy.pad` est tenu à jour en continu (toutes les 33 ms environ). Les côtés sont ceux de l'ÉCRAN, les angles en degrés depuis sa position de départ :
- `gauche`, `droite` (booléens) : l'antenne à gauche ou à droite de l'écran est poussée ; `antennes.gauche`, `antennes.droite` : de combien.
- `corps` : l'angle de sa base, positif = tournée vers la droite de la page (volant).
- `lacet`, `tangage`, `roulis` : sa tête (0, 0, 0 = le centre du joystick ; lacet positif = vers la droite de la page, base comprise ; tangage positif = vers le haut) ; `avant`, `cote`, `haut` : de combien la main l'a déplacée, en millimètres, vers la personne, vers la droite de la page, vers le haut (tete).
- `voix` : le niveau sonore que son micro entend, de 0 (le calme de la pièce) à 1 (fort), 0 si le profil n'écoute pas : une voix normale donne 0,5 à 0,8, un fredonnement ou un souffle 0,2 à 0,5 ; seul ce nombre arrive à la page, jamais le son.
- `t` : l'heure de la mesure, en millisecondes à l'horloge du robot.
`reachy.quand('pad', (pad) => …)` prévient à chaque mesure, `reachy.quand('appui', (cote) => …)` quand une antenne vient d'être poussée ('gauche' ou 'droite'), `reachy.quand('manette', (etat, avant) => …)` quand la manette change ('non', 'attente', 'oui').
Règles :
- Garde le clavier et les boutons à l'écran : la partie se joue sans robot. Si la manette passe à 'non' en cours de partie, continue au clavier.
- À la fin de la partie : `reachy.feter(nom)` rend la manette, puis le robot joue cette émotion ou ce geste une fois son corps repris. `reachy.manette(null)` la rend sans rien jouer.
- Tant que la manette marche, ni geste, ni émotion, ni regard, ni antennes : ils ne partent pas (sauf avec 'voix' seule). Le robot se tait tant qu'elle est tenue.
- Pour que le robot refasse exactement ce que la main lui a fait faire pendant que la manette prêtait son corps (il s'enregistre lui-même : tête entière, antennes, base) : note `reachy.pad.t` au début et à la fin du passage, rends la manette, puis `reachy.rejouer(debut, fin, tours)` (tours de 1 à 8 ; 0 arrête). Il attend d'avoir repris son corps, puis rejoue le passage (0,3 à 30 secondes). `reachy.quand('rejeu', ({{etat, secondes, tours}}) => …)` : etat vaut 'debut' quand le passage commence (secondes : la durée d'un tour ; 0,4 seconde de retour entre deux tours), puis 'fin' ; 'fin' seul : rien à rejouer. Ne recopie jamais un geste avec regard et antennes.
- Une page qui écoute (voix) : mesure le calme pendant une seconde au début de la partie et place ton seuil au-dessus, avec une marge ; ne joue aucun son tant qu'elle écoute (le micro entend les haut-parleurs) ; ignore la voix pendant 0,8 s après avoir demandé un mouvement au robot et 4 s après une émotion (il entend ses propres moteurs). Garde une touche du clavier qui remplace la voix.

=== LE ROBOT SE TAIT-IL PENDANT L'APP ? ===
Tant que l'app est à l'écran, le robot écoute la pièce et répond à ce qu'il entend. Juge selon le bon sens si les gens qui jouent parlent entre eux plutôt qu'à lui (un quiz, un jeu à plusieurs, un jeu de société, un minuteur, un jeu qu'on joue seul) : alors mets `<meta name="reachy-quiet" content="oui">` dans le `<head>`, et le robot se tait jusqu'à ce qu'on lui rende la parole. Mets `content="non"` seulement si on doit pouvoir lui parler pendant l'app (une app qu'on commente avec lui, où il répond, qu'il anime de la voix).

=== CE QUI EST ATTENDU ===
- Ça marche du premier coup : aucune erreur dans la console, pas de TODO, rien de promis qui manque, rien de tronqué.
- Les jeux se jouent au clavier ET au doigt ou à la souris : un bouton à l'écran pour chaque action de jeu, et le balayage (swipe) pour un jeu qui se dirige. Les flèches et l'espace ne font jamais défiler la page (preventDefault). La page s'ouvre sur un écran d'accueil avec un gros bouton « Jouer » ; Entrée ou Espace lance aussi la partie. Score bien visible, pause avec P, recommencer avec R ou un bouton, fin de partie claire, commandes rappelées à l'écran.
- Les animations passent par requestAnimationFrame, avec une logique qui avance à pas fixes, indépendante de la fréquence d'affichage.
- Définis `window.__selfTest = () => [[libellé, valeur calculée, valeur attendue, tolérance], …]` : la fonction retourne (return, synchrone) un tableau d'au moins cinq lignes ; dans chaque ligne, les trois derniers éléments sont des nombres (true vaut 1, false vaut 0). Compare des valeurs (un score, une position, un angle, un nombre de cases) plutôt que des booléens. Chaque ligne appelle TES vraies fonctions de jeu — celles qu'utilise la partie, pas des copies — sur une situation construite dont tu connais le résultat : une collision, un score, une ligne gagnante, l'angle envoyé au robot pour une case… Le test ne touche pas à la partie en cours et n'envoie rien au robot. Un test qui ne peut pas échouer ne compte pas.

=== UNE PAGE FACILE À RETOUCHER ===
On te demandera souvent de la retoucher (« plus rapide », « ajoute un niveau »). Écris-la pour qu'une retouche touche peu de lignes :
- Vitesses, dimensions, règles et données dans des constantes nommées en tête du script (`const VITESSE_INITIALE = 8;`), pas de nombres magiques dispersés.
- Les couleurs dans des variables CSS sur `:root`.
- Du code découpé en fonctions courtes et nommées.

=== LE STYLE ===
- Accordé au tableau de bord, sombre et sobre : fond #121417, surfaces #1D2024, texte #EBEDF0, texte secondaire #A0A7B1, accent jaune #FFCA3B ; coins arrondis, gros chiffres lisibles de loin.
- Tout le texte visible est en français, avec ses accents, libellés des boutons compris.

=== SI LA DEMANDE EST AMBIGUË ===
Choisis l'interprétation la plus probable et la plus amusante à regarder. Ne réponds jamais par une question."""


def kit() -> str:
    """The apps' toolbox (`window.reachy`: assets/kit.js), as a script: the robot's channel, the manette, the replay,
    the sound, the canvas and the loop every page is served with, so that none writes its own. Its lists are the
    contracts' own. Read at each call: a change to it shows at the next page served."""
    lists = {
        '__EMOTIONS__': list(anim.EMOTIONS),
        '__GESTURES__': list(anim.GESTURES),
        '__PROFILES__': list(get_args(ManetteProfile)),
    }
    script = KIT.read_text(encoding='utf-8')
    for name, values in lists.items():
        script = script.replace(name, json.dumps(values))
    return f'<script>{script}</script>'


def served(page: str) -> str:
    """The page as it is shown: its CSP before everything (a policy covers only what follows it), then the toolbox,
    then the page. A srcdoc is never in quirks mode, so the parser opens <html> and <head> for the meta, which is the
    head's first element whatever the page's own tags say (HTML, the 'initial' insertion mode)."""
    return f'<meta http-equiv="Content-Security-Policy" content="{CSP}">{kit()}{page}'


def extract(text: str) -> str | None:
    """The page in a model's answer, fences and chatter around it dropped; None when it is missing or cut."""
    lower = text.lower()
    start = lower.find('<!doctype html')
    start = lower.find('<html') if start < 0 else start
    end = lower.rfind('</html>')
    return text[start : end + len('</html>')] if 0 <= start < end else None


def check(page: str) -> list[str]:
    """Why the page cannot run as it is (French), or nothing."""
    reasons = ['la page dépasse 400 Ko'] if len(page) > PAGE_MAX else []
    names = [re.sub(r'\s', '', m['call'] or m['key'] or m['api'] or '') for m in FORBIDDEN.finditer(page)]
    if names:
        found = ', '.join(dict.fromkeys(name or 'une adresse externe' for name in names))
        reasons.append(f'elle utilise ce que la sandbox interdit\xa0: {found}')
    if OWN_CHANNEL.search(page):
        reasons.append(
            'elle parle au tableau de bord par son propre postMessage\xa0: tout passe par la boîte à outils `reachy` '
            '(reachy.emotion, reachy.regard, reachy.lancer…)'
        )
    ways = [re.sub(r'[\s=]', '', m['way']) for m in LEAVING.finditer(page)]
    if ways:
        reasons.append(
            f'elle recharge ou quitte sa page ({", ".join(dict.fromkeys(ways))}), ce que le tableau de bord arrête\xa0: '
            'une partie se recommence dans la page'
        )
    return reasons


def title_of(page: str) -> str:
    found = re.search(r'<title[^>]*>(.*?)</title>', page, re.I | re.S)
    return ' '.join(html.unescape(found.group(1)).split())[:APP_TITLE_MAX] if found else ''


def quiet_of(page: str) -> bool:
    """Whether the robot keeps silent while the page is on screen: unless its builder judged people talk to it there
    (`<meta name="reachy-quiet" content="non">`); an app is first a game played in front of it."""
    tag = re.search(r'<meta[^>]*name=["\']reachy-quiet["\'][^>]*>', page, re.I)
    content = re.search(r'content=["\']\s*([^"\']*)', tag.group(0), re.I) if tag else None
    return not (content and content.group(1).strip().lower() in ('non', 'no', 'false', '0'))


RETOUCH = f"""Tu retouches une petite application web qui s'affiche dans le tableau de bord d'un robot de bureau, un Reachy Mini : le fichier `app.html` du dossier courant, une page HTML autonome (tout le CSS dans un `<style>`, tout le JavaScript dans un `<script>` en ligne). La personne attend devant l'écran : va droit au but.

=== COMMENT ===
- Lis `app.html`, puis fais la retouche demandée par des modifications ciblées avec l'outil Edit : le moins de lignes possible, aucune réécriture du fichier, aucun autre fichier. Garde tout ce qui marchait.
- Les couleurs sont des variables CSS sur `:root`, les réglages des constantes nommées en tête du script : change-les là plutôt qu'ailleurs.
- Si la retouche change une règle testée par `window.__selfTest`, mets le test à jour ; sinon n'y touche pas.
- Une fois fait, réponds en une phrase, en français : ce que tu as changé. Ne pose jamais de question : choisis l'interprétation la plus probable.

=== CE QUE LA PAGE DOIT RESPECTER ===
- Elle tourne dans une iframe `sandbox="allow-scripts"` sous la CSP `{CSP}`.
- Aucune ressource externe, aucune adresse http(s), polices système seulement.
- Ces noms ne doivent apparaître nulle part, pas même en commentaire : fetch, XMLHttpRequest, WebSocket, EventSource, sendBeacon, RTCPeerConnection, WebTransport, localStorage, sessionStorage, indexedDB, document.cookie, alert, confirm, prompt, window.open.
- Elle ne se recharge jamais et ne quitte pas son adresse (ni location.reload, ni location.href, location.replace ou location.assign) : le tableau de bord l'arrêterait. Recommencer remet la partie à zéro dans la page.
- Le robot ne bouge que par la boîte à outils `window.reachy`, déjà chargée avant les scripts de la page (`reachy.emotion`, `reachy.geste`, `reachy.regard`, `reachy.antennes`, `reachy.lancer`, `reachy.feter`…) : réutilise les appels déjà présents, un par moment qui compte, jamais dans la boucle d'animation. N'écris ni `postMessage` ni écoute de `message` : une page qui le fait est refusée.
- Tout le texte visible en français ; un bouton à l'écran pour chaque action ; la page tient sans défilement de 360 x 520 à 1100 x 850."""


def verify(
    page: str,
    cancelled: Callable[[], bool] = lambda: False,
    started: Callable[[subprocess.Popen[bytes]], None] = lambda _chrome: None,
) -> list[str]:
    """Why the page cannot be shown (French): the scan, then what breaks once it runs (`app_check.run`)."""
    return check(page) or app_check.run(page, CSP, cancelled, started, kit=kit())


class Build:
    """One build in flight: the Claude runs (a creation, a retouch, a repair), their progress published as it
    changes (`publish`, at most every PUBLISH_S, the latest only), and `cancel` from any thread."""

    def __init__(
        self,
        app_id: str,
        kind: str,
        publish: Callable[[AppBuild], None],
        model: str = claude.MODEL,
        effort: str = 'high',
    ) -> None:
        self.app_id, self.kind, self._publish, self._model, self._effort = app_id, kind, publish, model, effort
        self.started_at = time.time()
        self._lock = threading.Lock()
        self._run: claude.Run | None = None
        self._chrome: subprocess.Popen[bytes] | None = None  # the page's check
        self._cancelled = False
        self._phase, self._round, self._problems = 'starting', 1, list[str]()
        self._progress = claude.Progress()
        self._written: str | None = None  # a creation's page once written whole: a repair's run writes none
        self._cost = 0.0
        self._dirty = threading.Event()
        self._done = threading.Event()
        threading.Thread(target=self._publisher, name=f'app-{app_id}-progress', daemon=True).start()

    def create(self, ask: str) -> str:
        """The page for `ask`, written whole, checked, repaired once by editing it."""
        try:
            answer = self._claude(ask, PROMPT, CREATE_TIMEOUT_S, 'la page')
            page = extract(answer)
            rewritten = page is None
            if rewritten:  # cut short: only a whole page again can mend it, and that is its one repair
                self._repairing(['la page est incomplète (coupée avant </html>) ou absente'])
                again = f'{ask}\n\nRends la page COMPLÈTE, plus courte.'
                page = extract(self._claude(again, PROMPT, CREATE_TIMEOUT_S, 'la page'))
            if page is None:
                raise ClaudeError('La page a été refusée\xa0: elle est encore incomplète ou absente.')
            self._written = page
            return self._checked(page, ask, repair=not rewritten)
        finally:
            self._end()

    def retouch(self, base: str, ask: str) -> str:
        """`base` changed as `ask` says, by targeted edits on a copy, checked, repaired once."""
        try:
            return self._checked(base, ask, edit_first=True)
        finally:
            self._end()

    @property
    def cancelled(self) -> bool:
        return self._cancelled

    def cancel(self) -> None:
        """From any thread, the build's own one included: a stopping server gives that one no time to poll."""
        with self._lock:
            self._cancelled = True
            run, chrome = self._run, self._chrome
        if run is not None:
            run.cancel()
        if chrome is not None:
            app_check.stop(chrome)

    def draft(self) -> str | None:
        """The page as written so far (a creation), the page being repaired, or None."""
        text = self._progress.text
        start = max(text.lower().find('<!doctype'), -1)
        start = text.lower().find('<html') if start < 0 else start
        return text[start:] if start >= 0 else self._written

    def snapshot(self) -> AppBuild:
        progress, page = self._progress, self.draft() or ''
        tail = '\n'.join(page.splitlines()[-TAIL_LINES:])[-TAIL_MAX:]
        edits = [AppEdit(old=old[:EDIT_MAX], new=new[:EDIT_MAX]) for old, new in progress.edits[-EDITS_SHOWN:]]
        return AppBuild(
            app_id=self.app_id, kind=self.kind, phase=self._phase, round=self._round, started_at=self.started_at,
            thinking_tokens=progress.thinking_tokens, chars=len(page), tail=tail, edits=edits,
            problems=self._problems, cost_usd=round(self._cost, 4) if self._cost else None,
        )  # fmt: skip

    # ----- the runs

    def _checked(self, page: str, ask: str, *, edit_first: bool = False, repair: bool = True) -> str:
        """Edit `page` (a retouch), then check it; refused, it is edited once more with the reasons."""
        with tempfile.TemporaryDirectory() as work:
            path = Path(work) / 'app.html'
            path.write_text(page, encoding='utf-8')
            if edit_first:
                before = page
                self._claude(ask, RETOUCH, RETOUCH_TIMEOUT_S, 'la retouche', workdir=path.parent)
                page = path.read_text(encoding='utf-8')
                if page == before:
                    raise ClaudeError("Claude n'a rien changé à la page.")
            reasons = self._verifying(page)
            if reasons and repair:
                self._repairing(reasons)
                fix = f'{ask}\n\nLa page a été refusée\xa0: ' + '\xa0; '.join(reasons) + '. Corrige `app.html`.'
                self._claude(fix, RETOUCH, RETOUCH_TIMEOUT_S, 'la réparation', workdir=path.parent)
                page = path.read_text(encoding='utf-8')
                if self._written is not None:  # a creation: its page as repaired, shown while it is checked
                    self._written = page
                reasons = self._verifying(page)
            if reasons:
                raise ClaudeError('La page a été refusée\xa0: ' + '\xa0; '.join(reasons) + '.')
            return page

    def _claude(self, text: str, system: str, timeout_s: float, what: str, workdir: Path | None = None) -> str:
        with self._lock:
            if self._cancelled:
                raise Cancelled('Construction annulée.')
            run = self._run = claude.Run(
                text, system, effort=self._effort, timeout_s=timeout_s, what=what, model=self._model,
                tools='Read,Edit' if workdir else '', workdir=workdir, on_progress=self._on_progress,
            )  # fmt: skip
            edits = list(self._progress.edits)  # a repair's edits follow the retouch's
            run.progress.edits = edits
            self._progress = run.progress
        try:
            return run.wait().text
        finally:
            self._cost += run.cost_usd

    def _verifying(self, page: str) -> list[str]:
        self._phase = 'checking'
        self._dirty.set()
        if self._cancelled:
            raise Cancelled('Construction annulée.')
        reasons = verify(page, lambda: self._cancelled, self._checking)
        if self._cancelled:  # the run was stopped: its verdict means nothing
            raise Cancelled('Construction annulée.')
        return reasons

    def _checking(self, chrome: subprocess.Popen[bytes]) -> None:
        with self._lock:
            self._chrome = chrome
            if self._cancelled:  # cancelled as Chrome started: it would run on for nobody
                app_check.stop(chrome)

    def _repairing(self, reasons: list[str]) -> None:
        log.warning('app %s refused (%s): one repair', self.app_id, '; '.join(reasons))
        self._round, self._problems, self._phase = 2, reasons, 'starting'
        self._dirty.set()

    def _on_progress(self, progress: claude.Progress) -> None:
        self._phase = progress.phase
        self._dirty.set()

    # ----- publishing

    def _publisher(self) -> None:
        """The latest snapshot, at most every PUBLISH_S; the build's last state always goes out."""
        self._publish(self.snapshot())
        while not self._done.is_set():
            self._dirty.wait()
            self._dirty.clear()
            self._publish(self.snapshot())
            self._done.wait(PUBLISH_S)
        if self._dirty.is_set():  # ended while the pace was waited out
            self._publish(self.snapshot())

    def _end(self) -> None:
        self._done.set()
        self._dirty.set()
