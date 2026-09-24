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
| Disque | C: 98,6 Go libres (build/ déplaçable via `MOGRAPH_BUILD_DIR`) |
| FFmpeg | 8.1.2 build gyan.dev « full » (libx265, prores_ks, libzimg/zscale, premultiply, alphaextract, blackdetect, ebur128) |
| Python orchestrateur | 3.12.10 — venv `.venv` → **`.venv/Scripts/python.exe`** |
| Paquets pip | playwright 1.56.0, jsonschema 4.23.0, opencv-python-headless 4.12.0.88, numpy 2.2.6 (`requirements.txt`) |
| Chromium | Chromium Headless Shell 141.0.7390.37 (playwright build v1194) |
| Node / npm | 24.17.0 / 12.0.2 (sert uniquement à récupérer des fichiers figés : sans effet sur les pixels) |
| npm | gsap 3.15.0, @fontsource/* 5.3.0 (`package.json` + `package-lock.json`) |
| Blender | bpy 5.0.1 sous CPython 3.11 — venv `.venv-blender` → **`.venv-blender/Scripts/python.exe`** |
| Référence Cycles | cube 540², 16 éch., OIDN : **1,09 s/image CPU (24 threads)**, 2,10 s OptiX (surcoût d'init sur scène triviale) |

### Machine 2 : conteneur Linux (reprise du travail, étapes 18 à 20)
| Élément | Valeur |
|---|---|
| OS | Ubuntu 24.04.4 LTS (noyau 6.18), sans GPU |
| CPU / RAM / disque | Intel Xeon 2,10 GHz, 4 cœurs, 16 Go, ~28 Go libres (le minimum de la SPEC) |
| FFmpeg | **6.1.1** (paquet Ubuntu), libzimg 3.0.5 : défauts d'alpha contournés (§7), plage ProRes lue sur l'image (§8) |
| Python / Node | 3.12.3 (`.venv/bin/python`) / Node 22.22.2, npm 10.9.7 ; mêmes versions pip/npm figées |
| Chromium | 141.0.7390.37 (playwright build v1194, identique à la machine 1) |
| Blender | bpy 5.0.1 sous CPython 3.11 (`.venv-blender/bin/python`), Cycles CPU |
| Référence Cycles | scène clay 540², 24 éch., flou 0,5 : **~5 s/image (4 threads)** ; web 1080p : ~0,5 s/image |

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
  numéro d'image sont identiques au bit près en CPU et en OptiX.

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
(`determinism.json` ok). Rapport `out/<scene>/qc_report.json` + `.md` (verdict en tête).

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
