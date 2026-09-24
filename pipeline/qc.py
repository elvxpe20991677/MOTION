"""Contrôle qualité automatique des livrables d'une scène (étape 17).

Chaque livrable de ``compiled["outputs"]`` est inspecté par ffprobe (avec ``-count_frames`` : les
images sont COMPTÉES en décodant, pas lues dans l'en-tête) puis par des passes FFmpeg de contenu
(alphaextract, blackdetect, ebur128). S'y ajoutent deux contrôles de scène : zones sûres (relevés
``__layout`` du manifeste maître) et résultat du test de déterminisme.

Format d'un contrôle (CONTRACT §4) :
    {"id", "label", "status": "PASS"|"FAIL"|"WARN"|"SKIP", "expected", "actual", "blocking", "detail"}
Convention : un contrôle bloquant en échec est ``FAIL`` + ``blocking=True`` ; un constat purement
informatif est ``WARN`` + ``blocking=False`` ; le verdict est « NON CONFORME » dès qu'un contrôle
``FAIL`` bloquant existe.

Sorties : ``out/<scène>/qc_report.json`` et ``out/<scène>/qc_report.md`` (verdict en tête).
CLI : ``python -m pipeline.qc build/<scène>/compiled.json`` (code 0 si CONFORME, 1 si NON CONFORME,
2 si le QC n'a pas pu s'exécuter).
"""

from __future__ import annotations

import argparse
import concurrent.futures
import datetime as _dt
import functools
import json
import os
import re
import subprocess
import sys
import threading
from fractions import Fraction
from pathlib import Path
from typing import Any, Callable

import numpy as np
from jsonschema import Draft202012Validator
from referencing import Registry, Resource

from pipeline import config

# ---------------------------------------------------------------------------
# Constantes propres au QC (aucune n'influence les pixels livrés)
# ---------------------------------------------------------------------------

REPORT_CONTRACT = "mograph-qc/1"
VERDICT_OK = "CONFORME"
VERDICT_KO = "NON CONFORME"
STATUSES = ("PASS", "FAIL", "WARN", "SKIP")

# Le décodeur ProRes de FFmpeg restitue TOUJOURS le 4444 en 12 bits (yuva444p12le), quel que soit
# le nombre de bits encodés : on accepte donc le format de config.PROFILES ET son équivalent décodé.
_DECODED_PIX_FMT_EQUIVALENTS: dict[str, tuple[str, ...]] = {
    "yuva444p10le": ("yuva444p10le", "yuva444p12le"),
}

# Codec audio attendu par conteneur (SPEC §15) : AAC dans le MP4 de diffusion, PCM 24 bits dans le
# MOV de post-production ; un conteneur inconnu accepte l'un ou l'autre.
_AUDIO_CODEC_BY_EXT: dict[str, tuple[str, ...]] = {
    ".mp4": ("aac",),
    ".mov": ("pcm_s24le",),
}
_AUDIO_CODECS_ANY: tuple[str, ...] = ("aac", "pcm_s24le")

# Métadonnées couleur attendues (SPEC §15 : Rec.709 x 3 + plage limitée « tv »).
_COLOR_EXPECTED: dict[str, str] = {
    "color_primaries": "bt709",
    "color_transfer": "bt709",
    "color_space": "bt709",
    "color_range": "tv",
}

# Délais proportionnels à la longueur : -count_frames et blackdetect décodent TOUT le fichier
# (une minute de ProRes 4444 en 4K se décode en quelques dizaines de secondes sur la machine cible),
# une marge large évite les faux échecs sans laisser un processus bloqué indéfiniment.
_TIMEOUT_BASE_S = 300.0
_TIMEOUT_PER_FRAME_S = 0.5
_PROBE_TIMEOUT_DEFAULT_S = 1800.0

# Comparaisons de durées en flottant : 1 µs absorbe les arrondis décimaux de ffprobe (6 décimales).
_FLOAT_EPS = 1e-6

_MANIFEST_SCHEMA_PATH: Path = config.SCHEMA_DIR / "internal" / "manifests.schema.json"

# Clés de la scène compilée lues par le QC (CONTRACT §4) : leur absence rend le QC impossible.
_REQUIRED_COMPILED_KEYS = ("scene_id", "format", "output", "frames", "outputs", "qc")
_REQUIRED_QC_KEYS = ("safe_zone", "safe_zone_tolerance_s", "allow_black_s", "loudness_target_lufs")


class QCError(Exception):
    """Erreur empêchant un contrôle de s'exécuter (outil absent, fichier illisible, entrée invalide)."""


# ---------------------------------------------------------------------------
# Utilitaires
# ---------------------------------------------------------------------------


def _timeout(frames: int) -> float:
    return _TIMEOUT_BASE_S + _TIMEOUT_PER_FRAME_S * max(0, int(frames))


def _run(argv: list[str], *, timeout: float) -> subprocess.CompletedProcess:
    """Lance un outil FFmpeg sans stdin et capture ses sorties en octets.

    subprocess.run tue puis attend l'enfant si le délai est dépassé : aucun processus orphelin.
    """
    kwargs: dict[str, Any] = {}
    if config.IS_WINDOWS:
        # Pas de fenêtre console qui s'ouvre quand le QC tourne dans un processus détaché (--detach).
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    try:
        return subprocess.run(
            argv, stdin=subprocess.DEVNULL, capture_output=True, timeout=timeout, check=False, **kwargs
        )
    except FileNotFoundError as exc:
        raise QCError(
            f"Outil introuvable : {argv[0]!r}. Installez FFmpeg (build « full » avec ffprobe) dans le PATH "
            "ou indiquez son chemin via MOGRAPH_FFMPEG / MOGRAPH_FFPROBE."
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise QCError(
            f"Délai dépassé ({timeout:.0f} s) pour « {Path(argv[0]).name} » sur {argv[-1] if argv else '?'} : "
            "vérifiez que le fichier n'est pas corrompu et qu'il n'est pas sur un disque réseau lent."
        ) from exc


def _text(data: bytes) -> str:
    # FFmpeg écrit en UTF-8 ; « replace » évite qu'un nom de fichier exotique fasse planter le QC.
    return data.decode("utf-8", errors="replace")


def _tail(text: str, lines: int = 6) -> str:
    return " | ".join(line.strip() for line in text.strip().splitlines()[-lines:] if line.strip())


def _float(value: Any) -> float | None:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if f == f else None  # NaN -> None


def _int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _parse_rate(value: Any) -> Fraction | None:
    """« 25/1 » -> Fraction(25) ; « 0/0 » ou valeur illisible -> None."""
    try:
        num, den = str(value).split("/")
        den_i = int(den)
        return Fraction(int(num), den_i) if den_i else None
    except (ValueError, ZeroDivisionError):
        return None


def _check(cid: str, label: str, status: str, expected: Any, actual: Any, blocking: bool,
           detail: str = "") -> dict:
    """Construit un contrôle au format CONTRACT §4 (clés fixes, valeurs sérialisables en JSON)."""
    if status not in STATUSES:
        raise ValueError(f"statut de contrôle invalide : {status!r}")
    return {
        "id": cid,
        "label": label,
        "status": status,
        "expected": expected,
        "actual": actual,
        "blocking": bool(blocking),
        "detail": detail,
    }


def _write_atomic(path: Path, text: str) -> None:
    """Écriture atomique : un lecteur ne voit jamais un rapport à moitié écrit."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8", newline="\n")
    os.replace(tmp, path)


def _streams(info: dict, kind: str) -> list[dict]:
    return [s for s in info.get("streams", []) if s.get("codec_type") == kind]


@functools.lru_cache(maxsize=None)
def _tool_version(exe: str) -> str:
    """Première ligne de « -version » (tracée dans le rapport pour la reproductibilité)."""
    try:
        res = _run([exe, "-hide_banner", "-version"], timeout=30)
    except QCError as exc:
        return f"indisponible ({exc})"
    first = _text(res.stdout).strip().splitlines()
    # Seule la version est utile au rapport : la mention de copyright est retirée.
    return first[0].split(" Copyright")[0] if first else "inconnue"


# ---------------------------------------------------------------------------
# Sondes FFmpeg
# ---------------------------------------------------------------------------


def probe(path, *, timeout: float | None = None) -> dict:
    """ffprobe -count_frames -show_streams -show_format (JSON) ; lève QCError si illisible."""
    p = Path(path)
    if not p.is_file():
        raise QCError(
            f"Livrable introuvable : {p}. Lancez l'encodage (python mograph.py encode <scène.json>) "
            "puis relancez le QC."
        )
    # -count_frames : nb_read_frames est obtenu en décodant réellement le flux, ce qui détecte un
    # fichier tronqué que nb_frames (lu dans l'en-tête du conteneur) masquerait.
    argv = [config.FFPROBE, "-v", "error", "-count_frames", "-show_streams", "-show_format",
            "-of", "json", str(p)]
    res = _run(argv, timeout=timeout or _PROBE_TIMEOUT_DEFAULT_S)
    stderr = _text(res.stderr)
    if res.returncode != 0:
        raise QCError(
            f"ffprobe n'a pas pu lire {p} (code {res.returncode}) : {_tail(stderr)}. "
            "Le fichier est probablement tronqué ou d'un format inattendu : ré-encodez-le."
        )
    try:
        info = json.loads(_text(res.stdout))
    except json.JSONDecodeError as exc:
        raise QCError(f"Sortie JSON de ffprobe illisible pour {p} : {exc}. Vérifiez la version de ffprobe.") from exc
    if not info.get("streams"):
        raise QCError(f"Aucun flux dans {p} : le fichier est vide ou corrompu, ré-encodez-le.")
    return info


def _alpha_stats(path: Path, frame_index: int, fps: int, width: int, height: int,
                 start_time: float, timeout: float) -> dict:
    """Extrait le plan alpha d'UNE image (alphaextract) et renvoie min et part de pixels < 255."""
    # Recherche d'entrée (-ss avant -i) : ProRes est intra-image, le saut est exact et instantané ;
    # on vise un quart d'image AVANT l'image voulue pour ne jamais tomber sur la suivante par arrondi.
    t = max(0.0, start_time + (frame_index - 0.25) / fps)
    argv = [config.FFMPEG, "-hide_banner", "-nostats", "-v", "error",
            "-ss", f"{t:.6f}", "-i", str(path), "-map", "0:v:0", "-frames:v", "1",
            # format=gray : alpha ramené sur 8 bits, le critère du contrat est « valeurs < 255 ».
            "-vf", "alphaextract,format=gray", "-f", "rawvideo", "pipe:1"]
    res = _run(argv, timeout=timeout)
    stderr = _text(res.stderr)
    if res.returncode != 0 or not res.stdout:
        if "planes not available" in stderr.lower():
            raise QCError("le flux n'a PAS de plan alpha (alphaextract : plans demandés indisponibles)")
        raise QCError(f"extraction alpha impossible : {_tail(stderr) or 'aucune image produite'}")
    plane = np.frombuffer(res.stdout, dtype=np.uint8)
    if plane.size != width * height:
        raise QCError(
            f"plan alpha de {plane.size} octets au lieu de {width * height} ({width}x{height}) : "
            "résolution inattendue"
        )
    below = int(np.count_nonzero(plane < 255))
    return {
        "frame": frame_index,
        "min": int(plane.min()),
        "max": int(plane.max()),
        "pixels_below_255": below,
        "fraction_below_255": round(below / plane.size, 6),
    }


_BLACK_RE = re.compile(r"black_start:\s*([-\d.eE+]+)\s+black_end:\s*([-\d.eE+]+)\s+black_duration:\s*([-\d.eE+]+)")


def _blackdetect(path: Path, fps: int, video_duration: float, timeout: float) -> list[dict]:
    """Segments noirs détectés par blackdetect (pix_th = config.BLACKDETECT_PIX_TH, durée min. 0)."""
    # d=0 : on veut TOUS les segments, le seuil de tolérance (allow_black_s) est appliqué ensuite.
    vf = f"blackdetect=d=0:pix_th={config.BLACKDETECT_PIX_TH}"
    # -v info : blackdetect journalise ses segments au niveau info ; -nostats évite la ligne de progression.
    argv = [config.FFMPEG, "-hide_banner", "-nostats", "-v", "info", "-i", str(path),
            "-map", "0:v:0", "-vf", vf, "-f", "null", "-"]
    res = _run(argv, timeout=timeout)
    stderr = _text(res.stderr)
    if res.returncode != 0:
        raise QCError(f"blackdetect a échoué : {_tail(stderr)}")
    segments = []
    for m in _BLACK_RE.finditer(stderr):
        start, end = float(m.group(1)), float(m.group(2))
        # En fin de fichier, blackdetect clôt le segment sur l'horodatage de la DERNIÈRE image (et non
        # sur sa fin) : on étend jusqu'à la fin de la vidéo pour ne pas sous-estimer le noir. Un noir
        # qui s'arrête juste avant la dernière image est indiscernable dans le journal : il est alors
        # surestimé d'une image, erreur volontairement du côté strict.
        if video_duration > 0 and end >= video_duration - 1.25 / fps:
            end = video_duration
        segments.append({"start": round(start, 6), "end": round(end, 6), "seconds": round(max(0.0, end - start), 6)})
    return segments


_LOUD_I_RE = re.compile(r"Integrated loudness:\s*I:\s*(-?inf|-?[\d.]+)\s*LUFS", re.S)
_LOUD_LRA_RE = re.compile(r"Loudness range:\s*LRA:\s*(-?inf|-?[\d.]+)\s*LU", re.S)
_LOUD_TP_RE = re.compile(r"True peak:\s*Peak:\s*(-?inf|-?[\d.]+)\s*dBFS", re.S)


def _loudness(path: Path, timeout: float) -> dict:
    """Mesure EBU R128 (intégré, LRA, true peak) de la première piste audio."""
    # peak=true : true peak suréchantillonné (4x) exigé par la contrainte <= -1 dBTP ;
    # framelog=quiet : seul le résumé final est journalisé, pas une ligne toutes les 100 ms.
    argv = [config.FFMPEG, "-hide_banner", "-nostats", "-v", "info", "-i", str(path),
            "-map", "0:a:0", "-af", "ebur128=peak=true:framelog=quiet", "-f", "null", "-"]
    res = _run(argv, timeout=timeout)
    stderr = _text(res.stderr)
    if res.returncode != 0:
        raise QCError(f"mesure ebur128 impossible : {_tail(stderr)}")
    summary = stderr[stderr.rfind("Summary:"):] if "Summary:" in stderr else ""
    mi, ml, mt = _LOUD_I_RE.search(summary), _LOUD_LRA_RE.search(summary), _LOUD_TP_RE.search(summary)
    if not (mi and mt):
        raise QCError(f"résumé ebur128 introuvable dans la sortie de FFmpeg : {_tail(stderr)}")

    def val(m):
        return float("-inf") if m is None or "inf" in m.group(1) else float(m.group(1))

    return {"integrated_lufs": val(mi), "true_peak_dbtp": val(mt), "lra": val(ml) if ml else None}


# ---------------------------------------------------------------------------
# Attendus d'un livrable
# ---------------------------------------------------------------------------


def _validate_compiled(compiled: dict) -> None:
    missing = [k for k in _REQUIRED_COMPILED_KEYS if k not in compiled]
    if missing:
        raise QCError(
            f"Scène compilée incomplète : clé(s) {', '.join(missing)} absente(s). "
            "Recompilez la scène (python mograph.py validate <scène.json>) avec la version courante du pipeline."
        )
    missing_qc = [k for k in _REQUIRED_QC_KEYS if k not in (compiled.get("qc") or {})]
    if missing_qc:
        raise QCError(
            f"Section qc incomplète dans la scène compilée : {', '.join(missing_qc)} absente(s). "
            "Recompilez la scène : le compilateur complète ces valeurs par défaut."
        )
    for k in ("aspect", "fps"):
        if k not in compiled["format"]:
            raise QCError(f"format.{k} absent de la scène compilée : recompilez la scène.")


def expected_for_output(compiled: dict, output: dict) -> dict:
    """Attendus d'UN livrable déduits de la scène compilée (argument ``expected`` de check_output)."""
    profile = output.get("profile")
    if profile not in config.PROFILES:
        raise QCError(
            f"Profil de livrable inconnu {profile!r} ; profils valides : {', '.join(config.PROFILES)}."
        )
    prof = config.PROFILES[profile]
    fmt = compiled["format"]
    qc = compiled["qc"]
    raw_path = output.get("path")
    if not raw_path:
        raise QCError(f"Le livrable {profile} n'a pas de chemin (outputs[].path) : recompilez la scène.")
    path = Path(raw_path)
    if not path.is_absolute():
        # Un chemin relatif est interprété depuis la racine du projet, comme dans encode_commands.sh.
        path = config.ROOT / path
    audio = compiled.get("audio")
    audio_expected = None
    if audio:
        audio_expected = {
            "codecs": list(_AUDIO_CODEC_BY_EXT.get(path.suffix.lower(), _AUDIO_CODECS_ANY)),
            "sample_rate": config.AUDIO_RATE,
            "target_lufs": float(qc["loudness_target_lufs"]),
            "source_target_lufs": _float(audio.get("target_lufs")),
        }
    return {
        "name": f"{profile}{output.get('suffix') or ''}",
        "profile": profile,
        "path": str(path),
        "codec": prof["codec"],
        "codec_profile": prof["profile"],
        "pix_fmts": list(_DECODED_PIX_FMT_EQUIVALENTS.get(prof["pix_fmt"], (prof["pix_fmt"],))),
        "width": int(compiled["output"]["width"]),
        "height": int(compiled["output"]["height"]),
        "fps": int(fmt["fps"]),
        "frames": int(compiled["frames"]),
        "hvc1": prof["codec"] == "hevc",
        "timecode": prof["codec"] == "prores",
        "timecode_value": config.TIMECODE_START,
        "color": dict(_COLOR_EXPECTED),
        # Alpha réel exigé pour tout format de pixel avec plan alpha (prores_4444).
        "alpha": prof["pix_fmt"].startswith("yuva"),
        "black": {
            "allow_black_s": float(qc["allow_black_s"]),
            # Scène à fond transparent : le noir (transparent aplati ou luma nulle) est attendu -> informatif.
            "informative": bool(fmt.get("alpha")),
        },
        "audio": audio_expected,
    }


# ---------------------------------------------------------------------------
# Contrôles d'UN fichier
# ---------------------------------------------------------------------------


def check_output(path, expected: dict) -> list[dict]:
    """Contrôles d'UN livrable ; ``expected`` est normalement produit par expected_for_output.

    Clés obligatoires : ``fps`` (int), ``frames`` (int). Clés facultatives (absente/None/False =
    contrôle non effectué) : ``name`` (préfixe des id, défaut : nom du fichier), ``codec``,
    ``codec_profile``, ``pix_fmts`` (liste acceptée), ``width``/``height``, ``hvc1`` (bool),
    ``color`` (dict champ ffprobe -> valeur), ``timecode`` (bool) + ``timecode_value``, ``alpha``
    (bool), ``black`` ({allow_black_s, informative}), ``audio`` (None = scène sans audio, sinon
    {codecs, sample_rate, target_lufs, source_target_lufs}).
    """
    p = Path(path)
    name = expected.get("name") or p.stem
    fps = int(expected["fps"])
    frames = int(expected["frames"])
    timeout = _timeout(frames)
    checks: list[dict] = []

    def add(key: str, label: str, status: str, exp: Any, act: Any, blocking: bool = True, detail: str = "") -> None:
        checks.append(_check(f"{name}.{key}", f"{name} : {label}", status, exp, act, blocking, detail))

    # --- Lecture ---------------------------------------------------------------------------------
    try:
        info = probe(p, timeout=timeout)
    except QCError as exc:
        add("readable", "fichier présent et lisible", "FAIL", "fichier lisible par ffprobe", "absent ou illisible",
            True, str(exc))
        return checks
    videos = _streams(info, "video")
    if not videos:
        add("readable", "fichier présent et lisible", "FAIL", "1 flux vidéo", "aucun flux vidéo", True,
            f"{p} ne contient aucun flux vidéo : vérifiez « -map 0:v:0 » dans encode_commands.sh.")
        return checks
    size = p.stat().st_size
    if len(videos) == 1:
        add("readable", "fichier présent et lisible", "PASS", "1 flux vidéo", "1 flux vidéo", True,
            f"{size} octets")
    else:
        add("readable", "fichier présent et lisible", "WARN", "1 flux vidéo", f"{len(videos)} flux vidéo", False,
            f"{size} octets ; seul le premier flux vidéo est contrôlé, les lecteurs peuvent choisir un autre flux.")
    v = videos[0]

    # --- Codec et profil -------------------------------------------------------------------------
    if expected.get("codec"):
        act = v.get("codec_name")
        ok = act == expected["codec"]
        add("codec", "codec vidéo", "PASS" if ok else "FAIL", expected["codec"], act, True,
            "" if ok else f"Codec {act} au lieu de {expected['codec']} : vérifiez « -c:v » (libx265 / prores_ks) "
                          "dans out/<scène>/encode_commands.sh puis ré-encodez.")
    if expected.get("codec_profile"):
        act = v.get("profile")
        ok = act == expected["codec_profile"]
        add("profile", "profil du codec", "PASS" if ok else "FAIL", expected["codec_profile"], act, True,
            "" if ok else "Profil inattendu : « -profile:v main10 » pour HEVC, « -profile:v 3 » (HQ) ou "
                          "« -profile:v 4 » (4444) pour prores_ks.")

    # --- Format de pixel -------------------------------------------------------------------------
    if expected.get("pix_fmts"):
        act = v.get("pix_fmt")
        ok = act in expected["pix_fmts"]
        add("pix_fmt", "format de pixel", "PASS" if ok else "FAIL", " ou ".join(expected["pix_fmts"]), act, True,
            "" if ok else "Format de pixel inattendu : la chaîne vidéo doit finir par « format=<pix_fmt> » et "
                          "l'encodeur recevoir « -pix_fmt <pix_fmt> » (voir config.PROFILES).")

    # --- Résolution ------------------------------------------------------------------------------
    if expected.get("width") and expected.get("height"):
        exp_res = f"{expected['width']}x{expected['height']}"
        act_res = f"{v.get('width')}x{v.get('height')}"
        ok = exp_res == act_res
        add("resolution", "résolution", "PASS" if ok else "FAIL", exp_res, act_res, True,
            "" if ok else "Taille différente de compiled.output : vérifiez device_scale_factor et la séquence "
                          "maître (build/<scène>/master) avant de ré-encoder.")

    # --- Cadence ---------------------------------------------------------------------------------
    r = _parse_rate(v.get("r_frame_rate"))
    a = _parse_rate(v.get("avg_frame_rate"))
    ok = r == fps and a == fps
    add("fps", "cadence r_frame_rate et avg_frame_rate", "PASS" if ok else "FAIL", f"{fps}/1",
        f"r_frame_rate={v.get('r_frame_rate')} avg_frame_rate={v.get('avg_frame_rate')}", True,
        "" if ok else f"Cadence différente de {fps} i/s : l'entrée doit être lue avec « -framerate {fps} » "
                      "(sans -r de sortie différent) ; ré-encodez.")

    # --- Nombre d'images COMPTÉ ------------------------------------------------------------------
    counted = _int(v.get("nb_read_frames"))
    ok = counted == frames
    add("frames", "nombre d'images compté (nb_read_frames)", "PASS" if ok else "FAIL", frames, counted, True,
        "" if ok else ("ffprobe n'a pas pu compter les images." if counted is None else
                       f"{counted} images décodées au lieu de {frames} : vérifiez « -frames:v {frames} » et que la "
                       "séquence maître est complète."))

    # --- Durée ± ½ image -------------------------------------------------------------------------
    exp_dur = frames / fps
    dur = _float(v.get("duration"))
    if dur is None:
        dur = _float(info.get("format", {}).get("duration"))
    tol = 0.5 / fps
    ok = dur is not None and abs(dur - exp_dur) <= tol + _FLOAT_EPS
    add("duration", "durée vidéo (± 1/2 image)", "PASS" if ok else "FAIL",
        {"seconds": round(exp_dur, 6), "tolerance_s": round(tol, 6)},
        None if dur is None else round(dur, 6), True,
        "" if ok else "Durée incohérente avec images/cadence : vérifiez -framerate et -frames:v.")

    # --- Tag hvc1 (lecture QuickTime / Apple) ----------------------------------------------------
    if expected.get("hvc1"):
        act = v.get("codec_tag_string")
        ok = act == "hvc1"
        add("hvc1", "étiquette HEVC hvc1", "PASS" if ok else "FAIL", "hvc1", act, True,
            "" if ok else "Étiquette hev1/absente : QuickTime et les appareils Apple refusent le fichier ; "
                          "ajoutez « -tag:v hvc1 ».")

    # --- Métadonnées couleur ---------------------------------------------------------------------
    if expected.get("color"):
        act = {k: v.get(k) for k in expected["color"]}
        bad = [k for k, want in expected["color"].items() if act.get(k) != want]
        add("color", "métadonnées couleur (bt709 x3, plage tv)", "FAIL" if bad else "PASS", expected["color"], act,
            True, "" if not bad else
            f"Champs non conformes : {', '.join(bad)}. Ajoutez « -color_primaries bt709 -color_trc bt709 "
            "-colorspace bt709 -color_range tv » (et +write_colr dans -movflags).")

    # --- Piste timecode tmcd (ProRes) ------------------------------------------------------------
    if expected.get("timecode"):
        tmcd = [s for s in info.get("streams", []) if s.get("codec_tag_string") == "tmcd"]
        want_tc = expected.get("timecode_value")
        if not tmcd:
            add("timecode", "piste timecode tmcd", "FAIL", {"tmcd": True, "timecode": want_tc}, {"tmcd": False}, True,
                f"Aucune piste tmcd : ajoutez « -timecode {want_tc or config.TIMECODE_START} » à la commande ProRes.")
        else:
            tc = (tmcd[0].get("tags") or {}).get("timecode") or (v.get("tags") or {}).get("timecode")
            if want_tc and tc != want_tc:
                add("timecode", "piste timecode tmcd", "WARN", {"tmcd": True, "timecode": want_tc},
                    {"tmcd": True, "timecode": tc}, False,
                    f"Timecode de départ {tc} au lieu de {want_tc} (config.TIMECODE_START) : sans effet sur "
                    "l'image, mais le conformage en montage sera décalé.")
            else:
                add("timecode", "piste timecode tmcd", "PASS", {"tmcd": True, "timecode": want_tc},
                    {"tmcd": True, "timecode": tc}, True)

    # --- Alpha réel (4444) -----------------------------------------------------------------------
    if expected.get("alpha"):
        median = frames // 2
        start_time = _float(v.get("start_time")) or 0.0
        try:
            stats = _alpha_stats(p, median, fps, int(v.get("width") or 0), int(v.get("height") or 0),
                                 start_time, timeout)
            ok = stats["min"] < 255
            add("alpha", "alpha réel (image médiane)", "PASS" if ok else "FAIL",
                {"frame": median, "alpha_min_below": 255}, stats, True,
                "" if ok else f"Alpha entièrement opaque sur l'image {median} : la transparence a été perdue. "
                              "Vérifiez que la séquence maître est en RGBA (fond transparent) et que la chaîne "
                              "finit par format=yuva444p10le avec « -alpha_bits 16 ».")
        except QCError as exc:
            add("alpha", "alpha réel (image médiane)", "FAIL", {"frame": median, "alpha_min_below": 255}, None,
                True, f"{exc}. Un livrable prores_4444 doit porter un plan alpha (pix_fmt yuva444p10le).")

    # --- Noir non voulu --------------------------------------------------------------------------
    black = expected.get("black")
    if black is not None:
        allow = float(black.get("allow_black_s", 0.0))
        informative = bool(black.get("informative"))
        vid_dur = dur if dur is not None else exp_dur
        exp_black = {"max_black_s": allow, "pix_th": config.BLACKDETECT_PIX_TH, "informative": informative}
        try:
            segs = _blackdetect(p, fps, vid_dur, timeout)
            # Critère : durée noire TOTALE (somme des segments) comparée à allow_black_s ; un budget total
            # est plus simple à régler qu'un maximum par segment et ne laisse pas passer des noirs répétés.
            total = round(float(sum(s["seconds"] for s in segs)), 6)
            act = {"black_s": total, "segments": segs}
            if informative:
                add("black", "noir (informatif : scène à fond transparent)", "WARN" if total > 0 else "PASS",
                    exp_black, act, False,
                    "" if total <= 0 else f"{total:.3f} s de noir détectées ; attendu pour une scène à alpha "
                                          "(zones transparentes aplaties sur noir) : à vérifier à l'œil.")
            elif total > allow + _FLOAT_EPS:
                add("black", "noir non voulu (blackdetect)", "FAIL", exp_black, act, True,
                    f"{total:.3f} s de noir (> {allow:g} s autorisées) : "
                    + ", ".join(f"{s['start']:.3f}-{s['end']:.3f} s" for s in segs[:8])
                    + ". Vérifiez les plans concernés ou augmentez qc.allow_black_s si ce noir est voulu.")
            else:
                add("black", "noir non voulu (blackdetect)", "PASS", exp_black, act, True,
                    "" if total <= 0 else f"{total:.3f} s de noir, dans la tolérance de {allow:g} s.")
        except QCError as exc:
            add("black", "noir non voulu (blackdetect)", "WARN" if informative else "FAIL", exp_black, None,
                not informative, f"Analyse blackdetect impossible : {exc}")

    # --- Audio -----------------------------------------------------------------------------------
    aexp = expected.get("audio")
    astreams = _streams(info, "audio")
    if aexp is None:
        if astreams:
            add("audio", "piste audio", "WARN", "aucune (scène sans audio)", f"{len(astreams)} piste(s)", False,
                "La scène ne déclare pas d'audio mais le fichier en contient : ancien encodage ? Ré-encodez.")
        else:
            add("audio", "piste audio", "SKIP", "aucune (scène sans audio)", "aucune", False, "Scène sans audio.")
        add("loudness", "loudness EBU R128", "SKIP", None, None, False, "Scène sans audio : aucune mesure.")
        return checks

    a_tol = 1.0 / fps + config.AAC_PRIMING_S
    exp_audio = {"codec": " ou ".join(aexp["codecs"]), "sample_rate": aexp["sample_rate"],
                 "duration_s": round(exp_dur, 6), "tolerance_s": round(a_tol, 6)}
    exp_loud = {"integrated_lufs": aexp["target_lufs"], "tolerance_lu": config.LOUDNESS_TOLERANCE_LU,
                "true_peak_max_dbtp": config.TRUE_PEAK_MAX_DBTP}
    if not astreams:
        add("audio", "piste audio", "FAIL", exp_audio, "aucune piste audio", True,
            "La scène a un audio mais le livrable n'en contient pas : vérifiez « -map 1:a:0 » et le chemin "
            "audio.src (ou lancez la génération de la piste de test).")
        add("loudness", "loudness EBU R128", "SKIP", exp_loud, None, False, "Pas de piste audio à mesurer.")
        return checks
    ast = astreams[0]
    a_codec = ast.get("codec_name")
    a_rate = _int(ast.get("sample_rate"))
    a_dur = _float(ast.get("duration"))
    problems = []
    if a_codec not in aexp["codecs"]:
        problems.append(f"codec {a_codec} au lieu de {' ou '.join(aexp['codecs'])} (-c:a aac pour MP4, "
                        "-c:a pcm_s24le pour MOV)")
    if a_rate != aexp["sample_rate"]:
        problems.append(f"fréquence {a_rate} Hz au lieu de {aexp['sample_rate']} Hz (-ar {aexp['sample_rate']})")
    if a_dur is None or abs(a_dur - exp_dur) > a_tol + _FLOAT_EPS:
        problems.append(f"durée {a_dur} s au lieu de {exp_dur:.6f} s ± {a_tol:.3f} s "
                        "(filtre « apad=whole_dur=D,atrim=end=D » avec D = images / fps)")
    add("audio", "piste audio (codec, 48 kHz, durée)", "FAIL" if problems else "PASS", exp_audio,
        {"codec": a_codec, "sample_rate": a_rate, "duration_s": None if a_dur is None else round(a_dur, 6)},
        True, "; ".join(problems))

    try:
        loud = _loudness(p, timeout)
        integ, tp = loud["integrated_lufs"], loud["true_peak_dbtp"]
        lproblems = []
        if not abs(integ - aexp["target_lufs"]) <= config.LOUDNESS_TOLERANCE_LU + _FLOAT_EPS:
            lproblems.append(f"intégré {integ:.1f} LUFS au lieu de {aexp['target_lufs']:.1f} "
                             f"± {config.LOUDNESS_TOLERANCE_LU:g} LU (corrigez le gain de la piste source : "
                             f"{aexp['target_lufs'] - integ:+.1f} dB)")
        if not tp <= config.TRUE_PEAK_MAX_DBTP + _FLOAT_EPS:
            lproblems.append(f"true peak {tp:.1f} dBTP > {config.TRUE_PEAK_MAX_DBTP:g} dBTP (baissez le gain ou "
                             "appliquez un limiteur true peak à la source)")
        note = ""
        src_target = aexp.get("source_target_lufs")
        if src_target is not None and abs(src_target - aexp["target_lufs"]) > _FLOAT_EPS:
            note = (f" Note : la piste de synthèse vise {src_target:g} LUFS alors que qc.loudness_target_lufs "
                    f"vaut {aexp['target_lufs']:g} LUFS ; le QC applique la cible qc.")
        add("loudness", "loudness EBU R128 (intégré, true peak)", "FAIL" if lproblems else "PASS", exp_loud,
            loud, True, ("; ".join(lproblems) + "." if lproblems else "") + note)
    except QCError as exc:
        add("loudness", "loudness EBU R128 (intégré, true peak)", "FAIL", exp_loud, None, True,
            f"Mesure impossible : {exc}")
    return checks


# ---------------------------------------------------------------------------
# Contrôles de scène : zones sûres et déterminisme
# ---------------------------------------------------------------------------


@functools.lru_cache(maxsize=None)
def _manifest_validator(defname: str) -> Draft202012Validator:
    """Validateur d'une définition de manifests.schema.json (référentiel « referencing », sans réseau)."""
    try:
        schema = json.loads(_MANIFEST_SCHEMA_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise QCError(f"Schéma {_MANIFEST_SCHEMA_PATH} illisible : {exc}. Restaurez-le depuis git.") from exc
    registry = Registry().with_resource(schema["$id"], Resource.from_contents(schema))
    return Draft202012Validator({"$ref": f"{schema['$id']}#/$defs/{defname}"}, registry=registry)


def _schema_errors(doc: Any, defname: str, limit: int = 5) -> list[str]:
    errs = sorted(_manifest_validator(defname).iter_errors(doc), key=lambda e: list(map(str, e.absolute_path)))
    return [f"{'/'.join(map(str, e.absolute_path)) or '(racine)'} : {e.message}" for e in errs[:limit]]


def _load_json(path: Path) -> tuple[Any, str | None]:
    """(document, None) ou (None, message d'erreur actionnable)."""
    try:
        return json.loads(path.read_text(encoding="utf-8")), None
    except FileNotFoundError:
        return None, f"{path} introuvable"
    except (OSError, json.JSONDecodeError) as exc:
        return None, f"{path} illisible : {exc}"


def _overflow(box: list[float], rect: dict) -> float:
    """Dépassement maximal (px logiques) d'une boîte [x, y, l, h] hors du rectangle ; <= 0 si dedans."""
    x, y, w, h = (float(b) for b in box)
    return max(rect["left"] - x, rect["top"] - y, (x + w) - rect["right"], (y + h) - rect["bottom"])


def _visible(box: list[float], canvas: tuple[int, int]) -> bool:
    """Vrai si la boîte intersecte le canevas : un texte garé entièrement hors champ n'est pas vu."""
    x, y, w, h = (float(b) for b in box)
    cw, ch = canvas
    return x < cw and y < ch and x + w > 0 and y + h > 0


def analyse_safe_zones(manifest: dict, rect: dict, *, canvas: tuple[int, int] | None = None,
                       eps: float = config.SAFE_ZONE_EPSILON_PX) -> list[dict]:
    """Excursions CONTINUES hors du rectangle sûr, par (plan, calque), triées par durée décroissante.

    Les relevés sont échantillonnés (toutes les ``layout_every`` images locales + dernière image de
    chaque plan) : chaque relevé représente l'intervalle qui le sépare du relevé SUIVANT du même
    plan (échantillonneur bloqueur, borné à ``layout_every`` images). Une excursion couvrant 3 relevés
    espacés de 5 images vaut donc 15 images. Les échantillons sont ceux du PLAN du relevé : dans un
    fondu, les deux plans sont relevés à des instants globaux décalés et ne doivent pas s'interrompre.
    """
    fps = int(manifest["fps"])
    total = int(manifest["frames"])
    every = max(1, int(manifest["layout_every"]))
    layout = {int(k): v for k, v in (manifest.get("layout") or {}).items()}
    segments = {s["shot_id"]: s for s in manifest.get("segments", [])}

    # Instants de relevé théoriques de chaque plan (contrat du shot_manifest), en images globales.
    shot_samples: dict[str, set[int]] = {}
    for sid, seg in segments.items():
        local = set(range(0, int(seg["frames"]), every))
        local.add(int(seg["frames"]) - 1)
        shot_samples[sid] = {int(seg["global_start"]) + i for i in local}

    # Regroupement des relevés par (plan, calque) ; un relevé sans plan connu utilise la grille globale.
    series: dict[tuple[str, str], dict[int, list[dict]]] = {}
    for g, records in layout.items():
        for rec in records:
            sid = rec.get("shot_id")
            key = (sid if sid in segments else "", str(rec.get("id")))
            series.setdefault(key, {}).setdefault(g, []).append(rec)
    all_keys = sorted(layout)

    excursions: list[dict] = []
    for (sid, eid), by_frame in sorted(series.items()):
        if sid:
            seg = segments[sid]
            samples = sorted(shot_samples[sid] | set(by_frame))
            end_limit = int(seg["global_start"]) + int(seg["frames"])
        else:
            samples = all_keys
            end_limit = total
        run: dict | None = None
        for i, g in enumerate(samples):
            worst = None
            for rec in by_frame.get(g, []):
                box = rec.get("box") or [0, 0, 0, 0]
                if canvas is not None and not _visible(box, canvas):
                    continue
                ov = _overflow(box, rect)
                if ov > eps and (worst is None or ov > worst[0]):
                    worst = (ov, rec)
            if worst is None:
                if run is not None:
                    excursions.append(run)
                    run = None
                continue
            nxt = samples[i + 1] if i + 1 < len(samples) else end_limit
            sample_end = min(nxt, g + every, end_limit)
            if run is None:
                run = {"shot_id": sid or None, "id": eid, "text": str(worst[1].get("text", ""))[:80],
                       "start_frame": g, "end_frame": sample_end, "max_overflow_px": 0.0,
                       "worst_box": None, "worst_frame": g}
            run["end_frame"] = max(run["end_frame"], sample_end)
            if worst[0] > run["max_overflow_px"]:
                run["max_overflow_px"] = round(worst[0], 3)
                run["worst_box"] = [round(float(b), 3) for b in worst[1]["box"]]
                run["worst_frame"] = g
        if run is not None:
            excursions.append(run)
    for e in excursions:
        e["frames"] = e["end_frame"] - e["start_frame"]
        e["seconds"] = round(e["frames"] / fps, 6)
    excursions.sort(key=lambda e: (-e["seconds"], e["start_frame"], e["id"]))
    return excursions


def check_safe_zones(compiled: dict) -> dict:
    """Zones sûres d'après build/<scène>/master/manifest.json (bloquant au-delà d'une durée continue)."""
    scene_id = compiled["scene_id"]
    qc = compiled["qc"]
    zone = qc["safe_zone"]
    tol = float(qc["safe_zone_tolerance_s"])
    cid, label = "scene.safe_zone", "scène : zones sûres (relevés __layout)"
    if zone == "off":
        return _check(cid, label, "SKIP", "qc.safe_zone = off", None, False, "Contrôle désactivé par la scène.")
    aspect = compiled["format"]["aspect"]
    rect = config.safe_rect(aspect, zone)
    exp = {"zone": zone, "rect_px": [round(rect[k], 3) for k in ("left", "top", "right", "bottom")],
           "max_continuous_out_s": tol, "epsilon_px": config.SAFE_ZONE_EPSILON_PX}
    path = config.master_dir(scene_id) / "manifest.json"
    manifest, err = _load_json(path)
    if err:
        return _check(cid, label, "FAIL", exp, None, True,
                      f"Manifeste maître {err} : lancez la composition (python mograph.py render <scène.json>) "
                      "avant le QC.")
    errors = _schema_errors(manifest, "master_manifest")
    if errors:
        return _check(cid, label, "FAIL", exp, None, True,
                      f"{path} ne respecte pas manifests.schema.json#/$defs/master_manifest : " + " ; ".join(errors)
                      + ". Recomposez la séquence maître.")
    stale = []
    if manifest["scene_id"] != scene_id:
        stale.append(f"scene_id {manifest['scene_id']} au lieu de {scene_id}")
    if int(manifest["fps"]) != int(compiled["format"]["fps"]):
        stale.append(f"fps {manifest['fps']} au lieu de {compiled['format']['fps']}")
    if int(manifest["frames"]) != int(compiled["frames"]):
        stale.append(f"{manifest['frames']} images au lieu de {compiled['frames']}")
    if stale:
        return _check(cid, label, "FAIL", exp, None, True,
                      f"Manifeste maître périmé ({', '.join(stale)}) : relancez le rendu et la composition.")
    excursions = analyse_safe_zones(manifest, rect, canvas=config.LOGICAL_CANVAS.get(aspect))
    worst = excursions[0]["seconds"] if excursions else 0.0
    blocking = [e for e in excursions if e["seconds"] > tol + _FLOAT_EPS]
    samples = sum(len(v) for v in manifest["layout"].values())
    actual = {"max_continuous_out_s": worst, "excursions": len(excursions), "records": samples,
              "worst": excursions[:5]}

    def describe(e: dict) -> str:
        where = f"plan {e['shot_id']}, " if e.get("shot_id") else ""
        return (f"« {e['text']} » ({where}calque {e['id']}) hors zone {e['seconds']:.2f} s continues, images "
                f"globales {e['start_frame']}-{e['end_frame'] - 1}, dépassement max {e['max_overflow_px']:.1f} px "
                f"(boîte {e['worst_box']} à l'image {e['worst_frame']})")

    if blocking:
        return _check(cid, label, "FAIL", exp, actual, True,
                      "; ".join(describe(e) for e in blocking[:6])
                      + f". Tolérance : {tol:g} s continues. Rapprochez ces textes du centre (positions "
                        "« safe:N% ») ou réduisez leur taille/maxWidth.")
    if not samples:
        return _check(cid, label, "PASS", exp, actual, True,
                      "Aucun relevé de texte (aucun calque safe: true visible) : rien à contrôler.")
    return _check(cid, label, "PASS", exp, actual, True,
                  "" if not excursions else "Dépassements brefs tolérés : " + "; ".join(describe(e) for e in excursions[:4]))


def check_determinism(compiled: dict) -> dict:
    """Résultat du test de déterminisme (build/<scène>/determinism.json : ok et total >= 1)."""
    scene_id = compiled["scene_id"]
    cid, label = "scene.determinism", "scène : déterminisme (rendu témoin dans un navigateur neuf)"
    exp = {"ok": True, "total_min": 1, "matches_equal_total": True}
    path = config.scene_build_dir(scene_id) / "determinism.json"
    doc, err = _load_json(path)
    if err:
        return _check(cid, label, "FAIL", exp, None, True,
                      f"Rapport de déterminisme {err} : lancez « python mograph.py determinism <scène.json> ».")
    errors = _schema_errors(doc, "determinism")
    if errors:
        return _check(cid, label, "FAIL", exp, None, True,
                      f"{path} ne respecte pas manifests.schema.json#/$defs/determinism : " + " ; ".join(errors)
                      + ". Relancez le test de déterminisme.")
    checked = doc["checked"]
    n_match = sum(1 for c in checked if c.get("match"))
    actual = {"ok": doc["ok"], "total": doc["total"], "matches": doc["matches"], "checked": len(checked)}
    problems = []
    if doc["scene_id"] != scene_id:
        problems.append(f"rapport d'une autre scène ({doc['scene_id']})")
    if doc["total"] < 1:
        problems.append("aucune image témoin")
    if doc["matches"] != doc["total"]:
        problems.append(f"{doc['total'] - doc['matches']} image(s) témoin(s) différente(s) sur {doc['total']}")
    if not doc["ok"]:
        problems.append("ok = false")
    if len(checked) != doc["total"] or n_match != doc["matches"] or doc["ok"] != (doc["matches"] == doc["total"]):
        problems.append(f"rapport incohérent (checked={len(checked)}, correspondances réelles={n_match})")
    if problems:
        bad = [f"{c['shot_id']}:{c['frame']}" + (f" (diff {c['diff'].get('diff_png')})" if c.get("diff") else "")
               for c in checked if not c.get("match")]
        return _check(cid, label, "FAIL", exp, actual, True,
                      "; ".join(problems) + (f". Images en écart : {', '.join(bad[:10])}" if bad else "")
                      + ". Consultez build/<scène>/determinism_diff/ puis supprimez la source d'aléa "
                        "(horloge, Math.random, police, image asynchrone) et relancez le rendu.")
    return _check(cid, label, "PASS", exp, actual, True,
                  f"{doc['matches']}/{doc['total']} images témoins identiques au bit près "
                  f"(qc.determinism_frames = {compiled['qc'].get('determinism_frames')}).")


# ---------------------------------------------------------------------------
# Rapport
# ---------------------------------------------------------------------------


def _fmt(value: Any) -> str:
    """Valeur lisible pour une cellule Markdown."""
    if value is None:
        return "—"
    if isinstance(value, bool):
        return "oui" if value else "non"
    if isinstance(value, float):
        return f"{value:.6g}"
    if isinstance(value, dict):
        return ", ".join(f"{k}={_fmt(v)}" for k, v in value.items() if k not in ("segments", "worst"))
    if isinstance(value, list):
        return "[" + ", ".join(_fmt(v) for v in value) + "]"
    return str(value)


def _cell(value: Any, limit: int = 160) -> str:
    text = _fmt(value)
    if len(text) > limit:
        text = text[: limit - 1] + "…"
    # Le tube et les retours à la ligne casseraient le tableau Markdown.
    return text.replace("|", "\\|").replace("\r", " ").replace("\n", "<br>")


def render_markdown(report: dict) -> str:
    """Rapport humain : verdict en tête, livrables, tableau des contrôles, détails des échecs."""
    verdict = report["verdict"]
    fails = [c for c in report["checks"] if c["status"] == "FAIL" and c["blocking"]]
    warns = [c for c in report["checks"] if c["status"] == "WARN"]
    fmt = report.get("format") or {}
    lines = [
        f"# QC {report['scene_id']} : {verdict}",
        "",
        (f"**Verdict : {verdict}**" if verdict == VERDICT_OK
         else f"**Verdict : {verdict}** ({len(fails)} contrôle(s) bloquant(s) en échec)"),
        "",
        f"- Scène : {report.get('title') or report['scene_id']}",
        f"- Format : {fmt.get('aspect')} {fmt.get('resolution')}, {fmt.get('fps')} i/s, "
        f"{report.get('frames')} images ({report.get('duration_s')} s), sortie "
        f"{report.get('output', {}).get('width')}x{report.get('output', {}).get('height')}, "
        f"alpha : {_fmt(bool(fmt.get('alpha')))}",
        f"- Contrôles : " + ", ".join(f"{report['counts'][s]} {s}" for s in STATUSES),
        f"- Généré le {report['generated_at']} ; {report['tools']['ffmpeg']}",
        "",
        "## Livrables",
        "",
        "| Profil | Fichier | Taille (octets) | Échecs bloquants |",
        "|---|---|---|---|",
    ]
    for d in report["deliverables"]:
        lines.append(f"| {_cell(d['name'])} | {_cell(d['path'])} | {_cell(d['size_bytes'])} | "
                     f"{_cell(d['blocking_failures'])} |")
    lines += [
        "",
        "## Contrôles",
        "",
        "| Statut | Id | Contrôle | Attendu | Mesuré | Bloquant |",
        "|---|---|---|---|---|---|",
    ]
    for c in report["checks"]:
        lines.append(f"| **{c['status']}** | `{c['id']}` | {_cell(c['label'])} | {_cell(c['expected'])} | "
                     f"{_cell(c['actual'])} | {_cell(c['blocking'])} |")
    lines += ["", "## Détails des échecs", ""]
    if not fails:
        lines.append("Aucun échec bloquant.")
    for c in fails:
        lines += [f"### `{c['id']}` : {c['label']}", "",
                  f"- Attendu : {_fmt(c['expected'])}",
                  f"- Mesuré : {_fmt(c['actual'])}",
                  f"- Détail : {c['detail'] or '—'}", ""]
    if warns:
        lines += ["", "## Avertissements (non bloquants)", ""]
        for c in warns:
            lines.append(f"- `{c['id']}` : {c['detail'] or _fmt(c['actual'])}")
    lines.append("")
    return "\n".join(lines)


def run_qc(compiled: dict, *, log: Callable[[str], Any] = print) -> dict:
    """Contrôle TOUS les livrables + zones sûres + déterminisme ; écrit qc_report.json et .md."""
    _validate_compiled(compiled)
    scene_id = compiled["scene_id"]
    outputs = list(compiled.get("outputs") or [])
    fmt = compiled["format"]
    lock = threading.Lock()

    def say(msg: str) -> None:
        # Les livrables sont contrôlés en parallèle : le verrou évite les lignes de journal entremêlées.
        with lock:
            log(msg)

    expectations = [expected_for_output(compiled, o) for o in outputs]
    say(f"QC {scene_id} : {len(expectations)} livrable(s), {compiled['frames']} images à {fmt['fps']} i/s")

    def one(exp: dict) -> list[dict]:
        res = check_output(exp["path"], exp)
        nfail = sum(1 for c in res if c["status"] == "FAIL" and c["blocking"])
        say(f"  {exp['name']} : {len(res)} contrôles, {nfail} échec(s) bloquant(s)")
        return res

    # Un fil par livrable : chaque contrôle attend surtout ffprobe/ffmpeg (processus externes), le
    # parallélisme divise le temps total sans concurrence sur le GIL.
    per_output: list[list[dict]] = []
    if expectations:
        with concurrent.futures.ThreadPoolExecutor(max_workers=len(expectations)) as pool:
            per_output = list(pool.map(one, expectations))

    checks: list[dict] = []
    if not outputs:
        checks.append(_check("scene.outputs", "scène : livrables déclarés", "FAIL", ">= 1 livrable", 0, True,
                             "La scène compilée ne déclare aucun livrable (outputs) : ajoutez-en dans la scène."))
    for lst in per_output:
        checks.extend(lst)
    checks.append(check_safe_zones(compiled))
    checks.append(check_determinism(compiled))
    say(f"  {checks[-2]['id']} : {checks[-2]['status']} ; {checks[-1]['id']} : {checks[-1]['status']}")

    blocking_failures = [c["id"] for c in checks if c["status"] == "FAIL" and c["blocking"]]
    verdict = VERDICT_KO if blocking_failures else VERDICT_OK
    deliverables = []
    for exp, lst in zip(expectations, per_output):
        path = Path(exp["path"])
        deliverables.append({
            "name": exp["name"],
            "profile": exp["profile"],
            "path": str(path),
            "size_bytes": path.stat().st_size if path.is_file() else None,
            "checks": [c["id"] for c in lst],
            "blocking_failures": sum(1 for c in lst if c["status"] == "FAIL" and c["blocking"]),
        })
    out_dir = config.scene_out_dir(scene_id)
    json_path = out_dir / "qc_report.json"
    md_path = out_dir / "qc_report.md"
    report = {
        "contract": REPORT_CONTRACT,
        "scene_id": scene_id,
        "title": compiled.get("title"),
        "verdict": verdict,
        "generated_at": _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).isoformat(),
        "format": {k: fmt.get(k) for k in ("aspect", "resolution", "fps", "alpha")},
        "output": compiled.get("output"),
        "frames": compiled["frames"],
        "duration_s": round(int(compiled["frames"]) / int(fmt["fps"]), 6),
        "counts": {s: sum(1 for c in checks if c["status"] == s) for s in STATUSES},
        "blocking_failures": blocking_failures,
        "deliverables": deliverables,
        "checks": checks,
        "tools": {"ffmpeg": _tool_version(config.FFMPEG), "ffprobe": _tool_version(config.FFPROBE)},
        "report_paths": {"json": str(json_path), "md": str(md_path)},
    }
    _write_atomic(json_path, json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    _write_atomic(md_path, render_markdown(report))
    say(f"QC {scene_id} : {verdict}"
        + (f" ({len(blocking_failures)} échec(s) : {', '.join(blocking_failures)})" if blocking_failures else "")
        + f" -> {md_path}")
    return report


# ---------------------------------------------------------------------------
# CLI minimale : python -m pipeline.qc build/<scène>/compiled.json
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        # Console Windows en cp1252 : un caractère non représentable ne doit pas faire échouer le QC.
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass
    parser = argparse.ArgumentParser(
        prog="python -m pipeline.qc",
        description="Contrôle qualité des livrables d'une scène compilée (code 0 = CONFORME, 1 = NON CONFORME).",
    )
    parser.add_argument("compiled", help="chemin de la scène compilée (build/<scène>/compiled.json)")
    args = parser.parse_args(argv)
    path = Path(args.compiled)
    try:
        compiled = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        print(f"ERREUR : scène compilée introuvable : {path}. Compilez d'abord la scène "
              "(python mograph.py render <scène.json>) qui écrit build/<scène>/compiled.json.", file=sys.stderr)
        return 2
    except (OSError, json.JSONDecodeError) as exc:
        print(f"ERREUR : {path} illisible ({exc}). Recompilez la scène.", file=sys.stderr)
        return 2
    try:
        report = run_qc(compiled)
    except QCError as exc:
        print(f"ERREUR QC : {exc}", file=sys.stderr)
        return 2
    for c in report["checks"]:
        if c["status"] == "FAIL" and c["blocking"]:
            print(f"FAIL {c['id']} : {c['detail']}")
    print(f"VERDICT {report['verdict']} ({report['report_paths']['md']})")
    return 0 if report["verdict"] == VERDICT_OK else 1


if __name__ == "__main__":
    sys.exit(main())
