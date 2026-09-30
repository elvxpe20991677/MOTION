#!/usr/bin/env python3
"""Baseline du pipeline : tests de vérification + porte de déterminisme + versions, puis comparaison.

Usage (depuis la racine, avec le Python de l'orchestrateur) :
    .venv/Scripts/python.exe scripts/baseline.py run --label phase-0-a
    .venv/Scripts/python.exe scripts/baseline.py run --label phase-1 --save docs/baseline/phase-1.json
    .venv/Scripts/python.exe scripts/baseline.py compare docs/baseline/phase-0.json build/_baseline/phase-1/baseline.json

`run` exécute, dans cet ordre :
  1. tous les scripts tests/verify_step*.py (ordre alphabétique) puis tests/verify_features.py, un par
     un, avec un journal par script ;
  2. sur la scène de référence (défaut demo_flat_riso), dans un build/ et un out/ isolés et vidés :
     render -> determinism -> compose -> encode -> qc, puis relève les empreintes de TOUTES les images
     des plans, de toutes les images maître et des images décodées de chaque livrable ;
  3. les versions des outils et l'état git (commit + fichiers non commités).
Le résultat est écrit dans build/_baseline/<label>/baseline.json (et copié vers --save si demandé).
`run` sort avec un code ≠ 0 si un test échoue, si une étape de la scène échoue, si la porte de
déterminisme échoue ou si le QC n'est pas CONFORME.

`compare A B` sort avec un code ≠ 0 (régression) si, dans B par rapport à A :
  - un test manque, échoue, perd des PASS, ou gagne des FAIL, WARN ou SKIP ; un nouveau test échoue ;
  - la scène de référence manque, une de ses étapes échoue, la porte de déterminisme échoue ;
  - le nombre d'images des plans ou du maître change, une empreinte d'image change ou manque
    (changement d'empreinte toléré seulement avec --allow-pixel-change) ;
  - un livrable manque, n'est pas décodable, ou ses images décodées changent (toléré seulement avec
    --allow-encode-change) ;
  - le verdict QC change, ou le QC gagne des FAIL, WARN ou SKIP.
Sont seulement signalés (NOTE) : versions, octets d'un livrable aux images identiques, nouveaux tests
réussis, PASS en hausse, fichiers non commités au moment de la mesure.
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

REFERENCE_SCENE = "demo_flat_riso"
# Lignes d'assertion des tests : en colonne 0 (CONTRACT §1.8). Les lignes indentées sont des échos
# (ex. rapport QC imprimé par verify_step17) et ne doivent pas être comptées.
STATUS_RE = re.compile(r"^(PASS|FAIL|WARN|SKIP|INFO)\b")
# Un label devient un nom de dossier vidé par rmtree : jamais de « .. », de séparateur ni de chemin absolu.
LABEL_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
BASELINE_ROOT = config.BUILD_DIR / "_baseline"
STEP_TIMEOUT_S = 7200


def test_scripts() -> list[str]:
    """Tous les verify_step*.py (ordre alphabétique = ordre des étapes), puis verify_features.py.

    Liste découverte et non codée en dur : un test ajouté par une phase est rejoué automatiquement.
    """
    steps = sorted(p.relative_to(ROOT).as_posix() for p in (ROOT / "tests").glob("verify_step*.py"))
    extra = [p for p in ("tests/verify_features.py",) if (ROOT / p).is_file()]
    return steps + extra


def _write_text_atomic(path: Path, text: str) -> None:
    # Écriture atomique (CONTRACT §1.5) : jamais de fichier à moitié écrit.
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8", newline="\n")
    os.replace(tmp, path)


def _write_json_atomic(path: Path, obj) -> None:
    _write_text_atomic(path, json.dumps(obj, ensure_ascii=False, indent=2) + "\n")


def _env(extra: dict | None = None) -> dict:
    # UTF-8 forcé : les tests impriment des accents, la console Windows est en cp1252 par défaut.
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1")
    env.update(extra or {})
    return env


def _kill_tree(proc: subprocess.Popen) -> None:
    """Arrête le processus lancé ET ses descendants (ffmpeg, Blender, navigateurs), par PID connu."""
    if config.IS_WINDOWS:
        # taskkill /T : tout l'arbre du PID connu (CONTRACT §1.9 : jamais de kill par motif).
        subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True)
    else:
        try:
            os.killpg(proc.pid, 9)  # groupe créé par start_new_session
        except OSError:
            proc.kill()


def _run(argv: list[str], log_path: Path, *, env: dict | None = None,
         timeout: int = STEP_TIMEOUT_S) -> tuple[int, str, float]:
    """Lance une commande, écrit sa sortie complète dans log_path, renvoie (code, sortie, durée).

    Popen + groupe de processus : en cas de délai dépassé, tout l'arbre est arrêté, sinon les
    petits-enfants qui gardent les tubes ouverts feraient attendre communicate() sans fin.
    """
    t0 = time.perf_counter()
    kwargs = ({"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if config.IS_WINDOWS
              else {"start_new_session": True})
    proc = subprocess.Popen(argv, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                            encoding="utf-8", errors="replace", env=env or _env(), **kwargs)
    try:
        so, se = proc.communicate(timeout=timeout)
        code = proc.returncode
        out = (so or "") + (se or "")
    except subprocess.TimeoutExpired:
        _kill_tree(proc)
        try:
            so, se = proc.communicate(timeout=60)
        except subprocess.TimeoutExpired:
            so, se = "", ""
        code, out = 124, (so or "") + (se or "") + f"\nDÉLAI DÉPASSÉ ({timeout} s) : processus arrêté.\n"
    dt = time.perf_counter() - t0
    _write_text_atomic(log_path, out)
    return code, out, dt


def run_tests(workdir: Path, log) -> list[dict]:
    results = []
    for rel in test_scripts():
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
        log(f"  -> code {code}, {counts['PASS']} PASS / {counts['FAIL']} FAIL / {counts['WARN']} WARN "
            f"/ {counts['SKIP']} SKIP en {dt:.0f} s")
    return results


def _pixel_sha256(path: Path) -> str:
    # Même définition que le pilote web (pixels décodés), pour comparer avec les manifestes.
    import cv2
    import numpy as np
    from pipeline.web_render import pixel_sha256

    # np.fromfile + imdecode : chemins non ASCII sous Windows (cv2.imread les refuse), comme compose.py.
    img = cv2.imdecode(np.fromfile(str(path), dtype=np.uint8), cv2.IMREAD_UNCHANGED)
    if img is None:
        raise RuntimeError(f"image illisible : {path}")
    return pixel_sha256(img)


def _decoded_md5(path: Path) -> str | None:
    """MD5 des images DÉCODÉES (flux vidéo seul, insensible au conteneur) ; None si le décodage échoue."""
    try:
        r = subprocess.run([config.FFMPEG, "-v", "error", "-i", str(path), "-map", "0:v:0", "-f", "md5", "-"],
                           capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=1800)
    except (OSError, subprocess.SubprocessError):
        return None
    out = r.stdout.strip()
    return out.removeprefix("MD5=") if r.returncode == 0 and out.startswith("MD5=") else None


def _file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _clear_dir(d: Path) -> None:
    """Vide un dossier de travail ; un échec (fichier ouvert ailleurs) arrête la baseline.

    Sous Windows, un livrable ouvert par un lecteur ou par Resolve survivrait à un rmtree silencieux
    et la baseline relirait l'ANCIEN fichier : on refuse de mesurer dans ce cas.
    """
    try:
        shutil.rmtree(d)
    except FileNotFoundError:
        pass
    except OSError as exc:
        raise SystemExit(f"ERREUR : impossible de vider {d} ({exc}). Fermez le lecteur vidéo ou DaVinci "
                         "Resolve qui utilise un fichier de ce dossier, puis relancez la baseline.")


def _collect_qc(q: dict) -> tuple[dict, list[str]]:
    checks = []

    def walk(o):
        # Les contrôles peuvent être imbriqués par fichier : on collecte tout dict qui a un statut et un id.
        if isinstance(o, dict):
            if "status" in o and "id" in o:
                checks.append(o)
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)

    walk(q)
    counts = {s: sum(1 for c in checks if c.get("status") == s) for s in ("PASS", "FAIL", "WARN", "SKIP")}
    non_pass = [f"{c.get('status')} {c.get('id')} : {str(c.get('actual'))[:120]}"
                for c in checks if c.get("status") != "PASS"]
    return counts, non_pass


def run_scene(workdir: Path, scene: str, count: int | None, log) -> dict:
    build, out = workdir / "build", workdir / "out"
    # Dossiers vidés : la baseline mesure un rendu COMPLET, jamais une reprise partielle.
    for d in (build, out):
        _clear_dir(d)
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
    try:
        # build/ isolé et vidé : la seule scène compilée est la nôtre, que --scene soit un id ou un chemin.
        found = sorted(build.glob("*/compiled.json"))
        if len(found) != 1:
            raise RuntimeError(f"{len(found)} scène(s) compilée(s) trouvée(s) dans {build} (1 attendue) : "
                               f"voir {workdir / 'logs' / 'scene_render.log'}")
        sdir = found[0].parent
        scene_id = sdir.name
        result["scene_id"] = scene_id
        compiled = json.loads(found[0].read_text(encoding="utf-8"))
        shots = {}
        for shot in compiled["shots"]:
            mpath = sdir / "shots" / shot["id"] / "manifest.json"
            if mpath.exists():
                fd = json.loads(mpath.read_text(encoding="utf-8"))["frames_done"]
                shots[shot["id"]] = [fd[k]["sha256"] for k in sorted(fd, key=int)]
        result["shot_frames"] = shots
        # Empreinte globale : un seul hash résume toutes les images de tous les plans.
        lines = "".join(f"{sid}:{i}:{h}\n" for sid in shots for i, h in enumerate(shots[sid]))
        result["shots_fingerprint"] = hashlib.sha256(lines.encode()).hexdigest() if shots else None
        det = sdir / "determinism.json"
        if det.exists():
            d = json.loads(det.read_text(encoding="utf-8"))
            result["determinism"] = {"ok": d["ok"], "matches": d["matches"], "total": d["total"], "order": d["order"]}
        master = sorted((sdir / "master").glob("[0-9]*.png"))
        result["master_frames"] = len(master)
        result["master_fingerprint"] = (hashlib.sha256("".join(_pixel_sha256(p) + "\n" for p in master).encode())
                                        .hexdigest() if master else None)
        deliverables = {}
        for p in sorted((out / scene_id).glob("*")):
            if p.suffix in (".mp4", ".mov") and ".partial" not in p.name:
                deliverables[p.name] = {"decoded_md5": _decoded_md5(p), "file_sha256": _file_sha256(p),
                                        "bytes": p.stat().st_size}
        result["deliverables"] = deliverables
        qc_json = out / scene_id / "qc_report.json"
        if qc_json.exists():
            q = json.loads(qc_json.read_text(encoding="utf-8"))
            result["qc_verdict"] = q.get("verdict")
            result["qc_counts"], result["qc_non_pass"] = _collect_qc(q)
    except Exception as exc:  # noqa: BLE001 — la baseline est écrite quand même, avec l'erreur (code ≠ 0)
        result["error"] = f"{type(exc).__name__} : {exc}"
    return result


def _git(args: list[str]) -> str:
    try:
        r = subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=60)
        return r.stdout.strip()
    except (OSError, subprocess.SubprocessError) as exc:
        return f"(indisponible : {exc})"


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
        "git_head": _git(["log", "-1", "--format=%H %s"]),
        # Fichiers non commités au moment de la mesure : une baseline « propre » a une liste vide.
        "git_dirty": [l for l in _git(["status", "--porcelain"]).splitlines() if l.strip()],
    }


def cmd_run(args) -> int:
    if not LABEL_RE.fullmatch(args.label) or ".." in args.label:
        raise SystemExit(f"ERREUR : label invalide « {args.label} » : lettres, chiffres, « . », « _ » et « - » "
                         "uniquement (ex. phase-1).")
    workdir = BASELINE_ROOT / args.label
    if not workdir.resolve().is_relative_to(BASELINE_ROOT.resolve()):
        raise SystemExit(f"ERREUR : le dossier de travail {workdir} sort de {BASELINE_ROOT}.")
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
    problems = [f"test en échec : {t['script']} (code {t['exit_code']})" for t in data["tests"] if t["exit_code"] != 0]
    ref = data["reference_scene"]
    if ref is not None:
        problems += [f"étape {n} de la scène en échec (code {s['exit_code']})"
                     for n, s in ref["steps"].items() if s["exit_code"] != 0]
        if ref.get("error"):
            problems.append(f"relevé de la scène impossible : {ref['error']}")
        if not (ref.get("determinism") or {}).get("ok"):
            problems.append("porte de déterminisme en ÉCHEC ou absente")
        if ref.get("qc_verdict") != "CONFORME":
            problems.append(f"verdict QC : {ref.get('qc_verdict')}")
    for p in problems:
        log(f"ÉCHEC {p}")
    log("baseline saine" if not problems else f"{len(problems)} problème(s)")
    return 0 if not problems else 1


def _load(p: str) -> dict:
    path = Path(p) if Path(p).is_absolute() else ROOT / p
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"ERREUR : baseline illisible {path} ({exc}). Lancez d'abord : "
                         ".venv/Scripts/python.exe scripts/baseline.py run --label <label>")


def cmd_compare(args) -> int:
    a, b = _load(args.a), _load(args.b)
    problems, notes = [], []
    ta = {t["script"]: t for t in a.get("tests", [])}
    tb = {t["script"]: t for t in b.get("tests", [])}
    for s in sorted(ta.keys() - tb.keys()):
        problems.append(f"test absent de B : {s} (run lancé avec --skip-tests, ou script supprimé)")
    for s, t in tb.items():
        ref = ta.get(s)
        c = t["counts"]
        if ref is None:
            if t["exit_code"] != 0 or c["FAIL"]:
                problems.append(f"nouveau test en échec : {s} (code {t['exit_code']}, {c['FAIL']} FAIL) ; {t['fail_lines'][:3]}")
            else:
                notes.append(f"nouveau test réussi : {s} ({c['PASS']} PASS)")
            continue
        r = ref["counts"]
        if t["exit_code"] != 0 and ref["exit_code"] == 0:
            problems.append(f"RÉGRESSION {s} : code {ref['exit_code']} -> {t['exit_code']} ; {t['fail_lines'][:3]}")
        if c["PASS"] < r["PASS"]:
            problems.append(f"RÉGRESSION {s} : PASS {r['PASS']} -> {c['PASS']} (contrôles perdus)")
        elif c["PASS"] > r["PASS"]:
            notes.append(f"{s} : PASS {r['PASS']} -> {c['PASS']}")
        for k in ("FAIL", "WARN", "SKIP"):
            if c[k] > r[k]:
                problems.append(f"RÉGRESSION {s} : {k} {r[k]} -> {c[k]}")
    ra, rb = a.get("reference_scene"), b.get("reference_scene")
    if ra and not rb:
        problems.append("scène de référence absente de B (run lancé avec --skip-scene ?)")
    if rb:
        for n, st in (rb.get("steps") or {}).items():
            if st["exit_code"] != 0:
                problems.append(f"étape {n} de la scène en échec dans B (code {st['exit_code']}) : voir logs/scene_{n}.log")
        if rb.get("error"):
            problems.append(f"relevé de la scène en erreur dans B : {rb['error']}")
        if not (rb.get("determinism") or {}).get("ok"):
            problems.append("porte de déterminisme en ÉCHEC ou absente dans B")
    if ra and rb:
        if ra.get("scene_id", ra.get("scene")) != rb.get("scene_id", rb.get("scene")):
            problems.append(f"scènes différentes : {ra.get('scene')} / {rb.get('scene')}")
        fa = {k: len(v) for k, v in (ra.get("shot_frames") or {}).items()}
        fb = {k: len(v) for k, v in (rb.get("shot_frames") or {}).items()}
        if fa != fb:
            problems.append(f"images par plan différentes : {fa} -> {fb}")
        if ra.get("master_frames") != rb.get("master_frames"):
            problems.append(f"images maître : {ra.get('master_frames')} -> {rb.get('master_frames')}")
        for key in ("shots_fingerprint", "master_fingerprint"):
            if rb.get(key) is None:
                problems.append(f"{key} absent de B")
            elif ra.get(key) != rb.get(key):
                (notes if args.allow_pixel_change else problems).append(
                    f"{key} différent : {ra.get(key)} -> {rb.get(key)}")
        da, db = ra.get("deliverables") or {}, rb.get("deliverables") or {}
        for name in sorted(da.keys() - db.keys()):
            problems.append(f"livrable absent de B : {name}")
        for name, d in db.items():
            if d["decoded_md5"] is None:
                problems.append(f"livrable {name} : décodage impossible dans B")
                continue
            old = da.get(name)
            if old is None:
                notes.append(f"nouveau livrable : {name}")
            elif old["decoded_md5"] != d["decoded_md5"]:
                (notes if args.allow_encode_change else problems).append(
                    f"livrable {name} : images décodées différentes ({old['decoded_md5']} -> {d['decoded_md5']})")
            elif old["file_sha256"] != d["file_sha256"]:
                notes.append(f"livrable {name} : octets du fichier différents (images décodées identiques)")
        if ra.get("qc_verdict") != rb.get("qc_verdict"):
            problems.append(f"verdict QC : {ra.get('qc_verdict')} -> {rb.get('qc_verdict')}")
        qa, qb = ra.get("qc_counts") or {}, rb.get("qc_counts") or {}
        for k in ("FAIL", "WARN", "SKIP"):
            if qb.get(k, 0) > qa.get(k, 0):
                problems.append(f"QC : {k} {qa.get(k, 0)} -> {qb.get(k, 0)} ; {rb.get('qc_non_pass', [])[:4]}")
        if qb.get("PASS", 0) != qa.get("PASS", 0):
            notes.append(f"QC : PASS {qa.get('PASS', 0)} -> {qb.get('PASS', 0)}")
    va, vb = a.get("versions", {}), b.get("versions", {})
    for k in ("ffmpeg", "python_orchestrator", "bpy", "chromium", "node", "uv", "gpu"):
        if va.get(k) != vb.get(k):
            notes.append(f"version {k} : {va.get(k)} -> {vb.get(k)}")
    for k in ("pip_freeze", "npm_ls"):
        if va.get(k) != vb.get(k):
            notes.append(f"{k} différent : -{sorted(set(va.get(k, [])) - set(vb.get(k, [])))} "
                         f"+{sorted(set(vb.get(k, [])) - set(va.get(k, [])))}")
    if vb.get("git_dirty"):
        notes.append(f"B mesurée avec des fichiers non commités : {vb['git_dirty'][:8]}")
    for n in notes:
        print("NOTE", n)
    for p in problems:
        print("FAIL", p)
    print("PASS aucune régression" if not problems else f"\n{len(problems)} régression(s)")
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
    r.add_argument("--label", required=True, help="nom du dossier build/_baseline/<label>/ (ex. phase-1)")
    r.add_argument("--scene", default=REFERENCE_SCENE, help=f"scène de référence, id ou chemin (défaut {REFERENCE_SCENE})")
    r.add_argument("--count", type=int, default=None, help="images témoins du déterminisme (défaut : scène)")
    r.add_argument("--save", default=None, help="copier baseline.json vers ce chemin (ex. docs/baseline/phase-0.json)")
    r.add_argument("--skip-tests", action="store_true", help="ne pas lancer les scripts de vérification")
    r.add_argument("--skip-scene", action="store_true", help="ne pas rendre la scène de référence")
    r.set_defaults(func=cmd_run)
    c = sub.add_parser("compare", help="comparer deux baselines (code ≠ 0 si régression)")
    c.add_argument("a", help="baseline de référence (chemin relatif à la racine ou absolu)")
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
