"""Pilote web : Chromium headless (Playwright) + runtime GSAP -> images PNG déterministes par plan.

Contrat : docs/CONTRACT.md §4 (API), §5 (page <-> pilote), §6 (déterminisme) ;
fichiers : schema/internal/manifests.schema.json (shot_manifest, determinism).

    render_shot(compiled, shot_index, *, workers=None, frames=None, log=print) -> manifeste du plan
    render_scene(compiled, *, workers=None, log=print)                         -> [manifestes]
    verify_determinism(compiled, *, count=None, log=print)                     -> rapport

Usage de débogage :
    python -m pipeline.web_render <scène.json | compiled.json> [--shot ID] [--frames 0,5,9]
                                  [--workers N] [--determinism] [--count N]
"""

from __future__ import annotations

import argparse
import atexit
import hashlib
import json
import math
import multiprocessing as mp
import os
import queue
import random
import sys
import time
import traceback
from pathlib import Path
from typing import Callable, Iterable
from urllib.parse import unquote, urlsplit

import cv2
import numpy as np

from pipeline import config

MANIFEST_NAME = "manifest.json"
SHOT_MANIFEST_CONTRACT = "mograph-shot-manifest/1"
DETERMINISM_CONTRACT = "mograph-determinism/1"

# Taille d'un lot d'images confié à un worker : assez petit pour équilibrer la charge entre
# navigateurs et mettre le manifeste à jour souvent (reprise), assez grand pour garder des seeks
# majoritairement vers l'avant (moins de travail GSAP).
BATCH_FRAMES = 6

# Nombre maximal de navigateurs simultanés : au-delà, le coût mémoire (~400 Mo par Chromium) et la
# contention CPU de SwiftShader annulent le gain (réglable : MOGRAPH_MAX_WORKERS, voir config).
MAX_WORKERS = config.WEB_MAX_WORKERS


class WebRenderError(RuntimeError):
    """Échec du rendu web (page en erreur, capture invalide, manifeste incohérent...)."""


# Une seule instance Playwright par processus : avec playwright 1.56, un start()/stop() manuel laisse
# la boucle interne marquée active et tout démarrage suivant échoue (« Sync API inside the asyncio
# loop », mesuré). Chaque session lance néanmoins son PROPRE processus Chromium (navigateur neuf).
_PLAYWRIGHT: list = []


def _playwright():
    if not _PLAYWRIGHT:
        from playwright.sync_api import sync_playwright

        cm = sync_playwright()
        _PLAYWRIGHT.extend([cm, cm.start()])
        atexit.register(_stop_playwright)
    return _PLAYWRIGHT[1]


def _stop_playwright() -> None:
    if _PLAYWRIGHT:
        cm = _PLAYWRIGHT[0]
        _PLAYWRIGHT.clear()
        try:
            cm.stop()
        except Exception:  # noqa: BLE001 — arrêt au mieux en fin de processus
            pass


# ---------------------------------------------------------------------------------------------
# Outils : hachage, écriture atomique, images
# ---------------------------------------------------------------------------------------------

def _canonical_json(obj) -> bytes:
    # Même forme canonique que pipeline.scene.canonical_json (CONTRACT §4) : clés triées, séparateurs
    # compacts, UTF-8 ; recopiée ici pour que le pilote ne dépende pas du compilateur à l'exécution.
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def spec_sha256(spec: dict) -> str:
    return hashlib.sha256(_canonical_json(spec)).hexdigest()


def pixel_sha256(img: np.ndarray) -> str:
    """SHA-256 des PIXELS décodés (BGRA contigu ; uint16 forcé en little-endian), pas des octets PNG."""
    if img.dtype == np.uint16:
        img = img.astype("<u2", copy=False)
    return hashlib.sha256(np.ascontiguousarray(img).tobytes()).hexdigest()


def _write_json_atomic(path: Path, data) -> None:
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=1, sort_keys=False)
        fh.write("\n")
    os.replace(tmp, path)


def _write_png_atomic(path: Path, img: np.ndarray) -> None:
    # Le suffixe .png du fichier temporaire est nécessaire : OpenCV choisit l'encodeur par l'extension.
    tmp = path.with_name(path.stem + ".tmp.png")
    ok = cv2.imwrite(str(tmp), img, [cv2.IMWRITE_PNG_COMPRESSION, config.PNG_COMPRESSION])
    if not ok:
        raise WebRenderError(f"Écriture PNG impossible : {tmp} (disque plein ou droits ?).")
    os.replace(tmp, path)


def _frame_files(d: Path) -> dict[int, Path]:
    """Images définitives du dossier (exactement 6 chiffres + .png ; jamais les .tmp.png)."""
    out: dict[int, Path] = {}
    if d.exists():
        for p in d.glob("[0-9][0-9][0-9][0-9][0-9][0-9].png"):
            out[int(p.stem)] = p
    return out


def _normalize_bgra8(buf: bytes, expect_w: int, expect_h: int) -> np.ndarray:
    """Décode une capture Chromium et la ramène TOUJOURS en BGRA 8 bits.

    Chromium omet le canal alpha quand l'image est entièrement opaque : sans cette normalisation, une
    même séquence mélangerait PNG RVB et RVBA (piège déjà rencontré).
    """
    img = cv2.imdecode(np.frombuffer(buf, np.uint8), cv2.IMREAD_UNCHANGED)
    if img is None:
        raise WebRenderError("Capture Chromium indécodable (PNG corrompu).")
    if img.dtype != np.uint8:
        raise WebRenderError(f"Capture Chromium inattendue en {img.dtype} (8 bits attendus).")
    if img.ndim == 2:
        img = cv2.cvtColor(img, cv2.COLOR_GRAY2BGRA)
    elif img.shape[2] == 3:
        img = cv2.cvtColor(img, cv2.COLOR_BGR2BGRA)  # alpha = 255 : l'image était opaque
    elif img.shape[2] != 4:
        raise WebRenderError(f"Capture Chromium à {img.shape[2]} canaux (3 ou 4 attendus).")
    if img.shape[1] != expect_w or img.shape[0] != expect_h:
        raise WebRenderError(
            f"Capture de {img.shape[1]}x{img.shape[0]} px au lieu de {expect_w}x{expect_h} : "
            "le facteur d'échelle n'est pas appliqué (capture en pixels CSS ?)."
        )
    return np.ascontiguousarray(img)


def _average_premultiplied(samples: list[np.ndarray]) -> np.ndarray:
    """Moyenne de sous-images BGRA 8 bits en alpha prémultiplié, sortie BGRA 16 bits.

    Moyenner en prémultiplié évite les franges sombres sur les bords transparents ; la sortie 16 bits
    conserve les demi-teintes créées par la moyenne (8 bits les écraserait en bandes).
    """
    acc_rgb = np.zeros(samples[0].shape[:2] + (3,), np.float32)
    acc_a = np.zeros(samples[0].shape[:2], np.float32)
    for s in samples:
        a = s[..., 3].astype(np.float32) * (1.0 / 255.0)
        acc_rgb += s[..., :3].astype(np.float32) * (1.0 / 255.0) * a[..., None]
        acc_a += a
    n = float(len(samples))
    acc_rgb /= n
    acc_a /= n
    safe_a = np.where(acc_a > 0, acc_a, 1.0)
    rgb = np.where(acc_a[..., None] > 0, acc_rgb / safe_a[..., None], 0.0)
    out = np.empty(samples[0].shape[:2] + (4,), np.uint16)
    out[..., :3] = np.rint(np.clip(rgb, 0.0, 1.0) * 65535.0).astype(np.uint16)
    out[..., 3] = np.rint(np.clip(acc_a, 0.0, 1.0) * 65535.0).astype(np.uint16)
    return out


def _subframe_offsets(samples: int, shutter: float) -> list[float]:
    # Obturateur CENTRÉ sur l'image : l'image nette de référence reste au milieu du flou.
    return [shutter * ((i + 0.5) / samples - 0.5) for i in range(samples)]


# ---------------------------------------------------------------------------------------------
# Réseau : seule l'origine fictive est servie, depuis des dossiers autorisés
# ---------------------------------------------------------------------------------------------

def _make_route_handler(blocked: list[str]) -> Callable:
    origin = urlsplit(config.ORIGIN)
    prefixes = [(pfx, base.resolve()) for pfx, base in config.SERVED_PREFIXES.items()]

    def handler(route) -> None:
        url = route.request.url
        parts = urlsplit(url)
        if parts.scheme != origin.scheme or parts.netloc != origin.netloc:
            blocked.append(url)
            route.abort("blockedbyclient")
            return
        path = unquote(parts.path)
        if path == "/favicon.ico":
            # Requête automatique du navigateur, sans effet sur l'image : réponse vide plutôt qu'une erreur console.
            route.fulfill(status=204, body=b"")
            return
        for pfx, base in prefixes:
            if not path.startswith(pfx):
                continue
            target = (base / path[len(pfx):]).resolve()
            # Anti-traversée : le fichier résolu doit rester sous le dossier mappé (pas de « ../ »).
            if base != target and base not in target.parents:
                break
            if not target.is_file():
                blocked.append(url)
                route.fulfill(status=404, body=b"", headers={"Content-Type": "text/plain"})
                return
            mime = config.MIME_TYPES.get(target.suffix.lower(), "application/octet-stream")
            route.fulfill(status=200, body=target.read_bytes(), headers={"Content-Type": mime})
            return
        blocked.append(url)
        route.abort("blockedbyclient")

    return handler


# ---------------------------------------------------------------------------------------------
# Session : un navigateur + une page chargée avec un plan compilé
# ---------------------------------------------------------------------------------------------

class ShotSession:
    """Chromium headless configuré (config.CHROMIUM_ARGS) avec la page du plan prête (READY)."""

    def __init__(self, spec: dict, *, playwright=None):
        self.spec = spec
        self.dsf = int(spec["device_scale_factor"])
        self.width = int(spec["canvas"]["width"]) * self.dsf
        self.height = int(spec["canvas"]["height"]) * self.dsf
        self.errors: list[str] = []
        self.blocked: list[str] = []
        self._pw = playwright
        self.browser = None
        self.context = None
        self.page = None

    # -- cycle de vie ------------------------------------------------------------------------
    def open(self) -> "ShotSession":
        if self._pw is None:
            self._pw = _playwright()
        self.browser = self._pw.chromium.launch(headless=True, args=list(config.CHROMIUM_ARGS))
        self.context = self.browser.new_context(
            viewport={"width": int(self.spec["canvas"]["width"]), "height": int(self.spec["canvas"]["height"])},
            device_scale_factor=self.dsf,
            locale=config.BROWSER_LOCALE,
            timezone_id=config.BROWSER_TIMEZONE,
            # Pas de préférence d'animation réduite ni de thème système : la page ne dépend que du plan.
            reduced_motion="no-preference",
            color_scheme="light",
        )
        self.context.route("**/*", _make_route_handler(self.blocked))
        # Ordre imposé : graine + plan PUIS horloge virtuelle, avant tout script de la page.
        init = (
            f"window.__MOGRAPH_SEED__ = {int(self.spec['seed'])};\n"
            f"window.__MOGRAPH_EPOCH_MS__ = {int(config.VIRTUAL_EPOCH_MS)};\n"
            f"window.__MOGRAPH_SHOT__ = {json.dumps(self.spec, ensure_ascii=False)};\n"
        )
        self.context.add_init_script(script=init)
        self.context.add_init_script(path=str(config.RUNTIME_DIR / "clock.js"))
        self.page = self.context.new_page()
        self.page.set_default_timeout(config.FRAME_TIMEOUT_MS)
        self.page.on("pageerror", lambda exc: self.errors.append(f"exception JS : {exc}"))
        self.page.on("console", self._on_console)
        self.page.goto(config.PLAYER_URL, wait_until="load", timeout=config.PAGE_READY_TIMEOUT_MS)
        self.page.wait_for_function(
            "window.__MOGRAPH_READY__ === true || typeof window.__MOGRAPH_ERROR__ === 'string'",
            timeout=config.PAGE_READY_TIMEOUT_MS,
        )
        err = self.page.evaluate("window.__MOGRAPH_ERROR__ || null")
        if err:
            raise WebRenderError(f"Plan « {self.spec['shot_id']} » : le runtime a refusé de démarrer : {err}")
        self.check()
        dpr = self.page.evaluate("window.devicePixelRatio")
        if dpr != self.dsf:
            raise WebRenderError(f"devicePixelRatio = {dpr} au lieu de {self.dsf} : rendu 4K impossible.")
        return self

    def close(self) -> None:
        for obj, meth in ((self.context, "close"), (self.browser, "close")):
            if obj is not None:
                try:
                    getattr(obj, meth)()
                except Exception:  # noqa: BLE001 — fermeture au mieux, l'erreur d'origine prime
                    pass
        self.context = self.browser = self.page = None

    def __enter__(self) -> "ShotSession":
        return self.open()

    def __exit__(self, *exc) -> None:
        self.close()

    # -- contrôle d'erreurs --------------------------------------------------------------------
    def _on_console(self, msg) -> None:
        if msg.type == "error":
            self.errors.append(f"console.error : {msg.text}")

    def check(self) -> None:
        """Toute erreur de page, de console ou requête bloquée arrête le rendu (jamais d'image fausse)."""
        if self.errors or self.blocked:
            details = self.errors + [f"requête bloquée : {u}" for u in self.blocked]
            raise WebRenderError(
                f"Plan « {self.spec['shot_id']} » : erreur dans la page, rendu arrêté :\n  - "
                + "\n  - ".join(details[:10])
            )

    # -- actions -------------------------------------------------------------------------------
    def seek(self, frame: int, sub: float = 0.0) -> None:
        try:
            self.page.evaluate("async ([f, s]) => { await window.__seek(f, s); return true; }", [int(frame), float(sub)])
        except Exception as exc:  # noqa: BLE001 — on enrichit le message avec l'état de la page
            err = None
            try:
                err = self.page.evaluate("window.__MOGRAPH_ERROR__ || null")
            except Exception:  # noqa: BLE001
                pass
            raise WebRenderError(
                f"Plan « {self.spec['shot_id']} », image {frame} (sous-image {sub:+.4f}) : __seek a échoué : "
                f"{err or exc}"
            ) from exc
        self.check()

    def capture(self) -> np.ndarray:
        # page.screenshot(scale="device") : SEULE capture qui rend vraiment en 2x ET garde l'alpha
        # (Page.captureScreenshot brut rend en pixels CSS ; avec clip.scale il perd l'alpha).
        buf = self.page.screenshot(type="png", scale="device", omit_background=True, animations="allow", caret="hide")
        self.check()
        return _normalize_bgra8(buf, self.width, self.height)

    def layout(self) -> list[dict]:
        recs = self.page.evaluate("window.__layout()")
        self.check()
        return recs

    def render_frame(self, frame: int, *, want_layout: bool = False) -> tuple[np.ndarray, list[dict] | None]:
        """Rend une image (avec flou de mouvement si actif) ; relevé de mise en page optionnel."""
        mb = self.spec["motion_blur"]
        self.seek(frame, 0.0)
        lay = self.layout() if want_layout else None
        if not mb["enabled"] or int(mb["samples"]) <= 1:
            return self.capture(), lay
        samples = []
        for sub in _subframe_offsets(int(mb["samples"]), float(mb["shutter"])):
            self.seek(frame, sub)
            samples.append(self.capture())
        return _average_premultiplied(samples), lay


# ---------------------------------------------------------------------------------------------
# Manifeste d'un plan
# ---------------------------------------------------------------------------------------------

def _shot_depth(spec: dict) -> int:
    mb = spec["motion_blur"]
    return 16 if mb["enabled"] and int(mb["samples"]) > 1 else 8


def _new_manifest(compiled: dict, spec: dict) -> dict:
    dsf = int(spec["device_scale_factor"])
    mb = spec["motion_blur"]
    return {
        "contract": SHOT_MANIFEST_CONTRACT,
        "scene_id": compiled["scene_id"],
        "shot_id": spec["shot_id"],
        "spec_sha256": spec_sha256(spec),
        "fps": int(spec["fps"]),
        "frames": int(spec["frames"]),
        "width": int(spec["canvas"]["width"]) * dsf,
        "height": int(spec["canvas"]["height"]) * dsf,
        "depth": _shot_depth(spec),
        "motion_blur_samples": int(mb["samples"]) if mb["enabled"] else 1,
        "frames_done": {},
        "layout_every": config.layout_every(int(spec["fps"])),
        "layout": {},
        "timing": {},
    }


def _layout_frames(spec: dict) -> set[int]:
    every = config.layout_every(int(spec["fps"]))
    n = int(spec["frames"])
    return set(range(0, n, every)) | {n - 1}


def _frame_is_valid(path: Path, man: dict) -> np.ndarray | None:
    img = cv2.imread(str(path), cv2.IMREAD_UNCHANGED)
    want = np.uint16 if man["depth"] == 16 else np.uint8
    if img is None or img.ndim != 3 or img.shape[2] != 4 or img.dtype != want:
        return None
    if img.shape[1] != man["width"] or img.shape[0] != man["height"]:
        return None
    return img


def _load_or_reset_manifest(compiled: dict, spec: dict, out_dir: Path, log) -> dict:
    """Reprise : réutilise un manifeste cohérent, sinon repart de zéro (en supprimant les images périmées)."""
    fresh = _new_manifest(compiled, spec)
    mpath = out_dir / MANIFEST_NAME
    man = None
    if mpath.exists():
        try:
            man = json.loads(mpath.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            log(f"plan {spec['shot_id']} : manifeste illisible, reprise impossible -> rendu complet")
            man = None
    keys = ("spec_sha256", "width", "height", "depth", "frames", "fps", "motion_blur_samples", "contract")
    if man is not None and any(man.get(k) != fresh[k] for k in keys):
        log(f"plan {spec['shot_id']} : définition compilée modifiée depuis le dernier rendu -> rendu complet")
        man = None
    if man is None:
        # Images d'une AUTRE définition du plan : les garder mélangerait deux versions dans la séquence.
        for p in _frame_files(out_dir).values():
            p.unlink()
        man = fresh
    for tmp in out_dir.glob("*.tmp*"):
        tmp.unlink()  # restes d'une écriture interrompue (jamais une image complète)
    on_disk = _frame_files(out_dir)
    # Entrées du manifeste dont le fichier a disparu : à refaire.
    for key in list(man["frames_done"]):
        if int(key) not in on_disk or int(key) >= man["frames"]:
            del man["frames_done"][key]
    # Images écrites (atomiquement) mais pas encore consignées : relues, vérifiées et adoptées.
    adopted = 0
    for idx, p in sorted(on_disk.items()):
        if str(idx) in man["frames_done"]:
            continue
        img = _frame_is_valid(p, man) if idx < man["frames"] else None
        if img is None:
            p.unlink()
            continue
        man["frames_done"][str(idx)] = {"file": p.name, "sha256": pixel_sha256(img)}
        adopted += 1
    if adopted:
        log(f"plan {spec['shot_id']} : {adopted} image(s) présente(s) sur disque adoptée(s) (reprise)")
    man["layout"] = {k: v for k, v in man.get("layout", {}).items() if int(k) < man["frames"]}
    man["layout_every"] = fresh["layout_every"]
    return man


def _ordered_manifest(man: dict) -> dict:
    out = dict(man)
    out["frames_done"] = {str(k): man["frames_done"][str(k)] for k in sorted(map(int, man["frames_done"]))}
    out["layout"] = {str(k): man["layout"][str(k)] for k in sorted(map(int, man["layout"]))}
    return out


# ---------------------------------------------------------------------------------------------
# Travail par lots (en ligne ou dans des processus workers)
# ---------------------------------------------------------------------------------------------

def _render_batch(session: ShotSession, out_dir: Path, frames: list[int], layout_set: set[int],
                  capture_set: set[int]) -> list[dict]:
    results = []
    for f in frames:
        want_layout = f in layout_set
        if f in capture_set:
            img, lay = session.render_frame(f, want_layout=want_layout)
            path = out_dir / config.FRAME_PATTERN.format(f)
            _write_png_atomic(path, img)
            results.append({"frame": f, "file": path.name, "sha256": pixel_sha256(img), "layout": lay})
        else:
            session.seek(f, 0.0)
            results.append({"frame": f, "file": None, "sha256": None, "layout": session.layout()})
    return results


def _worker_main(spec: dict, out_dir: str, layout_set: list[int], capture_set: list[int],
                 tasks, results) -> None:
    """Processus worker : un navigateur persistant, des lots tirés d'une file jusqu'à la sentinelle."""
    session = None
    try:
        session = ShotSession(spec).open()
        lset, cset = set(layout_set), set(capture_set)
        while True:
            batch = tasks.get()
            if batch is None:
                break
            t0 = time.perf_counter()
            res = _render_batch(session, Path(out_dir), batch, lset, cset)
            results.put(("ok", res, time.perf_counter() - t0))
    except BaseException as exc:  # noqa: BLE001 — tout échec est renvoyé au parent, qui arrête le rendu
        results.put(("error", f"{exc}\n{traceback.format_exc(limit=4)}", 0.0))
    finally:
        if session is not None:
            session.close()


def _default_workers(n_frames: int) -> int:
    cpu = os.cpu_count() or 4
    # Un navigateur par cœur jusqu'à MAX_WORKERS : mesuré sur 4 cœurs (demo_flat_riso, 165 images),
    # 1 navigateur 0,95 s/image, 2 : 0,54 s, 4 : 0,37 s, empreintes identiques ; l'ancien cpu // 6
    # n'en lançait qu'un sous 6 cœurs.
    w = min(MAX_WORKERS, max(1, cpu))
    # Un navigateur coûte ~2-4 s à démarrer : inutile d'en lancer plus que de lots de travail.
    return max(1, min(w, math.ceil(n_frames / (BATCH_FRAMES * 2))))


# ---------------------------------------------------------------------------------------------
# API publique
# ---------------------------------------------------------------------------------------------

def render_shot(compiled: dict, shot_index: int, *, workers: int | None = None,
                frames: list[int] | None = None, log=print) -> dict:
    """Rend (ou reprend) les images d'un plan et renvoie son manifeste à jour."""
    shot = compiled["shots"][shot_index]
    spec = shot["spec"]
    sid = spec["shot_id"]
    # Calques vidéo : images extraites par FFmpeg AVANT d'ouvrir le navigateur (réutilisées si à jour).
    from pipeline import media
    media.ensure_shot_media(compiled, shot_index, log=log)
    out_dir = config.shot_frames_dir(compiled["scene_id"], sid)
    out_dir.mkdir(parents=True, exist_ok=True)
    man = _load_or_reset_manifest(compiled, spec, out_dir, log)
    n = int(spec["frames"])
    wanted = set(range(n)) if frames is None else {int(f) for f in frames}
    bad = [f for f in wanted if f < 0 or f >= n]
    if bad:
        raise WebRenderError(f"Plan « {sid} » : images {sorted(bad)[:5]} hors plage 0..{n - 1}.")
    capture_set = {f for f in wanted if str(f) not in man["frames_done"]}
    layout_set = {f for f in _layout_frames(spec) & wanted if str(f) not in man["layout"]}
    todo = sorted(capture_set | layout_set)
    if not todo:
        log(f"plan {sid} : {len(man['frames_done'])}/{n} images déjà rendues, rien à faire")
        _write_json_atomic(out_dir / MANIFEST_NAME, _ordered_manifest(man))
        return _ordered_manifest(man)

    n_workers = workers if workers is not None else _default_workers(len(todo))
    n_workers = max(1, min(int(n_workers), MAX_WORKERS, len(todo)))
    batches = [todo[i:i + BATCH_FRAMES] for i in range(0, len(todo), BATCH_FRAMES)]
    log(f"plan {sid} : {len(capture_set)} image(s) à rendre, {len(layout_set)} relevé(s) de mise en page, "
        f"{n_workers} navigateur(s), profondeur {man['depth']} bits"
        + (f", flou {man['motion_blur_samples']} sous-images" if man["motion_blur_samples"] > 1 else ""))

    t_start = time.perf_counter()
    done_count = 0

    def absorb(res: list[dict]) -> None:
        nonlocal done_count
        for r in res:
            if r["sha256"] is not None:
                man["frames_done"][str(r["frame"])] = {"file": r["file"], "sha256": r["sha256"]}
                done_count += 1
            if r["layout"] is not None:
                man["layout"][str(r["frame"])] = r["layout"]
        # Manifeste réécrit après chaque lot : un arrêt brutal ne perd au plus qu'un lot.
        _write_json_atomic(out_dir / MANIFEST_NAME, _ordered_manifest(man))
        el = time.perf_counter() - t_start
        log(f"plan {sid} : {len(man['frames_done'])}/{n} images "
            f"({el / max(done_count, 1):.2f} s/image, {el:.0f} s)")

    if n_workers == 1:
        with ShotSession(spec) as session:
            for batch in batches:
                absorb(_render_batch(session, out_dir, batch, layout_set, capture_set))
    else:
        # spawn : seul mode disponible sous Windows, et le plus sûr avec Playwright (pas d'état hérité).
        ctx = mp.get_context("spawn")
        tasks, results = ctx.Queue(), ctx.Queue()
        for b in batches:
            tasks.put(b)
        for _ in range(n_workers):
            tasks.put(None)
        procs = [ctx.Process(target=_worker_main, daemon=True,
                             args=(spec, str(out_dir), sorted(layout_set), sorted(capture_set), tasks, results))
                 for _ in range(n_workers)]
        for p in procs:
            p.start()
        pending = len(batches)
        try:
            while pending:
                try:
                    kind, payload, _dt = results.get(timeout=5)
                except queue.Empty:
                    dead = [p for p in procs if p.exitcode not in (None, 0)]
                    if dead:
                        raise WebRenderError(
                            f"Plan « {sid} » : un worker de rendu s'est arrêté (code {dead[0].exitcode}) ; "
                            "relancez la commande, le rendu reprendra aux images manquantes.")
                    continue
                if kind == "error":
                    raise WebRenderError(f"Plan « {sid} » : échec dans un worker de rendu :\n{payload}")
                absorb(payload)
                pending -= 1
        finally:
            for p in procs:
                p.join(timeout=30)
            for p in procs:
                if p.is_alive():
                    p.terminate()  # PID connu et possédé : on ne tue jamais par motif de nom

    total = time.perf_counter() - t_start
    man["timing"] = {
        "seconds_per_frame": round(total / max(done_count, 1), 4),
        "workers": n_workers,
        "total_seconds": round(total, 2),
    }
    man = _ordered_manifest(man)
    _write_json_atomic(out_dir / MANIFEST_NAME, man)
    missing = [f for f in wanted if str(f) not in man["frames_done"]]
    if missing:
        raise WebRenderError(f"Plan « {sid} » : {len(missing)} image(s) non rendue(s) : {missing[:8]}")
    return man


def render_scene(compiled: dict, *, workers: int | None = None, log=print) -> list[dict]:
    manifests = []
    for i in range(len(compiled["shots"])):
        manifests.append(render_shot(compiled, i, workers=workers, log=log))
    return manifests


def _witnesses(compiled: dict, count: int) -> tuple[list[tuple[int, int]], int]:
    """Images témoins : premières/dernières de chaque plan, une par fondu, complétées au hasard (graine fixe)."""
    seed_src = hashlib.sha256(_canonical_json([s["spec"]["seed"] for s in compiled["shots"]])).hexdigest()
    rng = random.Random(int(seed_src[:16], 16))
    mandatory: list[tuple[int, int]] = []
    for i, shot in enumerate(compiled["shots"]):
        n = int(shot["frames"])
        for f in (0, n - 1):
            if (i, f) not in mandatory:
                mandatory.append((i, f))
        fade = int(shot.get("fade_in_frames") or 0)
        if fade:
            # Image au cœur du fondu côté plan entrant ET son homologue côté plan sortant.
            k = fade // 2
            prev_n = int(compiled["shots"][i - 1]["frames"])
            for cand in ((i, k), (i - 1, prev_n - fade + k)):
                if cand not in mandatory:
                    mandatory.append(cand)
    pool = [(i, f) for i, s in enumerate(compiled["shots"]) for f in range(int(s["frames"]))
            if (i, f) not in mandatory]
    extra = max(0, count - len(mandatory))
    chosen = mandatory + rng.sample(pool, min(extra, len(pool)))
    rng.shuffle(chosen)  # ordre volontairement mélangé : aucun rendu ne bénéficie d'un seek précédent voisin
    return chosen, rng.randrange(1 << 30)


def verify_determinism(compiled: dict, *, count: int | None = None, log=print) -> dict:
    """Re-rend des images témoins isolées, dans le désordre, dans un navigateur NEUF, et compare les empreintes."""
    scene_id = compiled["scene_id"]
    count = int(count if count is not None else compiled["qc"]["determinism_frames"])
    witnesses, _ = _witnesses(compiled, count)
    manifests = {}
    for i, shot in enumerate(compiled["shots"]):
        mpath = config.shot_frames_dir(scene_id, shot["id"]) / MANIFEST_NAME
        if not mpath.exists():
            raise WebRenderError(f"Plan « {shot['id']} » jamais rendu : lancez d'abord `python mograph.py render {scene_id}`.")
        man = json.loads(mpath.read_text(encoding="utf-8"))
        if man.get("spec_sha256") != spec_sha256(shot["spec"]):
            raise WebRenderError(f"Plan « {shot['id']} » : les images ne correspondent plus à la scène compilée "
                                 f"(définition modifiée) : relancez le rendu avant le test de déterminisme.")
        manifests[i] = man
    diff_dir = config.scene_build_dir(scene_id) / "determinism_diff"
    checked, order = [], []
    log(f"déterminisme : {len(witnesses)} image(s) témoin(s), navigateur neuf, ordre mélangé")
    sessions: dict[int, ShotSession] = {}
    try:
        for i, f in witnesses:
            if i not in sessions:
                # Navigateur NEUF : nouveau processus Chromium, jamais utilisé pour le rendu principal.
                sessions[i] = ShotSession(compiled["shots"][i]["spec"]).open()
            shot_id = compiled["shots"][i]["id"]
            entry = manifests[i]["frames_done"].get(str(f))
            if entry is None:
                raise WebRenderError(f"Plan « {shot_id} », image {f} absente du manifeste : rendu incomplet.")
            img, _ = sessions[i].render_frame(f)
            actual = pixel_sha256(img)
            rec = {"shot_id": shot_id, "frame": f, "expected": entry["sha256"], "actual": actual,
                   "match": actual == entry["sha256"]}
            if not rec["match"]:
                rec["diff"] = _diff_report(img, config.shot_frames_dir(scene_id, shot_id) / entry["file"],
                                           diff_dir / f"{shot_id}_{f:06d}.png")
            checked.append(rec)
            order.append(f"{shot_id}:{f}")
            log(f"  {shot_id}:{f:<5} {'identique' if rec['match'] else 'DIFFÉRENT'}")
    finally:
        for s in sessions.values():
            s.close()
    matches = sum(1 for c in checked if c["match"])
    report = {
        "contract": DETERMINISM_CONTRACT,
        "scene_id": scene_id,
        "fresh_browser": True,
        "order": order,
        "checked": checked,
        "total": len(checked),
        "matches": matches,
        "ok": len(checked) > 0 and matches == len(checked),
    }
    _write_json_atomic(config.scene_build_dir(scene_id) / "determinism.json", report)
    return report


def _diff_report(actual: np.ndarray, expected_path: Path, out_png: Path) -> dict:
    """Localise un écart : nombre de pixels, boîte englobante, écart max, image de différence amplifiée."""
    exp = cv2.imread(str(expected_path), cv2.IMREAD_UNCHANGED)
    if exp is None or exp.shape != actual.shape:
        return {"pixels": -1, "bbox": [], "max_abs": -1, "diff_png": ""}
    d = np.abs(actual.astype(np.int32) - exp.astype(np.int32)).max(axis=2)
    ys, xs = np.nonzero(d)
    out_png.parent.mkdir(parents=True, exist_ok=True)
    scale = 255.0 / max(int(d.max()), 1)
    vis = np.clip(d * scale, 0, 255).astype(np.uint8)
    cv2.imwrite(str(out_png), vis)
    return {
        "pixels": int(len(xs)),
        "bbox": [int(xs.min()), int(ys.min()), int(xs.max()), int(ys.max())] if len(xs) else [],
        "max_abs": float(d.max()),
        "diff_png": str(out_png),
    }


# ---------------------------------------------------------------------------------------------
# Ligne de commande de débogage
# ---------------------------------------------------------------------------------------------

def _load_compiled(path: str) -> dict:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if data.get("contract") == "mograph-scene/1":
        return data
    from pipeline import scene as scene_mod  # import tardif : seulement pour une scène source

    return scene_mod.compile_scene(path)


def _main(argv: Iterable[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Pilote web mograph (débogage)")
    ap.add_argument("source", help="scène .json ou build/<scène>/compiled.json")
    ap.add_argument("--shot", help="id du plan (défaut : tous)")
    ap.add_argument("--frames", help="liste d'images, ex. 0,5,9")
    ap.add_argument("--workers", type=int)
    ap.add_argument("--determinism", action="store_true")
    ap.add_argument("--count", type=int)
    a = ap.parse_args(list(argv) if argv is not None else None)
    compiled = _load_compiled(a.source)
    if a.determinism:
        rep = verify_determinism(compiled, count=a.count)
        print(f"{rep['matches']}/{rep['total']} identiques")
        return 0 if rep["ok"] else 1
    frames = [int(x) for x in a.frames.split(",")] if a.frames else None
    for i, shot in enumerate(compiled["shots"]):
        if a.shot and shot["id"] != a.shot:
            continue
        render_shot(compiled, i, workers=a.workers, frames=frames)
    return 0


if __name__ == "__main__":
    sys.exit(_main())
