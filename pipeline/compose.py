"""Séquence maître : build/<scene>/master/NNNNNN.png (index GLOBAL) + master/manifest.json.

- reconstruite de zéro à chaque appel (aucune image périmée ne peut survivre) ;
- un plan rendu pour une autre version de la scène (spec_sha256 ≠ empreinte du plan compilé) est
  refusé : relancer le rendu ;
- coupes : liens physiques (os.link) vers les images des plans, aucune recopie ;
- profondeur UNIQUE = compiled["depth"] : un plan 8 bits dans un maître 16 bits est converti (v × 257) ;
- fondus : mélange en alpha prémultiplié, poids du plan entrant smoothstep((k+1)/(F+1)) (CONTRACT §3.2),
  désprémultiplication propre (alpha 0 => RVB 0) ;
- contrôle final : nombre d'images = compiled["frames"].
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import cv2
import jsonschema
import numpy as np
import referencing
import referencing.jsonschema

from pipeline import config

MASTER_CONTRACT = "mograph-master/1"
SHOT_MANIFEST_CONTRACT = "mograph-shot-manifest/1"
_MANIFESTS_SCHEMA_PATH = config.SCHEMA_DIR / "internal" / "manifests.schema.json"
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_PNG_COLOR_RGBA = 6           # type de couleur PNG « truecolor + alpha »
_MAXV = {8: 255.0, 16: 65535.0}
# Mémoire allouable aux mélanges simultanés (octets) : 6 Go laissent de la marge sur 16 Go de RAM.
MIX_MEMORY_BUDGET = 6 * 1024 ** 3
_DTYPE = {8: np.uint8, 16: np.uint16}


class ComposeError(RuntimeError):
    """Erreur de composition destinée à l'utilisateur (message en français, actionnable)."""


# ---------------------------------------------------------------------------
# Schémas
# ---------------------------------------------------------------------------

_VALIDATORS: dict[str, jsonschema.protocols.Validator] = {}


def _validator(defname: str):
    """Validateur Draft 2020-12 d'une définition de manifests.schema.json (mis en cache)."""
    if defname not in _VALIDATORS:
        schema = json.loads(_MANIFESTS_SCHEMA_PATH.read_text(encoding="utf-8"))
        resource = referencing.Resource.from_contents(schema, default_specification=referencing.jsonschema.DRAFT202012)
        # Registre referencing : le $ref vers « <id>#/$defs/<nom> » se résout sans accès réseau.
        registry = referencing.Registry().with_resource(schema["$id"], resource)
        _VALIDATORS[defname] = jsonschema.Draft202012Validator(
            {"$ref": f"{schema['$id']}#/$defs/{defname}"}, registry=registry)
    return _VALIDATORS[defname]


def _schema_errors(obj: dict, defname: str) -> list[str]:
    errs = sorted(_validator(defname).iter_errors(obj), key=lambda e: list(e.absolute_path))
    return [f"{'/'.join(str(p) for p in e.absolute_path) or '(racine)'} : {e.message}" for e in errs[:10]]


# ---------------------------------------------------------------------------
# Entrées / sorties d'images
# ---------------------------------------------------------------------------

def smoothstep(x: float) -> float:
    """smoothstep(x) = x²(3 − 2x) (CONTRACT §3.2)."""
    return x * x * (3.0 - 2.0 * x)


def fade_weight(k: int, fade_frames: int) -> float:
    """Poids du plan ENTRANT à l'image k (0..F−1) d'un fondu de F images : jamais 0 ni 1."""
    return smoothstep((k + 1) / (fade_frames + 1))


def _png_header(path: Path) -> tuple[int, int, int, int]:
    """(largeur, hauteur, profondeur de bits, type de couleur) lus dans le bloc IHDR.

    Lecture de 26 octets au lieu d'un décodage complet : on peut vérifier toutes les images d'une
    longue séquence 4K en une fraction de seconde.
    """
    with open(path, "rb") as fh:
        head = fh.read(26)
    if len(head) < 26 or head[:8] != _PNG_SIGNATURE or head[12:16] != b"IHDR":
        raise ComposeError(f"{path} n'est pas un PNG valide (en-tête illisible) : relancez le rendu de ce plan.")
    width = int.from_bytes(head[16:20], "big")
    height = int.from_bytes(head[20:24], "big")
    return width, height, head[24], head[25]


def _read_png(path: Path) -> np.ndarray:
    """PNG -> tableau BGRA uint8/uint16 ; np.fromfile + imdecode supporte les chemins non ASCII
    sous Windows (cv2.imread non)."""
    buf = np.fromfile(str(path), dtype=np.uint8)
    img = cv2.imdecode(buf, cv2.IMREAD_UNCHANGED)
    if img is None or img.ndim != 3 or img.shape[2] != 4:
        raise ComposeError(f"Impossible de décoder {path} en BGRA : relancez le rendu de ce plan.")
    return img


def _write_png(path: Path, img: np.ndarray) -> None:
    """Écriture atomique : encodage en mémoire, fichier .tmp, puis os.replace."""
    ok, buf = cv2.imencode(".png", img, [cv2.IMWRITE_PNG_COMPRESSION, config.PNG_COMPRESSION])
    if not ok:
        raise ComposeError(f"Encodage PNG impossible pour {path}.")
    tmp = path.with_name(path.name + ".tmp")
    buf.tofile(str(tmp))
    os.replace(tmp, path)


def _to_float(img: np.ndarray) -> np.ndarray:
    """Normalisation [0, 1] en float32 : précision relative 6e-8, soit < 0,01 pas de quantification
    même en 16 bits, avec deux fois moins de mémoire que float64 pour les images 4K."""
    maxv = 65535.0 if img.dtype == np.uint16 else 255.0
    return img.astype(np.float32) * np.float32(1.0 / maxv)


def spec_sha256(spec) -> str:
    """Empreinte d'un plan compilé : SHA-256 du JSON canonique (clés triées, séparateurs compacts,
    UTF-8), même forme que pipeline.scene.canonical_json ; recopiée ici pour ne pas dépendre du
    compilateur à l'exécution."""
    canon = json.dumps(spec, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(canon).hexdigest()


def _quantize(v: np.ndarray, depth: int) -> np.ndarray:
    maxv = _MAXV[depth]
    return np.clip(np.rint(v * np.float32(maxv)), 0, maxv).astype(_DTYPE[depth])


def blend_premultiplied(layers: list[tuple[np.ndarray, float]], depth: int) -> np.ndarray:
    """Mélange successif en alpha PRÉMULTIPLIÉ : acc = acc·(1 − w) + image·w pour chaque plan
    entrant (le premier plan a w = 1). Prémultiplier évite les franges sombres ou colorées là où
    un plan est transparent. Désprémultiplication : RVB = P / alpha, et RVB = 0 si alpha = 0."""
    acc = None
    for img, w in layers:
        f = _to_float(img)
        alpha = f[:, :, 3:4]
        pre = np.concatenate([f[:, :, :3] * alpha, alpha], axis=2)
        if acc is None:
            acc = pre
        else:
            acc = acc * np.float32(1.0 - w) + pre * np.float32(w)
    alpha = acc[:, :, 3:4]
    safe = np.where(alpha > 0, alpha, np.float32(1.0))
    rgb = np.where(alpha > 0, acc[:, :, :3] / safe, np.float32(0.0))
    out = _quantize(np.concatenate([np.clip(rgb, 0.0, 1.0), np.clip(alpha, 0.0, 1.0)], axis=2), depth)
    # La règle « alpha 0 => RVB 0 » porte sur l'alpha QUANTIFIÉ : un alpha flottant minuscule
    # (< ½ niveau) s'arrondit à 0 et laisserait sinon une couleur invisible mais non nulle, qui
    # réapparaîtrait en frange si un logiciel désprémultiplie ou compose ce pixel.
    out[..., :3][out[..., 3] == 0] = 0
    return out


def convert_depth(img: np.ndarray, depth: int) -> np.ndarray:
    """8 -> 16 bits exact : v × 257 (255 × 257 = 65535, 0 -> 0 : les extrêmes sont conservés)."""
    if depth == 16 and img.dtype == np.uint8:
        return img.astype(np.uint16) * np.uint16(257)
    if (depth == 8 and img.dtype == np.uint8) or (depth == 16 and img.dtype == np.uint16):
        return img
    raise ComposeError("Conversion 16 -> 8 bits refusée : la profondeur maître ne peut pas être "
                       "inférieure à celle d'un plan (recompilez la scène).")


# ---------------------------------------------------------------------------
# Lecture et vérification des plans
# ---------------------------------------------------------------------------

def _load_shot(compiled: dict, shot: dict) -> dict:
    """Lit et vérifie le manifeste d'un plan ; renvoie {id, dir, depth, files[], layout, manifest}."""
    scene_id = compiled["scene_id"]
    shot_id = shot["id"]
    sdir = config.shot_frames_dir(scene_id, shot_id)
    mpath = sdir / "manifest.json"
    hint = f"relancez le rendu du plan « {shot_id} » (python mograph.py render <scène>)"
    if not mpath.is_file():
        raise ComposeError(f"Manifeste absent : {mpath}. Le plan « {shot_id} » n'a pas été rendu : {hint}.")
    try:
        man = json.loads(mpath.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise ComposeError(f"Manifeste illisible ({mpath}) : {exc}. {hint[0].upper()}{hint[1:]}.") from exc
    errs = _schema_errors(man, "shot_manifest")
    if errs:
        raise ComposeError(f"Manifeste non conforme ({mpath}) :\n  - " + "\n  - ".join(errs) + f"\n{hint}.")

    out_w, out_h = compiled["output"]["width"], compiled["output"]["height"]
    fps = compiled["format"]["fps"]
    problems = []
    if man["scene_id"] != scene_id or man["shot_id"] != shot_id:
        problems.append(f"manifeste de {man['scene_id']}/{man['shot_id']} au lieu de {scene_id}/{shot_id}")
    if man["frames"] != shot["frames"]:
        problems.append(f"{man['frames']} images au manifeste, {shot['frames']} attendues par la scène compilée")
    if (man["width"], man["height"]) != (out_w, out_h):
        problems.append(f"taille {man['width']}x{man['height']} au lieu de {out_w}x{out_h}")
    if man["fps"] != fps:
        problems.append(f"{man['fps']} i/s au lieu de {fps}")
    if man["depth"] > compiled["depth"]:
        problems.append(f"profondeur {man['depth']} bits > profondeur maître {compiled['depth']} bits")
    if problems:
        raise ComposeError(f"Plan « {shot_id} » incohérent avec la scène compilée : " + " ; ".join(problems)
                           + f". Le rendu est périmé : {hint}.")

    # Empreinte du plan : convention unique sha256(canonical_json(shot["spec"])) (celle qu'écrit
    # web_render et que vérifie verify_determinism). Un plan périmé est REFUSÉ et non simplement
    # signalé : `mograph.py encode` compose sans rendre, et livrerait sinon les anciennes images
    # d'une scène modifiée à nombre d'images constant (texte, couleur, easing).
    if spec_sha256(shot.get("spec")) != man["spec_sha256"]:
        raise ComposeError(
            f"Plan « {shot_id} » périmé : la scène a changé depuis le rendu de ses images "
            f"(empreinte {man['spec_sha256'][:12]}… au manifeste {mpath}, "
            f"{spec_sha256(shot.get('spec'))[:12]}… pour la scène compilée). "
            f"Relancez le rendu : python mograph.py render {scene_id} (ou all).")

    files: list[Path] = []
    missing: list[int] = []
    for i in range(shot["frames"]):
        entry = man["frames_done"].get(str(i))
        if entry is None:
            missing.append(i)
            continue
        f = sdir / entry["file"]
        if entry["file"] != config.FRAME_PATTERN.format(i) or not f.is_file():
            missing.append(i)
            continue
        w, h, bits, color = _png_header(f)
        if (w, h) != (out_w, out_h) or bits != man["depth"] or color != _PNG_COLOR_RGBA:
            raise ComposeError(
                f"{f} : PNG {w}x{h} {bits} bits (type de couleur {color}) alors que le manifeste annonce "
                f"{out_w}x{out_h} {man['depth']} bits RGBA : {hint}.")
        files.append(f)
    if missing:
        preview = ", ".join(str(i) for i in missing[:10]) + (" ..." if len(missing) > 10 else "")
        raise ComposeError(f"Plan « {shot_id} » incomplet : {len(missing)} image(s) manquante(s) ({preview}) "
                           f"dans {sdir}. {hint[0].upper()}{hint[1:]} (la reprise ne refait que les images manquantes).")
    return {"id": shot_id, "dir": sdir, "depth": man["depth"], "files": files, "manifest": man}


def _check_timeline(compiled: dict) -> None:
    """Recalcule global_start / total selon CONTRACT §3.2 et refuse toute incohérence."""
    shots = compiled["shots"]
    if not shots:
        raise ComposeError("La scène compilée ne contient aucun plan.")
    expected_start = 0
    prev_frames = None
    for i, s in enumerate(shots):
        f_in = s["fade_in_frames"]
        if i == 0 and f_in != 0:
            raise ComposeError(f"Le premier plan « {s['id']} » a fade_in_frames = {f_in} : un fondu d'entrée "
                               "sur le plan 0 est impossible (recompilez la scène).")
        if i > 0:
            if f_in > min(prev_frames, s["frames"]):
                raise ComposeError(f"Fondu de {f_in} images vers « {s['id']} » plus long que l'un des deux plans "
                                   f"({prev_frames} / {s['frames']} images) : recompilez la scène.")
            expected_start += prev_frames - f_in
        if s["global_start"] != expected_start:
            raise ComposeError(f"global_start du plan « {s['id']} » = {s['global_start']}, attendu {expected_start} "
                               "(début précédent + images précédentes − fondu) : recompilez la scène.")
        prev_frames = s["frames"]
    total = shots[-1]["global_start"] + shots[-1]["frames"]
    if total != compiled["frames"]:
        raise ComposeError(f"Total incohérent : {total} images d'après les plans, {compiled['frames']} dans "
                           "la scène compilée. Recompilez la scène.")


# ---------------------------------------------------------------------------
# Système de fichiers
# ---------------------------------------------------------------------------

def _rmtree(path: Path) -> None:
    """Suppression récursive robuste sous Windows (fichiers en lecture seule)."""
    def on_error(func, p, _exc):
        # Un fichier en lecture seule refuse la suppression sous Windows : on lève le drapeau et on réessaie.
        os.chmod(p, stat.S_IWRITE)
        func(p)
    try:
        shutil.rmtree(path, onexc=on_error)
    except OSError as exc:
        raise ComposeError(f"Impossible de supprimer {path} ({exc}). Fermez le programme qui utilise ces "
                           "images (lecteur, Resolve, explorateur) puis relancez la composition.") from exc


def _link(src: Path, dst: Path) -> None:
    try:
        os.link(src, dst)
    except OSError as exc:
        raise ComposeError(
            f"Lien physique impossible {src} -> {dst} ({exc}). Les liens physiques exigent un volume NTFS "
            "(ou ext4/APFS) et que build/ tienne sur UN SEUL volume : déplacez MOGRAPH_BUILD_DIR si besoin."
        ) from exc


# ---------------------------------------------------------------------------
# API publique
# ---------------------------------------------------------------------------

def compose_scene(compiled: dict, *, log=print) -> dict:
    """Construit build/<scene>/master/ et renvoie le manifeste maître (déjà écrit sur disque)."""
    t0 = time.perf_counter()
    scene_id = compiled["scene_id"]
    depth = int(compiled["depth"])
    if depth not in (8, 16):
        raise ComposeError(f"Profondeur maître invalide ({depth}) : 8 ou 16 attendus (recompilez la scène).")
    fps = int(compiled["format"]["fps"])
    total = int(compiled["frames"])
    _check_timeline(compiled)
    shots = [_load_shot(compiled, s) for s in compiled["shots"]]

    # Plans présents à chaque image globale. Les débuts et fins des plans sont croissants
    # (CONTRACT §3.2 : F_i ≤ frames_i), donc les plans actifs forment une plage contiguë ; deux plans
    # actifs = fondu ; trois (fondus qui se chevauchent sur un plan court) = mélanges successifs.
    starts = [s["global_start"] for s in compiled["shots"]]
    tasks: list[dict] = []
    for g in range(total):
        active = [i for i, s in enumerate(compiled["shots"]) if starts[i] <= g < starts[i] + s["frames"]]
        if not active:
            raise ComposeError(f"Image globale {g} couverte par aucun plan : recompilez la scène.")
        layers = []
        for pos, i in enumerate(active):
            local = g - starts[i]
            if pos == 0:
                w = 1.0
            else:
                fade = compiled["shots"][i]["fade_in_frames"]
                if not local < fade:
                    raise ComposeError(f"Image globale {g} : le plan « {shots[i]['id']} » recouvre le précédent "
                                       "hors de son fondu d'entrée (recompilez la scène).")
                w = fade_weight(local, fade)
            layers.append((i, local, w))
        if len(layers) == 1:
            i, local, _ = layers[0]
            mode = "link" if shots[i]["depth"] == depth else "convert"
            src = [[shots[i]["id"], local, 1.0]]
        else:
            mode = "blend"
            # Poids EFFECTIFS de chaque plan dans le mélange successif (somme = 1).
            eff = []
            for pos, (i, local, w) in enumerate(layers):
                share = w
                for _, _, w_next in layers[pos + 1:]:
                    share *= (1.0 - w_next)
                eff.append([shots[i]["id"], local, share])
            src = eff
        tasks.append({"g": g, "mode": mode, "layers": layers, "src": src})

    # Reconstruction de zéro dans un dossier temporaire voisin, puis bascule : en cas d'échec,
    # l'ancien maître reste intact et aucun maître partiel n'est jamais visible sous master/.
    master = config.master_dir(scene_id)
    if config.BUILD_DIR not in master.resolve().parents:
        raise ComposeError(f"Dossier maître inattendu ({master}) hors de {config.BUILD_DIR} : suppression refusée.")
    staging = master.with_name(master.name + ".tmp")
    if staging.exists():
        _rmtree(staging)
    staging.mkdir(parents=True)

    def run(task: dict) -> None:
        dst = staging / config.FRAME_PATTERN.format(task["g"])
        if task["mode"] == "link":
            i, local, _ = task["layers"][0]
            _link(shots[i]["files"][local], dst)
        elif task["mode"] == "convert":
            i, local, _ = task["layers"][0]
            _write_png(dst, convert_depth(_read_png(shots[i]["files"][local]), depth))
        else:
            layers = [(_read_png(shots[i]["files"][local]), w) for i, local, w in task["layers"]]
            _write_png(dst, blend_premultiplied(layers, depth))

    heavy = [t for t in tasks if t["mode"] != "link"]
    try:
        for t in tasks:
            if t["mode"] == "link":
                run(t)
        # Fils d'exécution : cv2 et numpy relâchent le GIL pendant le décodage/encodage PNG et le
        # calcul ; chaque image est indépendante, donc le résultat ne dépend pas de l'ordre.
        # Plafond mémoire : un mélange garde ~8 tableaux float32 pleine taille (≈ 1 Go en 4K), on
        # limite le nombre de fils pour rester sous MIX_MEMORY_BUDGET même sur une machine de 16 Go.
        per_task = compiled["output"]["width"] * compiled["output"]["height"] * 4 * 4 * 8
        workers = max(1, min(8, os.cpu_count() or 1, MIX_MEMORY_BUDGET // max(1, per_task)))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            for fut in [pool.submit(run, t) for t in heavy]:
                fut.result()
    except BaseException:
        _rmtree(staging)
        raise

    # Relevés de mise en page des plans ramenés en images GLOBALES (records + shot_id).
    layout: dict[int, list] = {}
    for info, s in zip(shots, compiled["shots"]):
        for key, records in info["manifest"]["layout"].items():
            g = s["global_start"] + int(key)
            if 0 <= g < total:
                layout.setdefault(g, []).extend({**r, "shot_id": info["id"]} for r in records)

    manifest = {
        "contract": MASTER_CONTRACT,
        "scene_id": scene_id,
        "fps": fps,
        "frames": total,
        "width": compiled["output"]["width"],
        "height": compiled["output"]["height"],
        "depth": depth,
        "alpha": bool(compiled["format"].get("alpha", False)),
        "segments": [{"shot_id": s["id"], "global_start": s["global_start"], "frames": s["frames"],
                      "fade_in_frames": s["fade_in_frames"]} for s in compiled["shots"]],
        "frame_map": [{"g": t["g"], "mode": t["mode"], "src": t["src"]} for t in tasks],
        "layout_every": config.layout_every(fps),
        "layout": {str(g): layout[g] for g in sorted(layout)},
    }
    errs = _schema_errors(manifest, "master_manifest")
    if errs:
        _rmtree(staging)
        raise ComposeError("Manifeste maître non conforme (erreur interne) :\n  - " + "\n  - ".join(errs))

    # Contrôle final AVANT bascule : exactement `total` images, nommées 000000..total-1.
    names = sorted(p.name for p in staging.iterdir() if p.suffix == ".png")
    expected = [config.FRAME_PATTERN.format(g) for g in range(total)]
    if names != expected:
        _rmtree(staging)
        raise ComposeError(f"Séquence maître incomplète : {len(names)} images produites pour {total} attendues.")

    tmp = staging / "manifest.json.tmp"
    tmp.write_text(json.dumps(manifest, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    os.replace(tmp, staging / "manifest.json")

    if master.exists():
        _rmtree(master)
    os.replace(staging, master)

    counts = {m: sum(1 for t in tasks if t["mode"] == m) for m in ("link", "convert", "blend")}
    log(f"[compose] {scene_id} : {total} images {depth} bits -> {master} "
        f"(liens {counts['link']}, conversions {counts['convert']}, fondus {counts['blend']}) "
        f"en {time.perf_counter() - t0:.2f} s")
    return manifest
