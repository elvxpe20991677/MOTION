# mograph — scène JSON → vidéo motion design déterministe

Transforme une scène JSON en vidéo motion design rendue **image par image, de façon
déterministe**, exportée en **HEVC Main 10 (`hvc1`)** et **ProRes 422 HQ** (ou **4444** avec alpha),
puis contrôlée automatiquement.

```
scène.json ──► compilateur ──► plans compilés ──► Chromium headless + GSAP ──► PNG par plan ─┐
   │            (validation,        │                (runtime/, horloge virtuelle)            │
   │             temps, easings)    └──► tâches Blender ──► bpy / Cycles ──► plaques PNG ─────┤
   │                                                         (calques <img> de la page web)   │
   └──► preset (DA)                                                                           ▼
                   QC ◄── FFmpeg (zscale Rec.709, x265 / prores_ks) ◄── séquence maître (fondus)
```

- **Web natif (Chromium + GSAP)** : colonne vertébrale 2D **et** compositeur final.
- **Blender (bpy 5.0.1, Cycles)** : plaques 3D intégrées comme calques de la page.
- **FFmpeg** : seul maillon de sortie (conversion couleur, encodage, contrôle).
- **Un seul format de scène JSON.** DaVinci Resolve : finition et scopes, hors boucle.

Documents : `docs/SPEC.md` (mission), `docs/CONTRACT.md` (décisions et API entre modules, normatif),
`QC_CHECKLIST.md` (contrôle humain).

---------------------------------------------------------------------------------------------------
## 1. Installation

### Windows 11 (machine de référence, natif — sans WSL)
Prérequis : git, Python 3.12, Node.js (≥ 22), uv, FFmpeg ≥ 6.1 compilé avec libx265, prores_ks et
**libzimg** (le build « full » de gyan.dev convient : `winget install Gyan.FFmpeg`).

```powershell
git clone <dépôt> mograph ; cd mograph
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m playwright install chromium
npm ci
uv venv --python 3.11 .venv-blender
uv pip install --python .venv-blender\Scripts\python.exe bpy==5.0.1
```

### Ubuntu 24.04 / WSL2 / macOS
```bash
sudo apt install -y git curl build-essential tmux mediainfo mpv ffmpeg \
  libxrender1 libxxf86vm1 libxfixes3 libxi6 libxkbcommon0 libsm6 libgl1 libegl1
python3.12 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python -m playwright install --with-deps chromium
npm ci
curl -LsSf https://astral.sh/uv/install.sh | sh
uv venv --python 3.11 .venv-blender && uv pip install --python .venv-blender/bin/python bpy==5.0.1
```
(macOS : `brew install git tmux mediainfo mpv ffmpeg node uv python@3.12`.)

### Vérifications rapides
```bash
ffmpeg -hide_banner -filters | grep -E " zscale | premultiply | alphaextract | blackdetect | ebur128 "
.venv-blender/bin/python -c "import bpy; print(bpy.app.version_string)"   # 5.0.1 (Windows : .venv-blender/Scripts/python.exe)
```
Les scripts `tests/verify_stepNN_*.py` rejouent la vérification de chaque étape :
```bash
.venv/bin/python tests/verify_step11_compiler.py      # Windows : .venv/Scripts/python.exe
```
FFmpeg 6.1.1 (Ubuntu 24.04) convient : ses conversions d'alpha inexactes (swscale, zscale) sont
contournées par l'encodeur (voir `docs/CONTRACT.md` §7), vérifié par `tests/verify_step15_encode.py`.

Versions figées : `requirements.txt` (pip), `package.json` + `package-lock.json` (npm),
`bpy==5.0.1`. Toute mise à jour peut changer des pixels : refaire la porte de déterminisme.

---------------------------------------------------------------------------------------------------
## 2. Commandes

`<scène>` = chemin d'un `.json` ou id d'une scène de `scenes/`. Sous Windows, utiliser
`.venv\Scripts\python.exe mograph.py …` (ou activer le venv).

| Commande | Rôle |
|---|---|
| `python mograph.py validate <scène>` | valide + compile ; affiche format, images, plans, fondus, plaques |
| `python mograph.py render <scène> [--workers N] [--detach]` | plaques 3D puis images web |
| `python mograph.py plates <scène> [--detach]` | plaques 3D uniquement |
| `python mograph.py determinism <scène> [--count N]` | porte de déterminisme (bloquante) |
| `python mograph.py compose <scène>` | séquence maître `build/<scène>/master/` |
| `python mograph.py encode <scène>` | maître + tous les livrables + `encode_commands.sh` |
| `python mograph.py qc <scène>` | QC automatique, code 1 si NON CONFORME |
| `python mograph.py all <scène> [--detach]` | plaques → web → déterminisme → maître → encodage → QC |
| `python mograph.py synth-beat --bpm 120 --duration 5 --out assets/audio/beat.wav` | piste test |
| `python mograph.py status <scène>` | avancement (y compris d'un rendu détaché) |
| `python mograph.py versions <scène>` | archive les versions dans `out/<scène>/versions.txt` |

### Rendus longs
`--detach` relance la commande dans un processus **indépendant du terminal** (Windows :
`DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_BREAKAWAY_FROM_JOB` ; Linux/macOS :
`setsid`), journal dans `build/<scène>/logs/<commande>-<date>.log`. Sous Linux, `tmux` reste une
alternative : `tmux new -d -s rendu "python mograph.py all <scène>"`.

**Reprise** : tout est image par image et écrit de façon atomique (`.tmp` puis renommage). Relancer
la même commande saute les images déjà faites (plans web : manifeste + empreintes ; plaques : PNG
existants). Un plan dont la définition compilée change repart de zéro.

### Sorties
- `build/<scène>/` : `compiled.json`, `shots/<plan>/NNNNNN.png + manifest.json`,
  `plates/<plan>__<plaque>/`, `master/`, `determinism.json`, `logs/`.
- `out/<scène>/` : `<scène><suffixe>_<profil>.mp4|.mov`, `encode_commands.sh`,
  `qc_report.json`, `qc_report.md`, `versions.txt`.
- `MOGRAPH_BUILD_DIR` / `MOGRAPH_OUT_DIR` déplacent `build/` et `out/` (disque plus grand).

---------------------------------------------------------------------------------------------------
## 3. Format de scène (`schema/scene.schema.json`, draft 2020-12)

```jsonc
{
  "version": "1.0", "id": "ma_scene", "title": "…", "brief": "…", "seed": 7,
  "preset": "flat_riso", "preset_overrides": { "palette": { "accent": "#FFB000" } },
  "format":  { "aspect": "16:9", "resolution": "1080p", "fps": 25, "alpha": false },
  "quality": { "plate_scale": 1, "blender_samples": 128,
               "motion_blur": { "enabled": true, "samples": 8, "shutter": 0.5 } },
  "audio":   { "src": "assets/audio/beat_120.wav", "bpm": 120, "offset": 0,
               "markers": [ { "id": "drop", "time": 2.0 } ], "synth": { "kind": "beat" } },
  "storyboard": [ { "shot": "s1", "intent": "…", "notes": "…" } ],
  "outputs": [ { "profile": "hevc_main10", "crf": 18 }, { "profile": "prores_422hq" } ],
  "qc": { "safe_zone": "title", "safe_zone_tolerance_s": 0.5, "allow_black_s": 0,
          "determinism_frames": 8, "loudness_target_lufs": -14 },
  "shots": [ { "id": "s1", "duration": 3.4, "background": "bg",
               "transition_in": "cut", "fx": {}, "plates": [], "layers": [] } ]
}
```

### Temps
- **nombre** = secondes **locales** au plan ;
- `"beat:N"` (N décimal, `beat:0` = `audio.offset`) ou `"marker:id"`, avec décalage `+x`/`-x`
  = temps **global** (converti en local par le compilateur) ;
- durées : secondes, `"beats:N"`, ou `short|medium|long` du preset ;
- `durée × fps` doit être **entier** (sinon refus avec valeurs proches proposées).

### Plans et transitions
`transition_in` : `cut` ou `crossfade` (ou `{ "type": "crossfade", "dur": 0.4 }`). Un fondu
**chevauche** les deux plans : total = Σ images − Σ images de fondu. Le mélange se fait en alpha
prémultiplié avec un poids smoothstep.

### Calques (communs)
`id, type (text|shape|image|sequence|chart), x, y, anchor, z, rotation, scale, opacity, blend,
safe, boil, anim`.
- Positions : px logiques, `"50%"` du canevas, `"safe:50%"` / `"action:50%"` de la zone sûre.
- Canevas logiques : 16:9 = 1920×1080, 9:16 = 1080×1920, 1:1 = 1080×1080 ; la 4K est la même
  page rastérisée à `devicePixelRatio` 2.
- `text` : `text, style (display|body), size, weight, color, align, maxWidth, lineHeight, tracking,
  case, split`. Sans `maxWidth`, jamais de retour à la ligne automatique.
- `shape` : `shape (rect|ellipse|line|path|polygon), w, h, radius, fill, stroke, strokeWidth, d, points`.
- `image` : `src (assets/…), w, h, fit` · `sequence` : `plate, w, h` (plaque 3D du plan).
- `chart` : `data [{label, value, color}], w, h, max, gap, barRadius, colors, valueFormat,
  labelStyle, valueStyle, grow {at, dur, stagger, ease}` — barres et compteurs partagent le même
  easing, sans dépassement (back/elastic refusés pour `grow`).

### Tweens (`anim`)
`move, at, dur, from, to, ease, stagger, target (self|chars|words|lines|shape|bars), revealDir,
allowLinear, sync (start|end)`.
- `move` développe un mouvement nommé du preset ; `dur` le remet à l'échelle.
- `sync: "end"` : la **fin de la première étape** tombe sur `at` (impact sur le temps musical).
- Propriétés : `x, y` (décalages px ou `"N%"` de la cible), `scale, scaleX, scaleY, rotation,
  skewX, skewY, opacity, draw` (tracé 0..1), `blur` (px), `reveal` (0..1 masqué), `color, fill, stroke`.
- Easing : alias du preset (`default, enter, exit, emphasis, anticipate, settle`), CustomEase du
  preset, ou GSAP (`power2.out`, `back.out(1.7)`, `steps(6)`…). **Linéaire refusé** sauf
  `allowLinear: true`.
- Le compilateur produit des `fromTo` explicites et continus (condition du déterminisme).

### Plaques 3D (`plates`)
`id, transparent, world {color, strength, sky}, camera {location, target, lens, fstop, anim},
lights [AREA|SUN|POINT|SPOT], objects [{id, primitive (sphere|cube|rounded_cube|torus|cylinder|
cone|plane|cyclorama), size, minor, bevel, location, rotation (degrés), scale, material, anim}]`.
Canaux : `{prop: location|rotation|scale|target, keys: [{t, v, ease}]}` — `ease` s'applique au
segment qui **arrive** sur la clé. Afficher une plaque : calque `{"type": "sequence", "plate": "<id>"}`.

### Effets (`fx` par plan)
Surcharges des textures du preset (`grain, paper, vignette, scanlines, tracking` : `false`,
nombre = intensité, ou objet) et `glitch {windows [[at, dur]], on_beats {from, to, every, length},
amount, colors, mode, layers}`.

### Livrables
`hevc_main10` (.mp4, `hvc1`, yuv420p10le), `prores_422hq` (.mov, timecode 01:00:00:00),
`prores_4444` (.mov, alpha 16 bits, **obligatoire** si `format.alpha`). Une sortie opaque d'une
scène alpha est aplatie sur noir.

---------------------------------------------------------------------------------------------------
## 4. Presets (`presets/`, `schema/preset.schema.json`)

| Preset | Usage | Palette | Polices |
|---|---|---|---|
| `flat_riso` | flat 2D, infographie, dataviz, explainer | #EEF1F6 #1B1F4A #2F5BEA #FF4F9A #FFC93C | Bricolage Grotesque 800 + DM Sans 500 |
| `kinetic_signal` | typo cinétique | #2A1FE6 #FFFFFF #FF4B23 | Anton (capitales) + Inter Tight 600 |
| `vhs_glitch` | rétro VHS, glitch | #0D0A1C #37F2FF #FF2BD1 #FFB21C | VT323 + Space Mono |
| `handmade_chalk` | craie, fait main (on twos) | #26352C #F1EEE4 #F4D35E #F28FAD #8FC9EA | Caveat 700 + Patrick Hand |
| `clay_3d` | 3D pâte à modeler (Blender) | #E9E4FF #8C7BFF #FF9F7A #62D2B4 #FFD66B | Fraunces 800 + DM Sans 500 |
| `photoreal_3d` | 3D produit (Blender) | #1E2024 #C9CED6 #D4A373 | Inter Tight 300/600 |

Dériver un preset : copier le JSON, changer `id`, ou utiliser `preset_overrides` dans la scène.

---------------------------------------------------------------------------------------------------
## 5. Déterminisme

Horloge virtuelle (Date / performance.now), `Math.random` graine (mulberry32), timeline GSAP en
pause positionnée par `seek`, `fromTo` explicites, effets aléatoires fonction de l'image, textures
cuites une fois et décodées avant capture, Chromium en rendu logiciel (SwiftShader) avec les
arguments de `pipeline/config.py`. La porte `python mograph.py determinism <scène>` re-rend des
images témoins **isolées, dans le désordre, dans un navigateur neuf** et exige 100 % d'empreintes
identiques. Garantie valable sur une même machine et une même installation : refaire la porte sur
la machine de rendu final.

---------------------------------------------------------------------------------------------------
## 6. Exploitation au quotidien
1. brief → storyboard (intention par plan, champ `storyboard`) ;
2. choisir un preset (ou en dériver un) ;
3. écrire la scène JSON, puis `validate` ;
4. préversion : `quality.plate_scale: 0.5`, peu d'échantillons, quelques images témoins ;
5. rendu final (`all --detach`) ;
6. QC automatique + `QC_CHECKLIST.md` ;
7. finition éventuelle dans DaVinci Resolve ;
8. livraison. Archivage : `out/<scène>/versions.txt` (écrit par `all`) puis commit de la scène et
   du preset dans git.

---------------------------------------------------------------------------------------------------
## 7. Limites connues
- Source web en 8 bits par canal (16 bits seulement avec le flou de mouvement).
- Pas de P3 ni de HDR : chaîne sRGB → Rec.709 SDR uniquement.
- EEVEE et Grease Pencil exigent un GPU (EEVEE sans GPU : 68 s pour un cube) : le pipeline utilise Cycles.
- Zones sûres 9:16 approximatives (interfaces TikTok / Reels / Shorts évolutives).
- Pas de détection automatique des temps musicaux (BPM et marqueurs saisis ; évolution : librosa).
- Rendu logiciel lent sans GPU (Chromium SwiftShader, Cycles CPU).
- Windows : pas de `tmux` ; `--detach` le remplace. `--font-render-hinting=none` n'a d'effet que
  sous Linux ; le déterminisme est garanti par machine, pas entre systèmes.
