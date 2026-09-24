/*
 * runtime.js — construit dans Chromium la scène d'un plan compilé (contrat mograph-shot/1, lu dans
 * window.__MOGRAPH_SHOT__) et expose au pilote Python l'API déterministe (CONTRACT §5) :
 *
 *   await window.__seek(frame, sub = 0)   place TOUT l'état de la page sur l'image demandée
 *   window.__layout()                     boîtes des textes visibles (contrôle des zones sûres)
 *   window.__MOGRAPH_READY__ = true       tout est chargé, décodé, construit
 *   window.__MOGRAPH_ERROR__ = "message"  erreur de montage (police absente, image indécodable...)
 *
 * Principe de déterminisme : l'image rendue ne dépend QUE de (plan, graine, frame, sub). Aucune
 * horloge réelle, aucun Math.random réel (clock.js), aucune ressource décodée paresseusement, aucun
 * état GSAP dépendant de l'historique des positions (on repasse toujours par t = 0).
 */
(function () {
  "use strict";

  const SVGNS = "http://www.w3.org/2000/svg";
  const SHOT = window.__MOGRAPH_SHOT__;
  // Graine uint32 du plan : dérivée par le compilateur, transmise par le pilote avant clock.js.
  const SEED = Number(window.__MOGRAPH_SEED__) >>> 0;
  // Segmentation et formats de nombres en français, figés ici (et locale fr-FR côté navigateur).
  const SEGMENT_LOCALE = "fr";
  const NUMBER_LOCALE = "fr-FR";

  // Espaces SÉCABLES uniquement : les espaces insécables (U+00A0, U+202F) restent dans les mots,
  // comme dans la typographie française (« Merci ! » ne se coupe jamais avant le point).
  const BREAKING_SPACE = /[ \t\r\n\f\v]/;
  const ONLY_BREAKING_SPACES = /^[ \t\r\n\f\v]+$/;

  // Classe d'erreur « attendue » : son message est déjà destiné à l'utilisateur (français, actionnable).
  class MographError extends Error {}

  function fail(message) {
    return new MographError(message);
  }

  const state = {
    ready: false,
    master: null,
    stage: null,
    scene: null,
    defs: null,
    layers: [],
    hooks: [],
    sceneFilterParts: { tracking: "", textures: "" },
    lastSceneFilter: null
  };

  // Mesures des cibles AVANT toute transformation (pour convertir x/y "N%" en px par cible).
  const BOX = new WeakMap();

  // ------------------------------------------------------------------------------------------
  // Outils généraux
  // ------------------------------------------------------------------------------------------

  function px(v) {
    return `${v}px`;
  }

  // Arrondi décimal : les chaînes CSS générées restent courtes et identiques d'un lancement à l'autre.
  function round(v, digits) {
    const k = Math.pow(10, digits);
    return Math.round(v * k) / k;
  }

  function clamp(v, lo, hi) {
    if (!(v === v)) {
      return lo; // NaN -> borne basse : un easing défectueux ne peut jamais faire déborder
    }
    return v < lo ? lo : v > hi ? hi : v;
  }

  function el(tag, className) {
    const node = document.createElement(tag);
    if (className) {
      node.className = className;
    }
    return node;
  }

  function svgNode(tag, attrs) {
    const node = document.createElementNS(SVGNS, tag);
    if (attrs) {
      for (const key of Object.keys(attrs)) {
        node.setAttribute(key, String(attrs[key]));
      }
    }
    return node;
  }

  function cssFamily(family) {
    // Famille seule, entre guillemets : AUCUNE police de repli (CONTRACT §3.5).
    return `"${String(family).replace(/["\\]/g, "\\$&")}"`;
  }

  // "#RRGGBB" / "#RRGGBBAA" -> "rgba(r, g, b, a)" : forme que GSAP interpole sans ambiguïté.
  function rgba(hex, where) {
    const m = /^#([0-9A-Fa-f]{6})([0-9A-Fa-f]{2})?$/.exec(String(hex));
    if (!m) {
      throw fail(`Couleur invalide « ${hex} »${where ? " (" + where + ")" : ""} : format #RRGGBB ou #RRGGBBAA attendu. Recompilez la scène (le compilateur résout les noms de palette).`);
    }
    const n = parseInt(m[1], 16);
    const a = m[2] ? parseInt(m[2], 16) / 255 : 1;
    return `rgba(${(n >> 16) & 255}, ${(n >> 8) & 255}, ${n & 255}, ${round(a, 4)})`;
  }

  function rgbTriplet(hex) {
    const m = /^#([0-9A-Fa-f]{6})([0-9A-Fa-f]{2})?$/.exec(String(hex));
    const n = parseInt(m[1], 16);
    return [((n >> 16) & 255) / 255, ((n >> 8) & 255) / 255, (n & 255) / 255, m[2] ? parseInt(m[2], 16) / 255 : 1];
  }

  function paint(v, where) {
    return v === "none" || v === null || v === undefined ? "none" : rgba(v, where);
  }

  // mulberry32 : même PRNG que clock.js, mais instancié avec des graines DÉDIÉES (grain...) pour
  // ne jamais consommer la suite de Math.random (qui dépendrait alors de l'ordre de construction).
  function mulberry32(a) {
    return function () {
      a = (a + 0x6D2B79F5) | 0;
      let t = Math.imul(a ^ (a >>> 15), 1 | a);
      t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
      return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
    };
  }

  // Hachage entier sans état (FNV-1a + finaliseur murmur3) : les effets aléatoires sont une fonction
  // PURE de (image quantifiée, calque, graine), donc identiques quel que soit l'ordre des seeks.
  function hash32() {
    let h = (0x811C9DC5 ^ SEED) >>> 0;
    for (let i = 0; i < arguments.length; i++) {
      h = Math.imul(h ^ (arguments[i] | 0), 0x01000193);
      h ^= h >>> 13;
    }
    h ^= h >>> 16;
    h = Math.imul(h, 0x85EBCA6B);
    h ^= h >>> 13;
    h = Math.imul(h, 0xC2B2AE35);
    h ^= h >>> 16;
    return h >>> 0;
  }

  function hash01() {
    return hash32.apply(null, arguments) / 4294967296;
  }

  function nextFrame() {
    return new Promise((resolve) => requestAnimationFrame(() => resolve()));
  }

  function canvasToObjectUrl(canvas, what) {
    return new Promise((resolve, reject) => {
      canvas.toBlob((blob) => {
        if (blob) {
          resolve(URL.createObjectURL(blob));
        } else {
          reject(fail(`Impossible d'encoder la texture « ${what} » (canvas.toBlob a échoué) : mémoire insuffisante ?`));
        }
      }, "image/png");
    });
  }

  async function decodedImage(url, what) {
    const img = new Image();
    img.decoding = "sync";
    img.src = url;
    try {
      await img.decode();
    } catch (e) {
      throw fail(`Image indécodable : ${what} (${url.startsWith("blob:") ? "texture générée" : url}). Vérifiez que le fichier existe et qu'il s'agit d'un PNG/JPEG/WebP valide.`);
    }
    return img;
  }

  // ------------------------------------------------------------------------------------------
  // Validation du plan compilé
  // ------------------------------------------------------------------------------------------

  const REQUIRED_SHOT_KEYS = ["contract", "scene_id", "shot_id", "shot_index", "seed", "fps", "frames", "duration",
    "canvas", "device_scale_factor", "alpha", "background", "frame_step", "motion_blur", "safe_zones", "palette",
    "fonts", "typography", "custom_eases", "textures", "glitch", "plates", "layers"];
  const LAYER_TYPES = new Set(["text", "shape", "image", "sequence", "chart"]);
  const TARGETS = new Set(["self", "chars", "words", "lines", "shape", "bars"]);

  function validateShot() {
    if (!SHOT || typeof SHOT !== "object") {
      throw fail("window.__MOGRAPH_SHOT__ absent : la page doit être ouverte par pipeline/web_render.py (script d'initialisation graine + plan).");
    }
    if (SHOT.contract !== "mograph-shot/1") {
      throw fail(`Contrat de plan inattendu « ${SHOT.contract} » (attendu « mograph-shot/1 ») : recompilez la scène avec pipeline/scene.py.`);
    }
    for (const key of REQUIRED_SHOT_KEYS) {
      if (!(key in SHOT)) {
        throw fail(`Plan compilé incomplet : clé « ${key} » absente. Recompilez la scène (schema/internal/compiled_shot.schema.json).`);
      }
    }
    const c = SHOT.canvas;
    if (!(Number.isInteger(c.width) && c.width > 0 && Number.isInteger(c.height) && c.height > 0)) {
      throw fail(`Canevas invalide ${JSON.stringify(c)} : largeur et hauteur entières > 0 attendues.`);
    }
    if (!(Number.isInteger(SHOT.frame_step) && SHOT.frame_step >= 1)) {
      throw fail(`frame_step invalide (${SHOT.frame_step}) : entier >= 1 attendu.`);
    }
    if (!Array.isArray(SHOT.fonts) || SHOT.fonts.length === 0) {
      throw fail("Aucune police dans le plan (fonts vide) : déclarez les polices dans preset.fonts.");
    }
    if (!Array.isArray(SHOT.layers)) {
      throw fail("Plan compilé invalide : layers doit être une liste.");
    }
    const ids = new Set();
    for (const L of SHOT.layers) {
      if (!LAYER_TYPES.has(L.type)) {
        throw fail(`Calque « ${L.id} » : type « ${L.type} » inconnu (text, shape, image, sequence, chart).`);
      }
      if (ids.has(L.id)) {
        throw fail(`Deux calques portent l'identifiant « ${L.id} » : les id doivent être uniques dans un plan.`);
      }
      ids.add(L.id);
      for (const tw of L.tweens || []) {
        if (!TARGETS.has(tw.target)) {
          throw fail(`Calque « ${L.id} » : cible de tween « ${tw.target} » inconnue.`);
        }
      }
      if (L.type === "sequence" && !(SHOT.plates && SHOT.plates[L.plate])) {
        throw fail(`Calque « ${L.id} » : la plaque « ${L.plate} » n'existe pas dans plates du plan. Vérifiez shot.plates et l'attribut plate du calque.`);
      }
    }
  }

  // ------------------------------------------------------------------------------------------
  // Easings
  // ------------------------------------------------------------------------------------------

  function registerCustomEases() {
    for (const name of Object.keys(SHOT.custom_eases || {})) {
      try {
        CustomEase.create(name, SHOT.custom_eases[name]);
      } catch (e) {
        throw fail(`customEase « ${name} » illisible (« ${SHOT.custom_eases[name]} ») : tracé SVG attendu, ex. "M0,0 C0.12,0.9 0.24,1 1,1".`);
      }
    }
  }

  function easeFunction(name, where) {
    const fn = typeof name === "string" && name.length ? gsap.parseEase(name) : null;
    if (typeof fn !== "function") {
      const customs = Object.keys(SHOT.custom_eases || {});
      throw fail(`Easing inconnu « ${name} » (${where}) : utilisez un ease GSAP 3 (ex. power2.out, back.out(1.7), steps(6)) ou un customEase déclaré (${customs.length ? customs.join(", ") : "aucun"}).`);
    }
    return fn;
  }

  // ------------------------------------------------------------------------------------------
  // Polices : FontFace + contrôle de couverture (aucune police de repli)
  // ------------------------------------------------------------------------------------------

  const FONT_FACES = [];

  function parseUnicodeRange(text, where) {
    const out = [];
    for (const raw of String(text).split(",")) {
      const part = raw.trim().toUpperCase();
      if (!part) {
        continue;
      }
      const m = /^U\+([0-9A-F?]{1,6})(?:-([0-9A-F]{1,6}))?$/.exec(part);
      if (!m) {
        throw fail(`unicode_range illisible « ${raw} » (${where}) : format CSS attendu, ex. U+0000-00FF,U+0131.`);
      }
      if (m[1].includes("?")) {
        out.push([parseInt(m[1].replace(/\?/g, "0"), 16), parseInt(m[1].replace(/\?/g, "F"), 16)]);
      } else {
        const lo = parseInt(m[1], 16);
        out.push([lo, m[2] ? parseInt(m[2], 16) : lo]);
      }
    }
    return out;
  }

  async function loadFonts() {
    for (const f of SHOT.fonts) {
      const where = `${f.family} ${f.weight} ${f.style}`;
      const face = new FontFace(f.family, `url("${f.url}")`, {
        weight: String(f.weight),
        style: f.style,
        unicodeRange: f.unicode_range
      });
      try {
        // load() est attendu fichier par fichier : un 404 donne un message qui nomme le fichier.
        await face.load();
      } catch (e) {
        throw fail(`Police introuvable ou illisible : ${where} (${f.url}). Vérifiez preset.fonts et lancez « npm install » (paquets @fontsource).`);
      }
      document.fonts.add(face);
      FONT_FACES.push({ family: f.family, weight: f.weight, style: f.style, ranges: parseUnicodeRange(f.unicode_range, where), face });
    }
  }

  // Vérifie qu'une (famille, graisse) est chargée ET couvre chaque caractère du texte à afficher.
  function requireFont(font, text, where) {
    const faces = FONT_FACES.filter((x) => x.family === font.family && x.weight === font.weight && x.style === "normal");
    if (faces.length === 0) {
      const known = Array.from(new Set(FONT_FACES.map((x) => `${x.family} ${x.weight}`))).join(", ");
      throw fail(`Police manquante : « ${font.family} » graisse ${font.weight} (${where}) n'est pas déclarée dans les polices du plan (${known}). Ajoutez-la à preset.fonts ou choisissez une graisse disponible ; aucune police de repli n'est utilisée.`);
    }
    for (const x of faces) {
      if (x.face.status !== "loaded") {
        throw fail(`Police non chargée : « ${font.family} » graisse ${font.weight} (${where}), état « ${x.face.status} ».`);
      }
    }
    for (const ch of String(text)) {
      if (ch === "\n" || ch === "\r") {
        continue;
      }
      const cp = ch.codePointAt(0);
      if (!faces.some((x) => x.ranges.some((r) => cp >= r[0] && cp <= r[1]))) {
        const hex = cp.toString(16).toUpperCase().padStart(4, "0");
        throw fail(`Police manquante pour le caractère « ${ch} » (U+${hex}) dans ${where} : aucun fichier de « ${font.family} » graisse ${font.weight} ne le couvre. Ajoutez le sous-ensemble @fontsource correspondant (ex. latin-ext) à preset.fonts ou retirez ce caractère.`);
      }
    }
    const probe = String(text).replace(/[\r\n]/g, "") || " ";
    const spec = `normal ${font.weight} 16px ${cssFamily(font.family)}`;
    if (!document.fonts.check(spec, probe)) {
      throw fail(`Police non disponible : document.fonts.check('${spec}') est faux pour ${where}. Vérifiez preset.fonts.`);
    }
  }

  function applyFont(node, font) {
    node.style.fontFamily = cssFamily(font.family);
    node.style.fontWeight = String(font.weight);
    node.style.fontStyle = "normal";
    node.style.fontSize = px(font.size);
    node.style.lineHeight = String(font.line_height);
    node.style.letterSpacing = `${font.tracking}em`;
  }

  // ------------------------------------------------------------------------------------------
  // Scène
  // ------------------------------------------------------------------------------------------

  function buildStage() {
    const W = SHOT.canvas.width;
    const H = SHOT.canvas.height;
    // Alpha : fond transparent partout (le compilateur refuse une couleur explicite en alpha).
    const bg = !SHOT.alpha && SHOT.background ? rgba(SHOT.background, "background du plan") : "transparent";
    document.documentElement.style.background = "transparent";
    document.body.style.background = "transparent";

    const root = document.getElementById("mg-root");
    if (!root) {
      throw fail("player.html invalide : élément #mg-root absent.");
    }
    const defsSvg = svgNode("svg", { class: "mg-defs", width: 0, height: 0, "aria-hidden": "true" });
    const defs = svgNode("defs");
    defsSvg.appendChild(defs);

    const stage = el("div", "mg-stage");
    stage.style.width = px(W);
    stage.style.height = px(H);
    stage.style.background = bg;

    const scene = el("div", "mg-scene");
    scene.style.width = px(W);
    scene.style.height = px(H);
    // Le fond est AUSSI dans le groupe isolé : les modes de fusion des calques se mélangent avec lui.
    scene.style.background = bg;

    stage.appendChild(scene);
    root.appendChild(defsSvg);
    root.appendChild(stage);
    state.stage = stage;
    state.root = root;
    state.scene = scene;
    state.defs = defs;
  }

  function glitchedLayerIds() {
    const g = SHOT.glitch;
    if (!g || !Array.isArray(g.windows) || g.windows.length === 0) {
      return new Set();
    }
    return g.layers ? new Set(g.layers) : new Set(SHOT.layers.map((L) => L.id));
  }

  function layerUsesProp(L, prop) {
    for (const k of Object.keys(L.initial || {})) {
      if (prop in L.initial[k]) {
        return true;
      }
    }
    return (L.tweens || []).some((tw) => prop in (tw.from || {}) || prop in (tw.to || {}));
  }

  function buildLayer(L, index, glitchIds) {
    const node = el("div", "mg-layer");
    node.dataset.id = L.id;
    node.style.left = px(L.x);
    node.style.top = px(L.y);
    node.style.zIndex = String(L.z);
    if (L.blend && L.blend !== "normal") {
      node.style.mixBlendMode = L.blend;
    }
    const rec = {
      spec: L,
      index,
      el: node,
      targets: { self: [node], chars: [], words: [], lines: [], shape: [], bars: [] },
      glitchEl: null,
      boilEl: null,
      textRoot: null,
      tokens: null,
      geom: null,
      media: null,
      plate: null,
      chart: null,
      pendingDecode: null
    };
    let host = node;
    // Enveloppes d'effets distinctes : flou (calque), glitch (enveloppe), boil (enveloppe interne)
    // composent proprement leurs filtres au lieu de se remplacer dans une seule propriété filter.
    if (glitchIds.has(L.id)) {
      rec.glitchEl = el("div", "mg-glitch");
      host.appendChild(rec.glitchEl);
      host = rec.glitchEl;
    }
    if (L.boil) {
      rec.boilEl = el("div", "mg-boil");
      host.appendChild(rec.boilEl);
      host = rec.boilEl;
    }
    switch (L.type) {
      case "text":
        buildText(rec, host);
        break;
      case "shape":
        buildShape(rec, host);
        break;
      case "image":
        buildImage(rec, host);
        break;
      case "sequence":
        buildSequence(rec, host);
        break;
      case "chart":
        buildChart(rec, host);
        break;
      default:
        throw fail(`Calque « ${L.id} » : type « ${L.type} » inconnu.`);
    }
    state.scene.appendChild(node);
    return rec;
  }

  // ---- Texte ---------------------------------------------------------------------------------

  // Jetons : mots (suites sans espace sécable, via Intl.Segmenter « word » fusionné), espaces, sauts.
  function tokenize(text) {
    const segmenter = new Intl.Segmenter(SEGMENT_LOCALE, { granularity: "word" });
    const tokens = [];
    let word = "";
    const flushWord = () => {
      if (word) {
        tokens.push({ type: "word", text: word });
        word = "";
      }
    };
    const pushSpace = (ch) => {
      const last = tokens[tokens.length - 1];
      if (last && last.type === "space") {
        last.text += ch;
      } else {
        tokens.push({ type: "space", text: ch });
      }
    };
    for (const s of segmenter.segment(String(text))) {
      const str = s.segment;
      if (!BREAKING_SPACE.test(str)) {
        word += str;
        continue;
      }
      // Segment d'espaces (ou mixte, par prudence) : traité caractère par caractère.
      for (const ch of str) {
        if (ch === "\n") {
          flushWord();
          tokens.push({ type: "nl" });
        } else if (ch === "\r") {
          continue;
        } else if (BREAKING_SPACE.test(ch)) {
          flushWord();
          pushSpace(ch);
        } else {
          word += ch;
        }
      }
    }
    flushWord();
    return tokens;
  }

  function graphemes(text) {
    const segmenter = new Intl.Segmenter(SEGMENT_LOCALE, { granularity: "grapheme" });
    return Array.from(segmenter.segment(text), (s) => s.segment);
  }

  function buildText(rec, host) {
    const L = rec.spec;
    const box = el("div", "mg-text");
    applyFont(box, L.font);
    // Couleur portée par le calque : une tween « color » sur self se transmet par héritage.
    rec.el.style.color = rgba(L.font.color, `calque ${L.id}`);
    box.style.textAlign = L.align || "left";
    if (L.max_width !== null && L.max_width !== undefined) {
      // Largeur bornée : retours à la ligne automatiques, sauts explicites conservés.
      box.style.maxWidth = px(L.max_width);
      box.style.whiteSpace = "pre-line";
    } else {
      // Sans largeur : jamais de retour automatique (sinon mots coupés en lignes parasites).
      box.style.whiteSpace = "pre";
    }
    host.appendChild(box);
    rec.textRoot = box;

    const split = L.split || { chars: false, words: false, lines: false };
    if (!split.chars && !split.words && !split.lines) {
      box.textContent = L.text;
      return;
    }
    const tokens = tokenize(L.text);
    for (const tok of tokens) {
      if (tok.type === "word") {
        // Le mot est toujours enveloppé : il porte la mesure de ligne et empêche toute coupure
        // entre caractères inline-block (sinon chaque caractère serait une opportunité de retour).
        const w = el("span", "mg-word");
        if (split.chars) {
          for (const g of graphemes(tok.text)) {
            const c = el("span", "mg-char");
            c.textContent = g;
            w.appendChild(c);
            rec.targets.chars.push(c);
          }
        } else {
          w.textContent = tok.text;
        }
        if (split.words) {
          rec.targets.words.push(w);
        }
        tok.el = w;
        box.appendChild(w);
      } else if (tok.type === "space") {
        tok.node = document.createTextNode(tok.text);
        box.appendChild(tok.node);
      } else {
        tok.node = document.createTextNode("\n");
        box.appendChild(tok.node);
      }
    }
    rec.tokens = tokens;
  }

  // Regroupe les mots par offsetTop APRÈS mise en page, puis reconstruit une ligne = un bloc.
  function splitLines(rec) {
    const L = rec.spec;
    const box = rec.textRoot;
    // Largeur figée : la boîte ne doit pas changer quand les lignes deviennent des blocs.
    const width = box.getBoundingClientRect().width;
    const tol = Math.max(1, L.font.size * L.font.line_height * 0.5);
    const lines = [];
    let cur = { items: [], top: null, words: 0 };
    const trimTrailingSpaces = (line) => {
      while (line.items.length) {
        const last = line.items[line.items.length - 1];
        if (last.nodeType === Node.TEXT_NODE && ONLY_BREAKING_SPACES.test(last.nodeValue)) {
          line.items.pop();
        } else {
          break;
        }
      }
    };
    for (const tok of rec.tokens) {
      if (tok.type === "nl") {
        lines.push(cur);
        cur = { items: [], top: null, words: 0 };
      } else if (tok.type === "word") {
        const top = tok.el.offsetTop;
        if (cur.top === null) {
          cur.top = top;
        } else if (top > cur.top + tol) {
          // Retour automatique : l'espace de coupure appartient à la fin de ligne, on l'enlève.
          trimTrailingSpaces(cur);
          lines.push(cur);
          cur = { items: [], top, words: 0 };
        }
        cur.items.push(tok.el);
        cur.words += 1;
      } else {
        cur.items.push(tok.node);
      }
    }
    lines.push(cur);
    box.textContent = "";
    for (const line of lines) {
      const d = el("div", "mg-line");
      for (const item of line.items) {
        d.appendChild(item);
      }
      if (line.words === 0) {
        // Ligne vide (double saut) : <br> lui garde exactement une hauteur de ligne.
        d.appendChild(document.createElement("br"));
      }
      box.appendChild(d);
      rec.targets.lines.push(d);
    }
    box.style.width = px(width);
  }

  // ---- Formes SVG ----------------------------------------------------------------------------

  function buildShape(rec, host) {
    const L = rec.spec;
    const w = Number(L.w) || 0;
    const h = Number(L.h) || 0;
    const where = `calque ${L.id}`;
    // Pas de viewBox : unités utilisateur = px CSS, ce qui reste valable pour h = 0 (trait horizontal).
    const svgEl = svgNode("svg", { class: "mg-shape", width: w, height: h });
    svgEl.style.width = px(w);
    svgEl.style.height = px(h);
    const pts = Array.isArray(L.points) ? L.points : null;
    const ptsAttr = (list) => list.map((p) => `${Number(p[0])},${Number(p[1])}`).join(" ");
    let g;
    switch (L.shape) {
      case "rect":
        g = svgNode("rect", { x: 0, y: 0, width: w, height: h, rx: L.radius || 0, ry: L.radius || 0 });
        break;
      case "ellipse":
        g = svgNode("ellipse", { cx: w / 2, cy: h / 2, rx: w / 2, ry: h / 2 });
        break;
      case "line":
        if (pts && pts.length > 2) {
          g = svgNode("polyline", { points: ptsAttr(pts) });
        } else if (pts && pts.length === 2) {
          g = svgNode("line", { x1: pts[0][0], y1: pts[0][1], x2: pts[1][0], y2: pts[1][1] });
        } else {
          g = svgNode("line", { x1: 0, y1: h / 2, x2: w, y2: h / 2 });
        }
        break;
      case "path":
        if (!L.d) {
          throw fail(`Forme « path » sans tracé (${where}) : renseignez « d » (données SVG dans le repère 0..w x 0..h).`);
        }
        g = svgNode("path", { d: L.d });
        break;
      case "polygon":
        if (!pts || pts.length < 3) {
          throw fail(`Forme « polygon » (${where}) : au moins 3 sommets attendus dans « points ».`);
        }
        g = svgNode("polygon", { points: ptsAttr(pts) });
        break;
      default:
        throw fail(`Forme inconnue « ${L.shape} » (${where}) : rect, ellipse, line, path ou polygon.`);
    }
    // pathLength = 1 : le tracé progressif s'exprime en fraction (draw 0..1) quelle que soit la forme.
    g.setAttribute("pathLength", "1");
    g.style.fill = L.shape === "line" ? "none" : paint(L.fill, where);
    g.style.stroke = paint(L.stroke, where);
    g.style.strokeWidth = px(Number(L.stroke_width) || 0);
    g.style.strokeLinecap = L.linecap || "round";
    g.style.strokeLinejoin = "round";
    if (layerUsesProp(L, "draw")) {
      // Tiret de longueur 1 puis vide de 2 : offset = 1 - draw laisse voir [0, draw] et jamais un
      // début de tiret à la fin du tracé (ce qu'un motif « 1 1 » provoquerait à draw = 0).
      g.style.strokeDasharray = "1 2";
      g.style.strokeDashoffset = "0";
      rec.drawFix = true;
    }
    svgEl.appendChild(g);
    host.appendChild(svgEl);
    rec.geom = g;
    rec.el.__mgGeom = g;
    svgEl.__mgGeom = g;
    rec.targets.shape = [svgEl];
  }

  // ---- Images et séquences -------------------------------------------------------------------

  function buildImage(rec, host) {
    const L = rec.spec;
    const img = el("img", "mg-media");
    img.alt = "";
    img.decoding = "sync";
    img.style.width = px(L.w);
    img.style.height = px(L.h);
    img.style.objectFit = L.fit || "cover";
    host.appendChild(img);
    rec.media = img;
    rec.pendingDecode = async () => {
      img.src = L.src;
      try {
        await img.decode();
      } catch (e) {
        throw fail(`Image indécodable ou introuvable : ${L.src} (calque ${L.id}). Vérifiez le chemin sous assets/ et le format (PNG, JPEG, WebP).`);
      }
    };
  }

  function plateFrameUrl(plate, q) {
    const idx = Math.max(0, Math.min(q, plate.frames - 1));
    return `${plate.url}${String(idx).padStart(6, "0")}.png`;
  }

  async function showPlateFrame(rec, q) {
    const url = plateFrameUrl(rec.plate, q);
    if (rec.media.__mgSrc === url) {
      return;
    }
    rec.media.src = url;
    rec.media.__mgSrc = url;
    try {
      // decode() garantit que l'image est prête à peindre : la capture ne voit jamais l'image précédente.
      await rec.media.decode();
    } catch (e) {
      rec.media.__mgSrc = null;
      throw fail(`Image de plaque 3D indécodable ou absente : ${url} (calque ${rec.spec.id}, plaque ${rec.spec.plate}). Rendez d'abord les plaques (pipeline/blender_render.py).`);
    }
  }

  function buildSequence(rec, host) {
    const L = rec.spec;
    const plate = SHOT.plates[L.plate];
    const img = el("img", "mg-media");
    img.alt = "";
    img.decoding = "sync";
    img.style.width = px(L.w);
    img.style.height = px(L.h);
    // La plaque est rendue au rapport du calque (éventuellement réduite par plate_scale) : fill.
    img.style.objectFit = "fill";
    host.appendChild(img);
    rec.media = img;
    rec.plate = plate;
    rec.pendingDecode = () => showPlateFrame(rec, 0);
  }

  // ---- Graphique en barres -------------------------------------------------------------------

  function buildChart(rec, host) {
    const L = rec.spec;
    const vf = L.value_format;
    const root = el("div", "mg-chart");
    root.style.width = px(L.w);
    root.style.height = px(L.h);
    host.appendChild(root);
    // Formatage figé fr-FR (virgule décimale, espace fine insécable) avec un nombre FIXE de décimales.
    const nf = new Intl.NumberFormat(NUMBER_LOCALE, {
      minimumFractionDigits: vf.decimals,
      maximumFractionDigits: vf.decimals,
      useGrouping: true
    });
    const fmt = (v) => `${vf.prefix}${nf.format(v)}${vf.suffix}`;
    const vertical = L.orientation !== "horizontal";
    const c = { root, bars: [], labels: [], values: [], labelSpans: [], valueSpans: [], fmt, vertical, ease: null, geo: null };
    const bars = [];
    const texts = [];
    L.data.forEach((d, i) => {
      const bar = el("div", "mg-bar");
      bar.style.background = rgba(d.color, `barre ${i} du calque ${L.id}`);
      const r = px(Number(L.bar_radius) || 0);
      // Arrondi côté extrémité seulement : la barre « pousse » depuis une base droite.
      bar.style.borderRadius = vertical ? `${r} ${r} 0 0` : `0 ${r} ${r} 0`;
      bar.style.transformOrigin = vertical ? "50% 100%" : "0% 50%";
      bars.push(bar);
      c.bars.push(bar);

      const lab = el("div", "mg-chart-label");
      applyFont(lab, L.label_font);
      lab.style.color = rgba(L.label_font.color, `libellé du calque ${L.id}`);
      const ls = el("span");
      ls.textContent = d.label;
      lab.appendChild(ls);
      c.labels.push(lab);
      c.labelSpans.push(ls);

      const val = el("div", "mg-chart-value");
      applyFont(val, L.value_font);
      val.style.color = rgba(L.value_font.color, `valeur du calque ${L.id}`);
      const vs = el("span");
      vs.textContent = fmt(d.value);
      val.appendChild(vs);
      c.values.push(val);
      c.valueSpans.push(vs);
      texts.push(lab, val);
    });
    // Barres d'abord, textes ensuite : un libellé n'est jamais masqué par une barre voisine.
    for (const b of bars) {
      root.appendChild(b);
    }
    for (const t of texts) {
      root.appendChild(t);
    }
    rec.chart = c;
    rec.targets.bars = bars;
  }

  function layoutChart(rec) {
    const L = rec.spec;
    const c = rec.chart;
    const n = L.data.length;
    const lf = L.label_font;
    const vfnt = L.value_font;
    const labelH = lf.size * lf.line_height;
    const valueH = vfnt.size * vfnt.line_height;
    // Marge interne proportionnelle au corps : même respiration à toute taille de graphique.
    const pad = Math.round(Math.max(lf.size, vfnt.size) * 0.3);
    const dataMax = Math.max.apply(null, L.data.map((d) => d.value));
    const maxV = L.max > 0 ? L.max : dataMax > 0 ? dataMax : 1;
    const gap = Number(L.gap) || 0;
    const ratios = L.data.map((d) => clamp(d.value / maxV, 0, 1));
    if (c.vertical) {
      const barW = Math.max(0, (L.w - gap * (n - 1)) / n);
      const plotTop = valueH + pad * 0.5;
      const plotBottom = L.h - labelH - pad;
      const plotH = Math.max(0, plotBottom - plotTop);
      for (let i = 0; i < n; i++) {
        const left = i * (barW + gap);
        const bar = c.bars[i];
        bar.style.left = px(left);
        bar.style.width = px(barW);
        bar.style.top = px(plotBottom);
        bar.style.height = "0px";
        const lab = c.labels[i];
        lab.style.left = px(left - gap / 2);
        lab.style.width = px(barW + gap);
        lab.style.top = px(plotBottom + pad);
        lab.style.height = px(labelH);
        lab.style.justifyContent = "center";
        const val = c.values[i];
        val.style.left = px(left - gap / 2);
        val.style.width = px(barW + gap);
        val.style.height = px(valueH);
        val.style.justifyContent = "center";
      }
      c.geo = { full: plotH, plotBottom, pad, valueH, ratios };
    } else {
      // Colonnes mesurées sur les textes réels (police chargée) : libellés à gauche, valeurs finales à droite.
      let labelW = 0;
      let valueW = 0;
      for (let i = 0; i < n; i++) {
        labelW = Math.max(labelW, c.labelSpans[i].getBoundingClientRect().width);
        valueW = Math.max(valueW, c.valueSpans[i].getBoundingClientRect().width);
      }
      const labelCol = labelW + pad;
      const valueCol = valueW + pad;
      const rowH = Math.max(0, (L.h - gap * (n - 1)) / n);
      const plotW = Math.max(0, L.w - labelCol - valueCol);
      for (let i = 0; i < n; i++) {
        const top = i * (rowH + gap);
        const lab = c.labels[i];
        lab.style.left = "0px";
        lab.style.width = px(labelW);
        lab.style.top = px(top);
        lab.style.height = px(rowH);
        lab.style.justifyContent = "flex-end";
        const bar = c.bars[i];
        bar.style.left = px(labelCol);
        bar.style.top = px(top);
        bar.style.height = px(rowH);
        bar.style.width = "0px";
        const val = c.values[i];
        val.style.top = px(top);
        val.style.height = px(rowH);
        val.style.width = px(valueW);
        val.style.justifyContent = "flex-start";
      }
      c.geo = { full: plotW, labelCol, pad, ratios };
    }
    c.ease = easeFunction(L.grow.ease, `grow du calque ${L.id}`);
  }

  // Progression commune barre + compteur, bornée [0, 1] : jamais de dépassement des données.
  function chartHook(rec, t) {
    const L = rec.spec;
    const c = rec.chart;
    const g = L.grow;
    const geo = c.geo;
    for (let i = 0; i < L.data.length; i++) {
      const lin = clamp((t - (g.start + i * g.stagger)) / g.dur, 0, 1);
      const p = clamp(c.ease(lin), 0, 1);
      const size = geo.full * geo.ratios[i] * p;
      if (c.vertical) {
        c.bars[i].style.top = px(geo.plotBottom - size);
        c.bars[i].style.height = px(size);
        c.values[i].style.top = px(geo.plotBottom - size - geo.pad * 0.5 - geo.valueH);
      } else {
        c.bars[i].style.width = px(size);
        c.values[i].style.left = px(geo.labelCol + size + geo.pad * 0.5);
      }
      const text = c.fmt(L.data[i].value * p);
      if (c.valueSpans[i].textContent !== text) {
        c.valueSpans[i].textContent = text;
      }
    }
  }

  // ------------------------------------------------------------------------------------------
  // Vérification des polices de tous les textes (après chargement des FontFace)
  // ------------------------------------------------------------------------------------------

  function checkAllFonts() {
    for (const L of SHOT.layers) {
      if (L.type === "text") {
        requireFont(L.font, L.text, `calque texte « ${L.id} »`);
      } else if (L.type === "chart") {
        const vf = L.value_format;
        const nf = new Intl.NumberFormat(NUMBER_LOCALE, { minimumFractionDigits: vf.decimals, maximumFractionDigits: vf.decimals, useGrouping: true });
        requireFont(L.label_font, L.data.map((d) => d.label).join(""), `libellés du graphique « ${L.id} »`);
        // Toutes les valeurs intermédiaires du compteur utilisent chiffres + séparateurs fr-FR.
        const samples = L.data.map((d) => `${vf.prefix}${nf.format(d.value)}${vf.suffix}`).join("") + nf.format(1234567.89) + "0123456789";
        requireFont(L.value_font, samples, `valeurs du graphique « ${L.id} »`);
      }
    }
  }

  // ------------------------------------------------------------------------------------------
  // Mesures (avant toute transformation GSAP)
  // ------------------------------------------------------------------------------------------

  function measureTargets() {
    for (const rec of state.layers) {
      for (const kind of Object.keys(rec.targets)) {
        for (const node of rec.targets[kind]) {
          const r = node.getBoundingClientRect();
          BOX.set(node, { w: r.width, h: r.height });
        }
      }
    }
  }

  // ------------------------------------------------------------------------------------------
  // Propriétés animées -> variables GSAP
  // ------------------------------------------------------------------------------------------

  const DIRECT_KEYS = new Set(["scaleX", "scaleY", "rotation", "skewX", "skewY", "opacity"]);
  const PERCENT = /^(-?\d+(?:\.\d+)?)%$/;

  // x/y "N%" -> px de la boîte de CHAQUE cible, mesurée au montage : une chaîne "%" passée à GSAP
  // serait interprétée comme xPercent/yPercent et casserait l'ancre.
  function axisValue(axis, v, where) {
    if (typeof v === "number") {
      return v;
    }
    const m = PERCENT.exec(String(v));
    if (!m) {
      throw fail(`Valeur ${axis} invalide « ${v} » (${where}) : nombre (px) ou "N%" attendu.`);
    }
    const f = parseFloat(m[1]) / 100;
    return (i, target) => {
      const b = BOX.get(target);
      if (!b) {
        throw fail(`Cible non mesurée pour ${axis} en % (${where}).`);
      }
      return f * (axis === "x" ? b.w : b.h);
    };
  }

  function revealInset(v, dir) {
    // reveal = fraction MASQUÉE ; revealDir = sens de découverte (right : de gauche à droite, donc
    // la partie encore masquée est à droite).
    const p = `${round(v * 100, 4)}%`;
    switch (dir) {
      case "left":
        return `inset(0% 0% 0% ${p})`;
      case "up":
        return `inset(${p} 0% 0% 0%)`;
      case "down":
        return `inset(0% 0% ${p} 0%)`;
      default:
        return `inset(0% ${p} 0% 0%)`;
    }
  }

  function elementKind(node) {
    if (node.__mgGeom) {
      return "geom";
    }
    if (node.classList && node.classList.contains("mg-bar")) {
      return "bar";
    }
    return "box";
  }

  // Sépare un jeu de propriétés en groupes (éléments, variables) : les transformations vont sur la
  // cible, draw/fill/stroke sur la géométrie SVG associée. from et to donnent les mêmes groupes.
  function routeProps(els, props, revealDir, where) {
    const kind = elementKind(els[0]);
    const main = {};
    const geom = {};
    let hasMain = false;
    let hasGeom = false;
    for (const key of Object.keys(props)) {
      const v = props[key];
      if (key === "x" || key === "y") {
        main[key] = axisValue(key, v, where);
        hasMain = true;
      } else if (DIRECT_KEYS.has(key)) {
        main[key] = Number(v);
        hasMain = true;
      } else if (key === "blur") {
        main.filter = `blur(${round(Math.max(0, Number(v)), 4)}px)`;
        hasMain = true;
      } else if (key === "reveal") {
        main.clipPath = revealInset(clamp(Number(v), 0, 1), revealDir);
        hasMain = true;
      } else if (key === "color") {
        main.color = rgba(v, where);
        hasMain = true;
      } else if (key === "draw") {
        if (kind !== "geom") {
          throw fail(`« draw » ne s'applique qu'aux formes (${where}).`);
        }
        geom.strokeDashoffset = round(1 - clamp(Number(v), 0, 1), 6);
        hasGeom = true;
      } else if (key === "fill") {
        if (kind === "geom") {
          geom.fill = rgba(v, where);
          hasGeom = true;
        } else if (kind === "bar") {
          main.backgroundColor = rgba(v, where);
          hasMain = true;
        } else {
          main.color = rgba(v, where);
          hasMain = true;
        }
      } else if (key === "stroke") {
        if (kind !== "geom") {
          throw fail(`« stroke » ne s'applique qu'aux formes (${where}).`);
        }
        geom.stroke = rgba(v, where);
        hasGeom = true;
      } else {
        throw fail(`Propriété animée inconnue « ${key} » (${where}).`);
      }
    }
    const groups = [];
    if (hasMain) {
      groups.push({ els, vars: main });
    }
    if (hasGeom) {
      groups.push({ els: els.map((n) => n.__mgGeom), vars: geom });
    }
    return groups;
  }

  function targetsOf(rec, kind) {
    return rec.targets[kind] || [];
  }

  function firstRevealDir(L, kind) {
    for (const tw of L.tweens || []) {
      if (tw.target === kind && ("reveal" in tw.from || "reveal" in tw.to)) {
        return tw.reveal_dir;
      }
    }
    return "right";
  }

  function buildTimeline() {
    // Timeline maître en pause : seul __seek la déplace (jamais le ticker ni l'horloge réelle).
    const master = gsap.timeline({ paused: true });
    for (const rec of state.layers) {
      const L = rec.spec;
      const ax = Number(L.anchor[0]);
      const ay = Number(L.anchor[1]);
      // Base : ancre par xPercent/yPercent (indépendant de la taille mesurée), x/y = décalages,
      // origine des transformations au point d'ancrage (rotation/échelle autour de l'ancre).
      gsap.set(rec.el, {
        xPercent: -ax * 100,
        yPercent: -ay * 100,
        x: 0,
        y: 0,
        rotation: Number(L.rotation) || 0,
        scaleX: Number(L.scale),
        scaleY: Number(L.scale),
        opacity: Number(L.opacity),
        transformOrigin: `${ax * 100}% ${ay * 100}%`
      });
      // État initial = from de la première tween de chaque (cible, propriété) (compilateur).
      for (const kind of Object.keys(L.initial || {})) {
        const els = targetsOf(rec, kind);
        if (!els.length) {
          continue;
        }
        const where = `état initial « ${kind} » du calque ${L.id}`;
        for (const g of routeProps(els, L.initial[kind], firstRevealDir(L, kind), where)) {
          gsap.set(g.els, g.vars);
        }
      }
      (L.tweens || []).forEach((tw, ti) => {
        const where = `tween ${ti} (${tw.target}) du calque ${L.id}`;
        const els = targetsOf(rec, tw.target);
        if (!els.length) {
          // Cible vide (ex. découpe en caractères d'un texte sans caractère) : rien à animer.
          return;
        }
        easeFunction(tw.ease, where);
        const fromGroups = routeProps(els, tw.from, tw.reveal_dir, where);
        const toGroups = routeProps(els, tw.to, tw.reveal_dir, where);
        if (fromGroups.length !== toGroups.length) {
          throw fail(`from et to n'ont pas les mêmes propriétés (${where}) : le compilateur doit produire des fromTo complets.`);
        }
        for (let k = 0; k < toGroups.length; k++) {
          const vars = Object.assign({}, toGroups[k].vars, {
            duration: Number(tw.dur),
            ease: tw.ease,
            stagger: Number(tw.stagger) || 0,
            // fromTo explicites : aucune valeur de départ lue au premier rendu (qui dépendrait de
            // l'historique des positions) ; lazy false : écriture immédiate, dans l'ordre des tweens.
            immediateRender: false,
            lazy: false,
            overwrite: false
          });
          master.fromTo(toGroups[k].els, fromGroups[k].vars, vars, Number(tw.start));
        }
      });
    }
    state.master = master;
  }

  // ------------------------------------------------------------------------------------------
  // Effets : papier, grain, vignette, balayage, bande de tracking, boil, glitch
  // ------------------------------------------------------------------------------------------

  const NEUTRAL_HIGH = new Set(["multiply", "darken", "color-burn"]);
  const NEUTRAL_LOW = new Set(["screen", "lighten", "color-dodge", "difference", "exclusion"]);

  // Papier : feTurbulence rastérisé UNE FOIS (SVG -> canvas -> blob -> img décodée) ; un filtre
  // recalculé à chaque image rendrait le plan 3 à 10 fois plus lent.
  async function makePaperUrl(paper) {
    const dsf = SHOT.device_scale_factor;
    const w = SHOT.canvas.width * dsf;
    const h = SHOT.canvas.height * dsf;
    const blend = paper.blend || "multiply";
    // Niveaux centrés sur le neutre du mode de fusion (blanc pour multiply, noir pour screen,
    // gris moyen pour overlay...) : la texture n'assombrit/éclaircit pas globalement l'image.
    let k;
    let b;
    if (NEUTRAL_HIGH.has(blend)) {
      k = 2.0;
      b = 0.0;
    } else if (NEUTRAL_LOW.has(blend)) {
      k = 2.0;
      b = -1.0;
    } else {
      k = 2.5;
      b = -0.75;
    }
    const tint = paper.color ? rgbTriplet(paper.color) : [1, 1, 1];
    const row = (c) => `${round(k * c, 6)} 0 0 0 ${round(b * c, 6)}`;
    const matrix = `${row(tint[0])} ${row(tint[1])} ${row(tint[2])} 0 0 0 0 1`;
    // Fréquence exprimée par px LOGIQUE : divisée par dsf pour que la 4K ait le même grain de papier.
    const freq = round(Number(paper.frequency) / dsf, 6);
    const seedAttr = (SEED % 9973) + 1;
    const svgText = `<svg xmlns="${SVGNS}" width="${w}" height="${h}" viewBox="0 0 ${w} ${h}">` +
      `<filter id="p" filterUnits="userSpaceOnUse" x="0" y="0" width="${w}" height="${h}" color-interpolation-filters="sRGB">` +
      `<feTurbulence type="fractalNoise" baseFrequency="${freq}" numOctaves="${Number(paper.octaves)}" seed="${seedAttr}" result="n"/>` +
      `<feColorMatrix in="n" type="matrix" values="${matrix}"/></filter>` +
      `<rect x="0" y="0" width="${w}" height="${h}" filter="url(#p)"/></svg>`;
    const svgUrl = URL.createObjectURL(new Blob([svgText], { type: "image/svg+xml" }));
    const svgImg = await decodedImage(svgUrl, "papier (SVG feTurbulence)");
    const canvas = el("canvas");
    canvas.width = w;
    canvas.height = h;
    const ctx = canvas.getContext("2d");
    ctx.drawImage(svgImg, 0, 0, w, h);
    URL.revokeObjectURL(svgUrl);
    return canvasToObjectUrl(canvas, "papier");
  }

  // Vignette statique rastérisée une fois (canvas) : même bitmap en mode opaque et alpha.
  async function makeVignetteUrl(v) {
    const dsf = SHOT.device_scale_factor;
    const w = SHOT.canvas.width * dsf;
    const h = SHOT.canvas.height * dsf;
    const canvas = el("canvas");
    canvas.width = w;
    canvas.height = h;
    const ctx = canvas.getContext("2d");
    const col = rgbTriplet(v.color || "#000000");
    const rgb = `${Math.round(col[0] * 255)}, ${Math.round(col[1] * 255)}, ${Math.round(col[2] * 255)}`;
    const softness = clamp(Number(v.softness), 0, 1);
    // Repère unité étiré au cadre : le dégradé radial devient une ellipse qui épouse le format.
    ctx.setTransform(w / 2, 0, 0, h / 2, w / 2, h / 2);
    const grad = ctx.createRadialGradient(0, 0, 0, 0, 0, Math.SQRT2);
    grad.addColorStop(0, `rgba(${rgb}, 0)`);
    grad.addColorStop(clamp(1 - softness, 0, 0.999), `rgba(${rgb}, 0)`);
    grad.addColorStop(1, `rgba(${rgb}, ${clamp(Number(v.amount), 0, 1)})`);
    ctx.fillStyle = grad;
    ctx.fillRect(-2, -2, 4, 4);
    return canvasToObjectUrl(canvas, "vignette");
  }

  // Balayage statique : lignes horizontales à pas fixe, positionnées sur des px réels entiers.
  async function makeScanlinesUrl(s) {
    const dsf = SHOT.device_scale_factor;
    const w = SHOT.canvas.width * dsf;
    const h = SHOT.canvas.height * dsf;
    const canvas = el("canvas");
    canvas.width = w;
    canvas.height = h;
    const ctx = canvas.getContext("2d");
    const col = rgbTriplet(s.color || "#000000");
    ctx.fillStyle = `rgba(${Math.round(col[0] * 255)}, ${Math.round(col[1] * 255)}, ${Math.round(col[2] * 255)}, ${clamp(Number(s.amount), 0, 1)})`;
    const step = Math.max(1, Number(s.spacing) * dsf);
    const thick = Math.max(1, Math.round(step / 2));
    for (let k = 0; ; k++) {
      const y = Math.round(k * step);
      if (y >= h) {
        break;
      }
      ctx.fillRect(0, y, w, thick);
    }
    return canvasToObjectUrl(canvas, "balayage");
  }

  // Grain : 4 tuiles générées par un PRNG DÉDIÉ, en pixels réels (cellule = size x dsf px réels).
  async function makeGrainTiles(grain) {
    const dsf = SHOT.device_scale_factor;
    const cell = Math.max(1, Math.round((Number(grain.size) || 1) * dsf));
    // Tuile d'environ 256 px logiques, taille réelle multiple de la cellule ET de dsf : la tuile
    // s'affiche à une taille CSS entière, donc à l'échelle 1:1 exacte (aucun rééchantillonnage).
    let cells = Math.floor((256 * dsf) / cell);
    while (cells > 1 && (cells * cell) % dsf !== 0) {
      cells -= 1;
    }
    const dev = cells * cell;
    const css = dev / dsf;
    const blend = grain.blend || "overlay";
    const rnd = mulberry32((SEED ^ 0x9E3779B9) >>> 0);
    const tiles = [];
    for (let k = 0; k < 4; k++) {
      const canvas = el("canvas");
      canvas.width = dev;
      canvas.height = dev;
      const ctx = canvas.getContext("2d");
      const img = ctx.createImageData(dev, dev);
      const data = img.data;
      for (let cy = 0; cy < cells; cy++) {
        for (let cx = 0; cx < cells; cx++) {
          // Bruit triangulaire [-1, 1] (somme de deux uniformes) : grain plus doux qu'un uniforme.
          const n0 = rnd() + rnd() - 1;
          const n1 = grain.monochrome === false ? rnd() + rnd() - 1 : n0;
          const n2 = grain.monochrome === false ? rnd() + rnd() - 1 : n0;
          const vals = [n0, n1, n2].map((n) => {
            if (NEUTRAL_HIGH.has(blend)) {
              return Math.round(255 - 255 * Math.abs(n));
            }
            if (NEUTRAL_LOW.has(blend)) {
              return Math.round(255 * Math.abs(n));
            }
            return Math.round(128 + 127 * n);
          });
          for (let yy = 0; yy < cell; yy++) {
            let o = ((cy * cell + yy) * dev + cx * cell) * 4;
            for (let xx = 0; xx < cell; xx++) {
              data[o] = vals[0];
              data[o + 1] = vals[1];
              data[o + 2] = vals[2];
              data[o + 3] = 255;
              o += 4;
            }
          }
        }
      }
      ctx.putImageData(img, 0, 0);
      const url = await canvasToObjectUrl(canvas, `grain ${k}`);
      // Décodage au démarrage : la tuile est prête avant la première capture.
      await decodedImage(url, `tuile de grain ${k}`);
      tiles.push({ url, css });
    }
    return { tiles, css, blend, opacity: clamp(Number(grain.amount), 0, 1) };
  }

  function grainChoice(q, css) {
    const k = hash32(q, 0x47524149) % 4;
    const cssInt = Math.max(1, Math.round(css));
    // Décalage ENTIER en px logiques = entier en px réels : le grain reste net, jamais rééchantillonné.
    const ox = hash32(q, 0x4F46585F) % cssInt;
    const oy = hash32(q, 0x4F46595F) % cssInt;
    return { k, ox, oy };
  }

  // Mode opaque : superpositions HTML au-dessus de la scène, fusionnées par mix-blend-mode.
  async function buildOverlaysOpaque(statics, grain, tracking) {
    const W = SHOT.canvas.width;
    const H = SHOT.canvas.height;
    const stage = state.stage;
    const addImg = async (item) => {
      const img = await decodedImage(item.url, item.what);
      img.className = "mg-overlay";
      img.alt = "";
      img.style.width = px(W);
      img.style.height = px(H);
      if (item.blend !== "normal") {
        img.style.mixBlendMode = item.blend;
      }
      img.style.opacity = String(item.opacity);
      stage.appendChild(img);
    };
    const paper = statics.find((s) => s.what === "papier");
    if (paper) {
      await addImg(paper);
    }
    if (grain) {
      const layers = grain.tiles.map((tile) => {
        const d = el("div", "mg-grain");
        d.style.left = px(-grain.css);
        d.style.top = px(-grain.css);
        d.style.width = px(W + 2 * grain.css);
        d.style.height = px(H + 2 * grain.css);
        // Image de fond posée UNE fois ; par image on ne change que visibilité + position.
        d.style.backgroundImage = `url("${tile.url}")`;
        d.style.backgroundSize = `${px(grain.css)} ${px(grain.css)}`;
        if (grain.blend !== "normal") {
          d.style.mixBlendMode = grain.blend;
        }
        d.style.opacity = String(grain.opacity);
        stage.appendChild(d);
        return d;
      });
      let last = null;
      state.hooks.push(async (q) => {
        if (q === last) {
          return;
        }
        last = q;
        const ch = grainChoice(q, grain.css);
        layers.forEach((d, i) => {
          d.style.visibility = i === ch.k ? "visible" : "hidden";
        });
        layers[ch.k].style.transform = `translate(${ch.ox}px, ${ch.oy}px)`;
      });
    }
    for (const s of statics) {
      if (s.what !== "papier") {
        await addImg(s);
      }
    }
    if (tracking) {
      const bandH = Number(tracking.height) * H;
      const hl = el("div", "mg-overlay");
      hl.style.width = px(W);
      hl.style.height = px(bandH);
      hl.style.background = "linear-gradient(to bottom, rgba(255,255,255,0) 0%, rgba(255,255,255,1) 50%, rgba(255,255,255,0) 100%)";
      hl.style.mixBlendMode = "screen";
      hl.style.opacity = String(round(0.25 * clamp(Number(tracking.amount), 0, 1), 4));
      stage.appendChild(hl);
      state.trackingHighlight = hl;
    }
  }

  // Mode alpha : les superpositions HTML rempliraient la transparence ; les textures passent donc
  // par un filtre SVG sur la scène, composé en « atop » (alpha final = alpha du contenu).
  function buildTextureFiltersAlpha(statics, grain) {
    const W = SHOT.canvas.width;
    const H = SHOT.canvas.height;
    const variants = grain ? grain.tiles.length : 1;
    const filters = [];
    for (let k = 0; k < variants; k++) {
      const f = svgNode("filter", {
        id: `mg-tex-${k}`,
        filterUnits: "userSpaceOnUse",
        primitiveUnits: "userSpaceOnUse",
        x: 0,
        y: 0,
        width: W,
        height: H,
        "color-interpolation-filters": "sRGB"
      });
      let cur = "SourceGraphic";
      const ordered = statics.filter((s) => s.what === "papier").concat(grain ? [null] : [], statics.filter((s) => s.what !== "papier"));
      let n = 0;
      let grainImage = null;
      for (const item of ordered) {
        let src;
        let blend;
        let opacity;
        if (item === null) {
          grainImage = svgNode("feImage", { href: grain.tiles[k].url, x: 0, y: 0, width: grain.css, height: grain.css, preserveAspectRatio: "none", result: `gi${n}` });
          f.appendChild(grainImage);
          f.appendChild(svgNode("feTile", { in: `gi${n}`, result: `gt${n}` }));
          src = `gt${n}`;
          blend = grain.blend;
          opacity = grain.opacity;
        } else {
          f.appendChild(svgNode("feImage", { href: item.url, x: 0, y: 0, width: W, height: H, preserveAspectRatio: "none", result: `si${n}` }));
          src = `si${n}`;
          blend = item.blend;
          opacity = item.opacity;
        }
        const ct = svgNode("feComponentTransfer", { in: src, result: `sa${n}` });
        ct.appendChild(svgNode("feFuncA", { type: "linear", slope: opacity, intercept: 0 }));
        f.appendChild(ct);
        f.appendChild(svgNode("feBlend", { in: `sa${n}`, in2: cur, mode: blend, result: `sb${n}` }));
        cur = `sb${n}`;
        n += 1;
      }
      f.appendChild(svgNode("feComposite", { in: cur, in2: "SourceGraphic", operator: "atop" }));
      state.defs.appendChild(f);
      filters.push({ id: f.id, grainImage });
    }
    let last = null;
    state.hooks.push(async (q) => {
      if (q === last) {
        return;
      }
      last = q;
      if (grain) {
        const ch = grainChoice(q, grain.css);
        const g = filters[ch.k].grainImage;
        g.setAttribute("x", String(ch.ox));
        g.setAttribute("y", String(ch.oy));
        state.sceneFilterParts.textures = `url(#${filters[ch.k].id})`;
      } else {
        state.sceneFilterParts.textures = `url(#${filters[0].id})`;
      }
    });
  }

  // Bande de tracking : filtre SVG sur la scène (décalage horizontal dans une bande qui défile),
  // paramètres fonctions PURES de t.
  function buildTracking(tracking) {
    const W = SHOT.canvas.width;
    const H = SHOT.canvas.height;
    const dsf = SHOT.device_scale_factor;
    const bandH = Number(tracking.height) * H;
    const period = Math.max(1e-3, Number(tracking.period));
    const amp = Number(tracking.shift) * clamp(Number(tracking.amount), 0, 1);
    const f = svgNode("filter", {
      id: "mg-tracking",
      filterUnits: "userSpaceOnUse",
      primitiveUnits: "userSpaceOnUse",
      x: 0,
      y: 0,
      width: W,
      height: H,
      "color-interpolation-filters": "sRGB"
    });
    const flood = svgNode("feFlood", { "flood-color": "#FFFFFF", "flood-opacity": 1, x: 0, y: -bandH, width: W, height: bandH, result: "band" });
    const rest = svgNode("feComposite", { in: "SourceGraphic", in2: "band", operator: "out", result: "rest" });
    const off = svgNode("feOffset", { in: "SourceGraphic", dx: 0, dy: 0, result: "shifted" });
    const inBand = svgNode("feComposite", { in: "shifted", in2: "band", operator: "in", result: "inband" });
    const merge = svgNode("feMerge");
    merge.appendChild(svgNode("feMergeNode", { in: "rest" }));
    merge.appendChild(svgNode("feMergeNode", { in: "inband" }));
    f.appendChild(flood);
    f.appendChild(rest);
    f.appendChild(off);
    f.appendChild(inBand);
    f.appendChild(merge);
    state.defs.appendChild(f);
    state.sceneFilterParts.tracking = "url(#mg-tracking)";
    state.hooks.push(async (q, t) => {
      const phase = ((t / period) % 1 + 1) % 1;
      // Positions arrondies au px RÉEL : bords de bande nets et identiques à chaque rendu.
      const y = Math.round((phase * (H + bandH) - bandH) * dsf) / dsf;
      const wobble = 0.6 * Math.sin(2 * Math.PI * t * 2.1) + 0.4 * Math.sin(2 * Math.PI * (t * 5.3 + 0.25));
      const dx = Math.round(amp * wobble * dsf) / dsf;
      flood.setAttribute("y", String(y));
      off.setAttribute("dx", String(dx));
      if (state.trackingHighlight) {
        state.trackingHighlight.style.transform = `translateY(${y}px)`;
      }
    });
  }

  async function buildTextures() {
    const T = SHOT.textures || {};
    const statics = [];
    if (T.paper) {
      statics.push({ what: "papier", url: await makePaperUrl(T.paper), blend: T.paper.blend || "multiply", opacity: clamp(Number(T.paper.amount), 0, 1) });
    }
    if (T.scanlines) {
      statics.push({ what: "balayage", url: await makeScanlinesUrl(T.scanlines), blend: "normal", opacity: 1 });
    }
    if (T.vignette) {
      statics.push({ what: "vignette", url: await makeVignetteUrl(T.vignette), blend: "normal", opacity: 1 });
    }
    const grain = T.grain ? await makeGrainTiles(T.grain) : null;
    for (const s of statics) {
      await decodedImage(s.url, s.what);
    }
    if (SHOT.alpha) {
      if (statics.length || grain) {
        buildTextureFiltersAlpha(statics, grain);
      }
    } else {
      await buildOverlaysOpaque(statics, grain, T.tracking);
    }
    if (T.tracking) {
      buildTracking(T.tracking);
    }
    // Filtre de scène composé (tracking puis textures) : une seule écriture de style par image.
    state.hooks.push(async () => {
      const value = [state.sceneFilterParts.tracking, state.sceneFilterParts.textures].filter(Boolean).join(" ");
      if (value !== state.lastSceneFilter) {
        state.scene.style.filter = value || "none";
        state.lastSceneFilter = value;
      }
    });
  }

  // Marge de région de filtre autour de la boîte du calque (débordements de glyphes et de trait).
  function filterMargin(rec, extra) {
    const L = rec.spec;
    let m = 48;
    if (L.type === "text") {
      m = Math.max(m, L.font.size * 0.6);
    } else if (L.type === "shape") {
      m = Math.max(m, (Number(L.stroke_width) || 0) * 2);
    }
    return Math.ceil(m + extra);
  }

  // Boil : turbulence + déplacement par calque, graine changée toutes les « step » images.
  function buildBoil(rec) {
    const b = rec.spec.boil;
    const box = BOX.get(rec.el);
    const amount = Math.max(0, Number(b.amount) || 0);
    const m = filterMargin(rec, amount * 2);
    const id = `mg-boil-${rec.index}`;
    const f = svgNode("filter", {
      id,
      filterUnits: "userSpaceOnUse",
      primitiveUnits: "userSpaceOnUse",
      x: -m,
      y: -m,
      width: box.w + 2 * m,
      height: box.h + 2 * m,
      "color-interpolation-filters": "sRGB"
    });
    const turb = svgNode("feTurbulence", { type: "fractalNoise", baseFrequency: Number(b.freq), numOctaves: 2, seed: 1, result: "noise" });
    // Déplacement max = amount px : feDisplacementMap déplace de scale x (C - 0.5), donc scale = 2 x amount.
    const disp = svgNode("feDisplacementMap", { in: "SourceGraphic", in2: "noise", scale: 2 * amount, xChannelSelector: "R", yChannelSelector: "G" });
    f.appendChild(turb);
    f.appendChild(disp);
    state.defs.appendChild(f);
    rec.boilEl.style.filter = `url(#${id})`;
    const step = Math.max(1, Math.floor(Number(b.step) || 1));
    let lastSeed = null;
    state.hooks.push(async (q) => {
      const s = (hash32(Math.floor(q / step), rec.index, 0x424F494C) % 100000) + 1;
      if (s !== lastSeed) {
        turb.setAttribute("seed", String(s));
        lastSeed = s;
      }
    });
  }

  // Glitch : uniquement dans les fenêtres ; séparation RVB (filtre) ou découpe (clip-path) selon
  // hash01(image, calque, graine) ; rien hors fenêtre.
  function buildGlitch(glitchRecs) {
    const g = SHOT.glitch;
    const amount = clamp(Number(g.amount), 0, 1);
    const c1 = rgbTriplet(g.colors[0]);
    const c2 = rgbTriplet(g.colors[1]);
    const maxShift = 6 + 34 * amount;
    const items = glitchRecs.map((rec) => {
      const box = BOX.get(rec.el);
      const m = filterMargin(rec, maxShift * 1.5);
      const id = `mg-glitch-${rec.index}`;
      const f = svgNode("filter", {
        id,
        filterUnits: "userSpaceOnUse",
        primitiveUnits: "userSpaceOnUse",
        x: -m,
        y: -m,
        width: box.w + 2 * m,
        height: box.h + 2 * m,
        "color-interpolation-filters": "sRGB"
      });
      const color = (c, n) => `rgb(${Math.round(c[0] * 255)}, ${Math.round(c[1] * 255)}, ${Math.round(c[2] * 255)})`;
      f.appendChild(svgNode("feFlood", { "flood-color": color(c1), "flood-opacity": c1[3], result: "f1" }));
      f.appendChild(svgNode("feComposite", { in: "f1", in2: "SourceAlpha", operator: "in", result: "s1" }));
      const o1 = svgNode("feOffset", { in: "s1", dx: 0, dy: 0, result: "o1" });
      f.appendChild(o1);
      f.appendChild(svgNode("feFlood", { "flood-color": color(c2), "flood-opacity": c2[3], result: "f2" }));
      f.appendChild(svgNode("feComposite", { in: "f2", in2: "SourceAlpha", operator: "in", result: "s2" }));
      const o2 = svgNode("feOffset", { in: "s2", dx: 0, dy: 0, result: "o2" });
      f.appendChild(o2);
      f.appendChild(svgNode("feBlend", { in: "o1", in2: "o2", mode: "screen", result: "ghost" }));
      const merge = svgNode("feMerge");
      merge.appendChild(svgNode("feMergeNode", { in: "ghost" }));
      merge.appendChild(svgNode("feMergeNode", { in: "SourceGraphic" }));
      f.appendChild(merge);
      state.defs.appendChild(f);
      return { rec, id, o1, o2, last: "" };
    });
    const fps = SHOT.fps;
    const dsf = SHOT.device_scale_factor;
    const snap = (v) => Math.round(v * dsf) / dsf;
    state.hooks.push(async (q) => {
      const tq = q / fps;
      const active = g.windows.some((w) => tq >= w[0] && tq < w[0] + w[1]);
      for (const it of items) {
        const node = it.rec.glitchEl;
        // Décalages RVB remis à zéro à CHAQUE image : l'état du filtre ne dépend que de l'image
        // courante, jamais d'une image précédemment rendue (seeks dans le désordre).
        it.o1.setAttribute("dx", "0");
        it.o2.setAttribute("dx", "0");
        if (!active) {
          node.style.filter = "";
          node.style.clipPath = "";
          node.style.transform = "";
          it.last = "";
          continue;
        }
        const idx = it.rec.index;
        const pick = hash01(q, idx, 0x4D4F4445);
        const mode = g.mode === "both" ? (pick < 0.5 ? "rgb" : "slice") : g.mode;
        if (mode === "rgb") {
          const d = snap(maxShift * (0.35 + 0.65 * hash01(q, idx, 0x52474231)));
          const jitter = snap((hash01(q, idx, 0x4A495431) - 0.5) * maxShift * 0.4);
          it.o1.setAttribute("dx", String(-d));
          it.o2.setAttribute("dx", String(d));
          node.style.clipPath = "";
          node.style.filter = `url(#${it.id})`;
          node.style.transform = `translateX(${jitter}px)`;
        } else {
          // Découpe : 1 à 3 bandes horizontales retirées (polygone débordant de la boîte pour ne
          // rien rogner d'autre), plus un décalage horizontal franc.
          const bands = 1 + Math.floor(hash01(q, idx, 0x42414E44) * 3);
          const pts = ["-50% -50%", "150% -50%"];
          const cuts = [];
          for (let k = 0; k < bands; k++) {
            const a = hash01(q, idx, 0x43555441 + k) * 90;
            const h = 2 + hash01(q, idx, 0x43555448 + k) * 10;
            cuts.push([a, Math.min(100, a + h)]);
          }
          cuts.sort((u, v) => u[0] - v[0]);
          for (const cut of cuts) {
            pts.push(`150% ${round(cut[0], 3)}%`, `-50% ${round(cut[0], 3)}%`, `-50% ${round(cut[1], 3)}%`, `150% ${round(cut[1], 3)}%`);
          }
          pts.push("150% 150%", "-50% 150%");
          const shift = snap((hash01(q, idx, 0x53484946) - 0.5) * 2 * maxShift);
          node.style.filter = "";
          node.style.clipPath = `polygon(${pts.join(", ")})`;
          node.style.transform = `translateX(${shift}px)`;
        }
        it.last = mode;
      }
    });
  }

  // Tracé progressif : à draw = 0 on masque le trait (un tiret de longueur nulle peut laisser un
  // point de capuchon rond) ; à draw = 1 on retire les tirets (angles fermés avec leur jointure).
  function buildDrawFix() {
    const geoms = state.layers.filter((r) => r.drawFix).map((r) => r.geom);
    if (!geoms.length) {
      return;
    }
    state.hooks.push(async () => {
      for (const g of geoms) {
        const off = parseFloat(g.style.strokeDashoffset);
        const draw = 1 - (Number.isFinite(off) ? off : 0);
        g.style.strokeOpacity = draw <= 1e-4 ? "0" : "";
        g.style.strokeDasharray = draw >= 1 - 1e-6 ? "none" : "1 2";
      }
    });
  }

  // ------------------------------------------------------------------------------------------
  // __seek
  // ------------------------------------------------------------------------------------------

  async function doSeek(frame, sub) {
    if (!state.ready) {
      throw fail("__seek appelé avant __MOGRAPH_READY__ : attendez le drapeau READY.");
    }
    const f = Number(frame);
    const s = sub === undefined || sub === null ? 0 : Number(sub);
    if (!Number.isInteger(f) || f < 0) {
      throw fail(`__seek : numéro d'image invalide (${frame}) : entier >= 0 attendu.`);
    }
    if (!Number.isFinite(s)) {
      throw fail(`__seek : sous-image invalide (${sub}).`);
    }
    const q = Math.floor(f / SHOT.frame_step) * SHOT.frame_step;
    // t < 0 (sous-image du flou avant la première image) : l'état avant 0 est l'état à 0.
    const t = Math.max(0, (q + s) / SHOT.fps);
    window.__MOGRAPH_CLOCK__.set(t * 1000);
    // Rendu repeint à neuf : display none + remise en page forcée détruit les objets de mise en page
    // et leurs listes d'affichage en cache ; sans cela, Chromium réutilise des peintures d'images
    // précédentes et une même image diffère de ±2 niveaux selon l'ordre des seeks (mesuré sur la
    // bascule filtre/découpe du glitch). Le DOM, lui, est déjà identique quel que soit l'historique.
    state.root.style.display = "none";
    void state.root.offsetHeight;
    state.root.style.display = "";
    void state.root.offsetHeight;
    // Repasser par 0 rend l'état indépendant de l'historique des positions demandées.
    state.master.seek(0, false);
    state.master.seek(t, false);
    for (const hook of state.hooks) {
      await hook(q, t);
    }
    // Animations CSS éventuelles : mises en pause et placées sur le temps virtuel.
    for (const a of document.getAnimations()) {
      a.pause();
      a.currentTime = t * 1000;
    }
    // Double rAF : le style est recalculé et une image est effectivement produite avant la capture.
    await nextFrame();
    await nextFrame();
    return { frame: f, q, t };
  }

  let seekChain = Promise.resolve();
  window.__seek = function (frame, sub) {
    // Sérialisation : deux seeks ne s'entrelacent jamais (hooks asynchrones).
    const p = seekChain.then(() => doSeek(frame, sub));
    seekChain = p.catch(() => undefined);
    return p;
  };

  // ------------------------------------------------------------------------------------------
  // __layout
  // ------------------------------------------------------------------------------------------

  function parseInsetFractions(value, node) {
    // Valeur calculée de clip-path : "inset(a b c d)" (px ou %), on ramène tout en fractions.
    const m = /^inset\(([^)]*)\)/.exec(value);
    if (!m) {
      return null;
    }
    const parts = m[1].replace(/\s+round\s.*$/, "").trim().split(/\s+/);
    while (parts.length < 4) {
      parts.push(parts.length === 1 ? parts[0] : parts.length === 2 ? parts[0] : parts[1]);
    }
    const b = BOX.get(node) || { w: node.offsetWidth || 1, h: node.offsetHeight || 1 };
    const frac = (token, size) => {
      const v = parseFloat(token);
      if (!Number.isFinite(v)) {
        return 0;
      }
      return token.endsWith("%") ? v / 100 : size > 0 ? v / size : 0;
    };
    return {
      top: frac(parts[0], b.h),
      right: frac(parts[1], b.w),
      bottom: frac(parts[2], b.h),
      left: frac(parts[3], b.w)
    };
  }

  function nodeVisibility(node, cache) {
    // Opacité effective (produit des ancêtres jusqu'à la scène) + rectangle de découpe reveal.
    let opacity = 1;
    let clip = null;
    let n = node.nodeType === Node.ELEMENT_NODE ? node : node.parentElement;
    while (n && n !== state.stage) {
      let info = cache.get(n);
      if (!info) {
        const cs = getComputedStyle(n);
        let o = parseFloat(cs.opacity);
        if (!Number.isFinite(o)) {
          o = 1;
        }
        if (cs.visibility === "hidden" || cs.display === "none") {
          o = 0;
        }
        let rect = null;
        if (cs.clipPath && cs.clipPath.startsWith("inset(")) {
          const fr = parseInsetFractions(cs.clipPath, n);
          if (fr) {
            const r = n.getBoundingClientRect();
            rect = [r.left + fr.left * r.width, r.top + fr.top * r.height, r.right - fr.right * r.width, r.bottom - fr.bottom * r.height];
          }
        }
        info = { o, rect };
        cache.set(n, info);
      }
      opacity *= info.o;
      if (info.rect) {
        clip = clip ? [Math.max(clip[0], info.rect[0]), Math.max(clip[1], info.rect[1]), Math.min(clip[2], info.rect[2]), Math.min(clip[3], info.rect[3])] : info.rect.slice();
      }
      n = n.parentElement;
    }
    return { opacity, clip };
  }

  function measureTextBox(root, cache) {
    const W = SHOT.canvas.width;
    const H = SHOT.canvas.height;
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
    const range = document.createRange();
    let box = null;
    let minOpacity = 1;
    for (let node = walker.nextNode(); node; node = walker.nextNode()) {
      if (!node.nodeValue || !node.nodeValue.trim()) {
        continue;
      }
      const vis = nodeVisibility(node, cache);
      if (vis.opacity < 0.9) {
        continue;
      }
      range.selectNodeContents(node);
      // getClientRects d'une Range : boîtes des glyphes APRÈS transformations CSS, en px logiques.
      for (const r of Array.from(range.getClientRects())) {
        if (r.width <= 0 || r.height <= 0) {
          continue;
        }
        let x0 = r.left;
        let y0 = r.top;
        let x1 = r.right;
        let y1 = r.bottom;
        if (vis.clip) {
          x0 = Math.max(x0, vis.clip[0]);
          y0 = Math.max(y0, vis.clip[1]);
          x1 = Math.min(x1, vis.clip[2]);
          y1 = Math.min(y1, vis.clip[3]);
        }
        // Hors cadre = invisible (la scène découpe) : on ne garde que la partie dans le canevas.
        x0 = Math.max(x0, 0);
        y0 = Math.max(y0, 0);
        x1 = Math.min(x1, W);
        y1 = Math.min(y1, H);
        if (x1 - x0 <= 0 || y1 - y0 <= 0) {
          continue;
        }
        box = box ? [Math.min(box[0], x0), Math.min(box[1], y0), Math.max(box[2], x1), Math.max(box[3], y1)] : [x0, y0, x1, y1];
        minOpacity = Math.min(minOpacity, vis.opacity);
      }
    }
    if (!box) {
      return null;
    }
    return {
      box: [round(box[0], 3), round(box[1], 3), round(box[2] - box[0], 3), round(box[3] - box[1], 3)],
      opacity: round(minOpacity, 4)
    };
  }

  function truncate(text) {
    const chars = Array.from(String(text));
    return chars.length > 80 ? chars.slice(0, 80).join("") : chars.join("");
  }

  window.__layout = function () {
    if (!state.ready) {
      throw fail("__layout appelé avant __MOGRAPH_READY__.");
    }
    const cache = new Map();
    const out = [];
    for (const rec of state.layers) {
      const L = rec.spec;
      if (!L.safe) {
        continue;
      }
      if (L.type === "text") {
        const m = measureTextBox(rec.textRoot, cache);
        if (m) {
          out.push({ id: L.id, text: truncate(L.text), box: m.box, opacity: m.opacity });
        }
      } else if (L.type === "chart") {
        const c = rec.chart;
        for (let i = 0; i < L.data.length; i++) {
          const ml = measureTextBox(c.labels[i], cache);
          if (ml) {
            out.push({ id: `${L.id}.label.${i}`, text: truncate(L.data[i].label), box: ml.box, opacity: ml.opacity });
          }
          const mv = measureTextBox(c.values[i], cache);
          if (mv) {
            out.push({ id: `${L.id}.value.${i}`, text: truncate(c.valueSpans[i].textContent), box: mv.box, opacity: mv.opacity });
          }
        }
      }
    }
    return out;
  };

  // ------------------------------------------------------------------------------------------
  // Démarrage
  // ------------------------------------------------------------------------------------------

  function reportError(e) {
    if (typeof window.__MOGRAPH_ERROR__ === "string") {
      return;
    }
    let message;
    if (e instanceof MographError) {
      message = e.message;
    } else {
      const detail = e && e.stack ? String(e.stack).split("\n").slice(0, 3).join(" | ") : String(e);
      message = `Erreur interne du runtime (${e && e.name ? e.name : "Error"}) : ${e && e.message ? e.message : String(e)} [${detail}]`;
    }
    window.__MOGRAPH_ERROR__ = message;
  }

  async function boot() {
    try {
      validateShot();
      if (typeof window.gsap === "undefined") {
        throw fail("GSAP absent : /node_modules/gsap/dist/gsap.min.js n'a pas été chargé (npm install ?).");
      }
      if (typeof window.CustomEase === "undefined") {
        throw fail("CustomEase absent : /node_modules/gsap/dist/CustomEase.min.js n'a pas été chargé (npm install ?).");
      }
      gsap.registerPlugin(CustomEase);
      // force3D false : matrices 2D, pas de calques composités GPU dont l'arrondi peut varier.
      gsap.config({ force3D: false, nullTargetWarn: false });
      // lagSmoothing(0) : GSAP ne « rattrape » jamais un retard d'horloge, le temps est celui imposé.
      gsap.ticker.lagSmoothing(0);
      registerCustomEases();

      await loadFonts();
      checkAllFonts();
      buildStage();

      const glitchIds = glitchedLayerIds();
      SHOT.layers.forEach((L, i) => {
        state.layers.push(buildLayer(L, i, glitchIds));
      });

      await document.fonts.ready;
      // Images et première image de chaque plaque décodées AVANT de déclarer la page prête.
      for (const rec of state.layers) {
        if (rec.pendingDecode) {
          await rec.pendingDecode();
        }
      }

      // Mise en page dépendante des polices chargées : lignes, graphiques, puis mesures.
      for (const rec of state.layers) {
        if (rec.spec.type === "text" && rec.tokens && rec.spec.split && rec.spec.split.lines) {
          splitLines(rec);
        }
        if (rec.spec.type === "chart") {
          layoutChart(rec);
        }
      }
      measureTargets();

      // Hooks par image, dans un ordre fixe.
      for (const rec of state.layers) {
        if (rec.spec.type === "chart") {
          state.hooks.push(async (q, t) => chartHook(rec, t));
        } else if (rec.spec.type === "sequence") {
          state.hooks.push(async (q) => showPlateFrame(rec, q));
        }
      }
      for (const rec of state.layers) {
        if (rec.boilEl) {
          buildBoil(rec);
        }
      }
      const glitchRecs = state.layers.filter((r) => r.glitchEl);
      if (glitchRecs.length) {
        buildGlitch(glitchRecs);
      }
      buildDrawFix();
      await buildTextures();

      buildTimeline();
      // Ticker endormi APRÈS la construction (gsap.set/fromTo le réveillent) : plus aucun rendu
      // GSAP spontané, seul __seek fait avancer l'animation.
      gsap.ticker.sleep();

      state.ready = true;
      await window.__seek(0, 0);
      window.__MOGRAPH_RUNTIME__ = Object.freeze({
        contract: SHOT.contract,
        shot_id: SHOT.shot_id,
        layers: state.layers.map((r) => r.spec.id),
        device_pixel_ratio: window.devicePixelRatio
      });
      window.__MOGRAPH_READY__ = true;
    } catch (e) {
      state.ready = false;
      reportError(e);
    }
  }

  boot();
})();
