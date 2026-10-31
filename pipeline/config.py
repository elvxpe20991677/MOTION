"""Configuration centrale : tout ce qui influence les pixels ou le fichier livré est défini ici.

Règle : aucun autre module ne code en dur un chemin, une taille de canevas, une zone sûre,
un argument Chromium ou une correspondance d'easing. Changer une valeur ici change la sortie ;
elle doit donc être versionnée avec les scènes.
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Chemins
# ---------------------------------------------------------------------------

# Racine du projet déduite de l'emplacement de ce fichier : le pipeline marche quel que soit le cwd.
ROOT: Path = Path(__file__).resolve().parent.parent

SCHEMA_DIR: Path = ROOT / "schema"
PRESETS_DIR: Path = ROOT / "presets"
# Scènes « par id » (new, brief, résolution d'un id) : déplaçable pour isoler les tests (phase 6).
SCENES_DIR: Path = Path(os.environ.get("MOGRAPH_SCENES_DIR", ROOT / "scenes")).resolve()
TEMPLATES_DIR: Path = ROOT / "templates"
RUNTIME_DIR: Path = ROOT / "runtime"
NODE_MODULES: Path = ROOT / "node_modules"
ASSETS_DIR: Path = ROOT / "assets"
AUDIO_DIR: Path = ASSETS_DIR / "audio"
# Assets produits par une IA (gelés, hashés et verrouillés : phase 3). Une scène qui en référence un
# doit déclarer provenance.ai_disclosure (contrôle QC scene.ai_disclosure). Déplaçable par variable
# d'environnement (tests isolés), mais toujours SOUS assets/ : seul dossier servi au navigateur.
AI_ASSETS_DIR: Path = Path(os.environ.get("MOGRAPH_AI_ASSETS_DIR", ASSETS_DIR / "ai")).resolve()
# Verrou des assets IA gelés : empreinte SHA-256 + provenance de chaque fichier de AI_ASSETS_DIR.
# Versionné avec les scènes ; le rendu ÉCHOUE si un asset IA n'y figure pas ou si son empreinte diffère.
ASSETS_LOCK: Path = Path(os.environ.get("MOGRAPH_ASSETS_LOCK", ROOT / "assets.lock.json")).resolve()
# Voix off ElevenLabs (phase 4, pipeline/voice.py, CONTRACT §11.5). Clé et voix par défaut lues dans
# .env (jamais versionné) ; API redirigeable (tests : faux serveur local, aucun caractère consommé).
ENV_FILE: Path = Path(os.environ.get("MOGRAPH_ENV_FILE", ROOT / ".env")).resolve()
ELEVENLABS_API_BASE: str = os.environ.get("MOGRAPH_ELEVENLABS_API", "https://api.elevenlabs.io")
VOICE_DEFAULT_MODEL: str = "eleven_multilingual_v2"     # meilleur français ; décompte réel lu dans l'historique
# Limite de caractères d'UNE requête par modèle (elevenlabs.io/docs/models, relevé le 02/10/2026) ;
# modèle absent de la table : pas de contrôle local, l'API répond 400 au besoin.
VOICE_MAX_CHARS_PER_REQUEST: dict[str, int] = {
    "eleven_v4": 10_000, "eleven_v3": 5_000, "eleven_flash_v2_5": 40_000, "eleven_flash_v2": 30_000,
    "eleven_multilingual_v2": 10_000, "eleven_multilingual_v1": 10_000}
# MP3 44,1 kHz 128 kb/s : seul format de qualité ouvert à toutes les offres (PCM/WAV 44,1 kHz : offre Pro).
VOICE_OUTPUT_FORMAT: str = "mp3_44100_128"
VOICE_HTTP_TIMEOUT_S: float = 120.0
# Garde-fou de quota : plafond de caractères par commande, confirmation au-delà d'un seuil. Une valeur
# d'environnement invalide ne casse aucune autre commande : « mograph voice » la refuse (VOICE_ENV_ERRORS).
VOICE_ENV_ERRORS: list[str] = []


def _env_count(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        value = -1
    if value < 0:
        VOICE_ENV_ERRORS.append(f"{name} doit être un entier ≥ 0 (valeur actuelle « {raw} ») : corrigez-la ou "
                                "supprimez-la")
        return default
    return value


VOICE_BUDGET_CHARS: int = _env_count("MOGRAPH_VOICE_BUDGET", 1000)
VOICE_CONFIRM_ABOVE: int = _env_count("MOGRAPH_VOICE_CONFIRM_ABOVE", 200)
# Journal de consommation (une ligne JSON par commande « mograph voice », versionné : suivi du quota).
VOICE_USAGE_LOG: Path = Path(os.environ.get("MOGRAPH_VOICE_USAGE_LOG", ROOT / "voice_usage.jsonl")).resolve()
# Sous-titres générés depuis l'alignement : coupe en fin de phrase, sur un silence, ou au-delà de ces limites.
VOICE_SUB_MAX_CHARS: int = 60
VOICE_SUB_MAX_S: float = 5.0
VOICE_SUB_GAP_S: float = 0.5
VOICE_SUB_HOLD_S: float = 0.25
# Ducking : la musique baisse pendant la parole (intervalles de mots fusionnés si le silence < MERGE).
VOICE_DUCK_DB: float = -12.0
VOICE_DUCK_ATTACK_S: float = 0.08
VOICE_DUCK_RELEASE_S: float = 0.35
VOICE_DUCK_MERGE_S: float = 0.4
# Musique (phase 5, CONTRACT §11.6) : dérive maximale tolérée entre la grille à tempo FIXE de
# « mograph beats » et les attaques du morceau (40 ms = une image à 25 i/s) ; au-delà, avertissement :
# le contrat ne gère qu'un tempo fixe (beat:, beats:, on_beats).
TEMPO_DRIFT_WARN_MS: float = 40.0

# Provenance C2PA (phase 7, CONTRACT §11.8). Certificat et clé privée LOCAUX (jamais versionnés) :
# MOGRAPH_C2PA_DIR, défaut .c2pa/ à la racine (ignoré par git). c2patool (vérification indépendante) :
# MOGRAPH_C2PATOOL, sinon .tools/c2patool-<version>/c2patool/c2patool(.exe), sinon le PATH.
C2PA_DIR: Path = Path(os.environ.get("MOGRAPH_C2PA_DIR", ROOT / ".c2pa")).resolve()
C2PATOOL_VERSION: str = "0.28.4"
C2PA_IPTC_BASE: str = "http://cv.iptc.org/newscodes/digitalsourcetype/"
# Horodatage RFC 3161 facultatif (URL d'une autorité d'horodatage) : appel RÉSEAU à chaque signature ;
# sans lui, un manifeste devient « Invalid » à l'expiration du certificat local (825 jours).
C2PA_TSA_URL: str | None = os.environ.get("MOGRAPH_C2PA_TSA") or None


def c2patool_path() -> str | None:
    env = os.environ.get("MOGRAPH_C2PATOOL")
    if env:
        return env
    local = ROOT / ".tools" / f"c2patool-{C2PATOOL_VERSION}" / "c2patool" / ("c2patool.exe" if IS_WINDOWS else "c2patool")
    return str(local) if local.is_file() else shutil.which("c2patool")

# Couche agent (phase 6, CONTRACT §11.7) : appels à Claude par « claude -p » (abonnement, jamais l'API
# facturée). « mograph brief » : AU PLUS 3 appels (feuille de route), plafond non modifiable.
BRIEF_MAX_ITERATIONS: int = 3
CLAUDE_ENV_ERRORS: list[str] = []


def _env_pos_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = float(raw)
        if value > 0 and value != float("inf"):
            return value
    except ValueError:
        pass
    CLAUDE_ENV_ERRORS.append(f"{name} doit être un nombre > 0 (valeur actuelle « {raw} ») : corrigez-la ou retirez-la.")
    return default


# Modèle demandé à la CLI (alias « sonnet » : économise le quota de l'abonnement ; « opus » possible).
CLAUDE_MODEL: str = os.environ.get("MOGRAPH_CLAUDE_MODEL", "sonnet").strip() or "sonnet"
CLAUDE_TIMEOUT_S: float = _env_pos_float("MOGRAPH_CLAUDE_TIMEOUT", 600.0)
# Plafond par appel passé à --max-budget-usd (coût « liste » indicatif ; un abonnement n'est pas facturé à
# l'usage) : coupe un appel qui s'emballerait.
CLAUDE_MAX_BUDGET_USD: float = _env_pos_float("MOGRAPH_CLAUDE_MAX_BUDGET_USD", 1.0)

# Nature d'un asset IA déduite de son extension (verrou, CLI « assets add »).
AI_ASSET_KINDS: dict[str, tuple[str, ...]] = {
    "video": (".mp4", ".mov", ".m4v", ".webm", ".mkv", ".avi", ".mxf"),
    "image": (".png", ".jpg", ".jpeg", ".webp", ".svg"),
    "audio": (".wav", ".mp3", ".m4a", ".flac", ".ogg", ".aac"),
    "subtitles": (".srt", ".vtt"),
    "mesh": (".glb", ".gltf", ".obj", ".fbx", ".stl", ".ply", ".usd", ".usdz"),
    "text": (".txt", ".md", ".json"),
}
BACKENDS_DIR: Path = ROOT / "backends"

# build/ et out/ peuvent être déplacés sur un disque plus grand sans toucher au code
# (les séquences PNG 4K 16 bits pèsent 20 à 40 Mo par image).
BUILD_DIR: Path = Path(os.environ.get("MOGRAPH_BUILD_DIR", ROOT / "build")).resolve()
OUT_DIR: Path = Path(os.environ.get("MOGRAPH_OUT_DIR", ROOT / "out")).resolve()

IS_WINDOWS: bool = sys.platform == "win32"

# L'interpréteur de l'orchestrateur est celui qui exécute ce code (venv Python 3.12).
ORCHESTRATOR_PYTHON: Path = Path(sys.executable)

# bpy 5.0.1 n'existe que pour CPython 3.11 : il vit dans un venv séparé créé par uv.
BLENDER_VENV: Path = ROOT / ".venv-blender"
BLENDER_PYTHON: Path = BLENDER_VENV / ("Scripts/python.exe" if IS_WINDOWS else "bin/python")
BLENDER_BACKEND: Path = BACKENDS_DIR / "blender_backend.py"

# FFmpeg doit être compilé avec libx265, prores_ks et libzimg (zscale) ; on le cherche dans le PATH,
# une variable d'environnement permet d'imposer un build statique précis.
FFMPEG: str = os.environ.get("MOGRAPH_FFMPEG") or shutil.which("ffmpeg") or "ffmpeg"
FFPROBE: str = os.environ.get("MOGRAPH_FFPROBE") or shutil.which("ffprobe") or "ffprobe"


def scene_build_dir(scene_id: str) -> Path:
    """Dossier de travail d'une scène (images, plaques, manifestes)."""
    return BUILD_DIR / scene_id


def scene_out_dir(scene_id: str) -> Path:
    """Dossier des livrables d'une scène (vidéos, rapports QC, commandes d'encodage)."""
    return OUT_DIR / scene_id


def shot_frames_dir(scene_id: str, shot_id: str) -> Path:
    return scene_build_dir(scene_id) / "shots" / shot_id


def plate_dir(scene_id: str, shot_id: str, plate_id: str) -> Path:
    # Double soulignement : les id de plan et de plaque n'en contiennent jamais (voir schéma).
    return scene_build_dir(scene_id) / "plates" / f"{shot_id}__{plate_id}"


def media_dir(scene_id: str, shot_id: str, layer_id: str) -> Path:
    """Images extraites d'une vidéo source (calque « video ») : même nommage que les plaques."""
    return scene_build_dir(scene_id) / "media" / f"{shot_id}__{layer_id}"


def master_dir(scene_id: str) -> Path:
    return scene_build_dir(scene_id) / "master"


# Nommage des images : 6 chiffres, index local au plan à partir de 0 (000000.png).
FRAME_PATTERN: str = "{:06d}.png"
FRAME_PATTERN_FFMPEG: str = "%06d.png"

# ---------------------------------------------------------------------------
# Réseau du runtime navigateur
# ---------------------------------------------------------------------------

# Origine fictive : les polices et images sont same-origin (canvas non « tainted », toBlob autorisé)
# et aucune requête ne peut sortir vers Internet.
ORIGIN: str = "http://mograph.render"
PLAYER_URL: str = f"{ORIGIN}/runtime/player.html"

# Seuls ces préfixes d'URL sont servis ; tout le reste est bloqué par la route Playwright.
SERVED_PREFIXES: dict[str, Path] = {
    "/runtime/": RUNTIME_DIR,
    "/node_modules/": NODE_MODULES,
    "/assets/": ASSETS_DIR,
    "/build/": BUILD_DIR,
}

MIME_TYPES: dict[str, str] = {
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".woff2": "font/woff2",
    ".woff": "font/woff",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".svg": "image/svg+xml",
    # Aperçu en direct (mograph preview) : pistes audio lues par le navigateur.
    ".wav": "audio/wav",
    ".mp3": "audio/mpeg",
    ".m4a": "audio/mp4",
    ".ogg": "audio/ogg",
}

# Vidéos acceptées par le calque « video » (extraites en PNG par FFmpeg, jamais lues par le navigateur).
VIDEO_EXTENSIONS: tuple[str, ...] = (".mp4", ".mov", ".m4v", ".webm", ".mkv", ".avi", ".mxf")
# Sous-titres acceptés par le calque « subtitles ».
SUBTITLE_EXTENSIONS: tuple[str, ...] = (".srt", ".vtt")

# ---------------------------------------------------------------------------
# Canevas, échelle, zones sûres
# ---------------------------------------------------------------------------

# Canevas LOGIQUES (pixels CSS) : la mise en page est identique en 1080p et en 4K.
LOGICAL_CANVAS: dict[str, tuple[int, int]] = {
    "16:9": (1920, 1080),
    "9:16": (1080, 1920),
    "1:1": (1080, 1080),
}

# La 4K est la même page rastérisée avec devicePixelRatio = 2 (vrai 2x, pas un agrandissement).
RESOLUTION_SCALE: dict[str, int] = {
    "1080p": 1,
    "4k": 2,
}

ASPECTS = tuple(LOGICAL_CANVAS)
RESOLUTIONS = tuple(RESOLUTION_SCALE)
FPS_ALLOWED = (24, 25, 30, 50, 60)


def output_size(aspect: str, resolution: str) -> tuple[int, int]:
    """Taille en pixels du livrable (canevas logique x facteur d'échelle)."""
    w, h = LOGICAL_CANVAS[aspect]
    s = RESOLUTION_SCALE[resolution]
    return w * s, h * s


# Marges en FRACTION du canevas, ordre (haut, droite, bas, gauche).
# 16:9 et 1:1 : EBU R95 (graphiques 5 %, action 3,5 %).
# 9:16 : marges empiriques des interfaces TikTok / Reels / Shorts (bas chargé : légende + boutons).
SAFE_ZONES: dict[str, dict[str, tuple[float, float, float, float]]] = {
    "16:9": {"title": (0.05, 0.05, 0.05, 0.05), "action": (0.035, 0.035, 0.035, 0.035)},
    "1:1": {"title": (0.05, 0.05, 0.05, 0.05), "action": (0.035, 0.035, 0.035, 0.035)},
    "9:16": {"title": (0.12, 0.12, 0.22, 0.06), "action": (0.08, 0.08, 0.15, 0.04)},
}


def safe_rect(aspect: str, zone: str) -> dict[str, float]:
    """Rectangle sûr en pixels LOGIQUES : {left, top, right, bottom, width, height, cx, cy}."""
    w, h = LOGICAL_CANVAS[aspect]
    top, right, bottom, left = SAFE_ZONES[aspect][zone]
    x0, y0, x1, y1 = left * w, top * h, w - right * w, h - bottom * h
    return {
        "left": x0, "top": y0, "right": x1, "bottom": y1,
        "width": x1 - x0, "height": y1 - y0,
        "cx": (x0 + x1) / 2, "cy": (y0 + y1) / 2,
    }


# Tolérance (px logiques) d'une boîte de texte qui touche le bord de la zone sûre (anticrénelage).
SAFE_ZONE_EPSILON_PX: float = 1.0

# ---------------------------------------------------------------------------
# Chromium
# ---------------------------------------------------------------------------

# Chaque argument supprime une source de non-déterminisme ou de dépendance à l'environnement.
CHROMIUM_ARGS: list[str] = [
    "--use-angle=swiftshader",                    # GL logiciel : mêmes pixels sans dépendre du pilote GPU
    "--enable-unsafe-swiftshader",                # autorise SwiftShader pour WebGL/canvas malgré l'avertissement
    "--force-color-profile=srgb",                 # pas de gestion couleur liée au moniteur de la machine
    "--font-render-hinting=none",                 # pas de hinting (effet sous Linux ; inoffensif ailleurs)
    "--disable-lcd-text",                         # anticrénelage en niveaux de gris, pas de sous-pixel RVB
    "--disable-checker-imaging",                  # décodage d'image synchrone : jamais d'image à moitié peinte
    "--run-all-compositor-stages-before-draw",    # la capture attend la fin de toute la composition
    "--disable-background-timer-throttling",      # pas de ralentissement des minuteries en headless
    "--disable-renderer-backgrounding",           # le moteur de rendu garde sa priorité
    "--disable-backgrounding-occluded-windows",   # une fenêtre masquée continue de peindre
    "--hide-scrollbars",                          # aucune barre de défilement dans la capture
    "--mute-audio",                               # aucun périphérique audio sollicité
    # Ajout mesuré (hors liste SPEC) : la rastérisation GPU émulée réutilisait des tuiles d'une image à
    # l'autre (texte ±2 niveaux selon l'ordre des seeks) ; la rastérisation CPU est exacte et 17 % plus rapide.
    "--disable-gpu-rasterization",
]

BROWSER_LOCALE: str = "fr-FR"      # Intl.Segmenter et formats de nombres figés
BROWSER_TIMEZONE: str = "UTC"      # Date virtuelle interprétée sans heure d'été locale
PAGE_READY_TIMEOUT_MS: int = 60_000
FRAME_TIMEOUT_MS: int = 30_000

# Époque de l'horloge virtuelle (Date.now() = époque + temps de la timeline).
VIRTUAL_EPOCH_ISO: str = "2025-01-01T00:00:00Z"
VIRTUAL_EPOCH_MS: int = 1735689600000

# ---------------------------------------------------------------------------
# Images, profondeur, flou de mouvement
# ---------------------------------------------------------------------------

# Compression PNG 1 : encodage rapide, fichier ~20 % plus gros que 9 mais sans perte.
PNG_COMPRESSION: int = 1

# Relevé des boîtes de texte toutes les fps/5 images (5 relevés par seconde).
LAYOUT_SAMPLES_PER_SECOND: int = 5


def layout_every(fps: int) -> int:
    return max(1, round(fps / LAYOUT_SAMPLES_PER_SECOND))


# Nombre d'images témoins par défaut pour le test de déterminisme.
DETERMINISM_FRAMES_DEFAULT: int = 8

# Navigateurs Chromium simultanés au plus (rendu web). 4 par défaut (~400 Mo et 2 à 4 threads
# SwiftShader chacun) ; MOGRAPH_MAX_WORKERS permet de l'augmenter sur une machine de 16 threads ou
# plus après mesure (les empreintes ne dépendent pas du nombre de navigateurs : vérifié étape 12).
WEB_MAX_WORKERS: int = max(1, int(os.environ.get("MOGRAPH_MAX_WORKERS", "4")))

# Seuil au-dessus duquel un rendu est considéré « long » et doit être détaché.
LONG_RENDER_FRAMES: int = 300

# ---------------------------------------------------------------------------
# Easing : GSAP -> Blender
# ---------------------------------------------------------------------------

# Familles GSAP -> interpolation Blender (même courbe mathématique, noms différents).
GSAP_TO_BLENDER_INTERP: dict[str, str] = {
    "sine": "SINE",
    "power1": "QUAD",
    "quad": "QUAD",
    "power2": "CUBIC",
    "cubic": "CUBIC",
    "power3": "QUART",
    "quart": "QUART",
    "power4": "QUINT",
    "quint": "QUINT",
    "strong": "QUINT",
    "expo": "EXPO",
    "circ": "CIRC",
    "back": "BACK",
    "elastic": "ELASTIC",
    "bounce": "BOUNCE",
}

GSAP_TO_BLENDER_EASING: dict[str, str] = {
    "in": "EASE_IN",
    "out": "EASE_OUT",
    "inOut": "EASE_IN_OUT",
}

# GSAP : sans suffixe, la direction par défaut est « out ».
GSAP_DEFAULT_DIRECTION: str = "out"

# Un CustomEase n'a pas d'équivalent Blender : approximation documentée par BACK EASE_IN_OUT 1.2.
CUSTOM_EASE_BLENDER: dict[str, object] = {"interpolation": "BACK", "easing": "EASE_IN_OUT", "back": 1.2}

# Paramètres par défaut de GSAP, repris pour Blender.
GSAP_BACK_DEFAULT: float = 1.70158
GSAP_ELASTIC_PERIOD_DEFAULT: float = 0.3

# Easings linéaires : refusés sauf allowLinear (mouvement mécanique, rarement voulu en motion design).
LINEAR_EASES: frozenset[str] = frozenset({"none", "linear", "power0", "power0.in", "power0.out", "power0.inOut"})

# ---------------------------------------------------------------------------
# Encodage et QC
# ---------------------------------------------------------------------------

# Conversion couleur commune (RGB plein -> YUV limité Rec.709, diffusion d'erreur contre le banding).
ZSCALE: str = (
    "zscale=rangein=full:range=limited:primariesin=709:primaries=709:"
    "transferin=709:transfer=709:matrix=709:dither=error_diffusion"
)

PROFILES: dict[str, dict[str, str]] = {
    "hevc_main10": {"ext": ".mp4", "codec": "hevc", "profile": "Main 10", "pix_fmt": "yuv420p10le"},
    "prores_422hq": {"ext": ".mov", "codec": "prores", "profile": "HQ", "pix_fmt": "yuv422p10le"},
    "prores_4444": {"ext": ".mov", "codec": "prores", "profile": "4444", "pix_fmt": "yuva444p10le"},
    # Phase 7 : AV1 10 bits par NVENC (GPU NVIDIA Ada ou plus récent ; RTX 4060 : un seul moteur NVENC).
    # Le flux NVENC n'entre pas dans le contrat de déterminisme (porte : images du maître).
    "av1_nvenc": {"ext": ".mp4", "codec": "av1", "profile": "Main", "pix_fmt": "yuv420p10le"},
}
AV1_CQ_DEFAULT: int = 30

HEVC_CRF_DEFAULT: int = 18
TIMECODE_START: str = "01:00:00:00"
AUDIO_RATE: int = 48_000

# QC
BLACKDETECT_PIX_TH: float = 0.03          # 0.06 pénalise les fonds sombres voulus (vhs, ardoise)
# Plage BRUTE de luminance (min/max signalstats, codes 10 bits) utilisée par le QC jusqu'à la phase 1 :
# conservée comme indicateur informatif (nombre d'images qui la dépassent), sans effet sur le statut.
LEVELS_R103_Y: tuple[float, float] = (55.0, 966.0)
# EBU R103 v3.0 (mai 2020, tech.ebu.ch/publications/r103), Table 1 en 10 bits : plage « préférée »
# de Y ET de chaque composante R'G'B' ; mesure après le filtre passe-bas recommandé (H 1,2,3,4,3,2,1
# /16, V 1,2,1 /4) ; un « hors gamut » n'est signalé que si l'erreur dépasse 1 % de l'image (les
# dépassements bénins plus petits restent des erreurs de gamut, mais ne sont pas signalés).
LEVELS_R103_PREFERRED_10BIT: tuple[float, float] = (20.0, 984.0)
# R103 v3.0, Annexe 1 (Introduction) : un programme pré-produit et étalonné (cas de mograph) doit
# suivre DE PRÈS la plage nominale 64..940. Tolérance ±1 % de l'excursion nominale (8,76 codes) :
# absorbe le bruit de quantification des codecs (Éclosion Urbaine : moins de 4,4 codes sur plus de
# 99 % de chaque image) et détecte un vrai défaut de plage (fond en super-blanc ou en sous-noir).
LEVELS_NOMINAL_10BIT: tuple[float, float] = (64.0, 940.0)
LEVELS_NOMINAL_TOLERANCE: float = 0.01
LEVELS_R103_FILTER_H: tuple[int, ...] = (1, 2, 3, 4, 3, 2, 1)
LEVELS_R103_FILTER_V: tuple[int, ...] = (1, 2, 1)
LEVELS_R103_AREA_MAX: float = 0.01
# Codes réservés au transport en 10 bits (plage totale R103 : 4..1019) : avertissement informatif
# (jamais bloquant : une mire plein écran de test les atteint volontairement).
LEVELS_RESERVED_10BIT: tuple[float, float] = (4.0, 1019.0)
LOUDNESS_TOLERANCE_LU: float = 1.0
TRUE_PEAK_MAX_DBTP: float = -1.0
AAC_PRIMING_S: float = 0.025              # amorçage AAC toléré en plus d'une image sur la durée audio
SAFE_ZONE_TOLERANCE_S_DEFAULT: float = 0.5
