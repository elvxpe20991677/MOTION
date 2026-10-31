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
`QC_CHECKLIST.md` (contrôle humain), `docs/BASELINE.md` (état de référence mesuré : tests, déterminisme,
versions).

---------------------------------------------------------------------------------------------------
## 1. Installation

**En une commande** (idempotente, relançable) :
- Windows 11 : `powershell -ExecutionPolicy Bypass -File .\setup.ps1 -Tests` (ajouter `-Defender`, en
  administrateur, pour exclure `build\` de l'antivirus : écriture des PNG nettement plus rapide) ;
- Linux / macOS / WSL2 : `./setup.sh --tests`.

Ils vérifient les prérequis (FFmpeg avec zscale, Python 3.12, Node, uv), créent les deux venvs,
installent les versions figées, Chromium et bpy, détectent le GPU NVIDIA (OptiX) puis rejouent les
tests rapides. Installation manuelle équivalente :

### Windows 11 (machine de référence, natif — sans WSL)
Prérequis : git, Python 3.12, Node.js (≥ 22), uv, FFmpeg ≥ 6.1 compilé avec libx265, prores_ks et
**libzimg** (le build « full » de gyan.dev convient : `winget install Gyan.FFmpeg`).

```powershell
git clone <dépôt> mograph ; cd mograph
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt -c constraints.txt
.\.venv\Scripts\python.exe -m playwright install chromium
npm ci
uv venv --python 3.11.15 .venv-blender
uv pip install --python .venv-blender\Scripts\python.exe bpy==5.0.1 -c constraints-blender.txt
```

### Ubuntu 24.04 / WSL2 / macOS
```bash
sudo apt install -y git curl build-essential tmux mediainfo mpv ffmpeg \
  libxrender1 libxxf86vm1 libxfixes3 libxi6 libxkbcommon0 libsm6 libgl1 libegl1
python3.12 -m venv .venv && .venv/bin/pip install -r requirements.txt -c constraints.txt
.venv/bin/python -m playwright install --with-deps chromium
npm ci
curl -LsSf https://astral.sh/uv/install.sh | sh
uv venv --python 3.11.15 .venv-blender && uv pip install --python .venv-blender/bin/python bpy==5.0.1 -c constraints-blender.txt
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
**Baseline** : `scripts/baseline.py run --label <nom>` rejoue tous les scripts de vérification, rend
`demo_flat_riso` de bout en bout (porte de déterminisme, maître, encodage, QC) et relève toutes les
empreintes ; `scripts/baseline.py compare docs/baseline/phase-0.json build/_baseline/<nom>/baseline.json`
signale toute régression (code ≠ 0). Référence : `docs/BASELINE.md`.

FFmpeg 6.1.1 (Ubuntu 24.04) convient : ses conversions d'alpha inexactes (swscale, zscale) sont
contournées par l'encodeur (voir `docs/CONTRACT.md` §7), vérifié par `tests/verify_step15_encode.py`.

Versions figées : `versions.json` (références), `requirements.txt` + `constraints.txt` (pip, dépendances
indirectes comprises), `package.json` + `package-lock.json` (npm), `bpy==5.0.1` + `constraints-blender.txt`.
`scripts/check_versions.py` vérifie l'installation (FFmpeg 9.0.2 gyan.dev « full » sur la machine de
référence ; `--profile ci-linux` sous Ubuntu, où FFmpeg est la 6.1.1 du paquet). Toute mise à jour peut
changer des pixels : refaire la porte de déterminisme et la baseline.

**VS Code** : ouvrir le dossier suffit. `.vscode/settings.json` associe les schémas aux scènes,
modèles et presets : autocomplétion des propriétés, des valeurs permises et erreurs soulignées à la
frappe. **CI** : `.github/workflows/tests.yml` rejoue les tests à chaque push : deux jobs Ubuntu 24.04
(FFmpeg 6.1.1, écart documenté) et un job Windows avec FFmpeg 9.0.2 épinglé (Chocolatey `ffmpeg-full`, même
build que la machine de rendu) ; chaque job vérifie d'abord les versions (`scripts/check_versions.py`).

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
| `python mograph.py all <scène> [--detach]` | plaques → web → déterminisme → maître → encodage → planches → QC → **signature C2PA** (si `provenance.c2pa.sign`) |
| `python mograph.py all <scène> --draft` | **préversion** rapide (1080p, 3D réduite, sans flou, sans QC) → `out/<scène>_draft/` |
| `python mograph.py validate <scène> --layout` | contrôle des zones sûres en quelques secondes, avant le rendu complet |
| `python mograph.py preview <scène>` | **aperçu en direct** dans le navigateur, rechargé à chaque sauvegarde |
| `python mograph.py new <id> --template titre` | nouvelle scène depuis un modèle (`new --list` : les modèles) |
| `python mograph.py batch <scènes…>` | rend plusieurs scènes (motifs acceptés : `"scenes/promo_*.json"`) |
| `python mograph.py batch <modèle> --data lignes.csv` | **déclinaisons** : une vidéo par ligne du CSV |
| `python mograph.py beats <musique> [--scene <scène> --write] [--json]` | tempo, premier temps fort, drop et dérive détectés ; musique d'IA : gelée d'abord |
| `python mograph.py sheets <scène>` | planches contact (`out/<scène>/planche_contact.png`, `planche_alpha.png`) |
| `python mograph.py sheets <scène> [--draft] --vision` | + **relecture visuelle** par Claude (abonnement) : avertissements seulement, jamais bloquants |
| `python mograph.py brief "<idée>" [--preset P] [--aspect A] [--id ID] [--draft]` | **une idée → une scène VALIDE** écrite par Claude (`claude -p`, abonnement ; 3 essais au plus) |
| `python mograph.py c2pa init` / `c2pa sign <scène>` / `c2pa verify <fichier>` | **provenance C2PA** : certificat local, signature des livrables (dernière étape), vérification |
| `python mograph.py mcp` | serveur MCP local « mograph » (stdio) : piloter le pipeline depuis Claude Code ou Claude Desktop |
| `python mograph.py assets add <fichier> --tool … --model … --prompt … --license …` | **geler** une sortie d'IA (copie sous `assets/ai/`, SHA-256 + provenance dans `assets.lock.json`) |
| `python mograph.py assets verify` / `assets list` | assets IA présents et intacts / provenance |
| `python mograph.py voice <scène> [--dry-run] [--yes] [--budget N] [--regenerate]` | **voix off** ElevenLabs du bloc `voiceover` : estimation, garde-fou de quota, cache par empreinte (même requête, même dans une autre scène = 0 caractère), gel |
| `python mograph.py voice --quota` | quota ElevenLabs du compte (lecture seule) |
| `python mograph.py synth-beat --bpm 120 --duration 5 --out assets/audio/beat.wav` | piste test |
| `python mograph.py status <scène> [--draft]` | avancement (y compris d'un rendu détaché) |
| `python mograph.py versions <scène>` | archive les versions dans `out/<scène>/versions.txt` |

### Flux de travail conseillé
1. `new ma_video --template titre` (ou partir d'une démo) ;
2. `preview ma_video` : modifier le JSON, sauvegarder, regarder (lecture, ←/→ image par image, son) ;
3. `validate ma_video --layout` : zones sûres garanties avant de lancer le rendu ;
4. `all ma_video --draft` : préversion encodée à montrer ;
5. `all ma_video --detach` : rendu final, QC, planches contact.

### Déclinaisons (CSV)
Dans la scène modèle, écrire `{{colonne}}` partout où une valeur change (`"text": "{{nom}}"`,
`"value": "{{ca}}"`). Le CSV (UTF-8, séparateur `,` ou `;`, export Excel accepté) a une ligne
d'en-têtes ; la colonne facultative `id` nomme chaque vidéo (sinon `<id>_001`, `<id>_002`…). Une
cellule seule dans sa chaîne devient un nombre si c'en est un (`30,5` → 30.5). Les scènes générées
sont relisibles dans `build/_variants/<modèle>/` ; le bilan du lot dans `build/_batch/`.

### Musique
`beats <fichier>` (wav, mp3, m4a, ou vidéo) estime le tempo (± 0,1 BPM), le premier temps fort
(± 5 ms) et le « drop » (plus forte hausse d'énergie d'une mesure à la suivante), avec un indice de
confiance et la dérive de la grille ; `--scene <scène> --write` écrit le bloc `audio` (src, bpm,
offset, marqueur `drop`, et la fiche `analysis` : empreinte du fichier analysé, grille mesurée) en
reprenant la mesure et la plage de tempo déjà choisies pour la scène. Même fichier → même analyse,
au bit près. Tempo fixe uniquement : un tempo variable, une rupture ou une syncope tenue sont
détectés (dérive > 40 ms) et signalés ; vérifiez à l'oreille et calez alors les moments clés par
marqueurs. `"audio": {"normalize": true}` ramène la piste
à `qc.loudness_target_lufs` (mesure puis gain linéaire, true peak ≤ −1,5 dBTP).

Musique générée par une IA (Eleven Music dans l'interface web d'ElevenLabs, Lyria dans l'application
Gemini) : téléchargez-la, gelez-la une fois, puis calez la scène dessus :
```bash
python mograph.py assets add musique.mp3 --tool "ElevenLabs Music" --model music_v1 --prompt "…" --license "Conditions Eleven Music, offre payante : usage commercial hors film, TV, radio et jeux « Studio »" --source manual_import
python mograph.py beats assets/ai/audio/musique.mp3 --scene ma_scene --write
```
`beats` refuse une musique de `assets/ai/` qui n'est pas gelée. Eleven Music (offres payantes) :
usage commercial autorisé sauf film, TV, radio et jeux « Studio » ; Lyria : vérifiez les conditions
avant un usage commercial (filigrane SynthID dans le fichier).

### Couche agent (Claude pilote MOTION)
Aucun appel à l'API facturée : Claude est appelé par la CLI `claude -p` de votre **abonnement**. Une
fois pour toutes : `claude auth login` (compte claude.ai). mograph retire toute clé API et tout
choix de fournisseur de l'environnement de l'appel, ignore les réglages de Claude Code, vérifie AVANT
l'appel (`claude auth status`) que la CLI est connectée par l'abonnement claude.ai et refuse sinon
(clé API, assistant de clé, fournisseur cloud) ; un lanceur npm `claude.cmd` n'est jamais exécuté
(le `claude.exe` qu'il appelle est utilisé).
- `brief "Annonce de l'ouverture d'un café, ton chaleureux, 6 s"` : Claude remplit un schéma de scène
  simplifié (mouvements et couleurs du preset), le compilateur valide, ses messages de refus sont
  renvoyés à Claude ; **3 essais au plus**. Résultat versionné : `scenes/<id>.json` (pas le prompt) ;
  bilan dans `build/_brief/`. `--draft` enchaîne la préversion. Modèle : `MOGRAPH_CLAUDE_MODEL`
  (défaut `sonnet`, économe en quota).
- `sheets <scène> --vision` : Claude lit les planches contact et signale texte coupé, contraste,
  chevauchements… dans `out/<scène>/vision_qc.md`. **Jamais bloquant**, hors QC et hors porte de
  déterminisme.
- **Serveur MCP** : `.mcp.json` déclare le serveur `mograph` (outils `validate`, `preview`, `draft`,
  `all`, `status`, `beats`, `sheets`, sorties JSON structurées). `draft`, `all` et `beats` (écriture)
  exigent `"confirm": true`. Claude Code demande d'approuver le serveur à la première ouverture du
  projet. Le chemin `.venv/Scripts/python.exe` est celui de Windows (Linux/macOS : `.venv/bin/python`).
- **Skill** `.claude/skills/mograph/SKILL.md` : Claude Code y trouve la démarche et les dix règles
  d'écriture d'une scène (`docs/GUIDE_SCENES.md`).
- `validate`, `status`, `sheets` et `beats` acceptent `--json` (un seul objet JSON sur la sortie
  standard) pour les scripts. CLI introuvable : `MOGRAPH_CLAUDE=<chemin de claude>`.

### Rendu en parallèle
Les plans sans plaque 3D sont rendus par Chromium **pendant** que Blender calcule les plaques
(gain total avec un GPU). `MOGRAPH_MAX_WORKERS` (défaut 4) règle le nombre de navigateurs
simultanés : sur 16 threads, essayer 6 à 8 et garder la valeur la plus rapide.

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
  `qc_report.json`, `qc_report.md`, `versions.txt`, `planche_contact.png` (+ `planche_alpha.png`),
  `vision_qc.json` / `vision_qc.md` (relecture visuelle, si demandée).
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
  "provenance": { "authors": ["…"], "ai_disclosure": { "ai_used": true, "roles": ["voice"],
                  "tools": [ { "name": "ElevenLabs", "model": "…", "role": "voix off" } ],
                  "date": "2026-10-01", "statement": "Voix off synthétique" } },
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
`transition_in` : `cut`, `crossfade` (fondu), `wipe` (volet) ou `push` (le plan entrant pousse le
précédent) ; forme objet `{ "type": "wipe", "dur": 0.4, "direction": "left|right|up|down" }`
(left = le plan entrant arrive par la droite). Toute transition autre que `cut` **chevauche** les
deux plans : total = Σ images − Σ images de chevauchement. Mélange en alpha prémultiplié, progression
smoothstep ; la poussée translate d'un nombre entier de pixels (bords nets).

### Calques (communs)
`id, type (text|shape|image|sequence|chart|video|subtitles), x, y, anchor, z, rotation, scale, opacity, blend,
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
- `video` : `src (assets/….mp4|mov|webm…), w, h, fit (cover|contain|fill), start (s), loop`. FFmpeg
  extrait l'image k du plan = la source à `start + k / fps`, déjà cadrée, dans
  `build/<scène>/media/` (réutilisée tant que la source et les réglages ne changent pas) : la page
  ne lit jamais la vidéo, le rendu reste déterministe. Source plus courte que le plan : dernière
  image tenue (avertissement) ou `"loop": true`. Animable comme tout calque (`anim`). Son de la
  vidéo ignoré (mettre la piste dans `audio`).
- `subtitles` : `src (assets/….srt|vtt), offset, fade, style, size, color, align, maxWidth, x, y,
  anchor` — temps du fichier = temps **global** de la vidéo ; chaque réplique devient un calque
  texte (fondus d'entrée/sortie `fade`, 0,12 s par défaut), placé par défaut en bas de la zone sûre
  titre, largeur 90 % de la zone ; balises `<i>`, `{\an8}` retirées. Glyphes vérifiés comme tout
  texte (aucune police de repli).
- `ai_clip` : `asset (assets/ai/….mp4|mov…), w, h, fit, start, loop` — clip vidéo produit par une IA
  et **gelé** (`assets add`) ; rendu exactement comme `video` (extraction FFmpeg image par image, alpha
  conservé). Le rendu **échoue** si le clip n'est pas dans `assets.lock.json` ou si son empreinte a
  changé.

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
cone|plane|cyclorama), size, minor, bevel, segments, shading, location, rotation (degrés), scale,
material, anim}]`.
Canaux : `{prop: location|rotation|scale|target, keys: [{t, v, ease}]}` — `ease` s'applique au
segment qui **arrive** sur la clé. Afficher une plaque : calque `{"type": "sequence", "plate": "<id>"}`.
**Low-poly** : `"segments": 6, "shading": "flat"` (sphère, cylindre, cône, tore) donne des facettes
visibles ; un petit nombre de segments allège aussi les scènes à milliers d'objets (particules).

### Effets (`fx` par plan)
Surcharges des textures du preset (`grain, paper, vignette, scanlines, tracking` : `false`,
nombre = intensité, ou objet) et `glitch {windows [[at, dur]], on_beats {from, to, every, length},
amount, colors, mode, layers}`.

### Voix off (`voiceover`)
```jsonc
"voiceover": { "text": "Bienvenue chez Ondea. [[logo]] Le son, enfin libre.", "start": 0.5,
               "duck": { "db": -12 } }   // asset et alignment sont écrits par « mograph voice »
```
`python mograph.py voice <scène>` génère la voix UNE fois (ElevenLabs, voix et clé lues dans `.env`),
avec garde-fous : estimation des caractères (`--dry-run`), budget par commande, quota restant lu sur le
compte, confirmation au-delà de 200 caractères (`--yes`). Rejouer la même requête (texte, voix, modèle,
réglages), dans cette scène ou une autre, coûte 0 caractère tant que les fichiers gelés sont sur le
disque. Le son et l'alignement mot à mot sont gelés sous `assets/ai/voice/` ; ces médias ne sont pas
versionnés : sur un autre poste, restaurez-les depuis votre sauvegarde (une voix gelée disparue n'est
JAMAIS régénérée sans `--regenerate`, qui consomme à nouveau). Une réponse payée est conservée dès sa
réception : un échec ensuite ne fait pas repayer. Chaque commande est journalisée dans
`voice_usage.jsonl`. Dans la scène : `"at": "word:3"` (début du 4e mot de la voix),
`[[logo]]` dans le texte → `"marker:logo"`, sous-titres automatiques `{"id": "st", "type":
"subtitles", "voice": true}`. Au rendu, la voix est mixée avec la musique, qui baisse pendant la parole
(ducking, `duck.db`, -12 dB par défaut), puis normalisée à `qc.loudness_target_lufs`.

### Provenance et IA
`provenance` (facultatif) : `authors`, `tools [{name, version, role}]` et `ai_disclosure {ai_used,
roles, tools [{name, model, role}], date, statement}` (rôles : scene, text, image, video, music,
voice, subtitles, 3d, matte, other). Tout fichier placé sous `assets/ai/` est considéré comme produit
par une IA : sans déclaration `ai_used: true`, la compilation avertit et le QC émet un WARN (non
bloquant) `scene.ai_disclosure`. La déclaration est reprise dans la section « Provenance » de
`qc_report.md`.

**Assets IA gelés** : une sortie d'IA est générée UNE fois puis gelée par `mograph assets add` (copie
sous `assets/ai/<nature>/`, SHA-256 et provenance — outil, modèle, date, prompt, graine, licence,
coût — dans `assets.lock.json`). Le rendu n'appelle jamais une IA. Tout fichier de `assets/ai/`
utilisé par une scène (calque `ai_clip`, `video`, `image`, sous-titres, audio) doit être gelé et
intact : sinon la compilation, le rendu, l'encodage et le QC (`scene.ai_lock`, bloquant) échouent.
La provenance de chaque asset figure dans `qc_report.md`. Seul le verrou est versionné : les médias
de `assets/ai/` ne le sont pas (dépôt public, licences) — voir `assets/ai/README.md`.

**Manifeste C2PA** (phase 7) : `"provenance": {"c2pa": {"sign": true}}` fait signer chaque livrable à la
fin de `all`, APRÈS le QC (seulement s'il est CONFORME), avec un certificat local créé une fois par
`python mograph.py c2pa init` (`.c2pa/`, jamais versionné ; signataire « non reconnu » par les lecteurs,
signature intègre). Le manifeste déclare le type de source IPTC (`compositeSynthetic` avec un asset IA,
`algorithmicMedia` sinon ; `digital_source_type` pour l'imposer), la déclaration d'IA et les assets IA
gelés (empreintes, outils, modèles — jamais les prompts). **Tout réencodage, et même un remux
`-c copy`, supprime le manifeste** : ne plus toucher aux fichiers signés ; `c2pa sign <scène>` resigne
sans réencoder. Vérification : `c2pa verify <fichier>` (SDK + c2patool, installé par
`python scripts/install_c2patool.py`). MP4 (HEVC, AV1) et MOV (ProRes) testés.
**Mention visible** : `"visible_label": {"text": "Contenu créé avec l'aide de l'IA", "corner":
"bottom-right"}` l'ajoute à chaque plan, dans la zone sûre. Rappel AI Act (art. 50, applicable depuis
le 02/08/2026) : le marquage lisible par machine est une obligation des FOURNISSEURS d'IA générative ;
l'utilisateur doit déclarer un hypertrucage (forme allégée pour une œuvre manifestement créative).
Détail et sources : `docs/CONTRACT.md` §11.8 (information, pas un conseil juridique).

### Livrables
`hevc_main10` (.mp4, `hvc1`, yuv420p10le), `prores_422hq` (.mov, timecode 01:00:00:00),
`prores_4444` (.mov, alpha 16 bits, **obligatoire** si `format.alpha`), `av1_nvenc` (.mp4, AV1 Main
10 bits par le GPU NVIDIA — Ada ou plus récent —, `cq` 1–63, défaut 30 ; ses octets dépendent du GPU et
du pilote : le déterminisme se vérifie sur le maître). Une sortie opaque d'une scène alpha est aplatie
sur noir.

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
## 7. Contrôle qualité automatique
En plus des contrôles de l'étape 17 (codec, profil, fps, images comptées, hvc1, bt709/tv, timecode,
alpha, noir, son, loudness, zones sûres, déterminisme), chaque livrable passe un **contrôle des
niveaux** sur toutes les images, **informatif** (jamais bloquant) : codes réservés (< 4 ou > 1019 en
10 bits) = avertissement ; puis, selon **EBU R103 v3.0**, Y et R'G'B' filtrés comme le recommande la
norme : avertissement si plus de 1 % d'une image sort de la plage préférée 20..984 (gamut) ou de la
plage nominale 64..940 à ±1 % près (programme pré-produit). Les dépassements bénins, plus petits, sont
mesurés et affichés dans le rapport mais non signalés (détails : `docs/CONTRACT.md` §11.1). Contrôles de
scène ajoutés : `scene.ai_disclosure` (informatif) vérifie la déclaration d'usage de l'IA ;
`scene.ai_lock` (**bloquant**) vérifie que chaque asset de `assets/ai/` utilisé est gelé, présent et
intact (voir « Provenance et IA »). Les **planches contact** (`out/<scène>/planche_contact.png`, et `planche_alpha.png` sur fonds
clair et sombre pour une scène transparente) montrent les coupes, transitions, temps musicaux et
marqueurs : la relecture humaine commence par elles.

---------------------------------------------------------------------------------------------------
## 8. Limites connues
- Source web en 8 bits par canal (16 bits seulement avec le flou de mouvement).
- Pas de P3 ni de HDR : chaîne sRGB → Rec.709 SDR uniquement.
- EEVEE et Grease Pencil exigent un GPU (EEVEE sans GPU : 68 s pour un cube) : le pipeline utilise Cycles.
- Zones sûres 9:16 approximatives (interfaces TikTok / Reels / Shorts évolutives).
- Détection des temps (`beats`) pour les morceaux à tempo FIXE seulement : un tempo variable est
  détecté et signalé (dérive), pas suivi (CONTRACT §11.6).
- Aperçu en direct : fidèle mais pas au pixel près (navigateur de bureau) ; seul `all` fait foi.
- Rendu logiciel lent sans GPU (Chromium SwiftShader, Cycles CPU).
- Windows : pas de `tmux` ; `--detach` le remplace. `--font-render-hinting=none` n'a d'effet que
  sous Linux ; le déterminisme est garanti par machine, pas entre systèmes.
