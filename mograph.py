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
    preview      <scène>   aperçu en direct dans le navigateur (rechargé à chaque sauvegarde)
    new          <id>      nouvelle scène depuis un modèle (templates/) : --template titre
    batch        <scènes>  rend plusieurs scènes, ou une scène modèle × un CSV (--data)
    beats        <audio>   détecte tempo, premier temps et drop d'une musique (--scene … --write)
    sheets       <scène>   planches contact (out/<scène>/planche_contact.png)

Options utiles : validate --layout (zones sûres vérifiées sur quelques images, avant le rendu) ;
all/render --draft (préversion rapide : 1080p, plaques 3D réduites, sans flou, sans QC).

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


def draft_scene(raw: dict) -> dict:
    """Préversion rapide : id « <id>_draft » (build/ et out/ séparés du rendu final), 1080p, plaques 3D
    à mi-résolution et 16 échantillons, sans flou de mouvement, un seul livrable HEVC (plus le 4444
    exigé par une scène alpha). Les pixels diffèrent du rendu final : jamais livrable."""
    import copy

    d = copy.deepcopy(raw)
    d["id"] = f"{d['id']}_draft"[:64]
    d["title"] = f"{d.get('title', d['id'])} (préversion)"
    d.setdefault("format", {})["resolution"] = "1080p"
    q = d.setdefault("quality", {})
    q["plate_scale"] = min(0.5, float(q.get("plate_scale", 1)))
    q["blender_samples"] = min(16, int(q.get("blender_samples", 16)))
    q["motion_blur"] = {"enabled": False}
    if d["format"].get("alpha"):
        d["outputs"] = [{"profile": "prores_4444"}, {"profile": "hevc_main10", "crf": 23, "suffix": "_flat"}]
    else:
        d["outputs"] = [{"profile": "hevc_main10", "crf": 23}]
    return d


def compile_or_exit(scene_arg, *, draft: bool = False) -> dict:
    """Compile la scène (chemin, id ou dict) ; en cas d'erreur, affiche les messages et quitte avec 1."""
    from pipeline import scene as scene_mod

    if isinstance(scene_arg, dict):
        source, label = scene_arg, scene_arg.get("id", "(scène)")
    else:
        path = resolve_scene_path(scene_arg)
        source, label = path, path
        if draft:
            source = json.loads(path.read_text(encoding="utf-8"))
    if draft:
        source = draft_scene(source)
    try:
        compiled = scene_mod.compile_scene(source)
    except scene_mod.SceneError as exc:
        print(f"SCÈNE REFUSÉE : {label}", file=sys.stderr)
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


def step_plates_and_web(compiled: dict, workers: int | None, *, plates: bool = True) -> None:
    """Plaques 3D (Blender) et images web EN PARALLÈLE quand c'est possible.

    Les plans sans plaque ne dépendent pas de Blender : ils sont rendus pendant que Blender calcule
    (avec un GPU, la 2D devient gratuite ; sans GPU, le temps total est au pire inchangé). Les plans
    qui affichent une plaque attendent qu'elle soit complète. Résultat identique au bit près à
    l'enchaînement séquentiel : chaque image ne dépend que de sa définition compilée.
    """
    from pipeline import web_render

    with_plates = [i for i, s in enumerate(compiled["shots"]) if s.get("plates")]
    if not plates or not with_plates:
        if plates:
            step_plates(compiled)
        step_web(compiled, workers)
        return
    import threading

    independent = [i for i in range(len(compiled["shots"])) if i not in with_plates]
    failure: list[BaseException] = []

    def blender() -> None:
        try:
            step_plates(compiled)
        except BaseException as exc:  # noqa: BLE001 — relayée au fil principal après join
            failure.append(exc)

    th = threading.Thread(target=blender, name="plaques-blender", daemon=True)
    th.start()
    if independent:
        _banner(f"IMAGES WEB pendant les plaques 3D ({len(independent)} plan(s) sans plaque)")
        for i in independent:
            web_render.render_shot(compiled, i, workers=workers, log=_log)
    th.join()
    if failure:
        raise failure[0]
    _banner(f"IMAGES WEB des plans à plaque 3D ({len(with_plates)} plan(s))")
    for i in with_plates:
        web_render.render_shot(compiled, i, workers=workers, log=_log)


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


def step_sheets(compiled: dict) -> None:
    from pipeline import sheets

    try:
        sheets.write_contact_sheets(compiled, log=_log)
    except sheets.SheetError as exc:
        _log(f"Planche contact non produite : {exc}")


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
    if not args.layout:
        return EXIT_OK
    return layout_check(compiled, args.workers)


def layout_check(compiled: dict, workers: int | None) -> int:
    """Rend uniquement les images de relevé (5 par seconde) puis contrôle les zones sûres AVANT le rendu
    complet. Les images rendues servent ensuite au rendu (reprise) : aucun travail perdu."""
    from pipeline import qc, web_render

    _banner("MISE EN PAGE (relevés des zones sûres)")
    skipped = []
    for i, s in enumerate(compiled["shots"]):
        if any(not (Path(j["out_dir"]).is_dir() and len(list(Path(j["out_dir"]).glob("[0-9]" * 6 + ".png"))) >= j["frames"])
               for j in s.get("plates") or []):
            skipped.append(s["id"])
            continue
        frames = sorted(web_render._layout_frames(s["spec"]))
        web_render.render_shot(compiled, i, workers=workers, frames=frames, log=_log)
    if skipped:
        _log(f"Plans non contrôlés (plaque 3D pas encore rendue) : {', '.join(skipped)} — lancez « plates » d'abord.")
        compiled = dict(compiled, shots=[s for s in compiled["shots"] if s["id"] not in skipped])
        if not compiled["shots"]:
            return EXIT_OK
    rep = qc.preflight_layout(compiled)
    if rep["zone"] == "off":
        print("Contrôle des zones sûres désactivé par la scène (qc.safe_zone = off).")
        return EXIT_OK
    print(f"\nZone sûre « {rep['zone']} » : {rep['rect']} (px logiques), {rep['records']} relevés de texte.")
    for e in rep["blocking"]:
        print(f"  HORS ZONE  « {e['text']} » (plan {e['shot_id']}, calque {e['id']}) : {e['seconds']:.2f} s, "
              f"dépassement {e['max_overflow_px']:.1f} px à l'image {e['worst_frame']}")
    for e in [x for x in rep["excursions"] if x not in rep["blocking"]]:
        print(f"  bref       « {e['text']} » (plan {e['shot_id']}, calque {e['id']}) : {e['seconds']:.2f} s toléré")
    for m in rep["near"]:
        print(f"  PRÈS DU BORD « {m['text']} » (plan {m['shot_id']}, calque {m['id']}) : marge {m['margin_px']:.1f} px "
              f"< {rep['margin_px']:.1f} px (image {m['frame']}) : risque de débord sur une autre machine")
    if rep["ok"] and not rep["near"]:
        print("  Tous les textes sont dans la zone sûre avec une marge confortable.")
    print("VERDICT MISE EN PAGE : " + ("CONFORME" if rep["ok"] else "NON CONFORME (le QC final échouera)"))
    return EXIT_OK if rep["ok"] else EXIT_FAIL


def cmd_plates(args) -> int:
    if args.detach:
        return detach(sys.argv[1:], _scene_id_from_arg(args.scene), "plates")
    compiled = compile_or_exit(args.scene)
    step_plates(compiled)
    return EXIT_OK


def cmd_render(args) -> int:
    if args.detach:
        return detach(sys.argv[1:], _scene_id_from_arg(args.scene), "render")
    compiled = compile_or_exit(args.scene, draft=args.draft)
    step_audio(compiled)
    step_plates_and_web(compiled, args.workers, plates=not args.no_plates)
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


def run_all(scene, *, workers: int | None = None, count: int | None = None, draft: bool = False) -> int:
    """Pipeline complet d'une scène (chemin, id ou dict) ; renvoie le code de sortie."""
    t0 = time.perf_counter()
    compiled = compile_or_exit(scene, draft=draft)
    from pipeline import scene as scene_mod

    _banner(f"SCÈNE {compiled['scene_id']}" + ("  (PRÉVERSION)" if draft else ""))
    print(scene_mod.summary(compiled), flush=True)
    step_audio(compiled)
    step_plates_and_web(compiled, workers)
    if not draft and not step_determinism(compiled, count):
        # Porte bloquante (SPEC étape 19) : aucun livrable n'est produit si le rendu n'est pas reproductible.
        return EXIT_FAIL
    step_compose(compiled)
    step_encode(compiled)
    step_sheets(compiled)
    if draft:
        _log("Préversion : ni porte de déterminisme ni QC (jamais livrable) ; rendu final : sans --draft.")
        _log(f"Durée totale : {time.perf_counter() - t0:.1f} s")
        return EXIT_OK
    versions = write_versions(compiled["scene_id"])
    _log(f"Versions archivées : {versions}")
    ok = step_qc(compiled)
    _log(f"Durée totale : {time.perf_counter() - t0:.1f} s")
    return EXIT_OK if ok else EXIT_FAIL


def cmd_all(args) -> int:
    if args.detach:
        return detach(sys.argv[1:], _scene_id_from_arg(args.scene), "all")
    return run_all(args.scene, workers=args.workers, count=args.count, draft=args.draft)


def cmd_sheets(args) -> int:
    from pipeline import sheets

    compiled = compile_or_exit(args.scene)
    try:
        for p in sheets.write_contact_sheets(compiled, log=_log):
            print(p)
    except sheets.SheetError as exc:
        print(f"ÉCHEC : {exc}", file=sys.stderr)
        return EXIT_FAIL
    return EXIT_OK


def cmd_preview(args) -> int:
    from pipeline import preview

    preview.serve(resolve_scene_path(args.scene), port=args.port, open_browser=not args.no_open, log=_log)
    return EXIT_OK


def cmd_new(args) -> int:
    templates = sorted(config.TEMPLATES_DIR.glob("*.json"))
    if args.list or not args.id:
        print("Modèles disponibles (templates/) :")
        for t in templates:
            data = json.loads(t.read_text(encoding="utf-8"))
            fmt = data.get("format", {})
            print(f"  {t.stem:<14} {fmt.get('aspect', '?'):>5} {data.get('preset', '?'):<15} {data.get('brief', '')}")
        print("\nUsage : python mograph.py new <id> --template <modèle>")
        return EXIT_OK if args.list else EXIT_FAIL
    from pipeline import scene as scene_mod

    names = {t.stem: t for t in templates}
    if args.template not in names:
        print(f"Modèle « {args.template} » inconnu ; modèles : {', '.join(names)}.", file=sys.stderr)
        return EXIT_FAIL
    import re

    if not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]|_(?!_))*", args.id) or len(args.id) > 64:
        print(f"Id « {args.id} » invalide : minuscules, chiffres, « - » et « _ » (jamais « __ »), 64 caractères au plus.",
              file=sys.stderr)
        return EXIT_FAIL
    dest = config.SCENES_DIR / f"{args.id}.json"
    if dest.exists() and not args.force:
        print(f"{dest} existe déjà : choisissez un autre id ou ajoutez --force.", file=sys.stderr)
        return EXIT_FAIL
    data = json.loads(names[args.template].read_text(encoding="utf-8"))
    data["id"] = args.id
    if args.title:
        data["title"] = args.title
    dest.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    try:
        scene_mod.validate_scene(data)
    except scene_mod.SceneError as exc:
        print(f"Attention, la scène créée est refusée :\n{exc}", file=sys.stderr)
        return EXIT_FAIL
    print(f"Scène créée : {dest}")
    print(f"  1. modifiez les textes (VS Code : autocomplétion par le schéma) ;")
    print(f"  2. aperçu en direct   : python mograph.py preview {args.id}")
    print(f"  3. préversion rapide  : python mograph.py all {args.id} --draft")
    print(f"  4. rendu final        : python mograph.py all {args.id}")
    return EXIT_OK


def cmd_batch(args) -> int:
    from pipeline import batch

    if args.detach:
        return detach(sys.argv[1:], "_batch", "batch")
    try:
        jobs = batch.expand(args.scenes, data=args.data)
    except batch.BatchError as exc:
        print(f"ÉCHEC : {exc}", file=sys.stderr)
        return EXIT_FAIL
    _banner(f"LOT : {len(jobs)} scène(s)" + ("  (PRÉVERSIONS)" if args.draft else ""))
    results = []
    for label, scene in jobs:
        t0 = time.perf_counter()
        try:
            code = run_all(scene, workers=args.workers, count=args.count, draft=args.draft)
        except SystemExit as exc:  # scène refusée par le compilateur : on continue le lot
            code = int(exc.code or EXIT_FAIL)
        except Exception as exc:  # noqa: BLE001 — une scène en échec n'arrête pas le lot
            traceback.print_exc()
            _log(f"ÉCHEC de {label} : {exc}")
            code = EXIT_FAIL
        results.append((label, code, time.perf_counter() - t0))
    report = batch.write_report(results, draft=args.draft)
    _banner("BILAN DU LOT")
    for label, code, dt in results:
        print(f"  {'OK   ' if code == 0 else 'ÉCHEC'}  {label:<40} {dt:7.1f} s")
    print(f"Rapport : {report}")
    return EXIT_OK if all(code == 0 for _, code, _ in results) else EXIT_FAIL


def cmd_beats(args) -> int:
    from pipeline import audio

    src = Path(args.audio)
    if not src.is_absolute():
        src = (config.ROOT / src) if (config.ROOT / src).exists() else src.resolve()
    if not src.is_file():
        print(f"Fichier audio introuvable : {src}", file=sys.stderr)
        return EXIT_FAIL
    r = audio.analyse_tempo(src, bpm_min=args.bpm_min, bpm_max=args.bpm_max, beats_per_bar=args.beats_per_bar)
    print(f"{src.name} : {r['bpm']} BPM, premier temps fort à {r['offset']} s, mesure à {r['beats_per_bar']} temps, "
          f"confiance {r['confidence']:.0%}, durée {r['duration']} s"
          + (f", drop à {r['drop']} s" if r["drop"] is not None else ", pas de drop net"))
    if r["confidence"] < 0.6:
        print("  Confiance faible : tempo variable ou peu marqué ; vérifiez à l'oreille dans l'aperçu (preview).")
    try:
        rel = src.resolve().relative_to(config.ROOT).as_posix()
    except ValueError:
        rel = str(src)
    block = {"src": rel, "bpm": r["bpm"], "offset": r["offset"], "beats_per_bar": r["beats_per_bar"]}
    if r["drop"] is not None:
        block["markers"] = [{"id": "drop", "time": r["drop"]}]
    if args.scene and args.write:
        path = resolve_scene_path(args.scene)
        data = json.loads(path.read_text(encoding="utf-8"))
        old = data.get("audio") or {}
        markers = [m for m in old.get("markers", []) if m.get("id") != "drop"] + block.get("markers", [])
        new = {**{k: v for k, v in old.items() if k != "synth"}, **block}
        if markers:
            new["markers"] = markers
        data["audio"] = new
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"Bloc audio écrit dans {path} (vérifiez avec : python mograph.py validate {path.stem}).")
    else:
        print('Bloc à mettre dans la scène :\n"audio": ' + json.dumps(block, ensure_ascii=False, indent=2))
    return EXIT_OK


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

    v = scene_cmd("validate", cmd_validate, "valider et compiler une scène", workers=True)
    v.add_argument("--layout", action="store_true",
                   help="rendre les images de relevé et contrôler les zones sûres avant le rendu complet")
    scene_cmd("plates", cmd_plates, "rendre les plaques 3D (Blender)", detachable=True)
    r = scene_cmd("render", cmd_render, "plaques 3D + images web", detachable=True, workers=True)
    r.add_argument("--no-plates", action="store_true", help="ne pas (re)lancer Blender")
    r.add_argument("--draft", action="store_true", help="préversion rapide (id <scène>_draft)")
    scene_cmd("determinism", cmd_determinism, "porte de déterminisme", count=True)
    scene_cmd("compose", cmd_compose, "reconstruire la séquence maître")
    scene_cmd("encode", cmd_encode, "séquence maître + encodage des livrables")
    scene_cmd("qc", cmd_qc, "contrôle qualité automatique")
    a_ = scene_cmd("all", cmd_all, "pipeline complet", detachable=True, workers=True, count=True)
    a_.add_argument("--draft", action="store_true",
                    help="préversion rapide : 1080p, plaques 3D réduites, sans flou, sans déterminisme ni QC")
    scene_cmd("sheets", cmd_sheets, "planches contact de la séquence maître")
    pv = scene_cmd("preview", cmd_preview, "aperçu en direct dans le navigateur")
    pv.add_argument("--port", type=int, default=8765, help="port local (défaut 8765 ; 0 = libre)")
    pv.add_argument("--no-open", action="store_true", help="ne pas ouvrir le navigateur")

    nw = sub.add_parser("new", help="nouvelle scène depuis un modèle")
    nw.add_argument("id", nargs="?", help="id de la nouvelle scène (scenes/<id>.json)")
    nw.add_argument("--template", "-t", default="titre", help="modèle de templates/ (défaut : titre)")
    nw.add_argument("--title", help="titre de la scène")
    nw.add_argument("--list", action="store_true", help="lister les modèles")
    nw.add_argument("--force", action="store_true", help="écraser une scène existante")
    nw.set_defaults(func=cmd_new)

    bt = sub.add_parser("batch", help="rendre plusieurs scènes, ou déclinaisons d'un modèle × CSV")
    bt.add_argument("scenes", nargs="+", help="scènes (chemins, ids ou motifs « scenes/promo_*.json »)")
    bt.add_argument("--data", help="CSV de déclinaisons : {{colonne}} remplacé dans la scène modèle, une vidéo par ligne")
    bt.add_argument("--draft", action="store_true", help="préversions rapides")
    bt.add_argument("--workers", type=int, default=None)
    bt.add_argument("--count", type=int, default=None)
    bt.add_argument("--detach", action="store_true", help="processus détaché, journal dans build/_batch/logs/")
    bt.set_defaults(func=cmd_batch)

    be = sub.add_parser("beats", help="détecter tempo, premier temps et drop d'une musique")
    be.add_argument("audio", help="fichier audio (wav, mp3, m4a…) ou vidéo")
    be.add_argument("--bpm-min", type=float, default=60.0)
    be.add_argument("--bpm-max", type=float, default=200.0)
    be.add_argument("--beats-per-bar", type=int, default=4)
    be.add_argument("--scene", help="scène dont remplacer le bloc audio (avec --write)")
    be.add_argument("--write", action="store_true", help="écrire le bloc audio dans --scene")
    be.set_defaults(func=cmd_beats)
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
