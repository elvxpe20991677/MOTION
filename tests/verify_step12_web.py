"""Vérification de l'étape 12 : runtime navigateur + pilote web (pipeline/web_render.py).

Lancement depuis la racine :  .venv/Scripts/python.exe tests/verify_step12_web.py
Affiche PASS/FAIL par contrôle (INFO pour les mesures) ; code de sortie ≠ 0 si un FAIL.
Isolation : MOGRAPH_BUILD_DIR = build/_tests/step12/build (vidé au début), rien n'est écrit ailleurs.

Contrôles (SPEC étape 12) :
  (a) 5 images d'une démo -> planche contact build/_tests/step12/contact_sheet.png (à regarder)
  (b) temps par image 1080p avec effets, 1 navigateur puis 4 ; empreintes identiques quel que soit le nombre
  (c) 4K : devicePixelRatio 2, capture 3840x2160, blocs 2x2 NON uniformes sur les bords du texte
  (d) scène alpha + flou de mouvement : PNG 16 bits, pixels transparents > 0, alpha de 0 à max
  (e) porte de déterminisme : témoins isolés, mélangés, navigateur neuf -> 100 % identiques
  (f) reprise : seules les images manquantes sont refaites, les autres restent intactes
  (g) réseau : toute requête hors http://mograph.render (et toute traversée de dossier) est bloquée
  (h) police manquante -> __MOGRAPH_ERROR__ et arrêt propre
"""

from __future__ import annotations

import copy
import json
import os
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TEST_DIR = ROOT / "build" / "_tests" / "step12"
# Isolation AVANT d'importer pipeline.config : tous les rendus du test vont sous build/_tests/step12/.
os.environ["MOGRAPH_BUILD_DIR"] = str(TEST_DIR / "build")
sys.path.insert(0, str(ROOT))

import cv2  # noqa: E402
import jsonschema  # noqa: E402
import numpy as np  # noqa: E402
from referencing import Registry, Resource  # noqa: E402

from pipeline import config, scene, web_render  # noqa: E402

FIX = ROOT / "tests" / "fixtures"
RESULTS: list[bool] = []


def check(label: str, ok: bool, detail: str = "") -> bool:
    RESULTS.append(bool(ok))
    print(f"{'PASS' if ok else 'FAIL'} {label}" + (f" — {detail}" if detail else ""), flush=True)
    return bool(ok)


def info(msg: str) -> None:
    print(f"INFO {msg}", flush=True)


def quiet(_msg: str) -> None:
    pass


def manifest_validator(name: str):
    s = json.loads((ROOT / "schema/internal/manifests.schema.json").read_text(encoding="utf-8"))
    reg = Registry().with_resource(s["$id"], Resource.from_contents(s))
    return jsonschema.Draft202012Validator({"$ref": f"{s['$id']}#/$defs/{name}"}, registry=reg)


def shot_dir(compiled: dict, i: int) -> Path:
    return config.shot_frames_dir(compiled["scene_id"], compiled["shots"][i]["id"])


def load_manifest(compiled: dict, i: int) -> dict:
    return json.loads((shot_dir(compiled, i) / web_render.MANIFEST_NAME).read_text(encoding="utf-8"))


def fake_plates(compiled: dict) -> None:
    """Plaques 3D synthétiques (au lieu de Blender) : dégradé + disque qui se déplace, PNG RGBA 16 bits."""
    for shot in compiled["shots"]:
        for job in shot["plates"]:
            d = Path(job["out_dir"])
            d.mkdir(parents=True, exist_ok=True)
            w, h = job["width"], job["height"]
            yy, xx = np.mgrid[0:h, 0:w]
            for i in range(job["frames"]):
                img = np.zeros((h, w, 4), np.uint16)
                img[..., 0] = (xx * 65535 // max(w - 1, 1)).astype(np.uint16)
                img[..., 1] = (yy * 65535 // max(h - 1, 1)).astype(np.uint16)
                cx = int(w * (0.2 + 0.6 * i / max(job["frames"] - 1, 1)))
                disk = (xx - cx) ** 2 + (yy - h // 2) ** 2 < (h // 5) ** 2
                img[disk, 2] = 65535
                img[..., 3] = 65535
                cv2.imwrite(str(d / f"{i:06d}.png"), img)


def block_nonuniform_ratio(img: np.ndarray) -> tuple[float, int]:
    """Part des blocs 2x2 alignés, situés sur un bord (gradient fort), dont les 4 pixels ne sont pas égaux."""
    g = cv2.cvtColor(np.ascontiguousarray(img[..., :3]), cv2.COLOR_BGR2GRAY).astype(np.int32)
    h, w = g.shape[0] // 2 * 2, g.shape[1] // 2 * 2
    g = g[:h, :w]
    a, b, c, d = g[0::2, 0::2], g[0::2, 1::2], g[1::2, 0::2], g[1::2, 1::2]
    lo = np.minimum(np.minimum(a, b), np.minimum(c, d))
    hi = np.maximum(np.maximum(a, b), np.maximum(c, d))
    # Bord = le bloc et son voisinage ont un fort contraste (texte), mesuré à l'échelle du bloc.
    blk = ((a + b + c + d) // 4).astype(np.float32)
    grad = np.abs(cv2.Sobel(blk, cv2.CV_32F, 1, 0)) + np.abs(cv2.Sobel(blk, cv2.CV_32F, 0, 1))
    edges = grad > 60
    n = int(edges.sum())
    return (float((hi[edges] != lo[edges]).mean()) if n else 0.0), n


def main() -> int:
    shutil.rmtree(TEST_DIR, ignore_errors=True)
    TEST_DIR.mkdir(parents=True)
    v_shot = manifest_validator("shot_manifest")
    v_det = manifest_validator("determinism")

    # (a) planche contact d'une démo ---------------------------------------------------------------
    print("== (a) planche contact : demo_flat_riso ==")
    flat = scene.compile_scene(ROOT / "scenes" / "demo_flat_riso.json")
    frames_a = [0, 12, 25, 45, 84]
    web_render.render_shot(flat, 0, workers=1, frames=frames_a, log=quiet)
    imgs = [cv2.imread(str(shot_dir(flat, 0) / f"{f:06d}.png"), cv2.IMREAD_UNCHANGED) for f in frames_a]
    check("(a) 5 images BGRA 8 bits 1920x1080",
          all(i is not None and i.shape == (1080, 1920, 4) and i.dtype == np.uint8 for i in imgs))
    th = [cv2.resize(i[..., :3], (640, 360), interpolation=cv2.INTER_AREA) for i in imgs]
    for t, f in zip(th, frames_a):
        cv2.putText(t, str(f), (12, 34), cv2.FONT_HERSHEY_SIMPLEX, 1.0, (40, 40, 220), 2)
    sheet = np.vstack([np.hstack(th[:3]), np.hstack(th[3:] + [np.full_like(th[0], 255)])])
    cv2.imwrite(str(TEST_DIR / "contact_sheet.png"), sheet)
    info(f"planche contact à regarder : {TEST_DIR / 'contact_sheet.png'}")
    man_a = load_manifest(flat, 0)
    check("(a) manifeste conforme au schéma shot_manifest", not list(v_shot.iter_errors(man_a)))

    # (b) temps par image + indépendance vis-à-vis du nombre de navigateurs ------------------------
    print("== (b) temps par image 1080p avec effets ==")
    det = scene.compile_scene(FIX / "web_det.json")
    fake_plates(det)
    t0 = time.perf_counter()
    m1 = web_render.render_shot(det, 0, workers=1, log=quiet)
    t1 = time.perf_counter() - t0
    n0 = det["shots"][0]["frames"]
    info(f"1 navigateur : {n0} images en {t1:.1f} s = {t1 / n0:.3f} s/image (démarrage inclus) ; "
          f"moyenne interne {m1['timing']['seconds_per_frame']} s/image")
    hashes_1 = {k: v["sha256"] for k, v in m1["frames_done"].items()}
    shutil.rmtree(shot_dir(det, 0))
    t0 = time.perf_counter()
    m4 = web_render.render_shot(det, 0, workers=4, log=quiet)
    t4 = time.perf_counter() - t0
    info(f"4 navigateurs : {n0} images en {t4:.1f} s = {t4 / n0:.3f} s/image (démarrages inclus)")
    hashes_4 = {k: v["sha256"] for k, v in m4["frames_done"].items()}
    check("(b) mêmes empreintes avec 1 et 4 navigateurs (lots, processus, ordre différents)",
          hashes_1 == hashes_4 and len(hashes_1) == n0, f"{sum(hashes_1[k] == hashes_4.get(k) for k in hashes_1)}/{n0}")
    check("(b) relevés de mise en page présents (toutes les fps/5 images + dernière)",
          set(map(int, m4["layout"])) == set(range(0, n0, config.layout_every(25))) | {n0 - 1},
          f"{sorted(map(int, m4['layout']))}")
    m_b = web_render.render_shot(det, 1, workers=3, log=quiet)
    check("(b) plan avec plaque + boil rendu (3 navigateurs)", len(m_b["frames_done"]) == det["shots"][1]["frames"])

    # (c) 4K réelle ---------------------------------------------------------------------------------
    print("== (c) 4K : devicePixelRatio 2 ==")
    k4 = scene.compile_scene(FIX / "web_4k.json")
    with web_render.ShotSession(k4["shots"][0]["spec"]) as s:
        dpr = s.page.evaluate("devicePixelRatio")
    check("(c) devicePixelRatio = 2", dpr == 2, str(dpr))
    web_render.render_shot(k4, 0, workers=1, frames=[9], log=quiet)
    img4 = cv2.imread(str(shot_dir(k4, 0) / "000009.png"), cv2.IMREAD_UNCHANGED)
    check("(c) capture 3840x2160", img4 is not None and img4.shape[:2] == (2160, 3840), str(None if img4 is None else img4.shape))
    ratio4, n4 = block_nonuniform_ratio(img4)
    src = json.loads((FIX / "web_4k.json").read_text(encoding="utf-8"))
    src["id"] = "web_4k_as_1080"
    src["format"]["resolution"] = "1080p"
    k1 = scene.compile_scene(src)
    web_render.render_shot(k1, 0, workers=1, frames=[9], log=quiet)
    img1 = cv2.imread(str(shot_dir(k1, 0) / "000009.png"), cv2.IMREAD_UNCHANGED)
    up = cv2.resize(img1, (3840, 2160), interpolation=cv2.INTER_NEAREST)
    ratio_up, n_up = block_nonuniform_ratio(up)
    info(f"blocs 2x2 non uniformes sur les bords : 4K = {ratio4:.3f} ({n4} blocs de bord), "
          f"1080p agrandi = {ratio_up:.3f} ({n_up} blocs)")
    check("(c) blocs 2x2 NON uniformes sur les bords du texte (vrai 2x, pas un agrandissement)",
          ratio4 > 0.5 and ratio_up < 0.01, f"{ratio4:.3f} contre {ratio_up:.3f} pour un agrandissement")
    zoom = cv2.resize(img4[900:1100, 1300:1700, :3], (800, 400), interpolation=cv2.INTER_NEAREST)
    cv2.imwrite(str(TEST_DIR / "zoom_4k_text.png"), zoom)
    info(f"agrandissement d'un bord de texte 4K à regarder : {TEST_DIR / 'zoom_4k_text.png'}")

    # (d) alpha + flou de mouvement -------------------------------------------------------------------
    print("== (d) alpha 4K + flou de mouvement 4 sous-images ==")
    lt = scene.compile_scene(ROOT / "scenes" / "test_lowerthird_4k_alpha.json")
    t0 = time.perf_counter()
    web_render.render_shot(lt, 0, workers=2, frames=[8, 30], log=quiet)
    info(f"4K alpha avec flou 4 sous-images : {(time.perf_counter() - t0) / 2:.2f} s/image (démarrages inclus)")
    for f in (8, 30):
        a = cv2.imread(str(shot_dir(lt, 0) / f"{f:06d}.png"), cv2.IMREAD_UNCHANGED)
        alpha = a[..., 3]
        transparent = float((alpha == 0).mean() * 100)
        partial = int(((alpha > 0) & (alpha < 65535)).sum())
        check(f"(d) image {f} : PNG RGBA 16 bits 3840x2160", a.dtype == np.uint16 and a.shape == (2160, 3840, 4))
        check(f"(d) image {f} : pixels transparents > 0 %, alpha de 0 à 65535",
              transparent > 0 and int(alpha.min()) == 0 and int(alpha.max()) == 65535,
              f"{transparent:.1f} % transparents, alpha {int(alpha.min())}..{int(alpha.max())}, {partial} pixels semi-transparents")
    prev = cv2.imread(str(shot_dir(lt, 0) / "000008.png"), cv2.IMREAD_UNCHANGED)
    a8 = (prev[..., 3:4].astype(np.float32) / 65535.0)
    rgb8 = (prev[..., :3].astype(np.float32) / 65535.0)
    over_grey = rgb8 * a8 + 0.5 * (1 - a8)
    cv2.imwrite(str(TEST_DIR / "alpha_over_grey_f8.png"),
                cv2.resize((over_grey * 255).astype(np.uint8)[1500:2160, 0:2200], (1100, 330), interpolation=cv2.INTER_AREA))
    info(f"image 8 (flou d'entrée) incrustée sur gris à regarder : {TEST_DIR / 'alpha_over_grey_f8.png'}")

    # (e) déterminisme ------------------------------------------------------------------------------------
    print("== (e) porte de déterminisme ==")
    rep = web_render.verify_determinism(det, log=quiet)
    check("(e) web_det : 100 % des témoins identiques (glitch, discontinuité, fondu, plaque, boil, chart)",
          rep["ok"], f"{rep['matches']}/{rep['total']} ; ordre {rep['order']}")
    check("(e) determinism.json conforme au schéma", not list(v_det.iter_errors(rep)))
    fade = det["shots"][1]["fade_in_frames"]
    check("(e) témoins : premières/dernières images et images de fondu incluses",
          all(f"{sid}:{f}" in rep["order"] for sid, f in (("a", 0), ("a", 24), ("b", 0), ("b", 24), ("b", fade // 2))),
          f"{len(rep['order'])} témoins")
    chalk = scene.compile_scene(FIX / "web_chalk.json")
    web_render.render_shot(chalk, 0, workers=2, log=quiet)
    rep_c = web_render.verify_determinism(chalk, log=quiet)
    check("(e) web_chalk (on twos, boil, papier screen) : 100 % identiques", rep_c["ok"], f"{rep_c['matches']}/{rep_c['total']}")
    mc = load_manifest(chalk, 0)
    same_twos = all(mc["frames_done"][str(2 * k)]["sha256"] == mc["frames_done"][str(2 * k + 1)]["sha256"]
                    for k in range(chalk["shots"][0]["frames"] // 2))
    check("(e) frame_step 2 : chaque pose tenue exactement 2 images (empreintes égales par paires)", same_twos)

    # (f) reprise --------------------------------------------------------------------------------------------
    print("== (f) reprise ==")
    d = shot_dir(chalk, 0)
    before = {k: v["sha256"] for k, v in mc["frames_done"].items()}
    mtimes = {p.name: p.stat().st_mtime_ns for p in d.glob("[0-9]*.png")}
    for f in (3, 4, 5):
        (d / f"{f:06d}.png").unlink()
    man = json.loads((d / web_render.MANIFEST_NAME).read_text(encoding="utf-8"))
    for f in (10, 11):
        del man["frames_done"][str(f)]  # images présentes sur disque mais non consignées (arrêt brutal)
    (d / web_render.MANIFEST_NAME).write_text(json.dumps(man), encoding="utf-8")
    logs: list[str] = []
    m_r = web_render.render_shot(chalk, 0, workers=1, log=logs.append)
    rerendered = [p.name for p in d.glob("[0-9]*.png") if p.stat().st_mtime_ns != mtimes.get(p.name)]
    check("(f) seules les 3 images supprimées sont refaites ; les 2 non consignées sont adoptées",
          sorted(rerendered) == ["000003.png", "000004.png", "000005.png"] and any("adoptée" in l for l in logs),
          f"refaites {sorted(rerendered)}")
    check("(f) empreintes identiques après reprise", {k: v["sha256"] for k, v in m_r["frames_done"].items()} == before)

    # (g) réseau --------------------------------------------------------------------------------------------
    print("== (g) réseau bloqué ==")
    with web_render.ShotSession(chalk["shots"][0]["spec"]) as s:
        res = s.page.evaluate("""async () => {
            const out = {};
            for (const [k, u] of [["externe", "https://example.com/"],
                                  ["traversee", "http://mograph.render/node_modules/%2e%2e/pipeline/config.py"],
                                  ["hors_prefixe", "http://mograph.render/pipeline/config.py"],
                                  ["autorise", "http://mograph.render/runtime/clock.js"]]) {
                try { const r = await fetch(u); out[k] = r.status; } catch (e) { out[k] = "bloqué"; }
            }
            return out;
        }""")
        blocked = list(s.blocked)
        s.errors.clear()
        s.blocked.clear()
    info(f"réponses : {res} ; requêtes consignées comme bloquées : {len(blocked)}")
    check("(g) domaine externe bloqué", res["externe"] == "bloqué")
    check("(g) traversée de dossier et chemin hors préfixe refusés",
          res["traversee"] in ("bloqué", 404) and res["hors_prefixe"] in ("bloqué", 404))
    check("(g) ressource autorisée servie", res["autorise"] == 200)
    check("(g) toute requête refusée est consignée (et arrêterait le rendu)", len(blocked) >= 3, str(len(blocked)))

    # (h) police manquante -----------------------------------------------------------------------------------
    print("== (h) police manquante ==")
    bad = copy.deepcopy(chalk["shots"][0]["spec"])
    bad["fonts"][0]["url"] = bad["fonts"][0]["url"].replace(".woff2", "-nexiste-pas.woff2")
    msg = ""
    try:
        web_render.ShotSession(bad).open()
    except web_render.WebRenderError as exc:
        msg = str(exc)
    check("(h) police manquante => __MOGRAPH_ERROR__ et arrêt avec message", bool(msg) and ("olice" in msg or "font" in msg.lower()),
          msg.splitlines()[0][:220] if msg else "aucune erreur levée")

    n_fail = RESULTS.count(False)
    print(f"\nRÉSUMÉ étape 12 : {RESULTS.count(True)} PASS, {n_fail} FAIL")
    return 1 if n_fail else 0


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main())
