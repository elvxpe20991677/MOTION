"""Vérification de bout en bout des fonctions ajoutées après l'étape 20 (CLI, rendu, QC).

    .venv/bin/python tests/verify_features.py        (Windows : .venv\\Scripts\\python.exe)

Isolé : build/ et out/ redirigés vers build/_tests/features/ (MOGRAPH_BUILD_DIR / MOGRAPH_OUT_DIR),
médias de test générés sous assets/_tests/ (ignoré par git). Couvre :
  A. modèles (new --list, new, chaque modèle se compile)      G. lot + CSV (batch --data, --draft)
  B. transitions wipe / push (mélange pixel par pixel)         H. planches contact (dont alpha)
  C. calque vidéo + sous-titres SRT + normalisation audio      I. aperçu en direct (serveur + navigateur)
  D. détection du tempo (beats, --write)                       J. contrôle des niveaux dans le QC
  E. validate --layout (texte collé au bord refusé)            K. plaques 3D en parallèle du rendu web
  F. préversion (all --draft)                                  L. MOGRAPH_MAX_WORKERS
"""

from __future__ import annotations

import csv
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import cv2  # noqa: E402
import numpy as np  # noqa: E402

TEST = ROOT / "build" / "_tests" / "features"
BUILD, OUT, SC = TEST / "build", TEST / "out", TEST / "scenes"
ASSETS = ROOT / "assets" / "_tests"
ENV = dict(os.environ, MOGRAPH_BUILD_DIR=str(BUILD), MOGRAPH_OUT_DIR=str(OUT), PYTHONIOENCODING="utf-8")
PY = sys.executable
FAILS: list[str] = []
QC = {"safe_zone": "title", "safe_zone_tolerance_s": 0.5, "allow_black_s": 0, "determinism_frames": 3,
      "loudness_target_lufs": -14}


def check(label: str, ok: bool, detail: str = "") -> bool:
    print(("PASS " if ok else "FAIL ") + label + (f" — {detail}" if detail else ""), flush=True)
    if not ok:
        FAILS.append(label)
    return ok


def info(text: str) -> None:
    print("INFO " + text, flush=True)


def mograph(*args: str, timeout: float = 1200) -> subprocess.CompletedProcess:
    return subprocess.run([PY, str(ROOT / "mograph.py"), *args], cwd=ROOT, env=ENV, capture_output=True,
                          text=True, encoding="utf-8", errors="replace", timeout=timeout)


def scene(sid: str, shots: list, **extra) -> Path:
    d = {"version": "1.0", "id": sid, "title": sid, "brief": "test", "seed": 7, "preset": extra.pop("preset", "flat_riso"),
         "format": extra.pop("format", {"aspect": "16:9", "resolution": "1080p", "fps": 25, "alpha": False}),
         "outputs": extra.pop("outputs", [{"profile": "hevc_main10"}]), "qc": QC, **extra, "shots": shots}
    p = SC / f"{sid}.json"
    p.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
    return p


def master(sid: str, g: int) -> np.ndarray:
    return cv2.imread(str(BUILD / sid / "master" / f"{g:06d}.png"), cv2.IMREAD_UNCHANGED)


def _pink(row: np.ndarray) -> np.ndarray:
    """Pixels du rose « secondary » de flat_riso (#FF4F9A), distincts du bleu primary et du jaune accent."""
    return (row[:, 2] > 200) & (row[:, 0] > 120) & (row[:, 1] < 120)


def tail(cp: subprocess.CompletedProcess, n: int = 12) -> str:
    return "\n".join((cp.stdout + cp.stderr).strip().splitlines()[-n:])


# ---------------------------------------------------------------------------------------------------
def test_templates() -> None:
    print("== A. Modèles ==")
    cp = mograph("new", "--list")
    names = sorted(p.stem for p in (ROOT / "templates").glob("*.json"))
    check("new --list : code 0 et tous les modèles listés", cp.returncode == 0 and all(n in cp.stdout for n in names),
          ", ".join(names))
    from pipeline import scene as scene_mod
    for n in names:
        data = json.loads((ROOT / "templates" / f"{n}.json").read_text(encoding="utf-8"))
        try:
            c = scene_mod.compile_scene(data)
            check(f"modèle {n} : se compile sans avertissement", not c["warnings"], str(c["warnings"])[:200])
        except scene_mod.SceneError as exc:
            check(f"modèle {n} : se compile", False, str(exc)[:300])
    dest = ROOT / "scenes" / "zz_test_new.json"
    try:
        cp = mograph("new", "zz_test_new", "--template", "citation", "--title", "Essai")
        data = json.loads(dest.read_text(encoding="utf-8")) if dest.exists() else {}
        check("new <id> --template : scène créée, id et titre remplacés, valide",
              cp.returncode == 0 and data.get("id") == "zz_test_new" and data.get("title") == "Essai", tail(cp, 3))
        cp2 = mograph("new", "zz_test_new", "--template", "citation")
        check("new : refuse d'écraser sans --force", cp2.returncode == 1 and "existe déjà" in cp2.stderr)
        cp3 = mograph("new", "Mauvais__Id", "--template", "titre")
        check("new : id invalide refusé avec message", cp3.returncode == 1 and "invalide" in cp3.stderr)
    finally:
        dest.unlink(missing_ok=True)


def test_transitions() -> None:
    print("== B. Transitions volet / poussée ==")
    for kind in ("wipe", "push"):
        sid = f"f_{kind}"
        p = scene(sid, [
            {"id": "a", "duration": 1, "background": "primary", "layers": [
                {"id": "carre", "type": "shape", "shape": "rect", "w": 200, "h": 200, "fill": "accent", "x": 960, "y": 540}]},
            {"id": "b", "duration": 1, "background": "secondary",
             "transition_in": {"type": kind, "dur": 0.4, "direction": "left"}, "layers": []}])
        cp = mograph("render", str(p), "--workers", "2")
        cp2 = mograph("compose", str(p))
        if not check(f"{kind} : rendu et composition", cp.returncode == 0 and cp2.returncode == 0, tail(cp) + tail(cp2)):
            continue
        man = json.loads((BUILD / sid / "master" / "manifest.json").read_text(encoding="utf-8"))
        modes = [f["mode"] for f in man["frame_map"]]
        check(f"{kind} : 10 images de transition (mode {kind}) aux images 15..24",
              modes[15:25] == [kind] * 10 and modes[14] == "link" and modes[25] == "link", str(modes[13:27]))
        fr = [float(_pink(master(sid, g)[540]).mean()) for g in range(14, 26)]
        check(f"{kind} : part du plan entrant croissante 0 -> 1", fr[0] == 0.0 and fr[-1] == 1.0
              and all(b >= a for a, b in zip(fr, fr[1:])), " ".join(f"{x:.2f}" for x in fr))
        if kind == "push":
            # Le carré jaune du plan sortant est poussé vers la gauche du même décalage que le bord du plan entrant.
            img = master(sid, 19)
            yellow = np.nonzero((img[540, :, 2] > 200) & (img[540, :, 1] > 150) & (img[540, :, 0] < 120))[0]
            pink_start = int(np.nonzero(_pink(img[540]))[0].min())
            shift = 1920 - pink_start
            check("push : le contenu du plan sortant est translaté (carré décalé du même pas que le bord)",
                  yellow.size > 0 and abs((860 - shift) - int(yellow.min())) <= 3,
                  f"carré à x={int(yellow.min()) if yellow.size else None}, attendu {860 - shift}")


def test_video_subtitles_audio() -> None:
    print("== C. Calque vidéo + sous-titres + normalisation audio ==")
    ASSETS.mkdir(parents=True, exist_ok=True)
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=25:duration=3",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", "-colorspace", "bt709", str(ASSETS / "mire.mp4")], check=True)
    (ASSETS / "st.srt").write_text("1\n00:00:00,400 --> 00:00:01,200\nPremière <i>réplique</i>\n\n"
                                   "2\n00:00:01,600 --> 00:00:02,400\nSeconde réplique\n", encoding="utf-8")
    cp = mograph("synth-beat", "--bpm", "120", "--duration", "3", "--out", "assets/_tests/calme.wav", "--target-lufs", "-26")
    check("synth-beat : piste calme -26 LUFS", cp.returncode == 0, tail(cp, 2))
    sid = "f_media"
    p = scene(sid, [{"id": "a", "duration": 2.8, "background": "bg", "layers": [
        {"id": "film", "type": "video", "src": "assets/_tests/mire.mp4", "w": 1200, "h": 600, "fit": "contain", "x": 960, "y": 470},
        {"id": "st", "type": "subtitles", "src": "assets/_tests/st.srt", "color": "fg"}]}],
        outputs=[{"profile": "hevc_main10"}, {"profile": "prores_422hq"}],
        audio={"src": "assets/_tests/calme.wav", "bpm": 120, "normalize": True})
    cp = mograph("all", str(p), "--workers", "2")
    check("all (vidéo + sous-titres + normalize) : code 0, QC CONFORME", cp.returncode == 0 and "Verdict : CONFORME" in cp.stdout,
          tail(cp))
    comp = json.loads((BUILD / sid / "compiled.json").read_text(encoding="utf-8"))
    layers = comp["shots"][0]["spec"]["layers"]
    ids = [(l["id"], l["type"]) for l in layers]
    check("sous-titres : une réplique = un calque texte (st-001, st-002)", ("st-001", "text") in ids and ("st-002", "text") in ids
          and not any(t == "subtitles" for _, t in ids), str(ids))
    st1 = next(l for l in layers if l["id"] == "st-001")
    check("sous-titres : balises retirées, fondus d'entrée à 0,4 s et de sortie à 1,2 s",
          st1["text"] == "Première réplique" and abs(st1["tweens"][0]["start"] - 0.4) < 1e-6
          and abs(st1["tweens"][1]["start"] + st1["tweens"][1]["dur"] - 1.2) < 1e-6, json.dumps(st1["tweens"])[:200])
    media_dir = BUILD / sid / "media" / "a__film"
    frames = sorted(media_dir.glob("[0-9]" * 6 + ".png"))
    check("vidéo : 70 images extraites (2,8 s à 25 i/s), 1200x600", len(frames) == 70
          and cv2.imread(str(frames[0]), cv2.IMREAD_UNCHANGED).shape[:2] == (600, 1200), f"{len(frames)} images")
    f0 = cv2.imread(str(frames[0]), cv2.IMREAD_UNCHANGED)
    check("vidéo fit contain : bandes latérales transparentes (alpha 0), centre opaque",
          f0.shape[2] == 4 and int(f0[300, 5, 3]) == 0 and int(f0[300, 600, 3]) == 255)
    f10, f11 = (cv2.imread(str(frames[k])) for k in (10, 11))
    check("vidéo : les images successives diffèrent (la source bouge)", float(np.abs(f10.astype(int) - f11.astype(int)).mean()) > 0.1)
    stamp = (media_dir / "media.json").stat().st_mtime_ns
    cp = mograph("render", str(p), "--workers", "2")
    check("vidéo : extraction réutilisée au 2e rendu (media.json inchangé)",
          cp.returncode == 0 and (media_dir / "media.json").stat().st_mtime_ns == stamp)
    # Sous-titres visibles au bon moment : zone du bas du cadre, image 20 (0,8 s) contre image 30 (1,2 s + fondu fini).
    a, b = master(sid, 20), master(sid, 32)
    band = slice(900, 1030)
    check("sous-titres : texte présent à 0,8 s, absent à 1,28 s (contraste de la bande basse)",
          float(a[band, 400:1520, :3].std()) > float(b[band, 400:1520, :3].std()) + 10,
          f"écart-type {a[band, 400:1520, :3].std():.1f} / {b[band, 400:1520, :3].std():.1f}")
    rep = json.loads((OUT / sid / "qc_report.json").read_text(encoding="utf-8"))
    by = {c["id"]: c for c in rep["checks"]}
    loud = by.get("hevc_main10.loudness", {})
    check("normalize : source -26 LUFS livrée à -14 ± 1 LUFS", loud.get("status") == "PASS",
          str(loud.get("actual")))
    lv = [c for c in rep["checks"] if c["id"].endswith(".levels")]
    check("J. QC : contrôle des niveaux présent pour chaque livrable, informatif (PASS ou WARN, jamais bloquant)",
          len(lv) == 2 and all(c["status"] in ("PASS", "WARN") and not c["blocking"] for c in lv),
          str([c.get("actual") for c in lv])[:200])
    check("H. planche contact produite par all", (OUT / sid / "planche_contact.png").is_file())


def test_beats() -> None:
    print("== D. Détection du tempo ==")
    from pipeline import audio
    ASSETS.mkdir(parents=True, exist_ok=True)
    for bpm, off in ((100, 0.4), (128, 0.1)):
        f = audio.synth_beat(ASSETS / f"b{bpm}.wav", bpm, 10, offset=off)
        r = audio.analyse_tempo(f)
        check(f"beats : {bpm} BPM / premier temps {off} s retrouvés (± 0,1 BPM, ± 10 ms)",
              abs(r["bpm"] - bpm) <= 0.1 and abs(r["offset"] - off) <= 0.010 and r["confidence"] >= 0.9, str(r))
    p = scene("f_beats", [{"id": "a", "duration": 2, "layers": []}])
    cp = mograph("beats", "assets/_tests/b128.wav", "--scene", str(p), "--write")
    data = json.loads(p.read_text(encoding="utf-8"))
    au = data.get("audio") or {}
    check("beats --scene --write : bloc audio écrit (src, bpm, offset) et scène valide",
          cp.returncode == 0 and au.get("src") == "assets/_tests/b128.wav" and abs(au.get("bpm", 0) - 128) < 0.1
          and mograph("validate", str(p)).returncode == 0, tail(cp, 3))


def test_layout() -> None:
    print("== E. validate --layout ==")
    bad = scene("f_layout_bad", [{"id": "a", "duration": 1, "layers": [
        {"id": "deborde", "type": "text", "text": "Déborde à gauche", "style": "body", "size": 60, "x": 80, "y": "50%",
         "anchor": "left", "anim": [{"move": "rise", "at": 0}]}]}])
    edge = scene("f_layout_edge", [{"id": "a", "duration": 1, "layers": [
        {"id": "coin", "type": "text", "text": "Collé au bord", "style": "body", "size": 60, "x": "safe:0%", "y": "safe:0%",
         "anchor": "top-left"}]}])
    good = scene("f_layout_ok", [{"id": "a", "duration": 1, "layers": [
        {"id": "centre", "type": "text", "text": "Au centre", "style": "body", "size": 60, "x": "50%", "y": "50%"}]}])
    t = time.perf_counter()
    cb = mograph("validate", str(bad), "--layout")
    dt = time.perf_counter() - t
    ce = mograph("validate", str(edge), "--layout")
    cg = mograph("validate", str(good), "--layout")
    check("validate --layout : texte qui sort de la zone sûre (16 px) -> code 1, « HORS ZONE »",
          cb.returncode == 1 and "HORS ZONE" in cb.stdout, tail(cb, 4))
    check("validate --layout : texte collé au bord -> avertissement « PRÈS DU BORD », code 0",
          ce.returncode == 0 and "PRÈS DU BORD" in ce.stdout, tail(ce, 3))
    check("validate --layout : texte centré -> marge confortable, CONFORME", cg.returncode == 0
          and "marge confortable" in cg.stdout, tail(cg, 3))
    info(f"validate --layout : {dt:.1f} s pour 1 s de scène (images de relevé seulement)")


def test_draft_batch_sheets() -> None:
    print("== F/G/H. Préversion, lot + CSV, planches ==")
    tpl = scene("f_tpl", [{"id": "a", "duration": 1.2, "background": "bg", "layers": [
        {"id": "nom", "type": "text", "text": "{{nom}}", "style": "display", "size": 90, "x": "50%", "y": "40%"},
        {"id": "graph", "type": "chart", "w": 700, "h": 300, "x": 960, "y": 750,
         "data": [{"label": "A", "value": "{{v1}}"}, {"label": "B", "value": "{{v2}}"}], "grow": {"at": 0, "dur": 0.8}}]}])
    csv_path = SC / "donnees.csv"
    with open(csv_path, "w", encoding="utf-8-sig", newline="") as fh:
        w = csv.writer(fh, delimiter=";")
        w.writerow(["id", "nom", "v1", "v2"])
        w.writerow(["Équipe Nord", "Nord", "12", "30,5"])
        w.writerow(["", "Sud", "7", "9"])
    cp = mograph("batch", str(tpl), "--data", str(csv_path), "--draft", "--workers", "2")
    gen = sorted((BUILD / "_variants" / "f_tpl").glob("*.json"))
    check("batch --data : 2 scènes générées (id depuis la colonne id, sinon numéro)",
          [g.stem for g in gen] == ["f_tpl_002", "f_tpl_equipe_nord"], str([g.stem for g in gen]))
    if gen:
        d = json.loads(gen[1].read_text(encoding="utf-8"))
        vals = [x["value"] for x in d["shots"][0]["layers"][1]["data"]]
        check("batch --data : valeurs numériques converties (12, 30,5 -> 30.5), texte remplacé",
              vals == [12, 30.5] and d["shots"][0]["layers"][0]["text"] == "Nord", str(vals))
    outs = [OUT / "f_tpl_equipe_nord_draft" / "f_tpl_equipe_nord_draft_hevc_main10.mp4",
            OUT / "f_tpl_002_draft" / "f_tpl_002_draft_hevc_main10.mp4"]
    check("batch --draft : code 0, une vidéo par ligne, bilan écrit", cp.returncode == 0 and all(o.is_file() for o in outs)
          and "BILAN DU LOT" in cp.stdout, tail(cp, 6))
    check("F. préversion : ni QC ni déterminisme (pas de qc_report), planche produite",
          not (OUT / "f_tpl_002_draft" / "qc_report.json").exists()
          and (OUT / "f_tpl_002_draft" / "planche_contact.png").is_file())
    bad_csv = SC / "mauvais.csv"
    bad_csv.write_text("nom;v1\nX;1\n", encoding="utf-8")
    cp = mograph("batch", str(tpl), "--data", str(bad_csv))
    check("batch --data : colonne manquante refusée AVANT tout rendu", cp.returncode == 1 and "v2" in cp.stderr, tail(cp, 2))
    # Planche alpha : préversion du modèle lower_third (fond transparent).
    cp = mograph("all", str(ROOT / "templates" / "lower_third.json"), "--draft", "--workers", "2")
    alpha_sheet = OUT / "modele_lower_third_draft" / "planche_alpha.png"
    ok = cp.returncode == 0 and alpha_sheet.is_file()
    check("H. planche alpha (fond clair | sombre) pour une scène transparente", ok, tail(cp, 4))
    if ok:
        img = cv2.imread(str(alpha_sheet))
        check("H. planche alpha : vignettes claire ET sombre présentes", img.shape[1] > 1500
              and float(img[:, :img.shape[1] // 4].mean()) > float(img[:, img.shape[1] // 4:img.shape[1] // 2].mean()) + 60)


def test_preview() -> None:
    print("== I. Aperçu en direct ==")
    from playwright.sync_api import sync_playwright
    from pipeline import config, preview
    p = scene("f_preview", [
        {"id": "a", "duration": 1, "background": "primary", "layers": [
            {"id": "t", "type": "text", "text": "Aperçu", "size": 120, "color": "bg", "x": "50%", "y": "50%"}]},
        {"id": "b", "duration": 1, "background": "secondary", "transition_in": {"type": "push", "dur": 0.4}, "layers": []}])
    ready, stop = threading.Event(), threading.Event()
    th = threading.Thread(target=preview.serve, args=(p,), daemon=True,
                          kwargs=dict(port=0, open_browser=False, ready_event=ready, stop_event=stop, log=lambda m: None))
    th.start()
    if not check("serveur d'aperçu démarré", ready.wait(120)):
        return
    try:
        with sync_playwright() as pw:
            b = pw.chromium.launch(args=list(config.CHROMIUM_ARGS))
            pg = b.new_page(viewport={"width": 1300, "height": 900})
            errs: list[str] = []
            pg.on("pageerror", lambda e: errs.append(str(e)))
            pg.goto(ready.url)  # type: ignore[attr-defined]
            pg.wait_for_function("window.__preview && window.__preview.state().ready.length === 2", timeout=60000)
            check("aperçu : les 2 plans chargés (runtime prêt dans chaque cadre)", True)
            pg.evaluate("window.__preview.show(20)")
            pg.wait_for_function("window.__preview.state().frame === 20", timeout=10000)
            tr = pg.evaluate("document.querySelectorAll('#stage iframe')[1].style.transform")
            check("aperçu : poussée reproduite (plan entrant translaté à mi-transition)", "translate(" in tr, tr)
            txt = p.read_text(encoding="utf-8")
            p.write_text(txt.replace('"duration": 1,', '"duration": 1.01,', 1), encoding="utf-8")
            pg.wait_for_function("window.__preview.state().error !== null", timeout=15000)
            err = pg.evaluate("window.__preview.state().error")
            check("aperçu : erreur de compilation affichée sans arrêter le serveur", "non entier" in err, err[:120])
            p.write_text(txt, encoding="utf-8")
            pg.wait_for_function("window.__preview.state().error === null && window.__preview.state().ready.length === 2",
                                 timeout=30000)
            check("aperçu : rechargé automatiquement après correction, sans erreur JavaScript", not errs, str(errs)[:200])
            b.close()
    finally:
        stop.set()
        th.join(10)


def test_parallel_plates() -> None:
    print("== K. Plaques 3D en parallèle du rendu web ==")
    sid = "f_para"
    p = scene(sid, [
        {"id": "texte", "duration": 1, "background": "bg", "layers": [
            {"id": "t", "type": "text", "text": "Pendant la 3D", "size": 80, "x": "50%", "y": "50%", "anim": [{"move": "rise", "at": 0}]}]},
        {"id": "trois_d", "duration": 0.5, "plates": [{"id": "p", "world": {"color": "bg", "strength": 1.0},
            "camera": {"location": [0, -8, 2], "target": [0, 0, 0.5], "lens": 50},
            "objects": [{"id": "s", "primitive": "sphere", "size": 1, "location": [0, 0, 0.5], "material": "lilac"}]}],
         "layers": [{"id": "plaque", "type": "sequence", "plate": "p", "w": 540, "h": 540, "x": 540, "y": 540}]}],
        preset="clay_3d", format={"aspect": "1:1", "resolution": "1080p", "fps": 24, "alpha": False},
        quality={"plate_scale": 0.25, "blender_samples": 4})
    cp = mograph("render", str(p), "--workers", "2")
    out = cp.stdout
    i_web = out.find("IMAGES WEB pendant les plaques 3D")
    i_plate_done = max(out.rfind("image 11/12"), out.rfind("12/12"))
    check("render : plan sans plaque rendu PENDANT Blender (bannière dédiée), code 0", cp.returncode == 0 and i_web >= 0, tail(cp, 5))
    web = sorted((BUILD / sid / "shots" / "texte").glob("[0-9]" * 6 + ".png"))
    plates = sorted((BUILD / sid / "plates" / "trois_d__p").glob("[0-9]" * 6 + ".png"))
    if web and plates:
        first_web = min(f.stat().st_mtime for f in web)
        last_plate = max(f.stat().st_mtime for f in plates)
        check("render : images web écrites avant la fin des plaques (chevauchement réel)", first_web < last_plate,
              f"1re image web {first_web - last_plate:+.1f} s par rapport à la dernière plaque")
    check("render : plan à plaque rendu après la plaque (12 images)",
          len(list((BUILD / sid / "shots" / "trois_d").glob("[0-9]" * 6 + ".png"))) == 12)
    info(f"(indices dans le journal : web {i_web}, fin plaque {i_plate_done})")


def test_max_workers() -> None:
    print("== L. MOGRAPH_MAX_WORKERS ==")
    r = subprocess.run([PY, "-c", "from pipeline import config, web_render; print(config.WEB_MAX_WORKERS, web_render.MAX_WORKERS)"],
                       cwd=ROOT, env=dict(ENV, MOGRAPH_MAX_WORKERS="7"), capture_output=True, text=True)
    check("MOGRAPH_MAX_WORKERS=7 -> plafond de navigateurs 7", r.stdout.split() == ["7", "7"], r.stdout + r.stderr)


def main() -> int:
    assert TEST.parent.name == "_tests"
    if TEST.exists():
        shutil.rmtree(TEST)
    SC.mkdir(parents=True)
    t0 = time.perf_counter()
    for fn in (test_templates, test_transitions, test_video_subtitles_audio, test_beats, test_layout,
               test_draft_batch_sheets, test_preview, test_parallel_plates, test_max_workers):
        try:
            fn()
        except Exception as exc:  # noqa: BLE001 — un bloc en erreur n'empêche pas les suivants
            import traceback
            traceback.print_exc()
            check(f"{fn.__name__} s'exécute sans exception", False, str(exc)[:200])
    print(f"\nDurée : {time.perf_counter() - t0:.0f} s")
    if FAILS:
        print(f"ÉCHEC : {len(FAILS)} contrôle(s) : {FAILS}")
        return 1
    print("SUCCÈS : toutes les nouveautés fonctionnent de bout en bout.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
