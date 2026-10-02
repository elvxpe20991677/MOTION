# CONTRAT D'INTERFACES — mograph

Ce document fait foi pour l'implémentation. `docs/SPEC.md` décrit la mission ; ce fichier fixe
les décisions techniques, les API entre modules et les adaptations à la machine réelle.
Les schémas JSON dans `schema/` et `schema/internal/` sont normatifs.

---------------------------------------------------------------------------------------------------
## 0. Machine et environnement RÉELS (mesurés)

| Élément | Valeur |
|---|---|
| OS | Windows 11 Famille 10.0.26200, **natif** (pas de WSL : son installation exige admin + redémarrage) |
| CPU / RAM | AMD Ryzen 9 7845HX, 12 cœurs / 24 threads, 64 Go |
| GPU | NVIDIA GeForce RTX 4060 Laptop 8 Go, pilote 616.92 (OptiX fonctionne dans bpy) |
| Disque | C: 321,6 Go (= 299,5 Gio) libres le 30/09/2026, mesuré par `Get-PSDrive` (98,6 Go à l'intégration ; build/ déplaçable via `MOGRAPH_BUILD_DIR`) |
| FFmpeg | **9.0.2** build gyan.dev « full » (`winget install Gyan.FFmpeg` ; 8.1.2 à l'intégration) : libx265, prores_ks, libzimg/zscale, premultiply, alphaextract, blackdetect, ebur128 |
| Python orchestrateur | 3.12.10 — venv `.venv` → **`.venv/Scripts/python.exe`** |
| Paquets pip | playwright 1.56.0, jsonschema 4.23.0, opencv-python-headless 4.12.0.88, numpy 2.2.6 (`requirements.txt`) ; dépendances indirectes figées par `constraints.txt` (phase 1) |
| Chromium | Chromium Headless Shell 141.0.7390.37 (playwright build v1194) |
| Node / npm | 24.19.0 / 12.0.2 (sert uniquement à récupérer des fichiers figés : sans effet sur les pixels) |
| npm | gsap 3.15.0, @fontsource/* 5.3.0 (`package.json` + `package-lock.json`) |
| Blender | bpy 5.0.1 sous CPython 3.11.15 (uv 0.11.7) — venv `.venv-blender` → **`.venv-blender/Scripts/python.exe`** |
| Référence Cycles | cube 540², 16 éch., OIDN : **1,09 s/image CPU (24 threads)**, 2,10 s OptiX (surcoût d'init sur scène triviale) |
| Baseline | `docs/BASELINE.md` + `docs/baseline/phase-0.json` (versions exactes, tests, empreintes de `demo_flat_riso`), rejouable par `scripts/baseline.py` |

### Machine 2 : conteneur Linux (reprise du travail, étapes 18 à 20)
| Élément | Valeur |
|---|---|
| OS | Ubuntu 24.04.4 LTS (noyau 6.18), sans GPU |
| CPU / RAM / disque | Intel Xeon 2,10 GHz, 4 cœurs, 16 Go, ~28 Go libres (le minimum de la SPEC) |
| FFmpeg | **6.1.1** (paquet Ubuntu), libzimg 3.0.5 : défauts d'alpha contournés (§7), plage ProRes lue sur l'image (§8) |
| Python / Node | 3.12.3 (`.venv/bin/python`) / Node 22.22.2, npm 10.9.7 ; mêmes versions pip/npm figées |
| Chromium | 141.0.7390.37 (playwright build v1194, identique à la machine 1) |
| Blender | bpy 5.0.1 sous CPython 3.11 (`.venv-blender/bin/python`), Cycles CPU |
| Référence Cycles | scène clay 540², 24 éch., flou 0,5 : **~5 s/image (4 threads)** ; web 1080p : 0,37 s/image (4 navigateurs) ; plaque 1080², 96 éch. : ~56 s/image |

Le déterminisme est garanti PAR machine (§6) : la porte de l'étape 19 est rejouée ici.

Constat bpy : `world.color` seul n'est PAS rendu (fond gris sombre) ; le monde doit être construit
avec un arbre de nœuds (Background / Sky Texture).

### Adaptations de plateforme (équivalences)
- `tmux` / `setsid` → processus Windows détachés : `subprocess.Popen(..., creationflags=
  DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_BREAKAWAY_FROM_JOB)` (repli sans BREAKAWAY
  si le job l'interdit), stdout/stderr vers un fichier journal, fichier PID. Sous Linux/macOS :
  `start_new_session=True` (équivalent de setsid). Implémenté dans `mograph.py` (option `--detach`).
- `.venv/bin/python` → `.venv/Scripts/python.exe` ; toujours passer par `pipeline.config`
  (`config.BLENDER_PYTHON`, `config.ORCHESTRATOR_PYTHON`).
- Liens physiques : `os.link` (NTFS les supporte, même volume obligatoire).
- `--font-render-hinting=none` est sans effet sous Windows (DirectWrite) : le déterminisme reste
  garanti sur une même machine (c'est ce que teste la porte de l'étape 19).
- Outils de shell : l'outil Bash est Git Bash (POSIX), PowerShell 5.1 est aussi disponible.
- `mediainfo`, `mpv`, Blender (appli) : absents (recommandés, non bloquants). DaVinci Resolve
  Studio 21.1 est installé (contrôle humain).

---------------------------------------------------------------------------------------------------
## 1. Conventions de code (toutes obligatoires)

1. Code complet et exécutable ; aucun `TODO`, aucun pseudo-code, aucun fichier tronqué.
2. Commentaires en français. **Chaque choix technique est justifié en une phrase** dans un
   commentaire au plus près du code concerné.
3. Aucun chemin, taille, zone sûre, argument Chromium, correspondance d'easing codé en dur hors de
   `pipeline/config.py` : importer `from pipeline import config`.
4. Imports Python : bibliothèque standard + `numpy`, `cv2`, `jsonschema`, `referencing`,
   `playwright` uniquement (côté Blender : `bpy`, `bmesh`, `mathutils`, `bpy_extras`, stdlib ;
   **toujours `import bpy` AVANT `bmesh`/`mathutils`**).
5. Écritures de fichiers atomiques : écrire `*.tmp` puis `os.replace`.
6. Messages d'erreur destinés à l'utilisateur : en français, actionnables (où, quoi, comment corriger).
7. Ne jamais modifier un fichier possédé par un autre module (voir §9). Un besoin de changement du
   contrat se signale dans le rapport final ; en attendant, on s'adapte sans casser le contrat.
8. Tests de vérification : `tests/verify_stepNN_<nom>.py`, exécutables par
   `.venv/Scripts/python.exe tests/verify_stepNN_<nom>.py` depuis la racine, affichant des lignes
   `PASS ...` / `FAIL ...` et sortant avec un code ≠ 0 en cas d'échec. Fichiers temporaires de test
   sous `build/_tests/<étape>/` (jamais dans le code source).
9. Tout processus ou navigateur lancé est fermé (try/finally). Ne jamais tuer de processus par
   motif large ; si nécessaire, filtrer par PID connu.

---------------------------------------------------------------------------------------------------
## 2. Arborescence et propriété des fichiers

```
mograph.py                    CLI (intégrateur)
pipeline/config.py            constantes (intégrateur — NE PAS MODIFIER sans accord)
pipeline/scene.py             compilateur                               [agent compilateur]
pipeline/web_render.py        pilote Chromium                           [agent runtime]
pipeline/blender_render.py    lanceur Blender                           [agent blender]
pipeline/compose.py           séquence maître                           [agent média]
pipeline/encode.py            FFmpeg                                    [agent média]
pipeline/audio.py             piste test + mesures                      [agent média]
pipeline/qc.py                contrôle qualité                          [agent qc]
backends/blender_backend.py   script bpy (Python 3.11)                  [agent blender]
runtime/clock.js player.html runtime.css runtime.js                     [agent runtime]
schema/*.json, schema/internal/*.json   contrats (intégrateur)
presets/*.json                6 presets                                 [agent compilateur]
scenes/*.json                 démos (intégrateur)
tests/                        vérifications par étape (chaque agent les siennes)
build/<scene>/                compiled.json, shots/<shot>/, plates/<shot>__<plate>/, master/,
                              determinism.json, logs/
out/<scene>/                  vidéos, encode_commands.sh, qc_report.json/.md, versions.txt
```

Nommage des images : `%06d.png`, index LOCAL au plan à partir de 0 (plaques et plans web), GLOBAL
pour `master/`.

---------------------------------------------------------------------------------------------------
## 3. Sémantique de la scène (compilateur → tout le reste)

### 3.1 Temps
- Nombre = secondes LOCALES au plan (0 = première image du plan).
- `beat:N[±x]` : temps GLOBAL = `audio.offset + N × 60 / bpm (+ x)`. `marker:id[±x]` : temps
  GLOBAL = `markers[id].time (+ x)`. Converti en local : `t_local = t_global − global_start / fps`.
- Sans `audio.bpm` : toute référence `beat:`/`beats:` est REFUSÉE ; marqueur inconnu : REFUS.
- Durées : secondes, `beats:N` (= N × 60 / bpm) ou alias `short|medium|long` du preset.
- Durée de plan : `duration × fps` doit être entier à 1e-6 près, sinon REFUS avec explication et
  deux valeurs valides proches (ex. « 1,01 s à 25 i/s = 25,25 images ; utilisez 1,00 s ou 1,04 s »).
- Une tween qui démarre avant 0 (ex. `sync:end` trop tôt) ou finit après la fin du plan : REFUS
  (avant 0) / AVERTISSEMENT (après la fin, la fin est simplement hors champ).

### 3.2 Plans, fondus, total
- Plan i : `frames_i = duration_i × fps`.
- `transition_in` du plan i (i > 0) : `cut` (défaut = `preset.transitions.default` si omis) ou
  `crossfade` de durée `dur` (défaut `preset.transitions.crossfade.dur`). `fade_in_frames_i =
  dur × fps` entier, sinon REFUS ; `fade_in_frames_i ≤ min(frames_{i-1}, frames_i)`. Le premier
  plan a toujours `fade_in_frames = 0` (un `crossfade` sur le plan 0 est REFUSÉ).
- `global_start_0 = 0` ; `global_start_i = global_start_{i-1} + frames_{i-1} − fade_in_frames_i`.
- Total = `global_start_last + frames_last`.
- Dans le fondu (k = 0..F−1) : image maître `global_start_i + k` = mélange de
  `A[frames_{i-1} − F + k]` et `B[k]`, poids de B `w = smoothstep((k+1)/(F+1))`,
  `smoothstep(x) = x²(3 − 2x)`. Aucune image de fondu n'est donc 100 % A ou 100 % B.

### 3.3 Graine par plan
`seed_plan = int(sha256(f"{scene.seed}:{shot.id}").hexdigest()[:8], 16)` (uint32). Graine Blender
d'une plaque : `int(sha256(f"{scene.seed}:{shot.id}:{plate.id}").hexdigest()[:8], 16) & 0x7FFFFFFF`.

### 3.4 Couleurs, positions, ancres
- Couleur : `#RRGGBB(AA)` conservée (majuscules), ou nom de palette résolu en hexadécimal.
  Nom inconnu : REFUS listant les noms disponibles.
- Position : nombre = px logiques ; `N%` du canevas ; `safe:N%` / `action:N%` = `left + N% × width`
  (resp. `top + N% × height`) du rectangle sûr (`config.safe_rect`).
- Ancre nommée → `[ax, ay]` : center [0.5,0.5], top-left [0,0], top [0.5,0], top-right [1,0],
  left [0,0.5], right [1,0.5], bottom-left [0,1], bottom [0.5,1], bottom-right [1,1].

### 3.5 Texte
- Police : `style` (display|body) → `preset.typography[style]` ; `weight`/`size`/`tracking`/
  `lineHeight`/`case` du calque remplacent ceux du style. Taille par défaut :
  `typography[style].size` sinon 96 (display) / 40 (body).
- La casse est appliquée PAR LE COMPILATEUR (`str.upper`, `str.lower`, `str.title`) : le runtime
  reçoit le texte final.
- (famille, graisse, style) doit exister dans `preset.fonts`, sinon REFUS « police manquante ».
- Couverture des glyphes : chaque caractère (hors \n) doit appartenir à l'`unicodeRange` d'un
  fichier de la même (famille, graisse) ; l'`unicodeRange` est lu dans
  `node_modules/@fontsource/<pkg>/<graisse>.css` (ou `<graisse>-italic.css`), dans le bloc
  `@font-face` dont le `src` référence `./files/<nom du fichier>` (vérifié : les fichiers
  `<sous-ensemble>-<graisse>.css` n'ont PAS d'unicode-range). Caractère non couvert : REFUS
  (jamais de police de repli). Graisses disponibles : Anton, VT323, Patrick Hand = 400 seulement ;
  Space Mono 400/700 ; Caveat 400-700 ; les autres 100/200-900.
- `split` compilé = `{chars, words, lines}` : vrai pour chaque niveau demandé par `layer.split` OU
  ciblé par une tween. Ex. une tween `target: chars` ⇒ `chars: true`.
- `max_width` = `maxWidth` ou null (null ⇒ `white-space: pre` côté runtime).

### 3.6 Tweens (le point le plus sensible pour le déterminisme)
Développement :
1. `move: nom` → étapes `preset.motion.moves[nom]`. Chaque étape : `start = at + offset`.
   Si la tween donne `dur`, facteur `k = dur / durée_totale_du_move` appliqué à `offset` et `dur`
   de chaque étape. `ease`, `stagger`, `target` de la tween REMPLACENT ceux des étapes ; `from`/`to`
   de la tween sont FUSIONNÉS clé à clé dans chaque étape qui anime déjà cette clé (et dans la
   première étape sinon).
2. Tween sans `move` : une seule étape (`dur` défaut `durations.medium`, `ease` défaut `default`).
3. `sync: end` : décalage `Δ = −(offset_0 + dur_0)` (après remise à l'échelle) sur TOUTES les
   étapes, de sorte que la FIN de la première étape tombe exactement sur `at`.
4. `stagger` défaut : `preset.motion.stagger` si la cible est chars|words|lines|bars, sinon 0.
5. Alias d'easing (`default, enter, exit, emphasis, anticipate, settle`) → valeur du preset
   (résolution récursive d'un niveau). Un nom présent dans `customEases` reste tel quel (le runtime
   l'enregistre). Sinon, doit être un ease GSAP 3 valide : familles `none|linear|power0-4|sine|
   expo|circ|back|elastic|bounce|quad|cubic|quart|quint|strong` + `.in|.out|.inOut` + paramètres
   optionnels `(...)`, ou `steps(N)`. Sans direction, GSAP applique `.out` : le compilateur écrit
   explicitement `.out`. Inconnu : REFUS avec la liste des alias et customEases disponibles.
6. Linéaire (`config.LINEAR_EASES`) : REFUSÉ sauf `allowLinear: true` (alors émis `"none"`).
7. `scale` est normalisé en `scaleX` + `scaleY`. Couleurs résolues en hex.
8. **Continuité et fromTo explicites** : le compilateur suit, par (calque, cible, propriété), la
   valeur courante, initialisée à la valeur de base (x 0, y 0, scaleX/scaleY = scale du calque,
   rotation du calque, opacity du calque, skewX/Y 0, draw 1, blur 0, reveal 0, color/fill/stroke du
   calque). Les étapes sont traitées par `start` croissant. Toute propriété présente dans `to` mais
   absente de `from` reçoit la valeur courante ; toute propriété de `from` absente de `to` reçoit
   `to = from`. Chaque tween compilée a donc des `from`/`to` COMPLETS avec les mêmes clés.
9. Chevauchement temporel de deux tweens sur la même (calque, cible, propriété) : REFUS (indiquer
   les deux tweens et leurs intervalles ; pour une cible avec stagger, l'intervalle couvre
   `[start, start + dur + (n−1) × stagger]` où n est inconnu du compilateur → utiliser
   `[start, start + dur]` + avertissement si stagger > 0 et chaîne sur la même propriété).
10. `initial[cible]` = `from` de la PREMIÈRE tween de chaque (cible, propriété).
11. `x`/`y` en `N%` restent des chaînes : le runtime les convertit en px par cible au montage.

### 3.7 Graphique (chart)
- `grow` : `start` (défaut 0), `dur` (défaut `durations.long`), `stagger` (défaut preset),
  `ease` (défaut alias `enter`). Un ease qui DÉPASSE 1 est REFUSÉ pour `grow` : familles
  `back`/`elastic`, et tout customEase dont une ordonnée de point de contrôle sort de [0, 1].
- Barres et compteurs utilisent la MÊME progression (même ease, même temps) ; le runtime borne la
  progression à [0, 1] (garde-fou).
- `max` défaut = max des valeurs ; couleurs : `data[i].color` sinon `colors[i % n]` sinon cycle
  (primary, secondary, accent). `value_format` défaut {decimals 0, prefix "", suffix ""} ;
  formatage `Intl.NumberFormat('fr-FR')` côté runtime (locale figée).
- `label_font` / `value_font` : style body par défaut, surchargés par `labelStyle` / `valueStyle`.

### 3.8 Textures et effets
- Base = `preset.textures` ; `shot.fx.<nom>` : `false` → null, nombre → `amount`, objet → fusion.
- Valeurs par défaut complétées pour satisfaire `compiled_shot.schema.json`
  (grain : size 1, blend overlay, monochrome true ; paper : blend multiply, frequency 0.8,
  octaves 4 ; vignette : softness 0.6, color #000000 ; scanlines : spacing 3, color #000000 ;
  tracking : height 0.06, period 2.5, shift 18).
- Glitch : `windows` explicites + `on_beats` (k = from, from+every, …, ≤ to : fenêtre
  `[temps(beat:k) local, length]`, length défaut 0,1 s), triées, fusionnées si chevauchement,
  bornées au plan. `colors` défaut (primary, secondary). `layers` null = tous.
- `boil` d'un calque : `true` → `preset.textures.boil` (défaut amount 2, freq 0.03, step 2) ;
  objet → fusion ; absent/false → null.

### 3.9 Rendu
- `frame_step` = `preset.motion.frame_step`.
- Flou de mouvement : `quality.motion_blur` fusionné sur `preset.render.motion_blur` (défauts
  samples 8, shutter 0.5). Si `frame_step > 1` : désactivé + avertissement.
- Profondeur maître : 16 si flou de mouvement actif, sinon 8.
- `format.alpha` : fond de tous les plans = transparent (une couleur explicite est REFUSÉE) ;
  `outputs` doit contenir `prores_4444`, sinon REFUS. `prores_4444` sans alpha : REFUS.
- Sorties : `out/<scene>/<scene_id><suffix>_<profile>.mp4|.mov` ; `alpha_flatten = alpha et profil
  opaque`.

### 3.10 Plaques 3D → tâches Blender (`blender_job.schema.json`)
- Dimensions : calque `sequence` qui référence la plaque : `w` (défaut canevas) × dsf × plate_scale
  (arrondi au pair supérieur). Plusieurs calques : le plus grand. Aucun : taille du canevas +
  avertissement. Référence à une plaque inexistante : REFUS.
- Temps des clés : `frame = t_local × fps` (flottant). Clés triées par frame.
- `ease` d'une clé = segment qui ARRIVE sur elle → écrit dans `out` de la clé PRÉCÉDENTE :
  GSAP `famille.dir(params)` → `interpolation = config.GSAP_TO_BLENDER_INTERP[famille]`,
  `easing = config.GSAP_TO_BLENDER_EASING[dir]` ; `back.x(s)` → `back = s` (défaut 1.70158) ;
  `elastic.x(a, p)` → `amplitude = 0`, `period = p × (frame_fin − frame_début)` (p défaut 0.3) ;
  customEase → `config.CUSTOM_EASE_BLENDER` ; `steps(n)` → CONSTANT/AUTO ; `none` (allowLinear) →
  LINEAR/AUTO ; ease absent → alias `default`. Linéaire sans allowLinear : REFUS.
- Dernière clé : `out = null`. Canal à une seule clé : valeur constante.
- `rotation` en degrés conservée telle quelle (le backend convertit). `scale` nombre → vec3.
- Facettes : `segments` (sphere, cylinder, cone, torus ; ignoré ailleurs avec avertissement) et
  `shading` (smooth|flat) ne sont écrits dans la tâche que si la scène les donne (les tâches des
  scènes qui ne s'en servent pas restent identiques). Backend : sphère u = segments,
  v = max(3, segments // 2) ; cylindre et cône : segments côtés ; tore : u = segments,
  v = max(3, (3 × segments + 4) // 8) ; `flat` = aucun lissage (facettes visibles). Absents :
  128×64, 128, 128×48, lissé. Journal : `géométrie : N face(s) …` si un objet les règle.
- Matériau : nom → `preset.blender.materials[nom]` (inconnu : REFUS) ; objet en ligne fusionné
  sur les défauts : roughness 0.5, metallic 0, specular 0.5, sheen 0, sheen_roughness 0.5, coat 0,
  coat_roughness 0.03, subsurface 0, transmission 0, ior 1.45, anisotropic 0, emission #000000,
  emission_strength 0, base_color palette.primary ; `name` = nom ou `<objet>_mat`.
- Monde : `plate.world` fusionné sur `preset.blender.world` (color défaut palette.bg, strength 1) ;
  `sky: true` → réglages ciel du preset ; objet → fusion ; défauts ciel : sun_elevation 35,
  sun_rotation 0, altitude 0, air 1, dust 1, ozone 1, sun_size 0.545, sun_intensity 1.
- Lumières : `plate.lights` sinon `preset.blender.light_rig`. Défauts : shape DISK, size 1,
  size_y = size, energy 500, color #FFFFFF, target [0,0,0], spot_size 45, angle 0.526.
- Caméra : lens défaut `preset.blender.lens` sinon 50 ; fstop défaut `preset.blender.fstop`
  sinon null ; sensor_width 36.
- Rendu : device = `quality.blender_device` sinon `preset.blender.device` sinon auto ;
  samples = `quality.blender_samples` sinon `preset.blender.samples` sinon 64 ; adaptive_threshold
  défaut 0.01 ; denoise défaut true ; view_transform défaut "AgX" ; look défaut "None" ;
  exposure défaut 0 ; bounces défaut total 8, diffuse 4, glossy 4, transmission 4,
  transparent 8, volume 0 ; caustics défaut false ; motion_blur_shutter défaut 0.5.
- `transparent` = `plate.transparent` OU `format.alpha`.
- Plan compilé : `spec.plates[<id>]` = `{url, frames, width, height, source}` ; `source` =
  `sha256_json(tâche sans out_dir)` (out_dir : chemin absolu propre à la machine). Toute modification
  qui fait re-rendre la plaque (matériau, caméra, lumière, échantillons...) change donc `spec_sha256`
  et le plan est recomposé : la reprise du rendu web ne réutilise jamais des images composées sur une
  ancienne plaque.

---------------------------------------------------------------------------------------------------
## 4. API Python (signatures exactes)

### pipeline/scene.py
```python
class SceneError(Exception):
    errors: list[str]            # messages actionnables ; str(e) = puces jointes par "\n"

def load_preset(preset_id: str, overrides: dict | None = None) -> dict
    # presets/<id>.json + fusion profonde des overrides, validé, polices complétées (unicodeRange)
def validate_scene(raw: dict) -> None                       # SceneError si invalide (schéma + règles)
def compile_scene(source: str | os.PathLike | dict) -> dict # scène compilée (ci-dessous)
def canonical_json(obj) -> bytes      # json.dumps(sort_keys=True, separators=(",",":"), ensure_ascii=False).encode()
def sha256_json(obj) -> str
def summary(compiled: dict) -> str    # texte humain : format, sortie px, fps, images, plans, fondus, plaques, livrables, avertissements
def write_compiled(compiled: dict) -> Path   # build/<scene>/compiled.json
```
Scène compilée (JSON sérialisable) :
```
{ "contract": "mograph-scene/1", "scene_id", "title", "source", "preset_id",
  "format": {aspect, resolution, fps, alpha},
  "canvas": {width, height}, "device_scale_factor": 1|2, "output": {width, height},
  "frames", "duration", "frame_step", "motion_blur": {enabled, samples, shutter}, "depth": 8|16,
  "audio": null | {src (chemin absolu), bpm, offset, beats_per_bar, markers, synth|null, target_lufs},
  "outputs": [{profile, crf, suffix, path (absolu), alpha_flatten}],
  "qc": {safe_zone, safe_zone_tolerance_s, allow_black_s, determinism_frames, loudness_target_lufs},
  "shots": [{id, index, frames, global_start, fade_in_frames,
             spec: <compiled_shot.schema.json>, plates: [<blender_job.schema.json>]}],
  "warnings": [str] }
```
`target_lufs` = `audio.synth.target_lufs` sinon `qc.loudness_target_lufs`.

### pipeline/web_render.py
```python
def render_shot(compiled: dict, shot_index: int, *, workers: int | None = None,
                frames: list[int] | None = None, log=print) -> dict   # manifeste du plan
def render_scene(compiled: dict, *, workers: int | None = None, log=print) -> list[dict]
def verify_determinism(compiled: dict, *, count: int | None = None, log=print) -> dict
    # écrit build/<scene>/determinism.json (manifests.schema.json#/$defs/determinism)
```
### pipeline/blender_render.py
```python
def render_plate(job: dict, *, log=print) -> Path        # écrit job.json, lance bpy, relaie, vérifie
def render_scene_plates(compiled: dict, *, log=print) -> list[Path]
```
### pipeline/compose.py
```python
def compose_scene(compiled: dict, *, log=print) -> dict  # master/ + master/manifest.json
```
### pipeline/encode.py
```python
def build_commands(compiled: dict) -> list[dict]         # [{profile, output, argv: list[str]}]
def encode_scene(compiled: dict, *, log=print) -> list[Path]   # + out/<scene>/encode_commands.sh
```
### pipeline/audio.py
```python
def synth_beat(out_path, bpm: float, duration: float, *, beats_per_bar: int = 4,
               offset: float = 0.0, target_lufs: float = -14.0) -> Path
def measure_loudness(path) -> dict          # {integrated_lufs, true_peak_dbtp, lra}
def detect_onsets(path, *, threshold_db: float = -30.0) -> list[float]
def ensure_scene_audio(compiled: dict, *, log=print) -> Path | None   # génère si synth et absent
```
### pipeline/qc.py
```python
def probe(path) -> dict                                   # ffprobe -count_frames JSON
def check_output(path, expected: dict) -> list[dict]      # contrôles d'UN fichier
def run_qc(compiled: dict, *, log=print) -> dict          # rapport ; écrit qc_report.json/.md
    # rapport["verdict"] ∈ {"CONFORME", "NON CONFORME"} ; le CLI sort ≠ 0 si NON CONFORME
```
Chaque contrôle : `{"id", "label", "status": "PASS"|"FAIL"|"WARN"|"SKIP", "expected", "actual",
"blocking": bool, "detail"}`.

---------------------------------------------------------------------------------------------------
## 5. Runtime navigateur (contrat page ⇄ pilote)

Chargement : le pilote injecte, via `context.add_init_script`, DANS CET ORDRE :
1. `window.__MOGRAPH_SEED__ = <seed>; window.__MOGRAPH_SHOT__ = <plan compilé>;`
2. le contenu de `runtime/clock.js`.
Puis `page.goto(config.PLAYER_URL)` ; `player.html` charge `runtime.css`,
`/node_modules/gsap/dist/gsap.min.js`, `/node_modules/gsap/dist/CustomEase.min.js`, `runtime.js`.

Réseau : `context.route("**/*", ...)` ne sert QUE `http://mograph.render` + préfixes de
`config.SERVED_PREFIXES` (chemin résolu obligatoirement sous le dossier mappé), types MIME de
`config.MIME_TYPES` ; tout le reste est `route.abort()`.

Drapeaux : `window.__MOGRAPH_READY__ = true` quand tout est prêt ; `window.__MOGRAPH_ERROR__ =
"message"` en cas d'erreur (police absente, image indécodable, plan invalide...). Le pilote attend
l'un des deux ; toute `pageerror` ou `console.error` arrête le rendu.

API :
- `await window.__seek(frame, sub = 0)` : `q = floor(frame / frame_step) × frame_step` ;
  `t = (q + sub) / fps` ; horloge virtuelle = t (ms) ; `master.seek(0, false)` puis
  `master.seek(t, false)` (repasser par 0 rend l'état indépendant de l'historique des positions) ;
  attente de tous les hooks (plaques décodées, grain, glitch, boil, bande de tracking) ; animations
  CSS mises en pause et placées sur t ; double `requestAnimationFrame`.
  Les effets à caractère aléatoire (grain, boil, glitch) sont fonction de `q` (image quantifiée),
  jamais de `sub` : le flou de mouvement ne moyenne pas le grain.
- `window.__layout()` → liste de `layoutRecord` (manifests.schema.json) pour les textes (calques
  text + libellés/valeurs de chart) de `safe: true` et d'opacité effective ≥ 0,9.

GSAP : `gsap.config({force3D: false})`, `gsap.ticker.lagSmoothing(0)`, timeline maître
`paused: true`, `CustomEase.create` pour chaque `custom_eases`, `gsap.set(initial)` puis tweens
`fromTo` avec `immediateRender: false`, `gsap.ticker.sleep()` APRÈS la construction.

Flou de mouvement (pilote) : N sous-images, `sub_i = shutter × ((i + 0.5)/N − 0.5)`, moyenne
float32 en alpha prémultiplié, désprémultiplication, PNG 16 bits (`round(v × 65535)`).

Capture : `page.screenshot(type="png", scale="device", omit_background=True)` exclusivement ;
normalisation BGRA 8 bits (ajout d'un alpha 255 si Chromium l'a omis) ; `cv2.imwrite` avec
`IMWRITE_PNG_COMPRESSION = config.PNG_COMPRESSION`.

---------------------------------------------------------------------------------------------------
## 6. Déterminisme

- Horloge virtuelle, `Math.random` = mulberry32(seed), `Date`/`performance.now` virtuels
  (époque `config.VIRTUAL_EPOCH_MS`).
- Empreinte = SHA-256 des pixels décodés (voir manifests.schema.json).
- Mesuré à l'intégration (fixture web_det, rendu avant / arrière / isolé) : un DOM identique ne
  suffit pas, Chromium réutilisait des peintures et des tuiles d'images précédentes (±2 niveaux sur
  le texte selon l'ordre des seeks). Deux correctifs cumulés, chacun suffisant sur le cas mesuré :
  `__seek` fait passer `#mg-root` par `display: none` + remise en page forcée (tout est repeint à
  neuf), et `--disable-gpu-rasterization` (rastérisation CPU exacte) complète `config.CHROMIUM_ARGS`.
- `verify_determinism` : `count = qc.determinism_frames` images témoins choisies par un
  `random.Random(scene_seed)` parmi toutes les (plan, image), en incluant toujours la première et
  la dernière image de chaque plan et une image de chaque fondu ; rendues dans un NAVIGATEUR NEUF,
  une page par plan, dans un ORDRE MÉLANGÉ, chacune par un `__seek` isolé ; comparées aux
  empreintes du manifeste. En cas d'écart : image de différence `build/<scene>/determinism_diff/`.
- Plaques : Cycles avec graine fixe ; le rapport de l'agent Blender indique si deux rendus du même
  numéro d'image sont identiques au bit près en CPU et en OptiX. Exigence pour une plaque re-rendue
  (reprise) : écart invisible après composition 8 bits (max ≤ 128/65535, moyen ≤ 1/65535) — l'échantillonnage
  adaptatif multithread de Cycles n'est pas bit-exact d'une machine à l'autre (mesuré jusqu'à 33/65535 sur un
  runner GitHub). La porte de déterminisme, sur les images web recomposées des MÊMES plaques, reste au bit près.

---------------------------------------------------------------------------------------------------
## 7. Encodage (rappel normatif)
Voir SPEC §15. `-framerate fps -start_number 0 -i master/%06d.png`, `-frames:v N`, conversion
`config.ZSCALE`, aplatissement sur noir si `alpha_flatten`, audio
`-af apad=whole_dur=D,atrim=end=D` avec `D = N / fps` (texte décimal exact), `-map 0:v:0 -map 1:a:0`.

**Plan alpha (FFmpeg 6.1.1 d'Ubuntu 24.04, mesuré)** : swscale `rgba → gbrap` décale l'alpha ≥ 128
de +1, `rgba → rgba64le` donne 65532 pour 255, `vf_zscale` traite l'alpha comme une plage limitée
(255 → 1020 en 10 bits). Le préfixe littéral `format=rgba64le,premultiply=inplace=1` aplatit donc
le blanc opaque à Y = 937. Règle : l'alpha n'est JAMAIS converti par ces chemins ; il est lu par
`extractplanes=a`, recopié dans R = G = B (`mergeplanes`, maps par défaut) et converti par le
chemin couleur de zscale (exact). Formats selon `compiled.depth` : 8 bits `rgba`/`gbrp`,
16 bits `rgba64be`/`gbrp16le` (alpha recopié en `gbrp16be`, boutisme d'extractplanes).
- aplatissement (équivalent exact du préfixe du contrat) : RVB et alpha en `gbrp16le`, puis
  `premultiply=inplace=0` (2 entrées, alpha = plan 0 du 2e flux), puis `config.ZSCALE` ;
- ProRes 4444 : couleur `config.ZSCALE → yuv444p10le`, alpha `zscale plein, matrix=709 →
  yuv444p10le` (Y = alpha car Kr + Kg + Kb = 1), fusion `mergeplanes → yuva444p10le`.
Vérifié au bit près en 8 et 16 bits (`tests/verify_step15_encode.py`), identique en couleur à la
chaîne précédente, valable aussi sur FFmpeg 8.x (mêmes filtres, sans option dépréciée).
HEVC `keyint = 2 × fps`, `min-keyint = fps` (valeurs numériques). `encode_commands.sh` : script bash
exécutable depuis la racine (`cd "$(dirname "$0")/../.."`), une commande par livrable, citations
POSIX (`shlex.join`), chemins relatifs à la racine.

---------------------------------------------------------------------------------------------------
## 8. QC (rappel normatif)
Attendus déduits de la scène compilée : codec/profil/pix_fmt (`config.PROFILES`, 4444 décodé en
yuva444p10le OU yuva444p12le), taille `output`, `r_frame_rate` = `avg_frame_rate` = `fps/1`,
`nb_read_frames` = frames, durée ± ½ image, `hvc1` (HEVC), bt709 ×3 + tv (champ absent du flux :
lu sur la 1re image décodée — FFmpeg < 7 ne pose la plage ProRes que sur les images, l'atome MOV
`colr nclc` n'ayant pas de drapeau de plage), piste `tmcd` (ProRes),
alpha réel (4444, alphaextract image médiane : min < 255), blackdetect `pix_th = 0.03`
(bloquant si noir > allow_black_s, informatif si `format.alpha`), audio (codec aac|pcm_s24le,
48 kHz, durée ± (1 image + 25 ms)), loudness ±1 LU + true peak ≤ −1 dBTP, zones sûres (master
manifest, bloquant au-delà de `safe_zone_tolerance_s` continu hors zone), déterminisme
(`determinism.json` ok), niveaux vidéo (informatif, jamais bloquant : voir §11.1), déclaration
d'usage de l'IA `scene.ai_disclosure` (informatif, jamais bloquant : voir §11.2), assets IA gelés
`scene.ai_lock` (**bloquant** : verrouillé, présent, empreinte identique ; SKIP sans relevé `assets` :
voir §11.4).
Rapport `out/<scene>/qc_report.json` + `.md` (verdict en tête).

---------------------------------------------------------------------------------------------------
## 9. Qui écrit quoi (parallélisation)
Agents parallèles, chacun propriétaire exclusif de ses fichiers (§2) et de ses tests :
- compilateur : `presets/*.json`, `pipeline/scene.py`, `tests/verify_step09_presets.py`,
  `tests/verify_step11_compiler.py`, `tests/fixtures/scene_*.json` (préfixe `scene_`).
- runtime : `runtime/*`, `pipeline/web_render.py`, `tests/verify_step12_web.py`,
  `tests/fixtures/shot_*.json` (plans compilés écrits à la main, conformes au schéma).
- blender : `backends/blender_backend.py`, `pipeline/blender_render.py`,
  `tests/verify_step13_blender.py`, `tests/fixtures/job_*.json`.
- média : `pipeline/compose.py`, `pipeline/encode.py`, `pipeline/audio.py`,
  `tests/verify_step14_compose.py`, `tests/verify_step15_encode.py`, `tests/verify_step16_audio.py`.
- qc : `pipeline/qc.py`, `tests/verify_step17_qc.py`.
L'intégrateur écrit `mograph.py`, les scènes démo, README.md, QC_CHECKLIST.md.

---------------------------------------------------------------------------------------------------
## 10. Extensions (après l'étape 20) — normatives

### 10.1 Scène compilée : ajouts
Chaque entrée de `shots[]` porte en plus (hors `spec` : les empreintes des plans ne changent pas) :
- `transition` : `{type: cut|crossfade|wipe|push, direction: left|right|up|down}` ; `fade_in_frames`
  reste le chevauchement (commun aux trois transitions non « cut »).
- `media` : tâches d'extraction des calques vidéo `[{kind: "video", layer_id, plate_key, src, start,
  loop, fit, fps, frames, width, height, color_matrix, out_dir, digest}]`.
`audio` gagne `normalize` (booléen).
Dans `spec` (change l'empreinte des plans concernés) : chaque entrée de `spec.plates` porte `source`
(obligatoire, SHA-256) — plaque 3D : empreinte de la tâche Blender (§3.10) ; calque vidéo : `digest`
de sa tâche d'extraction (§10.2). Ajouté après coup pour les plaques 3D : les plans à plaque rendus
auparavant sont recomposés une fois (les plaques elles-mêmes sont conservées, leur tâche est inchangée).

### 10.2 Calques ajoutés (développés par le compilateur, le runtime n'a pas de nouveau type)
- `video` → calque `sequence` compilé dont `plate` = `video__<id>` ; `spec.plates["video__<id>"].source`
  = `digest` (SHA-256 de la source + réglages, invalide le plan s'il change).
  Images dans `build/<scène>/media/<plan>__<calque>/NNNNNN.png` (`config.media_dir`), extraites par
  `pipeline/media.py` avant le rendu web du plan (`web_render.render_shot` l'appelle).
- `subtitles` → un calque `text` par réplique (id `<id>-NNN`), développé après `_timing()` : temps
  SRT/VTT globaux convertis en local, fondus `fade`, répliques hors plan ignorées.

### 10.3 Transitions géométriques (`pipeline/compose.py`)
`blend_transitions` : volet (masque anticrénelé sur 1 px) et poussée (translation ENTIÈRE, sans
rééchantillonnage), en alpha prémultiplié, progression = `fade_weight` (smoothstep). Le fondu garde
`blend_premultiplied` inchangé (maîtres existants identiques au bit près). Manifeste maître :
`frame_map[].mode` ∈ {link, convert, blend, wipe, push} ; `segments[]` + `transition`, `direction`.

### 10.4 API ajoutées
```python
# pipeline/audio.py
def analyse_tempo(path, *, bpm_min=60.0, bpm_max=200.0, beats_per_bar=4) -> dict
    # {bpm, offset (1er temps fort), beats_per_bar, confidence 0..1, drop (s|None), duration}
# pipeline/encode.py
def loudnorm_prefix(src, duration: str, target_lufs: float) -> str   # 2 passes, loudnorm linéaire
# pipeline/media.py
def ensure_shot_media(compiled, shot_index, *, log=print) -> list[Path]
# pipeline/qc.py
def preflight_layout(compiled, *, margin_px=None) -> dict   # relevés des plans, avant composition
# check_output : contrôle « levels » (EBU R103 v3.0, voir §11.1) si expected["levels"]
# pipeline/sheets.py
def pick_frames(compiled, max_tiles=16) -> list[tuple[int, str]]
def write_contact_sheets(compiled, *, log=print) -> list[Path]
# pipeline/preview.py
def serve(scene_path, *, port=8765, open_browser=True, log=print, ready_event=None, stop_event=None)
# pipeline/batch.py
def expand(scenes: list[str], *, data: str | None = None) -> list[tuple[str, str | dict]]
def write_report(results, *, draft: bool) -> Path
```

### 10.5 Réglages (`pipeline/config.py`)
`WEB_MAX_WORKERS` (env `MOGRAPH_MAX_WORKERS`, défaut 4) ; `_default_workers` = un navigateur par
cœur jusqu'à ce plafond. `LEVELS_R103_Y = (55, 966)` (indicateur brut informatif depuis la phase 1,
voir §11.1). `TEMPLATES_DIR`, `VIDEO_EXTENSIONS`,
`SUBTITLE_EXTENSIONS`, `media_dir()`. MIME audio ajoutés (aperçu).

### 10.6 CLI
`validate --layout`, `all|render --draft` (id `<id>_draft` : aucun mélange avec le rendu final ;
pas de porte de déterminisme ni de QC), `preview`, `new`, `batch [--data CSV]`, `beats`, `sheets`.
`all` produit les planches contact avant le QC. `render`/`all` rendent les plans sans plaque
pendant Blender (`step_plates_and_web`).

### 10.7 Tests
`tests/verify_features.py` (isolé dans `build/_tests/features/`, médias dans `assets/_tests/`),
rejoué par la CI (`.github/workflows/tests.yml`) avec les étapes 9 à 17.

---------------------------------------------------------------------------------------------------
## 11. Évolutions de la feuille de route (phases) — normatives

Chaque phase se termine par `scripts/baseline.py run` comparé à `docs/baseline/phase-0.json`
(`docs/BASELINE.md`) : aucune régression tolérée.

### 11.1 Phase 1 — niveaux vidéo selon EBU R103 v3.0
Source : EBU R 103 v3.0 (mai 2020), https://tech.ebu.ch/publications/r103, Table 1 et § « Video
Signal Filtering » / « Signal Issues ». Contrôle `<livrable>.levels`, **informatif** (`blocking:
false`, comme avant) :
- **codes réservés** : Y ou chroma brut (signalstats) hors `config.LEVELS_RESERVED_10BIT` (4..1019)
  → WARN ;
- sinon **gamut R103 v3.0** (`qc._gamut_r103`) : chaque image est décodée en 4:4:4 10 bits ; Y et
  R'G'B' (matrice BT.709 inverse, plage limitée, sans écrêtage) sont filtrés par
  `config.LEVELS_R103_FILTER_H` (1,2,3,4,3,2,1 /16) et `LEVELS_R103_FILTER_V` (1,2,1 /4) ; une image
  est **hors gamut** si la part de ses pixels hors `config.LEVELS_R103_PREFERRED_10BIT` (20..984)
  dépasse `config.LEVELS_R103_AREA_MAX` (1 %, seuil de signalement de R103 « Signal Issues ») → WARN ;
- sinon **plage nominale** (R103 v3.0, Annexe 1, Introduction : un programme pré-produit et étalonné
  doit suivre de près la plage nominale 64..940) : même mesure filtrée, plage
  `config.LEVELS_NOMINAL_10BIT` élargie de `LEVELS_NOMINAL_TOLERANCE` (±1 % = 8,76 codes : 55,24..
  948,76) ; plus de 1 % d'une image au-delà → WARN, sinon PASS. La tolérance est un choix mograph
  (R103 ne la chiffre pas) : mesuré sur Éclosion Urbaine, le bruit de quantification des codecs
  dépasse la plage stricte 64..940 sur 1,94 % (HEVC) et 1,73 % (ProRes) de la pire image, mais reste
  sous 4,4 codes sur plus de 99 % de chaque image ; un vrai défaut de plage (fond à 975 ou à 30) est
  détecté ;
- toujours relevés dans `actual` : min/max bruts Y/chroma/R'G'B', nombre d'images à Y brut hors
  `config.LEVELS_R103_Y` (55..966, ancienne mesure, indicateur seulement), plages filtrées, pire
  image et surface hors plage (préférée et nominale). Le texte du contrôle donne des mesures, jamais
  de cause : l'explication ci-dessous vaut pour Éclosion Urbaine seulement.
- délai : une minuterie tue FFmpeg (PID connu) à l'échéance, même s'il se bloque sans rien écrire.

Diagnostic qui a motivé ce changement (Éclosion Urbaine, 744 images) : avant codec, la chaîne
`config.ZSCALE` donne Y 76..940 (0 image hors 55..966) ; en 4:4:4 sans codec, R'G'B' reste dans
63,6..942,5. Les 105 images HEVC (Y brut 52..1001) et 31 images ProRes (76..987) signalées hors
55..966 viennent donc de la **compression** (pointes aux arêtes vives), et les R'G'B' bruts hors
plage du **sous-échantillonnage de la chroma** (4:2:0 / 4:2:2) sur les arêtes saturées. Ce sont des
dépassements bénins au sens de R103 v3.0 : 0 image signalée hors gamut (surface maximale 0,014 % en
HEVC, 0,0023 % en ProRes) ni hors plage nominale tolérancée. L'encodeur n'est pas modifié : R103
déconseille les légaliseurs avant la fin du traitement, et aucun pixel livré ne change.

### 11.2 Phase 1 — provenance et déclaration d'usage de l'IA
Scène (`schema/scene.schema.json`) : bloc optionnel `provenance` = `{authors[], tools[{name, version?,
model?, role}], ai_disclosure}` ; `ai_disclosure` = `{ai_used, roles[], tools[], date (AAAA-MM-JJ),
statement?}`, `roles` ⊂ {scene, text, image, video, music, voice, subtitles, 3d, matte, other} ;
`ai_used: true` exige `roles` (≥ 1), `tools` (≥ 1) et `date`. Compilateur : date réelle vérifiée
(REFUS sinon) ; `ai_used: false` avec rôles ou outils → AVERTISSEMENT ; asset de
`config.AI_ASSETS_DIR` (`assets/ai/`) sans `ai_used: true` → AVERTISSEMENT.

Scène compilée, clés ajoutées (§4) :
- `assets` : `[{path (relatif à la racine, « / » ; absolu seulement pour une piste audio hors du
  projet), kinds ⊂ {image, video, subtitles, audio, text (alignement de la voix off, §11.5)}, ai, sha256 (assets IA verrouillés, phase 3)}]`,
  trié par chemin ; `ai` = fichier sous `config.AI_ASSETS_DIR`. Relevé des fichiers réellement
  utilisés (images, vidéos, sous-titres, piste audio, y compris la piste test générée).
- `provenance` : bloc normalisé (`authors`, `tools`, `ai_disclosure`) ou `null`.
Ces clés n'entrent dans aucune empreinte de plan (`spec_sha256`) : aucun re-rendu, aucun pixel modifié.

QC : contrôle de scène `scene.ai_disclosure` (`qc.check_ai_disclosure`), **jamais bloquant** :
WARN si un asset IA est utilisé sans déclaration ou avec `ai_used: false`, SKIP si le compilé n'a
pas de relevé `assets` (compilé d'avant la phase 1), PASS sinon. Textes libres du rapport Markdown
ramenés sur une ligne (aucun titre injecté). Le rapport
porte `provenance` et `ai_assets` (JSON) et une section « Provenance » (Markdown).

### 11.3 Phase 1 — versions de référence et CI
- `versions.json` : versions de référence (FFmpeg 9.0.2 gyan.dev « full » + filtres et encodeurs
  requis, Python 3.12.10, CPython 3.11.15 + bpy 5.0.1, Chromium 141.0.7390.37, Node 24.19.0,
  uv 0.11.7). C'est **le fichier de versions** : toute nouvelle dépendance y est ajoutée, épinglée.
- `constraints.txt` / `constraints-blender.txt` : TOUTES les versions installées (dépendances
  indirectes comprises), relevées le 30/09/2026 ; installation : `pip install -r requirements.txt -c
  constraints.txt`, `uv pip install bpy==5.0.1 -c constraints-blender.txt` (setup.ps1, setup.sh, CI).
- `scripts/check_versions.py [--profile reference|ci-linux] [--no-blender] [--no-chromium] [--strict]` :
  PASS/FAIL/NOTE, code ≠ 0 si un écart. FFmpeg ET ffprobe comparés à la chaîne de version complète
  (profil `reference` : `9.0.2-full_build-www.gyan.dev` ; `ci-linux` : `6.1.1-3ubuntu5`), plus les
  filtres et encodeurs requis (dont extractplanes et mergeplanes, utilisés par l'encodeur). Tout le
  reste est exigé à l'identique dans les deux profils ; Node et uv (sans effet sur les pixels) ne
  donnent qu'une NOTE, un FAIL avec `--strict` (utilisé en CI).
- CI (`.github/workflows/tests.yml`) : versions épinglées en tête (`env`, égales à versions.json) ;
  jobs `rapide` et `rendu` sur Ubuntu 24.04 (FFmpeg 6.1.1 : **écart documenté** ; aucune build Linux
  n'est identique à la build gyan.dev de référence — BtbN publie des builds `n9.0.2` (quotidiennes
  gardées 14 jours, de fin de mois gardées 2 ans) mais avec d'autres versions de bibliothèques ;
  option non retenue, à décider) ; job `reference-windows` (windows-2025) avec `choco install
  ffmpeg-full --version=9.0.2` (paquet Chocolatey qui embarque l'archive gyan.dev : SHA-256 de
  ffmpeg.exe et ffprobe.exe identiques aux binaires winget de la machine de rendu, vérifié le
  01/10/2026) et `MOGRAPH_FFMPEG` / `MOGRAPH_FFPROBE` pointés dessus, qui rejoue les 9 scripts.
  Chaque job lance `check_versions.py` avant les tests. Les empreintes d'images ne sont pas comparées
  entre machines (déterminisme garanti par machine, §6).

### 11.4 Phase 3 — assets IA gelés
Principe : toute sortie d'IA (clip, image, musique, voix, mesh, matte, texte) est un **asset gelé** :
générée une fois, rangée sous `config.AI_ASSETS_DIR` (`assets/ai/`, env `MOGRAPH_AI_ASSETS_DIR`),
identifiée dans `config.ASSETS_LOCK` (`assets.lock.json` à la racine, env `MOGRAPH_ASSETS_LOCK`). Le
rendu n'appelle **jamais** une IA. La porte de déterminisme ne couvre que ce qui suit le gel.

Verrou (`schema/assets_lock.schema.json`, contrat `mograph-assets-lock/1`) : `assets` = objet clé →
entrée ; clé = chemin relatif à la racine, « / » ; entrée = `{sha256, bytes, kind ∈ {video, image,
audio, subtitles, mesh, text, other}, added (UTC), replaces?, media? (ffprobe : width, height, fps,
duration_s, codec, pix_fmt, alpha), provenance {tool, tool_version?, model, date (AAAA-MM-JJ),
prompt, seed?, license, cost?, source ∈ {manual_import, api, local}, notes?}}`. Écrit trié, de façon
atomique (temporaire propre au processus), sous un verrou inter-processus (`assets.lock.json.lck`,
attente 30 s) ; validé à la lecture et à l'écriture, erreurs traduites en français ; lu en
`utf-8-sig` (un BOM est toléré).

Chemins : l'appartenance au dossier IA est jugée sur le chemin LEXICAL normalisé et sur le chemin
RÉSOLU, préfixes Win32 `\\?\` / `\\.\` retirés, casse normalisée (`ai_location` → `in`, `link_out`,
`out`). Un lien ou une jonction écrit sous le dossier IA et qui en sort (`link_out`) est REFUSÉ ; le
dossier IA doit être sous `config.ASSETS_DIR`. `audio.src` en chemin de périphérique, étendu ou
réseau est refusé, et une piste `audio.synth` ne peut pas être rangée sous le dossier IA (elle est
régénérée par le pipeline ; `audio.ensure_scene_audio` refuse aussi d'y écrire).

`pipeline/assets.py` :
```python
class AssetError(RuntimeError)
def file_sha256(path) -> str
def is_ai_path(path) -> bool ; def lock_key(path) -> str ; def kind_for(path) -> str
def load_lock(lock_path=None) -> dict ; def save_lock(lock, lock_path=None) -> Path
def ai_location(path) -> str                                    # in | link_out | out
def add(src, *, tool, model, prompt, license, date=None, seed=None, cost=None, notes=None,
        tool_version=None, source=None, kind=None, dest=None, replace=False,
        lock_path=None) -> tuple[str, dict, str, list[str]]  # (clé, entrée, statut, avertissements)
def problems_for(paths, lock=None, lock_path=None) -> list[str]   # non verrouillé, absent, modifié
def ai_paths_of(compiled) -> list[str] ; def check_compiled(compiled, *, stage) -> None   # AssetError
def verify(lock_path=None) -> list[dict] ; def entries_for(paths, lock_path=None) -> dict
```
`add` valide TOUT (options, date AAAA-MM-JJ réelle, graine, `--dest` = chemin de fichier relatif sans
`..`, entrée conforme au schéma) avant de toucher au disque ; copie un fichier extérieur sous
`assets/ai/<nature>/<nom>` (ou `--dest`) via un temporaire vérifié ; un asset gelé n'est jamais écrasé
par un autre contenu sans `--replace` (ancienne empreinte gardée dans `replaces`, ancien fichier
restauré si l'écriture du verrou échoue). Statuts : `ajouté`, `remplacé`, `inchangé`, `provenance mise
à jour` ou `mis à jour (champs)` ; re-geler le même fichier COMPLÈTE la provenance (seules les options
données remplacent les valeurs existantes, `replaces` est conservé). Avertissement si ffprobe ne lit
pas une vidéo ou un son. `verify` signale aussi les clés non normalisées et les liens sortants.

Contrôles (refus explicite, message actionnable) : à la **compilation** (`_asset_list` : tout asset
de `assets/ai/` utilisé, quel que soit le calque, doit être verrouillé, présent et d'empreinte
identique — REFUS sinon) ; avant le **rendu web de chaque plan** (`media.ensure_shot_media`) et avant
l'**encodage** (`encode.encode_scene`) : `assets.check_compiled` (un fichier modifié pendant un rendu
détaché est refusé) ; au **QC** : contrôle de scène `scene.ai_lock`, **bloquant** (SKIP si le compilé
n'a pas de relevé `assets`). Le rapport porte `ai_assets_lock` (JSON) et, dans la section
« Provenance », pour chaque asset IA : nature, SHA-256, outil/modèle, date, licence, graine, coût,
source, prompt (tronqué à 300 caractères).

Calque `ai_clip` (`schema/scene.schema.json`) : `{asset, w, h, fit, start, loop}` ; `asset` sous
`assets/ai/`, verrouillé, de nature `video`. Compilé par `_video_fields` (chemin « video » :
extraction FFmpeg exacte de l'image k = clip à `start + k / fps`, alpha conservé — vérifié sur un
ProRes 4444 —, calque runtime `sequence`) ; le job média porte en plus `ai_sha256` (ajouté APRÈS le
calcul de `digest` : les jobs vidéo existants gardent leur empreinte, aucun re-rendu). Un calque
`image` dont la source est un asset IA porte `ai_sha256` (empreinte gelée) : remplacer l'asset change
`spec_sha256`, donc le plan est re-rendu (les images non IA sont inchangées). L'aperçu (`preview`)
surveille le verrou et les assets IA de la scène : leur modification recompile, donc revérifie.

CLI : `mograph assets add <fichier> --tool --model (--prompt | --prompt-file) --license [--seed]
[--date] [--cost] [--notes] [--tool-version] [--source] [--kind] [--dest] [--replace]` ;
`mograph assets verify` (code 1 si un asset est absent, modifié ou non gelé) ; `mograph assets list
[--json]`.

Versionnement : `assets.lock.json` est versionné ; les médias de `assets/ai/` **ne le sont pas**
(`.gitignore` : dépôt public, conditions des services, poids) — sur une autre machine, `assets verify`
liste ceux à replacer. Tests : `tests/verify_step30_assets.py` (numérotation des tests de phase :
`verify_step<phase>0_*.py`).

### 11.5 Phase 4 — voix off ElevenLabs, alignement, sous-titres, mixage
Sources vérifiées le 01/10/2026 : API « text-to-speech with timestamps »
(`POST /v1/text-to-speech/{voice_id}/with-timestamps?output_format=…`, réponse `audio_base64`,
`alignment.characters`, `character_start_times_seconds`, `character_end_times_seconds`), quota
(`GET /v1/user/subscription` : `character_count`, `character_limit`,
`next_character_count_reset_unix`, `can_extend_character_limit`), historique (`GET /v1/history`).
Le 02/10/2026 : limite de caractères d'une requête par modèle (elevenlabs.io/docs/models :
`eleven_multilingual_v2` 10 000, `eleven_v3` 5 000, `eleven_flash_v2_5` 40 000…,
`config.VOICE_MAX_CHARS_PER_REQUEST`). L'alignement mot à mot vient du MÊME appel (aucun coût en
plus) ; l'API « forced alignment » n'est pas utilisée (coût non publié). PCM/WAV 44,1 kHz exigent
l'offre Pro : format retenu `mp3_44100_128` (toutes offres).

Scène : bloc `voiceover = {text, voice_id?, model?, language?, seed?, settings?, asset?, alignment?,
start?, gain_db?, duck?{db, attack, release}}`. `[[id]]` dans `text` crée `marker:id` au début du mot
suivant (retiré du texte envoyé ; position comptée dans le texte FINAL, blancs de bord retirés,
marqueurs consécutifs compris). Temps : `word:N[±x]` = début du mot N (0 = premier mot PRONONCÉ) de
la voix, en temps GLOBAL : la ponctuation isolée est rattachée à un mot — l'ouvrante (« ( [ — – - ¿ ¡
“ ‘) et celle qui précède le premier mot au mot SUIVANT, toute autre au mot précédent. Calque
`subtitles` avec `"voice": true` : répliques déduites de l'alignement (`voice.cues_from_words` :
coupe en fin de phrase — ponctuation finale éventuellement suivie de ponctuation fermante, « fini. »
» —, sur un silence > `config.VOICE_SUB_GAP_S`, ou au-delà de `VOICE_SUB_MAX_CHARS` /
`VOICE_SUB_MAX_S` ; tenue `VOICE_SUB_HOLD_S`, coupée au début de la réplique suivante).

`pipeline/voice.py` :
```python
class VoiceError(RuntimeError) ; class ApiRefused(VoiceError)   # refus HTTP / connexion : rien produit
def split_markers(text) -> tuple[str, list[tuple[str, int]]]
def request_params(vo) -> tuple[dict, str, bool] ; def request_key(params) -> str ; def billed_chars(params) -> int
def subscription(key) -> dict                      # lecture seule : tier, used, limit, remaining, reset, can_extend
def quota_now(key) -> dict                         # idem, compteur corrigé par l'historique récent (+ lag)
def billed_from_history(key, params, since_unix) -> int | None   # décompte réel ; ne lève jamais
def words_from_alignment(text, alignment) -> list[dict]   # {text, start, end, char_start, char_end}
def cues_from_words(words) -> list[tuple[float, float, str]]
def generate(scene_path, *, yes=False, dry_run=False, budget=None, regenerate=False, log=print, ask=None) -> dict
def load_alignment(path) -> dict                   # contrat mograph-voice-alignment/1
def secret_values() -> list[str] ; def redact(text) -> str   # masquage de la clé et de la voix de .env
```
`generate` : empreinte de la requête (texte nettoyé, voix, modèle, réglages — nombres normalisés,
`1` = `1.0` —, langue, graine, format). **Cache partagé entre scènes** : toute paire son +
alignement déjà produite pour cette empreinte (fichiers `assets/ai/voice/<empreinte[:16]>.mp3` et
`.words.json` ; anciens noms `<scène>_<empreinte[:12]>` retrouvés par le verrou, note « requête
<empreinte[:12]> ») est réutilisée : 0 appel, 0 caractère, chemins écrits dans la scène.
**Voix gelée disparue** (entrée du verrou, fichier absent ou modifié — médias non versionnés,
clone neuf) : REFUS sans appel (« restaurez-le depuis votre sauvegarde ») ; `--regenerate` seul la
régénère (nouvelle consommation, après les garde-fous) aux MÊMES chemins, gelés avec `replace`
(champ `replaces` du verrou). **Garde-fous AVANT tout appel payant** : variables
`MOGRAPH_VOICE_BUDGET` / `MOGRAPH_VOICE_CONFIRM_ABOVE` valides (entiers ≥ 0, sinon refus français de
`mograph voice` seulement, `config.VOICE_ENV_ERRORS`) ; caractères = longueur du texte envoyé ;
limite d'une requête du modèle ; budget de la commande (`config.VOICE_BUDGET_CHARS`, `--budget`,
entier ≥ 0) ; quota restant (`quota_now` : le compteur agrégé de l'abonnement reste en retard après
un appel, l'historique de l'heure écoulée le corrige) ; confirmation au-delà de
`config.VOICE_CONFIRM_ABOVE` (seuil strict) sauf `--yes` (refus sans vraie console). Une seule
génération à la fois par empreinte (fichier exclusif `voice/.<empreinte[:16]>.lck`) : une seconde
commande simultanée attend, puis trouve le cache. **Un seul appel**, dont la réponse payée est
écrite AUSSITÔT dans `voice/.<empreinte[:16]>.reponse.json` (contrat `mograph-voice-response/1`,
ignoré par `assets verify`) puis journalisée, avant toute exploitation : si l'alignement est
inexploitable ou le gel échoue, la relance reprend cette réponse sans nouvel appel (statut
« reprise »). Alignement : temps par POINT DE CODE (un élément multi-points — accent décomposé,
émoji — voit ses temps répétés ; normalisation NFC tentée), correspondance position par position
(caractère normalisé par le service accepté), refus si les longueurs diffèrent. Puis gel des deux
fichiers (`assets.add`, source `api`, outil ElevenLabs, prompt = texte, coût, empreinte de la voix ;
alignement de nature `text`, champ `characters_sent` = caractères ENVOYÉS — l'alignement gelé de
`demo_voix` porte l'ancien nom `characters_billed`, même sens), chemins écrits dans la scène,
réponse en attente supprimée. Journal `config.VOICE_USAGE_LOG` (`voice_usage.jsonl`, versionné) :
une ligne par commande — date, scène, empreinte, caractères envoyés `chars_estimated`, décomptés
`chars_billed` (lus dans l'historique du compte : `character_count_change_from` → `_to` de l'élément
de même texte et modèle ; **null** si l'élément n'y figure pas encore ou si l'historique est
illisible, `chars_estimated` restant le majorant ; mesuré le 01/10/2026 : 27 envoyés, 11
décomptés), `cache`, `dry_run`, `reprise`, `erreur` (« réponse perdue après l'envoi » : 200
illisible, délai dépassé ; « interrompu pendant l'appel » : Ctrl+C), quota restant avant l'appel.
`--dry-run` n'écrit ni la scène ni le verrou (voix en cache : « en cache », 0 caractère).
**Secrets** : clé lue dans l'environnement (blancs de bord retirés) puis `config.ENV_FILE` (`.env`,
env `MOGRAPH_ENV_FILE` ; lu comme un shell : guillemets, commentaire de fin de ligne, `export`,
dernière occurrence ; UTF-8 exigé), validée (ASCII imprimable, sans blanc) sans jamais être
affichée ; un identifiant de voix venu de `.env` n'est consigné que par son empreinte ; les détails
d'erreur de l'API sont masqués (voix et clé remplacées, `input` des erreurs de validation jamais
affiché), et la frontière du CLI (`mograph.main`, `cmd_voice`) passe trace et message par `redact`.
API redirigeable (`MOGRAPH_ELEVENLABS_API`) : les tests utilisent un faux serveur local, sans
aucune consommation.

Compilateur : `_load_voice` (après `_setup`) exige `asset` et `alignment` gelés sous le dossier IA
(contrôlés par le verrou comme tout asset IA ; fichier absent : « restaurez-le », `mograph voice` le
retrouve s'il existe sous un autre nom, `--regenerate` sinon), vérifie que `alignment.text` = texte
nettoyé (sinon REFUS « a changé depuis la génération »), place les mots en temps global (`start` =
secondes, `beat:` ou `marker:` audio ; départ < 0 = REFUS ; départ ≥ durée de la vidéo = REFUS,
voix finissant après la vidéo = avertissement « la fin sera coupée »), ajoute les marqueurs `[[id]]`
(doublon = REFUS). Scène compilée : clé `voiceover = {asset, alignment, start, gain_db, duck,
duration, text, model, words[{i, text, start, end}], markers[{id, time}]}` ou null ; avec une voix,
`audio` porte `mix` (build/<scène>/audio/mix.wav) et `normalize` vaut true par défaut (loudness d'un
mixage inconnue d'avance) ; sans musique, `audio.src` est null.

Mixage (`audio.build_mix`, appelé par `audio.ensure_scene_audio`) : voix décodée en 48 kHz stéréo,
placée à l'échantillon `round(start × 48000)` (un départ négatif coupe le début, par défense), gain
`gain_db` ; musique (si présente) multipliée par `1 + w × (10^(duck.db/20) − 1)`, w = enveloppe de
ducking (`audio.duck_envelope` : rampe linéaire d'attaque AVANT chaque passage de parole, 1 pendant,
rampe de relâchement après ; mots fusionnés si silence < `config.VOICE_DUCK_MERGE_S`) ; garde-fou
d'écrêtage à 0,999 ; écriture WAV 24 bits `-bitexact` + fiche `mix.wav.json` (mixage non refait s'il
est à jour). L'encodeur livre `audio.mix` s'il existe, sinon `audio.src` ; une piste muette (mesure
loudnorm infinie) est refusée par `encode.loudnorm_prefix` (EncodeError explicite). L'aperçu fait
entendre la musique, sinon la voix gelée décalée de `voiceover.start` (champ `audio_offset` de
l'API de l'aperçu).

CLI : `mograph voice <scène> [--dry-run] [--yes] [--budget N] [--regenerate]` ; `mograph voice
--quota`. Ctrl+C pendant l'appel : message « il a pu être décompté », ligne de journal.
Tests : `tests/verify_step40_voice.py` (faux serveur, 0 caractère consommé).
