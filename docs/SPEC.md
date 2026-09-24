# SPÉCIFICATION DE MISSION (texte de référence, fourni par l'utilisateur)

> Ce fichier reproduit la mission d'origine. Les adaptations à la plateforme réelle
> (Windows 11 natif, pas de WSL) et les décisions d'interface sont dans `docs/CONTRACT.md`,
> qui fait foi en cas de divergence d'implémentation.

# MISSION
Tu es directeur artistique et directeur technique motion design. Construis, étape par étape, un
système qui transforme une scène JSON en vidéo motion design professionnelle, rendue image par
image de façon déterministe, exportée en HEVC 10 bits (tag hvc1) et en ProRes (422 HQ, ou 4444
si alpha), puis contrôlée automatiquement.

Architecture imposée (validée) : HYBRIDE.
- Web natif (Chromium headless + GSAP) : colonne vertébrale 2D ET compositeur final.
- Blender (module bpy, moteur Cycles) : plaques 3D intégrées comme calques de la page web.
- FFmpeg : seul maillon de sortie (conversion couleur, encodage, contrôle).
- Un seul format de scène JSON pour tout. DaVinci Resolve : finition et scopes, hors boucle.

# RÈGLES DE TRAVAIL
1. Code complet et exécutable uniquement : jamais de pseudo-code ni de fichier tronqué.
2. Après chaque étape, exécute la VÉRIFICATION et montre la sortie. Étape suivante seulement si elle est verte.
3. N'affirme jamais qu'une chose est rendue ou testée sans l'avoir exécutée.
4. Chaque choix technique est justifié en une phrase en commentaire.
5. Versions figées partout (pip, npm, bpy).
6. Rendus longs : processus détachés (setsid ou tmux) et reprise image par image.

# MATÉRIEL
- Minimum : CPU 4 cœurs, 16 Go de RAM, 100 Go libres en SSD (les séquences PNG sont lourdes :
  1080p 8 bits ≈ 2 à 4 Mo par image, 4K 16 bits ≈ 20 à 40 Mo).
- Recommandé : 8 cœurs ou plus, GPU NVIDIA (Cycles OptiX, EEVEE utilisable).
  Sans GPU, EEVEE est inutilisable (mesuré : 68 s pour un cube) : utiliser Cycles CPU.
- Moniteur calibré Rec.709 (gamma 2.4, 100 cd/m²) pour le contrôle humain.

# LOGICIELS (TOUS)
| Logiciel | Version | Rôle | Obligatoire |
|---|---|---|---|
| Ubuntu 24.04 (ou macOS 14+, ou Windows 11 + WSL2 Ubuntu 24.04) | — | Système | oui |
| git, curl, build-essential | système | Versionnage, téléchargements | oui |
| tmux | système | Rendus longs détachés | oui |
| FFmpeg + ffprobe, avec libx265, prores_ks, libzimg (zscale) | ≥ 6.1 | Encodage, conversion couleur, QC | oui |
| Python | 3.12 | Orchestrateur | oui |
| playwright (Python) + Chromium | 1.56.0 | Rendu web sans écran | oui |
| jsonschema | 4.23.0 | Validation des scènes et presets | oui |
| opencv-python-headless (+ numpy 2.2.6) | 4.12.0.88 | PNG 16 bits, flou de mouvement, fondus | oui |
| Node.js + npm | 22 LTS | Récupération des bibliothèques navigateur | oui |
| gsap (+ CustomEase) | 3.15.0 | Timeline d'animation | oui |
| @fontsource : bricolage-grotesque, dm-sans, anton, space-grotesk, vt323, space-mono, caveat, patrick-hand, fraunces, inter-tight | 5.3.0 | Polices embarquées | oui |
| uv (Astral) | récent | Fournit CPython 3.11 pour bpy | oui |
| Python (via uv) + bpy | 3.11 + 5.0.1 | Backend 3D | oui |
| Bibliothèques Linux pour bpy : libxrender1 libxxf86vm1 libxfixes3 libxi6 libxkbcommon0 libsm6 libgl1 libegl1 | système | Dépendances de bpy | oui (Linux) |
| mediainfo | système | Contrôle humain des fichiers | recommandé |
| mpv ou VLC | récent | Lecture image par image | recommandé |
| Blender (application) | 5.0 | Développement du look 3D, inspection | recommandé |
| DaVinci Resolve Studio | 21 | Finition, scopes, contrôle broadcast | recommandé |
| VS Code (ou autre éditeur) | récent | Édition | recommandé |

# ÉTAPES

## Étape 1 — Machine
FAIRE :
  sudo apt update && sudo apt install -y git curl build-essential tmux mediainfo mpv \
    libxrender1 libxxf86vm1 libxfixes3 libxi6 libxkbcommon0 libsm6 libgl1 libegl1
  mkdir -p ~/mograph && cd ~/mograph && git init
  (macOS : brew install git tmux mediainfo mpv ffmpeg node uv python@3.12)
VÉRIFIER : git --version ; nproc ; free -h ; df -h . (au moins 100 Go libres)

## Étape 2 — FFmpeg
FAIRE : sudo apt install -y ffmpeg   (Ubuntu 24.04 fournit la 6.1.1)
VÉRIFIER :
  ffmpeg -version | head -1                                   # >= 6.1
  ffmpeg -hide_banner -encoders | grep -E "libx265|prores_ks"   # les 2 présents
  ffmpeg -hide_banner -filters | grep -E " zscale | premultiply | alphaextract | blackdetect | ebur128 "
SI zscale MANQUE : installer un build statique de FFmpeg compilé avec libzimg, puis revérifier.

## Étape 3 — Python orchestrateur et Chromium
FAIRE :
  python3.12 -m venv .venv && source .venv/bin/activate
  pip install playwright==1.56.0 jsonschema==4.23.0 opencv-python-headless==4.12.0.88
  python -m playwright install --with-deps chromium
VÉRIFIER (le rendu 2x doit être un VRAI 2x) :
  python - <<'EOF'
  import cv2, numpy as np
  from playwright.sync_api import sync_playwright
  with sync_playwright() as p:
      b = p.chromium.launch()
      pg = b.new_context(viewport={"width": 400, "height": 200}, device_scale_factor=2).new_page()
      pg.set_content("<div style='width:100px;height:40px;background:red'></div>")
      img = cv2.imdecode(np.frombuffer(pg.screenshot(scale="device"), np.uint8), -1)
      print(pg.evaluate("devicePixelRatio"), img.shape)   # attendu : 2 (400, 800, 3)
      b.close()
  EOF

## Étape 4 — Bibliothèques navigateur
FAIRE : écrire package.json avec les versions exactes du tableau, puis npm install.
VÉRIFIER :
  ls node_modules/gsap/dist/gsap.min.js node_modules/gsap/dist/CustomEase.min.js
  ls node_modules/@fontsource/fraunces/files/fraunces-latin-800-normal.woff2

## Étape 5 — Blender (bpy)
FAIRE :
  curl -LsSf https://astral.sh/uv/install.sh | sh
  uv venv --python 3.11 .venv-blender
  uv pip install --python .venv-blender/bin/python bpy==5.0.1
VÉRIFIER :
  .venv-blender/bin/python -c "import bpy; print(bpy.app.version_string)"   # 5.0.1
  Rendre un cube en Cycles CPU, 540x540, 16 échantillons, débruitage OIDN : noter le temps
  par image (référence mesurée sur 1 cœur : 5,5 s ; scène clay réelle avec flou : ~21 s).
RÈGLE : toujours importer bpy AVANT bmesh et mathutils (sinon ModuleNotFoundError).

## Étape 6 — Arborescence
  mograph.py                 CLI
  pipeline/                  config.py scene.py web_render.py blender_render.py compose.py
                             encode.py audio.py qc.py __init__.py
  backends/blender_backend.py
  runtime/                   clock.js player.html runtime.css runtime.js
  schema/                    scene.schema.json preset.schema.json
  presets/                   6 presets JSON
  scenes/                    scènes démo
  assets/audio/  build/  out/
  README.md  QC_CHECKLIST.md
VÉRIFIER : tree -L 1 (ou ls -R)

## Étape 7 — config.py
FAIRE : centraliser tout ce qui influence la sortie :
- chemins du projet, origine fictive "http://mograph.render" ;
- canevas logiques : 16:9 = 1920x1080, 9:16 = 1080x1920, 1:1 = 1080x1080 ;
- échelle : 1080p = facteur 1, 4K = facteur 2 (même mise en page, rastérisée en 2x) ;
- zones sûres (haut, droite, bas, gauche) :
  - 16:9 et 1:1 : graphiques 5 % partout, action 3,5 % (EBU R95) ;
  - 9:16 : titres 12 / 12 / 22 / 6 %, action 8 / 8 / 15 / 4 % (interfaces TikTok, Reels, Shorts) ;
- arguments Chromium :
  --use-angle=swiftshader --enable-unsafe-swiftshader --force-color-profile=srgb
  --font-render-hinting=none --disable-lcd-text --disable-checker-imaging
  --run-all-compositor-stages-before-draw --disable-background-timer-throttling
  --disable-renderer-backgrounding --disable-backgrounding-occluded-windows
  --hide-scrollbars --mute-audio ;
- table GSAP -> Blender :
  sine=SINE, power1=QUAD, power2=CUBIC, power3=QUART, power4=QUINT, expo=EXPO, circ=CIRC,
  back=BACK, elastic=ELASTIC, bounce=BOUNCE ; directions in/out/inOut = EASE_IN/EASE_OUT/EASE_IN_OUT.
VÉRIFIER : python -c "import pipeline.config as c; print(c.LOGICAL_CANVAS, c.SAFE_ZONES['9:16'])"

## Étape 8 — Schémas JSON (draft 2020-12)

scene.schema.json :
- Racine : version "1.0", id, title, brief, seed, preset, preset_overrides.
- format :
  - aspect : 16:9 | 9:16 | 1:1 ;
  - resolution : 1080p | 4k ;
  - fps : 24 | 25 | 30 | 50 | 60 ;
  - alpha : booléen.
- quality : plate_scale (0,25 à 1), blender_samples, motion_blur {enabled, samples, shutter}.
- audio : src, bpm, offset, markers [{id, time}].
- storyboard : [{shot, intent, notes}].
- outputs : [{profile : hevc_main10 | prores_422hq | prores_4444, crf, suffix}].
- qc : safe_zone, safe_zone_tolerance_s, allow_black_s, determinism_frames, loudness_target_lufs.
- shots : [{id, duration, background, transition_in : cut | crossfade, fx, plates, layers}].
- Références de temps :
  - nombre = secondes locales au plan ;
  - "beat:N" ou "marker:id", avec décalage ±x, = temps global ;
  - durées : secondes ou "beats:N".
- Tween : move, at, dur, from, to, ease, stagger, target (self | chars | words | lines | shape | bars),
  revealDir, allowLinear, sync (start | end).
- Calques communs : id, type (text | shape | image | sequence | chart), x, y, anchor, z, rotation,
  scale, opacity, blend, safe, boil, anim.
- Champs par type de calque :
  - text : text, style, size, weight, color, align, maxWidth, lineHeight, tracking, case, split ;
  - shape : shape (rect | ellipse | line | path | polygon), w, h, radius, fill, stroke,
    strokeWidth, d, points ;
  - image : src, w, h, fit ;
  - sequence : plate, w, h ;
  - chart : data [{label, value, color}], max, gap, barRadius, colors, valueFormat,
    labelStyle, valueStyle, grow {at, dur, stagger, ease}.
- Plaques 3D :
  - racine : id, transparent, world {color, strength, sky} ;
  - caméra : location, target, lens, fstop, anim ;
  - lumières : AREA | SUN | POINT | SPOT ;
  - objets : primitive (sphere, cube, rounded_cube, torus, cylinder, cone, plane, cyclorama),
    size, minor, bevel, location, rotation en degrés, scale, material, anim.
  - Canaux d'animation : prop (location | rotation | scale), keys [{t, v, ease}], où ease
    s'applique au segment qui ARRIVE sur la clé.
- Glitch : windows [[at, dur]], on_beats {from, to, every, length}, amount, colors, layers.

preset.schema.json :
- Champs obligatoires :
  - id, name, styles ;
  - palette : bg, fg, primary, secondary, accent (+ couleurs libres, en hexadécimal) ;
  - typography : display, body, avec family, weight, tracking, lineHeight, case ;
  - fonts : [{family, weight, file relatif à node_modules}] ;
  - motion :
    - eases : default, enter, exit, emphasis, anticipate, settle ;
    - customEases, durations {short, medium, long}, stagger, frame_step ;
    - moves : { nom : [étapes {offset, dur, from, to, ease, stagger, target}] } ;
  - textures, composition, transitions, render, color {working : srgb, output : rec709}.
- Champ optionnel blender : samples, adaptive_threshold, denoise, view_transform, look, bounces,
  motion_blur_shutter, lens, fstop, world, materials, light_rig.
VÉRIFIER : jsonschema.Draft202012Validator.check_schema(...) sur les 2 schémas, sans erreur.

## Étape 9 — Les 6 presets
FAIRE (chaque preset entièrement défini, palettes non génériques) :
- flat_riso (flat 2D, infographie, dataviz, explainer, interfaces) :
  - palette : fond #EEF1F6, texte #1B1F4A, bleu #2F5BEA, rose #FF4F9A, jaune #FFC93C ;
  - polices : Bricolage Grotesque 800 + DM Sans 500 ;
  - easings personnalisés : flatSettle "M0,0 C0.12,0.9 0.24,1 1,1"
    et flatAnticipate "M0,0 C0.35,-0.28 0.2,1.12 1,1" ;
  - mouvements : rise, chars_up, pop, wipe_in, draw, slide_anticipate, exit_down ;
  - textures : grain 0.07, papier 0.08 ;
  - transition : fondu 0,4 s.
- kinetic_signal (typo cinétique) :
  - palette : fond #2A1FE6, texte #FFFFFF, orange #FF4B23 ;
  - polices : Anton en capitales + Inter Tight 600 ;
  - mouvements : slam (avec écrasement), chars_drop, slide_anticipate, punch_out ;
  - flou de mouvement : 8 échantillons ;
  - transition : coupe franche.
- vhs_glitch :
  - palette : fond #0D0A1C, cyan #37F2FF, magenta #FF2BD1, ambre #FFB21C ;
  - polices : VT323 + Space Mono ;
  - easings en escalier steps(n) ;
  - textures : balayage, bande de tracking, vignette, grain 0.16 ;
  - pas de flou de mouvement.
- handmade_chalk :
  - palette : ardoise #26352C, craie #F1EEE4, jaune #F4D35E, rose #F28FAD, bleu #8FC9EA ;
  - polices : Caveat 700 + Patrick Hand ;
  - animation tenue deux images (frame_step 2), trait qui bouille (boil), papier en mode screen.
- clay_3d (Blender) :
  - palette : fond #E9E4FF, lilas #8C7BFF, pêche #FF9F7A, menthe #62D2B4, beurre #FFD66B ;
  - polices : Fraunces 800 + DM Sans 500 ;
  - rendu : Cycles, AgX + look Base Contrast ;
  - rebonds lumineux : total 6 / diffus 3 / brillant 2 / transmission 0, sans caustiques ;
  - matériaux : pâte mate veloutée (rugosité 0,5, sheen 0,35) ;
  - éclairage : 3 sources surfaciques en disque (clé, remplissage, contre-jour).
- photoreal_3d (Blender) :
  - palette : graphite #1E2024, aluminium #C9CED6, laiton #D4A373 ;
  - police : Inter Tight 300 / 600 ;
  - rendu : 512 échantillons, AgX Medium High Contrast, ciel physique, f/2.8 ;
  - matériaux : métaux et céramique.
VÉRIFIER : chaque preset passe preset.schema.json, et chaque fichier de police existe.

## Étape 10 — Runtime navigateur (runtime/)

clock.js (injecté AVANT tout script via add_init_script) :
- Math.random = PRNG mulberry32 avec graine ;
- Date et performance.now virtuels, époque 2025-01-01 UTC ;
- window.__MOGRAPH_CLOCK__.set(ms) pour avancer le temps.

player.html : charge runtime.css, gsap.min.js, CustomEase.min.js, puis runtime.js.

runtime.js — préparation :
- Lire window.__MOGRAPH_SHOT__.
- Polices : charger via FontFace, puis document.fonts.check ; ERREUR si une police manque
  (jamais de police de repli).
- GSAP : timeline maître en pause, ticker.sleep(), lagSmoothing(0), force3D:false.

runtime.js — calques :
- Placement : left/top en CSS ; x/y GSAP = DÉCALAGES ; xPercent/yPercent pour l'ancre.
- Texte :
  - découpe en caractères, mots ou lignes via Intl.Segmenter ;
  - white-space: pre sauf si maxWidth est fourni (sinon retours à la ligne parasites).
- Formes SVG : pathLength=1 pour le tracé progressif.
- Séquence (plaque 3D) : img.src par image, img.decode() attendu.
- Graphique : barres et compteurs avec le même easing, sans dépassement sur les données.
- Propriétés spéciales : draw → strokeDashoffset, blur → filter, reveal → clip-path inset.

runtime.js — effets :
- Papier : feTurbulence CUIT UNE FOIS en bitmap au démarrage (sinon rendu 3 à 10 fois plus lent).
- Grain (correctif déterminisme) :
  - 4 tuiles générées par un PRNG dédié, chacune dans son propre calque ;
  - toutes décodées (await img.decode()) au démarrage ;
  - on bascule seulement leur visibilité et leur position ;
  - JAMAIS de changement de background-image en data-URL à chaque image.
- Vignette et balayage : statiques.
- Bande de tracking : fonction pure du temps.
- Boil : graine de turbulence changée toutes les `step` images.
- Glitch : fenêtres temporelles, séparation RVB ou découpe selon hash01(image, calque, graine).

runtime.js — contrat avec le pilote :
- window.__seek(frame, sub) :
  - quantifie pour frame_step ;
  - règle l'horloge, puis master.seek(t, false) ;
  - attend les hooks ;
  - force les animations CSS sur le temps virtuel ;
  - attend un double requestAnimationFrame.
- window.__layout() : boîtes des textes d'opacité >= 0,9 (contrôle des zones sûres).
- Drapeaux window.__MOGRAPH_READY__ et __MOGRAPH_ERROR__.
VÉRIFIER : voir étape 12.

## Étape 11 — Compilateur (pipeline/scene.py)
FAIRE :
- validation par schéma avec messages actionnables ;
- temps : nombres = secondes locales au plan ; beat: et marker: = temps global converti en local ;
- durées : si durée x fps n'est pas un entier, REFUS avec explication ;
- développement des moves du preset, résolution des alias d'easing ;
- easing linéaire REFUSÉ sauf allowLinear ;
- sync:end : décale le départ pour que la fin de la première étape tombe sur la référence
  (synchro musicale) ;
- fenêtres de glitch calculées depuis on_beats ;
- fondus : chevauchement des plans, nombre d'images entier ;
- une graine par plan ;
- alpha => livrable prores_4444 obligatoire ;
- frame_step > 1 => flou de mouvement désactivé ;
- plaques 3D => tâches Blender entièrement résolues :
  - numéros d'image flottants ;
  - interpolation Blender par segment (CustomEase approximé par BACK EASE_IN_OUT 1.2) ;
  - elastic : amplitude 0, période en images ;
  - couleurs en hexadécimal.
VÉRIFIER :
  une scène valide compile, avec nombre d'images et fondus affichés ;
  une durée de 1,01 s à 25 i/s est refusée ;
  un ease "none" est refusé ;
  une référence beat: sans bpm est refusée.

## Étape 12 — Pilote web (pipeline/web_render.py)
FAIRE :
- Contexte Chromium :
  - arguments de l'étape 7 ;
  - viewport logique, device_scale_factor 1 ou 2, locale fr-FR, fuseau UTC.
- Réseau : route qui sert UNIQUEMENT http://mograph.render depuis la racine du projet ;
  tout le reste est bloqué.
- Chargement :
  - scripts d'initialisation dans l'ordre : graine + plan, puis clock.js ;
  - attente de READY ;
  - toute erreur de page ou de console arrête le rendu.
- Capture : page.screenshot(type="png", scale="device", omit_background=True).
  NE PAS utiliser Page.captureScreenshot brut (mesuré : rend en pixels CSS, donc une « 4K » qui
  n'est qu'un 1080p agrandi ; avec clip.scale, il perd l'alpha).
- Format : normaliser chaque capture en RGBA 8 bits (Chromium omet l'alpha si l'image est
  opaque), puis écrire en PNG via OpenCV, compression 1.
- Flou de mouvement :
  - N sous-images sur un obturateur centré ;
  - moyenne en flottant, en alpha prémultiplié ;
  - sortie PNG 16 bits.
- manifest.json :
  - SHA-256 des pixels de chaque image ;
  - relevé __layout toutes les fps/5 images ;
  - reprise possible.
- verify_determinism : re-rendre des images témoins, isolées et hors ordre, dans un navigateur
  neuf, puis comparer les empreintes.
VÉRIFIER :
  (a) 5 images d'une démo : planche contact regardée à l'œil ;
  (b) temps par image mesuré (référence sur 1 cœur : 0,55 s en 1080p avec effets) ;
  (c) 4K : devicePixelRatio = 2, capture en 3840x2160, et des blocs 2x2 NON uniformes sur les
      bords du texte (sinon c'est un agrandissement) ;
  (d) scène alpha : pourcentage de pixels transparents supérieur à 0, alpha de 0 à 255.

## Étape 13 — Backend Blender (backends/blender_backend.py + pipeline/blender_render.py)
FAIRE (le backend) :
- Initialisation : lire job.json, read_factory_settings(use_empty=True).
- Moteur : Cycles CPU, ou OptiX si GPU NVIDIA (paramètre device dans le preset).
- Échantillonnage : échantillonnage adaptatif, débruitage OIDN.
- Rebonds lumineux lus dans le preset, caustiques coupées.
- use_persistent_data = True.
- Graine fixe + use_animated_seed.
- Flou de mouvement : obturateur lu dans le preset.
- Fond transparent si demandé.
- Résolution : taille de sortie x plate_scale.
- Couleur :
  - vue AgX ;
  - look : essayer "Base Contrast", puis "AgX - Base Contrast" (l'enum est dynamique,
    l'introspection ne voit que NONE) ;
  - ÉCHEC explicite si le look est introuvable.
- Sortie : PNG RGBA 16 bits.
- Monde : couleur unie ou ciel physique.
- Caméra : contrainte TRACK_TO vers un objet vide, profondeur de champ si fstop.
- Lumières : AREA en disque, orientées vers leur cible.
- Objets :
  - cyclorama construit en bmesh avec congé biseauté ;
  - rounded_cube = bevel 6 segments + harden_normals + lissage ;
  - Principled BSDF, avec tolérance sur les noms d'entrées selon la version.
- Animation :
  - clés insérées, puis interpolation posée sur la clé de DÉPART de chaque segment ;
  - F-curves via bpy_extras.anim_utils.action_get_channelbag_for_slot (API « slotted actions »
    de Blender 5).
- Boucle de rendu :
  - image par image ;
  - écriture .tmp puis renommage (écriture atomique) ;
  - images existantes sautées (reprise) ;
  - ligne de journal "MOGRAPH_BLENDER image i/n".
FAIRE (le lanceur) : écrire job.json, lancer le Python 3.11, relayer la progression, vérifier que
la plaque est complète.
VÉRIFIER :
  rendre 3 images isolées ;
  imprimer les valeurs animées (ex. z d'une sphère : 3,9 puis 0,5 puis 0,36 puis 0,5) ;
  imprimer le look réellement appliqué ;
  regarder les images ;
  noter le temps par image pour planifier (sur 1 cœur : ~21 s en 540x540, 24 échantillons,
  avec flou de mouvement).

## Étape 14 — Compositeur (pipeline/compose.py)
FAIRE :
- séquence maître build/<scène>/master/NNNNNN.png, reconstruite de zéro à chaque fois ;
- coupes = liens physiques (hard links), aucune recopie ;
- fondus : mélange en alpha prémultiplié, poids en smoothstep ;
- profondeur UNIQUE sur toute la séquence (16 bits si flou de mouvement, sinon 8 bits) ;
- contrôle final : nombre d'images = total attendu.
VÉRIFIER : compte d'images exact ; une image de fondu contient bien les deux plans.

## Étape 15 — Encodeur (pipeline/encode.py)

Conversion couleur commune (variable $ZS) :
  zscale=rangein=full:range=limited:primariesin=709:primaries=709:transferin=709:transfer=709:matrix=709:dither=error_diffusion

HEVC :
  -vf "$ZS,format=yuv420p10le" -c:v libx265 -preset slow -crf 18 -pix_fmt yuv420p10le -profile:v main10
  -x265-params colorprim=bt709:transfer=bt709:colormatrix=bt709:range=limited:aq-mode=3:no-sao=1:keyint=2*fps:min-keyint=fps
  -tag:v hvc1 -color_primaries bt709 -color_trc bt709 -colorspace bt709 -color_range tv
  -movflags +faststart+write_colr -c:a aac -b:a 320k -ar 48000

ProRes 422 HQ :
  -vf "$ZS,format=yuv422p10le" -c:v prores_ks -profile:v 3 -vendor apl0 -pix_fmt yuv422p10le
  + mêmes tags couleur, -movflags +write_colr -timecode 01:00:00:00 -c:a pcm_s24le -ar 48000

ProRes 4444 :
  -vf "$ZS,format=yuva444p10le" -profile:v 4 -pix_fmt yuva444p10le -alpha_bits 16

Règles communes :
- sortie opaque d'une scène à alpha : préfixer la chaîne vidéo par
  format=rgba64le,premultiply=inplace=1 (aplatissement sur noir) ;
- toujours -frames:v N (nombre exact d'images) ;
- audio : -af "apad=whole_dur=D,atrim=end=D" (calage exact sur la durée vidéo) ;
- écrire out/<scène>/encode_commands.sh avec les commandes exactes utilisées.

VÉRIFIER (niveaux exacts, en 10 bits) :
  blanc pur -> Y = 940, noir pur -> Y = 64, chroma = 512 ;
  alpha dégradé -> 0 à 1023 ;
  dégradé 16 bits de 256 px -> 256 valeurs Y distinctes (pas de banding).

## Étape 16 — Audio (pipeline/audio.py)
FAIRE :
- piste de test générée par aevalsrc (grosse caisse sur chaque temps, accent sur le 1er temps de
  la mesure, charleston sur les contretemps) ;
- sortie WAV 48 kHz, 24 bits, avec -bitexact ;
- expression ENTRE APOSTROPHES (sinon les virgules sont lues comme séparateurs de filtres).
VÉRIFIER :
  2 générations donnent le même md5sum ;
  détection d'attaques : 0,5 / 1,0 / 1,5 s... à 120 BPM ;
  durée exacte.

## Étape 17 — Contrôle qualité (pipeline/qc.py)

Contrôles ffprobe (avec -count_frames) :
- codec et profil : Main 10 / HQ / 4444 ;
- format de pixel : yuv420p10le / yuv422p10le / yuva444p10le, ou yuva444p12le au décodage du 4444 ;
- résolution ;
- r_frame_rate et avg_frame_rate = fps/1 ;
- nombre d'images COMPTÉ ;
- durée à ± ½ image ;
- tag hvc1 ;
- métadonnées couleur : bt709 x 3, plage tv ;
- piste timecode (tmcd).

Contrôles de contenu :
- alpha réel (alphaextract sur l'image médiane) ;
- noir non voulu : blackdetect avec pix_th=0.03 (et non 0.06, qui pénalise les fonds sombres voulus) ;
- audio : codec, 48 kHz, durée (tolérance 1 image + 25 ms d'amorçage AAC) ;
- loudness : ebur128, cible −14 LUFS pour les réseaux ou −23 LUFS en broadcast,
  true peak ≤ −1 dBTP ;
- zones sûres d'après les relevés __layout : bloquant au-delà de 0,5 s continu hors zone ;
- résultat du test de déterminisme.

Sorties : qc_report.json + qc_report.md ; code de sortie non nul si échec.
VÉRIFIER : un fichier volontairement faux (ex. -r 24 au lieu de 25) doit donner ÉCHEC.

## Étape 18 — CLI (mograph.py)
FAIRE : commandes validate, render, determinism, encode, qc, all, synth-beat.
all enchaîne : plaques 3D -> images web -> déterminisme -> séquence maître -> encodage -> QC.
VÉRIFIER : python mograph.py validate scenes/<démo>.json affiche format, images et fondus.

## Étape 19 — PORTE DE DÉTERMINISME (bloquante)
FAIRE : python mograph.py determinism sur chaque démo.
CRITÈRE : 100 % des images témoins identiques au bit près. Sinon, NE PAS livrer. Diagnostiquer :
  1. Désactiver les effets un par un (grain, papier, vignette, glitch, boil) et relancer le test.
  2. Remplacer tout tween "to" seul par un "fromTo" explicite (GSAP enregistre la valeur de
     départ au premier rendu, qui dépend de l'ordre des positionnements).
  3. Vérifier que toutes les images (tuiles de grain, plaques 3D) sont décodées avant la capture.
  4. Comparer les images divergentes pixel à pixel pour localiser la zone fautive.
NOTE : le déterminisme est garanti sur une même machine et une même installation. Entre deux
systèmes différents, la rastérisation des polices peut varier : refaire le test sur la machine
de rendu finale.

## Étape 20 — Démos et livraison
FAIRE (python mograph.py all <scène>, chaque rendu lancé dans tmux) :
1. demo_flat_riso : 16:9, 1080p, 25 i/s, 2 plans avec fondu de 0,4 s, graphique en barres ;
   165 images ; HEVC + ProRes 422 HQ.
2. demo_vhs_kinetic : 9:16, 30 i/s, piste 120 BPM ; un mot par temps en sync:end, glitch sur
   les temps ; 150 images ; texte centré sur la ZONE SÛRE (et non sur le cadre).
3. demo_clay_3d : 1:1, 24 i/s ; plaque Blender (chute, écrasement, rebond, travelling) + titre
   Fraunces composité ; 72 images ; plate_scale 0,5 sur petite machine, 1 en production.
4. test_lowerthird_4k_alpha : 16:9, 4K, 25 i/s, fond transparent, flou de mouvement
   4 échantillons ; ProRes 4444 + HEVC aplati sur noir ; 50 images.
VÉRIFIER :
  chaque qc_report.md dit CONFORME ;
  lecture image par image aux coupes, fondus et impacts ;
  import dans Resolve : scopes (niveaux 64-940), interprétation 709, alpha incrusté sur fond
  clair ET sur fond sombre ;
  checklist humaine QC_CHECKLIST.md cochée.

## Étape 21 — Exploitation au quotidien
Pour chaque nouvelle vidéo :
1. brief -> storyboard (intention par plan) ;
2. choisir un preset (ou en dériver un) ;
3. écrire la scène JSON, puis validate ;
4. préversion : plate_scale 0,5, peu d'échantillons, quelques images témoins ;
5. rendu final ;
6. QC automatique + checklist humaine ;
7. finition éventuelle dans Resolve ;
8. livraison.

Archivage : enregistrer dans out/<scène>/versions.txt les sorties de ffmpeg -version, pip freeze,
npm ls, et la version de bpy, puis committer scène + preset dans git.

# DÉFINITION DE « 100 % FONCTIONNEL »
- Les 4 scènes passent `all` avec un code de sortie 0, QC CONFORME, déterminisme 100 %.
- ffprobe confirme codec, profil, format de pixel, fps exact, nombre d'images exact, hvc1,
  bt709/tv ; l'alpha est présent en 4444.
- Un rendu interrompu reprend sans perte ; une scène erronée est refusée avec un message clair.
- README (installation, commandes, format de scène), QC_CHECKLIST.md et encode_commands.sh
  sont présents.
- Limites connues documentées :
  - source web en 8 bits (16 bits seulement avec flou de mouvement) ;
  - pas de P3 ni de HDR ;
  - EEVEE et Grease Pencil exigent un GPU ;
  - zones sûres 9:16 approximatives ;
  - pas de détection automatique des temps (évolution possible : librosa) ;
  - rendu logiciel lent sans GPU.

# PIÈGES DÉJÀ RENCONTRÉS (à éviter d'emblée)
- Processus lancés avec & tués à la fin de la session shell : utiliser setsid ou tmux.
- `pkill -f motif` tue aussi le shell qui le lance : écrire `pkill -f "[m]otif"`.
- bmesh introuvable : importer bpy d'abord.
- Look AgX ignoré : l'enum est dynamique, il faut essayer les noms complets.
- 4K floue ou alpha perdu : capture Playwright scale="device", jamais CDP brut.
- Changement de format PNG en cours de séquence : normaliser en RGBA.
- Effet SVG re-calculé à chaque image : le cuire en bitmap s'il est statique.
- Impact invisible sur le temps : sync:"end".
- Mots coupés en plusieurs lignes : white-space: pre sans maxWidth.
- Virgules des expressions FFmpeg : les protéger par des apostrophes.
