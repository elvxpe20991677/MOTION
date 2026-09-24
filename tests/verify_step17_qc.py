"""Vérification de l'étape 17 : contrôle qualité (pipeline/qc.py).

Lancement depuis la racine :  .venv/Scripts/python.exe tests/verify_step17_qc.py

Le test fabrique TOUTES ses entrées sous build/_tests/step17/ (aucune dépendance aux autres modules) :
- des livrables synthétiques encodés par FFmpeg à partir de sources lavfi (testsrc2, gradients,
  aevalsrc), avec les options de la SPEC §15 pour le jeu conforme ;
- des scènes compilées synthétiques (clés du CONTRACT §4), un manifeste maître et un
  determinism.json validés contre schema/internal/manifests.schema.json.
Puis il vérifie : jeu conforme => CONFORME ; chaque défaut isolé => NON CONFORME avec le bon
contrôle en FAIL (et aucun autre échec hors de ceux qu'il entraîne mécaniquement).
Affiche PASS/FAIL par contrôle ; code de sortie non nul en cas d'échec.
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
TEST_DIR = ROOT / "build" / "_tests" / "step17"

# build/ et out/ redirigés AVANT l'import de pipeline.config (lu à l'import) : le QC écrit ses
# rapports et lit manifestes/déterminisme uniquement sous build/_tests/step17/.
os.environ["MOGRAPH_BUILD_DIR"] = str(TEST_DIR / "build")
os.environ["MOGRAPH_OUT_DIR"] = str(TEST_DIR / "out")
sys.path.insert(0, str(ROOT))

from jsonschema import Draft202012Validator  # noqa: E402
from referencing import Registry, Resource  # noqa: E402

from pipeline import config, qc  # noqa: E402

for _s in (sys.stdout, sys.stderr):
    # Sortie UTF-8 (lisible par Git Bash et les journaux) ; « replace » : jamais d'arrêt sur un caractère.
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass
# Les sous-processus Python (CLI du QC) écrivent aussi en UTF-8, décodé comme tel par le test.
CHILD_ENV = {**os.environ, "PYTHONIOENCODING": "utf-8"}

MEDIA = TEST_DIR / "media"
FPS = 25
FRAMES = 50                      # 2 s : assez pour 0,6 s hors zone, rapide à encoder en 1080p
ASPECT, RESOLUTION = "16:9", "1080p"
W, H = config.output_size(ASPECT, RESOLUTION)
# Deux plans avec un fondu de 5 images : exerce l'échantillonnage des relevés PAR PLAN dans le fondu.
SEGMENTS = [("intro", 0, 30, 0), ("outro", 25, 25, 5)]

RESULTS: list[tuple[bool, str]] = []


def record(ok: bool, label: str, info: str = "") -> bool:
    RESULTS.append((ok, label))
    print(f"{'PASS' if ok else 'FAIL'} {label}" + (f" -- {info}" if info else ""), flush=True)
    return ok


# ---------------------------------------------------------------------------
# Fabrication des livrables (FFmpeg lavfi)
# ---------------------------------------------------------------------------


def ffmpeg(args: list[str]) -> None:
    argv = [config.FFMPEG, "-hide_banner", "-nostats", "-v", "error", "-y", *args]
    res = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8", errors="replace",
                         stdin=subprocess.DEVNULL)
    if res.returncode != 0:
        raise RuntimeError(f"FFmpeg a échoué ({res.returncode}) : {' '.join(argv)}\n{res.stderr}")


def audio_src(lufs: float, seconds: float, *, peaks: bool = False) -> str:
    # Sinus 1 kHz identique sur 2 canaux : BS.1770 donne L = 20·log10(A) LUFS (-3,01 dB par canal
    # pleine échelle + 3,01 dB pour la paire), d'où l'amplitude A = 10^(L/20) sans calibration.
    amp = 10 ** (lufs / 20)
    expr = f"{amp:.6f}*sin(2*PI*1000*t)"
    if peaks:
        # Impulsions de 1 ms toutes les 0,5 s : crête ~ -0,4 dBFS, énergie négligeable (intégré inchangé).
        expr += "+if(lt(mod(t,0.5),0.001),0.75,0)"
    # Expression ENTRE APOSTROPHES : ses virgules ne doivent pas être lues comme séparateurs de filtres.
    return f"aevalsrc='{expr}|{expr}':s={config.AUDIO_RATE}:d={seconds + 0.5:.3f}"


def video_src(kind: str, fps: int) -> str:
    if kind == "black":
        return f"color=c=black:s={W}x{H}:r={fps}"
    return f"testsrc2=s={W}x{H}:r={fps}"


COLOR_FLAGS = ["-color_primaries", "bt709", "-color_trc", "bt709", "-colorspace", "bt709", "-color_range", "tv"]


def encode_hevc(out: Path, *, fps: int = FPS, frames: int = FRAMES, tag: str = "hvc1", color: bool = True,
                lufs: float = -14.0, kind: str = "testsrc2", audio_seconds: float | None = None,
                peaks: bool = False) -> Path:
    """HEVC Main10 selon SPEC §15 (preset ultrafast : seul le conteneur/flux compte pour le QC)."""
    d = audio_seconds if audio_seconds is not None else frames / fps
    chain = ["format=rgb24"]
    if kind == "partblack":
        # 10 premières images (0,4 s) recouvertes de noir : noir partiel mesurable.
        chain.append("drawbox=x=0:y=0:w=iw:h=ih:color=black:t=fill:enable='lt(n,10)'")
    if color:
        chain.append(config.ZSCALE)
    chain.append("format=yuv420p10le")
    x265 = f"keyint={2 * fps}:min-keyint={fps}:log-level=error"
    if color:
        x265 = "colorprim=bt709:transfer=bt709:colormatrix=bt709:range=limited:" + x265
    args = ["-f", "lavfi", "-i", video_src("black" if kind == "black" else "testsrc2", fps),
            "-f", "lavfi", "-i", audio_src(lufs, d, peaks=peaks),
            "-map", "0:v:0", "-map", "1:a:0", "-frames:v", str(frames),
            "-vf", ",".join(chain),
            "-c:v", "libx265", "-preset", "ultrafast", "-crf", "28", "-pix_fmt", "yuv420p10le",
            "-profile:v", "main10", "-x265-params", x265, "-tag:v", tag]
    if color:
        args += COLOR_FLAGS
    args += ["-movflags", "+faststart+write_colr",
             "-af", f"apad=whole_dur={d},atrim=end={d}", "-c:a", "aac", "-b:a", "320k",
             "-ar", str(config.AUDIO_RATE), str(out)]
    ffmpeg(args)
    return out


def encode_prores(out: Path, *, profile: str, timecode: bool = True, real_alpha: bool = True,
                  lufs: float = -14.0) -> Path:
    """ProRes 422 HQ (profile 3) ou 4444 (profile 4) selon SPEC §15."""
    d = FRAMES / FPS
    args = ["-f", "lavfi", "-i", video_src("testsrc2", FPS)]
    if profile == "prores_4444":
        if real_alpha:
            # Masque alpha = dégradé animé (0..255) : transparence réelle et variable ; graine fixée
            # (gradients tire sa géométrie au hasard par défaut) pour des médias de test reproductibles.
            args += ["-f", "lavfi", "-i",
                     f"gradients=s={W}x{H}:r={FPS}:c0=black:c1=white:nb_colors=2:speed=0.01:seed=17",
                     "-f", "lavfi", "-i", audio_src(lufs, d),
                     "-filter_complex",
                     f"[0:v]format=rgba[c];[1:v]format=gray[a];[c][a]alphamerge,{config.ZSCALE},"
                     f"format=yuva444p10le[v];[2:a]apad=whole_dur={d},atrim=end={d}[au]",
                     "-map", "[v]", "-map", "[au]"]
        else:
            # Source opaque encodée en yuva444p10le : plan alpha présent mais entièrement à 1023.
            args += ["-f", "lavfi", "-i", audio_src(lufs, d),
                     "-filter_complex",
                     f"[0:v]format=rgba,{config.ZSCALE},format=yuva444p10le[v];"
                     f"[1:a]apad=whole_dur={d},atrim=end={d}[au]",
                     "-map", "[v]", "-map", "[au]"]
        args += ["-frames:v", str(FRAMES), "-c:v", "prores_ks", "-profile:v", "4", "-vendor", "apl0",
                 "-pix_fmt", "yuva444p10le", "-alpha_bits", "16"]
    else:
        args += ["-f", "lavfi", "-i", audio_src(lufs, d),
                 "-map", "0:v:0", "-map", "1:a:0", "-frames:v", str(FRAMES),
                 "-vf", f"format=rgb24,{config.ZSCALE},format=yuv422p10le",
                 "-af", f"apad=whole_dur={d},atrim=end={d}",
                 "-c:v", "prores_ks", "-profile:v", "3", "-vendor", "apl0", "-pix_fmt", "yuv422p10le"]
    args += COLOR_FLAGS + ["-movflags", "+write_colr"]
    if timecode:
        args += ["-timecode", config.TIMECODE_START]
    args += ["-c:a", "pcm_s24le", "-ar", str(config.AUDIO_RATE), str(out)]
    ffmpeg(args)
    return out


def build_media() -> dict[str, Path]:
    MEDIA.mkdir(parents=True, exist_ok=True)
    m: dict[str, Path] = {}
    jobs = [
        ("ok_hevc", lambda p: encode_hevc(p)),
        ("ok_hq", lambda p: encode_prores(p, profile="prores_422hq")),
        ("ok_4444", lambda p: encode_prores(p, profile="prores_4444")),
        # -r 24 au lieu de 25 (cas exigé par la SPEC) : mêmes 50 images, cadence fausse.
        ("fps24_hevc", lambda p: encode_hevc(p, fps=24)),
        ("frames49_hevc", lambda p: encode_hevc(p, frames=FRAMES - 1, audio_seconds=FRAMES / FPS)),
        ("hev1_hevc", lambda p: encode_hevc(p, tag="hev1")),
        ("nocolor_hevc", lambda p: encode_hevc(p, color=False)),
        ("notmcd_hq", lambda p: encode_prores(p, profile="prores_422hq", timecode=False)),
        ("noalpha_4444", lambda p: encode_prores(p, profile="prores_4444", real_alpha=False)),
        ("black_hevc", lambda p: encode_hevc(p, kind="black")),
        ("partblack_hevc", lambda p: encode_hevc(p, kind="partblack")),
        ("quiet_hevc", lambda p: encode_hevc(p, lufs=-20.0)),
        ("peaks_hevc", lambda p: encode_hevc(p, peaks=True)),
    ]
    for name, fn in jobs:
        ext = ".mp4" if name.endswith("hevc") else ".mov"
        path = MEDIA / f"{name}{ext}"
        t0 = time.perf_counter()
        fn(path)
        print(f"     media {path.name} : {path.stat().st_size} octets en {time.perf_counter() - t0:.2f} s", flush=True)
        m[name] = path
    return m


# ---------------------------------------------------------------------------
# Scène compilée, manifeste maître, déterminisme (synthétiques, conformes aux schémas)
# ---------------------------------------------------------------------------


def make_compiled(scene_id: str, outputs: list[tuple[str, Path]], *, alpha: bool, audio: bool = True,
                  qc_over: dict | None = None) -> dict:
    qc_block = {"safe_zone": "title", "safe_zone_tolerance_s": config.SAFE_ZONE_TOLERANCE_S_DEFAULT,
                "allow_black_s": 0.0, "determinism_frames": config.DETERMINISM_FRAMES_DEFAULT,
                "loudness_target_lufs": -14.0}
    qc_block.update(qc_over or {})
    return {
        "contract": "mograph-scene/1",
        "scene_id": scene_id,
        "title": f"Test QC {scene_id}",
        "source": str(TEST_DIR / "scenes" / f"{scene_id}.json"),
        "preset_id": "test",
        "format": {"aspect": ASPECT, "resolution": RESOLUTION, "fps": FPS, "alpha": alpha},
        "canvas": {"width": config.LOGICAL_CANVAS[ASPECT][0], "height": config.LOGICAL_CANVAS[ASPECT][1]},
        "device_scale_factor": config.RESOLUTION_SCALE[RESOLUTION],
        "output": {"width": W, "height": H},
        "frames": FRAMES,
        "duration": FRAMES / FPS,
        "frame_step": 1,
        "motion_blur": {"enabled": False, "samples": 8, "shutter": 0.5},
        "depth": 8,
        "audio": ({"src": str(MEDIA / "beat.wav"), "bpm": 120, "offset": 0.0, "beats_per_bar": 4, "markers": [],
                   "synth": None, "target_lufs": -14.0} if audio else None),
        "outputs": [{"profile": prof, "crf": config.HEVC_CRF_DEFAULT if prof == "hevc_main10" else None,
                     "suffix": "", "path": str(path), "alpha_flatten": bool(alpha and prof != "prores_4444")}
                    for prof, path in outputs],
        "qc": qc_block,
        "shots": [{"id": sid, "index": i, "frames": n, "global_start": gs, "fade_in_frames": f, "plates": []}
                  for i, (sid, gs, n, f) in enumerate(SEGMENTS)],
        "warnings": [],
    }


def smoothstep(x: float) -> float:
    return x * x * (3 - 2 * x)


def make_master_manifest(scene_id: str, *, alpha: bool, out_local: range | None = None) -> dict:
    """Manifeste maître : 2 plans + fondu ; ``out_local`` = images LOCALES du plan intro où le
    sous-titre sort de la zone titre (x = 30 px < 96 px)."""
    every = config.layout_every(FPS)
    frame_map = []
    fade = SEGMENTS[1][3]
    for g in range(FRAMES):
        if g < SEGMENTS[1][1]:
            frame_map.append({"g": g, "mode": "link", "src": [["intro", g, 1.0]]})
        elif g < SEGMENTS[1][1] + fade:
            k = g - SEGMENTS[1][1]
            w = smoothstep((k + 1) / (fade + 1))
            frame_map.append({"g": g, "mode": "blend", "src": [["intro", g, 1.0 - w], ["outro", k, w]]})
        else:
            frame_map.append({"g": g, "mode": "link", "src": [["outro", g - SEGMENTS[1][1], 1.0]]})
    layout: dict[str, list] = {}
    for sid, gs, n, _f in SEGMENTS:
        local = sorted(set(range(0, n, every)) | {n - 1})
        for l in local:
            recs = []
            if sid == "intro":
                recs.append({"id": "titre", "text": "TITRE PRINCIPAL", "box": [300.0, 400.0, 900.0, 140.0],
                             "opacity": 1.0, "shot_id": sid})
                out = out_local is not None and l in out_local
                recs.append({"id": "sous_titre", "text": "Sous-titre qui déborde" if out else "Sous-titre",
                             "box": [30.0 if out else 300.0, 800.0, 600.0, 60.0], "opacity": 1.0, "shot_id": sid})
                # Texte garé entièrement hors champ (invisible) : ne doit jamais compter.
                recs.append({"id": "hors_champ", "text": "garé hors champ", "box": [-700.0, 500.0, 400.0, 80.0],
                             "opacity": 1.0, "shot_id": sid})
            else:
                recs.append({"id": "chiffre", "text": "42 %", "box": [800.0, 450.0, 320.0, 180.0],
                             "opacity": 0.95, "shot_id": sid})
                # Boîte qui touche le bord gauche à 0,6 px près : dans la tolérance SAFE_ZONE_EPSILON_PX.
                recs.append({"id": "bord", "text": "bord", "box": [95.4, 900.0, 200.0, 50.0],
                             "opacity": 1.0, "shot_id": sid})
            layout.setdefault(str(gs + l), []).extend(recs)
    return {
        "contract": "mograph-master/1", "scene_id": scene_id, "fps": FPS, "frames": FRAMES,
        "width": W, "height": H, "depth": 8, "alpha": alpha,
        "segments": [{"shot_id": sid, "global_start": gs, "frames": n, "fade_in_frames": f}
                     for sid, gs, n, f in SEGMENTS],
        "frame_map": frame_map, "layout_every": every, "layout": layout,
    }


def make_determinism(scene_id: str, *, ok: bool = True) -> dict:
    witnesses = [("intro", 0), ("intro", 29), ("outro", 0), ("outro", 3), ("outro", 24), ("intro", 12)]
    checked = []
    for i, (sid, f) in enumerate(witnesses):
        h = f"{i:02d}" * 32
        match = ok or i != len(witnesses) - 1
        entry = {"shot_id": sid, "frame": f, "expected": h, "actual": h if match else "ff" * 32, "match": match}
        if not match:
            entry["diff"] = {"pixels": 1234, "bbox": [10, 10, 60, 40], "max_abs": 37.0,
                             "diff_png": str(TEST_DIR / "build" / scene_id / "determinism_diff" / f"{sid}_{f:06d}.png")}
        checked.append(entry)
    matches = sum(1 for c in checked if c["match"])
    return {"contract": "mograph-determinism/1", "scene_id": scene_id, "fresh_browser": True,
            "order": [f"{c['shot_id']}:{c['frame']}" for c in reversed(checked)],
            "checked": checked, "total": len(checked), "matches": matches, "ok": matches == len(checked)}


SCHEMA = json.loads((config.SCHEMA_DIR / "internal" / "manifests.schema.json").read_text(encoding="utf-8"))
REGISTRY = Registry().with_resource(SCHEMA["$id"], Resource.from_contents(SCHEMA))


def schema_errors(doc: dict, defname: str) -> list[str]:
    v = Draft202012Validator({"$ref": f"{SCHEMA['$id']}#/$defs/{defname}"}, registry=REGISTRY)
    return [f"{list(e.absolute_path)}: {e.message}" for e in v.iter_errors(doc)]


def write_json(path: Path, doc: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, path)


def setup_scene(scene_id: str, outputs: list[tuple[str, Path]], *, alpha: bool, qc_over: dict | None = None,
                out_local: range | None = None, determinism: str = "ok") -> dict:
    compiled = make_compiled(scene_id, outputs, alpha=alpha, qc_over=qc_over)
    bdir = config.scene_build_dir(scene_id)
    manifest = make_master_manifest(scene_id, alpha=alpha, out_local=out_local)
    errs = schema_errors(manifest, "master_manifest")
    if errs:
        raise RuntimeError(f"manifeste synthétique non conforme : {errs[:3]}")
    write_json(config.master_dir(scene_id) / "manifest.json", manifest)
    if determinism != "absent":
        det = make_determinism(scene_id, ok=(determinism == "ok"))
        errs = schema_errors(det, "determinism")
        if errs:
            raise RuntimeError(f"determinism.json synthétique non conforme : {errs[:3]}")
        write_json(bdir / "determinism.json", det)
    write_json(bdir / "compiled.json", compiled)
    return compiled


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def check_statuses(report: dict) -> dict[str, dict]:
    return {c["id"]: c for c in report["checks"]}


def unit_safe_zone_tests() -> None:
    rect = config.safe_rect(ASPECT, "title")
    canvas = config.LOGICAL_CANVAS[ASPECT]
    m06 = make_master_manifest("unit", alpha=False, out_local=range(5, 20))
    ex06 = qc.analyse_safe_zones(m06, rect, canvas=canvas)
    record(len(ex06) == 1 and ex06[0]["id"] == "sous_titre" and abs(ex06[0]["seconds"] - 0.6) < 1e-9,
           "zones sûres (unitaire) : 15 images hors zone (relevés 5/10/15) => excursion de 0,60 s",
           f"{[(e['id'], e['seconds'], e['start_frame'], e['end_frame']) for e in ex06]}")
    m03 = make_master_manifest("unit", alpha=False, out_local=range(5, 13))
    ex03 = qc.analyse_safe_zones(m03, rect, canvas=canvas)
    record(len(ex03) == 1 and ex03[0]["seconds"] <= 0.5,
           "zones sûres (unitaire) : 8 images hors zone (0,32 s) => excursion estimée <= 0,5 s",
           f"{[(e['id'], e['seconds']) for e in ex03]}")
    m00 = make_master_manifest("unit", alpha=False, out_local=None)
    ex00 = qc.analyse_safe_zones(m00, rect, canvas=canvas)
    record(ex00 == [], "zones sûres (unitaire) : bord à 0,6 px (< epsilon) et texte hors champ ignorés",
           f"{ex00}")
    # Sans canevas, le texte garé hors champ compte : prouve que c'est bien le filtre de visibilité qui l'écarte.
    ex_nc = qc.analyse_safe_zones(m00, rect, canvas=None)
    record(any(e["id"] == "hors_champ" for e in ex_nc),
           "zones sûres (unitaire) : sans filtre de visibilité, le texte hors champ serait compté",
           f"{[(e['id'], e['seconds']) for e in ex_nc]}")


def main() -> int:
    t_all = time.perf_counter()
    if not str(config.BUILD_DIR).startswith(str(TEST_DIR)) or not str(config.OUT_DIR).startswith(str(TEST_DIR)):
        print(f"FAIL environnement : BUILD_DIR={config.BUILD_DIR} OUT_DIR={config.OUT_DIR} hors de {TEST_DIR}")
        return 1
    if TEST_DIR.exists():
        shutil.rmtree(TEST_DIR)   # uniquement build/_tests/step17 : reconstruit de zéro à chaque passage
    TEST_DIR.mkdir(parents=True)

    print(f"FFmpeg : {qc._tool_version(config.FFMPEG)}")
    t0 = time.perf_counter()
    media = build_media()
    t_media = time.perf_counter() - t0
    print(f"     {len(media)} fichiers générés en {t_media:.1f} s", flush=True)

    # --- probe() -----------------------------------------------------------------------------------
    info = qc.probe(media["ok_hevc"])
    v = [s for s in info["streams"] if s["codec_type"] == "video"][0]
    record(v.get("nb_read_frames") == str(FRAMES), "probe() compte les images (nb_read_frames)",
           f"nb_read_frames={v.get('nb_read_frames')} codec={v.get('codec_name')} tag={v.get('codec_tag_string')}")
    try:
        qc.probe(MEDIA / "absent.mp4")
        record(False, "probe() sur un fichier absent lève QCError")
    except qc.QCError as exc:
        record("introuvable" in str(exc), "probe() sur un fichier absent lève QCError (message actionnable)", str(exc))

    unit_safe_zone_tests()

    ok3 = [("hevc_main10", media["ok_hevc"]), ("prores_422hq", media["ok_hq"]), ("prores_4444", media["ok_4444"])]
    ok2 = [("hevc_main10", media["ok_hevc"]), ("prores_422hq", media["ok_hq"])]

    def swap(base: list, profile: str, path: Path) -> list:
        return [(p, path if p == profile else src) for p, src in base]

    # (scene_id, description, sorties, alpha, qc_over, out_local, determinism, verdict attendu,
    #  contrôles attendus {id: statut}, échecs tolérés en plus (entraînés mécaniquement par le défaut))
    cases = [
        ("ok_alpha", "jeu conforme HEVC Main10 hvc1 + ProRes 422 HQ + 4444 alpha, -14 LUFS", ok3, True, None, None,
         "ok", qc.VERDICT_OK, {"hevc_main10.hvc1": "PASS", "hevc_main10.color": "PASS", "hevc_main10.fps": "PASS",
                               "hevc_main10.frames": "PASS", "hevc_main10.loudness": "PASS",
                               "prores_422hq.timecode": "PASS", "prores_422hq.profile": "PASS",
                               "prores_4444.alpha": "PASS", "prores_4444.pix_fmt": "PASS",
                               "prores_4444.profile": "PASS", "scene.safe_zone": "PASS",
                               "scene.determinism": "PASS"}, set()),
        ("ok_opaque", "jeu conforme opaque (noir bloquant actif, aucun noir)", ok2, False, None, None, "ok",
         qc.VERDICT_OK, {"hevc_main10.black": "PASS", "prores_422hq.black": "PASS"}, set()),
        ("fps24", "fichier -r 24 au lieu de 25 (cas SPEC)", swap(ok3, "hevc_main10", media["fps24_hevc"]), True,
         None, None, "ok", qc.VERDICT_KO, {"hevc_main10.fps": "FAIL"},
         {"hevc_main10.duration", "hevc_main10.audio"}),
        ("frames49", "nombre d'images faux (49 au lieu de 50)", swap(ok3, "hevc_main10", media["frames49_hevc"]),
         True, None, None, "ok", qc.VERDICT_KO, {"hevc_main10.frames": "FAIL"}, {"hevc_main10.duration"}),
        ("hev1", "étiquette hev1 au lieu de hvc1", swap(ok3, "hevc_main10", media["hev1_hevc"]), True, None, None,
         "ok", qc.VERDICT_KO, {"hevc_main10.hvc1": "FAIL"}, set()),
        ("nocolor", "métadonnées couleur absentes", swap(ok3, "hevc_main10", media["nocolor_hevc"]), True, None,
         None, "ok", qc.VERDICT_KO, {"hevc_main10.color": "FAIL"}, set()),
        ("notmcd", "ProRes 422 HQ sans piste tmcd", swap(ok3, "prores_422hq", media["notmcd_hq"]), True, None, None,
         "ok", qc.VERDICT_KO, {"prores_422hq.timecode": "FAIL"}, set()),
        ("noalpha", "ProRes 4444 sans alpha réel (plan alpha opaque)", swap(ok3, "prores_4444",
                                                                           media["noalpha_4444"]),
         True, None, None, "ok", qc.VERDICT_KO, {"prores_4444.alpha": "FAIL"}, set()),
        ("black", "vidéo noire (scène opaque)", swap(ok2, "hevc_main10", media["black_hevc"]), False, None, None,
         "ok", qc.VERDICT_KO, {"hevc_main10.black": "FAIL"}, set()),
        ("black_alpha_info", "vidéo noire dans une scène à alpha => informatif", swap(ok3, "hevc_main10",
                                                                                      media["black_hevc"]),
         True, None, None, "ok", qc.VERDICT_OK, {"hevc_main10.black": "WARN"}, set()),
        ("partblack_allowed", "0,4 s de noir avec allow_black_s = 0,5", swap(ok2, "hevc_main10",
                                                                             media["partblack_hevc"]),
         False, {"allow_black_s": 0.5}, None, "ok", qc.VERDICT_OK, {"hevc_main10.black": "PASS"}, set()),
        ("partblack_strict", "0,4 s de noir avec allow_black_s = 0", swap(ok2, "hevc_main10",
                                                                          media["partblack_hevc"]),
         False, None, None, "ok", qc.VERDICT_KO, {"hevc_main10.black": "FAIL"}, set()),
        ("loud20", "loudness à -20 LUFS (cible -14)", swap(ok3, "hevc_main10", media["quiet_hevc"]), True, None,
         None, "ok", qc.VERDICT_KO, {"hevc_main10.loudness": "FAIL"}, set()),
        ("truepeak", "true peak > -1 dBTP (intégré correct)", swap(ok3, "hevc_main10", media["peaks_hevc"]), True,
         None, None, "ok", qc.VERDICT_KO, {"hevc_main10.loudness": "FAIL"}, set()),
        ("safe06", "texte hors zone 0,6 s continu", ok3, True, None, range(5, 20), "ok", qc.VERDICT_KO,
         {"scene.safe_zone": "FAIL"}, set()),
        ("safe03", "texte hors zone 0,3 s continu (toléré)", ok3, True, None, range(5, 13), "ok", qc.VERDICT_OK,
         {"scene.safe_zone": "PASS"}, set()),
        ("safe_off", "0,6 s hors zone mais qc.safe_zone = off", ok3, True, {"safe_zone": "off"}, range(5, 20), "ok",
         qc.VERDICT_OK, {"scene.safe_zone": "SKIP"}, set()),
        ("determinism_ko", "determinism.json ok = false", ok3, True, None, None, "ko", qc.VERDICT_KO,
         {"scene.determinism": "FAIL"}, set()),
        ("determinism_absent", "determinism.json absent", ok3, True, None, None, "absent", qc.VERDICT_KO,
         {"scene.determinism": "FAIL"}, set()),
        ("missing_file", "livrable HEVC absent", swap(ok3, "hevc_main10", MEDIA / "absent.mp4"), True, None, None,
         "ok", qc.VERDICT_KO, {"hevc_main10.readable": "FAIL"}, set()),
    ]

    timings = []
    reports: dict[str, dict] = {}
    for sid, desc, outs, alpha, qc_over, out_local, det, want_verdict, want_checks, tolerated in cases:
        compiled = setup_scene(sid, outs, alpha=alpha, qc_over=qc_over, out_local=out_local, determinism=det)
        t0 = time.perf_counter()
        report = qc.run_qc(compiled, log=lambda m: None)
        dt = time.perf_counter() - t0
        timings.append((sid, dt))
        reports[sid] = report
        by_id = check_statuses(report)
        fails = [c["id"] for c in report["checks"] if c["status"] == "FAIL"]
        warns = [c["id"] for c in report["checks"] if c["status"] == "WARN"]
        print(f"     [{sid}] {desc} : verdict réel = {report['verdict']} ; FAIL = {fails or '-'} ; "
              f"WARN = {warns or '-'} ; {dt:.2f} s", flush=True)
        record(report["verdict"] == want_verdict, f"{sid} : verdict {want_verdict}", report["verdict"])
        for cid, want in want_checks.items():
            c = by_id.get(cid)
            got = c["status"] if c else "absent"
            info = "" if c is None else f"attendu={json.dumps(c['expected'], ensure_ascii=False)[:120]} " \
                                        f"mesuré={json.dumps(c['actual'], ensure_ascii=False)[:160]}"
            record(got == want, f"{sid} : contrôle {cid} = {want}", f"{got} ; {info}")
        allowed = {cid for cid, st in want_checks.items() if st == "FAIL"} | tolerated
        spurious = [f for f in fails if f not in allowed]
        record(not spurious, f"{sid} : aucun autre échec que ceux du défaut isolé", f"{spurious or 'aucun'}")
        for c in report["checks"]:
            if c["status"] == "FAIL" and not c["blocking"]:
                record(False, f"{sid} : FAIL non bloquant incohérent {c['id']}")

    tp = check_statuses(reports["truepeak"]).get("hevc_main10.loudness", {}).get("actual") or {}
    record(tp.get("true_peak_dbtp", -99) > config.TRUE_PEAK_MAX_DBTP
           and abs(tp.get("integrated_lufs", 0) + 14.0) <= config.LOUDNESS_TOLERANCE_LU,
           "truepeak : l'échec vient bien du true peak (intégré dans la tolérance)", f"{tp}")

    # --- Format des contrôles (CONTRACT §4) ----------------------------------------------------------
    keys = {"id", "label", "status", "expected", "actual", "blocking", "detail"}
    bad = [(sid, c.get("id")) for sid, r in reports.items() for c in r["checks"]
           if set(c) != keys or c["status"] not in qc.STATUSES or not isinstance(c["blocking"], bool)]
    record(not bad, "chaque contrôle a exactement les clés du CONTRACT §4 et un statut valide", f"{bad[:5] or 'ok'}")
    fail_details = [(sid, c["id"]) for sid, r in reports.items() for c in r["checks"]
                    if c["status"] == "FAIL" and not c["detail"]]
    record(not fail_details, "chaque FAIL porte un détail actionnable", f"{fail_details[:5] or 'ok'}")

    # --- Rapports écrits -----------------------------------------------------------------------------
    for sid in ("ok_alpha", "fps24"):
        out_dir = config.scene_out_dir(sid)
        jpath, mpath = out_dir / "qc_report.json", out_dir / "qc_report.md"
        ok_files = jpath.is_file() and mpath.is_file() and not list(out_dir.glob("*.tmp"))
        record(ok_files, f"{sid} : qc_report.json + qc_report.md écrits (sans .tmp résiduel)", str(out_dir))
        if ok_files:
            rj = json.loads(jpath.read_text(encoding="utf-8"))
            md = mpath.read_text(encoding="utf-8")
            first = md.splitlines()[0]
            want = reports[sid]["verdict"]
            record(rj["verdict"] == want and first == f"# QC {sid} : {want}",
                   f"{sid} : verdict {want} en tête du Markdown et dans le JSON", first)
            has_table = "| Statut | Id | Contrôle | Attendu | Mesuré | Bloquant |" in md
            has_fail_section = "## Détails des échecs" in md
            record(has_table and has_fail_section, f"{sid} : tableau des contrôles + section des échecs présents")
            if want == qc.VERDICT_KO:
                record("### `hevc_main10.fps`" in md, f"{sid} : le détail de l'échec fps figure dans le Markdown")

    # --- CLI ---------------------------------------------------------------------------------------
    for sid, want_code in (("ok_alpha", 0), ("fps24", 1)):
        cpath = config.scene_build_dir(sid) / "compiled.json"
        res = subprocess.run([sys.executable, "-m", "pipeline.qc", str(cpath)], cwd=str(ROOT), capture_output=True,
                             text=True, encoding="utf-8", errors="replace", env=CHILD_ENV)
        last = res.stdout.strip().splitlines()[-1] if res.stdout.strip() else res.stderr.strip()[-200:]
        record(res.returncode == want_code, f"CLI « python -m pipeline.qc » sur {sid} : code {want_code}",
               f"code={res.returncode} ; {last}")
    res = subprocess.run([sys.executable, "-m", "pipeline.qc", str(TEST_DIR / "nexiste_pas.json")], cwd=str(ROOT),
                         capture_output=True, text=True, encoding="utf-8", errors="replace", env=CHILD_ENV)
    record(res.returncode == 2 and "introuvable" in res.stderr, "CLI : scène compilée absente => code 2 + message",
           f"code={res.returncode} ; {res.stderr.strip()[:140]}")

    # --- Mesures réelles du jeu conforme -------------------------------------------------------------
    print("     Mesures réelles du jeu conforme (ok_alpha) :")
    for c in reports["ok_alpha"]["checks"]:
        print(f"       {c['status']:4} {c['id']:26} {json.dumps(c['actual'], ensure_ascii=False)[:150]}")
    print("     Temps run_qc par cas : " + ", ".join(f"{s}={t:.2f}s" for s, t in timings))
    print(f"     Temps total : {time.perf_counter() - t_all:.1f} s (dont génération des médias {t_media:.1f} s)")

    failed = [label for ok, label in RESULTS if not ok]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} contrôles réussis.")
    if failed:
        print("ÉCHECS :\n  - " + "\n  - ".join(failed))
        return 1
    print("ÉTAPE 17 (QC) : TOUS LES CONTRÔLES PASSENT")
    return 0


if __name__ == "__main__":
    sys.exit(main())
