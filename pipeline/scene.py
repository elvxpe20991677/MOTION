"""Compilateur de scène mograph (étape 11) : validation + résolution complète.

Entrée  : une scène JSON (schema/scene.schema.json) et son preset (presets/<id>.json).
Sortie  : la scène compilée (contrat « mograph-scene/1 », docs/CONTRACT.md §4) où TOUT est résolu :
          temps en secondes locales, couleurs en hexadécimal, positions en px logiques, easings en
          chaînes GSAP, tweens fromTo complets et continus, plans compilés
          (schema/internal/compiled_shot.schema.json) et tâches Blender
          (schema/internal/blender_job.schema.json).

Une scène fautive est REFUSÉE par une SceneError qui regroupe TOUTES les erreurs trouvées, chacune
localisée par un chemin JSON lisible (ex. « shots[0].layers[2].anim[1].ease (plan « intro »,
calque « titre ») ») et assortie d'une correction suggérée.

Usage direct (diagnostic) : .venv/Scripts/python.exe -m pipeline.scene scenes/<scène>.json
"""

from __future__ import annotations

import copy
import difflib
import hashlib
import json
import math
import os
import re
import sys
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import jsonschema
import referencing
from referencing.jsonschema import DRAFT202012

from pipeline import config

__all__ = [
    "SceneError",
    "load_preset",
    "validate_scene",
    "compile_scene",
    "canonical_json",
    "sha256_json",
    "summary",
    "write_compiled",
]

# ---------------------------------------------------------------------------------------------
# Constantes du contrat (docs/CONTRACT.md §3) — valeurs par défaut sémantiques, pas des réglages
# de rendu : elles restent ici car elles définissent le langage de scène lui-même.
# ---------------------------------------------------------------------------------------------

CONTRACT_SCENE = "mograph-scene/1"
CONTRACT_SHOT = "mograph-shot/1"
CONTRACT_JOB = "mograph-blender-job/1"

# Les 6 alias d'easing d'un preset (CONTRACT §3.6.5).
EASE_ALIASES = ("default", "enter", "exit", "emphasis", "anticipate", "settle")

# Tolérance « durée × fps entier » imposée par le contrat (§3.1).
_INT_TOLERANCE = 1e-6
# Tolérance de comparaison temporelle : bien en dessous d'une image à 60 i/s, au-dessus du bruit flottant.
_TIME_EPS = 1e-9
# Arrondi des temps calculés : supprime le bruit binaire (1.4000000000000001) sans perdre de précision utile,
# et rend le JSON compilé stable d'une machine à l'autre.
_ROUND_DIGITS = 9

_TEXT_SIZE_DEFAULT = {"display": 96, "body": 40}               # §3.5
_MOTION_BLUR_DEFAULT = {"enabled": False, "samples": 8, "shutter": 0.5}  # §3.9
_TEXTURE_NAMES = ("grain", "paper", "vignette", "scanlines", "tracking")
_TEXTURE_DEFAULTS = {                                           # §3.8
    "grain": {"size": 1, "blend": "overlay", "monochrome": True},
    "paper": {"blend": "multiply", "frequency": 0.8, "octaves": 4},
    "vignette": {"softness": 0.6, "color": "#000000"},
    "scanlines": {"spacing": 3, "color": "#000000"},
    "tracking": {"height": 0.06, "period": 2.5, "shift": 18},
}
# fx.<texture> = true alors que le preset n'a pas cette texture : intensité modérée, visible sans
# dominer (le contrat ne fixe pas de valeur ; ces amounts restent sous les maxima usuels des presets).
_TEXTURE_ON_AMOUNT = {"grain": 0.08, "paper": 0.08, "vignette": 0.35, "scanlines": 0.25, "tracking": 0.5}
_BOIL_DEFAULT = {"amount": 2, "freq": 0.03, "step": 2}          # §3.8
_GLITCH_LENGTH_DEFAULT = 0.1                                    # §3.8
_GLITCH_AMOUNT_DEFAULT = 0.5                                    # défaut du schéma de scène
_MATERIAL_DEFAULTS = {                                          # §3.10
    "roughness": 0.5, "metallic": 0.0, "specular": 0.5, "sheen": 0.0, "sheen_roughness": 0.5,
    "coat": 0.0, "coat_roughness": 0.03, "subsurface": 0.0, "transmission": 0.0, "ior": 1.45,
    "anisotropic": 0.0, "emission": "#000000", "emission_strength": 0.0,
}
_SKY_DEFAULTS = {                                               # §3.10
    "sun_elevation": 35.0, "sun_rotation": 0.0, "altitude": 0.0, "air": 1.0, "dust": 1.0,
    "ozone": 1.0, "sun_size": 0.545, "sun_intensity": 1.0,
}
_LIGHT_DEFAULTS = {"shape": "DISK", "size": 1.0, "energy": 500.0, "color": "#FFFFFF",
                   "target": [0.0, 0.0, 0.0], "spot_size": 45.0, "angle": 0.526}   # §3.10
# Un soleil n'a pas de position utile (seule sa direction compte) : position neutre au-dessus du plateau.
_SUN_LOCATION_DEFAULT = [0.0, 0.0, 10.0]
_BOUNCES_DEFAULT = {"total": 8, "diffuse": 4, "glossy": 4, "transmission": 4, "transparent": 8, "volume": 0}
_RENDER_DEFAULTS = {"adaptive_threshold": 0.01, "denoise": True, "view_transform": "AgX", "look": "None",
                    "exposure": 0.0, "caustics": False, "motion_blur_shutter": 0.5}
_SAMPLES_DEFAULT = 64
_LENS_DEFAULT = 50.0
_SENSOR_WIDTH_MM = 36.0     # plein format 24x36 : la focale donnée a son sens photographique usuel
# Primitives : cote secondaire par défaut en fraction de `size` (le schéma de tâche exige minor > 0 partout).
_MINOR_FACTOR = {"torus": 0.125, "cyclorama": 0.5}
_MINOR_FACTOR_OTHER = 0.25
_BEVEL_FACTOR = {"rounded_cube": 0.08, "cyclorama": 0.15}
_ANCHORS = {
    "center": [0.5, 0.5], "top-left": [0.0, 0.0], "top": [0.5, 0.0], "top-right": [1.0, 0.0],
    "left": [0.0, 0.5], "right": [1.0, 0.5], "bottom-left": [0.0, 1.0], "bottom": [0.5, 1.0],
    "bottom-right": [1.0, 1.0],
}
_SPLIT_TARGETS = ("chars", "words", "lines")
_STAGGER_TARGETS = ("chars", "words", "lines", "bars")
# Espacement par défaut des barres : 20 % du pas (w / n) — lisible quel que soit le nombre de barres,
# jamais de largeur négative contrairement à un écart fixe en px.
_CHART_GAP_FRACTION = 0.2

_TIME_RE = re.compile(
    r"^(?:beat:(?P<beat>\d+(?:\.\d+)?)|marker:(?P<marker>[A-Za-z_][A-Za-z0-9_]*))(?P<off>[+-]\d+(?:\.\d+)?)?$"
)
_BEATS_RE = re.compile(r"^beats:(?P<n>\d+(?:\.\d+)?)$")
_POS_RE = re.compile(r"^(?:(?P<zone>safe|action):)?(?P<pct>-?\d+(?:\.\d+)?)%$")
_GSAP_FAMILIES = ("none", "linear", "power0", "power1", "power2", "power3", "power4", "sine", "expo",
                  "circ", "back", "elastic", "bounce", "quad", "cubic", "quart", "quint", "strong")
_GSAP_RE = re.compile(
    r"^(?P<fam>" + "|".join(_GSAP_FAMILIES) + r")(?:\.(?P<dir>in|out|inOut))?(?:\((?P<params>[^()]*)\))?$"
)
_STEPS_RE = re.compile(r"^steps\((?P<n>\d+)\)$")
_LINEAR_FAMILIES = ("none", "linear", "power0")
# Seuls back (overshoot) et elastic (amplitude, période) acceptent des paramètres GSAP ; ailleurs
# GSAP n'a pas de config() et lèverait une erreur au chargement de la page.
_EASE_PARAMS_MAX = {"back": 1, "elastic": 2}
_NUM_RE = re.compile(r"-?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?")
_FONTFACE_RE = re.compile(r"@font-face\s*\{(.*?)\}", re.S)
_UNICODE_RANGE_RE = re.compile(r"unicode-range\s*:\s*([^;]+);")


# ---------------------------------------------------------------------------------------------
# Erreur publique
# ---------------------------------------------------------------------------------------------


class SceneError(Exception):
    """Scène ou preset refusé.

    `errors` : messages actionnables (où, quoi, comment corriger) ; str(e) = puces jointes par « \\n ».
    `warnings` : avertissements recueillis avant le refus (informatifs).
    """

    def __init__(self, errors: list[str] | str, warnings: list[str] | None = None):
        if isinstance(errors, str):
            errors = [errors]
        self.errors: list[str] = _dedupe(errors)
        self.warnings: list[str] = _dedupe(warnings or [])
        super().__init__("\n".join(f"• {e}" for e in self.errors))


class _Skip(Exception):
    """Abandonne l'élément fautif (erreur déjà enregistrée) sans arrêter le reste de la compilation,
    pour que la SceneError finale regroupe toutes les erreurs de la scène."""


class _EaseError(ValueError):
    """Ease non résoluble ; le message est déjà rédigé pour l'utilisateur."""


_MISSING = object()


# ---------------------------------------------------------------------------------------------
# Petits utilitaires
# ---------------------------------------------------------------------------------------------


def _dedupe(items) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for it in items:
        if it and it not in seen:
            seen.add(it)
            out.append(it)
    return out


def _r(x: float) -> float:
    """Arrondi des temps calculés (et suppression de -0.0) pour un JSON stable et lisible."""
    v = round(float(x), _ROUND_DIGITS)
    return 0.0 if v == 0 else v


def _fr(x: float, *, min_dec: int = 0, max_dec: int = 6) -> str:
    """Nombre au format français (virgule décimale) pour les messages : 25.25 -> « 25,25 »."""
    s = f"{float(x):.{max_dec}f}"
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    if s in ("-0", ""):
        s = "0"
    ip, _, dec = s.partition(".")
    if len(dec) < min_dec:
        dec += "0" * (min_dec - len(dec))
    return ip + ("," + dec if dec else "")


def _fr_seconds(frames: int, fps: int) -> str:
    """Durée exacte de `frames` images écrite avec le moins de décimales qui reste entière × fps."""
    exact = frames / fps
    for nd in range(2, 10):
        v = round(exact, nd)
        if abs(v * fps - frames) < 1e-7:
            return _fr(v, min_dec=2, max_dec=nd)
    return _fr(exact, min_dec=2, max_dec=9)


def _q(v: Any, limit: int = 80) -> str:
    """Valeur citée dans un message (chaînes entre guillemets français, le reste en JSON court)."""
    if isinstance(v, str):
        s = v if len(v) <= limit else v[: limit - 1] + "…"
        return f"« {s} »"
    s = json.dumps(v, ensure_ascii=False)
    return s if len(s) <= limit else s[: limit - 1] + "…"


def _names(items) -> str:
    items = list(items)
    return ", ".join(f"« {x} »" for x in items) if items else "aucun"


def _suggest(value: str, candidates) -> str:
    close = difflib.get_close_matches(str(value), [str(c) for c in candidates], n=1, cutoff=0.5)
    return f" (vouliez-vous dire « {close[0]} » ?)" if close else ""


def _deep_merge(base: Any, over: Any) -> Any:
    """Fusion profonde : les objets sont fusionnés clé à clé, tout le reste (listes comprises) est remplacé."""
    if isinstance(base, dict) and isinstance(over, dict):
        out = dict(base)
        for k, v in over.items():
            out[k] = _deep_merge(base[k], v) if k in base else copy.deepcopy(v)
        return out
    return copy.deepcopy(over)


class _NonFinite:
    """Littéral non standard NaN / Infinity / -Infinity rencontré par json.loads (via parse_constant) :
    conservé comme marqueur pour être signalé avec son chemin JSON plutôt que converti en flottant."""

    __slots__ = ("literal",)

    def __init__(self, literal: str):
        self.literal = literal


def _non_finite_paths(obj: Any, parts: tuple = ()):
    """Chemins (tuples) des valeurs non finies : littéraux NaN/Infinity, ou flottants inf/nan (ex. 1e400)."""
    if isinstance(obj, _NonFinite):
        yield parts, obj.literal
    elif isinstance(obj, float) and not math.isfinite(obj):
        # 1e400 est du JSON valide mais déborde en inf : on le signale comme un littéral Infinity.
        yield parts, "NaN" if math.isnan(obj) else ("Infinity" if obj > 0 else "-Infinity")
    elif isinstance(obj, dict):
        for k, v in obj.items():
            yield from _non_finite_paths(v, parts + (k,))
    elif isinstance(obj, (list, tuple)):
        for n, v in enumerate(obj):
            yield from _non_finite_paths(v, parts + (n,))


def _non_finite_errors(data: Any, prefix: str = "") -> list[str]:
    """Messages actionnables pour chaque valeur non finie (NaN / Infinity) d'un document JSON."""
    return [
        f"{prefix}{_fmt_path(parts)}{_context(data, parts)} : valeur non finie « {lit} » interdite (NaN et Infinity ne "
        "sont pas du JSON standard et rendraient temps, positions et courbes indéfinis ; un nombre comme 1e400 "
        "déborde en Infinity) ; donnez un nombre fini (ex. 0, 1.5, 120)."
        for parts, lit in _non_finite_paths(data)
    ]


def _read_json(path: Path, what: str) -> Any:
    try:
        # utf-8-sig : tolère le BOM qu'ajoutent certains éditeurs Windows (Bloc-notes, PowerShell 5.1).
        text = Path(path).read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        raise SceneError(f"{what} introuvable : {path}. Vérifiez le chemin (absolu, ou relatif au dossier courant).") from None
    except OSError as exc:
        raise SceneError(f"{what} illisible : {path} ({exc}). Vérifiez les droits d'accès au fichier.") from None
    try:
        # parse_constant : json.loads accepte par défaut NaN / Infinity (hors norme JSON) ; on les capture
        # comme marqueurs pour les refuser avec leur chemin au lieu de planter plus loin dans les calculs.
        data = json.loads(text, parse_constant=_NonFinite)
    except json.JSONDecodeError as exc:
        raise SceneError(
            f"{what} {path} : JSON invalide ligne {exc.lineno}, colonne {exc.colno} ({exc.msg}). "
            "Corrigez la syntaxe : virgule manquante ou en trop, guillemets droits \"…\", aucun commentaire."
        ) from None
    bad = _non_finite_errors(data, prefix=f"{what} {path} → ")
    if bad:
        raise SceneError(bad)
    return data


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)
        fh.flush()
        # fsync avant renommage : un plantage ne laisse jamais un compiled.json tronqué.
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def _apply_case(text: str, case: str) -> str:
    # La casse est appliquée ici (et non en CSS text-transform) pour que le runtime, la vérification des
    # glyphes et le découpage en caractères voient exactement le même texte (CONTRACT §3.5).
    if case == "upper":
        return text.upper()
    if case == "lower":
        return text.lower()
    if case == "title":
        return text.title()
    return text


def _served_url(path: Path, *, directory: bool = False) -> str:
    """URL http://mograph.render/... d'un fichier servi au navigateur, déduite de config.SERVED_PREFIXES
    (même table que la route Playwright : une URL produite ici est forcément servie)."""
    resolved = Path(path).resolve()
    for prefix, root in config.SERVED_PREFIXES.items():
        try:
            rel = resolved.relative_to(Path(root).resolve())
        except ValueError:
            continue
        # « @ » est légal dans un chemin d'URL (RFC 3986) : on le garde lisible (@fontsource) ; espaces et
        # caractères non ASCII sont encodés en %XX (le pilote décode le chemin avant de lire le fichier).
        return f"{config.ORIGIN}{prefix}{urllib.parse.quote(rel.as_posix(), safe='/@')}" + ("/" if directory else "")
    raise ValueError(f"{resolved} n'est sous aucun dossier servi ({', '.join(config.SERVED_PREFIXES)})")


def _even_up(x: float) -> int:
    """Arrondi au pair supérieur (les codecs et le sous-échantillonnage 4:2:0 exigent des tailles paires)."""
    n = math.ceil(x - 1e-9)
    return n + (n % 2)


# ---------------------------------------------------------------------------------------------
# Schémas et messages d'erreur actionnables
# ---------------------------------------------------------------------------------------------

_SCHEMA_FILES = {
    "scene": config.SCHEMA_DIR / "scene.schema.json",
    "preset": config.SCHEMA_DIR / "preset.schema.json",
    "shot": config.SCHEMA_DIR / "internal" / "compiled_shot.schema.json",
    "job": config.SCHEMA_DIR / "internal" / "blender_job.schema.json",
}
_SCHEMA_CACHE: dict[str, Any] = {}


def _schemas() -> dict[str, Any]:
    """Charge une fois les 4 schémas dans un Registry commun (les $ref du preset visent la scène)."""
    if not _SCHEMA_CACHE:
        docs = {k: _read_json(p, "schéma") for k, p in _SCHEMA_FILES.items()}
        registry = referencing.Registry().with_resources(
            [(d["$id"], referencing.Resource.from_contents(d, default_specification=DRAFT202012)) for d in docs.values()]
        )
        _SCHEMA_CACHE["docs"] = docs
        _SCHEMA_CACHE["registry"] = registry
        _SCHEMA_CACHE["validators"] = {
            k: jsonschema.Draft202012Validator(d, registry=registry) for k, d in docs.items()
        }
        _SCHEMA_CACHE["ref_validators"] = {}
    return _SCHEMA_CACHE


def _ref_validator(uri: str) -> jsonschema.Draft202012Validator:
    """Validateur d'un sous-schéma désigné par URI absolue (ses $ref locaux restent résolus)."""
    cache = _schemas()["ref_validators"]
    if uri not in cache:
        cache[uri] = jsonschema.Draft202012Validator({"$ref": uri}, registry=_schemas()["registry"])
    return cache[uri]


_KIND_BY_PARENT = {"shots": "plan", "layers": "calque", "plates": "plaque", "objects": "objet", "lights": "lumière"}

_TYPE_FR = {
    "string": "une chaîne", "number": "un nombre", "integer": "un entier", "boolean": "un booléen (true/false)",
    "object": "un objet {…}", "array": "une liste […]", "null": "null",
}

# Motifs des schémas -> explication humaine (les motifs sont copiés des schémas normatifs ; un motif
# inconnu retombe sur un message générique qui cite le motif).
_PATTERN_HELP = {
    r"^[a-z0-9](?:[a-z0-9-]|_(?!_))*$": "identifiant en minuscules, chiffres, « - » et « _ » (jamais « __ », réservé aux dossiers de plaques), ex. « intro_titre »",
    r"^#(?:[0-9A-Fa-f]{6}|[0-9A-Fa-f]{8})$": "couleur hexadécimale « #RRGGBB » ou « #RRGGBBAA », ex. « #2F5BEA »",
    r"^(?:#(?:[0-9A-Fa-f]{6}|[0-9A-Fa-f]{8})|[a-z][A-Za-z0-9_]*)$": "couleur « #RRGGBB(AA) » ou nom de la palette du preset (bg, fg, primary, secondary, accent…)",
    r"^(?:beat:\d+(?:\.\d+)?|marker:[A-Za-z_][A-Za-z0-9_]*)(?:[+-]\d+(?:\.\d+)?)?$": "temps « beat:N » ou « marker:id » avec décalage optionnel en secondes (ex. « beat:4 », « beat:8-0.1 », « marker:drop+0.25 »), ou un nombre de secondes locales",
    r"^beats:\d+(?:\.\d+)?$": "durée « beats:N » (ex. « beats:2 »), un nombre de secondes, ou short / medium / long",
    r"^(?:(?:safe|action):)?-?\d+(?:\.\d+)?%$": "position « N% » du canevas, « safe:N% » (zone sûre titre) ou « action:N% » (zone sûre action), ex. « safe:50% », ou un nombre de px logiques",
    r"^-?\d+(?:\.\d+)?%$": "décalage « N% » de la boîte de la cible (ex. « 110% ») ou un nombre de px",
    r"^[A-Za-z][A-Za-z0-9_]*$": "nom en lettres, chiffres et « _ », commençant par une lettre",
    r"^[A-Za-z_][A-Za-z0-9_]*$": "identifiant en lettres, chiffres et « _ »",
    r"^[A-Za-z0-9_-]*$": "suffixe en lettres, chiffres, « _ » et « - » uniquement",
    r"^[a-z0-9][a-z0-9_]*$": "identifiant de preset en minuscules, chiffres et « _ »",
    r"^[a-z][A-Za-z0-9_]*$": "nom de couleur commençant par une minuscule (lettres, chiffres, « _ »)",
    r"^@fontsource/[a-z0-9-]+/files/[a-z0-9-]+\.woff2$": "chemin relatif à node_modules « @fontsource/<paquet>/files/<fichier>.woff2 »",
    r"^M\s*-?[0-9.]+\s*,\s*-?[0-9.]+\s*C[-0-9., ]+$": "tracé CustomEase « M0,0 C x1,y1 x2,y2 1,1 »",
}

_REF_DESC = {
    "color": "une couleur (#RRGGBB(AA) ou nom de palette)",
    "hexColor": "une couleur #RRGGBB(AA)",
    "hex": "une couleur #RRGGBB",
    "vec3": "un vecteur [x, y, z]",
    "timeRef": "un temps (secondes locales, « beat:N » ou « marker:id » ± décalage)",
    "durationRef": "une durée (secondes > 0, « beats:N » ou short / medium / long)",
    "material": "un matériau en ligne {base_color, roughness, …}",
    "segment": "un segment d'interpolation",
}

# anyOf des $defs de scene.schema.json -> explication complète.
_ANYOF_HELP = (
    ("timeRef", "référence de temps invalide : nombre de secondes LOCALES au plan, ou « beat:N » / « marker:id » (temps GLOBAL) avec décalage optionnel « +0.1 » / « -0.25 », ex. « beat:4-0.1 »"),
    ("durationRef", "durée invalide : nombre de secondes > 0, « beats:N » (ex. « beats:2 », nécessite audio.bpm) ou alias du preset short / medium / long"),
    ("position", "position invalide : nombre de px logiques, « N% » du canevas, « safe:N% » ou « action:N% » de la zone sûre (ex. « safe:50% »)"),
    ("paint", "peinture invalide : couleur « #RRGGBB(AA) », nom de palette ou « none »"),
    ("transition", "transition invalide : « cut », « crossfade » ou {\"type\": \"crossfade\", \"dur\": 0.4}"),
    ("textureOverride", "surcharge de texture invalide : false (désactive), nombre 0..1 (amount) ou objet fusionné sur la texture du preset"),
    ("boil", "boil invalide : true, false ou {\"amount\": px, \"freq\": > 0, \"step\": entier ≥ 1}"),
)


def _type_of(v: Any) -> str:
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "boolean"
    if isinstance(v, int):
        return "integer"
    if isinstance(v, float):
        return "number"
    if isinstance(v, str):
        return "string"
    if isinstance(v, list):
        return "array"
    if isinstance(v, dict):
        return "object"
    return type(v).__name__


def _fmt_path(parts) -> str:
    out = ""
    for p in parts:
        if isinstance(p, int):
            out += f"[{p}]"
        else:
            out += ("." if out else "") + str(p)
    return out or "(racine)"


def _context(root: Any, parts) -> str:
    """Étiquettes lisibles des éléments traversés : (plan « intro », calque « titre »)."""
    labels = []
    node = root
    prev = None
    for p in parts:
        try:
            node = node[p]
        except (KeyError, IndexError, TypeError):
            break
        if isinstance(p, int) and prev in _KIND_BY_PARENT and isinstance(node, dict) and isinstance(node.get("id"), str):
            labels.append(f"{_KIND_BY_PARENT[prev]} « {node['id']} »")
        prev = p
    return f" ({', '.join(labels)})" if labels else ""


def _allowed_keys(schema: dict, inst: Any) -> set[str]:
    """Propriétés admises par un schéma, y compris celles des branches if/then qui s'appliquent."""
    keys = set(schema.get("properties", {}))
    for sub in schema.get("allOf", []):
        cond = sub.get("if", {}).get("properties", {})
        if cond and isinstance(inst, dict) and all("const" in c and inst.get(k) == c["const"] for k, c in cond.items()):
            keys |= set(sub.get("then", {}).get("properties", {}))
    return keys


def _branch_desc(b: Any) -> str:
    if not isinstance(b, dict):
        return str(b)
    if "$ref" in b:
        name = b["$ref"].rsplit("/", 1)[-1]
        return _REF_DESC.get(name, name)
    if "const" in b:
        return _q(b["const"])
    if "enum" in b:
        return "une des valeurs " + ", ".join(_q(o) for o in b["enum"])
    if "pattern" in b:
        return _PATTERN_HELP.get(b["pattern"], f"une chaîne au motif {b['pattern']}")
    t = b.get("type")
    if t == "array" and b.get("minItems") and b.get("minItems") == b.get("maxItems"):
        return f"une liste de {b['minItems']} valeurs"
    if isinstance(t, str):
        return _TYPE_FR.get(t, t)
    if "required" in b:
        return "un objet avec " + ", ".join(_q(k) for k in b["required"])
    return "une valeur conforme"


def _type_matches(branch: dict, inst: Any) -> bool:
    """La branche d'un anyOf vise-t-elle le type de l'instance ? (pour expliquer la bonne branche)"""
    if "const" in branch:
        return inst == branch["const"]
    if "enum" in branch:
        return inst in branch["enum"]
    t = branch.get("type")
    if t is None:
        return False
    kinds = [t] if isinstance(t, str) else list(t)
    it = _type_of(inst)
    return it in kinds or (it == "integer" and "number" in kinds)


def _explain(err: jsonschema.ValidationError, root: Any, prefix: str = "") -> str:
    """Traduit une erreur jsonschema en message(s) français actionnable(s) (vide si c'est du bruit)."""
    parts = list(err.absolute_path)
    if parts:
        loc = prefix + _fmt_path(parts) + _context(root, parts)
    else:
        loc = prefix[:-3] if prefix.endswith(" → ") else (prefix or "(racine)")
    v = err.validator
    vv = err.validator_value
    inst = err.instance
    sch = err.schema if isinstance(err.schema, dict) else {}

    if v == "required":
        m = re.search(r"'(.+?)' is a required property", err.message)
        key = m.group(1) if m else next((k for k in vv if isinstance(inst, dict) and k not in inst), "?")
        desc = (sch.get("properties", {}).get(key) or {}).get("description")
        return f"{loc} : champ obligatoire « {key} » manquant ; ajoutez-le" + (f" ({desc})" if desc else "") + "."
    if v in ("additionalProperties", "unevaluatedProperties"):
        type_branches = [s.get("if", {}).get("properties", {}).get("type", {}).get("const") for s in sch.get("allOf", [])]
        if isinstance(inst, dict) and any(type_branches) and inst.get("type") not in type_branches:
            # Type de calque invalide : l'erreur d'enum sur « type » est signalée ; lister ici ses
            # propriétés comme « inconnues » serait trompeur.
            return ""
        allowed = _allowed_keys(sch, inst)
        extras = [k for k in inst if k not in allowed] if isinstance(inst, dict) else []
        if not extras:
            # Propriété valide mais « non évaluée » parce qu'une autre règle du même objet a échoué :
            # cette autre erreur est déjà signalée, on n'ajoute pas de bruit.
            return ""
        items = [f"propriété inconnue « {x} »{_suggest(x, sorted(allowed))}" for x in extras]
        kind = f" pour un calque « {inst['type']} »" if isinstance(inst, dict) and "anim" in allowed and isinstance(inst.get("type"), str) else ""
        return f"{loc} : {', '.join(items)}. Propriétés autorisées{kind} : {', '.join(sorted(allowed))}."
    if v == "enum":
        # Suggestion orthographique seulement pour du texte (une « faute de frappe » sur 29 i/s n'a pas de sens).
        hint = _suggest(inst, [o for o in vv if isinstance(o, str)]) if isinstance(inst, str) else ""
        return f"{loc} : valeur {_q(inst)} invalide ; valeurs possibles : {', '.join(_q(o) for o in vv)}{hint}."
    if v == "const":
        return f"{loc} : doit valoir {_q(vv)} (reçu {_q(inst)})."
    if v == "type":
        exp = [vv] if isinstance(vv, str) else list(vv)
        return (f"{loc} : type invalide, attendu {' ou '.join(_TYPE_FR.get(t, t) for t in exp)}, "
                f"reçu {_TYPE_FR.get(_type_of(inst), _type_of(inst))} ({_q(inst)}).")
    if v == "pattern":
        return f"{loc} : valeur {_q(inst)} invalide ; attendu : {_PATTERN_HELP.get(vv, f'une chaîne au motif {vv}')}."
    if v == "minimum":
        return f"{loc} : {_q(inst)} est trop petit, minimum {_q(vv)}."
    if v == "maximum":
        return f"{loc} : {_q(inst)} est trop grand, maximum {_q(vv)}."
    if v == "exclusiveMinimum":
        return f"{loc} : {_q(inst)} doit être strictement supérieur à {_q(vv)}."
    if v == "exclusiveMaximum":
        return f"{loc} : {_q(inst)} doit être strictement inférieur à {_q(vv)}."
    if v == "minItems":
        return f"{loc} : au moins {vv} élément(s) requis (reçu {len(inst) if isinstance(inst, list) else '?'})."
    if v == "maxItems":
        return f"{loc} : au plus {vv} élément(s) admis (reçu {len(inst) if isinstance(inst, list) else '?'})."
    if v == "minLength":
        return f"{loc} : texte vide ou trop court (au moins {vv} caractère(s))."
    if v == "maxLength":
        return f"{loc} : texte trop long (au plus {vv} caractères)."
    if v == "minProperties":
        return f"{loc} : au moins {vv} entrée(s) requise(s)."
    if v in ("anyOf", "oneOf"):
        branches = vv if isinstance(vv, list) else []
        if branches and all(isinstance(b, dict) and set(b) == {"required"} for b in branches):
            keys = [b["required"][0] for b in branches]
            return (f"{loc} : il faut au moins une des clés {', '.join(_q(k) for k in keys)} "
                    "(ex. {\"move\": \"rise\"} ou {\"to\": {\"opacity\": 1}}).")
        defs = _schemas()["docs"]["scene"]["$defs"]
        for name, help_ in _ANYOF_HELP:
            if sch == defs.get(name):
                return f"{loc} : {_q(inst)} — {help_}."
        # Une seule branche vise le type de l'instance (ex. [null, objet] avec un objet) : on explique
        # précisément ce qui cloche DANS cette branche plutôt que de lister toutes les alternatives.
        matching = [n for n, b in enumerate(branches) if isinstance(b, dict) and _type_matches(b, inst)]
        if len(matching) == 1:
            sub = [e for e in (err.context or []) if e.relative_schema_path and e.relative_schema_path[0] == matching[0]]
            msgs = _dedupe(_explain(e, root, prefix) for e in sorted(sub, key=_path_sort_key))
            if msgs:
                return "\n".join(msgs)
        return f"{loc} : {_q(inst)} ne convient pas ; attendu {' ou '.join(_branch_desc(b) for b in branches)}."
    return f"{loc} : règle de schéma « {v} » non respectée ({err.message})."


def _path_sort_key(err: jsonschema.ValidationError):
    return ([f"{p:09d}" if isinstance(p, int) else str(p) for p in err.absolute_path], str(err.validator))


def _schema_errors(validator: jsonschema.Draft202012Validator, instance: Any, *, root: Any = None,
                   prefix: str = "") -> list[str]:
    errs = sorted(validator.iter_errors(instance), key=_path_sort_key)
    # Un anyOf expliqué par sa branche peut produire plusieurs messages (séparés par « \n »).
    return _dedupe(m for e in errs for m in _explain(e, instance if root is None else root, prefix).split("\n"))


# ---------------------------------------------------------------------------------------------
# Easings
# ---------------------------------------------------------------------------------------------


def _unknown_ease_msg(value: str, preset: dict) -> str:
    customs = list((preset["motion"].get("customEases") or {}))
    examples = ["power1.out", "power2.out", "power3.out", "power4.out", "power2.inOut", "sine.inOut", "expo.out",
                "circ.out", "back.out(1.7)", "elastic.out(1,0.3)", "bounce.out", "steps(4)"]
    return (
        f"easing {_q(value)} inconnu{_suggest(value, list(EASE_ALIASES) + customs + examples)}. Valeurs possibles : "
        f"alias du preset ({', '.join(EASE_ALIASES)}), customEases du preset ({', '.join(customs) or 'aucun'}), "
        "ou ease GSAP 3 « famille.direction(paramètres) » avec famille parmi power1-4, sine, expo, circ, back, "
        "elastic, bounce, quad, cubic, quart, quint, strong et direction in / out / inOut "
        "(ex. « power3.out », « back.out(1.7) », « elastic.out(1,0.3) »), ou « steps(N) »."
    )


def _resolve_ease(value: str, preset: dict, *, allow_linear: bool, in_preset: bool = False,
                  context: str = "tween") -> dict:
    """Résout un ease (alias -> customEase / GSAP / steps) ; lève _EaseError avec un message actionnable.

    `in_preset` : l'ease vient du preset lui-même (message adapté : un move n'a pas d'allowLinear).
    `context` : « tween », « key » (clé 3D) ou « grow » (croissance de graphique, sans allowLinear) ;
    il adapte la correction proposée pour un ease linéaire refusé.
    Retour : {"text": chaîne GSAP ou nom de customEase, "kind": custom|steps|linear|gsap, ...}.
    """
    motion = preset["motion"]
    eases = motion["eases"]
    customs = motion.get("customEases") or {}
    name = str(value).strip()
    via: list[str] = []
    if name in EASE_ALIASES:
        # Résolution récursive d'UN niveau (CONTRACT §3.6.5) : un alias peut renvoyer à un autre alias.
        via.append(name)
        name = str(eases[name]).strip()
        if name in EASE_ALIASES:
            via.append(name)
            name = str(eases[name]).strip()
            if name in EASE_ALIASES:
                raise _EaseError(
                    f"l'alias « {via[0]} » renvoie à « {via[1]} » qui renvoie encore à l'alias « {name} » : une seule "
                    "indirection est permise ; corrigez motion.eases du preset (donnez un ease GSAP ou un customEase)."
                )
    shown = f"{_q(value)} (→ « {name} »)" if via else _q(value)
    if name in customs:
        return {"text": name, "kind": "custom", "path": customs[name], "via": via}
    compact = re.sub(r"\s+", "", name)
    m = _STEPS_RE.match(compact)
    if m:
        n = int(m["n"])
        if n < 1:
            raise _EaseError(f"easing {shown} : steps(N) exige N ≥ 1 (ex. « steps(6) »).")
        return {"text": f"steps({n})", "kind": "steps", "n": n, "via": via}
    m = _GSAP_RE.match(compact)
    if not m:
        raise _EaseError(_unknown_ease_msg(str(value), preset))
    fam, direction, params = m["fam"], m["dir"], m["params"]
    if fam in _LINEAR_FAMILIES:
        if params:
            raise _EaseError(f"easing {shown} : un easing linéaire n'accepte pas de paramètres.")
        if in_preset:
            raise _EaseError(
                f"easing linéaire {shown} interdit dans un preset (un move n'a pas d'allowLinear) : choisissez un "
                "ease GSAP (ex. « power2.out », « sine.inOut »), un customEase ou steps(N)."
            )
        if context == "grow":
            # grow n'a pas d'allowLinear (schéma) : proposer cette clé mènerait à une nouvelle erreur.
            raise _EaseError(
                f"easing linéaire {shown} refusé pour grow (croissance d'un graphique, aucun allowLinear possible ici) : "
                "choisissez un easing sans dépassement, ex. « power2.out », « power3.out », « expo.out », "
                "« sine.inOut », ou supprimez « ease » (alias « enter » du preset par défaut)."
            )
        if not allow_linear:
            where = "à cette clé 3D" if context == "key" else "à cette tween"
            raise _EaseError(
                f"easing linéaire {shown} refusé : une vitesse constante paraît mécanique. Choisissez un easing "
                "(alias « default », « power2.out »…) ou, si c'est voulu (défilement continu, rotation), ajoutez "
                f"\"allowLinear\": true {where}."
            )
        return {"text": "none", "kind": "linear", "via": via}
    toks = [t for t in params.split(",")] if params else []
    values: list[float] = []
    for t in toks:
        try:
            v = float(t)
        except ValueError:
            raise _EaseError(f"easing {shown} : paramètre « {t} » non numérique (ex. « back.out(1.7) »).") from None
        # float() accepte « nan », « inf » et « 1e400 » : GSAP rendrait des NaN (pixels faux sans erreur) et le
        # JSON strict de la tâche Blender serait impossible ; on exige donc un nombre fini.
        if not math.isfinite(v):
            raise _EaseError(f"easing {shown} : paramètre « {t} » non fini ; donnez un nombre fini (ex. « back.out(1.7) », "
                             "« elastic.out(1,0.3) »).")
        values.append(v)
    nmax = _EASE_PARAMS_MAX.get(fam, 0)
    if len(toks) > nmax:
        allowed = {0: "aucun paramètre", 1: "1 paramètre (overshoot)", 2: "2 paramètres (amplitude, période)"}[nmax]
        raise _EaseError(f"easing {shown} : « {fam} » accepte {allowed} ; retirez les parenthèses en trop.")
    if fam == "elastic" and any(v <= 0 for v in values):
        # GSAP divise par l'amplitude et remplace une période nulle par sa valeur par défaut (« || ») : avec
        # 0 ou moins, la courbe du navigateur et celle de Blender (période en images) divergeraient.
        raise _EaseError(f"easing {shown} : amplitude et période d'elastic doivent être strictement positives "
                         "(ex. « elastic.out(1,0.3) »).")
    # Sans direction, GSAP applique « out » : on l'écrit explicitement pour que le runtime et Blender
    # lisent exactement la même courbe (CONTRACT §3.6.5).
    direction = direction or config.GSAP_DEFAULT_DIRECTION
    text = f"{fam}.{direction}" + (f"({','.join(toks)})" if toks else "")
    return {"text": text, "kind": "gsap", "family": fam, "dir": direction,
            "params": values, "via": via}


def _custom_points(path: str) -> list[tuple[float, float]]:
    nums = [float(x) for x in _NUM_RE.findall(path)]
    return list(zip(nums[0::2], nums[1::2]))


def _custom_path_problem(path: str) -> str | None:
    nums = _NUM_RE.findall(path)
    if len(nums) < 8 or len(nums) % 2 or (len(nums) - 2) % 6:
        return ("tracé CustomEase invalide : attendu « M0,0 C x1,y1 x2,y2 x,y » suivi éventuellement d'autres "
                "triplets de points (segments cubiques complets)")
    pts = _custom_points(path)
    if pts[0] != (0.0, 0.0) or pts[-1] != (1.0, 1.0):
        return "un tracé CustomEase doit partir de 0,0 et finir en 1,1 (temps et progression normalisés)"
    anchors = [pts[0]] + pts[3::3]
    if any(b[0] < a[0] for a, b in zip(anchors, anchors[1:])):
        return "les abscisses (temps) des points d'ancrage du tracé doivent être croissantes"
    return None


def _ease_overshoots(info: dict) -> bool:
    """Vrai si la courbe sort de [0, 1] (interdit pour la croissance des graphiques, CONTRACT §3.7)."""
    if info["kind"] == "gsap":
        return info["family"] in ("back", "elastic")
    if info["kind"] == "custom":
        return any(y < 0 or y > 1 for _, y in _custom_points(info["path"]))
    return False


def _blender_segment(info: dict, f0: float, f1: float) -> dict:
    """Interpolation Blender équivalente à l'ease GSAP d'un segment (CONTRACT §3.10)."""
    kind = info["kind"]
    if kind == "custom":
        # Aucun équivalent exact : approximation documentée par la config (BACK EASE_IN_OUT 1.2).
        return dict(config.CUSTOM_EASE_BLENDER)
    if kind == "steps":
        return {"interpolation": "CONSTANT", "easing": "AUTO"}
    if kind == "linear":
        return {"interpolation": "LINEAR", "easing": "AUTO"}
    fam = info["family"]
    seg: dict[str, Any] = {
        "interpolation": config.GSAP_TO_BLENDER_INTERP[fam],
        "easing": config.GSAP_TO_BLENDER_EASING[info["dir"]],
    }
    if fam == "back":
        seg["back"] = info["params"][0] if info["params"] else config.GSAP_BACK_DEFAULT
    elif fam == "elastic":
        # GSAP exprime la période en fraction de la durée, Blender en images : conversion par segment.
        p = info["params"][1] if len(info["params"]) >= 2 else config.GSAP_ELASTIC_PERIOD_DEFAULT
        seg["amplitude"] = 0.0
        seg["period"] = _r(p * (f1 - f0))
    return seg


# ---------------------------------------------------------------------------------------------
# Polices : unicode-range lu dans les CSS @fontsource
# ---------------------------------------------------------------------------------------------


def _parse_unicode_range(text: str) -> list[tuple[int, int]]:
    out = []
    for part in text.split(","):
        part = part.strip().upper()
        if not part:
            continue
        if not part.startswith("U+"):
            raise ValueError(f"segment d'unicode-range invalide « {part} »")
        body = part[2:]
        if "-" in body:
            a, b = body.split("-", 1)
            out.append((int(a, 16), int(b, 16)))
        elif "?" in body:
            out.append((int(body.replace("?", "0"), 16), int(body.replace("?", "F"), 16)))
        else:
            out.append((int(body, 16), int(body, 16)))
    return out


def _css_unicode_range(file_rel: str, weight: int, style: str) -> str:
    """unicode-range du bloc @font-face dont le src référence ce fichier (CONTRACT §3.5)."""
    parts = file_rel.split("/")
    pkg, fname = parts[1], parts[-1]
    # Les fichiers <sous-ensemble>-<graisse>.css n'ont pas d'unicode-range (vérifié) : seul <graisse>.css
    # (ou <graisse>-italic.css) les donne, un bloc par sous-ensemble.
    css = config.NODE_MODULES / "@fontsource" / pkg / (f"{weight}-italic.css" if style == "italic" else f"{weight}.css")
    try:
        text = css.read_text(encoding="utf-8")
    except OSError:
        raise ValueError(
            f"CSS {css} introuvable : impossible de lire l'unicode-range de {file_rel}. Lancez « npm ci » "
            "ou vérifiez que la graisse existe dans le paquet @fontsource."
        ) from None
    for block in _FONTFACE_RE.findall(text):
        if f"./files/{fname}" in block:
            m = _UNICODE_RANGE_RE.search(block)
            if m:
                return m.group(1).strip()
    raise ValueError(
        f"aucun bloc @font-face de {css.name} ne référence ./files/{fname} avec un unicode-range : vérifiez "
        "que le fichier correspond bien à cette graisse et à ce style, ou donnez unicodeRange explicitement."
    )


# ---------------------------------------------------------------------------------------------
# Presets
# ---------------------------------------------------------------------------------------------


def _check_preset(data: dict, label: str) -> list[str]:
    """Règles du preset que le schéma ne peut pas exprimer (fichiers, polices, eases, 3D)."""
    errors: list[str] = []
    palette = data["palette"]

    declared: set[tuple[str, int, str]] = set()
    for n, font in enumerate(data["fonts"]):
        where = f"{label} → fonts[{n}]"
        font.setdefault("style", "normal")
        path = config.NODE_MODULES / font["file"]
        if not path.is_file():
            errors.append(
                f"{where} : fichier de police introuvable : {path}. Lancez « npm ci » à la racine (versions figées "
                "de package-lock.json) ou corrigez le chemin (sous-ensemble latin : "
                "@fontsource/<paquet>/files/<paquet>-latin-<graisse>-normal.woff2)."
            )
            continue
        try:
            if "unicodeRange" not in font:
                font["unicodeRange"] = _css_unicode_range(font["file"], font["weight"], font["style"])
            _parse_unicode_range(font["unicodeRange"])
        except ValueError as exc:
            errors.append(f"{where} : {exc}")
            continue
        declared.add((font["family"], font["weight"], font["style"]))

    for style in ("display", "body"):
        t = data["typography"][style]
        if (t["family"], t["weight"], "normal") not in declared:
            errors.append(
                f"{label} → typography.{style} : police « {t['family']} » {t['weight']} non déclarée dans fonts "
                "(ou fichier absent) ; ajoutez {\"family\": \"" + t["family"] + "\", \"weight\": " + str(t["weight"])
                + ", \"file\": \"@fontsource/<paquet>/files/<paquet>-latin-" + str(t["weight"]) + "-normal.woff2\"}."
            )

    motion = data["motion"]
    for name, path in (motion.get("customEases") or {}).items():
        prob = _custom_path_problem(path)
        if prob:
            errors.append(f"{label} → motion.customEases.{name} : {prob}.")
    for alias in EASE_ALIASES:
        try:
            _resolve_ease(alias, data, allow_linear=False, in_preset=True)
        except _EaseError as exc:
            errors.append(f"{label} → motion.eases.{alias} : {exc}")
    for move, steps in motion["moves"].items():
        for i, st in enumerate(steps):
            where = f"{label} → motion.moves.{move}[{i}]"
            try:
                _resolve_ease(st.get("ease", "default"), data, allow_linear=False, in_preset=True)
            except _EaseError as exc:
                errors.append(f"{where}.ease : {exc}")
            props = set(st.get("from") or {}) | set(st.get("to") or {})
            target = st.get("target", "self")
            if props & {"draw", "fill", "stroke"} and target != "shape":
                errors.append(f"{where} : draw / fill / stroke s'animent sur la cible « shape » ; ajoutez \"target\": \"shape\".")
            if "color" in props and target in ("shape", "bars"):
                errors.append(f"{where} : « color » ne s'applique qu'au texte (cibles self, chars, words, lines).")
            for side in ("from", "to"):
                for p in ("color", "fill", "stroke"):
                    v = (st.get(side) or {}).get(p)
                    if isinstance(v, str) and not v.startswith("#") and v not in palette:
                        errors.append(f"{where}.{side}.{p} : couleur « {v} » absente de la palette ({', '.join(palette)}).")

    tr = data["transitions"]
    if tr["default"] == "crossfade" and "crossfade" not in tr:
        errors.append(f"{label} → transitions : default = « crossfade » exige transitions.crossfade.dur (ex. {{\"dur\": 0.4}}).")

    blender = data.get("blender") or {}

    def _check_color(v: Any, where: str) -> None:
        if isinstance(v, str) and not v.startswith("#"):
            if v not in palette:
                errors.append(f"{where} : couleur « {v} » absente de la palette ({', '.join(palette)}).")
            else:
                v = palette[v]
        if isinstance(v, str) and len(v) != 7:
            errors.append(f"{where} : couleur 3D « {v} » : Blender n'accepte pas d'alpha, utilisez « #RRGGBB ».")

    for name, mat in (blender.get("materials") or {}).items():
        for key in ("base_color", "emission"):
            if key in mat:
                _check_color(mat[key], f"{label} → blender.materials.{name}.{key}")
    if "color" in (blender.get("world") or {}):
        _check_color(blender["world"]["color"], f"{label} → blender.world.color")
    ids = set()
    for n, light in enumerate(blender.get("light_rig") or []):
        where = f"{label} → blender.light_rig[{n}]"
        if light["type"] != "SUN" and "location" not in light:
            errors.append(f"{where} : une lumière {light['type']} doit avoir une position « location » [x, y, z].")
        if "color" in light:
            _check_color(light["color"], f"{where}.color")
        lid = light.get("id", f"light_{n}")
        if lid in ids:
            errors.append(f"{where} : identifiant de lumière « {lid} » en double ; donnez un « id » unique.")
        ids.add(lid)
    return errors


def load_preset(preset_id: str, overrides: dict | None = None) -> dict:
    """presets/<id>.json + fusion profonde des overrides, validé, polices complétées (unicodeRange)."""
    if not isinstance(preset_id, str) or not re.fullmatch(r"[a-z0-9][a-z0-9_]*", preset_id):
        raise SceneError(f"identifiant de preset {_q(preset_id)} invalide : minuscules, chiffres et « _ » (ex. « flat_riso »).")
    path = config.PRESETS_DIR / f"{preset_id}.json"
    if not path.is_file():
        avail = sorted(p.stem for p in config.PRESETS_DIR.glob("*.json"))
        raise SceneError(
            f"preset « {preset_id} » introuvable ({path}){_suggest(preset_id, avail)} ; presets disponibles : "
            f"{', '.join(avail) or 'aucun'}. Corrigez le champ « preset » de la scène."
        )
    data = _read_json(path, "preset")
    label = f"preset « {preset_id} »" + (" (après preset_overrides)" if overrides else "")
    if overrides:
        if not isinstance(overrides, dict):
            raise SceneError("preset_overrides doit être un objet dont les clés sont celles de preset.schema.json.")
        data = _deep_merge(data, overrides)
    errors = _schema_errors(_schemas()["validators"]["preset"], data, prefix=f"{label} → ")
    if errors:
        raise SceneError(errors)
    if data["id"] != preset_id:
        raise SceneError(f"{label} → id : l'id interne « {data['id']} » ne correspond pas au fichier {path.name} ; "
                         "renommez le fichier ou corrigez l'id (et ne le changez pas via preset_overrides).")
    errors = _check_preset(data, label)
    if errors:
        raise SceneError(errors)
    return data


# ---------------------------------------------------------------------------------------------
# Compilateur
# ---------------------------------------------------------------------------------------------


@dataclass
class _ShotTiming:
    index: int
    id: str
    frames: int
    fade: int
    global_start: int
    duration: float


class _Compiler:
    def __init__(self, raw: Any, source: str | None):
        self.raw = raw
        self.source = source
        self.errors: list[str] = []
        self.warnings: list[str] = []

    # -- journal ---------------------------------------------------------------------------------

    def loc(self, *parts) -> str:
        return _fmt_path(parts) + _context(self.raw, parts)

    def _where(self, where) -> str:
        return self.loc(*where) if isinstance(where, tuple) else str(where)

    def err(self, where, msg: str) -> None:
        self.errors.append(f"{self._where(where)} : {msg}")

    def warn(self, where, msg: str) -> None:
        self.warnings.append(f"{self._where(where)} : {msg}")

    def fail(self, where, msg: str):
        self.err(where, msg)
        raise _Skip()

    # -- point d'entrée --------------------------------------------------------------------------

    def run(self) -> dict:
        raw = self.raw
        if not isinstance(raw, dict):
            raise SceneError("(racine) : une scène est un objet JSON {…} (voir schema/scene.schema.json).")
        errors = _schema_errors(_schemas()["validators"]["scene"], raw)
        if errors:
            # Sans structure valide, les règles sémantiques liraient des valeurs absurdes : on s'arrête là.
            raise SceneError(errors)
        # Les messages de load_preset nomment déjà le preset et le chemin fautif : on les relaie tels quels.
        self.preset = load_preset(raw["preset"], raw.get("preset_overrides"))
        self._setup()
        self._timing()
        shots = []
        for i, shot in enumerate(raw["shots"]):
            try:
                shots.append(self._compile_shot(i, shot))
            except _Skip:
                continue
        outputs = self._outputs()
        audio = self._audio()
        self._storyboard()
        if self.errors:
            raise SceneError(self.errors, self.warnings)
        last = self.timing[-1]
        total = last.global_start + last.frames
        compiled = {
            "contract": CONTRACT_SCENE,
            "scene_id": self.scene_id,
            "title": raw["title"],
            "source": self.source,
            "preset_id": self.preset["id"],
            "format": {"aspect": self.aspect, "resolution": self.resolution, "fps": self.fps, "alpha": self.alpha},
            "canvas": {"width": self.canvas_w, "height": self.canvas_h},
            "device_scale_factor": self.dsf,
            "output": {"width": self.out_w, "height": self.out_h},
            "frames": total,
            "duration": _r(total / self.fps),
            "frame_step": self.frame_step,
            "motion_blur": dict(self.motion_blur),
            # 16 bits seulement si le flou de mouvement moyenne des sous-images (sinon 8 bits suffit, SPEC §14).
            "depth": 16 if self.motion_blur["enabled"] else 8,
            "audio": audio,
            "outputs": outputs,
            "qc": self.qc,
            "shots": shots,
            "warnings": _dedupe(self.warnings),
        }
        self._assert_contract(compiled)
        return compiled

    # -- réglages globaux ------------------------------------------------------------------------

    def _setup(self) -> None:
        raw, preset = self.raw, self.preset
        fmt = raw["format"]
        self.scene_id = raw["id"]
        self.seed = int(raw.get("seed", 1))
        self.aspect, self.resolution, self.fps = fmt["aspect"], fmt["resolution"], int(fmt["fps"])
        self.alpha = bool(fmt.get("alpha", False))
        self.canvas_w, self.canvas_h = config.LOGICAL_CANVAS[self.aspect]
        self.dsf = config.RESOLUTION_SCALE[self.resolution]
        self.out_w, self.out_h = config.output_size(self.aspect, self.resolution)
        self.palette = {k: v.upper() for k, v in preset["palette"].items()}
        self.quality = raw.get("quality") or {}
        self.plate_scale = float(self.quality.get("plate_scale", 1))
        self.frame_step = int(preset["motion"]["frame_step"])
        self.durations = preset["motion"]["durations"]

        audio = raw.get("audio")
        self.bpm = float(audio["bpm"]) if audio and "bpm" in audio else None
        self.audio_offset = float(audio.get("offset", 0)) if audio else 0.0
        self.markers: dict[str, float] = {}
        for n, mk in enumerate((audio or {}).get("markers") or []):
            if mk["id"] in self.markers:
                # On garde la première définition : les références restent calculables (pas d'erreurs en cascade).
                self.err(("audio", "markers", n, "id"), f"marqueur « {mk['id']} » défini deux fois ; renommez-le ou supprimez le doublon.")
                continue
            self.markers[mk["id"]] = float(mk["time"])

        mb = dict(_MOTION_BLUR_DEFAULT)
        mb.update(preset["render"]["motion_blur"])
        mb.update(self.quality.get("motion_blur") or {})
        if mb["enabled"] and self.frame_step > 1:
            self.warn(("quality",) if "motion_blur" in self.quality else f"preset « {preset['id']} » → render.motion_blur",
                      f"flou de mouvement désactivé : le preset anime tenu {self.frame_step} images (frame_step "
                      f"{self.frame_step}), un flou entre images identiques n'aurait aucun sens.")
            mb["enabled"] = False
        self.motion_blur = {"enabled": bool(mb["enabled"]), "samples": int(mb["samples"]), "shutter": float(mb["shutter"])}

        qc = raw.get("qc") or {}
        self.qc = {
            "safe_zone": qc.get("safe_zone", "title"),
            "safe_zone_tolerance_s": float(qc.get("safe_zone_tolerance_s", config.SAFE_ZONE_TOLERANCE_S_DEFAULT)),
            "allow_black_s": float(qc.get("allow_black_s", 0)),
            "determinism_frames": int(qc.get("determinism_frames", config.DETERMINISM_FRAMES_DEFAULT)),
            "loudness_target_lufs": float(qc.get("loudness_target_lufs", -14)),
        }

    # -- références de temps et de durée -----------------------------------------------------------

    def time_ref(self, value: Any, where, st: _ShotTiming) -> float:
        """Temps -> secondes LOCALES au plan (CONTRACT §3.1)."""
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return _r(value)
        m = _TIME_RE.match(str(value))
        if not m:
            self.fail(where, f"référence de temps {_q(value)} invalide : secondes locales (nombre), « beat:N » ou « marker:id » avec décalage optionnel.")
        if m["beat"] is not None:
            if self.bpm is None:
                self.fail(where, f"référence {_q(value)} en temps musicaux alors que audio.bpm est absent : ajoutez "
                                 "\"audio\": {\"bpm\": <tempo>} (avec src ou synth) ou exprimez le temps en secondes locales au plan.")
            t_global = self.audio_offset + float(m["beat"]) * 60.0 / self.bpm
        else:
            name = m["marker"]
            if name not in self.markers:
                self.fail(where, f"marqueur « {name} » inconnu{_suggest(name, self.markers)} ; marqueurs définis dans "
                                 f"audio.markers : {_names(self.markers)}. Ajoutez {{\"id\": \"{name}\", \"time\": <secondes>}} "
                                 "à audio.markers ou corrigez le nom.")
            t_global = self.markers[name]
        if m["off"]:
            t_global += float(m["off"])
        return _r(t_global - st.global_start / self.fps)

    def duration_ref(self, value: Any, where) -> float:
        """Durée -> secondes (nombre, « beats:N » ou alias du preset)."""
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            d = float(value)
        elif value in ("short", "medium", "long"):
            d = float(self.durations[value])
        else:
            m = _BEATS_RE.match(str(value))
            if not m:
                self.fail(where, f"durée {_q(value)} invalide : secondes (> 0), « beats:N » ou short / medium / long.")
            if self.bpm is None:
                self.fail(where, f"durée {_q(value)} en temps musicaux alors que audio.bpm est absent : ajoutez "
                                 "\"audio\": {\"bpm\": <tempo>} ou donnez la durée en secondes.")
            d = float(m["n"]) * 60.0 / self.bpm
        if d <= 0:
            self.fail(where, f"durée {_q(value)} nulle : donnez une durée strictement positive.")
        return _r(d)

    def _integer_frames(self, seconds: float, where, what: str) -> int | None:
        """Nombre d'images entier ou erreur explicative avec deux valeurs valides proches (§3.1)."""
        n = seconds * self.fps
        if abs(n - round(n)) <= _INT_TOLERANCE:
            return int(round(n))
        lo, hi = math.floor(n), math.ceil(n)
        if lo < 1:
            lo, hi = hi, hi + 1
        self.err(where,
                 f"{what} {_fr(seconds, min_dec=2)} s à {self.fps} i/s = {_fr(n, max_dec=4)} images (non entier) ; "
                 f"utilisez {_fr_seconds(lo, self.fps)} s ou {_fr_seconds(hi, self.fps)} s ({lo} ou {hi} images) : "
                 "durée × fps doit être un nombre entier d'images.")
        return None

    # -- plans : durées, fondus, départs globaux ---------------------------------------------------

    def _timing(self) -> None:
        self.timing: list[_ShotTiming] = []
        preset_tr = self.preset["transitions"]
        prev: _ShotTiming | None = None
        seen: set[str] = set()
        for i, shot in enumerate(self.raw["shots"]):
            sid = shot["id"]
            if sid in seen:
                self.err(("shots", i, "id"), f"identifiant de plan « {sid} » en double : chaque plan doit avoir un id unique (dossier build/<scène>/shots/<id>).")
            seen.add(sid)
            try:
                dur = self.duration_ref(shot["duration"], ("shots", i, "duration"))
            except _Skip:
                dur = 1.0
            frames = self._integer_frames(dur, ("shots", i, "duration"), "durée de plan")
            if frames is None:
                # On poursuit avec l'arrondi pour signaler aussi les autres erreurs de la scène.
                frames = max(1, int(round(dur * self.fps)))
            if frames < 1:
                self.err(("shots", i, "duration"), "un plan doit durer au moins une image.")
                frames = 1

            tr = shot.get("transition_in")
            explicit = tr is not None
            if tr is None:
                kind, tdur = (preset_tr["default"] if i > 0 else "cut"), None
            elif isinstance(tr, str):
                kind, tdur = tr, None
            else:
                kind, tdur = tr["type"], tr.get("dur")
            fade = 0
            if kind == "crossfade":
                where = ("shots", i, "transition_in")
                if i == 0:
                    if explicit:
                        self.err(where, "un fondu enchaîné est impossible sur le premier plan (aucun plan précédent) : "
                                        "supprimez transition_in ou mettez « cut ».")
                else:
                    try:
                        if tdur is not None:
                            fsec = self.duration_ref(tdur, where + ("dur",))
                        elif "crossfade" in preset_tr:
                            fsec = float(preset_tr["crossfade"]["dur"])
                        else:
                            self.fail(where, f"le preset « {self.preset['id']} » ne définit pas transitions.crossfade.dur : "
                                             "précisez {\"type\": \"crossfade\", \"dur\": <secondes>}.")
                        what = "fondu" if explicit else f"fondu par défaut du preset « {self.preset['id']} » :"
                        f = self._integer_frames(fsec, where + (("dur",) if tdur is not None else ()), what)
                        if f is not None:
                            limit = min(prev.frames, frames)
                            if f < 1:
                                self.err(where, "un fondu doit durer au moins une image.")
                            elif f > limit:
                                self.err(where, f"fondu de {f} images plus long que le plan précédent ({prev.frames} images) "
                                                f"ou que ce plan ({frames} images) : réduisez-le à {limit} images "
                                                f"({_fr_seconds(limit, self.fps)} s) au plus, ou allongez les plans.")
                            else:
                                fade = f
                    except _Skip:
                        pass
            gs = 0 if prev is None else prev.global_start + prev.frames - fade
            st = _ShotTiming(index=i, id=sid, frames=frames, fade=fade, global_start=gs, duration=_r(frames / self.fps))
            self.timing.append(st)
            prev = st

    # -- couleurs, positions, polices ------------------------------------------------------------

    def color(self, value: Any, where, *, rgb_only: bool = False) -> str:
        """Couleur -> hexadécimal majuscule (nom de palette résolu), CONTRACT §3.4."""
        if isinstance(value, str) and value.startswith("#"):
            h = value.upper()
        elif isinstance(value, str) and value in self.palette:
            h = self.palette[value]
        else:
            self.fail(where, f"couleur {_q(value)} inconnue{_suggest(value, self.palette)} ; noms de la palette du preset "
                             f"« {self.preset['id']} » : {', '.join(sorted(self.palette))}, ou un hexadécimal « #RRGGBB ».")
        if rgb_only and len(h) != 7:
            self.fail(where, f"couleur {h} : les couleurs 3D (Blender) n'ont pas de canal alpha, utilisez « #RRGGBB ».")
        return h

    def paint(self, value: Any, where) -> str:
        return "none" if value == "none" else self.color(value, where)

    def position(self, value: Any, axis: str, where) -> float:
        """Position -> px logiques (nombre, « N% » du canevas, « safe:N% » / « action:N% »)."""
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return float(value)
        m = _POS_RE.match(str(value))
        if not m:
            self.fail(where, f"position {_q(value)} invalide : px, « N% », « safe:N% » ou « action:N% ».")
        pct = float(m["pct"]) / 100.0
        if m["zone"] is None:
            return _r(pct * (self.canvas_w if axis == "x" else self.canvas_h))
        rect = config.safe_rect(self.aspect, "title" if m["zone"] == "safe" else "action")
        if axis == "x":
            return _r(rect["left"] + pct * rect["width"])
        return _r(rect["top"] + pct * rect["height"])

    def fonts_for(self, family: str, weight: int, style: str = "normal") -> list[dict]:
        return [f for f in self.preset["fonts"]
                if f["family"] == family and f["weight"] == weight and f.get("style", "normal") == style]

    def require_font(self, family: str, weight: int, where) -> bool:
        if self.fonts_for(family, weight):
            return True
        avail = sorted({f["weight"] for f in self.preset["fonts"] if f["family"] == family})
        pkg = family.lower().replace(" ", "-")
        self.err(where,
                 f"police manquante : « {family} » graisse {weight} n'est pas déclarée dans preset.fonts ("
                 + (f"graisses disponibles : {', '.join(map(str, avail))}" if avail else "famille absente du preset")
                 + f"). Utilisez une graisse disponible, ou ajoutez via preset_overrides une entrée {{\"family\": \"{family}\", "
                 f"\"weight\": {weight}, \"file\": \"@fontsource/{pkg}/files/{pkg}-latin-{weight}-normal.woff2\"}} si ce "
                 "fichier existe (aucune police de repli n'est jamais utilisée).")
        return False

    def check_glyphs(self, text: str, family: str, weight: int, where) -> None:
        """Chaque caractère (hors \\n) doit être couvert par l'unicode-range d'un fichier de la police."""
        ranges: list[tuple[int, int]] = []
        for f in self.fonts_for(family, weight):
            ranges += _parse_unicode_range(f["unicodeRange"])
        missing = sorted({c for c in text if c != "\n" and not any(lo <= ord(c) <= hi for lo, hi in ranges)})
        if missing:
            shown = ", ".join(f"« {c} » (U+{ord(c):04X})" for c in missing[:12])
            self.err(where,
                     f"caractère(s) non couvert(s) par « {family} » {weight} : {shown}. Aucune police de repli n'est "
                     "utilisée (rendu non déterministe) : remplacez ces caractères, ou déclarez via preset_overrides un "
                     "fichier @fontsource qui les couvre (ex. sous-ensemble latin-ext).")

    def font_block(self, style: str, over: dict, where) -> tuple[dict, str, bool]:
        """Police résolue {family, weight, size, line_height, tracking, color} + casse + validité."""
        typo = self.preset["typography"][style]
        family = typo["family"]
        weight = int(over.get("weight", typo["weight"]))
        font = {
            "family": family,
            "weight": weight,
            "size": float(over.get("size", typo.get("size", _TEXT_SIZE_DEFAULT[style]))),
            "line_height": float(over.get("lineHeight", typo["lineHeight"])),
            "tracking": float(over.get("tracking", typo["tracking"])),
            "color": self.color(over.get("color", "fg"), where + (("color",) if "color" in over else ())),
        }
        ok = self.require_font(family, weight, where + (("weight",) if "weight" in over else ()))
        return font, over.get("case", typo["case"]), ok

    # -- plan ------------------------------------------------------------------------------------

    def _compile_shot(self, i: int, shot: dict) -> dict:
        st = self.timing[i]
        sp = ("shots", i)
        seed = int(hashlib.sha256(f"{self.seed}:{shot['id']}".encode("utf-8")).hexdigest()[:8], 16)

        bg = shot.get("background")
        background: str | None
        if self.alpha:
            if bg is not None and bg != "transparent":
                self.err(sp + ("background",), "format.alpha est actif : le fond de tous les plans est transparent. "
                                               "Supprimez « background » (ou mettez « transparent ») ; pour un fond coloré, "
                                               "retirez format.alpha.")
            background = None
        elif bg == "transparent":
            self.warn(sp + ("background",), "fond transparent sans format.alpha : les zones vides seront noires dans les "
                                            "livrables opaques ; activez format.alpha pour un livrable incrustable.")
            background = None
        else:
            try:
                background = self.color(bg if bg is not None else "bg", sp + (("background",) if bg is not None else ()))
            except _Skip:
                background = None

        layers = shot.get("layers") or []
        plates = shot.get("plates") or []
        layer_ids: list[str] = []
        for j, layer in enumerate(layers):
            if layer["id"] in layer_ids:
                self.err(sp + ("layers", j, "id"), f"identifiant de calque « {layer['id']} » en double dans ce plan ; donnez un id unique.")
            layer_ids.append(layer["id"])
        plate_ids: list[str] = []
        for p, plate in enumerate(plates):
            if plate["id"] in plate_ids:
                self.err(sp + ("plates", p, "id"), f"identifiant de plaque « {plate['id']} » en double dans ce plan ; donnez un id unique.")
            plate_ids.append(plate["id"])

        textures = self._textures(sp, shot)
        glitch = None
        try:
            glitch = self._glitch(sp, shot, st, layer_ids)
        except _Skip:
            pass

        compiled_layers = []
        for j, layer in enumerate(layers):
            try:
                cl = self._compile_layer(st, sp + ("layers", j), layer, plate_ids)
            except _Skip:
                continue
            compiled_layers.append((cl["z"], j, cl))
        # Tri stable par z puis ordre de déclaration : c'est l'ordre du DOM (compiled_shot.schema.json).
        compiled_layers.sort(key=lambda t: (t[0], t[1]))

        jobs, plates_spec = [], {}
        for p, plate in enumerate(plates):
            try:
                job = self._compile_plate(st, sp + ("plates", p), shot, plate, layers)
            except _Skip:
                continue
            jobs.append(job)
            plates_spec[plate["id"]] = {
                "url": _served_url(Path(job["out_dir"]), directory=True),
                "frames": job["frames"],
                "width": job["width"],
                "height": job["height"],
            }

        rects = {}
        for zone in ("title", "action"):
            r = config.safe_rect(self.aspect, zone)
            rects[zone] = {k: _r(r[k]) for k in ("left", "top", "right", "bottom")}
        spec = {
            "contract": CONTRACT_SHOT,
            "scene_id": self.scene_id,
            "shot_id": shot["id"],
            "shot_index": i,
            "seed": seed,
            "fps": self.fps,
            "frames": st.frames,
            "duration": st.duration,
            "canvas": {"width": self.canvas_w, "height": self.canvas_h},
            "device_scale_factor": self.dsf,
            "alpha": self.alpha,
            "background": background,
            "frame_step": self.frame_step,
            "motion_blur": dict(self.motion_blur),
            "safe_zones": rects,
            "palette": dict(self.palette),
            "fonts": [
                {"family": f["family"], "weight": f["weight"], "style": f.get("style", "normal"),
                 "url": _served_url(config.NODE_MODULES / f["file"]), "unicode_range": f["unicodeRange"]}
                for f in self.preset["fonts"]
            ],
            "typography": copy.deepcopy(self.preset["typography"]),
            "custom_eases": dict(self.preset["motion"].get("customEases") or {}),
            "textures": textures,
            "glitch": glitch,
            "plates": plates_spec,
            "layers": [cl for _, _, cl in compiled_layers],
        }
        return {"id": shot["id"], "index": i, "frames": st.frames, "global_start": st.global_start,
                "fade_in_frames": st.fade, "spec": spec, "plates": jobs}

    # -- textures et glitch ----------------------------------------------------------------------

    def _textures(self, sp: tuple, shot: dict) -> dict:
        base = self.preset["textures"]
        fx = shot.get("fx") or {}
        out: dict[str, Any] = {}
        for name in _TEXTURE_NAMES:
            b = base.get(name)
            where = sp + ("fx", name)
            if name not in fx:
                val = copy.deepcopy(b)
            else:
                o = fx[name]
                if o is False:
                    val = None
                elif o is True:
                    val = copy.deepcopy(b) if b else {"amount": _TEXTURE_ON_AMOUNT[name]}
                elif isinstance(o, (int, float)):
                    val = {**(b or {}), "amount": float(o)}
                else:
                    val = {**(b or {"amount": _TEXTURE_ON_AMOUNT[name]}), **o}
            if val is not None:
                val = {**_TEXTURE_DEFAULTS[name], **val}
                if isinstance(val.get("color"), str):
                    try:
                        val["color"] = self.color(val["color"], where)
                    except _Skip:
                        pass
                uri = f"{_schemas()['docs']['preset']['$id']}#/properties/textures/properties/{name}"
                for msg in _schema_errors(_ref_validator(uri), val, root={}, prefix=f"{self.loc(*where)} → "):
                    self.errors.append(msg)
            out[name] = val
        return out

    def _glitch(self, sp: tuple, shot: dict, st: _ShotTiming, layer_ids: list[str]) -> dict | None:
        g = (shot.get("fx") or {}).get("glitch")
        if not g:
            return None
        gp = sp + ("fx", "glitch")
        wins: list[tuple[float, float, str]] = []
        for w, pair in enumerate(g.get("windows") or []):
            try:
                s = self.time_ref(pair[0], gp + ("windows", w, 0), st)
                d = self.duration_ref(pair[1], gp + ("windows", w, 1))
            except _Skip:
                continue
            wins.append((s, s + d, f"windows[{w}]"))
        ob = g.get("on_beats")
        if ob:
            if self.bpm is None:
                self.err(gp + ("on_beats",), "on_beats exige audio.bpm (tempo) : ajoutez \"audio\": {\"bpm\": <tempo>} ou "
                                             "utilisez des fenêtres explicites « windows ».")
            elif ob["from"] > ob["to"]:
                self.err(gp + ("on_beats",), f"on_beats.from ({_fr(ob['from'])}) doit être ≤ on_beats.to ({_fr(ob['to'])}).")
            else:
                try:
                    length = self.duration_ref(ob.get("length", _GLITCH_LENGTH_DEFAULT), gp + ("on_beats", "length"))
                    every = float(ob.get("every", 1))
                    n = 0
                    while True:
                        k = ob["from"] + n * every
                        if k > ob["to"] + _TIME_EPS:
                            break
                        s = _r(self.audio_offset + k * 60.0 / self.bpm - st.global_start / self.fps)
                        wins.append((s, s + length, f"temps {_fr(k)}"))
                        n += 1
                except _Skip:
                    pass
        clipped = []
        for s, e, label in wins:
            s2, e2 = max(0.0, s), min(e, st.duration)
            if e2 - s2 <= _TIME_EPS:
                if label.startswith("windows"):
                    self.warn(gp + ("windows",), f"fenêtre {label} [{_fr(s)} s → {_fr(e)} s] hors du plan (0 → {_fr(st.duration)} s) : ignorée.")
                continue
            clipped.append((s2, e2))
        clipped.sort()
        merged: list[list[float]] = []
        for s, e in clipped:
            if merged and s <= merged[-1][1] + _TIME_EPS:
                merged[-1][1] = max(merged[-1][1], e)
            else:
                merged.append([s, e])
        if not merged:
            self.warn(gp, "aucune fenêtre de glitch ne tombe dans ce plan : l'effet sera invisible.")
        layers = g.get("layers")
        if layers is not None:
            unknown = [x for x in layers if x not in layer_ids]
            if unknown:
                self.err(gp + ("layers",), f"calque(s) inconnu(s) {_names(unknown)} ; calques de ce plan : {_names(layer_ids)}.")
            layers = list(layers)
        colors = []
        for c, value in enumerate(g.get("colors") or ["primary", "secondary"]):
            colors.append(self.color(value, gp + (("colors", c) if "colors" in g else ())))
        return {
            "windows": [[_r(s), _r(e - s)] for s, e in merged],
            "amount": float(g.get("amount", _GLITCH_AMOUNT_DEFAULT)),
            "colors": colors,
            "mode": g.get("mode", "both"),
            "layers": layers,
        }

    # -- calques ---------------------------------------------------------------------------------

    def _boil(self, value: Any, where) -> dict | None:
        # Test d'identité et non de vérité : un objet vide {} est une fusion (sans surcharge) sur le boil
        # du preset (CONTRACT §3.8), pas une désactivation.
        if value is None or value is False:
            return None
        out = {**_BOIL_DEFAULT, **(self.preset["textures"].get("boil") or {})}
        if isinstance(value, dict):
            out.update(value)
        return {"amount": float(out["amount"]), "freq": float(out["freq"]), "step": int(out["step"])}

    def _compile_layer(self, st: _ShotTiming, lp: tuple, layer: dict, plate_ids: list[str]) -> dict:
        typ = layer["type"]
        anchor = layer.get("anchor", "center")
        cl: dict[str, Any] = {
            "id": layer["id"],
            "type": typ,
            "x": self.position(layer.get("x", "50%"), "x", lp + ("x",)),
            "y": self.position(layer.get("y", "50%"), "y", lp + ("y",)),
            "anchor": list(_ANCHORS[anchor]) if isinstance(anchor, str) else [float(anchor[0]), float(anchor[1])],
            "z": int(layer.get("z", 0)),
            "rotation": float(layer.get("rotation", 0)),
            "scale": float(layer.get("scale", 1)),
            "opacity": float(layer.get("opacity", 1)),
            "blend": layer.get("blend", "normal"),
            "safe": bool(layer.get("safe", True)),
            "boil": self._boil(layer.get("boil"), lp + ("boil",)),
        }
        if typ == "text":
            self._text_fields(lp, layer, cl)
        elif typ == "shape":
            self._shape_fields(lp, layer, cl)
        elif typ == "image":
            self._image_fields(lp, layer, cl)
        elif typ == "sequence":
            if layer["plate"] not in plate_ids:
                self.fail(lp + ("plate",), f"plaque « {layer['plate']} » inexistante dans ce plan{_suggest(layer['plate'], plate_ids)} ; "
                                           f"plaques déclarées dans shots[].plates : {_names(plate_ids)}.")
            cl.update({"plate": layer["plate"], "w": float(layer.get("w", self.canvas_w)), "h": float(layer.get("h", self.canvas_h))})
        elif typ == "chart":
            self._chart_fields(st, lp, layer, cl)
        tweens, initial, targets = self._compile_tweens(st, lp, layer, cl)
        cl["initial"] = initial
        cl["tweens"] = tweens
        if typ == "text":
            req = layer.get("split", "none")
            # split = niveaux demandés par layer.split OU ciblés par une tween (CONTRACT §3.5).
            cl["split"] = {lvl: (req == lvl or lvl in targets) for lvl in _SPLIT_TARGETS}
        return cl

    def _text_fields(self, lp: tuple, layer: dict, cl: dict) -> None:
        style = layer.get("style", "display")
        font, case, ok = self.font_block(style, layer, lp)
        text = _apply_case(layer["text"], case)
        if ok:
            self.check_glyphs(text, font["family"], font["weight"], lp + ("text",))
        mw = layer.get("maxWidth")
        cl.update({
            "text": text,
            "font": font,
            "align": layer.get("align", "left"),
            # null => white-space: pre côté runtime (jamais de retour à la ligne automatique parasite).
            "max_width": float(mw) if mw is not None else None,
        })

    def _shape_fields(self, lp: tuple, layer: dict, cl: dict) -> None:
        shape = layer["shape"]
        w, h = layer.get("w"), layer.get("h")
        points, d = layer.get("points"), layer.get("d")
        if shape in ("rect", "ellipse", "path") and (w is None or h is None):
            self.fail(lp, f"une forme « {shape} » exige « w » et « h » (px logiques) ; ajoutez-les.")
        if shape == "path" and not d:
            self.fail(lp, "une forme « path » exige « d » (données de tracé SVG dans le repère 0..w × 0..h).")
        if shape == "polygon" and (not points or len(points) < 3):
            self.fail(lp + (("points",) if points else ()), "un « polygon » exige au moins 3 sommets dans « points » ([[x, y], …]).")
        if shape == "line" and not points:
            if w is None:
                self.fail(lp, "une « line » exige « points » ([[x0, y0], [x1, y1]]) ou au moins « w » (ligne horizontale).")
            # Ligne horizontale par défaut au milieu de la boîte : cas d'usage du soulignement.
            hh = float(h or 0)
            points = [[0.0, hh / 2], [float(w), hh / 2]]
        if shape in ("line", "polygon"):
            if w is None:
                w = max(1.0, max(float(p[0]) for p in points))
            if h is None:
                h = max(1.0, max(float(p[1]) for p in points))
        if points is not None and shape not in ("line", "polygon"):
            self.warn(lp + ("points",), f"« points » est ignoré pour une forme « {shape} ».")
            points = None
        if d is not None and shape != "path":
            self.warn(lp + ("d",), f"« d » est ignoré pour une forme « {shape} ».")
            d = None
        fill = self.paint(layer.get("fill", "none" if shape == "line" else "primary"), lp + (("fill",) if "fill" in layer else ()))
        stroke = self.paint(layer.get("stroke", "fg" if shape == "line" else "none"), lp + (("stroke",) if "stroke" in layer else ()))
        # Trait par défaut de 4 px s'il y a une couleur de trait : visible en 1080p sans épaissir le dessin.
        sw = float(layer.get("strokeWidth", 4 if stroke != "none" else 0))
        cl.update({
            "shape": shape, "w": float(w), "h": float(h), "radius": float(layer.get("radius", 0)),
            "fill": fill, "stroke": stroke, "stroke_width": sw, "linecap": layer.get("linecap", "round"),
            "d": d, "points": [[float(x), float(y)] for x, y in points] if points is not None else None,
        })

    def _image_fields(self, lp: tuple, layer: dict, cl: dict) -> None:
        src = layer["src"]
        path = (config.ROOT / src).resolve()
        # Seul assets/ est servi pour les images : un chemin ailleurs (ou « ../ ») serait bloqué par la route.
        if not path.is_relative_to(config.ASSETS_DIR.resolve()):
            self.fail(lp + ("src",), f"l'image {_q(src)} doit se trouver sous assets/ (seul dossier d'images servi au "
                                     "navigateur) : déplacez-la et donnez un chemin relatif à la racine, ex. « assets/images/logo.png ».")
        if path.suffix.lower() not in config.MIME_TYPES or not config.MIME_TYPES[path.suffix.lower()].startswith("image/"):
            self.fail(lp + ("src",), f"format d'image « {path.suffix} » non servi ; formats acceptés : "
                                     + ", ".join(e for e, t in config.MIME_TYPES.items() if t.startswith("image/")) + ".")
        if not path.is_file():
            self.fail(lp + ("src",), f"image introuvable : {path}. Vérifiez le chemin (relatif à la racine du projet).")
        cl.update({
            "src": _served_url(path),
            "w": float(layer["w"]), "h": float(layer["h"]), "fit": layer.get("fit", "cover"),
        })

    def _chart_fields(self, st: _ShotTiming, lp: tuple, layer: dict, cl: dict) -> None:
        data = layer["data"]
        colors_in = layer.get("colors")
        cycle = ["primary", "secondary", "accent"]
        label_font, label_case, label_ok = self.font_block(
            (layer.get("labelStyle") or {}).get("style", "body"), layer.get("labelStyle") or {}, lp + ("labelStyle",))
        value_font, _value_case, value_ok = self.font_block(
            (layer.get("valueStyle") or {}).get("style", "body"), layer.get("valueStyle") or {}, lp + ("valueStyle",))
        out_data = []
        for n, item in enumerate(data):
            if "color" in item:
                c = self.color(item["color"], lp + ("data", n, "color"))
            elif colors_in:
                c = self.color(colors_in[n % len(colors_in)], lp + ("colors", n % len(colors_in)))
            else:
                c = self.palette[cycle[n % 3]]
            out_data.append({"label": _apply_case(item["label"], label_case), "value": float(item["value"]), "color": c})
        values = [d["value"] for d in out_data]
        vmax = float(layer["max"]) if "max" in layer else max(values)
        if vmax <= 0:
            self.warn(lp + ("data",), "toutes les valeurs sont nulles : pleine échelle fixée à 1 (barres vides).")
            vmax = 1.0
        if any(v > vmax for v in values):
            self.warn(lp + ("max",), f"au moins une valeur dépasse max = {_fr(vmax)} : sa barre sera plus haute que le cadre du graphique.")
        vf = {"decimals": 0, "prefix": "", "suffix": "", **(layer.get("valueFormat") or {})}
        if label_ok:
            self.check_glyphs("".join(d["label"] for d in out_data), label_font["family"], label_font["weight"], lp + ("data",))
        if value_ok:
            # Glyphes des compteurs : chiffres, préfixe/suffixe, virgule décimale et espace fine insécable
            # (séparateur de milliers de Intl.NumberFormat('fr-FR')).
            glyphs = vf["prefix"] + vf["suffix"] + "0123456789" + ("," if vf["decimals"] else "") + ("  " if vmax >= 1000 else "")
            self.check_glyphs(glyphs, value_font["family"], value_font["weight"], lp + ("valueFormat",))
        orientation = layer.get("orientation", "vertical")
        span = float(layer["w"] if orientation == "vertical" else layer["h"])
        g = layer.get("grow") or {}
        gp = lp + ("grow",)
        start = self.time_ref(g.get("at", 0), gp + (("at",) if "at" in g else ()), st)
        dur = self.duration_ref(g["dur"], gp + ("dur",)) if "dur" in g else float(self.durations["long"])
        stagger = float(g.get("stagger", self.preset["motion"]["stagger"]))
        if "ease" in g:
            try:
                info = _resolve_ease(g["ease"], self.preset, allow_linear=False)
            except _EaseError as exc:
                self.fail(gp + ("ease",), str(exc))
            if _ease_overshoots(info):
                shown = _q(g["ease"]) + (f" (→ « {info['text']} »)" if info["text"] != g["ease"] else "")
                self.fail(gp + ("ease",), f"easing {shown} dépasse 1 : la barre et son compteur "
                                          "afficheraient une valeur fausse pendant le dépassement. Utilisez un easing sans "
                                          "dépassement (ex. « power3.out », « expo.out », alias « default ») ; back, elastic et "
                                          "les customEases qui sortent de [0, 1] sont refusés pour grow.")
        else:
            info = None
            for cand in ("enter", "default", "power2.out"):
                try:
                    c_info = _resolve_ease(cand, self.preset, allow_linear=False)
                except _EaseError:
                    continue
                if not _ease_overshoots(c_info):
                    info = c_info
                    break
            if info is None:  # impossible : « power2.out » ne dépasse jamais
                self.fail(gp, "aucun easing sans dépassement disponible pour grow ; précisez grow.ease.")
            if info["via"][:1] != ["enter"]:
                self.warn(gp, f"l'alias « enter » du preset dépasse 1 : grow utilise « {info['text']} » à la place.")
        if start < 0:
            self.err(gp + ("at",), f"la croissance démarre à {_fr(start)} s, avant le début du plan : retardez « at ».")
        if start + dur > st.duration + _TIME_EPS:
            self.warn(gp, f"la croissance finit à {_fr(start + dur)} s, après la fin du plan ({_fr(st.duration)} s) : fin hors champ.")
        cl.update({
            "w": float(layer["w"]), "h": float(layer["h"]), "orientation": orientation,
            "data": out_data, "max": vmax,
            "gap": float(layer["gap"]) if "gap" in layer else _r(_CHART_GAP_FRACTION * span / len(out_data)),
            "bar_radius": float(layer.get("barRadius", 0)),
            "value_format": {"decimals": int(vf["decimals"]), "prefix": vf["prefix"], "suffix": vf["suffix"]},
            "label_font": label_font, "value_font": value_font,
            "grow": {"start": _r(start), "dur": _r(dur), "stagger": _r(stagger), "ease": info["text"]},
        })

    # -- tweens ----------------------------------------------------------------------------------

    def norm_props(self, props: dict, where) -> dict:
        """scale -> scaleX + scaleY, couleurs -> hexadécimal (CONTRACT §3.6.7)."""
        out: dict[str, Any] = {}
        for p, v in props.items():
            if p == "scale":
                continue
            if p in ("color", "fill", "stroke"):
                out[p] = self.color(v, where + (p,) if isinstance(where, tuple) else f"{where}.{p}")
            else:
                out[p] = v
        if "scale" in props:
            # Une valeur explicite de scaleX / scaleY l'emporte sur scale.
            out.setdefault("scaleX", props["scale"])
            out.setdefault("scaleY", props["scale"])
        return out

    def _base_value(self, cl: dict, target: str, prop: str) -> Any:
        """Valeur de repos d'une propriété (CONTRACT §3.6.8). Pour les sous-cibles (chars, words, lines,
        shape, bars), les transformations du calque s'appliquent déjà au conteneur : repos = identité."""
        own = target == "self"
        if prop in ("x", "y", "skewX", "skewY", "blur", "reveal"):
            return 0
        if prop in ("scaleX", "scaleY"):
            return cl["scale"] if own else 1
        if prop == "rotation":
            return cl["rotation"] if own else 0
        if prop == "opacity":
            return cl["opacity"] if own else 1
        if prop == "draw":
            return 1
        if prop == "color":
            return (cl.get("font") or {}).get("color")
        if prop in ("fill", "stroke"):
            v = cl.get(prop)
            return None if v in (None, "none") else v
        return None

    def _expand_tween(self, st: _ShotTiming, lp: tuple, k: int, tw: dict) -> list[dict]:
        """Développe une tween (move du preset ou tween simple) en étapes datées (CONTRACT §3.6.1-5)."""
        P = lp + ("anim", k)
        at = self.time_ref(tw.get("at", 0), P + (("at",) if "at" in tw else ()), st)
        allow_linear = bool(tw.get("allowLinear", False))
        tw_from = self.norm_props(tw.get("from") or {}, P + ("from",))
        tw_to = self.norm_props(tw.get("to") or {}, P + ("to",))
        tw_ease = None
        if "ease" in tw:
            try:
                tw_ease = _resolve_ease(tw["ease"], self.preset, allow_linear=allow_linear)
            except _EaseError as exc:
                self.fail(P + ("ease",), str(exc))

        if "move" in tw:
            name = tw["move"]
            moves = self.preset["motion"]["moves"]
            if name not in moves:
                self.fail(P + ("move",), f"mouvement « {name} » inconnu du preset « {self.preset['id']} »{_suggest(name, moves)} ; "
                                         f"mouvements disponibles : {', '.join(moves)}.")
            raw_steps = moves[name]
            steps = []
            for n, s in enumerate(raw_steps):
                d = s["dur"]
                steps.append({
                    "n": n,
                    "label": f"anim[{k}] (move « {name} »" + (f", étape {n})" if len(raw_steps) > 1 else ")"),
                    "offset": float(s.get("offset", 0)),
                    "dur": float(self.durations[d]) if isinstance(d, str) else float(d),
                    "from": self.norm_props(s.get("from") or {}, P + ("move",)),
                    "to": self.norm_props(s.get("to") or {}, P + ("move",)),
                    "ease": s.get("ease"),
                    "stagger": s.get("stagger"),
                    "target": s.get("target", "self"),
                })
            total = max(s["offset"] + s["dur"] for s in steps)
            if "dur" in tw:
                # Remise à l'échelle du move complet : offsets et durées multipliés par k = dur / durée totale.
                kf = self.duration_ref(tw["dur"], P + ("dur",)) / total
                for s in steps:
                    s["offset"] *= kf
                    s["dur"] *= kf
            if "target" in tw:
                for s in steps:
                    s["target"] = tw["target"]
            if "stagger" in tw:
                for s in steps:
                    s["stagger"] = tw["stagger"]
            self._merge_overrides(steps, tw_from, "from")
            self._merge_overrides(steps, tw_to, "to")
        else:
            dur = self.duration_ref(tw["dur"], P + ("dur",)) if "dur" in tw else float(self.durations["medium"])
            steps = [{"n": 0, "label": f"anim[{k}]", "offset": 0.0, "dur": dur, "from": tw_from, "to": tw_to,
                      "ease": None, "stagger": tw.get("stagger"), "target": tw.get("target", "self")}]

        # sync: end -> la FIN de la première étape (déclarée) tombe exactement sur « at » (impact sur le temps).
        delta = -(steps[0]["offset"] + steps[0]["dur"]) if tw.get("sync") == "end" else 0.0
        first_start = _r(at + min(s["offset"] for s in steps) + delta)
        if first_start < -_TIME_EPS:
            # Un seul message pour toute la tween (ses étapes partagent la même cause) ; tween écartée.
            why = ""
            if delta:
                why = (f" : avec \"sync\": \"end\", la fin de la première étape ({_fr(-delta)} s) tombe sur « at » "
                       f"= {_fr(at)} s, le départ est donc {_fr(first_start)} s")
            label = f"anim[{k}]" + (f" (move « {tw['move']} »)" if "move" in tw else "")
            self.fail(P, f"{label} démarre à {_fr(first_start)} s, avant le début du plan{why}. Retardez « at » d'au moins "
                         f"{_fr(-first_start)} s, raccourcissez la tween (« dur »), ou placez-la dans le plan précédent.")
        out = []
        for s in steps:
            if tw_ease is not None:
                info = tw_ease
            else:
                try:
                    info = _resolve_ease(s["ease"] if s["ease"] is not None else "default", self.preset, allow_linear=allow_linear)
                except _EaseError as exc:
                    self.fail(P + ("move",) if "move" in tw else P, str(exc))
            target = s["target"]
            if target in _STAGGER_TARGETS:
                stagger = float(s["stagger"]) if s["stagger"] is not None else float(self.preset["motion"]["stagger"])
            else:
                if "stagger" in tw and tw["stagger"]:
                    self.warn(P + ("stagger",), f"stagger ignoré : la cible « {target} » est un élément unique.")
                stagger = 0.0
            start = _r(at + s["offset"] + delta)
            dur = _r(s["dur"])
            if dur <= 0:
                self.fail(P, f"{s['label']} : durée nulle après remise à l'échelle ; augmentez « dur ».")
            end = _r(start + dur)
            if end > st.duration + _TIME_EPS:
                self.warn(P, f"{s['label']} se termine à {_fr(end)} s, après la fin du plan ({_fr(st.duration)} s) : sa fin sera hors champ.")
            out.append({
                "k": k, "n": s["n"], "loc": P, "label": s["label"], "target": target, "start": max(0.0, start),
                "dur": dur, "end": end, "ease": info["text"], "stagger": _r(stagger),
                "from": dict(s["from"]), "to": dict(s["to"]), "reveal_dir": tw.get("revealDir", "right"),
            })
        return out

    @staticmethod
    def _merge_overrides(steps: list[dict], props: dict, side: str) -> None:
        """Fusionne from/to de la tween dans les étapes du move.

        Adaptation documentée du CONTRACT §3.6.1 : un `from` de la tween va dans la PREMIÈRE étape qui
        anime la clé (là où la propriété commence), un `to` dans la DERNIÈRE (là où elle finit) ; une
        clé qu'aucune étape n'anime va dans la première étape. Pour un move à une étape (cas usuel),
        c'est exactement « chaque étape qui anime la clé » ; pour un move à plusieurs étapes, cela
        évite des sauts (un from réinjecté au milieu d'une chaîne casserait la continuité §3.6.8).
        """
        first_overall = min(steps, key=lambda s: (s["offset"], s["n"]))
        for p, v in props.items():
            animating = [s for s in steps if p in s["from"] or p in s["to"]]
            if not animating:
                first_overall[side][p] = v
            elif side == "from":
                min(animating, key=lambda s: (s["offset"], s["n"]))[side][p] = v
            else:
                max(animating, key=lambda s: (s["offset"] + s["dur"], s["n"]))[side][p] = v

    def _compile_tweens(self, st: _ShotTiming, lp: tuple, layer: dict, cl: dict):
        typ = layer["type"]
        steps: list[dict] = []
        for k, tw in enumerate(layer.get("anim") or []):
            try:
                steps.extend(self._expand_tween(st, lp, k, tw))
            except _Skip:
                continue

        valid = []
        for s in steps:
            tgt = s["target"]
            props = set(s["from"]) | set(s["to"])
            ok = True
            if tgt in _SPLIT_TARGETS and typ != "text":
                self.err(s["loc"], f"{s['label']} : la cible « {tgt} » n'existe que pour un calque text (ce calque est « {typ} ») ; utilisez « self »"
                                   + (" ou « shape »" if typ == "shape" else " ou « bars »" if typ == "chart" else "") + ".")
                ok = False
            elif tgt == "shape" and typ != "shape":
                self.err(s["loc"], f"{s['label']} : la cible « shape » n'existe que pour un calque shape (ce calque est « {typ} »).")
                ok = False
            elif tgt == "bars" and typ != "chart":
                self.err(s["loc"], f"{s['label']} : la cible « bars » n'existe que pour un calque chart (ce calque est « {typ} »).")
                ok = False
            for p in sorted(props & {"draw", "fill", "stroke"}):
                if typ != "shape":
                    self.err(s["loc"], f"{s['label']} : « {p} » ne s'anime que sur un calque shape (ce calque est « {typ} »)"
                                       + (" ; pour la couleur d'un texte, animez « color »." if typ == "text" else "."))
                    ok = False
                elif tgt != "shape":
                    self.err(s["loc"], f"{s['label']} : « {p} » s'anime sur l'élément SVG de la forme : ajoutez \"target\": \"shape\".")
                    ok = False
            if "color" in props and typ != "text":
                self.err(s["loc"], f"{s['label']} : « color » ne s'anime que sur un calque text"
                                   + (" ; pour une forme, animez « fill » ou « stroke » avec \"target\": \"shape\"." if typ == "shape" else "."))
                ok = False
            if "draw" in props and ok and cl.get("stroke") == "none":
                self.warn(s["loc"], f"{s['label']} : « draw » anime le trait mais la forme n'a pas de « stroke » : rien ne sera visible.")
            if ok:
                valid.append(s)

        # Continuité (§3.6.8) : étapes traitées par départ croissant (puis ordre de déclaration).
        valid.sort(key=lambda s: (s["start"], s["k"], s["n"]))
        current: dict[tuple[str, str], Any] = {}
        chains: dict[tuple[str, str], list[dict]] = {}
        for s in valid:
            tgt = s["target"]
            for p in list(s["to"]):
                if p not in s["from"]:
                    cur = current.get((tgt, p), _MISSING)
                    if cur is _MISSING:
                        cur = self._base_value(cl, tgt, p)
                    if cur is None:
                        self.err(s["loc"], f"{s['label']} : valeur de départ de « {p} » impossible à déduire (le calque n'a pas de "
                                           f"{p}, « none ») ; donnez-la explicitement dans from.")
                        cur = s["to"][p]
                    s["from"][p] = cur
            for p in list(s["from"]):
                if p not in s["to"]:
                    s["to"][p] = s["from"][p]
            s["to"] = {p: s["to"][p] for p in s["from"]}
            for p in s["from"]:
                current[(tgt, p)] = s["to"][p]
                chains.setdefault((tgt, p), []).append(s)

        # Chevauchements (§3.6.9) : un seul message par paire d'étapes, toutes propriétés réunies.
        overlaps: dict[tuple[int, int], tuple[dict, dict, list[str]]] = {}
        stagger_warned: set[tuple[int, int]] = set()
        for (tgt, p), chain in chains.items():
            running = chain[0]
            for s in chain[1:]:
                if s["start"] < running["end"] - _TIME_EPS:
                    key = (id(running), id(s))
                    overlaps.setdefault(key, (running, s, []))[2].append(p)
                elif running["stagger"] > 0 and (id(running), id(s)) not in stagger_warned:
                    stagger_warned.add((id(running), id(s)))
                    self.warn(s["loc"], f"{running['label']} a un stagger de {_fr(running['stagger'])} s sur « {tgt} » : ses derniers "
                                        f"éléments peuvent encore s'animer après {_fr(running['end'])} s, quand {s['label']} "
                                        f"(départ {_fr(s['start'])} s) reprend « {p} » ; vérifiez l'espacement.")
                if s["end"] > running["end"]:
                    running = s
        for a, b, props in overlaps.values():
            self.err(b["loc"], f"chevauchement sur {_names(props)} (cible « {b['target']} ») : {a['label']} [{_fr(a['start'])} s → "
                               f"{_fr(a['end'])} s] et {b['label']} [{_fr(b['start'])} s → {_fr(b['end'])} s] animent la même "
                               f"propriété en même temps. Décalez « at » de {b['label']} à {_fr(a['end'])} s ou plus, raccourcissez "
                               "la première, ou animez une autre propriété.")

        initial: dict[str, dict] = {}
        for (tgt, p), chain in chains.items():
            initial.setdefault(tgt, {})[p] = chain[0]["from"][p]
        tweens = [{"target": s["target"], "start": s["start"], "dur": s["dur"], "ease": s["ease"], "stagger": s["stagger"],
                   "from": s["from"], "to": s["to"], "reveal_dir": s["reveal_dir"]} for s in valid]
        targets = {s["target"] for s in steps}
        return tweens, initial, targets

    # -- plaques 3D -> tâches Blender ------------------------------------------------------------

    def _channels(self, st: _ShotTiming, chans: list[dict], allowed: tuple[str, ...], where_base) -> list[dict]:
        out = []
        seen = set()
        for c, ch in enumerate(chans):
            cp = where_base + ("anim", c) if isinstance(where_base, tuple) else f"{where_base}.anim[{c}]"

            def at(*parts):
                return cp + parts if isinstance(cp, tuple) else cp + "".join(f"[{p}]" if isinstance(p, int) else f".{p}" for p in parts)

            prop = ch["prop"]
            if prop not in allowed:
                self.err(at("prop"), f"canal « {prop} » non animable ici ; canaux possibles : {_names(allowed)}.")
                continue
            if prop in seen:
                self.err(at("prop"), f"canal « {prop} » défini deux fois : réunissez toutes ses clés dans un seul canal.")
                continue
            seen.add(prop)
            keys = []
            for q, key in enumerate(ch["keys"]):
                try:
                    t_local = self.time_ref(key["t"], at("keys", q, "t"), st)
                except _Skip:
                    continue
                v = key["v"]
                if isinstance(v, (int, float)):
                    if prop != "scale":
                        self.err(at("keys", q, "v"), f"« {prop} » attend un vecteur [x, y, z] ; un nombre seul n'est admis que pour scale (uniforme).")
                        continue
                    v = [v, v, v]
                keys.append({"q": q, "frame": _r(t_local * self.fps), "value": [float(x) for x in v],
                             "ease": key.get("ease"), "allow_linear": bool(key.get("allowLinear", False))})
            if not keys:
                continue
            # Tri stable par image : l'ordre d'écriture des clés dans la scène n'a pas d'importance.
            keys.sort(key=lambda kk: kk["frame"])
            for a, b in zip(keys, keys[1:]):
                if abs(a["frame"] - b["frame"]) <= _TIME_EPS:
                    self.err(at("keys", b["q"], "t"), f"deux clés à la même image ({_fr(a['frame'])}) : supprimez le doublon ou décalez « t ».")
            if keys[0]["ease"] is not None and len(keys) > 1:
                self.warn(at("keys", keys[0]["q"], "ease"), "ease de la première clé ignoré : aucun segment n'arrive sur elle.")
            compiled_keys = []
            for idx, key in enumerate(keys):
                seg = None
                if idx + 1 < len(keys):
                    nxt = keys[idx + 1]
                    try:
                        # L'ease d'une clé décrit le segment qui ARRIVE sur elle ; Blender lit l'interpolation
                        # sur la clé de DÉPART : on l'écrit donc dans `out` de la clé précédente.
                        info = _resolve_ease(nxt["ease"] if nxt["ease"] is not None else "default", self.preset,
                                             allow_linear=nxt["allow_linear"])
                        seg = _blender_segment(info, key["frame"], nxt["frame"])
                    except _EaseError as exc:
                        self.err(at("keys", nxt["q"], "ease"), str(exc))
                        seg = {"interpolation": "BEZIER", "easing": "AUTO"}
                compiled_keys.append({"frame": key["frame"], "value": key["value"], "out": seg})
            out.append({"prop": prop, "keys": compiled_keys})
        return out

    def _material(self, mat: Any, obj_id: str, where) -> dict:
        blender = self.preset.get("blender") or {}
        if mat is None:
            name, src = f"{obj_id}_mat", {}
        elif isinstance(mat, str):
            mats = blender.get("materials") or {}
            if mat not in mats:
                self.fail(where, f"matériau « {mat} » inconnu du preset « {self.preset['id']} »{_suggest(mat, mats)} ; matériaux "
                                 f"disponibles : {_names(mats)}. Utilisez l'un d'eux ou définissez le matériau en ligne "
                                 "({\"base_color\": \"#RRGGBB\", \"roughness\": 0.5, …}).")
            name, src = mat, mats[mat]
        else:
            name, src = f"{obj_id}_mat", mat
        m = {**_MATERIAL_DEFAULTS, "base_color": "primary", **src}
        out = {"name": name}
        for key in ("base_color", "roughness", "metallic", "specular", "sheen", "sheen_roughness", "coat", "coat_roughness",
                    "subsurface", "transmission", "ior", "anisotropic", "emission", "emission_strength"):
            if key in ("base_color", "emission"):
                # Couleur écrite en ligne dans la scène : on pointe sa clé exacte ; sinon le matériau fautif.
                cw = where + (key,) if isinstance(mat, dict) and key in mat and isinstance(where, tuple) else where
                out[key] = self.color(m[key], cw, rgb_only=True)
            else:
                out[key] = float(m[key])
        return out

    def _compile_plate(self, st: _ShotTiming, pp: tuple, shot: dict, plate: dict, layers: list[dict]) -> dict:
        pid = plate["id"]
        blender = self.preset.get("blender") or {}
        preset_label = f"preset « {self.preset['id']} » → blender"

        # Taille : le plus grand calque « sequence » qui affiche la plaque, × dsf × plate_scale (§3.10).
        seq = [layer for layer in layers if layer["type"] == "sequence" and layer.get("plate") == pid]
        if seq:
            w, h = max(((float(s.get("w", self.canvas_w)), float(s.get("h", self.canvas_h))) for s in seq),
                       key=lambda wh: wh[0] * wh[1])
        else:
            self.warn(pp, f"aucun calque « sequence » n'affiche la plaque « {pid} » : rendue à la taille du canevas, mais "
                          "invisible dans la vidéo ; ajoutez {\"type\": \"sequence\", \"plate\": \"" + pid + "\"}.")
            w, h = float(self.canvas_w), float(self.canvas_h)
        width = max(16, _even_up(w * self.dsf * self.plate_scale))
        height = max(16, _even_up(h * self.dsf * self.plate_scale))

        q = self.quality
        render = {
            "device": q.get("blender_device") or blender.get("device") or "auto",
            "samples": int(q.get("blender_samples") or blender.get("samples") or _SAMPLES_DEFAULT),
            "adaptive_threshold": float(blender.get("adaptive_threshold", _RENDER_DEFAULTS["adaptive_threshold"])),
            "denoise": bool(blender.get("denoise", _RENDER_DEFAULTS["denoise"])),
            "view_transform": blender.get("view_transform", _RENDER_DEFAULTS["view_transform"]),
            "look": blender.get("look", _RENDER_DEFAULTS["look"]),
            "exposure": float(blender.get("exposure", _RENDER_DEFAULTS["exposure"])),
            "bounces": {k: int(v) for k, v in {**_BOUNCES_DEFAULT, **(blender.get("bounces") or {})}.items()},
            "caustics": bool(blender.get("caustics", _RENDER_DEFAULTS["caustics"])),
            "motion_blur_shutter": float(blender.get("motion_blur_shutter", _RENDER_DEFAULTS["motion_blur_shutter"])),
        }

        # Monde : plate.world fusionné sur preset.blender.world ; sky true = réglages ciel du preset.
        pw = blender.get("world") or {}
        world_in = {**pw, **(plate.get("world") or {})}
        wp = pp + (("world",) if "world" in plate else ())
        sky_in = world_in.get("sky", False)
        preset_sky = pw.get("sky") if isinstance(pw.get("sky"), dict) else {}
        if sky_in is True:
            sky = {**_SKY_DEFAULTS, **preset_sky}
        elif isinstance(sky_in, dict):
            sky = {**_SKY_DEFAULTS, **preset_sky, **sky_in}
        else:
            sky = None
        world = {
            "color": self.color(world_in.get("color", "bg"), wp + (("color",) if "color" in (plate.get("world") or {}) else ()), rgb_only=True),
            "strength": float(world_in.get("strength", 1.0)),
            "sky": {k: float(v) for k, v in sky.items()} if sky is not None else None,
        }

        cam = plate["camera"]
        cp = pp + ("camera",)
        camera = {
            "location": [float(x) for x in cam["location"]],
            "target": [float(x) for x in cam["target"]],
            "lens": float(cam.get("lens", blender.get("lens", _LENS_DEFAULT))),
            "fstop": (cam["fstop"] if "fstop" in cam else blender.get("fstop")),
            "sensor_width": _SENSOR_WIDTH_MM,
            "channels": self._channels(st, cam.get("anim") or [], ("location", "target"), cp),
        }
        if camera["fstop"] is not None:
            camera["fstop"] = float(camera["fstop"])

        if "lights" in plate:
            lights_in = [(n, light, pp + ("lights", n)) for n, light in enumerate(plate["lights"])]
        else:
            lights_in = [(n, light, f"{preset_label}.light_rig[{n}]") for n, light in enumerate(blender.get("light_rig") or [])]
        if not lights_in:
            self.warn(pp, "aucune lumière (ni plate.lights, ni blender.light_rig du preset) : seul le monde éclairera la scène.")
        lights, light_ids = [], set()
        for n, light, lw in lights_in:
            typ = light["type"]
            lid = light.get("id", f"light_{n}")
            if lid in light_ids:
                self.err(lw, f"identifiant de lumière « {lid} » en double ; donnez un « id » unique.")
            light_ids.add(lid)
            if "location" in light:
                location = [float(x) for x in light["location"]]
            elif typ == "SUN":
                location = list(_SUN_LOCATION_DEFAULT)
            else:
                self.err(lw, f"une lumière {typ} doit avoir une position « location » [x, y, z].")
                continue
            size = float(light.get("size", _LIGHT_DEFAULTS["size"]))
            try:
                color = self.color(light.get("color", _LIGHT_DEFAULTS["color"]), (lw + ("color",)) if isinstance(lw, tuple) else f"{lw}.color", rgb_only=True)
            except _Skip:
                continue
            lights.append({
                "id": lid, "type": typ, "shape": light.get("shape", _LIGHT_DEFAULTS["shape"]),
                "size": size, "size_y": float(light.get("size_y", size)),
                "energy": float(light.get("energy", _LIGHT_DEFAULTS["energy"])), "color": color,
                "location": location, "target": [float(x) for x in light.get("target", _LIGHT_DEFAULTS["target"])],
                "spot_size": float(light.get("spot_size", _LIGHT_DEFAULTS["spot_size"])),
                "angle": float(light.get("angle", _LIGHT_DEFAULTS["angle"])),
                "channels": self._channels(st, light.get("anim") or [], ("location", "target"), lw),
            })

        objects, obj_ids = [], set()
        for o, obj in enumerate(plate["objects"]):
            op = pp + ("objects", o)
            if obj["id"] in obj_ids:
                self.err(op + ("id",), f"identifiant d'objet « {obj['id']} » en double dans la plaque ; donnez un id unique.")
            obj_ids.add(obj["id"])
            prim = obj["primitive"]
            size = float(obj.get("size", 1))
            scale = obj.get("scale", 1)
            try:
                material = self._material(obj.get("material"), obj["id"], op + (("material",) if "material" in obj else ()))
            except _Skip:
                continue
            objects.append({
                "id": obj["id"], "primitive": prim, "size": size,
                "minor": float(obj.get("minor", size * _MINOR_FACTOR.get(prim, _MINOR_FACTOR_OTHER))),
                "bevel": float(obj.get("bevel", size * _BEVEL_FACTOR.get(prim, 0.0))),
                "location": [float(x) for x in obj.get("location", [0, 0, 0])],
                "rotation": [float(x) for x in obj.get("rotation", [0, 0, 0])],
                "scale": [float(scale)] * 3 if isinstance(scale, (int, float)) else [float(x) for x in scale],
                "material": material,
                "channels": self._channels(st, obj.get("anim") or [], ("location", "rotation", "scale"), op),
            })

        seed = int(hashlib.sha256(f"{self.seed}:{shot['id']}:{pid}".encode("utf-8")).hexdigest()[:8], 16) & 0x7FFFFFFF
        return {
            "contract": CONTRACT_JOB,
            "scene_id": self.scene_id,
            "shot_id": shot["id"],
            "plate_id": pid,
            "out_dir": str(config.plate_dir(self.scene_id, shot["id"], pid)),
            "frames": st.frames,
            "fps": self.fps,
            "width": width,
            "height": height,
            "transparent": bool(plate.get("transparent", False) or self.alpha),
            "seed": seed,
            "render": render,
            "world": world,
            "camera": camera,
            "lights": lights,
            "objects": objects,
        }

    # -- livrables, audio, storyboard ------------------------------------------------------------

    def _outputs(self) -> list[dict]:
        outs = []
        profiles = [o["profile"] for o in self.raw["outputs"]]
        if self.alpha and "prores_4444" not in profiles:
            self.err(("outputs",), "format.alpha exige un livrable « prores_4444 » (seul profil qui conserve l'alpha) : "
                                   "ajoutez {\"profile\": \"prores_4444\"} à outputs (les autres profils seront aplatis sur noir).")
        paths: dict[str, int] = {}
        for n, o in enumerate(self.raw["outputs"]):
            prof = o["profile"]
            if prof == "prores_4444" and not self.alpha:
                self.err(("outputs", n, "profile"), "prores_4444 sans format.alpha : ce profil ne sert qu'aux livrables à couche "
                                                   "alpha. Utilisez « prores_422hq », ou activez \"alpha\": true dans format.")
            suffix = o.get("suffix", "")
            path = config.scene_out_dir(self.scene_id) / f"{self.scene_id}{suffix}_{prof}{config.PROFILES[prof]['ext']}"
            if str(path) in paths:
                self.err(("outputs", n), f"même fichier de sortie que outputs[{paths[str(path)]}] ({path.name}) : changez « suffix ».")
            paths[str(path)] = n
            if prof == "hevc_main10":
                crf = int(o.get("crf", config.HEVC_CRF_DEFAULT))
            else:
                if "crf" in o:
                    self.warn(("outputs", n, "crf"), "crf ne concerne que hevc_main10 : ignoré pour ProRes (débit fixé par le profil).")
                crf = None
            outs.append({"profile": prof, "crf": crf, "suffix": suffix, "path": str(path),
                         "alpha_flatten": bool(self.alpha and prof != "prores_4444")})
        return outs

    def _audio(self) -> dict | None:
        audio = self.raw.get("audio")
        if not audio:
            return None
        synth = audio.get("synth")
        src = audio.get("src")
        if synth and self.bpm is None:
            self.err(("audio", "synth"), "la piste de test (synth) est rythmée : ajoutez audio.bpm.")
        if not src and not synth:
            # Bloc « tempo seul » (bpm / offset / marqueurs sans piste) : il ne sert qu'à résoudre les temps
            # beat: / marker:, déjà convertis en secondes locales ci-dessus. Le contrat (§4) n'admet pas de
            # src nul et l'aval (audio, encode, qc) attend une vraie piste dès que audio est non nul : on émet
            # donc audio = null (livrables muets), signalé par un avertissement.
            self.warn(("audio",), "bloc audio sans « src » ni « synth » : bpm et marqueurs servent seulement à caler "
                                  "l'animation, les livrables seront MUETS. Pour une bande-son, ajoutez \"src\": "
                                  "\"assets/audio/<piste>.wav\" ou \"synth\": {\"kind\": \"beat\"} (piste de test générée).")
            return None
        if src:
            path = (config.ROOT / src).resolve()
        else:
            # Piste de test sans chemin : générée dans assets/audio/ sous le nom de la scène.
            path = (config.AUDIO_DIR / f"{self.scene_id}_beat.wav").resolve()
        if not path.is_file() and not synth:
            self.err(("audio", "src"), f"fichier audio introuvable : {path}. Placez-le, corrigez le chemin (relatif à la racine) "
                                       "ou ajoutez \"synth\": {\"kind\": \"beat\"} pour générer une piste de test.")
        target = float(synth["target_lufs"]) if synth and "target_lufs" in synth else self.qc["loudness_target_lufs"]
        return {
            "src": str(path),
            "bpm": self.bpm,
            "offset": self.audio_offset,
            "beats_per_bar": int(audio.get("beats_per_bar", 4)),
            "markers": [{"id": m["id"], "time": float(m["time"])} for m in audio.get("markers") or []],
            "synth": {"kind": synth["kind"], "target_lufs": target} if synth else None,
            "target_lufs": target,
        }

    def _storyboard(self) -> None:
        ids = [s["id"] for s in self.raw["shots"]]
        for n, entry in enumerate(self.raw.get("storyboard") or []):
            if entry["shot"] not in ids:
                self.warn(("storyboard", n, "shot"), f"plan « {entry['shot']} » inconnu{_suggest(entry['shot'], ids)} (plans : {_names(ids)}).")

    # -- assertion interne -----------------------------------------------------------------------

    def _assert_contract(self, compiled: dict) -> None:
        """Le compilé doit respecter les schémas internes : sinon c'est un bogue du compilateur."""
        validators = _schemas()["validators"]
        problems = []
        for shot in compiled["shots"]:
            for e in sorted(validators["shot"].iter_errors(shot["spec"]), key=_path_sort_key):
                problems.append(f"plan « {shot['id']} » → {_fmt_path(e.absolute_path)} : {e.message}")
            for job in shot["plates"]:
                for e in sorted(validators["job"].iter_errors(job), key=_path_sort_key):
                    problems.append(f"plaque « {shot['id']}/{job['plate_id']} » → {_fmt_path(e.absolute_path)} : {e.message}")
        try:
            json.dumps(compiled, allow_nan=False)
        except (TypeError, ValueError) as exc:
            problems.append(f"compilé non sérialisable en JSON strict : {exc}")
        if problems:
            raise SceneError([f"erreur interne du compilateur (sortie non conforme à schema/internal) : {p}. "
                              "Signalez-la avec la scène en cause." for p in problems])


# ---------------------------------------------------------------------------------------------
# API publique
# ---------------------------------------------------------------------------------------------


def validate_scene(raw: dict) -> None:
    """Lève SceneError si la scène est invalide (schéma + toutes les règles sémantiques)."""
    # La validation EST la compilation (sans écriture) : une seule implémentation des règles, donc
    # aucune scène « valide » qui échouerait ensuite à la compilation.
    _Compiler(copy.deepcopy(raw), None).run()


def compile_scene(source: str | os.PathLike | dict) -> dict:
    """Compile une scène (chemin de fichier JSON ou dict déjà chargé) ; SceneError si refusée."""
    if isinstance(source, dict):
        raw, src = copy.deepcopy(source), None
    elif isinstance(source, (str, os.PathLike)):
        path = Path(source)
        raw, src = _read_json(path, "scène"), str(path.resolve())
    else:
        raise SceneError(f"source de scène invalide ({type(source).__name__}) : donnez le chemin d'un fichier .json "
                         "ou un dict déjà chargé.")
    return _Compiler(raw, src).run()


def canonical_json(obj: Any) -> bytes:
    """Forme canonique (clés triées, sans espaces, UTF-8) : base des empreintes SHA-256."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def sha256_json(obj: Any) -> str:
    return hashlib.sha256(canonical_json(obj)).hexdigest()


def write_compiled(compiled: dict) -> Path:
    """Écrit build/<scène>/compiled.json (écriture atomique) et renvoie son chemin."""
    path = config.scene_build_dir(compiled["scene_id"]) / "compiled.json"
    # Indentation + clés triées : fichier lisible et diff stable entre deux compilations.
    _atomic_write_text(path, json.dumps(compiled, indent=2, sort_keys=True, ensure_ascii=False) + "\n")
    return path


def summary(compiled: dict) -> str:
    """Résumé humain : format, sortie, images, plans, fondus, plaques, audio, livrables, avertissements."""
    fmt = compiled["format"]
    fps = fmt["fps"]
    mb = compiled["motion_blur"]
    lines = [
        f"Scène « {compiled['scene_id']} » — {compiled['title']}",
        f"  Preset      : {compiled['preset_id']}",
        f"  Format      : {fmt['aspect']} · {fmt['resolution']} · {fps} i/s · alpha {'oui' if fmt['alpha'] else 'non'}",
        f"  Sortie      : {compiled['output']['width']}×{compiled['output']['height']} px (canevas logique "
        f"{compiled['canvas']['width']}×{compiled['canvas']['height']}, facteur {compiled['device_scale_factor']})",
        f"  Images      : {compiled['frames']} images = {_fr(compiled['duration'], min_dec=2)} s · frame_step {compiled['frame_step']} · "
        f"flou de mouvement {'oui (' + str(mb['samples']) + ' sous-images, obturateur ' + _fr(mb['shutter']) + ')' if mb['enabled'] else 'non'}"
        f" · profondeur {compiled['depth']} bits",
        f"  Plans ({len(compiled['shots'])}) :",
    ]
    for s in compiled["shots"]:
        if s["index"] == 0:
            entry = "début"
        elif s["fade_in_frames"]:
            entry = f"fondu {s['fade_in_frames']} images ({_fr_seconds(s['fade_in_frames'], fps)} s)"
        else:
            entry = "coupe"
        lines.append(f"    [{s['index']}] {s['id']:<18} {s['frames']:>5} images ({_fr_seconds(s['frames'], fps)} s)  "
                     f"maître {s['global_start']:>5} → {s['global_start'] + s['frames'] - 1:<5}  entrée : {entry}")
    fades = [s for s in compiled["shots"] if s["fade_in_frames"]]
    total_frames = sum(s["frames"] for s in compiled["shots"])
    total_fade = sum(s["fade_in_frames"] for s in compiled["shots"])
    lines.append(f"  Fondus      : {len(fades)}" + (f" ({total_fade} images de chevauchement)" if fades else ""))
    lines.append(f"  Total       : {total_frames} images de plans − {total_fade} images de fondus = {compiled['frames']} images")
    plates = [(s["id"], j) for s in compiled["shots"] for j in s["plates"]]
    lines.append(f"  Plaques 3D  : {len(plates)}")
    for sid, j in plates:
        r = j["render"]
        lines.append(f"    {sid}/{j['plate_id']} : {j['width']}×{j['height']} px, {j['frames']} images, Cycles {r['device']} "
                     f"{r['samples']} éch., {r['view_transform']} / {r['look']}, {len(j['objects'])} objet(s), "
                     f"{len(j['lights'])} lumière(s){', transparent' if j['transparent'] else ''}")
    a = compiled["audio"]
    if a:
        marks = ", ".join(f"{m['id']}@{_fr(m['time'])} s" for m in a["markers"]) or "aucun"
        lines.append(f"  Audio       : {a['src']} · {(_fr(a['bpm']) + ' BPM') if a['bpm'] else 'sans tempo'} · "
                     f"offset {_fr(a['offset'])} s · marqueurs : {marks}" + (" · piste de test générée" if a["synth"] else ""))
    else:
        lines.append("  Audio       : aucun")
    lines.append(f"  Livrables ({len(compiled['outputs'])}) :")
    for o in compiled["outputs"]:
        extra = f" crf {o['crf']}" if o["crf"] is not None else ""
        lines.append(f"    {o['profile']:<13}{extra}{' (aplati sur noir)' if o['alpha_flatten'] else ''} → {o['path']}")
    if compiled["warnings"]:
        lines.append(f"  Avertissements ({len(compiled['warnings'])}) :")
        lines += [f"    - {w}" for w in compiled["warnings"]]
    else:
        lines.append("  Avertissements : aucun")
    return "\n".join(lines)


def _main(argv: list[str]) -> int:
    if len(argv) != 1:
        print("usage : python -m pipeline.scene <scène.json>", file=sys.stderr)
        return 2
    try:
        compiled = compile_scene(argv[0])
    except SceneError as exc:
        print("SCÈNE REFUSÉE :", file=sys.stderr)
        print(str(exc), file=sys.stderr)
        return 1
    print(summary(compiled))
    return 0


if __name__ == "__main__":
    # Sortie redirigée sous Windows = cp1252 : on force l'UTF-8 pour « → », « × » et les accents.
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(_main(sys.argv[1:]))
