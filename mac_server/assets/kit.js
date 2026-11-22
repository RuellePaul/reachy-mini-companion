// La boîte à outils des apps de Reachy : `window.reachy`. Le Mac la met en tête de chaque page qu'il sert, avant
// les scripts de la page : une app n'écrit ni son canal vers le robot, ni sa manette, ni son moteur de son. Ce qui
// est ici ne fait que s'ajouter d'une version à l'autre : une app rangée hier doit marcher demain.
(() => {
  'use strict';
  const VERSION = 1;
  const EMOTIONS = __EMOTIONS__;
  const GESTES = __GESTURES__;
  const PROFILS = __PROFILES__;
  // Le temps que Reachy reprenne son corps une fois la manette rendue (il attend d'être lâché), par corps prêté.
  const REPRISE_MS = { gachettes: 900, volant: 2600, paume: 2600, tete: 2600 };
  const TENUE = { gachettes: 'antennes = boutons', volant: 'volant actif', paume: 'paume = volant, doigts = boutons', tete: 'tête = manette', voix: 'Reachy t’écoute' };
  // « Porcelaine et graphite » : les couleurs du tableau de bord, en variables CSS (--page, --encre…) et pour un canvas.
  const COULEURS = {
    page: '#16181B', plaque: '#1D2024', relief: '#25292E', creux: '#121417', trait: '#30353C', 'trait-fort': '#474D56',
    encre: '#EBEDF0', 'encre-2': '#A0A7B1', 'encre-3': '#8A919C',
    ecoute: '#3DCB8E', pense: '#A592FF', parle: '#FFCA3B', sommeil: '#8EA8CB', attention: '#F2A541', faute: '#F26B5E',
  };

  const borne = (v, a, b) => Math.min(b, Math.max(a, v));
  const mele = (a, b, t) => a + (b - a) * t;
  const lisse = (t) => { t = borne(t, 0, 1); return t * t * (3 - 2 * t); };
  /** Un hasard qui se rejoue : la même graine donne la même suite de nombres entre 0 et 1. */
  const hasard = (graine) => {
    let a = graine >>> 0;
    return () => {
      a = (a + 0x6D2B79F5) >>> 0;
      let t = Math.imul(a ^ (a >>> 15), a | 1);
      t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
      return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
    };
  };

  // ===== Ce que la page sait du robot =====
  const etat = { eveille: null, manette: 'non', profil: null };   // eveille : null tant que rien n'est dit ; manette : 'non', 'attente' ou 'oui'
  // Les côtés sont ceux de l'ÉCRAN (face à toi, l'antenne droite du robot est à gauche de l'écran). Angles en degrés,
  // distances en mm, depuis la pose de repos ; voix de 0 (le calme de la pièce) à 1 ; t : l'heure de la mesure, en ms
  // à l'horloge du robot (ce qui borne un « rejoue »).
  const pad = {
    gauche: false, droite: false, antennes: { gauche: 0, droite: 0 },
    corps: 0, lacet: 0, tangage: 0, roulis: 0, avant: 0, cote: 0, haut: 0, voix: 0, t: 0,
  };
  const ecoutes = new Map();
  /** S'abonner : 'robot' (éveillé), 'manette' (état, état d'avant), 'pad' (pad), 'appui' (côté), 'rejeu' ({etat, secondes, tours}), 'son' (coupé). Rend de quoi se désabonner. */
  const quand = (nom, fn) => {
    if (!ecoutes.has(nom)) ecoutes.set(nom, new Set());
    ecoutes.get(nom).add(fn);
    return () => ecoutes.get(nom).delete(fn);
  };
  const dire = (nom, ...quoi) => { for (const fn of [...(ecoutes.get(nom) || [])]) fn(...quoi); };
  /** Ce qu'un profil prête du corps : 'gachettes', 'volant', 'paume', 'tete', ou rien ('voix' seule). */
  const corpsDe = (profil) => (!profil || profil === 'voix' ? null : profil.split('+')[0]);

  // ===== Vers le robot : un seul canal =====
  /** Le message pour le robot, borné ; null s'il n'est pas permis. Pur : les autotests l'appellent. */
  function message(type, champs) {
    const c = champs || {};
    if (type === 'emotion') return EMOTIONS.includes(c.name) ? { reachy: 1, type, name: c.name } : null;
    if (type === 'gesture') return GESTES.includes(c.name) ? { reachy: 1, type, name: c.name } : null;
    if (type === 'look') return { reachy: 1, type, yaw: borne(+c.yaw || 0, -30, 30), pitch: borne(+c.pitch || 0, -20, 20), roll: borne(+c.roll || 0, -20, 20) };
    if (type === 'antennas') return { reachy: 1, type, left: borne(+c.left || 0, -60, 60), right: borne(+c.right || 0, -60, 60) };
    if (type === 'manette') return c.profile === null || PROFILS.includes(c.profile) ? { reachy: 1, type, profile: c.profile } : null;
    if (type === 'replay') return { reachy: 1, type, start: +c.start || 0, end: +c.end || 0, loops: borne(Math.round(+c.loops || 0), 0, 8) };
    return null;
  }
  /** Tout part d'ici. Tant qu'il prête son corps, le robot ne joue ni geste ni regard (un « rejoue » attend qu'il l'ait repris) : rend false si rien n'est parti. */
  function envoyer(type, champs) {
    const m = message(type, champs);
    const prete = etat.manette !== 'non' && corpsDe(etat.profil);
    if (!m || (prete && type !== 'manette' && type !== 'replay')) return false;
    window.parent.postMessage(m, '*');
    return true;
  }
  const changer = (manette) => {
    const avant = etat.manette;
    etat.manette = manette;
    if (manette === 'non') {
      etat.profil = null;
      pad.gauche = pad.droite = false;
      pad.voix = 0;
    }
    if (avant !== manette) dire('manette', manette, avant);
  };
  /** Demander la manette (un profil : dans le gestionnaire même du clic ou de la touche du joueur, c'est son accord) ou la rendre (null). */
  function manette(profil) {
    if (profil === null) {
      if (etat.manette !== 'non') envoyer('manette', { profile: null });
      changer('non');
      return true;
    }
    if (!PROFILS.includes(profil)) return false;
    etat.profil = profil;
    envoyer('manette', { profile: profil });
    if (etat.manette === 'non') changer('attente');
    return true;
  }
  let tentative = null;   // le « Jouer » en cours : de quoi l'oublier si un autre le remplace
  /**
   * « Jouer », à appeler dans le clic ou la touche : avec Reachy dès qu'il y en a un (endormi, le tableau de bord
   * le réveille). `attendre(titre)` est appelé tant qu'il se met en place (« Reachy se réveille… », « Reachy se met
   * en place… »), puis `partir(avecManette, raison)` une seule fois : true, la manette marche ('oui') ; false, la
   * partie se joue sans lui : 'aucun' (pas de robot), 'absent' (demandé, il n'est pas venu) ou 'sans' (le joueur l'a
   * choisi). Rend { sans } pour le bouton « Jouer sans Reachy ».
   */
  function lancer(profil, suite) {
    const s = suite || {};
    if (tentative) tentative();
    if (etat.eveille === null || !PROFILS.includes(profil)) {
      if (s.partir) s.partir(false, 'aucun');
      return { sans() {} };
    }
    if (etat.manette === 'oui' && etat.profil === profil) {
      if (s.partir) s.partir(true, 'oui');
      return { sans() {} };
    }
    let fini = false;
    const oublier = [];
    const finir = (raison) => {
      if (fini) return;
      fini = true;
      tentative = null;
      oublier.forEach((off) => off());
      if (raison && s.partir) s.partir(raison === 'oui', raison);
    };
    const titre = () => { if (!fini && s.attendre) s.attendre(etat.eveille ? 'Reachy se met en place…' : 'Reachy se réveille…'); };
    tentative = () => finir(null);
    manette(profil);
    oublier.push(quand('manette', (m) => { if (m === 'oui') finir('oui'); else if (m === 'non') finir('absent'); }));
    oublier.push(quand('robot', titre));
    titre();
    return { sans() { if (!fini) { finir('sans'); manette(null); } } };
  }
  /** La fin d'une partie : rend la manette, puis Reachy joue une émotion ou un geste une fois son corps repris. */
  function feter(nom) {
    const corps = etat.manette !== 'non' ? corpsDe(etat.profil) : null;
    manette(null);
    const jouer = () => envoyer(EMOTIONS.includes(nom) ? 'emotion' : 'gesture', { name: nom });
    if (corps) setTimeout(jouer, REPRISE_MS[corps]);
    else jouer();
  }

  // ===== Du robot =====
  window.addEventListener('message', (e) => {
    const d = e.data;
    if (e.source !== window.parent || !d || d.reachy !== 1) return;
    if (d.type === 'robot') {
      const eveille = !!d.awake;
      if (eveille !== etat.eveille) { etat.eveille = eveille; dire('robot', eveille); }
    } else if (d.type === 'manette') {
      if (d.state === 'on' && d.profile) etat.profil = d.profile;
      changer(d.state === 'on' ? 'oui' : d.state === 'asking' ? 'attente' : 'non');
    } else if (d.type === 'pad' && etat.manette === 'oui') {
      const gauche = !!d.right, droite = !!d.left, tete = d.head || {}, antennes = d.antennas || {};
      const appuis = [gauche && !pad.gauche ? 'gauche' : null, droite && !pad.droite ? 'droite' : null];
      pad.gauche = gauche; pad.droite = droite;
      pad.antennes.gauche = +antennes.right || 0; pad.antennes.droite = +antennes.left || 0;
      pad.corps = +d.body || 0;
      pad.lacet = (+tete.yaw || 0) + pad.corps; pad.tangage = +tete.pitch || 0; pad.roulis = +tete.roll || 0;
      pad.avant = +tete.x || 0; pad.cote = +tete.y || 0; pad.haut = +tete.z || 0;
      pad.voix = borne(+d.voice || 0, 0, 1);
      pad.t = +d.t || 0;
      for (const cote of appuis) if (cote) dire('appui', cote);
      dire('pad', pad);
    } else if (d.type === 'replay') {
      dire('rejeu', { etat: d.state === 'started' ? 'debut' : 'fin', secondes: +d.seconds || 0, tours: +d.loops || 0 });
    }
  });

  // ===== Le son (WebAudio, lancé par un geste du joueur) =====
  const son = {
    ctx: null, sortie: null, limiteur: null, coupe: false, volume: 0.8,
    /** À appeler dans un clic ou une touche : le contexte audio, ou null s'il n'y en a pas. Tout ce qui sonne se branche sur `sortie`, qui passe par `limiteur` (un compresseur : ses réglages sont à la page). */
    demarrer() {
      if (!son.ctx) {
        const Contexte = window.AudioContext || window.webkitAudioContext;
        if (!Contexte) return null;
        son.ctx = new Contexte();
        son.sortie = son.ctx.createGain();
        son.sortie.gain.value = son.coupe ? 0 : son.volume;
        son.limiteur = son.ctx.createDynamicsCompressor();
        son.sortie.connect(son.limiteur).connect(son.ctx.destination);
      }
      if (son.ctx.state === 'suspended') son.ctx.resume().catch(() => {});
      return son.ctx;
    },
    /** Une note : fréquence (Hz), durée (s) ; options : forme, volume, apres (s), vers (Hz : la note glisse). */
    note(frequence, duree, options) {
      const ctx = son.demarrer();
      if (!ctx) return;
      const o = options || {}, t = ctx.currentTime + (o.apres || 0);
      const osc = ctx.createOscillator(), gain = ctx.createGain();
      osc.type = o.forme || 'sine';
      osc.frequency.setValueAtTime(frequence, t);
      if (o.vers) osc.frequency.exponentialRampToValueAtTime(o.vers, t + duree);
      gain.gain.setValueAtTime(0.0001, t);
      gain.gain.exponentialRampToValueAtTime(o.volume || 0.2, t + 0.008);
      gain.gain.exponentialRampToValueAtTime(0.0001, t + duree);
      osc.connect(gain).connect(son.sortie);
      osc.start(t);
      osc.stop(t + duree + 0.02);
    },
    /** Couper ou remettre le son ; rend true s'il est coupé. */
    basculer() {
      son.coupe = !son.coupe;
      if (son.sortie) son.sortie.gain.value = son.coupe ? 0 : son.volume;
      dire('son', son.coupe);
      return son.coupe;
    },
  };

  // ===== Le dessin et la boucle =====
  /** Un canvas net qui suit la taille de son parent : rend { ctx, largeur, hauteur, dpr } tenu à jour ; `surTaille(largeur, hauteur)` à chaque changement. Un canvas auquel le CSS de la page ne donne aucune taille remplit son parent. Un cadre peut mesurer 0×0 au départ : la toile garde 1 pixel. Un canvas déjà pris par WebGL n'a pas de `ctx` : seule sa taille est tenue. */
  function toile(canvas, options) {
    const o = options || {}, t = { ctx: canvas.getContext('2d'), largeur: 0, hauteur: 0, dpr: 1 };
    let pose = false;
    /** Sa boîte suit-elle ses propres pixels ? Alors la page ne lui a donné aucune taille : il couvre l'intérieur de son parent, hors du flux, pour que la taille du parent ne dépende plus de lui. Un parent qui n'avait de taille que celle de son canvas : la boîte d'un canvas par défaut, dans le flux. */
    const poser = () => {
      const l = canvas.clientWidth, h = canvas.clientHeight, pl = canvas.width, ph = canvas.height;
      canvas.width = pl + 16;
      canvas.height = ph + 16;
      const libre = canvas.clientWidth !== l && canvas.clientHeight !== h;
      canvas.width = pl;
      canvas.height = ph;
      const cadre = canvas.parentElement;
      if (!libre || !cadre) return;
      const c = getComputedStyle(cadre), px = (v) => parseFloat(v) || 0;
      const gauche = px(c.paddingLeft), haut = px(c.paddingTop);
      const position = cadre.style.position;
      if (c.position === 'static') cadre.style.position = 'relative';
      Object.assign(canvas.style, {
        display: 'block', position: 'absolute', left: gauche + 'px', top: haut + 'px',
        width: 'calc(100% - ' + (gauche + px(c.paddingRight)) + 'px)', height: 'calc(100% - ' + (haut + px(c.paddingBottom)) + 'px)',
      });
      const dedans = { l: canvas.clientWidth, h: canvas.clientHeight };
      if (dedans.l && dedans.h) return;
      cadre.style.position = position;
      Object.assign(canvas.style, { position: '', left: '', top: '', width: (dedans.l || 300) + 'px', height: (dedans.h || 150) + 'px' });
    };
    const ajuster = () => {
      t.dpr = Math.min(o.dprMax || 2, window.devicePixelRatio || 1);
      if (!pose && (canvas.clientWidth || canvas.clientHeight)) {
        pose = true;
        poser();
      }
      t.largeur = canvas.clientWidth;
      t.hauteur = canvas.clientHeight;
      canvas.width = Math.max(1, Math.round(t.largeur * t.dpr));
      canvas.height = Math.max(1, Math.round(t.hauteur * t.dpr));
      if (t.ctx) t.ctx.setTransform(t.dpr, 0, 0, t.dpr, 0, 0);
      if (o.surTaille) o.surTaille(t.largeur, t.hauteur);
    };
    if (window.ResizeObserver) new ResizeObserver(ajuster).observe(canvas.parentElement || canvas);
    ajuster();
    return t;
  }
  /** Une logique à pas fixes (`avancer(pas)`, 120 fois par seconde par défaut) et un dessin par image (`dessiner(dt, t)`) ; rend { arreter }. */
  function boucle(options) {
    const o = options || {}, pas = o.pas || 1 / 120;
    let dernier = 0, reste = 0, image = 0, marche = true;
    const tour = (t) => {
      if (!marche) return;
      const dt = Math.min(0.1, (t - dernier) / 1000 || 0);
      dernier = t;
      reste += dt;
      while (reste >= pas) { if (o.avancer) o.avancer(pas); reste -= pas; }
      if (o.dessiner) o.dessiner(dt, t);
      image = requestAnimationFrame(tour);
    };
    image = requestAnimationFrame(tour);
    return { arreter() { marche = false; cancelAnimationFrame(image); } };
  }
  /** Les deux pastilles d'état, tenues à jour : `robot` et `manette` (deux éléments), `tenue` (ce que dit la manette quand elle marche). */
  function pastilles(options) {
    const o = options || {};
    const montrer = () => {
      if (o.robot) {
        o.robot.textContent = etat.eveille === null ? 'Robot : en attente' : etat.eveille ? 'Robot réveillé' : 'Robot endormi';
        o.robot.dataset.etat = etat.eveille === null ? '' : etat.eveille ? 'oui' : 'dort';
      }
      if (o.manette) {
        const tenue = o.tenue || TENUE[corpsDe(etat.profil) || 'voix'];
        const attente = etat.eveille === false ? 'Reachy se réveille' : 'Reachy se met en place';
        o.manette.textContent = 'Manette : ' + (etat.manette === 'oui' ? tenue : etat.manette === 'attente' ? attente : 'non');
        o.manette.dataset.etat = etat.manette;
      }
    };
    quand('robot', montrer);
    quand('manette', montrer);
    montrer();
  }

  const style = document.createElement('style');
  style.textContent = ':root{' + Object.entries(COULEURS).map(([nom, valeur]) => '--' + nom + ':' + valeur).join(';') + '}';
  document.head.appendChild(style);

  window.reachy = Object.freeze({
    version: VERSION, EMOTIONS, GESTES, PROFILS, REPRISE_MS, couleurs: COULEURS,
    etat, pad, quand, corpsDe,
    message, envoyer, manette, lancer, feter,
    emotion: (nom) => envoyer('emotion', { name: nom }),
    geste: (nom) => envoyer('gesture', { name: nom }),
    /** Regarder un point de la page : lacet −30 (bord gauche) à 30, tangage −20 (bas) à 20, roulis −20 à 20 (la tête penchée, comme pad.roulis). */
    regard: (lacet, tangage, roulis) => envoyer('look', { yaw: lacet, pitch: tangage, roll: roulis }),
    /** Ses antennes, celle à gauche de l'écran puis celle à droite, de −60 à 60 degrés (positif : vers l'avant). */
    antennes: (gauche, droite) => envoyer('antennas', { left: droite, right: gauche }),
    /** Refaire ce que la main lui a fait faire entre deux `pad.t`, `tours` fois (1 à 8) ; 0 tour arrête. */
    rejouer: (debut, fin, tours) => envoyer('replay', { start: debut, end: fin, loops: tours }),
    son, toile, boucle, pastilles, hasard, borne, mele, lisse,
  });
})();
