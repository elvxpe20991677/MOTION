"""Extraction des images des calques « video » (FFmpeg -> PNG), avant le rendu web d'un plan.

Le navigateur ne lit jamais une vidéo (le décodage d'une balise <video> n'est pas calé sur l'horloge
virtuelle, donc non déterministe) : FFmpeg extrait l'image k du plan = la source à start + k / fps,
déjà cadrée à la taille exacte du calque (fit appliqué ici), puis le runtime l'affiche comme une
plaque 3D. Écriture dans un dossier temporaire puis bascule : jamais d'extraction à moitié faite ;
une extraction complète dont l'empreinte n'a pas changé n'est pas refaite (reprise).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

from pipeline import config


class MediaError(RuntimeError):
    """Erreur d'extraction destinée à l'utilisateur (message actionnable)."""


def _frames_in(d: Path) -> list[Path]:
    return sorted(d.glob("[0-9][0-9][0-9][0-9][0-9][0-9].png"))


def _filter_chain(job: dict) -> str:
    """fps -> mise à l'échelle au cadrage du calque -> RGBA plein."""
    w, h, fit = int(job["width"]), int(job["height"]), job["fit"]
    # lanczos + accurate_rnd : meilleure netteté et arrondis exacts ; matrice source explicite (une
    # vidéo HD non étiquetée serait sinon convertie en BT.601) ; sortie RVB plage pleine.
    sws = f"flags=lanczos+accurate_rnd+full_chroma_int:in_color_matrix={job['color_matrix']}:out_range=full"
    if fit == "cover":
        scale = f"scale={w}:{h}:force_original_aspect_ratio=increase:{sws},crop={w}:{h}"
    elif fit == "contain":
        scale = (f"scale={w}:{h}:force_original_aspect_ratio=decrease:{sws},format=rgba,"
                 f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:color=0x00000000")
    else:
        scale = f"scale={w}:{h}:{sws}"
    return f"fps={int(job['fps'])},{scale},setsar=1,format=rgba"


def extraction_argv(job: dict, out_pattern: str) -> list[str]:
    argv = [config.FFMPEG, "-hide_banner", "-nostdin", "-v", "error", "-y"]
    if job["loop"]:
        argv += ["-stream_loop", "-1"]
    # -ss avant -i : recherche rapide ET exacte (FFmpeg décode depuis l'image clé précédente).
    argv += ["-ss", f"{float(job['start']):.6f}", "-i", job["src"], "-map", "0:v:0", "-an", "-sn", "-dn",
             "-vf", _filter_chain(job), "-frames:v", str(int(job["frames"])), "-start_number", "0",
             "-compression_level", str(config.PNG_COMPRESSION), "-f", "image2", out_pattern]
    return argv


def ensure_job(job: dict, *, log=print) -> Path:
    """Extrait (ou réutilise) les images d'un calque vidéo ; renvoie le dossier des images."""
    out = Path(job["out_dir"])
    stamp = out / "media.json"
    n = int(job["frames"])
    if stamp.is_file() and len(_frames_in(out)) == n:
        try:
            if json.loads(stamp.read_text(encoding="utf-8")).get("digest") == job["digest"]:
                return out
        except (OSError, json.JSONDecodeError):
            pass
    if config.BUILD_DIR not in out.resolve().parents:
        raise MediaError(f"Dossier média inattendu ({out}) hors de {config.BUILD_DIR} : extraction refusée.")
    tmp = out.with_name(out.name + ".tmp")
    if tmp.exists():
        shutil.rmtree(tmp)
    tmp.mkdir(parents=True)
    argv = extraction_argv(job, str(tmp / config.FRAME_PATTERN_FFMPEG))
    log(f"vidéo {Path(job['src']).name} -> {n} images {job['width']}x{job['height']} ({job['fit']}) pour le calque "
        f"« {job['layer_id']} »")
    try:
        r = subprocess.run(argv, cwd=config.ROOT, capture_output=True, text=True, errors="replace")
    except OSError as exc:
        shutil.rmtree(tmp, ignore_errors=True)
        raise MediaError(f"FFmpeg introuvable ({config.FFMPEG} : {exc}).") from exc
    if r.returncode != 0:
        shutil.rmtree(tmp, ignore_errors=True)
        raise MediaError(f"Extraction de {job['src']} impossible (code {r.returncode}) : {r.stderr.strip()[-400:]}")
    frames = _frames_in(tmp)
    if not frames:
        shutil.rmtree(tmp, ignore_errors=True)
        raise MediaError(f"Aucune image extraite de {job['src']} à partir de {job['start']} s : vérifiez « start ».")
    # Source plus courte que le plan (sans boucle) : la dernière image est tenue (copie physique).
    last = frames[-1]
    for k in range(len(frames), n):
        shutil.copyfile(last, tmp / config.FRAME_PATTERN.format(k))
    held = n - len(frames)
    (tmp / "media.json").write_text(json.dumps({"digest": job["digest"], "frames": n, "held_frames": held,
                                                "argv": argv}, ensure_ascii=False, indent=2) + "\n",
                                    encoding="utf-8")
    if out.exists():
        shutil.rmtree(out)
    os.replace(tmp, out)
    if held:
        log(f"  {held} dernière(s) image(s) tenue(s) : la source est plus courte que le plan")
    return out


def ensure_shot_media(compiled: dict, shot_index: int, *, log=print) -> list[Path]:
    """Toutes les vidéos d'un plan, prêtes avant son rendu web."""
    return [ensure_job(job, log=log) for job in compiled["shots"][shot_index].get("media") or []]
