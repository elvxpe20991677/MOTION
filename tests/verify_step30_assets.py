"""Vérification de la phase 3 : assets IA gelés (pipeline/assets.py, calque ai_clip, CONTRACT §11.4).

Lancement depuis la racine :  .venv/Scripts/python.exe tests/verify_step30_assets.py
Isolé : build/ et out/ redirigés vers build/_tests/step30/, verrou de test build/_tests/step30/
assets.lock.json, dossier IA de test assets/_tests/ai30/ (ignoré par git) ; rien n'est écrit dans le
vrai assets.lock.json ni dans assets/ai/.
Contrôles :
  A. assets add / verify / list : copie sous le dossier IA, empreinte, provenance conforme au schéma,
     idempotence, refus d'écraser un asset gelé, options obligatoires ;
  B. calque ai_clip compilé vers le chemin « video » (séquence, job d'extraction, empreinte du verrou) ;
  C. rendu complet (all) : porte de déterminisme 100 %, QC CONFORME, provenance dans qc_report.md ;
  D. refus : empreinte modifiée (compilation, rendu, encodage, QC), asset absent du verrou, absent du
     disque, hors du dossier IA, nature incorrecte ;
  E. alpha d'un clip ProRes 4444 conservé par l'extraction.
Affiche PASS/FAIL ; code de sortie ≠ 0 au moindre échec.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TEST = ROOT / "build" / "_tests" / "step30"
AI = ROOT / "assets" / "_tests" / "ai30"
SRC = ROOT / "assets" / "_tests" / "ai30_src"
LOCK = TEST / "assets.lock.json"
# Environnement redirigé AVANT l'import de pipeline.config (lu à l'import), pour ce processus et la CLI.
os.environ.update({"MOGRAPH_BUILD_DIR": str(TEST / "build"), "MOGRAPH_OUT_DIR": str(TEST / "out"),
                   "MOGRAPH_AI_ASSETS_DIR": str(AI), "MOGRAPH_ASSETS_LOCK": str(LOCK), "PYTHONIOENCODING": "utf-8"})
sys.path.insert(0, str(ROOT))

import cv2  # noqa: E402
import numpy as np  # noqa: E402
from jsonschema import Draft202012Validator  # noqa: E402

from pipeline import assets, config, media, qc  # noqa: E402
from pipeline import scene as scene_mod  # noqa: E402

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

PY = sys.executable
FAILS: list[str] = []
QC = {"safe_zone": "title", "safe_zone_tolerance_s": 0.5, "allow_black_s": 0, "determinism_frames": 4,
      "loudness_target_lufs": -14}
DECL = {"ai_used": True, "roles": ["video"], "tools": [{"name": "Test", "model": "lavfi", "role": "plan"}],
        "date": "2026-10-01", "statement": "Plan généré (mire de test)"}


def check(label: str, ok: bool, detail: str = "") -> bool:
    print(("PASS " if ok else "FAIL ") + label + (f" -- {detail}" if detail else ""), flush=True)
    if not ok:
        FAILS.append(label)
    return ok


def mograph(*args: str, timeout: float = 1200) -> subprocess.CompletedProcess:
    return subprocess.run([PY, str(ROOT / "mograph.py"), *args], cwd=ROOT, env=dict(os.environ),
                          capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)


def tail(cp: subprocess.CompletedProcess, n: int = 6) -> str:
    return " | ".join((cp.stdout + cp.stderr).strip().splitlines()[-n:])


def ffmpeg(*args: str) -> None:
    subprocess.run([config.FFMPEG, "-hide_banner", "-v", "error", "-y", *args], check=True, capture_output=True)


def scene(sid: str, layer: dict, *, provenance: dict | None = None, duration: float = 1.0) -> Path:
    d = {"version": "1.0", "id": sid, "title": sid, "brief": "test phase 3", "seed": 7, "preset": "flat_riso",
         "format": {"aspect": "16:9", "resolution": "1080p", "fps": 25, "alpha": False},
         "outputs": [{"profile": "hevc_main10"}], "qc": QC,
         "shots": [{"id": "a", "duration": duration, "layers": [layer]}]}
    if provenance:
        d["provenance"] = provenance
    p = TEST / "scenes" / f"{sid}.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
    return p


def clip_layer(asset: str, **extra) -> dict:
    return {"id": "clip", "type": "ai_clip", "asset": asset, "w": 1920, "h": 1080, **extra}


def rel(p: Path) -> str:
    return p.resolve().relative_to(ROOT).as_posix()


def add_cli(src: Path, *extra: str) -> subprocess.CompletedProcess:
    return mograph("assets", "add", rel(src), "--tool", "Test", "--model", "lavfi", "--prompt", "mire testsrc2 animée",
                   "--license", "test interne", "--seed", "7", "--cost", "0 crédit", *extra)


# ---------------------------------------------------------------------------------------------------
def test_add_verify_list() -> Path:
    print("== A. assets add / verify / list ==")
    ffmpeg("-f", "lavfi", "-i", "testsrc2=size=640x360:rate=25:duration=3", "-c:v", "libx264", "-pix_fmt", "yuv420p",
           "-colorspace", "bt709", str(SRC / "mire.mp4"))
    cp = add_cli(SRC / "mire.mp4")
    dest = AI / "video" / "mire.mp4"
    lock = json.loads(LOCK.read_text(encoding="utf-8")) if LOCK.is_file() else {}
    key = rel(dest)
    entry = (lock.get("assets") or {}).get(key, {})
    schema = json.loads((config.SCHEMA_DIR / "assets_lock.schema.json").read_text(encoding="utf-8"))
    errs = list(Draft202012Validator(schema).iter_errors(lock))
    check("assets add : code 0, fichier copié sous le dossier IA (video/), entrée du verrou conforme au schéma",
          cp.returncode == 0 and dest.is_file() and not errs and "ajouté" in cp.stdout, tail(cp) + str(errs[:1]))
    pv = entry.get("provenance") or {}
    check("assets add : SHA-256 exact, taille, nature video, provenance complète (outil, modèle, prompt, licence, graine, "
          "coût, source manual_import, date du jour)",
          entry.get("sha256") == assets.file_sha256(dest) and entry.get("bytes") == dest.stat().st_size
          and entry.get("kind") == "video" and pv.get("tool") == "Test" and pv.get("model") == "lavfi"
          and pv.get("prompt") == "mire testsrc2 animée" and pv.get("license") == "test interne" and pv.get("seed") == 7
          and pv.get("cost") == "0 crédit" and pv.get("source") == "manual_import" and len(pv.get("date", "")) == 10,
          json.dumps(entry, ensure_ascii=False)[:300])
    check("assets add : caractéristiques ffprobe relevées (640x360, 25 i/s, 3 s, h264, sans alpha)",
          entry.get("media", {}).get("width") == 640 and entry["media"].get("fps") == "25/1"
          and abs(entry["media"].get("duration_s", 0) - 3) < 0.05 and entry["media"].get("alpha") is False,
          str(entry.get("media")))
    added = entry.get("added")
    cp2 = add_cli(SRC / "mire.mp4")
    lock2 = json.loads(LOCK.read_text(encoding="utf-8"))
    check("assets add du même fichier : idempotent (inchangé, date d'ajout conservée)",
          cp2.returncode == 0 and "inchangé" in cp2.stdout and lock2["assets"][key]["added"] == added, tail(cp2, 2))
    (SRC / "autre").mkdir(exist_ok=True)
    ffmpeg("-f", "lavfi", "-i", "testsrc=size=640x360:rate=25:duration=1", "-c:v", "libx264", "-pix_fmt", "yuv420p",
           str(SRC / "autre" / "mire.mp4"))
    cp3 = add_cli(SRC / "autre" / "mire.mp4")
    check("assets add d'un AUTRE contenu sous le même nom : refusé (code 1), asset gelé intact",
          cp3.returncode == 1 and "autre contenu" in cp3.stderr and assets.file_sha256(dest) == entry.get("sha256"),
          tail(cp3, 2))
    cp4 = mograph("assets", "add", rel(SRC / "mire.mp4"), "--tool", "T", "--model", "M", "--prompt", "P")
    check("assets add sans --license : refusé par la CLI (code 2)", cp4.returncode == 2 and "--license" in cp4.stderr,
          tail(cp4, 1))
    cp5 = mograph("assets", "verify")
    cp6 = mograph("assets", "list")
    check("assets verify : code 0, 1/1 conforme ; assets list : provenance affichée",
          cp5.returncode == 0 and "1/1" in cp5.stdout and "Test / lavfi" in cp6.stdout and "test interne" in cp6.stdout,
          tail(cp5, 2) + " || " + tail(cp6, 2))
    return dest


def test_ai_clip_compile(dest: Path) -> Path:
    print("== B. Calque ai_clip compilé vers le chemin « video » ==")
    p = scene("s30_ok", clip_layer(rel(dest), fit="cover"), provenance={"ai_disclosure": DECL})
    cp = mograph("validate", str(p))
    compiled = json.loads((TEST / "build" / "s30_ok" / "compiled.json").read_text(encoding="utf-8"))
    shot = compiled["shots"][0]
    lay = shot["spec"]["layers"][0]
    job = (shot.get("media") or [{}])[0]
    a = compiled["assets"][0] if compiled["assets"] else {}
    sha = assets.file_sha256(dest)
    check("validate : code 0 ; calque rendu comme une séquence (même runtime que « video »)",
          cp.returncode == 0 and lay["type"] == "sequence" and lay["plate"] == "video__clip", tail(cp, 2))
    check("job d'extraction : source = clip gelé, 25 images 1920x1080, empreinte du verrou portée (ai_sha256)",
          job.get("kind") == "video" and Path(job.get("src", "")).resolve() == dest.resolve() and job.get("frames") == 25
          and job.get("width") == 1920 and job.get("ai_sha256") == sha, json.dumps(job)[:240])
    check("relevé des assets : ai true, nature video, SHA-256 du verrou",
          a.get("path") == rel(dest) and a.get("ai") is True and a.get("kinds") == ["video"] and a.get("sha256") == sha,
          str(compiled["assets"]))
    return p


def test_full_render(p: Path) -> None:
    print("== C. Rendu complet (all) avec un ai_clip gelé ==")
    t0 = time.perf_counter()
    cp = mograph("all", str(p), "--workers", "2")
    det = json.loads((TEST / "build" / "s30_ok" / "determinism.json").read_text(encoding="utf-8")) \
        if (TEST / "build" / "s30_ok" / "determinism.json").is_file() else {}
    check("all : code 0 ; porte de déterminisme 100 % avec le clip gelé",
          cp.returncode == 0 and det.get("ok") is True and det.get("matches") == det.get("total") and det.get("total", 0) >= 1,
          f"{tail(cp, 3)} ; {det.get('matches')}/{det.get('total')} ; {time.perf_counter() - t0:.0f} s")
    rep = json.loads((TEST / "out" / "s30_ok" / "qc_report.json").read_text(encoding="utf-8")) \
        if (TEST / "out" / "s30_ok" / "qc_report.json").is_file() else {"checks": []}
    by = {c["id"]: c for c in rep["checks"]}
    md = (TEST / "out" / "s30_ok" / "qc_report.md").read_text(encoding="utf-8") \
        if (TEST / "out" / "s30_ok" / "qc_report.md").is_file() else ""
    check("QC : CONFORME, scene.ai_lock PASS (bloquant), scene.ai_disclosure PASS",
          rep.get("verdict") == "CONFORME" and by.get("scene.ai_lock", {}).get("status") == "PASS"
          and by.get("scene.ai_lock", {}).get("blocking") is True and by.get("scene.ai_disclosure", {}).get("status") == "PASS",
          f"{rep.get('verdict')} ; {by.get('scene.ai_lock', {}).get('detail')}")
    check("qc_report.md : provenance du verrou (outil, modèle, licence, graine, coût, prompt, empreinte)",
          all(s in md for s in ("Test / lavfi", "licence : test interne", "graine 7", "coût 0 crédit",
                                "prompt : « mire testsrc2 animée »", "SHA-256 `")), md[md.find("## Provenance"):][:400])
    m = TEST / "build" / "s30_ok" / "master"
    f0, f12 = (cv2.imread(str(m / f"{k:06d}.png"), cv2.IMREAD_UNCHANGED) for k in (0, 12))
    check("le clip est bien à l'image et il bouge (images maître 0 et 12 différentes)",
          f0 is not None and f12 is not None and float(np.abs(f0.astype(int) - f12.astype(int)).mean()) > 1.0)


def test_refusals(dest: Path, p_ok: Path) -> None:
    print("== D. Refus ==")
    original = dest.read_bytes()
    try:
        # Empreinte modifiée : compilation, verify, QC, rendu (fichier changé entre compilation et rendu).
        compiled = scene_mod.compile_scene(p_ok)
        dest.write_bytes(original + b"\x00")
        cp = mograph("validate", str(p_ok))
        check("empreinte modifiée : validate REFUSE (« modifié depuis son gel », SHA-256 attendu/trouvé)",
              cp.returncode == 1 and "modifié depuis son gel" in cp.stderr, tail(cp, 2))
        cp = mograph("assets", "verify")
        check("empreinte modifiée : assets verify échoue (code 1, statut « modifié »)",
              cp.returncode == 1 and "modifié" in cp.stdout, tail(cp, 2))
        try:
            media.ensure_shot_media(compiled, 0, log=lambda m: None)
            check("fichier modifié APRÈS la compilation : le rendu du plan est refusé", False, "accepté")
        except assets.AssetError as exc:
            check("fichier modifié APRÈS la compilation : le rendu du plan est refusé", "Rendu du plan" in str(exc),
                  str(exc)[:140])
        from pipeline import encode
        try:
            encode.encode_scene({**compiled, "outputs": []}, log=lambda m: None)
            check("fichier modifié : l'encodage est refusé", False, "accepté")
        except assets.AssetError as exc:
            check("fichier modifié : l'encodage est refusé", "Encodage refusé" in str(exc), str(exc)[:120])
        except Exception as exc:  # noqa: BLE001 — maître absent etc. : le contrôle doit venir AVANT
            check("fichier modifié : l'encodage est refusé", False, f"{type(exc).__name__} : {exc}"[:160])
        c = qc.check_ai_lock(compiled)
        check("fichier modifié : QC scene.ai_lock FAIL bloquant", c["status"] == "FAIL" and c["blocking"] is True,
              c["detail"][:120])
    finally:
        dest.write_bytes(original)
    check("fichier restauré : QC scene.ai_lock PASS", qc.check_ai_lock(scene_mod.compile_scene(p_ok))["status"] == "PASS")

    # Asset non gelé (présent sous le dossier IA mais absent du verrou), via « video » et via « ai_clip ».
    libre = AI / "video" / "libre.mp4"
    shutil.copyfile(SRC / "autre" / "mire.mp4", libre)
    try:
        pv = scene("s30_libre_video", {"id": "v", "type": "video", "src": rel(libre), "w": 640, "h": 360})
        cp = mograph("validate", str(pv))
        check("calque video sur un fichier IA non gelé : REFUS (« absent du verrou »)",
              cp.returncode == 1 and "absent du verrou" in cp.stderr, tail(cp, 2))
        pc = scene("s30_libre_clip", clip_layer(rel(libre)))
        cp = mograph("validate", str(pc))
        check("ai_clip sur un fichier non gelé : REFUS (« n'est pas gelé »)", cp.returncode == 1 and "pas gelé" in cp.stderr,
              tail(cp, 2))
        cp = mograph("assets", "verify")
        check("assets verify signale le fichier non gelé (code 1)", cp.returncode == 1 and "non verrouillé" in cp.stdout,
              tail(cp, 2))
    finally:
        libre.unlink(missing_ok=True)

    # Asset gelé mais absent du disque.
    moved = SRC / "mire_deplace.mp4"
    shutil.move(str(dest), str(moved))
    try:
        cp = mograph("validate", str(p_ok))
        check("asset gelé absent du disque : REFUS (« introuvable sur le disque »)",
              cp.returncode == 1 and "introuvable sur le disque" in cp.stderr, tail(cp, 2))
    finally:
        shutil.move(str(moved), str(dest))

    # Hors du dossier IA, et mauvaise nature.
    cp = mograph("validate", str(scene("s30_hors", clip_layer(rel(SRC / "mire.mp4")))))
    check("ai_clip hors du dossier IA : REFUS (« doit se trouver sous »)",
          cp.returncode == 1 and "doit se trouver sous" in cp.stderr, tail(cp, 2))
    cv2.imwrite(str(SRC / "fond.png"), np.full((90, 160, 3), 128, np.uint8))
    cpa = mograph("assets", "add", rel(SRC / "fond.png"), "--tool", "Test", "--model", "m", "--prompt", "fond gris",
                  "--license", "test")
    cp = mograph("validate", str(scene("s30_image", clip_layer(rel(AI / "image" / "fond.png")))))
    check("ai_clip sur un asset gelé de nature image : REFUS (« attend une vidéo »)",
          cpa.returncode == 0 and cp.returncode == 1 and "attend une" in cp.stderr, tail(cp, 2))


def test_alpha() -> None:
    print("== E. Alpha d'un clip ProRes 4444 ==")
    ffmpeg("-f", "lavfi", "-i", "color=c=0xFF000080:s=320x180:r=25:d=1,format=rgba", "-c:v", "prores_ks",
           "-profile:v", "4", "-pix_fmt", "yuva444p10le", str(SRC / "voile.mov"))
    cp = add_cli(SRC / "voile.mov")
    lock = json.loads(LOCK.read_text(encoding="utf-8"))
    entry = lock["assets"].get(rel(AI / "video" / "voile.mov"), {})
    p = scene("s30_alpha", clip_layer(rel(AI / "video" / "voile.mov"), w=640, h=360, fit="contain"),
              provenance={"ai_disclosure": DECL})
    compiled = scene_mod.compile_scene(p)
    out = media.ensure_shot_media(compiled, 0, log=lambda m: None)[0]
    img = cv2.imread(str(out / "000000.png"), cv2.IMREAD_UNCHANGED)
    a = int(img[180, 320, 3]) if img is not None and img.ndim == 3 and img.shape[2] == 4 else -1
    check("clip ProRes 4444 semi-transparent : gelé (alpha relevé), alpha conservé à l'extraction (≈ 128)",
          cp.returncode == 0 and entry.get("media", {}).get("alpha") is True and abs(a - 128) <= 1,
          f"alpha={a} media={entry.get('media')}")


def test_lock_integrity(dest: Path, p_ok: Path) -> None:
    print("== F. Verrou invalide, options de add, contournements, concurrence ==")
    saved = LOCK.read_bytes()
    key = rel(dest)
    try:
        lock = json.loads(saved)
        lock["assets"][key]["provenance"] = {}
        LOCK.write_text(json.dumps(lock), encoding="utf-8")
        cp = mograph("validate", str(p_ok))
        compiled = json.loads((TEST / "build" / "s30_ok" / "compiled.json").read_text(encoding="utf-8"))
        try:
            assets.check_compiled(compiled, stage="Rendu")
            refused = False
        except assets.AssetError:
            refused = True
        c = qc.check_ai_lock(compiled)
        cpv = mograph("assets", "verify")
        check("entrée sans provenance : validate, rendu, QC et verify refusent (message français « champ obligatoire »)",
              cp.returncode == 1 and "champ obligatoire" in cp.stderr and refused and c["status"] == "FAIL"
              and cpv.returncode == 1, tail(cp, 1))
        LOCK.write_text(saved.decode("utf-8")[:40], encoding="utf-8")
        cp = mograph("validate", str(p_ok))
        check("verrou JSON tronqué : validate refuse (« JSON invalide ligne … »)",
              cp.returncode == 1 and "JSON invalide" in cp.stderr, tail(cp, 1))
        LOCK.write_bytes(b"\xef\xbb\xbf" + saved)
        cp = mograph("validate", str(p_ok))
        check("verrou réenregistré avec BOM (Bloc-notes) : accepté", cp.returncode == 0, tail(cp, 1))
    finally:
        LOCK.write_bytes(saved)

    # Re-geler le même fichier : la provenance est complétée, jamais effacée.
    cp = mograph("assets", "add", key, "--tool", "Test", "--model", "lavfi", "--prompt", "mire testsrc2 animée",
                 "--license", "test interne", "--notes", "revu")
    pv = json.loads(LOCK.read_text(encoding="utf-8"))["assets"][key]["provenance"]
    check("re-gel avec --notes : « provenance mise à jour », graine, coût et date conservés",
          cp.returncode == 0 and "provenance mise à jour" in cp.stdout and pv.get("notes") == "revu"
          and pv.get("seed") == 7 and pv.get("cost") == "0 crédit", f"{tail(cp, 1)} {pv}")

    # --prompt-file (avec BOM) et --dest.
    pf = SRC / "prompt.txt"
    pf.write_bytes(b"\xef\xbb\xbf" + "Forêt brumeuse, travelling lent".encode("utf-8"))
    ffmpeg("-f", "lavfi", "-i", "testsrc2=size=320x180:rate=25:duration=1", "-c:v", "libx264", "-pix_fmt", "yuv420p",
           str(SRC / "petit.mp4"))
    cp = mograph("assets", "add", rel(SRC / "petit.mp4"), "--tool", "T", "--model", "M", "--prompt-file", str(pf),
                 "--license", "L", "--dest", "clips/foret.mp4")
    lock = json.loads(LOCK.read_text(encoding="utf-8"))
    e = lock["assets"].get(rel(AI / "clips" / "foret.mp4"), {})
    check("--prompt-file (BOM retiré) et --dest (rangé sous clips/foret.mp4)",
          cp.returncode == 0 and e.get("provenance", {}).get("prompt") == "Forêt brumeuse, travelling lent",
          f"{tail(cp, 1)} {e.get('provenance', {}).get('prompt')!r}")
    cp = mograph("assets", "add", rel(SRC / "petit.mp4"), "--tool", "T", "--model", "M", "--prompt", "P",
                 "--license", "L", "--dest", "clips/")
    check("--dest désignant un dossier : refusé proprement (code 1, message français, pas de trace)",
          cp.returncode == 1 and "chemin de FICHIER" in cp.stderr and "Traceback" not in cp.stderr, tail(cp, 1))
    cp = mograph("assets", "add", rel(SRC / "petit.mp4"), "--tool", "T", "--model", "M", "--prompt", "P",
                 "--license", "L", "--date", "20260915", "--dest", "clips/autre.mp4")
    check("--date au format non AAAA-MM-JJ : refusé AVANT toute copie",
          cp.returncode == 1 and "--date" in cp.stderr and not (AI / "clips" / "autre.mp4").exists(), tail(cp, 1))

    # --replace : remplacé, ancienne empreinte gardée, le job média du plan change (re-rendu).
    before = scene_mod.compile_scene(p_ok)["shots"][0]["media"][0]["digest"]
    old_sha = assets.file_sha256(dest)
    original = dest.read_bytes()
    try:
        cp = add_cli(SRC / "autre" / "mire.mp4", "--replace", "--dest", "video/mire.mp4")
        e = json.loads(LOCK.read_text(encoding="utf-8"))["assets"][key]
        after = scene_mod.compile_scene(p_ok)["shots"][0]["media"][0]["digest"]
        check("assets add --replace : « remplacé », champ replaces = ancienne empreinte, extraction à refaire",
              cp.returncode == 0 and "remplacé" in cp.stdout and e.get("replaces") == old_sha and after != before,
              tail(cp, 1))
    finally:
        dest.write_bytes(original)
        assets.add(dest, tool="Test", model="lavfi", prompt="mire testsrc2 animée", license="test interne",
                   seed=7, cost="0 crédit", replace=True)

    # Image IA remplacée : l'empreinte du plan change (sinon le plan ne serait pas re-rendu).
    img_src = SRC / "fond.png"
    pi = scene("s30_img", {"id": "fond", "type": "image", "src": rel(AI / "image" / "fond.png"), "w": 160, "h": 90},
               provenance={"ai_disclosure": {**DECL, "roles": ["image"]}})
    from pipeline import web_render
    s1 = web_render.spec_sha256(scene_mod.compile_scene(pi)["shots"][0]["spec"])
    cv2.imwrite(str(SRC / "fond2.png"), np.full((90, 160, 3), 30, np.uint8))
    assets.add(SRC / "fond2.png", tool="Test", model="m", prompt="fond sombre", license="test", dest="image/fond.png",
               replace=True)
    s2 = web_render.spec_sha256(scene_mod.compile_scene(pi)["shots"][0]["spec"])
    check("image IA remplacée (--replace) : empreinte du plan différente, le plan sera re-rendu", s1 != s2,
          f"{s1[:12]} -> {s2[:12]}")
    assets.add(img_src, tool="Test", model="m", prompt="fond gris", license="test", dest="image/fond.png", replace=True)

    # Contournements : chemin étendu Windows, jonction sous le dossier IA, piste synth sous le dossier IA.
    ffmpeg("-f", "lavfi", "-i", "sine=frequency=440:duration=1", "-ar", "48000", str(AI / "libre.wav"))
    try:
        ext = "\\\\?\\" + str((AI / "libre.wav").resolve())
        pa = scene("s30_audio_etendu", {"id": "t", "type": "text", "text": "A"})
        d = json.loads(pa.read_text(encoding="utf-8"))
        d["audio"] = {"src": ext}
        pa.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
        cp = mograph("validate", str(pa))
        check("audio.src en chemin étendu \\\\?\\ vers un fichier IA non gelé : REFUS", cp.returncode == 1 and
              ("refusé" in cp.stderr or "absent du verrou" in cp.stderr), tail(cp, 1))
        d["audio"] = {"src": rel(AI / "beat.wav"), "bpm": 120, "synth": {"kind": "beat"}}
        pa.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
        cp = mograph("validate", str(pa))
        check("audio.synth sous le dossier IA : REFUS (une piste générée n'est pas un asset gelé)",
              cp.returncode == 1 and "piste de test générée" in cp.stderr and not (AI / "beat.wav").exists(), tail(cp, 1))
    finally:
        (AI / "libre.wav").unlink(missing_ok=True)
    # Cible SOUS assets/ (servie au navigateur) mais HORS du dossier IA : le vrai scénario de contournement.
    outside = SRC / "hors_ia"
    outside.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(outside / "y.png"), np.full((8, 8, 3), 200, np.uint8))
    junction = AI / "jx"
    r = subprocess.run(["cmd", "/c", "mklink", "/J", str(junction), str(outside)], capture_output=True) \
        if config.IS_WINDOWS else None
    if not config.IS_WINDOWS:
        os.symlink(outside, junction)
    try:
        if junction.exists():
            pj = scene("s30_jonction", {"id": "y", "type": "image", "src": rel(AI) + "/jx/y.png", "w": 8, "h": 8})
            cp = mograph("validate", str(pj))
            cpv = mograph("assets", "verify")
            check("jonction/lien sous le dossier IA qui en sort : REFUS à la compilation, signalé par verify",
                  cp.returncode == 1 and "lien ou une jonction" in cp.stderr and "lien hors du dossier IA" in cpv.stdout,
                  tail(cp, 1))
        else:
            check("jonction/lien sous le dossier IA : création impossible sur cette machine", False, str(r))
    finally:
        if junction.exists() or junction.is_symlink():
            if config.IS_WINDOWS:
                os.rmdir(junction)  # supprime la jonction elle-même, jamais son contenu
            else:
                junction.unlink()

    # Deux « assets add » simultanés : aucune entrée perdue.
    for n in (1, 2):
        cv2.imwrite(str(SRC / f"c{n}.png"), np.full((8, 8, 3), 10 * n, np.uint8))
    procs = [subprocess.Popen([PY, str(ROOT / "mograph.py"), "assets", "add", rel(SRC / f"c{n}.png"), "--tool", "T",
                               "--model", "M", "--prompt", "P", "--license", "L"], cwd=ROOT, env=dict(os.environ),
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE) for n in (1, 2)]
    codes = [p.wait(timeout=120) for p in procs]
    keys = json.loads(LOCK.read_text(encoding="utf-8"))["assets"]
    check("deux « assets add » simultanés : codes 0 et les deux entrées présentes dans le verrou",
          codes == [0, 0] and rel(AI / "image" / "c1.png") in keys and rel(AI / "image" / "c2.png") in keys, str(codes))

    # Cohérence CLI / schéma, QC sans relevé, rapport d'un asset absent du verrou, aperçu.
    schema = json.loads((config.SCHEMA_DIR / "assets_lock.schema.json").read_text(encoding="utf-8"))
    ent = schema["$defs"]["entry"]["properties"]
    check("natures et sources de la CLI = énumérations du schéma",
          set(assets.KINDS) == set(ent["kind"]["enum"]) and set(assets.SOURCES) == set(ent["provenance"]["properties"]["source"]["enum"]))
    skip = qc.check_ai_lock({"scene_id": "x"})
    lines = qc._provenance_lines({"provenance": None, "ai_assets": ["assets/ai/x.mp4"],
                                  "ai_assets_lock": {"assets/ai/x.mp4": None}})
    rep = json.loads((TEST / "out" / "s30_ok" / "qc_report.json").read_text(encoding="utf-8"))
    check("QC : scene.ai_lock SKIP sans relevé ; rapport : « absent du verrou » ; clé ai_assets_lock dans le JSON",
          skip["status"] == "SKIP" and skip["blocking"] is False and any("absent du verrou" in l for l in lines)
          and key in (rep.get("ai_assets_lock") or {}))
    from pipeline import preview
    st = preview._State(p_ok, log=lambda m: None)
    st.compiled = scene_mod.compile_scene(p_ok)
    stamp = [Path(f) for f, _, _ in st._sources_stamp()]
    check("aperçu : le verrou et les assets IA de la scène sont surveillés (recompilation = revérification)",
          config.ASSETS_LOCK in stamp and (ROOT / key) in stamp)


def main() -> int:
    assert TEST.parent.name == "_tests" and AI.parent.name == "_tests"
    for d in (TEST, AI, SRC):
        if d.exists():
            shutil.rmtree(d)
    SRC.mkdir(parents=True)
    t0 = time.perf_counter()
    if not check("environnement isolé : verrou et dossier IA de test", config.ASSETS_LOCK == LOCK.resolve()
                 and config.AI_ASSETS_DIR == AI.resolve() and config.BUILD_DIR == (TEST / "build").resolve()):
        # Garde bloquante : sans isolation, les contrôles écriraient dans le vrai verrou et dans assets/ai/.
        print("ÉCHEC : isolation impossible, arrêt avant toute écriture.")
        return 1
    try:
        dest = test_add_verify_list()
        p_ok = test_ai_clip_compile(dest)
        test_full_render(p_ok)
        test_refusals(dest, p_ok)
        test_alpha()
        test_lock_integrity(dest, p_ok)
    except Exception as exc:  # noqa: BLE001 — l'exception est rapportée comme un échec, pas masquée
        import traceback
        traceback.print_exc()
        check("les contrôles s'exécutent sans exception", False, f"{type(exc).__name__} : {exc}"[:200])
    print(f"\nDurée : {time.perf_counter() - t0:.0f} s")
    if FAILS:
        print(f"ÉCHEC : {len(FAILS)} contrôle(s) : {FAILS}")
        return 1
    print("SUCCÈS : assets IA gelés conformes (phase 3).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
