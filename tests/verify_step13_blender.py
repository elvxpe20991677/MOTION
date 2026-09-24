"""Vérification de l'étape 13 : backend Blender (bpy 5.0.1) + lanceur pipeline/blender_render.py.

Lancement depuis la racine :  .venv/Scripts/python.exe tests/verify_step13_blender.py
Affiche une ligne PASS/FAIL par contrôle (INFO/SKIP pour les mesures), code de sortie ≠ 0 si un FAIL.
Fichiers temporaires : build/_tests/step13/ uniquement (vidé au début de chaque exécution).

Contrôles :
  1. fixtures conformes au schéma          6. reproductibilité CPU (≤ 1/65535 exigé) et OptiX (rapportée)
  2. valeurs animées évaluées (z sphère)   7. reprise : images présentes sautées, image tronquée refaite
  3. look réellement appliqué              8. look inexistant => échec explicite (backend et lanceur)
  4. rendu CPU de 3 images isolées          9. render_plate : tâche invalide, plaque incomplète, rendu réel,
  5. rendu OptiX + temps par image             render_scene_plates
 10. fixture « kit » : toutes primitives, ciel physique, lumières SUN/POINT/SPOT, DOF, alpha
 11. aperçus 8 bits à regarder (build/_tests/step13/preview/)
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import cv2  # noqa: E402
import numpy as np  # noqa: E402

from pipeline import blender_render as br  # noqa: E402
from pipeline import config  # noqa: E402

TEST_DIR: Path = config.BUILD_DIR / "_tests" / "step13"
FIXTURES = ROOT / "tests" / "fixtures"
CLAY = FIXTURES / "job_clay_drop.json"
KIT = FIXTURES / "job_kit_primitives.json"
TOL = 1e-3
# Un rendu 540² à 24 échantillons ne doit jamais prendre plusieurs minutes : garde-fou contre un blocage.
BACKEND_TIMEOUT_S = 900

results: list[tuple[str, bool]] = []


# ---------------------------------------------------------------------------------------------------
# Outils
# ---------------------------------------------------------------------------------------------------

def check(label: str, ok: bool, detail: str = "") -> bool:
    results.append((label, bool(ok)))
    print(f"{'PASS' if ok else 'FAIL'} {label}" + (f" — {detail}" if detail else ""), flush=True)
    return bool(ok)


def info(message: str) -> None:
    print(f"INFO {message}", flush=True)


def skip(label: str, why: str) -> None:
    print(f"SKIP {label} — {why}", flush=True)


def write_json_atomic(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def variant(base: dict, out_name: str, **render) -> dict:
    """Copie de la fixture avec un dossier de sortie de test et des réglages de rendu remplacés."""
    job = copy.deepcopy(base)
    job["out_dir"] = str(TEST_DIR / out_name)
    job["render"].update(render)
    return job


def run_backend(job: dict, name: str, *args: str) -> tuple[int, str, str, float]:
    """Lance le backend directement (sans le lanceur) ; renvoie (code, stdout, stderr, durée murale)."""
    job_path = TEST_DIR / "jobs" / f"{name}.json"
    write_json_atomic(job_path, job)
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1")
    t0 = time.perf_counter()
    proc = subprocess.run(
        [str(config.BLENDER_PYTHON), str(config.BLENDER_BACKEND), str(job_path), *args],
        cwd=str(ROOT), env=env, capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=BACKEND_TIMEOUT_S,
    )
    return proc.returncode, proc.stdout, proc.stderr, time.perf_counter() - t0


def progress(stdout: str) -> list[str]:
    return [m.group(1) for m in re.finditer(r"^MOGRAPH_BLENDER image (\d+/\d+)$", stdout, re.M)]


def frame_times(stdout: str) -> dict[int, float]:
    return {int(m.group(1)): float(m.group(2))
            for m in re.finditer(r"^MOGRAPH_BLENDER temps image (\d+) : ([\d.]+) s$", stdout, re.M)}


def values(stdout: str) -> dict[tuple[int, str], dict]:
    out = {}
    for line in stdout.splitlines():
        if line.startswith("MOGRAPH_VALUES "):
            rec = json.loads(line[len("MOGRAPH_VALUES "):])
            out[(rec["frame"], rec["object"])] = rec
    return out


def read16(path: Path) -> np.ndarray:
    img = cv2.imdecode(np.fromfile(str(path), dtype=np.uint8), cv2.IMREAD_UNCHANGED)
    if img is None:
        raise RuntimeError(f"image indécodable : {path}")
    return img


def pixel_sha(path: Path) -> str:
    # Même empreinte que manifests.schema.json : SHA-256 du tableau BGRA contigu (uint16 little-endian).
    img = np.ascontiguousarray(read16(path))
    return hashlib.sha256(img.astype(img.dtype.newbyteorder("<"), copy=False).tobytes()).hexdigest()


def max_diff(a: Path, b: Path) -> int:
    return int(np.abs(read16(a).astype(np.int32) - read16(b).astype(np.int32)).max())


def tail(text: str, n: int = 12) -> str:
    return "\n".join("    | " + t for t in text.strip().splitlines()[-n:])


def to_preview(path: Path) -> np.ndarray:
    """RGBA 16 bits -> BGR 8 bits composé sur damier (l'alpha éventuel devient visible)."""
    img = read16(path).astype(np.float32) / 65535.0
    h, w = img.shape[:2]
    yy, xx = np.mgrid[0:h, 0:w]
    checker = ((((yy // 12) + (xx // 12)) % 2) * 0.25 + 0.5)[..., None]
    alpha = img[..., 3:4]
    return np.round((img[..., :3] * alpha + checker * (1.0 - alpha)) * 255.0).astype(np.uint8)


def save_png(path: Path, img: np.ndarray) -> None:
    ok, buf = cv2.imencode(".png", img)
    if not ok:
        raise RuntimeError(f"encodage PNG impossible : {path}")
    tmp = path.with_name(path.name + ".tmp")
    buf.tofile(str(tmp))
    os.replace(tmp, path)


# ---------------------------------------------------------------------------------------------------
# Contrôles
# ---------------------------------------------------------------------------------------------------

def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    # Nettoyage limité au dossier de test de l'étape (garde-fou : jamais hors de build/_tests/).
    assert TEST_DIR.parent.name == "_tests" and TEST_DIR.name == "step13"
    if TEST_DIR.exists():
        shutil.rmtree(TEST_DIR)
    (TEST_DIR / "preview").mkdir(parents=True)
    clay = load(CLAY)
    kit = load(KIT)
    measures: dict[str, str] = {}

    # 1. Fixtures conformes au schéma normatif --------------------------------------------------
    for path, job in ((CLAY, clay), (KIT, kit)):
        # out_dir doit être ABSOLU (schéma), donc propre à la machine qui a écrit la fixture
        # (« C:/… » n'est pas absolu sous Linux) : on le reloge sous build/_tests avant validation.
        job = dict(job, out_dir=str(TEST_DIR / f"fixture_{path.stem.removeprefix('job_')}"))
        try:
            br.validate_job(job)
            check(f"schéma : {path.name} conforme à blender_job.schema.json", True)
        except br.BlenderRenderError as exc:
            check(f"schéma : {path.name} conforme à blender_job.schema.json", False, str(exc))

    # 2 + 3. Valeurs animées évaluées et look appliqué (sans rendu) ------------------------------
    job_vals = variant(clay, "values")
    code, out, err, dt = run_backend(job_vals, "values", "--frames", "0,12,15,20,23", "--print-values")
    check("valeurs : backend --print-values sort avec le code 0", code == 0, f"{dt:.2f} s" + ("" if code == 0 else "\n" + tail(err)))
    vals = values(out)
    info("valeurs évaluées après frame_set (sphère « ball ») :")
    for f in (0, 12, 15, 20):
        rec = vals.get((f, "ball"))
        if rec:
            print(f"       image {f:2d} : location {rec['location']}  scale {rec['scale']}")
    expected_z = {0: 3.9, 12: 0.5, 15: 0.36, 20: 0.5}
    got_z = {f: vals.get((f, "ball"), {}).get("location", [None] * 3)[2] for f in expected_z}
    check("valeurs : z de la sphère = 3,9 / 0,5 / 0,36 / 0,5 aux images 0 / 12 / 15 / 20 (±1e-3)",
          all(got_z[f] is not None and abs(got_z[f] - z) <= TOL for f, z in expected_z.items()), f"relu {got_z}")
    sz = vals.get((15, "ball"), {}).get("scale")
    check("valeurs : écrasement à l'image 15, scale = [1,18 ; 1,18 ; 0,72]",
          sz is not None and all(abs(a - b) <= TOL for a, b in zip(sz, (1.18, 1.18, 0.72))), f"relu {sz}")
    r0 = vals.get((0, "ring"), {}).get("rotation")
    r23 = vals.get((23, "ring"), {}).get("rotation")
    check("valeurs : rotation du tore en degrés (35° avant la clé 2,5 ; 125° après la clé 21,5)",
          r0 is not None and r23 is not None and abs(r0[2] - 35) <= TOL and abs(r23[2] - 125) <= TOL and abs(r0[0] - 90) <= TOL,
          f"image 0 {r0}, image 23 {r23}")
    c0 = vals.get((0, "camera"), {}).get("location")
    c23 = vals.get((23, "camera"), {}).get("location")
    check("valeurs : travelling caméra [1,2 ; -6,9 ; 2,4] -> [-0,9 ; -6,4 ; 2,0]",
          c0 is not None and c23 is not None and all(abs(a - b) <= TOL for a, b in zip(c0 + c23, (1.2, -6.9, 2.4, -0.9, -6.4, 2.0))),
          f"image 0 {c0}, image 23 {c23}")
    m = re.search(r"^MOGRAPH_BLENDER look appliqué : (.+?) \(vue", out, re.M)
    applied = m.group(1) if m else None
    info(f"look demandé « {clay['render']['look']} », look réellement appliqué : « {applied} »")
    check("look : « Base Contrast » résolu en « AgX - Base Contrast » et imprimé", applied == "AgX - Base Contrast")

    # 4. Rendu CPU de 3 images isolées ------------------------------------------------------------
    job_cpu = variant(clay, "cpu_a", device="CPU")
    code, out_cpu, err, wall = run_backend(job_cpu, "cpu_a", "--frames", "0,12,15")
    check("CPU : rendu --frames 0,12,15 (code 0)", code == 0, "" if code == 0 else tail(err))
    check("CPU : lignes de progression exactes « MOGRAPH_BLENDER image i/24 »",
          progress(out_cpu) == ["0/24", "12/24", "15/24"], f"relu {progress(out_cpu)}")
    cpu_dir = Path(job_cpu["out_dir"])
    problems = {i: br._frame_problem(cpu_dir / f"{i:06d}.png", 540, 540) for i in (0, 12, 15)}
    check("CPU : 3 PNG RGBA 16 bits 540x540 décodables", all(p is None for p in problems.values()), str(problems))
    check("CPU : aucune image hors demande ni fichier .tmp résiduel",
          sorted(p.name for p in cpu_dir.iterdir() if p.suffix in (".png", ".tmp")) == ["000000.png", "000012.png", "000015.png"])
    if all(p is None for p in problems.values()):
        a0, a12 = read16(cpu_dir / "000000.png"), read16(cpu_dir / "000012.png")
        check("CPU : alpha opaque partout (transparent = false)", int(a0[..., 3].min()) == 65535 and int(a12[..., 3].min()) == 65535)
        moved = float(np.abs(a0[..., :3].astype(np.int32) - a12[..., :3].astype(np.int32)).mean())
        check("CPU : l'image 12 diffère de l'image 0 (la sphère a bougé)", moved > 200, f"écart moyen {moved:.0f}/65535")
    t_cpu = frame_times(out_cpu)
    info(f"temps CPU par image ({os.cpu_count()} threads, 540², 24 éch., flou 0,5) : {t_cpu} ; processus complet {wall:.2f} s")
    measures["cpu"] = f"{t_cpu} (processus {wall:.2f} s)"

    # 5. Rendu OptiX ------------------------------------------------------------------------------
    job_gpu = variant(clay, "optix_a", device="OPTIX")
    code, out_gpu, err_gpu, wall_gpu = run_backend(job_gpu, "optix_a", "--frames", "0,12,15")
    optix_ok = code == 0
    if not optix_ok and "aucun GPU" in err_gpu:
        skip("OptiX : rendu", "aucun GPU OptiX sur cette machine")
    else:
        check("OptiX : rendu --frames 0,12,15 (code 0)", optix_ok, "" if optix_ok else tail(err_gpu))
    if optix_ok:
        gpu_dir = Path(job_gpu["out_dir"])
        check("OptiX : 3 PNG RGBA 16 bits 540x540 décodables",
              all(br._frame_problem(gpu_dir / f"{i:06d}.png", 540, 540) is None for i in (0, 12, 15)))
        dev = re.search(r"^MOGRAPH_BLENDER périphérique : (.+)$", out_gpu, re.M)
        info(f"périphérique GPU : {dev.group(1) if dev else '?'}")
        t_gpu = frame_times(out_gpu)
        info(f"temps OptiX par image : {t_gpu} ; processus complet {wall_gpu:.2f} s")
        measures["optix"] = f"{t_gpu} (processus {wall_gpu:.2f} s)"

    # 6. Déterminisme : même image rendue deux fois, processus neufs, image rendue seule ----------
    job_cpu_b = variant(clay, "cpu_b", device="CPU")
    code, out, err, _ = run_backend(job_cpu_b, "cpu_b", "--frames", "12")
    if check("déterminisme CPU : second rendu de l'image 12 (processus neuf, image seule)", code == 0, "" if code == 0 else tail(err)):
        a, b = cpu_dir / "000012.png", Path(job_cpu_b["out_dir"]) / "000012.png"
        sa, sb = pixel_sha(a), pixel_sha(b)
        info(f"SHA-256 pixels CPU : {sa[:16]}… / {sb[:16]}… ; écart max {max_diff(a, b)}")
        measures["det_cpu"] = "bit-exact" if sa == sb else f"NON bit-exact (écart max {max_diff(a, b)}/65535)"
        # Cycles CPU somme des flottants en multithread : mesuré ±1/65535 sur quelques pixels (OIDN l'étend
        # à plus de pixels, toujours ±1). La garantie exigée est donc ≤ 1 niveau 16 bits, soit < 1/257 de niveau
        # 8 bits : invisible après la composition 8 bits. La porte de déterminisme (étape 19) porte, elle, sur les
        # images web recomposées à partir des MÊMES fichiers de plaque, et reste exigée au bit près.
        check("reproductibilité CPU : image 12 re-rendue à ≤ 1/65535 près", max_diff(a, b) <= 1, measures["det_cpu"])
    if optix_ok:
        job_gpu_b = variant(clay, "optix_b", device="OPTIX")
        code, out, err, _ = run_backend(job_gpu_b, "optix_b", "--frames", "12")
        check("déterminisme OptiX : second rendu de l'image 12", code == 0, "" if code == 0 else tail(err))
        if code == 0:
            a, b = Path(job_gpu["out_dir"]) / "000012.png", Path(job_gpu_b["out_dir"]) / "000012.png"
            sa, sb = pixel_sha(a), pixel_sha(b)
            measures["det_optix"] = "bit-exact" if sa == sb else f"NON bit-exact (écart max {max_diff(a, b)}/65535)"
            info(f"SHA-256 pixels OptiX : {sa[:16]}… / {sb[:16]}… -> {measures['det_optix']}")
            cross = max_diff(cpu_dir / "000012.png", a)
            info(f"écart max CPU vs OptiX sur l'image 12 : {cross}/65535 (moteurs différents : identité non attendue)")
            measures["cpu_vs_optix"] = f"écart max {cross}/65535"

    # 7. Reprise ------------------------------------------------------------------------------------
    before = {i: ((cpu_dir / f"{i:06d}.png").stat().st_mtime_ns, pixel_sha(cpu_dir / f"{i:06d}.png")) for i in (0, 12, 15)}
    code, out, err, wall_skip = run_backend(job_cpu, "cpu_a_resume", "--frames", "0,12,15")
    after = {i: ((cpu_dir / f"{i:06d}.png").stat().st_mtime_ns, pixel_sha(cpu_dir / f"{i:06d}.png")) for i in (0, 12, 15)}
    check("reprise : relance -> aucune image re-rendue, fichiers inchangés",
          code == 0 and progress(out) == [] and before == after and "3 déjà présente(s)" in out, f"{wall_skip:.2f} s")
    victim = cpu_dir / "000015.png"
    original = cpu_dir.parent / "frame15_original.png"
    shutil.copyfile(victim, original)  # copie de référence : l'image va être volontairement tronquée
    data = victim.read_bytes()
    victim.write_bytes(data[: len(data) // 2])  # simule un arrêt brutal pendant l'écriture
    code, out, err, _ = run_backend(job_cpu, "cpu_a_resume2", "--frames", "0,12,15")
    check("reprise : image tronquée détectée et seule re-rendue",
          code == 0 and progress(out) == ["15/24"] and br._frame_problem(victim, 540, 540) is None, f"relu {progress(out)}")
    # Même tolérance que la reproductibilité CPU (±1/65535) : la reprise ne change pas l'image visible.
    ecart = max_diff(victim, original)
    check("reprise : l'image refaite est identique à l'originale à ≤ 1/65535 près", ecart <= 1,
          "bit-exact" if pixel_sha(victim) == before[15][1] else f"écart max {ecart}/65535 (Cycles CPU multithread)")

    # 8. Look inexistant => échec explicite ------------------------------------------------------
    job_badlook = variant(clay, "bad_look", look="Look Imaginaire")
    code, out, err, _ = run_backend(job_badlook, "bad_look", "--print-values", "--frames", "0")
    msg = next((l for l in err.splitlines() if "ERREUR" in l), "")
    check("look inexistant : backend en échec (code ≠ 0) avec message explicite",
          code != 0 and "introuvable" in msg and "'AgX - Look Imaginaire'" in msg, f"code {code} : {msg[:220]}")
    try:
        br.render_plate(variant(clay, "bad_look_plate", look="Look Imaginaire", device="CPU"), log=lambda s: None, frames=[0])
        check("look inexistant : render_plate lève BlenderRenderError", False, "aucune exception")
    except br.BlenderRenderError as exc:
        check("look inexistant : render_plate lève BlenderRenderError", "introuvable" in str(exc) and "code 2" in str(exc))

    # 9. Lanceur render_plate -------------------------------------------------------------------------
    bad = variant(clay, "invalid")
    del bad["render"]["samples"]
    bad["frames"] = 0
    try:
        br.render_plate(bad, log=lambda s: None)
        check("render_plate : tâche non conforme refusée avant lancement", False, "aucune exception")
    except br.BlenderRenderError as exc:
        check("render_plate : tâche non conforme refusée avant lancement",
              "samples" in str(exc) and "frames" in str(exc) and not Path(bad["out_dir"]).exists(), str(exc).splitlines()[0])

    stub = TEST_DIR / "stub_backend.py"
    stub.write_text(
        "# Faux backend : écrit une seule image 8 bits 16x16 puis sort avec 0 (simule un rendu incomplet).\n"
        "import json, struct, sys, zlib, pathlib\n"
        "job = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding='utf-8'))\n"
        "def chunk(t, d):\n"
        "    return struct.pack('>I', len(d)) + t + d + struct.pack('>I', zlib.crc32(t + d) & 0xFFFFFFFF)\n"
        "raw = b''.join(b'\\x00' + b'\\x80\\x80\\x80\\xff' * 16 for _ in range(16))\n"
        "png = b'\\x89PNG\\r\\n\\x1a\\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', 16, 16, 8, 6, 0, 0, 0))"
        " + chunk(b'IDAT', zlib.compress(raw)) + chunk(b'IEND', b'')\n"
        "pathlib.Path(job['out_dir'], '000000.png').write_bytes(png)\n"
        "print('MOGRAPH_BLENDER image 0/' + str(job['frames']), flush=True)\n",
        encoding="utf-8",
    )
    incomplete = variant(kit, "incomplete", device="CPU")
    incomplete.update(frames=3, width=32, height=32)
    saved_backend = config.BLENDER_BACKEND
    try:
        config.BLENDER_BACKEND = stub
        br.render_plate(incomplete, log=lambda s: None)
        check("render_plate : plaque incomplète détectée", False, "aucune exception")
    except br.BlenderRenderError as exc:
        text = str(exc)
        check("render_plate : plaque incomplète détectée (image 8 bits 16x16 + 2 absentes)",
              "incomplète" in text and "000000.png : profondeur uint8" in text and "000001.png : absente" in text
              and "000002.png : absente" in text, text.splitlines()[0])
    finally:
        config.BLENDER_BACKEND = saved_backend

    small = variant(kit, "small_plate", device="CPU", samples=4)
    small.update(frames=3, width=96, height=54)
    logs: list[str] = []
    t0 = time.perf_counter()
    try:
        path = br.render_plate(small, log=logs.append)
        ok = path == Path(small["out_dir"]) and load(path / "job.json") == small
        check("render_plate : rendu réel complet, job.json écrit, chemin renvoyé", ok, f"{time.perf_counter() - t0:.2f} s")
        check("render_plate : progression relayée en direct",
              [l for l in logs if l.endswith("rendue")] == [f"[gallery__kit] image {i}/3 rendue" for i in range(3)])
        check("render_plate : journal blender.log écrit", "MOGRAPH_BLENDER image 2/3" in (path / "blender.log").read_text(encoding="utf-8"))
    except br.BlenderRenderError as exc:
        check("render_plate : rendu réel complet", False, str(exc))
    logs.clear()
    compiled = {"scene_id": "kit", "shots": [{"id": "gallery", "plates": [small]}, {"id": "vide", "plates": []}]}
    try:
        paths = br.render_scene_plates(compiled, log=logs.append)
        check("render_scene_plates : toutes les plaques (reprise sans re-rendu)",
              paths == [Path(small["out_dir"])] and not any(l.endswith("rendue") for l in logs)
              and any("3 déjà présente(s)" in l for l in logs))
    except br.BlenderRenderError as exc:
        check("render_scene_plates : toutes les plaques", False, str(exc))
    # Tâche modifiée à taille égale (échantillons) : les images périmées sont purgées puis refaites.
    changed = copy.deepcopy(small)
    changed["render"]["samples"] = 6
    logs.clear()
    try:
        br.render_plate(changed, log=logs.append)
        rerendered = [l for l in logs if l.endswith("rendue")]
        check("render_plate : tâche modifiée => images périmées purgées et refaites (pas de mélange)",
              len(rerendered) == 3 and any("périmée(s) supprimée(s)" in l for l in logs), f"{len(rerendered)} refaite(s)")
    except br.BlenderRenderError as exc:
        check("render_plate : tâche modifiée => purge", False, str(exc))

    # 10. Fixture « kit » : chemins de code restants -------------------------------------------------
    job_kit = variant(kit, "kit")
    code, out, err, wall_kit = run_backend(job_kit, "kit", "--frames", "0,11")
    check("kit : rendu (ciel, SUN/POINT/SPOT/AREA rectangle, 7 primitives, DOF, alpha)", code == 0,
          f"{wall_kit:.2f} s" if code == 0 else tail(err))
    check("kit : modèle de ciel choisi par introspection", "ciel physique : modèle MULTIPLE_SCATTERING" in out)
    check("kit : look « Medium High Contrast » résolu", "look appliqué : AgX - Medium High Contrast" in out)
    kit_dir = Path(job_kit["out_dir"])
    if code == 0:
        k0 = read16(kit_dir / "000000.png")
        check("kit : alpha réel (fond transparent et objets opaques)", int(k0[..., 3].min()) == 0 and int(k0[..., 3].max()) == 65535)
        k11 = read16(kit_dir / "000011.png")
        check("kit : l'image 11 diffère de l'image 0", float(np.abs(k0.astype(np.int32) - k11.astype(np.int32)).mean()) > 100)

    # 11. Aperçus à regarder ----------------------------------------------------------------------------
    sheets = []
    rows = []
    for name in ("cpu_a", "optix_a"):
        d = TEST_DIR / name
        if all((d / f"{i:06d}.png").is_file() for i in (0, 12, 15)):
            tiles = [to_preview(d / f"{i:06d}.png") for i in (0, 12, 15)]
            for i, t in zip((0, 12, 15), tiles):
                save_png(TEST_DIR / "preview" / f"{name}_{i:06d}.png", t)
            rows.append(np.hstack(tiles))
    if rows:
        save_png(TEST_DIR / "preview" / "planche_clay.png", np.vstack(rows))
        sheets.append(TEST_DIR / "preview" / "planche_clay.png")
    if (kit_dir / "000000.png").is_file() and (kit_dir / "000011.png").is_file():
        kit_sheet = np.vstack([to_preview(kit_dir / "000000.png"), to_preview(kit_dir / "000011.png")])
        save_png(TEST_DIR / "preview" / "planche_kit.png", cv2.resize(kit_sheet, None, fx=2, fy=2, interpolation=cv2.INTER_NEAREST))
        sheets.append(TEST_DIR / "preview" / "planche_kit.png")
    for s in sheets:
        info(f"aperçu à regarder : {s}")

    print("\nMESURES")
    for k, v in measures.items():
        print(f"  {k} : {v}")
    fails = [label for label, ok in results if not ok]
    print(f"\nRÉSUMÉ étape 13 : {len(results) - len(fails)} PASS, {len(fails)} FAIL")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
