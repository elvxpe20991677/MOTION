"""Planches contact automatiques (contrôle à l'œil sans lecteur vidéo) : out/<scène>/planche_contact.png.

Images choisies là où les défauts se voient : première et dernière image, entrée, milieu et sortie de
chaque plan (coupes), milieu de chaque transition, premiers temps musicaux (impacts). Chaque vignette
porte son numéro d'image, son timecode et son plan. Scène à alpha : damier sous la transparence, et
une seconde planche out/<scène>/planche_alpha.png incruste chaque image sur fond CLAIR et SOMBRE
(franges, halos, prémultiplication fautive se voient sur l'un ou l'autre).
"""

from __future__ import annotations

import os
from pathlib import Path

import cv2
import numpy as np

from pipeline import config

TILE_W = 480                 # largeur d'une vignette (px) : planche lisible à l'écran, fichier léger
MAX_TILES = 16               # au-delà, sélection régulière parmi les candidats
MIN_TILES = 8                # en deçà, images régulièrement réparties
COLUMNS = 4
LIGHT_BG = (238, 238, 238)   # BGR
DARK_BG = (21, 21, 21)


class SheetError(RuntimeError):
    """Planche impossible (séquence maître absente)."""


def timecode(frame: int, fps: int) -> str:
    """HH:MM:SS:FF (timecode SMPTE sans décalage : image 0 = 00:00:00:00)."""
    s, f = divmod(int(frame), int(fps))
    m, s = divmod(s, 60)
    h, m = divmod(m, 60)
    return f"{h:02d}:{m:02d}:{s:02d}:{f:02d}"


def pick_frames(compiled: dict, max_tiles: int = MAX_TILES) -> list[tuple[int, str]]:
    """[(image globale, raison)] triées, sans doublon, au plus max_tiles (priorité aux coupes/transitions)."""
    total, fps = int(compiled["frames"]), int(compiled["format"]["fps"])
    must: dict[int, str] = {0: "début", total - 1: "fin"}
    extra: dict[int, str] = {}
    for s in compiled["shots"]:
        g0, n = int(s["global_start"]), int(s["frames"])
        fade = int(s["fade_in_frames"])
        if s["index"] > 0:
            if fade:
                kind = (s.get("transition") or {}).get("type", "crossfade")
                must.setdefault(g0 + fade // 2, f"{kind} {s['id']}")
            else:
                must.setdefault(g0 - 1, f"coupe fin")
                must.setdefault(g0, f"coupe {s['id']}")
        extra.setdefault(g0 + n // 2, f"milieu {s['id']}")
    audio = compiled.get("audio") or {}
    if audio.get("bpm"):
        beat = 60.0 / float(audio["bpm"])
        t, k = float(audio.get("offset", 0.0)), 0
        while t < total / fps and k < 8:
            g = int(round(t * fps))
            if 0 <= g < total:
                extra.setdefault(g, f"temps {k}")
            t += beat
            k += 1
    for m in audio.get("markers") or []:
        g = int(round(float(m["time"]) * fps))
        if 0 <= g < total:
            must.setdefault(g, f"marqueur {m['id']}")
    chosen = dict(sorted(must.items())[:max_tiles])
    rest = [kv for kv in sorted(extra.items()) if kv[0] not in chosen]
    room = max_tiles - len(chosen)
    if room > 0 and rest:
        step = max(1, len(rest) / room)
        for i in range(min(room, len(rest))):
            g, why = rest[int(i * step)]
            chosen.setdefault(g, why)
    # Scène sans événements (un seul plan, pas de musique) : compléter régulièrement jusqu'à MIN_TILES.
    if len(chosen) < min(MIN_TILES, total):
        want = min(MIN_TILES, total) - len(chosen)
        grid = [int(round(k * (total - 1) / (want + 1))) for k in range(1, want + 1)]
        for g in grid:
            if all(abs(g - c) > 2 for c in chosen):
                chosen[g] = ""
    return sorted(chosen.items())


def _read(path: Path) -> np.ndarray:
    img = cv2.imdecode(np.fromfile(str(path), dtype=np.uint8), cv2.IMREAD_UNCHANGED)
    if img is None:
        raise SheetError(f"Image illisible : {path}")
    if img.dtype == np.uint16:
        img = np.rint(img.astype(np.float32) / 257.0).astype(np.uint8)
    if img.ndim == 2:
        img = cv2.cvtColor(img, cv2.COLOR_GRAY2BGRA)
    if img.shape[2] == 3:
        img = cv2.cvtColor(img, cv2.COLOR_BGR2BGRA)
    return img


def _over(img: np.ndarray, bg: np.ndarray) -> np.ndarray:
    """Composition « over » d'une image BGRA (non prémultipliée) sur un fond BGR."""
    a = img[:, :, 3:4].astype(np.float32) / 255.0
    return np.rint(img[:, :, :3].astype(np.float32) * a + bg.astype(np.float32) * (1.0 - a)).astype(np.uint8)


def _checker(h: int, w: int, cell: int) -> np.ndarray:
    yy, xx = np.mgrid[0:h, 0:w]
    v = np.where(((yy // cell + xx // cell) % 2) == 0, 205, 150).astype(np.uint8)
    return np.repeat(v[:, :, None], 3, axis=2)


def _label(tile: np.ndarray, text: str) -> None:
    # Texte ASCII (OpenCV ne dessine pas les accents) : numéro, timecode et id de plan.
    org = (8, 22)
    cv2.putText(tile, text, org, cv2.FONT_HERSHEY_SIMPLEX, 0.55, (0, 0, 0), 3, cv2.LINE_AA)
    cv2.putText(tile, text, org, cv2.FONT_HERSHEY_SIMPLEX, 0.55, (255, 255, 255), 1, cv2.LINE_AA)


def _grid(tiles: list[np.ndarray], columns: int) -> np.ndarray:
    th = max(t.shape[0] for t in tiles)
    tw = max(t.shape[1] for t in tiles)
    cols = min(columns, len(tiles))
    rows = -(-len(tiles) // cols)
    gap = 6
    sheet = np.full((rows * th + (rows + 1) * gap, cols * tw + (cols + 1) * gap, 3), 32, np.uint8)
    for i, t in enumerate(tiles):
        r, c = divmod(i, cols)
        y, x = gap + r * (th + gap), gap + c * (tw + gap)
        sheet[y:y + t.shape[0], x:x + t.shape[1]] = t
    return sheet


def _ascii(text: str) -> str:
    table = str.maketrans("éèêàâùûôîïçÉÈÀ", "eeeaauuoiicEEA")
    return text.translate(table).encode("ascii", "replace").decode("ascii")


def _write(path: Path, img: np.ndarray) -> None:
    ok, buf = cv2.imencode(".png", img, [cv2.IMWRITE_PNG_COMPRESSION, 6])
    if not ok:
        raise SheetError(f"Encodage PNG impossible pour {path}")
    tmp = path.with_name(path.name + ".tmp")
    buf.tofile(str(tmp))
    os.replace(tmp, path)


def write_contact_sheets(compiled: dict, *, log=print) -> list[Path]:
    """Écrit planche_contact.png (et planche_alpha.png si alpha) dans out/<scène>/ ; renvoie les chemins."""
    scene_id, fps = compiled["scene_id"], int(compiled["format"]["fps"])
    master = config.master_dir(scene_id)
    picks = pick_frames(compiled)
    missing = [g for g, _ in picks if not (master / config.FRAME_PATTERN.format(g)).is_file()]
    if missing:
        raise SheetError(f"Séquence maître incomplète ({len(missing)} image(s) absentes, ex. {missing[0]}) : "
                         "lancez « mograph.py compose <scène> » d'abord.")
    alpha = bool(compiled["format"].get("alpha"))
    tiles, alpha_tiles = [], []
    for g, why in picks:
        img = _read(master / config.FRAME_PATTERN.format(g))
        h, w = img.shape[:2]
        th = max(1, round(h * TILE_W / w))
        small = cv2.resize(img, (TILE_W, th), interpolation=cv2.INTER_AREA)
        label = _ascii(f"#{g}  {timecode(g, fps)}  {why}")
        tile = _over(small, _checker(th, TILE_W, 12)) if alpha else small[:, :, :3].copy()
        _label(tile, label)
        tiles.append(tile)
        if alpha:
            light = _over(small, np.full((th, TILE_W, 3), LIGHT_BG, np.uint8))
            dark = _over(small, np.full((th, TILE_W, 3), DARK_BG, np.uint8))
            pair = np.concatenate([light, dark], axis=1)
            _label(pair, label + "  (clair | sombre)")
            alpha_tiles.append(pair)
    out = config.scene_out_dir(scene_id)
    out.mkdir(parents=True, exist_ok=True)
    paths = [out / "planche_contact.png"]
    _write(paths[0], _grid(tiles, COLUMNS))
    if alpha:
        paths.append(out / "planche_alpha.png")
        _write(paths[1], _grid(alpha_tiles, 2))
    log(f"Planche contact : {', '.join(str(p) for p in paths)} ({len(picks)} images)")
    return paths
