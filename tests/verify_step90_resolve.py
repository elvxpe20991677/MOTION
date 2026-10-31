"""Vérification de la phase 9 : aller-retour d'un maître ProRes par DaVinci Resolve (API de scripting officielle).

Lancement depuis la racine :  .venv/Scripts/python.exe tests/verify_step90_resolve.py
Partie 0 (toujours, ffmpeg seul) : le critère de plage de scripts/resolve_roundtrip.py détecte une expansion
vidéo -> pleine et une compression pleine -> vidéo, y compris sur une image grise peu contrastée (point fixe
vers Y ≈ 445), et accepte un retour identique ; une source non 422 HQ est refusée.
Parties A et B : SKIP si DaVinci Resolve Studio n'est pas lancé avec « External scripting using » = Local :
le test ne lance jamais Resolve et ne modifie aucun réglage (la baseline et la CI ne l'ouvrent donc pas).
Isolé : build/_tests/step90/ ; projets Resolve temporaires créés puis supprimés par le script.
Contrôles (scène 2D de 2 s rendue en ProRes 422 HQ par mograph) :
  A. aller-retour sans étalonnage : flux vidéo identique au bit près (Resolve recopie le ProRes),
     étiquettes couleur BT.709 tv, nombre d'images, timecode 01:00:00:00, niveaux identiques ;
  B. aller-retour avec étalonnage CDL neutre (rendu réellement traité) : étiquettes, images et timecode
     conservés, aucun décalage de niveau (luminance moyenne à ±2 codes), extrêmes à ±12 codes.
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TEST = ROOT / "build" / "_tests" / "step90"
os.environ.update({"MOGRAPH_BUILD_DIR": str(TEST / "build"), "MOGRAPH_OUT_DIR": str(TEST / "out"),
                   "MOGRAPH_SCENES_DIR": str(TEST / "scenes"), "PYTHONIOENCODING": "utf-8"})
sys.path.insert(0, str(ROOT))

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

PY = sys.executable
FAILS: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> bool:
    print(("PASS " if ok else "FAIL ") + label + (f" -- {detail}" if detail else ""), flush=True)
    if not ok:
        FAILS.append(label)
    return ok


def roundtrip_module():
    spec = importlib.util.spec_from_file_location("resolve_roundtrip", ROOT / "scripts" / "resolve_roundtrip.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_offline() -> None:
    """Critère de plage et refus des sources non 422 HQ, sans Resolve."""
    import subprocess as sp

    from pipeline import config

    mod = roundtrip_module()
    d = TEST / "offline"
    d.mkdir(parents=True, exist_ok=True)
    cases = {"contraste": "testsrc2=s=320x180:d=1:r=25",
             "gris": "color=c=0x6c6c6c:s=320x180:d=1:r=25,noise=alls=6:allf=t"}
    for name, src in cases.items():
        s = d / f"{name}.mov"
        sp.run([config.FFMPEG, "-v", "error", "-y", "-f", "lavfi", "-i", src, "-c:v", "prores_ks", "-profile:v", "3",
                "-pix_fmt", "yuv422p10le", "-color_range", "tv", "-colorspace", "bt709", "-color_primaries", "bt709",
                "-color_trc", "bt709", str(s)], check=True)
        for label, flt in mod.RANGE_HYPOTHESES.items():
            o = d / f"{name}_{label}.mov"
            sp.run([config.FFMPEG, "-v", "error", "-y", "-i", str(s), "-vf", flt, "-c:v", "prores_ks", "-profile:v",
                    "3", "-pix_fmt", "yuv422p10le", "-color_range", "tv", "-colorspace", "bt709", "-color_primaries",
                    "bt709", "-color_trc", "bt709", str(o)], check=True)
            c = mod.compare(s, o)
            check(f"0. {name} : {label} détectée (plage NON conservée, étiquette tv pourtant intacte)",
                  c["range_kept"] is False and c["color_tags_kept"], str(c["psnr_range_hypotheses_db"]))
        c = mod.compare(s, s)
        check(f"0. {name} : retour identique accepté (plage conservée)", c["range_kept"] is True)
    q = d / "q4444.mov"
    sp.run([config.FFMPEG, "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=s=320x180:d=0.4:r=25", "-c:v",
            "prores_ks", "-profile:v", "4", "-pix_fmt", "yuva444p10le", str(q)], check=True)
    r = sp.run([PY, str(ROOT / "scripts" / "resolve_roundtrip.py"), str(q)], cwd=ROOT, capture_output=True, text=True,
               encoding="utf-8", errors="replace")
    check("0. source ProRes 4444 refusée (seul un maître 422 HQ est vérifié)", r.returncode != 0
          and "422 HQ" in (r.stderr + r.stdout))


def main() -> int:
    assert TEST.parent.name == "_tests"
    if TEST.exists():
        shutil.rmtree(TEST)
    TEST.mkdir(parents=True)
    test_offline()
    try:
        roundtrip_module().connect()
    except SystemExit as exc:
        print(f"SKIP aller-retour DaVinci Resolve -- {str(exc)[:160]}", flush=True)
        if FAILS:
            print(f"ÉCHEC : {len(FAILS)} contrôle(s) : {FAILS}")
            return 1
        return 0
    (TEST / "scenes").mkdir(parents=True)
    scene = {"version": "1.0", "id": "rr90", "title": "rr90", "brief": "test phase 9", "seed": 3, "preset": "flat_riso",
             "format": {"aspect": "16:9", "resolution": "1080p", "fps": 25, "alpha": False},
             "outputs": [{"profile": "prores_422hq"}],
             "qc": {"safe_zone": "title", "safe_zone_tolerance_s": 0.5, "allow_black_s": 0, "determinism_frames": 2,
                    "loudness_target_lufs": -14},
             "shots": [{"id": "a", "duration": 2.0, "background": "bg",
                        "layers": [{"id": "t", "type": "text", "text": "Aller-retour", "x": "safe:50%", "y": "safe:50%",
                                    "anim": [{"move": "chars_up", "at": 0.2}]},
                                   {"id": "r", "type": "shape", "shape": "ellipse", "w": 300, "h": 300, "fill": "secondary",
                                    "x": "80%", "y": "50%", "anim": [{"move": "pop", "at": 0.4}]}]}]}
    (TEST / "scenes" / "rr90.json").write_text(json.dumps(scene, ensure_ascii=False), encoding="utf-8")
    cp = subprocess.run([PY, str(ROOT / "mograph.py"), "all", "rr90"], cwd=ROOT, env=dict(os.environ),
                        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=1200)
    master = TEST / "out" / "rr90" / "rr90_prores_422hq.mov"
    if not check("maître ProRes 422 HQ rendu par mograph (QC CONFORME)", cp.returncode == 0 and master.is_file(),
                 (cp.stdout + cp.stderr).strip().splitlines()[-1:] and (cp.stdout + cp.stderr).strip().splitlines()[-1]):
        return 1
    for label, extra in (("A. sans étalonnage", []), ("B. étalonnage CDL neutre", ["--force-reencode"])):
        try:
            r = subprocess.run([PY, str(ROOT / "scripts" / "resolve_roundtrip.py"), str(master), *extra], cwd=ROOT,
                               env=dict(os.environ), capture_output=True, text=True, encoding="utf-8",
                               errors="replace", timeout=1200)
        except subprocess.TimeoutExpired:
            check(f"{label} : aller-retour terminé en moins de 20 min", False)
            continue
        rep_line = next((x for x in r.stdout.splitlines() if x.startswith("Rapport : ")), "")
        rep = json.loads(Path(rep_line[len("Rapport : "):]).read_text(encoding="utf-8")) if rep_line else {}
        base = (r.returncode == 0 and rep.get("render_status", {}).get("JobStatus") == "Complete"
                and rep.get("color_tags_kept") and rep.get("frames_kept") and rep.get("timecode_kept")
                and rep.get("format_kept")
                and rep.get("range_kept") and rep.get("probe_returned", {}).get("color_range") == "tv"
                and rep.get("probe_returned", {}).get("timecode") == "01:00:00:00")
        check(f"{label} : étiquettes BT.709 tv, images, timecode et plage conservés", bool(base),
              json.dumps({k: rep.get(k) for k in ("color_tags_kept", "frames_kept", "timecode_kept", "range_kept",
                                                   "psnr_db")}) + " " + (r.stderr.strip()[-160:] if r.returncode else ""))
        if not extra:
            check(f"{label} : flux vidéo identique au bit près (aucun réencodage)",
                  rep.get("bitstream_identical") is True and rep.get("extremes_identical") is True)
        else:
            check(f"{label} : rendu réellement traité (flux différent), qualité élevée (PSNR ≥ 50 dB), "
                  "luminance moyenne à ±2 codes", rep.get("bitstream_identical") is False
                  and (rep.get("psnr_db") or 0) >= 50
                  and abs(rep["levels_source"]["YAVG"] - rep["levels_returned"]["YAVG"]) <= 2,
                  f"PSNR {rep.get('psnr_db')} ; YAVG {rep.get('levels_source', {}).get('YAVG')} -> "
                  f"{rep.get('levels_returned', {}).get('YAVG')}")
        check(f"{label} : projet Resolve temporaire réellement supprimé (fermé, supprimé, absent de la liste)",
              rep.get("deleted") is True)
    if FAILS:
        print(f"ÉCHEC : {len(FAILS)} contrôle(s) : {FAILS}")
        return 1
    print("SUCCÈS : aller-retour DaVinci Resolve conforme (phase 9).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
