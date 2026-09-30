#!/usr/bin/env python3
"""Baseline du pipeline : tests de vérification + porte de déterminisme + versions, puis comparaison.

Usage (depuis la racine, avec le Python de l'orchestrateur) :
    .venv/Scripts/python.exe scripts/baseline.py run --label phase-0-a
    .venv/Scripts/python.exe scripts/baseline.py run --label phase-1 --save docs/baseline/phase-1.json
    .venv/Scripts/python.exe scripts/baseline.py compare docs/baseline/phase-0.json build/_baseline/phase-1/baseline.json

`run` exécute, dans cet ordre :
  1. les scripts tests/verify_step*.py puis tests/verify_features.py (un par un, journal par script) ;
  2. sur la scène de référence (défaut demo_flat_riso), dans un build/ et un out/ isolés :
     render -> determinism -> compose -> encode -> qc, et relève les empreintes de TOUTES les images
     des plans, de toutes les images maître et des images décodées de chaque livrable ;
  3. les versions des outils.
Le résultat est écrit dans build/_baseline/<label>/baseline.json (et copié vers --save si demandé).
`compare` sort avec un code ≠ 0 si un test régresse ou si une empreinte d'image change.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pipeline import config  # noqa: E402  (après l'ajout de la racine au chemin d'import)

# Ordre fixe : le même à chaque baseline, pour que les journaux soient comparables ligne à ligne.
TEST_SCRIPTS = [
    "tests/verify_step09_presets.py",
    "tests/verify_step11_compiler.py",
    "tests/verify_step12_web.py",
    "tests/verify_step13_blender.py",
    "tests/verify_step14_compose.py",
    "tests/verify_step15_encode.py",
    "tests/verify_step16_audio.py",
    "tests/verify_step17_qc.py",
    "tests/verify_features.py",
]
REFERENCE_SCENE = "demo_flat_riso"
# Préfixes de ligne reconnus dans la sortie des tests (CONTRACT §1.8).
STATUS_RE = re.compile(r"^\s*(PASS|FAIL|WARN|SKIP|INFO)\b")


def _write_json_atomic(path: Path, obj) -> None:
    # Écriture atomique (CONTRACT §1.5) : jamais de baseline à moitié écrite.
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    os.replace(tmp, path)


def _env(extra: dict | None = None) -> dict:
    # UTF-8 forcé : les tests impriment des accents, la console Windows est en cp1252 par défaut.
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1")
    env.update(extra or {})
    return env


def _run(argv: list[str], log_path: Path, *, env: dict | None = None, timeout: int = 7200) -> tuple[int, str, float]:
    """Lance une commande, écrit sa sortie complète dans log_path, renvoie (code, sortie, durée)."""
    t0 = time.perf_counter()
    try:
        r = subprocess.run(argv, cwd=ROOT, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", env=env or _env(), timeout=timeout)
        code, out = r.returncode, (r.stdout or "") + (r.stderr or "")
    except subprocess.TimeoutExpired as exc:
        code, out = 124, f"DÉLAI DÉPASSÉ ({timeout} s) : {exc}"
    dt = time.perf_counter() - t0
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text(out, encoding="utf-8", newline="\n")
    return code, out, dt


def run_tests(workdir: Path, log) -> list[dict]:
    results = []
    for rel in TEST_SCRIPTS:
        name = Path(rel).stem
        log(f"test {name} ...")
        code, out, dt = _run([sys.executable, rel], workdir / "logs" / f"{name}.log")
        counts = {k: 0 for k in ("PASS", "FAIL", "WARN", "SKIP", "INFO")}
        fails = []
        for line in out.splitlines():
            m = STATUS_RE.match(line)
            if m:
                counts[m.group(1)] += 1
                if m.group(1) == "FAIL":
                    fails.append(line.strip()[:240])
        results.append({"script": rel, "exit_code": code, "seconds": round(dt, 1), "counts": counts, "fail_lines": fails})
        log(f"  -> code {code}, {counts['PASS']} PASS / {counts['FAIL']} FAIL / {counts['WARN']} WARN en {dt:.0f} s")
    return results


def _pixel_sha256(path: Path) -> str:
    # Même définition que le pilote web (pixels décodés), pour comparer avec les manifestes.
    from pipeline.web_render import pixel_sha256
    import cv2

    img = cv2.imread(str(path), cv2.IMREAD_UNCHANGED)
    if img is None:
        raise RuntimeError(f"image illisible : {path}")
    return pixel_sha256(img)


def _decoded_md5(path: Path) -> str:
    # MD5 des images DÉCODÉES (flux vidéo seul) : insensible aux métadonnées du conteneur.
    r = subprocess.run([config.FFMPEG, "-v", "error", "-i", str(path), "-map", "0:v:0", "-f", "md5", "-"],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    return r.stdout.strip().removeprefix("MD5=") or f"(échec : {r.stderr.strip()[:200]})"


def _file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def run_scene(workdir: Path, scene: str, count: int | None, log) -> dict:
    build, out = workdir / "build", workdir / "out"
    # Dossiers vidés : la baseline mesure un rendu COMPLET, jamais une reprise partielle.
    for d in (build, out):
        shutil.rmtree(d, ignore_errors=True)
    env = _env({"MOGRAPH_BUILD_DIR": str(build), "MOGRAPH_OUT_DIR": str(out)})
    steps = {}
    cli = [sys.executable, "mograph.py"]
    plan = [("render", [scene]), ("determinism", [scene] + (["--count", str(count)] if count else [])),
            ("compose", [scene]), ("encode", [scene]), ("qc", [scene])]
    for name, args in plan:
        log(f"scène {scene} : {name} ...")
        code, _, dt = _run(cli + [name] + args, workdir / "logs" / f"scene_{name}.log", env=env)
        steps[name] = {"exit_code": code, "seconds": round(dt, 1)}
        log(f"  -> code {code} en {dt:.0f} s")
    result: dict = {"scene": scene, "steps": steps}
    sdir = build / scene
    compiled_path = sdir / "compiled.json"
    if not compiled_path.exists():
        return result
    compiled = json.loads(compiled_path.read_text(encoding="utf-8"))
    shots = {}
    for shot in compiled["shots"]:
        mpath = sdir / "shots" / shot["id"] / "manifest.json"
        if mpath.exists():
            fd = json.loads(mpath.read_text(encoding="utf-8"))["frames_done"]
            shots[shot["id"]] = [fd[k]["sha256"] for k in sorted(fd, key=int)]
    result["shot_frames"] = shots
    # Empreinte globale : un seul hash résume toutes les images de tous les plans.
    lines = "".join(f"{sid}:{i}:{h}\n" for sid in shots for i, h in enumerate(shots[sid]))
    result["shots_fingerprint"] = hashlib.sha256(lines.encode()).hexdigest()
    det = sdir / "determinism.json"
    if det.exists():
        d = json.loads(det.read_text(encoding="utf-8"))
        result["determinism"] = {"ok": d["ok"], "matches": d["matches"], "total": d["total"], "order": d["order"]}
    master = sorted((sdir / "master").glob("[0-9]*.png"))
    if master:
        mh = [_pixel_sha256(p) for p in master]
        result["master_frames"] = len(mh)
        result["master_fingerprint"] = hashlib.sha256("".join(h + "\n" for h in mh).encode()).hexdigest()
    deliverables = {}
    for p in sorted((out / scene).glob("*")):
        if p.suffix in (".mp4", ".mov"):
            deliverables[p.name] = {"decoded_md5": _decoded_md5(p), "file_sha256": _file_sha256(p),
                                    "bytes": p.stat().st_size}
    result["deliverables"] = deliverables
    qc_json = out / scene / "qc_report.json"
    if qc_json.exists():
        q = json.loads(qc_json.read_text(encoding="utf-8"))
        result["qc_verdict"] = q.get("verdict")
        checks = []

        def walk(o):
            # Les contrôles peuvent être imbriqués par fichier : on collecte tout dict qui a un statut.
            if isinstance(o, dict):
                if "status" in o and "id" in o:
                    checks.append(o)
                for v in o.values():
                    walk(v)
            elif isinstance(o, list):
                for v in o:
                    walk(v)

        walk(q)
        result["qc_counts"] = {s: sum(1 for c in checks if c.get("status") == s) for s in ("PASS", "FAIL", "WARN", "SKIP")}
        result["qc_non_pass"] = [f"{c.get('status')} {c.get('id')} : {str(c.get('actual'))[:120]}"
                                 for c in checks if c.get("status") != "PASS"]
    return result


def collect_versions() -> dict:
    def run(argv: list[str]) -> str:
        try:
            r = subprocess.run(argv, cwd=ROOT, capture_output=True, text=True, encoding="utf-8",
                               errors="replace", timeout=120)
            return (r.stdout + r.stderr).strip()
        except (OSError, subprocess.SubprocessError) as exc:
            return f"(indisponible : {exc})"

    npm = shutil.which("npm") or "npm"
    node = shutil.which("node") or "node"
    ff = run([config.FFMPEG, "-hide_banner", "-version"]).splitlines()
    return {
        "system": f"{platform.platform()} | {platform.processor()} | {os.cpu_count()} threads",
        "python_orchestrator": sys.version.split()[0],
        "ffmpeg": ff[0] if ff else "",
        "ffmpeg_path": config.FFMPEG,
        "pip_freeze": run([sys.executable, "-m", "pip", "freeze"]).splitlines(),
        "npm_ls": run([npm, "ls", "--depth=0"]).splitlines()[1:],
        "node": run([node, "--version"]),
        "uv": run(["uv", "--version"]),
        "bpy": run([str(config.BLENDER_PYTHON), "-c",
                    "import bpy, sys; print('bpy', bpy.app.version_string, '| python', sys.version.split()[0])"]),
        "chromium": run([sys.executable, "-c",
                         "from playwright.sync_api import sync_playwright\n"
                         "with sync_playwright() as p:\n"
                         "    b = p.chromium.launch(); print(b.version); b.close()"]),
        "gpu": run(["nvidia-smi", "--query-gpu=name,driver_version,memory.total", "--format=csv,noheader"]),
        "git_head": run(["git", "log", "-1", "--format=%H %s"]),
    }


def cmd_run(args) -> int:
    workdir = config.BUILD_DIR / "_baseline" / args.label
    workdir.mkdir(parents=True, exist_ok=True)

    def log(msg: str) -> None:
        print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)

    t0 = time.perf_counter()
    data = {"label": args.label, "date_utc": time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())}
    data["versions"] = collect_versions()
    data["tests"] = [] if args.skip_tests else run_tests(workdir, log)
    data["reference_scene"] = None if args.skip_scene else run_scene(workdir, args.scene, args.count, log)
    data["seconds_total"] = round(time.perf_counter() - t0, 1)
    out = workdir / "baseline.json"
    _write_json_atomic(out, data)
    log(f"baseline écrite : {out}")
    if args.save:
        dest = Path(args.save) if Path(args.save).is_absolute() else ROOT / args.save
        _write_json_atomic(dest, data)
        log(f"copie : {dest}")
    bad = [t["script"] for t in data["tests"] if t["exit_code"] != 0]
    ref = data["reference_scene"] or {}
    det_ok = (ref.get("determinism") or {}).get("ok", args.skip_scene)
    log(f"tests en échec : {bad or 'aucun'} | déterminisme : {'OK' if det_ok else 'ÉCHEC'}")
    return 0 if not bad and det_ok else 1


def cmd_compare(args) -> int:
    a = json.loads(Path(args.a).read_text(encoding="utf-8"))
    b = json.loads(Path(args.b).read_text(encoding="utf-8"))
    problems, notes = [], []
    ta = {t["script"]: t for t in a.get("tests", [])}
    for t in b.get("tests", []):
        ref = ta.get(t["script"])
        if ref is None:
            notes.append(f"nouveau test : {t['script']} (code {t['exit_code']})")
            continue
        if t["exit_code"] != 0 and ref["exit_code"] == 0:
            problems.append(f"RÉGRESSION {t['script']} : code {ref['exit_code']} -> {t['exit_code']} ; {t['fail_lines'][:3]}")
        if t["counts"]["FAIL"] > ref["counts"]["FAIL"]:
            problems.append(f"RÉGRESSION {t['script']} : FAIL {ref['counts']['FAIL']} -> {t['counts']['FAIL']}")
        if t["counts"]["PASS"] != ref["counts"]["PASS"]:
            notes.append(f"{t['script']} : PASS {ref['counts']['PASS']} -> {t['counts']['PASS']}")
    ra, rb = a.get("reference_scene") or {}, b.get("reference_scene") or {}
    if ra and rb:
        for key in ("shots_fingerprint", "master_fingerprint"):
            if ra.get(key) != rb.get(key):
                (problems if not args.allow_pixel_change else notes).append(
                    f"{key} différent : {ra.get(key)} -> {rb.get(key)}")
        if not (rb.get("determinism") or {}).get("ok"):
            problems.append("porte de déterminisme en ÉCHEC")
        for name, d in (rb.get("deliverables") or {}).items():
            old = (ra.get("deliverables") or {}).get(name)
            if old and old["decoded_md5"] != d["decoded_md5"]:
                (problems if not args.allow_encode_change else notes).append(
                    f"livrable {name} : images décodées différentes ({old['decoded_md5']} -> {d['decoded_md5']})")
        if ra.get("qc_verdict") != rb.get("qc_verdict"):
            problems.append(f"verdict QC : {ra.get('qc_verdict')} -> {rb.get('qc_verdict')}")
    va, vb = a.get("versions", {}), b.get("versions", {})
    for k in ("ffmpeg", "python_orchestrator", "bpy", "chromium", "node", "uv", "gpu"):
        if va.get(k) != vb.get(k):
            notes.append(f"version {k} : {va.get(k)} -> {vb.get(k)}")
    for k in ("pip_freeze", "npm_ls"):
        if va.get(k) != vb.get(k):
            notes.append(f"{k} différent : -{sorted(set(va.get(k, [])) - set(vb.get(k, [])))} +{sorted(set(vb.get(k, [])) - set(va.get(k, [])))}")
    for n in notes:
        print("NOTE", n)
    for p in problems:
        print("FAIL", p)
    print("PASS baseline identique (aucune régression)" if not problems else f"\n{len(problems)} régression(s)")
    return 0 if not problems else 1


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="exécuter la baseline")
    r.add_argument("--label", required=True, help="nom du dossier build/_baseline/<label>/")
    r.add_argument("--scene", default=REFERENCE_SCENE, help=f"scène de référence (défaut {REFERENCE_SCENE})")
    r.add_argument("--count", type=int, default=None, help="images témoins du déterminisme (défaut : scène)")
    r.add_argument("--save", default=None, help="copier baseline.json vers ce chemin (ex. docs/baseline/phase-0.json)")
    r.add_argument("--skip-tests", action="store_true", help="ne pas lancer les scripts de vérification")
    r.add_argument("--skip-scene", action="store_true", help="ne pas rendre la scène de référence")
    r.set_defaults(func=cmd_run)
    c = sub.add_parser("compare", help="comparer deux baselines (code ≠ 0 si régression)")
    c.add_argument("a", help="baseline de référence")
    c.add_argument("b", help="baseline à contrôler")
    c.add_argument("--allow-pixel-change", action="store_true",
                   help="changement d'empreinte d'image attendu (ex. mise à jour de version validée)")
    c.add_argument("--allow-encode-change", action="store_true",
                   help="changement des images décodées d'un livrable attendu (ex. correctif d'encodage)")
    c.set_defaults(func=cmd_compare)
    args = p.parse_args()
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
