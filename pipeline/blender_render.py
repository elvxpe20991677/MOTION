"""Lanceur des plaques 3D : valide la tâche, lance le backend bpy (Python 3.11), relaie, vérifie.

API (docs/CONTRACT.md §4) :
    render_plate(job, *, log=print) -> Path          écrit job.json, lance bpy, relaie, vérifie
    render_scene_plates(compiled, *, log=print) -> list[Path]

Extension compatible (argument nommé facultatif) : render_plate(job, frames=[0, 12]) ne rend et ne
vérifie que ces images (reprise ciblée, préversion) ; sans lui, toute la plaque est exigée.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable, Iterable

import cv2
import jsonschema
import numpy as np

from pipeline import config

JOB_SCHEMA_PATH: Path = config.SCHEMA_DIR / "internal" / "blender_job.schema.json"
JOB_FILENAME = "job.json"
LOG_FILENAME = "blender.log"

# Ligne de progression exacte émise par le backend après l'écriture atomique de chaque image.
PROGRESS_RE = re.compile(r"^MOGRAPH_BLENDER image (\d+)/(\d+)$")
# Lignes gardées pour le message d'erreur : assez pour voir la trace Python complète du backend.
ERROR_TAIL_LINES = 40
# Nombre maximal d'images fautives détaillées dans un message (le total est toujours indiqué).
MAX_LISTED_PROBLEMS = 12

_validator: jsonschema.protocols.Validator | None = None


class BlenderRenderError(RuntimeError):
    """Échec d'une plaque : message en français indiquant où, quoi et comment corriger."""


# ---------------------------------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------------------------------

def _job_validator() -> jsonschema.protocols.Validator:
    global _validator
    if _validator is None:
        try:
            schema = json.loads(JOB_SCHEMA_PATH.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise BlenderRenderError(f"schéma des tâches Blender illisible ({JOB_SCHEMA_PATH}) : {exc}. Restaurez schema/internal/.") from exc
        # Draft 2020-12 : version déclarée par le schéma lui-même (« $schema »), validée avant usage.
        jsonschema.Draft202012Validator.check_schema(schema)
        _validator = jsonschema.Draft202012Validator(schema)
    return _validator


def validate_job(job: dict) -> None:
    """Lève BlenderRenderError listant chaque écart au schéma (chemin JSON + message)."""
    if not isinstance(job, dict):
        raise BlenderRenderError(f"tâche Blender invalide : un objet JSON (dict) est attendu, reçu {type(job).__name__}.")
    errors = sorted(_job_validator().iter_errors(job), key=lambda e: [str(p) for p in e.absolute_path])
    problems = []
    for err in errors:
        where = "/".join(str(p) for p in err.absolute_path) or "(racine)"
        problems.append(f"  - {where} : {err.message}")
    if not problems and not Path(job["out_dir"]).is_absolute():
        problems.append(f"  - out_dir : chemin relatif {job['out_dir']!r}, un chemin ABSOLU est exigé")
    if problems:
        ident = f"{job.get('scene_id', '?')}/{job.get('shot_id', '?')}/{job.get('plate_id', '?')}"
        raise BlenderRenderError(
            f"tâche Blender {ident} non conforme à {JOB_SCHEMA_PATH.name} ({len(problems)} écart(s)) :\n"
            + "\n".join(problems)
            + "\nCorrigez la plaque dans la scène (ou pipeline/scene.py) puis recompilez."
        )


# ---------------------------------------------------------------------------------------------------
# Fichiers
# ---------------------------------------------------------------------------------------------------

def _write_atomic(path: Path, text: str) -> None:
    # Écriture .tmp puis os.replace : un job.json n'est jamais lu à moitié écrit, même après une coupure.
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _frame_problem(path: Path, width: int, height: int) -> str | None:
    """None si l'image est un PNG RGBA 16 bits width×height décodable, sinon la raison."""
    if not path.is_file():
        return "absente"
    # np.fromfile + imdecode : cv2.imread échoue sous Windows sur les chemins non ASCII.
    try:
        buf = np.fromfile(str(path), dtype=np.uint8)
    except OSError as exc:
        return f"illisible ({exc})"
    img = cv2.imdecode(buf, cv2.IMREAD_UNCHANGED) if buf.size else None
    if img is None:
        return "indécodable (PNG tronqué ou corrompu)"
    if img.ndim != 3 or img.shape[2] != 4:
        return f"pas en RGBA (forme {img.shape})"
    if img.dtype != np.uint16:
        return f"profondeur {img.dtype} au lieu de 16 bits"
    if img.shape[:2] != (height, width):
        return f"taille {img.shape[1]}x{img.shape[0]} au lieu de {width}x{height}"
    return None


def verify_plate(job: dict, frames: Iterable[int] | None = None) -> None:
    """Vérifie que les images attendues existent et se décodent en RGBA 16 bits à la bonne taille."""
    out_dir = Path(job["out_dir"])
    width, height = int(job["width"]), int(job["height"])
    wanted = list(range(int(job["frames"]))) if frames is None else sorted(set(frames))
    paths = [out_dir / config.FRAME_PATTERN.format(i) for i in wanted]
    # Décodage en parallèle : cv2 libère le GIL, une plaque 4K de 300 images se vérifie en secondes.
    with ThreadPoolExecutor(max_workers=max(1, min(16, os.cpu_count() or 1))) as pool:
        results = list(pool.map(lambda p: _frame_problem(p, width, height), paths))
    bad = [(i, why) for i, why in zip(wanted, results) if why is not None]
    if bad:
        listed = "\n".join(f"  - {config.FRAME_PATTERN.format(i)} : {why}" for i, why in bad[:MAX_LISTED_PROBLEMS])
        more = f"\n  … et {len(bad) - MAX_LISTED_PROBLEMS} autre(s)" if len(bad) > MAX_LISTED_PROBLEMS else ""
        raise BlenderRenderError(
            f"plaque {job['shot_id']}__{job['plate_id']} incomplète : {len(bad)} image(s) sur {len(wanted)} "
            f"manquante(s) ou invalide(s) dans {out_dir} :\n{listed}{more}\n"
            f"Relancez le rendu : les images valides sont conservées et seules les autres sont refaites "
            f"(journal complet : {out_dir / LOG_FILENAME})."
        )


# ---------------------------------------------------------------------------------------------------
# Lancement
# ---------------------------------------------------------------------------------------------------

def _canonical(job: dict) -> str:
    return json.dumps(job, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _purge_if_job_changed(job: dict, job_path: Path, log: Callable[[str], None], tag: str) -> None:
    """Supprime les images d'une plaque rendue avec une AUTRE tâche (échantillons, matériau, animation...).

    Le backend saute les images présentes (reprise) : sans cette purge, une plaque modifiée à taille égale
    mélangerait images périmées et nouvelles. job.json précédent illisible = traité comme différent.
    """
    if not job_path.exists():
        return
    try:
        previous = json.loads(job_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        previous = None
    if previous is not None and _canonical(previous) == _canonical(job):
        return
    stale = sorted(job_path.parent.glob("[0-9][0-9][0-9][0-9][0-9][0-9]*.png"))
    for p in stale:
        p.unlink()
    log(f"[{tag}] tâche modifiée depuis le dernier rendu : {len(stale)} image(s) périmée(s) supprimée(s)")


def _check_installation() -> None:
    if not Path(config.BLENDER_PYTHON).is_file():
        raise BlenderRenderError(
            f"interpréteur Blender introuvable : {config.BLENDER_PYTHON}. Installez-le depuis la racine du projet : "
            "« uv venv --python 3.11 .venv-blender » puis « uv pip install --python .venv-blender/Scripts/python.exe bpy==5.0.1 »."
        )
    if not Path(config.BLENDER_BACKEND).is_file():
        raise BlenderRenderError(f"backend introuvable : {config.BLENDER_BACKEND}. Restaurez backends/blender_backend.py.")


def render_plate(job: dict, *, log: Callable[[str], None] = print, frames: list[int] | None = None) -> Path:
    """Rend une plaque (toutes ses images, ou `frames`) et renvoie son dossier une fois vérifiée."""
    validate_job(job)
    _check_installation()
    out_dir = Path(job["out_dir"])
    total = int(job["frames"])
    if frames is not None:
        bad = sorted(f for f in frames if not 0 <= int(f) < total)
        if bad or not frames:
            raise BlenderRenderError(f"frames {bad or '[]'} hors de la plaque (images valides : 0 à {total - 1}).")
    tag = f"{job['shot_id']}__{job['plate_id']}"
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise BlenderRenderError(f"impossible de créer {out_dir} : {exc}. Vérifiez les droits ou MOGRAPH_BUILD_DIR.") from exc
    job_path = out_dir / JOB_FILENAME
    _purge_if_job_changed(job, job_path, log, tag)
    _write_atomic(job_path, json.dumps(job, ensure_ascii=False, indent=2) + "\n")

    cmd = [str(config.BLENDER_PYTHON), str(config.BLENDER_BACKEND), str(job_path)]
    if frames is not None:
        cmd += ["--frames", ",".join(str(int(f)) for f in sorted(set(frames)))]
    env = dict(os.environ)
    # Sortie non tamponnée et en UTF-8 : progression relayée en direct, accents français intacts.
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    log(f"[{tag}] lancement du backend Blender ({job['width']}x{job['height']}, "
        f"{len(frames) if frames is not None else total} image(s), périphérique {job['render']['device']})")

    tail: deque[str] = deque(maxlen=ERROR_TAIL_LINES)
    log_path = out_dir / LOG_FILENAME
    log_tmp = log_path.with_name(log_path.name + ".tmp")
    proc: subprocess.Popen | None = None
    try:
        with open(log_tmp, "w", encoding="utf-8", newline="\n") as log_file:
            # stderr fusionné dans stdout : l'ordre des messages et des erreurs est conservé dans le journal.
            proc = subprocess.Popen(
                cmd, cwd=str(config.ROOT), env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace", bufsize=1,
            )
            assert proc.stdout is not None
            for raw in proc.stdout:
                line = raw.rstrip("\r\n")
                log_file.write(line + "\n")
                log_file.flush()
                if not line.strip():
                    continue
                tail.append(line)
                m = PROGRESS_RE.match(line)
                if m:
                    log(f"[{tag}] image {m.group(1)}/{m.group(2)} rendue")
                else:
                    log(f"[{tag}] {line}")
            code = proc.wait()
    finally:
        # Processus toujours fermé, même sur interruption (Ctrl+C) ou exception pendant le relais.
        if proc is not None and proc.poll() is None:
            proc.kill()
            proc.wait()
        if log_tmp.exists():
            os.replace(log_tmp, log_path)

    if code != 0:
        raise BlenderRenderError(
            f"le backend Blender a échoué (code {code}) pour la plaque {tag}. Dernières lignes :\n"
            + "\n".join(f"  | {t}" for t in tail)
            + f"\nJournal complet : {log_path}. Tâche rejouable : {config.BLENDER_PYTHON} {config.BLENDER_BACKEND} {job_path}"
        )
    verify_plate(job, frames)
    log(f"[{tag}] plaque vérifiée : {len(frames) if frames is not None else total} image(s) RGBA 16 bits "
        f"{job['width']}x{job['height']} dans {out_dir}")
    return out_dir


def render_scene_plates(compiled: dict, *, log: Callable[[str], None] = print) -> list[Path]:
    """Rend toutes les plaques de tous les plans d'une scène compilée, dans l'ordre des plans."""
    jobs = [job for shot in compiled.get("shots", []) for job in (shot.get("plates") or [])]
    if not jobs:
        log("aucune plaque 3D dans cette scène")
        return []
    results: list[Path] = []
    for n, job in enumerate(jobs, start=1):
        log(f"plaque {n}/{len(jobs)} : {job.get('shot_id')}/{job.get('plate_id')}")
        results.append(render_plate(job, log=log))
    return results
