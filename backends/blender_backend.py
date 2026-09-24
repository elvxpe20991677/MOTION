"""Backend Blender de mograph : rend une plaque 3D (tâche « mograph-blender-job/1 ») image par image.

Exécuté par l'interpréteur du venv Blender (CPython 3.11 + bpy 5.0.1), jamais par l'orchestrateur :

    .venv-blender/Scripts/python.exe backends/blender_backend.py <job.json> [--frames 0,12,40] [--print-values]

    --frames        sous-ensemble d'images locales à rendre (liste « 0,12,40 », intervalles « 0-10 » admis) ;
                    par défaut toutes les images 0..frames-1.
    --print-values  construit la scène, imprime pour chaque objet animé et chaque image demandée les
                    valeurs évaluées (après frame_set) de location/rotation/scale, puis s'arrête SANS rendre.

Journal (stdout, une ligne par événement, vidée immédiatement pour que le lanceur relaie en direct) :
    MOGRAPH_BLENDER image i/n          image i (numéro local) écrite, n = nombre d'images de la plaque
    MOGRAPH_BLENDER look appliqué : …  look réellement lu dans view_settings après affectation
    MOGRAPH_VALUES {json}              valeurs évaluées (mode --print-values)
    MOGRAPH_BLENDER ERREUR : …         (stderr) erreur ; le code de sortie est alors non nul

Codes de sortie : 0 succès, 2 erreur de tâche (message actionnable), 1 exception inattendue.
"""

# bpy DOIT être importé avant bmesh et mathutils : ces modules sont fournis par bpy (sinon ModuleNotFoundError).
import bpy
import bmesh
import mathutils
from bpy_extras import anim_utils

import argparse
import json
import math
import os
import struct
import sys
import time
import traceback
import warnings
import zlib
from pathlib import Path

# Le backend lit les mêmes constantes que l'orchestrateur (nommage des images, compression PNG) :
# config.py n'importe que la bibliothèque standard, il est donc importable depuis Python 3.11.
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
try:
    from pipeline import config  # noqa: E402
except Exception as exc:  # pragma: no cover - message d'installation
    print(
        f"MOGRAPH_BLENDER ERREUR : impossible d'importer pipeline/config.py depuis {ROOT} ({exc}). "
        "Lancez ce script depuis le dépôt mograph intact (backends/ et pipeline/ côte à côte).",
        file=sys.stderr,
        flush=True,
    )
    sys.exit(2)

CONTRACT = "mograph-blender-job/1"
PREFIX = "MOGRAPH_BLENDER"
VALUES_PREFIX = "MOGRAPH_VALUES"

# Résolution géométrique des primitives courbes : à 4K une sphère de 1000 px de rayon garde une corde
# inférieure à 0,3 px (128 segments), donc aucune facette visible sur la silhouette.
SPHERE_SEGMENTS = (128, 64)
TORUS_SEGMENTS = (128, 48)
ROUND_SEGMENTS = 128
FILLET_SEGMENTS = 24
# Nombre de segments du biseau d'un rounded_cube : imposé par la spécification (arrondi lisse à 4K).
BEVEL_SEGMENTS = 6
# Angle au-delà duquel une arête reste vive (cylindre/cône : jonction flanc/couvercle à 90°).
SHARP_ANGLE_DEG = 30.0
# Tolérance de correspondance entre la frame d'une clé du job et celle relue sur la F-curve.
FRAME_EPS = 1e-3

# Propriétés animables par type d'entité : la caméra et les lumières sont orientées par une contrainte
# TRACK_TO, une rotation animée y serait silencieusement écrasée, on la refuse donc explicitement.
OBJECT_PROPS = ("location", "rotation", "scale")
TRACKED_PROPS = ("location", "target")
DATA_PATHS = {"location": "location", "rotation": "rotation_euler", "scale": "scale", "target": "location"}

# Correspondance champ du job -> noms d'entrées du Principled BSDF, du plus récent au plus ancien :
# les noms ont changé en 4.0 (Sheen -> Sheen Weight, Clearcoat -> Coat Weight, Specular -> Specular IOR Level…).
PRINCIPLED_INPUTS = (
    ("base_color", ("Base Color",), "color"),
    ("roughness", ("Roughness",), "float"),
    ("metallic", ("Metallic",), "float"),
    ("specular", ("Specular IOR Level", "Specular"), "float"),
    ("sheen", ("Sheen Weight", "Sheen"), "float"),
    ("sheen_roughness", ("Sheen Roughness",), "float"),
    ("coat", ("Coat Weight", "Clearcoat"), "float"),
    ("coat_roughness", ("Coat Roughness", "Clearcoat Roughness"), "float"),
    ("subsurface", ("Subsurface Weight", "Subsurface"), "float"),
    ("transmission", ("Transmission Weight", "Transmission"), "float"),
    ("ior", ("IOR",), "float"),
    ("anisotropic", ("Anisotropic",), "float"),
    ("emission", ("Emission Color", "Emission"), "color"),
    ("emission_strength", ("Emission Strength",), "float"),
)

# Modèles de ciel physique par ordre de préférence : « MULTIPLE_SCATTERING » (5.0) remplace « NISHITA »
# (2.90-4.x) ; les modèles analytiques anciens ne servent qu'en dernier recours.
SKY_MODEL_PREFERENCE = ("MULTIPLE_SCATTERING", "NISHITA", "SINGLE_SCATTERING", "HOSEK_WILKIE", "PREETHAM")
# Réglages du ciel : clé du job -> noms possibles de la propriété du nœud Sky Texture selon la version.
SKY_PROPS = (
    ("sun_elevation", ("sun_elevation",), "angle"),
    ("sun_rotation", ("sun_rotation",), "angle"),
    ("altitude", ("altitude",), "float"),
    ("air", ("air_density",), "float"),
    ("dust", ("aerosol_density", "dust_density"), "float"),
    ("ozone", ("ozone_density",), "float"),
    ("sun_size", ("sun_size",), "angle"),
    ("sun_intensity", ("sun_intensity",), "float"),
)


class JobError(Exception):
    """Erreur de tâche : message en français, actionnable, affiché tel quel par le lanceur."""


def log(message: str) -> None:
    # flush=True : le lanceur lit un tube ; sans vidage, la progression arriverait par paquets à la fin.
    print(f"{PREFIX} {message}", flush=True)


# ---------------------------------------------------------------------------------------------------
# Lecture et contrôle de la tâche
# ---------------------------------------------------------------------------------------------------

def load_job(path: Path) -> dict:
    """Lit job.json ; le lanceur l'a déjà validé par jsonschema, on revérifie ici l'essentiel."""
    if not path.is_file():
        raise JobError(f"tâche introuvable : {path}. Passez le chemin d'un job.json écrit par pipeline/blender_render.py.")
    try:
        job = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise JobError(f"tâche illisible {path} : {exc}. Régénérez-la avec pipeline/scene.py.") from exc
    if not isinstance(job, dict) or job.get("contract") != CONTRACT:
        raise JobError(
            f"{path} n'est pas une tâche « {CONTRACT} » (contract = {job.get('contract') if isinstance(job, dict) else None!r}). "
            "Utilisez une tâche produite par le compilateur de scènes."
        )
    required = ("scene_id", "shot_id", "plate_id", "out_dir", "frames", "fps", "width", "height",
                "transparent", "seed", "render", "world", "camera", "lights", "objects")
    missing = [k for k in required if k not in job]
    if missing:
        raise JobError(f"{path} : clés manquantes {missing} ; validez la tâche avec schema/internal/blender_job.schema.json.")
    if not Path(job["out_dir"]).is_absolute():
        raise JobError(f"out_dir doit être un chemin ABSOLU (reçu {job['out_dir']!r}) ; corrigez la tâche.")
    if int(job["frames"]) < 1:
        raise JobError(f"frames doit valoir au moins 1 (reçu {job['frames']}).")
    return job


def parse_frames(spec: str | None, total: int) -> list[int]:
    """« 0,12,40 » ou « 0-10,20 » -> liste triée sans doublon ; None -> toutes les images."""
    if spec is None:
        return list(range(total))
    frames: set[int] = set()
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            if "-" in part[1:]:
                a, b = part.split("-", 1)
                lo, hi = int(a), int(b)
                if hi < lo:
                    raise ValueError
                frames.update(range(lo, hi + 1))
            else:
                frames.add(int(part))
        except ValueError:
            raise JobError(f"--frames : « {part} » n'est ni un entier ni un intervalle a-b croissant (ex. --frames 0,12,40).")
    bad = sorted(f for f in frames if f < 0 or f >= total)
    if bad:
        raise JobError(f"--frames : images {bad} hors de la plaque (images valides : 0 à {total - 1}).")
    if not frames:
        raise JobError("--frames : liste vide ; donnez au moins une image (ex. --frames 0).")
    return sorted(frames)


# ---------------------------------------------------------------------------------------------------
# Couleurs
# ---------------------------------------------------------------------------------------------------

def srgb_to_linear(c: float) -> float:
    # Formule IEC 61966-2-1 exacte : l'espace de travail de Cycles est linéaire Rec.709 (primaires sRGB).
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def hex_to_linear(value: str, where: str) -> tuple[float, float, float]:
    s = value.strip()
    if len(s) != 7 or s[0] != "#":
        raise JobError(f"{where} : couleur {value!r} invalide, attendu #RRGGBB (sRGB).")
    try:
        rgb = [int(s[i:i + 2], 16) / 255.0 for i in (1, 3, 5)]
    except ValueError:
        raise JobError(f"{where} : couleur {value!r} invalide, attendu #RRGGBB (sRGB).")
    return tuple(srgb_to_linear(c) for c in rgb)


# ---------------------------------------------------------------------------------------------------
# Réglages de rendu
# ---------------------------------------------------------------------------------------------------

def configure_render(scene: bpy.types.Scene, job: dict) -> str:
    """Cycles, échantillonnage, flou, format de sortie, gestion couleur. Renvoie le look appliqué."""
    r = job["render"]
    scene.render.engine = "CYCLES"
    cy = scene.cycles

    # Échantillonnage adaptatif : arrête tôt les pixels déjà convergés, même qualité perçue à coût moindre.
    cy.samples = int(r["samples"])
    cy.use_adaptive_sampling = True
    cy.adaptive_threshold = float(r["adaptive_threshold"])

    # OIDN : débruiteur par IA indépendant du GPU, qualité supérieure au débruiteur OptiX sur peu d'échantillons.
    cy.use_denoising = bool(r["denoise"])
    if cy.use_denoising:
        try:
            cy.denoiser = "OPENIMAGEDENOISE"
        except TypeError as exc:
            raise JobError(f"débruiteur OpenImageDenoise indisponible dans ce bpy ({exc}) ; mettez render.denoise à false.")

    b = r["bounces"]
    cy.max_bounces = int(b["total"])
    cy.diffuse_bounces = int(b["diffuse"])
    cy.glossy_bounces = int(b["glossy"])
    cy.transmission_bounces = int(b["transmission"])
    cy.transparent_max_bounces = int(b["transparent"])
    cy.volume_bounces = int(b["volume"])
    # Caustiques coupées par défaut : principale source de lucioles (« fireflies ») pour un gain visuel nul en motion design.
    cy.caustics_reflective = bool(r["caustics"])
    cy.caustics_refractive = bool(r["caustics"])

    # Graine fixe + graine animée : le bruit change d'une image à l'autre (pas de motif figé) mais reste reproductible.
    cy.seed = int(job["seed"])
    cy.use_animated_seed = True

    # Données persistantes : BVH et shaders conservés entre les images d'un même processus (rendu image par image plus rapide).
    scene.render.use_persistent_data = True

    shutter = float(r["motion_blur_shutter"])
    scene.render.use_motion_blur = shutter > 0
    scene.render.motion_blur_shutter = shutter if shutter > 0 else 0.5
    # Obturateur centré : le flou s'étale symétriquement autour de l'instant de l'image, comme le flou 2D du pilote web.
    scene.render.motion_blur_position = "CENTER"

    scene.render.film_transparent = bool(job["transparent"])
    scene.render.resolution_x = int(job["width"])
    scene.render.resolution_y = int(job["height"])
    scene.render.resolution_percentage = 100
    scene.render.pixel_aspect_x = 1.0
    scene.render.pixel_aspect_y = 1.0
    scene.render.fps = int(job["fps"])
    scene.render.fps_base = 1.0
    scene.frame_start = 0
    scene.frame_end = int(job["frames"]) - 1
    # Aucun post-traitement hors Cycles : la plaque est composée plus tard par la page web.
    scene.render.use_compositing = False
    scene.render.use_sequencer = False
    # Tramage inutile en 16 bits (il ne sert qu'à masquer les aplats en 8 bits) : on le coupe pour des pixels exacts.
    scene.render.dither_intensity = 0.0

    ims = scene.render.image_settings
    ims.file_format = "PNG"
    ims.color_mode = "RGBA"
    ims.color_depth = "16"
    # Blender convertit son pourcentage en niveau zlib = pourcentage × 9 / 100 (division entière) : on vise
    # exactement config.PNG_COMPRESSION (1 = encodage rapide, sans perte).
    ims.compression = min(100, math.ceil(config.PNG_COMPRESSION * 100 / 9))
    try:
        ims.color_management = "FOLLOW_SCENE"
    except (AttributeError, TypeError):
        pass

    return configure_color(scene, r)


def configure_color(scene: bpy.types.Scene, r: dict) -> str:
    """Vue + look + exposition. L'enum des looks est dynamique : seule l'affectation fait foi."""
    scene.display_settings.display_device = "sRGB"
    vs = scene.view_settings
    vt = str(r["view_transform"])
    try:
        vs.view_transform = vt
    except TypeError as exc:
        raise JobError(f"transformée de vue « {vt} » introuvable dans la config OCIO de bpy ({exc}). Utilisez par ex. « AgX ».")
    look = str(r["look"])
    # L'introspection de l'enum ne renvoie que « NONE » (liste construite à la volée) : on essaie donc
    # l'affectation du nom court puis du nom complet « <vue> - <look> » (forme exigée par Blender 4+).
    candidates = [look]
    if look != "None" and not look.startswith(f"{vt} - "):
        candidates.append(f"{vt} - {look}")
    errors = []
    applied = None
    for name in candidates:
        try:
            vs.look = name
        except TypeError as exc:
            errors.append(str(exc))
            continue
        if vs.look == name:
            applied = vs.look
            break
        errors.append(f"affectation de {name!r} relue {vs.look!r}")
    if applied is None:
        detail = errors[-1] if errors else "aucun détail"
        raise JobError(
            f"look « {look} » introuvable pour la vue « {vt} » (noms essayés : {', '.join(repr(c) for c in candidates)}). "
            f"Détail bpy : {detail}. Corrigez render.look dans le preset (ex. « Base Contrast », « Medium High Contrast », « None »)."
        )
    vs.exposure = float(r["exposure"])
    vs.gamma = 1.0
    # Pas de courbe utilisateur : la chaîne couleur se résume à vue + look + exposition, reproductible.
    vs.use_curve_mapping = False
    log(f"look appliqué : {applied} (vue {vs.view_transform}, exposition {vs.exposure:g})")
    return applied


def configure_device(scene: bpy.types.Scene, requested: str) -> str:
    """Choisit le périphérique Cycles ; renvoie une description lisible."""
    prefs = bpy.context.preferences.addons["cycles"].preferences

    def devices_of(kind: str) -> list:
        # compute_device_type est aussi un enum dynamique : l'affectation échoue si le type n'est pas compilé.
        try:
            prefs.compute_device_type = kind
        except TypeError:
            return []
        prefs.refresh_devices()
        return [d for d in prefs.get_devices_for_type(kind) if d.type == kind]

    def use_gpu(kind: str, devs: list) -> str:
        # Uniquement les GPU du type choisi (CPU exclu) : rendu hybride CPU+GPU = répartition variable des tuiles.
        for d in prefs.devices:
            d.use = d.type == kind
        scene.cycles.device = "GPU"
        # Débruitage OIDN sur le GPU : évite le va-et-vient mémoire GPU -> CPU à chaque image.
        scene.cycles.denoising_use_gpu = True
        return f"{kind} ({', '.join(d.name for d in devs)})"

    def use_cpu() -> str:
        try:
            prefs.compute_device_type = "NONE"
        except TypeError:
            pass
        scene.cycles.device = "CPU"
        scene.cycles.denoising_use_gpu = False
        # threads_mode AUTO : Cycles utilise tous les threads logiques (24 sur la machine de référence).
        scene.render.threads_mode = "AUTO"
        return f"CPU ({scene.render.threads} threads)"

    if requested == "CPU":
        return use_cpu()
    if requested in ("OPTIX", "CUDA"):
        devs = devices_of(requested)
        if not devs:
            raise JobError(
                f"périphérique {requested} demandé mais aucun GPU {requested} détecté par Cycles "
                "(pilote NVIDIA absent ou trop ancien ?). Mettez blender_device à « CPU » ou « auto »."
            )
        return use_gpu(requested, devs)
    if requested == "auto":
        devs = devices_of("OPTIX")
        if devs:
            return use_gpu("OPTIX", devs)
        log("aucun GPU NVIDIA compatible OptiX détecté : rendu sur CPU")
        return use_cpu()
    raise JobError(f"render.device {requested!r} inconnu ; valeurs admises : auto, CPU, OPTIX, CUDA.")


# ---------------------------------------------------------------------------------------------------
# Monde
# ---------------------------------------------------------------------------------------------------

def ensure_node_tree(idblock):
    """Arbre de nœuds d'un monde/matériau ; use_nodes est déprécié en 5.0 mais encore nécessaire si l'arbre manque."""
    if idblock.node_tree is None:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            idblock.use_nodes = True
    return idblock.node_tree


def build_world(scene: bpy.types.Scene, world_job: dict) -> None:
    # world.color seul n'est PAS rendu par Cycles (constat du contrat §0) : on construit un arbre de nœuds.
    world = bpy.data.worlds.new("monde")
    scene.world = world
    nt = ensure_node_tree(world)
    nt.nodes.clear()
    out = nt.nodes.new("ShaderNodeOutputWorld")
    bg = nt.nodes.new("ShaderNodeBackground")
    bg.inputs["Strength"].default_value = float(world_job["strength"])
    nt.links.new(bg.outputs["Background"], out.inputs["Surface"])
    sky = world_job.get("sky")
    if sky is None:
        bg.inputs["Color"].default_value = (*hex_to_linear(world_job["color"], "world.color"), 1.0)
        return
    node = nt.nodes.new("ShaderNodeTexSky")
    # Les identifiants de sky_type ont changé selon les versions : on lit l'enum réel avant de choisir.
    available = [item.identifier for item in node.bl_rna.properties["sky_type"].enum_items]
    model = next((m for m in SKY_MODEL_PREFERENCE if m in available), None)
    if model is None:
        raise JobError(f"aucun modèle de ciel connu dans ce bpy (sky_type disponibles : {available}) ; utilisez world.sky = null.")
    node.sky_type = model
    applied = []
    for key, names, kind in SKY_PROPS:
        prop = next((n for n in names if hasattr(node, n)), None)
        if prop is None:
            log(f"ciel : réglage « {key} » sans équivalent dans ce bpy, ignoré")
            continue
        value = float(sky[key])
        setattr(node, prop, math.radians(value) if kind == "angle" else value)
        applied.append(prop)
    if hasattr(node, "sun_disc"):
        node.sun_disc = True
    if model in ("HOSEK_WILKIE", "PREETHAM") and hasattr(node, "sun_direction"):
        # Modèles analytiques : pas d'élévation/rotation, seulement une direction de soleil (vecteur unitaire).
        el = math.radians(float(sky["sun_elevation"]))
        rot = math.radians(float(sky["sun_rotation"]))
        node.sun_direction = (math.cos(el) * math.sin(rot), -math.cos(el) * math.cos(rot), math.sin(el))
    nt.links.new(node.outputs["Color"], bg.inputs["Color"])
    log(f"ciel physique : modèle {model} (réglages {', '.join(applied)})")


# ---------------------------------------------------------------------------------------------------
# Matériaux
# ---------------------------------------------------------------------------------------------------

_MATERIAL_CACHE: dict[str, tuple[dict, bpy.types.Material]] = {}


def build_material(spec: dict, where: str) -> bpy.types.Material:
    name = str(spec["name"])
    cached = _MATERIAL_CACHE.get(name)
    # Un même matériau nommé est partagé entre objets (un seul shader à compiler) s'il est strictement identique.
    if cached is not None and cached[0] == spec:
        return cached[1]
    mat = bpy.data.materials.new(name)
    nt = ensure_node_tree(mat)
    bsdf = next((n for n in nt.nodes if n.bl_idname == "ShaderNodeBsdfPrincipled"), None)
    if bsdf is None:
        nt.nodes.clear()
        out = nt.nodes.new("ShaderNodeOutputMaterial")
        bsdf = nt.nodes.new("ShaderNodeBsdfPrincipled")
        nt.links.new(bsdf.outputs["BSDF"], out.inputs["Surface"])
    # Introspection des entrées réelles : on retient la première entrée activée portant l'un des noms connus.
    inputs: dict[str, object] = {}
    for sock in bsdf.inputs:
        if sock.name not in inputs and getattr(sock, "enabled", True):
            inputs[sock.name] = sock
    for key, names, kind in PRINCIPLED_INPUTS:
        sock = next((inputs[n] for n in names if n in inputs), None)
        if sock is None:
            log(f"{where} : entrée Principled pour « {key} » absente de ce bpy (noms essayés {names}), ignorée")
            continue
        if kind == "color":
            sock.default_value = (*hex_to_linear(spec[key], f"{where}.{key}"), 1.0)
        else:
            sock.default_value = float(spec[key])
    if cached is None:
        _MATERIAL_CACHE[name] = (spec, mat)
    return mat


# ---------------------------------------------------------------------------------------------------
# Géométrie
# ---------------------------------------------------------------------------------------------------

def mesh_from_bmesh(name: str, bm: bmesh.types.BMesh, *, recalc_normals: bool) -> bpy.types.Mesh:
    if recalc_normals:
        # Normales recalculées vers l'extérieur : géométrie fermée, orientation cohérente garantie.
        bmesh.ops.recalc_face_normals(bm, faces=bm.faces[:])
    me = bpy.data.meshes.new(name)
    bm.to_mesh(me)
    bm.free()
    me.update()
    return me


def smooth(me: bpy.types.Mesh, *, sharp_angle_deg: float | None = None) -> None:
    me.shade_smooth()
    if sharp_angle_deg is not None:
        # Arêtes vives au-delà de l'angle (jonction flanc/couvercle) : lissage sans ombrage « gonflé » sur les capots.
        me.set_sharp_from_angle(angle=math.radians(sharp_angle_deg))


def build_torus(size: float, minor: float, where: str) -> bmesh.types.BMesh:
    # Pas d'opérateur bmesh pour le tore : construction directe, déterministe et indépendante des opérateurs Python.
    major = size / 2.0 - minor
    if major <= 0:
        raise JobError(f"{where} : tore impossible, rayon du tube (minor={minor}) ≥ rayon extérieur (size/2={size / 2}). Réduisez minor.")
    nu, nv = TORUS_SEGMENTS
    bm = bmesh.new()
    grid = []
    for i in range(nu):
        u = 2.0 * math.pi * i / nu
        row = []
        for j in range(nv):
            v = 2.0 * math.pi * j / nv
            rr = major + minor * math.cos(v)
            row.append(bm.verts.new((rr * math.cos(u), rr * math.sin(u), minor * math.sin(v))))
        grid.append(row)
    for i in range(nu):
        for j in range(nv):
            a, b = grid[i][j], grid[(i + 1) % nu][j]
            c, d = grid[(i + 1) % nu][(j + 1) % nv], grid[i][(j + 1) % nv]
            bm.faces.new((a, b, c, d))
    return bm


def build_cyclorama(size: float, height: float, radius: float, where: str) -> bmesh.types.BMesh:
    """Sol (largeur = profondeur = size) + mur de fond (hauteur = height) + congé de rayon radius.

    Origine au centre du sol (z = 0) ; le mur est en y = +size/2 et fait face à -Y (vers la caméra).
    """
    if radius > height:
        raise JobError(f"{where} : congé (bevel={radius}) plus haut que le mur (minor={height}) ; prenez bevel ≤ minor.")
    if radius >= size:
        raise JobError(f"{where} : congé (bevel={radius}) plus profond que le sol (size={size}) ; prenez bevel < size.")
    half = size / 2.0
    y_fillet = half - radius
    # Profil (y, z) de l'avant du sol jusqu'au sommet du mur : l'extrusion en X donne la surface.
    profile = [(-half, 0.0), (y_fillet, 0.0)]
    if radius > 0:
        for k in range(1, FILLET_SEGMENTS + 1):
            t = (math.pi / 2.0) * k / FILLET_SEGMENTS
            profile.append((y_fillet + radius * math.sin(t), radius - radius * math.cos(t)))
    if height > radius:
        profile.append((half, height))
    bm = bmesh.new()
    left = [bm.verts.new((-half, y, z)) for y, z in profile]
    right = [bm.verts.new((half, y, z)) for y, z in profile]
    for i in range(len(profile) - 1):
        # Ordre (gauche_i, droite_i, droite_i+1, gauche_i+1) : sens antihoraire vu de +Z (sol) et de -Y (mur),
        # donc normales tournées vers l'intérieur du studio, là où se trouve la caméra.
        bm.faces.new((left[i], right[i], right[i + 1], left[i + 1]))
    return bm


def build_mesh(obj_job: dict) -> tuple[bpy.types.Mesh, bool]:
    """Renvoie (mesh, besoin d'un biseau rounded_cube)."""
    oid = obj_job["id"]
    where = f"objet « {oid} »"
    prim = obj_job["primitive"]
    size = float(obj_job["size"])
    minor = float(obj_job["minor"])
    bevel = float(obj_job["bevel"])
    bm = bmesh.new()
    if prim == "sphere":
        bmesh.ops.create_uvsphere(bm, u_segments=SPHERE_SEGMENTS[0], v_segments=SPHERE_SEGMENTS[1], radius=size / 2.0)
        me = mesh_from_bmesh(oid, bm, recalc_normals=True)
        smooth(me)
        return me, False
    if prim in ("cube", "rounded_cube"):
        bmesh.ops.create_cube(bm, size=size)
        me = mesh_from_bmesh(oid, bm, recalc_normals=True)
        if prim == "rounded_cube" and bevel > 0:
            # Lissage + normales durcies par le modificateur : faces planes nettes, arrondis doux.
            smooth(me)
            return me, True
        return me, False
    if prim == "torus":
        bm.free()
        me = mesh_from_bmesh(oid, build_torus(size, minor, where), recalc_normals=True)
        smooth(me)
        return me, False
    if prim in ("cylinder", "cone"):
        # Hauteur = size (le contrat ne prévoit pas de hauteur propre) ; l'échelle de l'objet ajuste les proportions.
        bmesh.ops.create_cone(bm, cap_ends=True, cap_tris=False, segments=ROUND_SEGMENTS,
                              radius1=size / 2.0, radius2=(size / 2.0 if prim == "cylinder" else 0.0), depth=size)
        me = mesh_from_bmesh(oid, bm, recalc_normals=True)
        smooth(me, sharp_angle_deg=SHARP_ANGLE_DEG)
        return me, False
    if prim == "plane":
        h = size / 2.0
        vs = [bm.verts.new(co) for co in ((-h, -h, 0.0), (h, -h, 0.0), (h, h, 0.0), (-h, h, 0.0))]
        bm.faces.new(vs)  # sens antihoraire vu de +Z : normale vers le haut
        return mesh_from_bmesh(oid, bm, recalc_normals=False), False
    if prim == "cyclorama":
        bm.free()
        me = mesh_from_bmesh(oid, build_cyclorama(size, minor, bevel, where), recalc_normals=False)
        if bevel > 0:
            smooth(me)  # sans congé l'angle sol/mur doit rester vif : ombrage plat
        return me, False
    bm.free()
    raise JobError(f"{where} : primitive {prim!r} inconnue ; valeurs admises : sphere, cube, rounded_cube, torus, cylinder, cone, plane, cyclorama.")


# ---------------------------------------------------------------------------------------------------
# Scène
# ---------------------------------------------------------------------------------------------------

def link(scene: bpy.types.Scene, obj: bpy.types.Object) -> bpy.types.Object:
    scene.collection.objects.link(obj)
    return obj


def make_target(scene: bpy.types.Scene, owner: str, location) -> bpy.types.Object:
    empty = link(scene, bpy.data.objects.new(f"{owner}__cible", None))
    empty.empty_display_type = "PLAIN_AXES"
    empty.location = mathutils.Vector(location)
    return empty


def track_to(obj: bpy.types.Object, target: bpy.types.Object) -> None:
    c = obj.constraints.new("TRACK_TO")
    c.target = target
    # Caméras et lumières regardent le long de leur -Z local, Y local vers le haut : convention Blender.
    c.track_axis = "TRACK_NEGATIVE_Z"
    c.up_axis = "UP_Y"


def build_camera(scene: bpy.types.Scene, cam_job: dict) -> tuple[bpy.types.Object, bpy.types.Object]:
    data = bpy.data.cameras.new("camera")
    data.lens = float(cam_job["lens"])
    data.sensor_width = float(cam_job["sensor_width"])
    # AUTO = comportement par défaut de Blender (capteur sur la plus grande dimension) : même cadrage que l'appli.
    data.sensor_fit = "AUTO"
    data.clip_end = 1000.0
    cam = link(scene, bpy.data.objects.new("camera", data))
    cam.location = mathutils.Vector(cam_job["location"])
    target = make_target(scene, "camera", cam_job["target"])
    track_to(cam, target)
    if cam_job["fstop"] is not None:
        # Mise au point sur l'objet vide visé : la netteté suit la cible même si elle est animée.
        data.dof.use_dof = True
        data.dof.focus_object = target
        data.dof.aperture_fstop = float(cam_job["fstop"])
    scene.camera = cam
    return cam, target


def build_light(scene: bpy.types.Scene, lj: dict) -> tuple[bpy.types.Object, bpy.types.Object]:
    lid = lj["id"]
    kind = lj["type"]
    data = bpy.data.lights.new(lid, kind)
    data.energy = float(lj["energy"])
    data.color = hex_to_linear(lj["color"], f"lumière « {lid} ».color")
    if kind == "AREA":
        data.shape = lj["shape"]
        data.size = float(lj["size"])
        data.size_y = float(lj["size_y"])
    elif kind == "SUN":
        data.angle = math.radians(float(lj["angle"]))
    elif kind == "POINT":
        data.shadow_soft_size = float(lj["size"])
    elif kind == "SPOT":
        data.shadow_soft_size = float(lj["size"])
        data.spot_size = math.radians(float(lj["spot_size"]))
    else:
        raise JobError(f"lumière « {lid} » : type {kind!r} inconnu ; valeurs admises : AREA, SUN, POINT, SPOT.")
    obj = link(scene, bpy.data.objects.new(lid, data))
    obj.location = mathutils.Vector(lj["location"])
    target = make_target(scene, lid, lj["target"])
    track_to(obj, target)
    return obj, target


def build_object(scene: bpy.types.Scene, oj: dict) -> bpy.types.Object:
    me, rounded = build_mesh(oj)
    obj = link(scene, bpy.data.objects.new(oj["id"], me))
    obj.rotation_mode = "XYZ"
    obj.location = mathutils.Vector(oj["location"])
    obj.rotation_euler = mathutils.Euler([math.radians(a) for a in oj["rotation"]], "XYZ")
    obj.scale = mathutils.Vector(oj["scale"])
    if rounded:
        mod = obj.modifiers.new("biseau", "BEVEL")
        mod.width = float(oj["bevel"])
        mod.segments = BEVEL_SEGMENTS
        mod.limit_method = "ANGLE"
        # Chevauchement borné : un biseau plus large que la moitié de l'arête ne retourne pas la géométrie.
        mod.use_clamp_overlap = True
        # Normales durcies : les grandes faces restent parfaitement planes à l'ombrage malgré le lissage.
        mod.harden_normals = True
    obj.data.materials.append(build_material(oj["material"], f"objet « {oj['id']} ».material"))
    return obj


def convert_value(prop: str, value) -> list[float]:
    vals = [float(v) for v in value]
    if len(vals) != 3:
        raise JobError(f"valeur {value!r} : un vec3 [x, y, z] est attendu.")
    return [math.radians(v) for v in vals] if prop == "rotation" else vals


def apply_channels(owner: str, obj: bpy.types.Object, target: bpy.types.Object | None,
                   channels: list, allowed: tuple[str, ...]) -> list[bpy.types.Object]:
    """Insère les clés puis pose l'interpolation sur la clé de DÉPART de chaque segment. Renvoie les objets animés."""
    animated: list[bpy.types.Object] = []
    seen: set[str] = set()
    for ch in channels:
        prop = ch["prop"]
        if prop not in allowed:
            raise JobError(
                f"« {owner} » : propriété animée {prop!r} non prise en charge ici (admises : {', '.join(allowed)}). "
                + ("L'orientation de la caméra et des lumières est donnée par leur cible : animez « target »."
                   if "target" in allowed else "« target » ne concerne que la caméra et les lumières.")
            )
        if prop in seen:
            raise JobError(f"« {owner} » : deux canaux animent {prop!r} ; fusionnez leurs clés dans un seul canal.")
        seen.add(prop)
        holder = target if prop == "target" else obj
        path = DATA_PATHS[prop]
        keys = ch["keys"]
        frames = [float(k["frame"]) for k in keys]
        if any(b <= a for a, b in zip(frames, frames[1:])):
            raise JobError(f"« {owner} ».{prop} : clés non triées ou en double (frames {frames}) ; le compilateur doit les trier.")
        if len(keys) == 1:
            # Contrat §3.10 : un canal à une seule clé est une valeur constante (aucune F-curve).
            setattr(holder, path, convert_value(prop, keys[0]["value"]))
            continue
        for k in keys:
            setattr(holder, path, convert_value(prop, k["value"]))
            # Clé à la frame FLOTTANTE exacte : le temps local × fps du compilateur n'est jamais arrondi.
            holder.keyframe_insert(data_path=path, frame=float(k["frame"]))
        ad = holder.animation_data
        # Blender 5 : actions à emplacements ; les F-curves vivent dans le channelbag de l'emplacement assigné.
        bag = anim_utils.action_get_channelbag_for_slot(ad.action, ad.action_slot)
        if bag is None:
            raise JobError(f"« {owner} ».{prop} : channelbag introuvable après insertion des clés (API bpy inattendue).")
        for index in range(3):
            fc = bag.fcurves.find(path, index=index)
            if fc is None:
                raise JobError(f"« {owner} ».{prop}[{index}] : F-curve introuvable après insertion des clés.")
            points = sorted(fc.keyframe_points, key=lambda p: p.co.x)
            if len(points) != len(keys):
                raise JobError(f"« {owner} ».{prop}[{index}] : {len(points)} clés relues pour {len(keys)} écrites (frames trop proches ?).")
            for kp, k in zip(points, keys):
                if abs(kp.co.x - float(k["frame"])) > FRAME_EPS:
                    raise JobError(f"« {owner} ».{prop}[{index}] : clé relue à {kp.co.x} au lieu de {k['frame']}.")
                seg = k["out"]
                if seg is None:
                    continue  # dernière clé : aucun segment ne part d'elle (extrapolation constante)
                # Blender lit l'interpolation d'un segment sur sa clé de DÉPART (contrat : champ « out »).
                kp.interpolation = seg["interpolation"]
                kp.easing = seg["easing"]
                if "back" in seg:
                    kp.back = float(seg["back"])
                if "amplitude" in seg:
                    kp.amplitude = float(seg["amplitude"])
                if "period" in seg:
                    kp.period = float(seg["period"])
            fc.update()
        if holder not in animated:
            animated.append(holder)
    return animated


def build_scene(job: dict) -> tuple[bpy.types.Scene, list[bpy.types.Object], str]:
    # Scène vide d'usine : aucun réglage utilisateur ni objet par défaut ne peut influencer les pixels.
    bpy.ops.wm.read_factory_settings(use_empty=True)
    scene = bpy.context.scene
    look = configure_render(scene, job)
    build_world(scene, job["world"])
    animated: list[bpy.types.Object] = []
    cam, cam_target = build_camera(scene, job["camera"])
    animated += apply_channels("camera", cam, cam_target, job["camera"]["channels"], TRACKED_PROPS)
    ids = set()
    for lj in job["lights"]:
        if lj["id"] in ids:
            raise JobError(f"identifiant de lumière « {lj['id']} » en double ; chaque id doit être unique.")
        ids.add(lj["id"])
        obj, target = build_light(scene, lj)
        animated += apply_channels(f"lumière {lj['id']}", obj, target, lj["channels"], TRACKED_PROPS)
    for oj in job["objects"]:
        if oj["id"] in ids:
            raise JobError(f"identifiant d'objet « {oj['id']} » en double (avec une lumière ou un objet) ; renommez-le.")
        ids.add(oj["id"])
        obj = build_object(scene, oj)
        animated += apply_channels(f"objet {oj['id']}", obj, None, oj["channels"], OBJECT_PROPS)
    return scene, animated, look


# ---------------------------------------------------------------------------------------------------
# Contrôle d'une image PNG (bibliothèque standard seulement)
# ---------------------------------------------------------------------------------------------------

def png_is_complete(path: Path, width: int, height: int) -> bool:
    """Vrai si path est un PNG RGBA 16 bits width×height entièrement décodable (CRC + flux zlib complet).

    Contrôle sans bpy ni Pillow : une image tronquée par un arrêt brutal est détectée et re-rendue.
    """
    try:
        data = path.read_bytes()
    except OSError:
        return False
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        return False
    pos = 8
    header = None
    inflater = zlib.decompressobj()
    produced = 0
    ended = False
    try:
        while pos + 12 <= len(data):
            length, ctype = struct.unpack(">I4s", data[pos:pos + 8])
            body = data[pos + 8:pos + 8 + length]
            crc_raw = data[pos + 8 + length:pos + 12 + length]
            if len(body) != length or len(crc_raw) != 4:
                return False
            if zlib.crc32(ctype + body) & 0xFFFFFFFF != struct.unpack(">I", crc_raw)[0]:
                return False
            if ctype == b"IHDR":
                header = struct.unpack(">IIBBBBB", body)
            elif ctype == b"IDAT":
                produced += len(inflater.decompress(body))
            elif ctype == b"IEND":
                ended = True
                break
            pos += 12 + length
        produced += len(inflater.flush())
    except (zlib.error, struct.error):
        return False
    if not ended or header is None or not inflater.eof:
        return False
    w, h, depth, color_type, _comp, _filter, interlace = header
    # Type de couleur 6 = RGBA ; 1 octet de filtre + 4 canaux × 2 octets par pixel et par ligne.
    return (w, h, depth, color_type, interlace) == (width, height, 16, 6, 0) and produced == h * (1 + w * 8)


# ---------------------------------------------------------------------------------------------------
# Boucles principales
# ---------------------------------------------------------------------------------------------------

def print_values(scene: bpy.types.Scene, animated: list[bpy.types.Object], frames: list[int]) -> None:
    for f in frames:
        scene.frame_set(f)
        for obj in animated:
            record = {
                "frame": f,
                "object": obj.name,
                "location": [round(v, 6) for v in obj.location],
                "rotation": [round(math.degrees(v), 6) for v in obj.rotation_euler],
                "scale": [round(v, 6) for v in obj.scale],
            }
            print(f"{VALUES_PREFIX} {json.dumps(record, ensure_ascii=False)}", flush=True)


def render_result_image() -> bpy.types.Image:
    # Recherche par type plutôt que par nom : le nom « Render Result » peut être traduit selon l'interface.
    for img in bpy.data.images:
        if img.type == "RENDER_RESULT":
            return img
    raise JobError("résultat de rendu introuvable après bpy.ops.render.render (rendu interrompu ?).")


def render_frames(scene: bpy.types.Scene, job: dict, frames: list[int]) -> None:
    out_dir = Path(job["out_dir"])
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise JobError(f"impossible de créer le dossier de sortie {out_dir} : {exc}. Vérifiez les droits et l'espace disque.")
    total = int(job["frames"])
    width, height = int(job["width"]), int(job["height"])
    rendered = skipped = 0
    times: list[float] = []
    for i in frames:
        final = out_dir / config.FRAME_PATTERN.format(i)
        # Reprise : une image présente ET décodable n'est jamais re-rendue ; une image tronquée l'est.
        if final.exists():
            if png_is_complete(final, width, height):
                skipped += 1
                continue
            log(f"image {i} présente mais incomplète ou invalide : nouveau rendu")
        tmp = final.with_name(final.name + ".tmp")
        scene.frame_set(i)
        t0 = time.perf_counter()
        result = bpy.ops.render.render(write_still=False)
        if "FINISHED" not in result:
            raise JobError(f"rendu de l'image {i} interrompu par Cycles ({result}).")
        # save_render applique vue + look + exposition de la scène et écrit EXACTEMENT ce chemin (pas de « # »
        # interprété, pas d'extension ajoutée), ce qui permet l'écriture atomique .tmp -> os.replace.
        render_result_image().save_render(str(tmp), scene=scene)
        if not png_is_complete(tmp, width, height):
            raise JobError(f"image {i} : PNG écrit invalide ({tmp}) ; espace disque plein ?")
        os.replace(tmp, final)
        dt = time.perf_counter() - t0
        times.append(dt)
        rendered += 1
        print(f"{PREFIX} image {i}/{total}", flush=True)
        log(f"temps image {i} : {dt:.3f} s")
    mean = (sum(times) / len(times)) if times else 0.0
    log(f"terminé : {rendered} image(s) rendue(s), {skipped} déjà présente(s) sautée(s), moyenne {mean:.3f} s/image")


def force_utf8_output() -> None:
    # Sous Windows, un tube hérite de la page de code ANSI (cp1252) : on impose UTF-8, que le lanceur décode.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def main(argv: list[str]) -> int:
    force_utf8_output()
    parser = argparse.ArgumentParser(description="Backend Blender mograph (plaques 3D, Cycles).")
    parser.add_argument("job", help="chemin de job.json (contrat mograph-blender-job/1)")
    parser.add_argument("--frames", default=None, help="images à traiter, ex. 0,12,40 ou 0-10 (défaut : toutes)")
    parser.add_argument("--print-values", action="store_true", help="imprime les valeurs animées évaluées, sans rendu")
    args = parser.parse_args(argv)
    job = load_job(Path(args.job).resolve())
    frames = parse_frames(args.frames, int(job["frames"]))
    log(f"bpy {bpy.app.version_string} ; tâche {job['scene_id']}/{job['shot_id']}/{job['plate_id']} ; "
        f"{job['width']}x{job['height']} ; {job['frames']} image(s) à {job['fps']} i/s ; {len(frames)} demandée(s)")
    t0 = time.perf_counter()
    scene, animated, _look = build_scene(job)
    log(f"scène construite en {time.perf_counter() - t0:.2f} s ({len(animated)} objet(s) animé(s))")
    if args.print_values:
        print_values(scene, animated, frames)
        return 0
    log(f"périphérique : {configure_device(scene, str(job['render']['device']))}")
    render_frames(scene, job, frames)
    return 0


if __name__ == "__main__":
    try:
        code = main(sys.argv[1:])
    except JobError as exc:
        print(f"{PREFIX} ERREUR : {exc}", file=sys.stderr, flush=True)
        code = 2
    except Exception as exc:  # toute autre erreur : trace complète pour le diagnostic, code non nul
        traceback.print_exc()
        print(f"{PREFIX} ERREUR : exception inattendue {type(exc).__name__} : {exc}. "
              "Voir la trace ci-dessus ; signalez-la avec le job.json.", file=sys.stderr, flush=True)
        code = 1
    sys.stdout.flush()
    sys.exit(code)
