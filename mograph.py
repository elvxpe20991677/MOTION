#!/usr/bin/env python3
"""mograph — CLI du pipeline motion design (scène JSON -> vidéo déterministe contrôlée).

Commandes :
    validate     <scène>   valide et compile, affiche format, images, plans et fondus
    plates       <scène>   rend uniquement les plaques 3D (Blender)
    render       <scène>   plaques 3D puis images web de chaque plan
    determinism  <scène>   porte de déterminisme (images témoins, navigateur neuf, ordre mélangé)
    compose      <scène>   reconstruit la séquence maître
    encode       <scène>   séquence maître puis encodage de tous les livrables
    qc           <scène>   contrôle qualité automatique (code de sortie 1 si NON CONFORME)
    all          <scène>   plaques -> images web -> déterminisme -> maître -> encodage -> QC
    synth-beat             génère une piste de test déterministe (WAV 48 kHz 24 bits)
    status       <scène>   avancement d'un rendu (y compris détaché)
    versions     <scène>   archive les versions des outils dans out/<scène>/versions.txt

<scène> = chemin d'un fichier JSON, ou id d'une scène de scenes/.
Option --detach (render, plates, all) : relance la commande dans un processus détaché qui survit
à la fermeture du terminal (équivalent Windows de setsid/tmux), journal dans build/<scène>/logs/.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import subprocess
import sys
import time
import traceback
from pathlib import Path

from pipeline import config

# Codes de sortie : 0 succès, 1 échec du pipeline ou verdict négatif, 2 erreur d'usage (argparse).
EXIT_OK = 0
EXIT_FAIL = 1


# ---------------------------------------------------------------------------------------------
# Utilitaires
# ---------------------------------------------------------------------------------------------

def _log(msg: str) -> None:
    # Horodatage court + flush immédiat : lisible en direct dans un journal de processus détaché.
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def _banner(title: str) -> None:
    print("\n" + "=" * 78 + f"\n  {title}\n" + "=" * 78, flush=True)


def resolve_scene_path(arg: str) -> Path:
    """Accepte un chemin de fichier ou un id de scène (scenes/<id>.json)."""
    p = Path(arg)
    if p.suffix.lower() == ".json" and p.exists():
        return p.resolve()
    candidate = config.SCENES_DIR / (arg if arg.endswith(".json") else f"{arg}.json")
    if candidate.exists():
        return candidate.resolve()
    raise SystemExit(
        f"Scène introuvable : « {arg} ». Donnez un chemin vers un fichier .json existant "
        f"ou l'id d'une scène de {config.SCENES_DIR} (ex. demo_flat_riso)."
    )


def compile_or_exit(scene_arg: str) -> dict:
    """Compile la scène ; en cas d'erreur, affiche les messages actionnables et quitte avec 1."""
    from pipeline import scene as scene_mod

    path = resolve_scene_path(scene_arg)
    try:
        compiled = scene_mod.compile_scene(path)
    except scene_mod.SceneError as exc:
        print(f"SCÈNE REFUSÉE : {path}", file=sys.stderr)
        print(str(exc), file=sys.stderr)
        raise SystemExit(EXIT_FAIL)
    scene_mod.write_compiled(compiled)
    return compiled


def _scene_id_from_arg(scene_arg: str) -> str:
    # Lecture légère de l'id sans compiler (utile pour status/detach même si la scène est invalide).
    path = resolve_scene_path(scene_arg)
    with open(path, encoding="utf-8") as fh:
        return str(json.load(fh).get("id") or path.stem)


def _logs_dir(scene_id: str) -> Path:
    d = config.scene_build_dir(scene_id) / "logs"
    d.mkdir(parents=True, exist_ok=True)
    return d


# ---------------------------------------------------------------------------------------------
# Processus détaché (équivalent setsid / tmux)
# ---------------------------------------------------------------------------------------------

def detach(argv: list[str], scene_id: str, command: str) -> int:
    """Relance `mograph.py <argv sans --detach>` dans un processus indépendant du terminal."""
    logs = _logs_dir(scene_id)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    log_path = logs / f"{command}-{stamp}.log"
    pid_path = logs / f"{command}.pid"
    child_argv = [str(config.ORCHESTRATOR_PYTHON), "-u", str(Path(__file__).resolve())] + [
        a for a in argv if a != "--detach"
    ]
    env = dict(os.environ, PYTHONUNBUFFERED="1", PYTHONIOENCODING="utf-8")
    log_fh = open(log_path, "ab")
    try:
        if config.IS_WINDOWS:
            # DETACHED_PROCESS : pas de console héritée ; NEW_PROCESS_GROUP : insensible au Ctrl+C du
            # terminal ; BREAKAWAY_FROM_JOB : survit à la fermeture du job de l'hôte (repli si interdit).
            flags = (
                subprocess.DETACHED_PROCESS
                | subprocess.CREATE_NEW_PROCESS_GROUP
                | 0x01000000  # CREATE_BREAKAWAY_FROM_JOB
            )
            try:
                proc = subprocess.Popen(child_argv, cwd=config.ROOT, stdin=subprocess.DEVNULL,
                                        stdout=log_fh, stderr=subprocess.STDOUT, env=env,
                                        creationflags=flags, close_fds=True)
            except OSError:
                flags &= ~0x01000000
                proc = subprocess.Popen(child_argv, cwd=config.ROOT, stdin=subprocess.DEVNULL,
                                        stdout=log_fh, stderr=subprocess.STDOUT, env=env,
                                        creationflags=flags, close_fds=True)
        else:
            # start_new_session = setsid : le rendu n'est plus rattaché à la session du shell.
            proc = subprocess.Popen(child_argv, cwd=config.ROOT, stdin=subprocess.DEVNULL,
                                    stdout=log_fh, stderr=subprocess.STDOUT, env=env,
                                    start_new_session=True, close_fds=True)
    finally:
        log_fh.close()
    pid_path.write_text(f"{proc.pid}\n{log_path}\n", encoding="utf-8")
    print(f"Rendu détaché : PID {proc.pid}")
    print(f"Journal       : {log_path}")
    print(f"Suivi         : python mograph.py status {scene_id}")
    return EXIT_OK


def _pid_alive(pid: int) -> bool:
    """Vrai si le processus existe encore (sans le signaler ni le tuer)."""
    if config.IS_WINDOWS:
        import ctypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        k32 = ctypes.windll.kernel32
        h = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not h:
            return False
        try:
            code = ctypes.c_ulong()
            ok = k32.GetExitCodeProcess(h, ctypes.byref(code))
            return bool(ok) and code.value == STILL_ACTIVE
        finally:
            k32.CloseHandle(h)
    try:
        os.kill(pid, 0)  # signal 0 : test d'existence uniquement
        return True
    except OSError:
        return False


# ---------------------------------------------------------------------------------------------
# Étapes du pipeline
# ---------------------------------------------------------------------------------------------

def step_audio(compiled: dict) -> None:
    if not compiled.get("audio"):
        return
    from pipeline import audio

    p = audio.ensure_scene_audio(compiled, log=_log)
    if p:
        _log(f"Audio : {p}")


def step_plates(compiled: dict) -> None:
    jobs = [j for s in compiled["shots"] for j in s.get("plates", [])]
    if not jobs:
        _log("Aucune plaque 3D.")
        return
    from pipeline import blender_render

    _banner(f"PLAQUES 3D ({len(jobs)})")
    blender_render.render_scene_plates(compiled, log=_log)


def step_web(compiled: dict, workers: int | None) -> None:
    from pipeline import web_render

    _banner(f"IMAGES WEB ({compiled['frames']} images maître, {len(compiled['shots'])} plan(s))")
    web_render.render_scene(compiled, workers=workers, log=_log)


def step_determinism(compiled: dict, count: int | None) -> bool:
    from pipeline import web_render

    _banner("PORTE DE DÉTERMINISME")
    report = web_render.verify_determinism(compiled, count=count, log=_log)
    _log(f"Déterminisme : {report['matches']}/{report['total']} images témoins identiques au bit près")
    if not report["ok"]:
        print(
            "\nÉCHEC DU DÉTERMINISME — NE PAS LIVRER. Diagnostic (SPEC étape 19) :\n"
            "  1. désactiver les effets un par un (fx: grain/paper/vignette/scanlines/tracking/glitch, boil)\n"
            "     et relancer `python mograph.py determinism <scène>` ;\n"
            "  2. vérifier que chaque tween est un fromTo explicite (build/<scène>/compiled.json) ;\n"
            "  3. vérifier que tuiles de grain et plaques 3D sont décodées avant la capture ;\n"
            f"  4. comparer les images de différence dans {config.scene_build_dir(compiled['scene_id']) / 'determinism_diff'}.",
            file=sys.stderr,
        )
    return bool(report["ok"])


def step_compose(compiled: dict) -> None:
    from pipeline import compose

    _banner("SÉQUENCE MAÎTRE")
    manifest = compose.compose_scene(compiled, log=_log)
    _log(f"Maître : {manifest['frames']} images, {manifest['depth']} bits, {manifest['width']}x{manifest['height']}")


def step_encode(compiled: dict) -> list[Path]:
    from pipeline import encode

    _banner(f"ENCODAGE ({len(compiled['outputs'])} livrable(s))")
    outs = encode.encode_scene(compiled, log=_log)
    for p in outs:
        _log(f"Livrable : {p}")
    return outs


def step_qc(compiled: dict) -> bool:
    from pipeline import qc

    _banner("CONTRÔLE QUALITÉ")
    report = qc.run_qc(compiled, log=_log)
    out = config.scene_out_dir(compiled["scene_id"])
    _log(f"Verdict : {report['verdict']}  ({out / 'qc_report.md'})")
    return report["verdict"] == "CONFORME"


def write_versions(scene_id: str) -> Path:
    """Archive les versions exactes des outils (SPEC étape 21) dans out/<scène>/versions.txt."""

    def run(argv: list[str]) -> str:
        try:
            r = subprocess.run(argv, cwd=config.ROOT, capture_output=True, text=True,
                               encoding="utf-8", errors="replace", timeout=120)
            return (r.stdout + r.stderr).strip()
        except (OSError, subprocess.SubprocessError) as exc:
            return f"(indisponible : {exc})"

    import shutil

    npm = shutil.which("npm") or "npm"
    sections = [
        ("Date (UTC)", time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())),
        ("Système", f"{platform.platform()} | {platform.processor()} | {os.cpu_count()} threads"),
        ("Python orchestrateur", sys.version.replace("\n", " ")),
        ("ffmpeg -version", run([config.FFMPEG, "-hide_banner", "-version"])),
        ("pip freeze", run([str(config.ORCHESTRATOR_PYTHON), "-m", "pip", "freeze"])),
        ("npm ls --depth=0", run([npm, "ls", "--depth=0"])),
        ("bpy", run([str(config.BLENDER_PYTHON), "-c",
                     "import bpy, sys; print('bpy', bpy.app.version_string, '| python', sys.version.split()[0])"])),
        ("Chromium (playwright)", run([str(config.ORCHESTRATOR_PYTHON), "-c",
                                       "from playwright.sync_api import sync_playwright\n"
                                       "with sync_playwright() as p:\n"
                                       "    b = p.chromium.launch(); print(b.version); b.close()"])),
        ("git", run(["git", "log", "-1", "--format=%H %cd"]) or "(aucun commit)"),
    ]
    out = config.scene_out_dir(scene_id)
    out.mkdir(parents=True, exist_ok=True)
    path = out / "versions.txt"
    tmp = path.with_suffix(".txt.tmp")
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        for title, body in sections:
            fh.write(f"### {title}\n{body}\n\n")
    os.replace(tmp, path)
    return path


# ---------------------------------------------------------------------------------------------
# Commandes
# ---------------------------------------------------------------------------------------------

def cmd_validate(args) -> int:
    from pipeline import scene as scene_mod

    compiled = compile_or_exit(args.scene)
    print(scene_mod.summary(compiled))
    print(f"\nScène compilée : {config.scene_build_dir(compiled['scene_id']) / 'compiled.json'}")
    return EXIT_OK


def cmd_plates(args) -> int:
    if args.detach:
        return detach(sys.argv[1:], _scene_id_from_arg(args.scene), "plates")
    compiled = compile_or_exit(args.scene)
    step_plates(compiled)
    return EXIT_OK


def cmd_render(args) -> int:
    if args.detach:
        return detach(sys.argv[1:], _scene_id_from_arg(args.scene), "render")
    compiled = compile_or_exit(args.scene)
    step_audio(compiled)
    if not args.no_plates:
        step_plates(compiled)
    step_web(compiled, args.workers)
    return EXIT_OK


def cmd_determinism(args) -> int:
    compiled = compile_or_exit(args.scene)
    return EXIT_OK if step_determinism(compiled, args.count) else EXIT_FAIL


def cmd_compose(args) -> int:
    compiled = compile_or_exit(args.scene)
    step_compose(compiled)
    return EXIT_OK


def cmd_encode(args) -> int:
    compiled = compile_or_exit(args.scene)
    step_audio(compiled)
    # La séquence maître est reconstruite de zéro à chaque encodage : jamais de maître périmé.
    step_compose(compiled)
    step_encode(compiled)
    return EXIT_OK


def cmd_qc(args) -> int:
    compiled = compile_or_exit(args.scene)
    return EXIT_OK if step_qc(compiled) else EXIT_FAIL


def cmd_all(args) -> int:
    if args.detach:
        return detach(sys.argv[1:], _scene_id_from_arg(args.scene), "all")
    t0 = time.perf_counter()
    compiled = compile_or_exit(args.scene)
    from pipeline import scene as scene_mod

    _banner(f"SCÈNE {compiled['scene_id']}")
    print(scene_mod.summary(compiled), flush=True)
    step_audio(compiled)
    step_plates(compiled)
    step_web(compiled, args.workers)
    if not step_determinism(compiled, args.count):
        # Porte bloquante (SPEC étape 19) : aucun livrable n'est produit si le rendu n'est pas reproductible.
        return EXIT_FAIL
    step_compose(compiled)
    step_encode(compiled)
    versions = write_versions(compiled["scene_id"])
    _log(f"Versions archivées : {versions}")
    ok = step_qc(compiled)
    _log(f"Durée totale : {time.perf_counter() - t0:.1f} s")
    return EXIT_OK if ok else EXIT_FAIL


def cmd_synth_beat(args) -> int:
    from pipeline import audio

    out = Path(args.out)
    if not out.is_absolute():
        out = config.ROOT / out
    out.parent.mkdir(parents=True, exist_ok=True)
    p = audio.synth_beat(out, args.bpm, args.duration, beats_per_bar=args.beats_per_bar,
                         offset=args.offset, target_lufs=args.target_lufs)
    m = audio.measure_loudness(p)
    print(f"{p}\n  {args.bpm} BPM, {args.duration} s, "
          f"{m['integrated_lufs']:.2f} LUFS intégrés, true peak {m['true_peak_dbtp']:.2f} dBTP")
    return EXIT_OK


def cmd_status(args) -> int:
    scene_id = _scene_id_from_arg(args.scene)
    bdir = config.scene_build_dir(scene_id)
    print(f"Scène {scene_id} — {bdir}")
    compiled_path = bdir / "compiled.json"
    if not compiled_path.exists():
        print("  (pas encore compilée : lancez validate ou render)")
        return EXIT_OK
    compiled = json.loads(compiled_path.read_text(encoding="utf-8"))
    for shot in compiled["shots"]:
        for job in shot.get("plates", []):
            d = Path(job["out_dir"])
            n = len(list(d.glob("[0-9][0-9][0-9][0-9][0-9][0-9].png"))) if d.exists() else 0
            print(f"  plaque {shot['id']}/{job['plate_id']:<12} {n:>5}/{job['frames']}")
        mpath = config.shot_frames_dir(scene_id, shot["id"]) / "manifest.json"
        done = 0
        if mpath.exists():
            try:
                done = len(json.loads(mpath.read_text(encoding="utf-8")).get("frames_done", {}))
            except (OSError, json.JSONDecodeError):
                done = -1
        print(f"  plan   {shot['id']:<19} {done:>5}/{shot['frames']}")
    master = config.master_dir(scene_id)
    n_master = len(list(master.glob("[0-9]*.png"))) if master.exists() else 0
    print(f"  maître {'':<19} {n_master:>5}/{compiled['frames']}")
    det = bdir / "determinism.json"
    if det.exists():
        r = json.loads(det.read_text(encoding="utf-8"))
        print(f"  déterminisme : {r.get('matches')}/{r.get('total')} ({'OK' if r.get('ok') else 'ÉCHEC'})")
    qc_json = config.scene_out_dir(scene_id) / "qc_report.json"
    if qc_json.exists():
        print(f"  QC : {json.loads(qc_json.read_text(encoding='utf-8')).get('verdict')}")
    for pid_file in sorted((bdir / "logs").glob("*.pid")) if (bdir / "logs").exists() else []:
        lines = pid_file.read_text(encoding="utf-8").splitlines()
        pid, log_path = int(lines[0]), Path(lines[1]) if len(lines) > 1 else None
        state = "EN COURS" if _pid_alive(pid) else "terminé"
        print(f"  processus détaché {pid_file.stem} PID {pid} : {state}")
        if log_path and log_path.exists():
            tail = log_path.read_text(encoding="utf-8", errors="replace").splitlines()[-args.lines:]
            print("    " + "\n    ".join(tail))
    return EXIT_OK


def cmd_versions(args) -> int:
    path = write_versions(_scene_id_from_arg(args.scene))
    print(path)
    return EXIT_OK


# ---------------------------------------------------------------------------------------------
# Analyse des arguments
# ---------------------------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="mograph", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="command", required=True)

    def scene_cmd(name: str, fn, help_: str, *, detachable=False, workers=False, count=False):
        sp = sub.add_parser(name, help=help_)
        sp.add_argument("scene", help="chemin d'un .json ou id de scène (scenes/<id>.json)")
        if detachable:
            sp.add_argument("--detach", action="store_true",
                            help="processus détaché (survit au terminal), journal dans build/<scène>/logs/")
        if workers:
            sp.add_argument("--workers", type=int, default=None,
                            help="navigateurs en parallèle pour les images web (défaut : automatique)")
        if count:
            sp.add_argument("--count", type=int, default=None,
                            help="nombre d'images témoins (défaut : qc.determinism_frames de la scène)")
        sp.set_defaults(func=fn)
        return sp

    scene_cmd("validate", cmd_validate, "valider et compiler une scène")
    scene_cmd("plates", cmd_plates, "rendre les plaques 3D (Blender)", detachable=True)
    r = scene_cmd("render", cmd_render, "plaques 3D + images web", detachable=True, workers=True)
    r.add_argument("--no-plates", action="store_true", help="ne pas (re)lancer Blender")
    scene_cmd("determinism", cmd_determinism, "porte de déterminisme", count=True)
    scene_cmd("compose", cmd_compose, "reconstruire la séquence maître")
    scene_cmd("encode", cmd_encode, "séquence maître + encodage des livrables")
    scene_cmd("qc", cmd_qc, "contrôle qualité automatique")
    scene_cmd("all", cmd_all, "pipeline complet", detachable=True, workers=True, count=True)
    s = scene_cmd("status", cmd_status, "avancement d'un rendu")
    s.add_argument("--lines", type=int, default=8, help="lignes de journal affichées")
    scene_cmd("versions", cmd_versions, "archiver les versions des outils")

    sb = sub.add_parser("synth-beat", help="générer une piste de test déterministe")
    sb.add_argument("--bpm", type=float, required=True)
    sb.add_argument("--duration", type=float, required=True, help="secondes")
    sb.add_argument("--out", required=True, help="fichier WAV (relatif à la racine du projet)")
    sb.add_argument("--beats-per-bar", type=int, default=4)
    sb.add_argument("--offset", type=float, default=0.0, help="temps du premier temps (s)")
    sb.add_argument("--target-lufs", type=float, default=-14.0)
    sb.set_defaults(func=cmd_synth_beat)
    return p


def main(argv: list[str] | None = None) -> int:
    # Sortie console en UTF-8 : accents lisibles même sous la console Windows (cp1252 par défaut).
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args) or EXIT_OK)
    except SystemExit:
        raise
    except KeyboardInterrupt:
        print("\nInterrompu : relancez la même commande, le rendu reprend image par image.", file=sys.stderr)
        return EXIT_FAIL
    except Exception as exc:  # noqa: BLE001 — frontière du CLI : tout échec devient un message + code 1
        traceback.print_exc()
        print(f"\nÉCHEC : {exc}", file=sys.stderr)
        return EXIT_FAIL


if __name__ == "__main__":
    sys.exit(main())
