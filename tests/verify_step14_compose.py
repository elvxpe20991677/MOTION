"""Vérification de l'étape 14 : pipeline/compose.py (séquence maître).

Lancement (depuis la racine) : .venv/Scripts/python.exe tests/verify_step14_compose.py
Fichiers temporaires : build/_tests/step14/ (recréé à chaque lancement).
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TEST_DIR = ROOT / "build" / "_tests" / "step14"
# Le dossier build/ du pipeline est redirigé AVANT l'import de config (lu à l'import).
os.environ["MOGRAPH_BUILD_DIR"] = str(TEST_DIR / "build")
sys.path.insert(0, str(ROOT))

import cv2  # noqa: E402
import jsonschema  # noqa: E402
import numpy as np  # noqa: E402
import referencing  # noqa: E402
import referencing.jsonschema  # noqa: E402

from pipeline import config  # noqa: E402
from pipeline import compose  # noqa: E402

FAILS: list[str] = []
# Sortie UTF-8 même redirigée (sinon les accents sont perdus dans une console non Unicode).
sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def check(label: str, ok: bool, detail: str = "") -> bool:
    print(f"{'PASS' if ok else 'FAIL'} {label}" + (f" -- {detail}" if detail else ""))
    if not ok:
        FAILS.append(label)
    return ok


W, H, FPS = 64, 36, 25


def smoothstep(x: float) -> float:
    return x * x * (3 - 2 * x)


def pixel_sha(img: np.ndarray) -> str:
    return hashlib.sha256(np.ascontiguousarray(img).tobytes()).hexdigest()


def write_png(path: Path, img: np.ndarray) -> None:
    ok, buf = cv2.imencode(".png", img, [cv2.IMWRITE_PNG_COMPRESSION, 1])
    assert ok
    buf.tofile(str(path))


def read_png(path: Path) -> np.ndarray:
    return cv2.imdecode(np.fromfile(str(path), dtype=np.uint8), cv2.IMREAD_UNCHANGED)


def make_shot(scene_id: str, shot_id: str, frames: int, depth: int, bgra_of_frame) -> None:
    """Plan synthétique : images BGRA + manifeste conforme à shot_manifest.

    bgra_of_frame(i) renvoie un pixel (b, g, r, a) répété partout, ou une image (H, W, 4) complète.
    """
    d = config.shot_frames_dir(scene_id, shot_id)
    d.mkdir(parents=True, exist_ok=True)
    dtype = np.uint8 if depth == 8 else np.uint16
    done = {}
    for i in range(frames):
        img = np.empty((H, W, 4), dtype=dtype)
        img[:, :] = bgra_of_frame(i)
        write_png(d / f"{i:06d}.png", img)
        done[str(i)] = {"file": f"{i:06d}.png", "sha256": pixel_sha(img)}
    every = config.layout_every(FPS)
    layout = {str(i): [{"id": f"titre_{shot_id}", "text": shot_id.upper(), "box": [10.0, 10.0, 20.0, 8.0],
                        "opacity": 1.0}] for i in range(0, frames, every)}
    spec = {"id": shot_id}
    man = {
        "contract": "mograph-shot-manifest/1", "scene_id": scene_id, "shot_id": shot_id,
        "spec_sha256": hashlib.sha256(json.dumps(spec, sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
        "fps": FPS, "frames": frames, "width": W, "height": H, "depth": depth, "motion_blur_samples": 1,
        "frames_done": done, "layout_every": every, "layout": layout,
        "timing": {"seconds_per_frame": 0.01, "workers": 1, "total_seconds": 0.01 * frames},
    }
    (d / "manifest.json").write_text(json.dumps(man), encoding="utf-8")


def make_compiled(scene_id: str, depth: int, shots: list[tuple[str, int, int]]) -> dict:
    """shots = [(id, frames, fade_in_frames)] ; global_start calculé selon CONTRACT §3.2."""
    out, gs, prev = [], 0, None
    for idx, (sid, frames, fade) in enumerate(shots):
        if idx > 0:
            gs += prev - fade
        out.append({"id": sid, "index": idx, "frames": frames, "global_start": gs, "fade_in_frames": fade,
                    "spec": {"id": sid}, "plates": []})
        prev = frames
    total = gs + prev
    return {
        "contract": "mograph-scene/1", "scene_id": scene_id, "title": "test compose", "source": "test",
        "preset_id": "flat_riso", "format": {"aspect": "16:9", "resolution": "1080p", "fps": FPS, "alpha": True},
        "canvas": {"width": W, "height": H}, "device_scale_factor": 1, "output": {"width": W, "height": H},
        "frames": total, "duration": total / FPS, "frame_step": 1,
        "motion_blur": {"enabled": depth == 16, "samples": 4, "shutter": 0.5}, "depth": depth,
        "audio": None, "outputs": [], "qc": {}, "shots": out, "warnings": [],
    }


def master_validator():
    schema = json.loads((ROOT / "schema" / "internal" / "manifests.schema.json").read_text(encoding="utf-8"))
    res = referencing.Resource.from_contents(schema, default_specification=referencing.jsonschema.DRAFT202012)
    reg = referencing.Registry().with_resource(schema["$id"], res)
    return jsonschema.Draft202012Validator({"$ref": f"{schema['$id']}#/$defs/master_manifest"}, registry=reg)


def expected_blend(a_bgra, b_bgra, w, maxa, maxb, maxo):
    """Référence indépendante (float64) du mélange prémultiplié de deux pixels."""
    a = np.array(a_bgra, dtype=np.float64) / maxa
    b = np.array(b_bgra, dtype=np.float64) / maxb
    pa, pb = a[:3] * a[3], b[:3] * b[3]
    alpha = (1 - w) * a[3] + w * b[3]
    rgb = ((1 - w) * pa + w * pb) / alpha if alpha > 0 else np.zeros(3)
    return np.rint(np.concatenate([rgb, [alpha]]) * maxo)


# ---------------------------------------------------------------------------
# Scénario 1 : 8 bits, rouge (alpha 200) -> bleu (alpha 128), fondu de 10 images à 25 i/s
# ---------------------------------------------------------------------------

def scenario_8bit() -> None:
    sid = "t14_rgb8"
    F = 10
    # BGRA : le vert encode l'index local pour vérifier quelle image source est utilisée.
    red = lambda i: (0, 3 * i, 255, 200)      # noqa: E731
    blue = lambda i: (255, 100 + i, 0, 128)   # noqa: E731
    make_shot(sid, "rouge", 30, 8, red)
    make_shot(sid, "bleu", 30, 8, blue)
    compiled = make_compiled(sid, 8, [("rouge", 30, 0), ("bleu", 30, F)])

    master = config.master_dir(sid)
    master.mkdir(parents=True, exist_ok=True)
    stale = master / "999999.png"
    stale.write_bytes(b"perime")
    man = compose.compose_scene(compiled)

    pngs = sorted(p.name for p in master.glob("*.png"))
    check("[8 bits] compte exact d'images (30 + 30 - 10 = 50)", len(pngs) == 50 == compiled["frames"],
          f"{len(pngs)} images")
    check("[8 bits] noms 000000..000049 contigus", pngs == [f"{g:06d}.png" for g in range(50)])
    check("[8 bits] fichier périmé supprimé à la reconstruction", not stale.exists())
    errs = list(master_validator().iter_errors(json.loads((master / "manifest.json").read_text(encoding="utf-8"))))
    check("[8 bits] master/manifest.json valide master_manifest", not errs, "; ".join(e.message for e in errs[:3]))
    modes = [e["mode"] for e in man["frame_map"]]
    check("[8 bits] frame_map : 40 liens + 10 fondus", modes.count("link") == 40 and modes.count("blend") == 10,
          f"link={modes.count('link')} blend={modes.count('blend')} convert={modes.count('convert')}")

    # Liens physiques : aucune recopie pour les coupes.
    a0, sa0 = master / "000000.png", config.shot_frames_dir(sid, "rouge") / "000000.png"
    b_last, sb_last = master / "000049.png", config.shot_frames_dir(sid, "bleu") / "000029.png"
    check("[8 bits] image 0 = lien physique vers rouge/000000.png (samefile)", os.path.samefile(a0, sa0))
    check("[8 bits] image 49 = lien physique vers bleu/000029.png (samefile)", os.path.samefile(b_last, sb_last))
    check("[8 bits] st_nlink = 2 sur une image liée", os.stat(sa0).st_nlink == 2, f"st_nlink={os.stat(sa0).st_nlink}")

    # Poids smoothstep et correspondance des images sources dans le fondu.
    max_w_err, max_px_err = 0.0, 0
    both_present = True
    for k in range(F):
        g = 20 + k
        entry = man["frame_map"][g]
        w = smoothstep((k + 1) / (F + 1))
        (sa, la, wa), (sb, lb, wb) = entry["src"]
        max_w_err = max(max_w_err, abs(wb - w), abs(wa - (1 - w)))
        if (sa, la, sb, lb) != ("rouge", 30 - F + k, "bleu", k):
            check(f"[8 bits] sources du fondu k={k}", False, f"{entry['src']}")
        img = read_png(master / f"{g:06d}.png")
        exp = expected_blend(red(30 - F + k), blue(k), w, 255, 255, 255)
        max_px_err = max(max_px_err, int(np.abs(img.astype(np.int64) - exp.astype(np.int64)).max()))
        # Les deux plans contribuent à CHAQUE image du fondu (même faiblement aux extrémités).
        if not (img[..., 2].min() > 0 and img[..., 0].min() > 0):
            both_present = False
    check("[8 bits] poids du plan entrant = smoothstep((k+1)/(F+1)) (écart max < 1e-12)", max_w_err < 1e-12,
          f"écart max {max_w_err:.2e}")
    check("[8 bits] pixels de fondu = référence float64 à ±1 niveau", max_px_err <= 1, f"écart max {max_px_err}")
    mid = read_png(master / f"{20 + F // 2:06d}.png")
    b, g_, r, a = (int(v) for v in mid[0, 0])
    check("[8 bits] une image de fondu contient bien les deux plans (image médiane : R et B > 100)",
          r > 100 and b > 100, f"image {20 + F // 2} BGRA=({b},{g_},{r},{a})")
    check("[8 bits] chaque image du fondu contient les deux plans (R > 0 et B > 0)", both_present)
    first = read_png(master / "000020.png")[0, 0].astype(int)
    last = read_png(master / "000029.png")[0, 0].astype(int)
    check("[8 bits] ni la 1re ni la dernière image du fondu n'est 100 % A ou 100 % B",
          first[0] > 0 and first[2] > 0 and last[0] > 0 and last[2] > 0, f"k=0 {first.tolist()} k=9 {last.tolist()}")
    ws = [round(man["frame_map"][20 + k]["src"][1][2], 4) for k in range(F)]
    print(f"      poids smoothstep mesurés : {ws}")
    print(f"      BGRA des images 20..29 (pixel 0,0) : "
          f"{[read_png(master / f'{20 + k:06d}.png')[0, 0].tolist() for k in range(F)]}")

    # Relevés de mise en page ramenés en images GLOBALES.
    lay = man["layout"]
    check("[8 bits] layout : relevé local 0 du plan bleu -> image globale 20 avec shot_id",
          any(r.get("shot_id") == "bleu" for r in lay.get("20", [])), f"clés {sorted(lay, key=int)[:8]}...")

    # Seconde composition : reconstruction propre, liens recréés, pas de lien en trop.
    compose.compose_scene(compiled, log=lambda *_: None)
    check("[8 bits] recomposition : st_nlink reste 2 (l'ancien maître est bien supprimé)",
          os.stat(sa0).st_nlink == 2, f"st_nlink={os.stat(sa0).st_nlink}")
    check("[8 bits] aucun dossier master.tmp résiduel", not master.with_name("master.tmp").exists())


# ---------------------------------------------------------------------------
# Scénario 2 : maître 16 bits, plan A 8 bits (converti), plan B 16 bits (lié)
# ---------------------------------------------------------------------------

def scenario_16bit() -> None:
    sid = "t14_rgb16"
    F = 10
    red8 = lambda i: (0, 3 * i, 255, 200)          # noqa: E731
    blue16 = lambda i: (65535, 1000 * i, 0, 30000)  # noqa: E731
    make_shot(sid, "rouge8", 30, 8, red8)
    make_shot(sid, "bleu16", 30, 16, blue16)
    compiled = make_compiled(sid, 16, [("rouge8", 30, 0), ("bleu16", 30, F)])
    man = compose.compose_scene(compiled)
    master = config.master_dir(sid)
    modes = [e["mode"] for e in man["frame_map"]]
    check("[16 bits] compte exact d'images (50)", len(list(master.glob("*.png"))) == 50 and man["frames"] == 50)
    check("[16 bits] 20 conversions (plan 8 bits) + 20 liens (plan 16 bits) + 10 fondus",
          (modes.count("convert"), modes.count("link"), modes.count("blend")) == (20, 20, 10),
          f"convert={modes.count('convert')} link={modes.count('link')} blend={modes.count('blend')}")
    conv_ok = True
    for g in (0, 7, 19):
        img = read_png(master / f"{g:06d}.png")
        exp = np.array(red8(g), dtype=np.uint32) * 257
        if img.dtype != np.uint16 or not np.array_equal(img[0, 0].astype(np.uint32), exp):
            conv_ok = False
            print(f"      image {g} : {img.dtype} {img[0, 0].tolist()} attendu {exp.tolist()}")
    check("[16 bits] conversion 8 -> 16 bits exacte (v × 257, dtype uint16)", conv_ok)
    check("[16 bits] conversion : pas de lien physique (copie convertie)",
          not os.path.samefile(master / "000000.png", config.shot_frames_dir(sid, "rouge8") / "000000.png"))
    check("[16 bits] plan 16 bits : lien physique (samefile)",
          os.path.samefile(master / "000049.png", config.shot_frames_dir(sid, "bleu16") / "000029.png"))
    depths = {cv2.imdecode(np.fromfile(str(p), np.uint8), cv2.IMREAD_UNCHANGED).dtype for p in master.glob("*.png")}
    check("[16 bits] profondeur UNIQUE sur toute la séquence (uint16)", depths == {np.dtype(np.uint16)}, str(depths))
    max_err = 0
    for k in range(F):
        w = smoothstep((k + 1) / (F + 1))
        img = read_png(master / f"{20 + k:06d}.png")
        exp = expected_blend(red8(30 - F + k), blue16(k), w, 255, 65535, 65535)
        max_err = max(max_err, int(np.abs(img[0, 0].astype(np.int64) - exp.astype(np.int64)).max()))
    check("[16 bits] fondu 8 bits x 16 bits = référence float64 à ±1 niveau (sur 65535)", max_err <= 1,
          f"écart max {max_err}")


# ---------------------------------------------------------------------------
# Scénario 3 : fondus qui se chevauchent sur un plan court (3 plans actifs à la fois)
# ---------------------------------------------------------------------------

def scenario_overlap() -> None:
    sid = "t14_overlap"
    make_shot(sid, "a", 10, 8, lambda i: (0, 0, 255, 255))
    make_shot(sid, "b", 4, 8, lambda i: (0, 255, 0, 255))
    make_shot(sid, "c", 10, 8, lambda i: (255, 0, 0, 255))
    compiled = make_compiled(sid, 8, [("a", 10, 0), ("b", 4, 3), ("c", 10, 3)])
    man = compose.compose_scene(compiled, log=lambda *_: None)
    triple = [e for e in man["frame_map"] if len(e["src"]) == 3]
    sums_ok = all(abs(sum(s[2] for s in e["src"]) - 1) < 1e-12 for e in man["frame_map"])
    check("[chevauchement] total 10 + 4 + 10 - 3 - 3 = 18 images", man["frames"] == 18 == len(man["frame_map"]))
    check("[chevauchement] images à 3 plans mélangées, poids effectifs de somme 1", bool(triple) and sums_ok,
          f"{len(triple)} image(s) à 3 plans : {[e['g'] for e in triple]}")


# ---------------------------------------------------------------------------
# Scénario 4 : erreurs actionnables
# ---------------------------------------------------------------------------

def scenario_transparent() -> None:
    """Fondu d'un plan TOTALEMENT transparent (couleur non nulle sous alpha 0) vers un voile blanc
    d'alpha 1/255 à gauche et 0 à droite : là où l'alpha QUANTIFIÉ vaut 0, le RVB doit valoir 0."""
    sid = "t14_transp"
    # F = 4 : poids 0,104 / 0,352 / 0,648 / 0,896, aucun alpha à exactement ½ niveau (pas d'égalité
    # d'arrondi entre le calcul float32 du module et la référence).
    F = 4
    vide = lambda i: (40, 80, 120, 0)   # noqa: E731  (RVB « fantôme » sous un alpha nul)
    voile_img = np.zeros((H, W, 4), np.uint8)
    voile_img[:, : W // 2] = (255, 255, 255, 1)
    voile_img[:, W // 2:] = (255, 255, 255, 0)
    make_shot(sid, "vide", 12, 8, vide)
    make_shot(sid, "voile", 12, 8, lambda i: voile_img)
    compiled = make_compiled(sid, 8, [("vide", 12, 0), ("voile", 12, F)])
    man = compose.compose_scene(compiled, log=lambda *_: None)
    master = config.master_dir(sid)
    bad, seq = 0, []
    for e in man["frame_map"]:
        img = read_png(master / f"{e['g']:06d}.png")
        if e["mode"] == "blend":
            zero = img[..., 3] == 0
            bad += int(np.count_nonzero(img[..., :3][zero]))
            seq.append((e["g"], img[0, 0].tolist(), img[0, W - 1].tolist()))
    check("[transparence] fondus : alpha quantifié 0 => RVB 0 sur tous les pixels", bad == 0,
          f"{bad} composante(s) RVB non nulles sous alpha 0")
    lefts = [s[1] for s in seq]
    expected_alpha = [int(np.rint(smoothstep((k + 1) / (F + 1)) * 1)) for k in range(F)]
    check("[transparence] voile gauche : alpha = arrondi(w × 1), blanc dès que alpha ≥ 1",
          [p[3] for p in lefts] == expected_alpha
          and all(p[:3] == ([255, 255, 255] if p[3] else [0, 0, 0]) for p in lefts),
          f"BGRA gauche {lefts} ; alpha attendu {expected_alpha}")
    check("[transparence] partie droite (alpha 0 des deux côtés) : BGRA (0, 0, 0, 0)",
          all(s[2] == [0, 0, 0, 0] for s in seq), f"{[s[2] for s in seq]}")
    print(f"      fondu vide -> voile (g, BGRA gauche, BGRA droite) : {seq}")


def scenario_errors() -> None:
    sid = "t14_err"
    make_shot(sid, "x", 5, 8, lambda i: (0, 0, 0, 255))
    make_shot(sid, "y", 5, 8, lambda i: (0, 0, 0, 255))

    # Plan périmé : la scène a changé (même nombre d'images) depuis le rendu -> REFUS, jamais
    # d'encodage des anciennes images.
    stale = make_compiled(sid, 8, [("x", 5, 0), ("y", 5, 2)])
    stale["shots"][1]["spec"] = {"id": "y", "texte": "modifié après le rendu"}
    master = config.master_dir(sid)
    try:
        compose.compose_scene(stale, log=lambda *_: None)
        check("[erreurs] plan à empreinte périmée refusé", False, "aucune erreur levée (anciennes images composées)")
    except compose.ComposeError as exc:
        msg = str(exc)
        check("[erreurs] plan à empreinte périmée refusé avec message actionnable",
              "périmé" in msg and "« y »" in msg and "mograph.py render" in msg, msg.splitlines()[0][:200])
    check("[erreurs] plan périmé : aucun maître produit", not master.exists() and not master.with_name("master.tmp").exists())

    compiled = make_compiled(sid, 8, [("x", 5, 0), ("y", 5, 2)])
    (config.shot_frames_dir(sid, "y") / "000003.png").unlink()
    try:
        compose.compose_scene(compiled, log=lambda *_: None)
        check("[erreurs] image manquante refusée", False, "aucune erreur levée")
    except compose.ComposeError as exc:
        check("[erreurs] image manquante refusée avec message actionnable",
              "manquante" in str(exc) and "relancez" in str(exc).lower(), str(exc).splitlines()[0])
    bad = make_compiled(sid, 8, [("x", 5, 0), ("y", 5, 2)])
    bad["frames"] = 99
    try:
        compose.compose_scene(bad, log=lambda *_: None)
        check("[erreurs] total incohérent refusé", False, "aucune erreur levée")
    except compose.ComposeError as exc:
        check("[erreurs] total incohérent refusé", "Total incohérent" in str(exc), str(exc).splitlines()[0])


def main() -> int:
    if TEST_DIR.exists():
        shutil.rmtree(TEST_DIR)
    TEST_DIR.mkdir(parents=True)
    for fn in (scenario_8bit, scenario_16bit, scenario_overlap, scenario_transparent, scenario_errors):
        try:
            fn()
        except Exception:
            traceback.print_exc()
            check(f"{fn.__name__} s'exécute sans exception", False)
    print()
    if FAILS:
        print(f"ÉCHEC : {len(FAILS)} contrôle(s) en échec : {FAILS}")
        return 1
    print("SUCCÈS : tous les contrôles de l'étape 14 passent.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
