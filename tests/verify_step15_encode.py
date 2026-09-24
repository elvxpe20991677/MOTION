"""Vérification de l'étape 15 : pipeline/encode.py (HEVC Main10 hvc1, ProRes 422 HQ / 4444).

Lancement (depuis la racine) : .venv/Scripts/python.exe tests/verify_step15_encode.py
Fichiers temporaires : build/_tests/step15/ (recréé à chaque lancement). Les niveaux sont mesurés
en 10 bits en décodant les fichiers livrés en rawvideo.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TEST_DIR = ROOT / "build" / "_tests" / "step15"
# build/ et out/ du pipeline redirigés AVANT l'import de config (lus à l'import).
os.environ["MOGRAPH_BUILD_DIR"] = str(TEST_DIR / "build")
os.environ["MOGRAPH_OUT_DIR"] = str(TEST_DIR / "out")
sys.path.insert(0, str(ROOT))

import cv2  # noqa: E402
import numpy as np  # noqa: E402

from pipeline import config  # noqa: E402
from pipeline import encode  # noqa: E402

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
FAILS: list[str] = []
TIMINGS: dict[str, float] = {}


def check(label: str, ok: bool, detail: str = "") -> bool:
    print(f"{'PASS' if ok else 'FAIL'} {label}" + (f" -- {detail}" if detail else ""))
    if not ok:
        FAILS.append(label)
    return ok


def info(text: str) -> None:
    print(f"INFO {text}")


# ---------------------------------------------------------------------------
# Outils FFmpeg / fichiers
# ---------------------------------------------------------------------------

def run(argv: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL, **kw)


def probe(path: Path) -> dict:
    p = run([config.FFPROBE, "-v", "error", "-count_frames", "-show_streams", "-show_format", "-of", "json", str(path)])
    assert p.returncode == 0, p.stderr.decode(errors="replace")
    return json.loads(p.stdout)


def video_stream(pr: dict) -> dict:
    return next(s for s in pr["streams"] if s["codec_type"] == "video")


def decode_planes(path: Path, pix_fmt: str, w: int, h: int, src_is_image: bool = False, vf: str | None = None):
    """Décode en rawvideo 10 bits -> liste d'images, chacune = dict de plans numpy uint16."""
    argv = [config.FFMPEG, "-v", "error", "-nostdin", "-i", str(path)]
    if not src_is_image:
        argv += ["-map", "0:v:0"]
    if vf:
        argv += ["-vf", vf]
    argv += ["-f", "rawvideo", "-pix_fmt", pix_fmt, "-"]
    p = run(argv)
    assert p.returncode == 0, p.stderr.decode(errors="replace")
    data = np.frombuffer(p.stdout, dtype="<u2")
    if pix_fmt == "yuv420p10le":
        shapes = [("Y", h, w), ("U", h // 2, w // 2), ("V", h // 2, w // 2)]
    elif pix_fmt == "yuv422p10le":
        shapes = [("Y", h, w), ("U", h, w // 2), ("V", h, w // 2)]
    elif pix_fmt == "yuv444p10le":
        shapes = [("Y", h, w), ("U", h, w), ("V", h, w)]
    elif pix_fmt == "yuva444p10le":
        shapes = [("Y", h, w), ("U", h, w), ("V", h, w), ("A", h, w)]
    else:
        raise ValueError(pix_fmt)
    per = sum(a * b for _, a, b in shapes)
    frames = []
    for f in range(data.size // per):
        chunk = data[f * per:(f + 1) * per]
        planes, pos = {}, 0
        for name, a, b in shapes:
            planes[name] = chunk[pos:pos + a * b].reshape(a, b)
            pos += a * b
        frames.append(planes)
    return frames


def audio_samples(path: Path) -> int:
    """Nombre d'échantillons PAR CANAL de la 1re piste audio après décodage (liste d'édition appliquée)."""
    p = run([config.FFMPEG, "-v", "error", "-nostdin", "-i", str(path), "-map", "0:a:0", "-f", "s32le", "-ac", "1", "-"])
    assert p.returncode == 0, p.stderr.decode(errors="replace")
    return len(p.stdout) // 4


def decoded_md5(path: Path, stream: str) -> str:
    p = run([config.FFMPEG, "-v", "error", "-nostdin", "-i", str(path), "-map", stream, "-f", "md5", "-"])
    assert p.returncode == 0, p.stderr.decode(errors="replace")
    return p.stdout.decode().strip()


def write_png(path: Path, img: np.ndarray) -> None:
    ok, buf = cv2.imencode(".png", img, [cv2.IMWRITE_PNG_COMPRESSION, 1])
    assert ok
    buf.tofile(str(path))


def make_master(scene_id: str, frames_bgra: list[np.ndarray], fps: int, alpha: bool, extra: list[np.ndarray] = ()) -> None:
    """Séquence maître synthétique + manifeste maître conforme (lu par encode pour les contrôles)."""
    d = config.master_dir(scene_id)
    d.mkdir(parents=True, exist_ok=True)
    for g, img in enumerate(list(frames_bgra) + list(extra)):
        write_png(d / f"{g:06d}.png", img)
    h, w = frames_bgra[0].shape[:2]
    n = len(frames_bgra)
    man = {
        "contract": "mograph-master/1", "scene_id": scene_id, "fps": fps, "frames": n, "width": w, "height": h,
        "depth": 16 if frames_bgra[0].dtype == np.uint16 else 8, "alpha": alpha,
        "segments": [{"shot_id": "s", "global_start": 0, "frames": n, "fade_in_frames": 0}],
        "frame_map": [{"g": g, "mode": "link", "src": [["s", g, 1.0]]} for g in range(n)],
        "layout_every": config.layout_every(fps), "layout": {},
    }
    (d / "manifest.json").write_text(json.dumps(man), encoding="utf-8")


def make_compiled(scene_id: str, w: int, h: int, fps: int, frames: int, depth: int, alpha: bool,
                  outputs: list[tuple[str, str, bool]], audio_src: Path | None) -> dict:
    outs = []
    for profile, suffix, flatten in outputs:
        ext = config.PROFILES[profile]["ext"]
        outs.append({"profile": profile, "crf": None, "suffix": suffix, "alpha_flatten": flatten,
                     "path": str(config.scene_out_dir(scene_id) / f"{scene_id}{suffix}_{profile}{ext}")})
    return {
        "contract": "mograph-scene/1", "scene_id": scene_id, "title": "test encode", "source": "test",
        "preset_id": "flat_riso", "format": {"aspect": "16:9", "resolution": "1080p", "fps": fps, "alpha": alpha},
        "canvas": {"width": w, "height": h}, "device_scale_factor": 1, "output": {"width": w, "height": h},
        "frames": frames, "duration": frames / fps, "frame_step": 1,
        "motion_blur": {"enabled": False, "samples": 1, "shutter": 0.5}, "depth": depth,
        "audio": None if audio_src is None else {"src": str(audio_src), "bpm": None, "offset": 0.0,
                                                 "beats_per_bar": 4, "markers": [], "synth": None,
                                                 "target_lufs": -14.0},
        "outputs": outs, "qc": {}, "shots": [], "warnings": [],
    }


def make_sine(path: Path, seconds: float) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    p = run([config.FFMPEG, "-v", "error", "-nostdin", "-y", "-f", "lavfi", "-i",
             f"sine=frequency=440:sample_rate=48000:duration={seconds}", "-ac", "2", "-c:a", "pcm_s24le", str(path)])
    assert p.returncode == 0, p.stderr.decode(errors="replace")


def find_bash() -> str | None:
    """Git Bash (le bash.exe de System32 est le lanceur WSL, absent de cette machine)."""
    cands = [os.environ.get("MOGRAPH_BASH"), r"C:\Program Files\Git\bin\bash.exe",
             r"C:\Program Files\Git\usr\bin\bash.exe", shutil.which("bash")]
    for c in cands:
        if c and Path(c).is_file() and "system32" not in c.lower():
            return c
    return None


def frame_color(path: Path) -> dict:
    """Champs couleur de la 1re image décodée (FFmpeg < 7 : plage ProRes posée sur les images seulement)."""
    p = run([config.FFPROBE, "-v", "error", "-select_streams", "v:0", "-read_intervals", "%+#1", "-show_entries",
             "frame=color_primaries,color_transfer,color_space,color_range", "-of", "json", str(path)])
    assert p.returncode == 0, p.stderr.decode(errors="replace")
    return (json.loads(p.stdout).get("frames") or [{}])[0]


def check_common_tags(label: str, vs: dict, fps: int, frames: int, path: Path) -> None:
    keys = ("color_primaries", "color_transfer", "color_space", "color_range")
    # Flux d'abord ; champ absent -> image décodée (même règle que pipeline/qc.py).
    fr = frame_color(path) if any(vs.get(k) is None for k in keys) else {}
    tags = tuple(vs.get(k) if vs.get(k) is not None else fr.get(k) for k in keys)
    check(f"{label} : bt709 ×3 + tv", tags == ("bt709", "bt709", "bt709", "tv"),
          str(tags) + (f" (image décodée pour {[k for k in keys if vs.get(k) is None]})" if fr else ""))
    check(f"{label} : r_frame_rate = avg_frame_rate = {fps}/1",
          vs.get("r_frame_rate") == f"{fps}/1" == vs.get("avg_frame_rate"),
          f"{vs.get('r_frame_rate')} / {vs.get('avg_frame_rate')}")
    check(f"{label} : -frames:v exact (ffprobe -count_frames = {frames})", vs.get("nb_read_frames") == str(frames),
          f"nb_read_frames={vs.get('nb_read_frames')}")


# ---------------------------------------------------------------------------
# Scénario 1 : niveaux 8 bits (blanc / noir), HEVC + ProRes 422 HQ, audio plus court que la vidéo
# ---------------------------------------------------------------------------

def scenario_levels(bash: str | None) -> None:
    sid, w, h, fps, n = "t15_levels", 256, 64, 25, 10
    white = np.full((h, w, 4), 255, np.uint8)
    black = np.zeros((h, w, 4), np.uint8)
    black[..., 3] = 255
    red = np.zeros((h, w, 4), np.uint8)
    red[..., 2] = 255
    red[..., 3] = 255
    # 3 images en trop dans le dossier : -frames:v doit s'arrêter à N.
    make_master(sid, [white] * 5 + [black] * 5, fps, False, extra=[red] * 3)
    audio = TEST_DIR / "audio" / "sine_0.3s.wav"
    make_sine(audio, 0.3)
    compiled = make_compiled(sid, w, h, fps, n, 8, False,
                             [("hevc_main10", "", False), ("prores_422hq", "", False)], audio)
    cmds = encode.build_commands(compiled)
    t0 = time.perf_counter()
    paths = encode.encode_scene(compiled)
    TIMINGS[sid] = time.perf_counter() - t0
    check("[niveaux] encode_scene produit 2 fichiers", len(paths) == 2 and all(p.is_file() for p in paths),
          ", ".join(p.name for p in paths))
    hevc, prores = paths
    argv = cmds[0]["argv"]
    joined = " ".join(argv)
    check("[niveaux] argv HEVC conforme SPEC (preset slow, crf 18, main10, keyint=50:min-keyint=25, hvc1, faststart)",
          all(x in joined for x in ["-c:v libx265", "-preset slow", "-crf 18", "-profile:v main10",
                                    "keyint=50:min-keyint=25", "-tag:v hvc1", "+faststart+write_colr",
                                    "-framerate 25 -start_number 0 -i", "-frames:v 10",
                                    "apad=whole_dur=0.4,atrim=end=0.4", "-c:a aac -b:a 320k -ar 48000",
                                    "-map 0:v:0", "-map 1:a:0"]) and argv[1:4] == ["-y", "-hide_banner", "-nostdin"])
    check("[niveaux] chaîne vidéo = config.ZSCALE,format=yuv420p10le",
          argv[argv.index("-vf") + 1] == f"{config.ZSCALE},format=yuv420p10le", argv[argv.index("-vf") + 1])
    pj = " ".join(cmds[1]["argv"])
    check("[niveaux] argv ProRes 422 HQ conforme (prores_ks profil 3, apl0, timecode, pcm_s24le)",
          all(x in pj for x in ["-c:v prores_ks", "-profile:v 3", "-vendor apl0", "-pix_fmt yuv422p10le",
                                "-movflags +write_colr", "-timecode 01:00:00:00", "-c:a pcm_s24le -ar 48000"]))

    ph, pp = probe(hevc), probe(prores)
    vh, vp = video_stream(ph), video_stream(pp)
    check("[niveaux] HEVC : codec hevc, profil Main 10, yuv420p10le",
          (vh["codec_name"], vh.get("profile"), vh["pix_fmt"]) == ("hevc", "Main 10", "yuv420p10le"),
          f"{vh['codec_name']} / {vh.get('profile')} / {vh['pix_fmt']}")
    check("[niveaux] HEVC : tag hvc1", vh.get("codec_tag_string") == "hvc1", vh.get("codec_tag_string"))
    check_common_tags("[niveaux] HEVC", vh, fps, n, hevc)
    check("[niveaux] ProRes : profil HQ, yuv422p10le",
          (vp["codec_name"], vp.get("profile"), vp["pix_fmt"]) == ("prores", "HQ", "yuv422p10le"),
          f"{vp['codec_name']} / {vp.get('profile')} / {vp['pix_fmt']}")
    check_common_tags("[niveaux] ProRes 422 HQ", vp, fps, n, prores)
    tmcd = [s for s in pp["streams"] if s.get("codec_tag_string") == "tmcd"]
    check("[niveaux] ProRes : piste timecode tmcd 01:00:00:00",
          bool(tmcd) and tmcd[0].get("tags", {}).get("timecode") == "01:00:00:00",
          str(tmcd[0].get("tags", {}).get("timecode")) if tmcd else "absente")

    for name, path, fmt in (("HEVC", hevc, "yuv420p10le"), ("ProRes 422 HQ", prores, "yuv422p10le")):
        frames = decode_planes(path, fmt, w, h)
        wy = np.unique(np.concatenate([frames[i]["Y"].ravel() for i in range(5)]))
        by = np.unique(np.concatenate([frames[i]["Y"].ravel() for i in range(5, 10)]))
        uv = np.unique(np.concatenate([np.concatenate([f["U"].ravel(), f["V"].ravel()]) for f in frames]))
        check(f"[niveaux] {name} : blanc pur -> Y = 940 (tous les pixels)", wy.tolist() == [940], str(wy.tolist()[:8]))
        check(f"[niveaux] {name} : noir pur -> Y = 64 (tous les pixels)", by.tolist() == [64], str(by.tolist()[:8]))
        check(f"[niveaux] {name} : chroma = 512 (tous les pixels)", uv.tolist() == [512], str(uv.tolist()[:8]))

    # Calage audio : 0,3 s de source complétés à D = 0,4 s exactement.
    exp_samples = n * config.AUDIO_RATE // fps
    ns = audio_samples(prores)
    check(f"[niveaux] audio ProRes (PCM) : exactement {exp_samples} échantillons (0,3 s complété à 0,4 s)",
          ns == exp_samples, f"{ns} échantillons")
    au = next(s for s in ph["streams"] if s["codec_type"] == "audio")
    na = audio_samples(hevc)
    dur = float(au.get("duration", "nan"))
    check("[niveaux] audio HEVC (AAC 48 kHz) : durée à ±(1 image + 25 ms)",
          au["codec_name"] == "aac" and au["sample_rate"] == "48000" and abs(dur - n / fps) <= 1 / fps + 0.025,
          f"codec {au['codec_name']} {au['sample_rate']} Hz, durée conteneur {dur:.6f} s, {na} échantillons décodés "
          f"(attendu {exp_samples})")

    # Script de commandes : présent, conforme, ré-exécutable par bash.
    script = config.scene_out_dir(sid) / "encode_commands.sh"
    text = script.read_text(encoding="utf-8") if script.is_file() else ""
    check("[script] encode_commands.sh présent (LF, shebang bash, cd racine)",
          text.startswith("#!/usr/bin/env bash") and "\r" not in text and 'cd "$(dirname "$SCRIPT_PATH")/' in text)
    check("[script] une commande FFmpeg par livrable, chemins relatifs à la racine",
          text.count('"$FFMPEG" ') == 2 and "build/_tests/step15/build/t15_levels/master/%06d.png" in text
          and "C:" not in text.split("set -euo pipefail", 1)[1], "")
    if bash is None:
        check("[script] Git Bash disponible pour rejouer le script", False, "bash.exe introuvable")
        return
    ref = {p.name: (decoded_md5(p, "0:v:0"), decoded_md5(p, "0:a:0")) for p in paths}
    for p in paths:
        p.unlink()
    for how, arg in (("chemin POSIX", script.as_posix()), ("chemin Windows à antislashs", str(script))):
        t0 = time.perf_counter()
        r = run([bash, arg], cwd=str(TEST_DIR))
        TIMINGS[f"bash ({how})"] = time.perf_counter() - t0
        ok = r.returncode == 0 and all(p.is_file() for p in paths)
        check(f"[script] bash encode_commands.sh ({how}) s'exécute et recrée les 2 fichiers", ok,
              "" if ok else r.stderr.decode(errors="replace")[-600:])
        if not ok:
            return
    replay = {p.name: (decoded_md5(p, "0:v:0"), decoded_md5(p, "0:a:0")) for p in paths}
    for p in paths:
        vs = video_stream(probe(p))
        check(f"[script] fichier rejoué valide : {p.name} ({vs['codec_name']}, {vs.get('nb_read_frames')} images)",
              vs.get("nb_read_frames") == str(n))
    check("[script] ProRes rejoué identique (md5 des images et du son décodés)",
          replay[prores.name] == ref[prores.name], f"{replay[prores.name][0]}")
    info(f"HEVC rejoué : md5 image {'identique' if replay[hevc.name][0] == ref[hevc.name][0] else 'différent'}, "
         f"md5 son {'identique' if replay[hevc.name][1] == ref[hevc.name][1] else 'différent'}")
    check("[script] aucun fichier .partial résiduel", not list(config.scene_out_dir(sid).glob("*.partial.*")))


# ---------------------------------------------------------------------------
# Scénario 2 : dégradés 16 bits (pas de banding), audio plus long que la vidéo
# ---------------------------------------------------------------------------

def scenario_gradient() -> None:
    sid, w, h, fps, n = "t15_grad16", 256, 64, 25, 4
    full = np.zeros((h, w, 4), np.uint16)
    full[..., :3] = (np.arange(w, dtype=np.uint32) * 257).astype(np.uint16)[None, :, None]
    full[..., 3] = 65535
    # Dégradé ÉTROIT : 256 codes Y consécutifs (300..555) sur 256 px, impossible depuis une source 8 bits.
    target = 300 + np.arange(w)
    narrow_v = np.rint((target - 64) / 876 * 65535).astype(np.uint16)
    narrow = np.zeros((h, w, 4), np.uint16)
    narrow[..., :3] = narrow_v[None, :, None]
    narrow[..., 3] = 65535
    make_master(sid, [full, full, narrow, narrow], fps, False)
    audio = TEST_DIR / "audio" / "sine_1.0s.wav"
    make_sine(audio, 1.0)
    compiled = make_compiled(sid, w, h, fps, n, 16, False,
                             [("prores_422hq", "", False), ("hevc_main10", "", False)], audio)
    t0 = time.perf_counter()
    prores, hevc = encode.encode_scene(compiled, log=lambda *_: None)
    TIMINGS[sid] = time.perf_counter() - t0
    row = h // 2
    master = config.master_dir(sid)

    # (a) Conversion seule (config.ZSCALE) sur les PNG 16 bits.
    conv_full = decode_planes(master / "000000.png", "yuv444p10le", w, h, True, f"{config.ZSCALE},format=yuv444p10le")[0]
    conv_nar = decode_planes(master / "000002.png", "yuv444p10le", w, h, True, f"{config.ZSCALE},format=yuv444p10le")[0]
    nf, nn = np.unique(conv_full["Y"][row]).size, np.unique(conv_nar["Y"][row]).size
    check("[dégradé] conversion ZSCALE : dégradé 16 bits plein de 256 px -> 256 valeurs Y distinctes", nf == 256, f"{nf}")
    check("[dégradé] conversion ZSCALE : dégradé 16 bits étroit -> 256 valeurs Y distinctes (300..555)", nn == 256,
          f"{nn} valeurs, écart max à la cible {int(np.abs(conv_nar['Y'][row].astype(int) - target).max())}")
    # Même dégradé étroit quantifié en 8 bits : le banding qu'éviterait une source 16 bits.
    narrow8 = (np.rint(narrow.astype(np.float64) / 257)).astype(np.uint8)
    tmp8 = TEST_DIR / "narrow8.png"
    write_png(tmp8, narrow8)
    conv8 = decode_planes(tmp8, "yuv444p10le", w, h, True, f"{config.ZSCALE},format=yuv444p10le")[0]
    info(f"même dégradé étroit quantifié en 8 bits : {np.unique(conv8['Y'][row]).size} valeurs Y distinctes (banding)")

    # (b) Fichiers livrés.
    pr = decode_planes(prores, "yuv422p10le", w, h)
    npf, npn = np.unique(pr[0]["Y"][row]).size, np.unique(pr[2]["Y"][row]).size
    check("[dégradé] ProRes 422 HQ : dégradé plein -> 256 valeurs Y distinctes", npf == 256, f"{npf}")
    check("[dégradé] ProRes 422 HQ : dégradé étroit -> 256 valeurs Y distinctes", npn == 256,
          f"{npn} valeurs, écart max à la cible {int(np.abs(pr[2]['Y'][row].astype(int) - target).max())}")
    hv = decode_planes(hevc, "yuv420p10le", w, h)
    info(f"HEVC Main10 (crf 18, 4:2:0) : dégradé plein {np.unique(hv[0]['Y'][row]).size} valeurs Y distinctes, "
         f"dégradé étroit {np.unique(hv[2]['Y'][row]).size} valeurs, écart max à la cible "
         f"{int(np.abs(hv[2]['Y'][row].astype(int) - target).max())}")
    check("[dégradé] HEVC : extrémités du dégradé plein à 64 et 940 (±2)",
          abs(int(hv[0]["Y"][row][0]) - 64) <= 2 and abs(int(hv[0]["Y"][row][-1]) - 940) <= 2,
          f"{int(hv[0]['Y'][row][0])} .. {int(hv[0]['Y'][row][-1])}")

    # Calage audio : 1 s de source coupée à D = 0,16 s exactement.
    exp_samples = n * config.AUDIO_RATE // fps
    ns = audio_samples(prores)
    check(f"[dégradé] audio ProRes : exactement {exp_samples} échantillons (1 s coupée à 0,16 s)", ns == exp_samples,
          f"{ns}")
    check_common_tags("[dégradé] ProRes 422 HQ", video_stream(probe(prores)), fps, n, prores)


# ---------------------------------------------------------------------------
# Scénario 3 : alpha -> ProRes 4444 ; HEVC aplati sur noir ; sans audio
# ---------------------------------------------------------------------------

def scenario_alpha() -> None:
    sid, w, h, fps, n = "t15_alpha", 1024, 64, 25, 2
    img = np.zeros((h, w, 4), np.uint16)
    ramp = np.rint(np.arange(w) * 65535 / (w - 1)).astype(np.uint16)
    img[: h // 2, :, :3] = 65535                   # haut : blanc, alpha en dégradé 0 -> 65535
    img[: h // 2, :, 3] = ramp[None, :]
    img[h // 2:, : w // 2, :3] = 65535             # bas gauche : blanc TOTALEMENT transparent
    img[h // 2:, : w // 2, 3] = 0
    img[h // 2:, w // 2:, 2] = 65535               # bas droite : rouge (BGRA) à 50 % d'alpha
    img[h // 2:, w // 2:, 3] = 32768
    make_master(sid, [img] * n, fps, True)
    compiled = make_compiled(sid, w, h, fps, n, 16, True,
                             [("prores_4444", "", False), ("hevc_main10", "_flat", True)], None)
    cmds = encode.build_commands(compiled)
    vf_flat = cmds[1]["argv"][cmds[1]["argv"].index("-vf") + 1]
    vf_4444 = cmds[0]["argv"][cmds[0]["argv"].index("-vf") + 1]
    # Aplatissement = préfixe du contrat (RVB × alpha en 16 bits) sans convertisseur d'alpha inexact
    # de FFmpeg 6.1 : alpha lu par extractplanes (maître 16 bits : rgba64be), premultiply à 2 entrées.
    check("[alpha] HEVC aplati : RVB et alpha étendus en 16 bits (alpha par extractplanes), premultiply "
          "à 2 entrées, puis config.ZSCALE ; pas d'audio mappé",
          vf_flat.startswith("split[c][a];[c]format=gbrp16le,") and "[a]format=rgba64be,extractplanes=a," in vf_flat
          and "[c16][a16]premultiply=inplace=0," in vf_flat and "rgba64le" not in vf_flat
          and vf_flat.endswith(f",{config.ZSCALE},format=yuv420p10le")
          and "-map" in cmds[1]["argv"] and "1:a:0" not in cmds[1]["argv"], vf_flat)
    check("[alpha] ProRes 4444 : profil 4, yuva444p10le, alpha_bits 16, couleur par config.ZSCALE, alpha exact "
          "fusionné (mergeplanes), sans aplatissement",
          all(x in " ".join(cmds[0]["argv"]) for x in ["-profile:v 4", "-pix_fmt yuva444p10le", "-alpha_bits 16"])
          and f"{config.ZSCALE},format=yuv444p10le[yuv]" in vf_4444 and "extractplanes=a" in vf_4444
          and vf_4444.endswith("format=yuva444p10le") and "premultiply" not in " ".join(cmds[0]["argv"]), vf_4444)
    t0 = time.perf_counter()
    p4444, hevc = encode.encode_scene(compiled, log=lambda *_: None)
    TIMINGS[sid] = time.perf_counter() - t0

    v4 = video_stream(probe(p4444))
    check("[alpha] ProRes 4444 : profil 4444, format yuva444p10le/12le",
          v4.get("profile") == "4444" and v4["pix_fmt"] in ("yuva444p10le", "yuva444p12le"),
          f"{v4.get('profile')} / {v4['pix_fmt']}")
    check_common_tags("[alpha] ProRes 4444", v4, fps, n, p4444)
    f4 = decode_planes(p4444, "yuva444p10le", w, h)[0]
    a_row = f4["A"][h // 4].astype(int)
    check("[alpha] ProRes 4444 : alpha dégradé de 0 à 1023", a_row.min() == 0 and a_row.max() == 1023
          and a_row[0] == 0 and a_row[-1] == 1023, f"min {a_row.min()} max {a_row.max()}")
    check("[alpha] ProRes 4444 : alpha = rampe idéale au code près (maître 16 bits)",
          bool(np.all(np.diff(a_row) >= 0)) and int(np.abs(a_row - np.arange(w)).max()) == 0,
          f"{np.unique(a_row).size} valeurs distinctes, écart max {int(np.abs(a_row - np.arange(w)).max())}")
    check("[alpha] ProRes 4444 : zones transparente (A = 0) et semi-transparente (A = 512 ±1)",
          int(f4["A"][48, 256]) == 0 and abs(int(f4["A"][48, 768]) - 512) <= 1,
          f"A bas gauche {int(f4['A'][48, 256])}, bas droite {int(f4['A'][48, 768])}")
    check("[alpha] ProRes 4444 : blanc opaque -> Y = 940", int(f4["Y"][h // 4, w - 1]) == 940, f"{int(f4['Y'][h // 4, w - 1])}")

    vh = video_stream(probe(hevc))
    check("[alpha] HEVC aplati : hvc1, Main 10, yuv420p10le",
          (vh.get("codec_tag_string"), vh.get("profile"), vh["pix_fmt"]) == ("hvc1", "Main 10", "yuv420p10le"))
    fh = decode_planes(hevc, "yuv420p10le", w, h)[0]
    y_bl, u_bl, v_bl = int(fh["Y"][48, 256]), int(fh["U"][24, 128]), int(fh["V"][24, 128])
    check("[alpha] aplatissement : blanc à alpha 0 -> noir (Y 64, U/V 512 ±1)",
          abs(y_bl - 64) <= 1 and abs(u_bl - 512) <= 1 and abs(v_bl - 512) <= 1, f"Y {y_bl} U {u_bl} V {v_bl}")
    # Rouge 709 à 50 % sur noir : Y = 64 + 876·0,2126·0,5 ; Cb = 512 − 896·0,1063/1,8556 ; Cr = 512 + 896·0,3937/1,5748.
    a = 32768 / 65535
    ey, eu, ev = 64 + 876 * 0.2126 * a, 512 - 896 * (0.2126 * a) / 1.8556, 512 + 896 * (a - 0.2126 * a) / 1.5748
    y_br, u_br, v_br = int(fh["Y"][48, 768]), int(fh["U"][24, 384]), int(fh["V"][24, 384])
    check("[alpha] aplatissement : rouge à 50 % -> rouge sombre sur noir (±3 codes)",
          abs(y_br - ey) <= 3 and abs(u_br - eu) <= 3 and abs(v_br - ev) <= 3,
          f"mesuré Y {y_br} U {u_br} V {v_br} / attendu {ey:.1f} {eu:.1f} {ev:.1f}")
    yr = fh["Y"][h // 4].astype(float)
    ideal = 64 + 876 * (ramp.astype(float) / 65535)
    check("[alpha] aplatissement : blanc × alpha dégradé -> rampe Y 64..940 (écart max ≤ 3)",
          float(np.abs(yr - ideal).max()) <= 3, f"écart max {float(np.abs(yr - ideal).max()):.2f}")
    check("[alpha] sans audio : aucun flux audio dans les deux fichiers",
          not any(s["codec_type"] == "audio" for p in (p4444, hevc) for s in probe(p)["streams"]))


def scenario_alpha8() -> None:
    """Maître 8 bits à alpha (scène alpha sans flou de mouvement) -> ProRes 4444 : l'alpha doit valoir
    exactement round(a × 1023 / 255) (défaut corrigé sous FFmpeg 6.1 : opaque = 1020, rampe décalée)."""
    sid, w, h, fps, n = "t15_alpha8", 256, 16, 25, 2
    x = np.arange(w, dtype=np.uint8)
    img = np.zeros((h, w, 4), np.uint8)
    img[..., :3] = 255
    img[..., 3] = x[None, :]                                   # blanc, alpha 0..255 selon x
    make_master(sid, [img] * n, fps, True)
    compiled = make_compiled(sid, w, h, fps, n, 8, True, [("prores_4444", "", False)], None)
    vf = encode.build_commands(compiled)[0]["argv"]
    vf = vf[vf.index("-vf") + 1]
    check("[alpha 8 bits] ProRes 4444 : alpha lu en rgba (sortie du décodeur PNG 8 bits)",
          "[a]format=rgba,extractplanes=a,mergeplanes=format=gbrp,format=gbrp," in vf, vf)
    (p4444,) = encode.encode_scene(compiled, log=lambda *_: None)
    f = decode_planes(p4444, "yuva444p10le", w, h)[0]
    a_row = f["A"][h // 2].astype(int)
    ideal = np.floor(x.astype(float) * 1023 / 255 + 0.5).astype(int)
    check("[alpha 8 bits] ProRes 4444 : alpha 0 -> 0 et 255 -> 1023 (opaque réellement opaque)",
          a_row[0] == 0 and a_row[-1] == 1023, f"A[0] {a_row[0]}, A[255] {a_row[-1]}")
    check("[alpha 8 bits] ProRes 4444 : alpha = round(a × 1023 / 255) sur toute la rampe (écart 0)",
          bool(np.array_equal(a_row, ideal)), f"écart max {int(np.abs(a_row - ideal).max())}")
    check("[alpha 8 bits] ProRes 4444 : blanc -> Y = 940, chroma 512 (couleur non prémultipliée)",
          np.unique(f["Y"]).tolist() == [940] and np.unique(np.concatenate([f["U"].ravel(), f["V"].ravel()])).tolist()
          == [512], f"Y {np.unique(f['Y']).tolist()[:4]}")

def scenario_flatten8() -> None:
    """Maître 8 bits à alpha (cas par défaut sans flou de mouvement) aplati pour HEVC et ProRes 422 HQ :
    le blanc opaque doit rester à Y = 940 (défaut corrigé : l'expansion swscale donnait 937)."""
    sid, w, h, fps, n = "t15_flat8", 256, 64, 25, 4
    x = np.arange(w, dtype=np.uint8)
    # Un motif par image (pas de bandes empilées) : aucune arête horizontale franche dont le
    # rebond de compression HEVC fausserait la mesure des niveaux.
    white = np.full((h, w, 4), 255, np.uint8)                  # image 0 : blanc opaque
    black = np.zeros((h, w, 4), np.uint8)                      # image 1 : noir opaque
    black[..., 3] = 255
    veil = np.full((h, w, 4), 255, np.uint8)                   # image 2 : blanc, alpha 0..255 selon x
    veil[..., 3] = x[None, :]
    gray = np.zeros((h, w, 4), np.uint8)                       # image 3 : gris v = x, opaque
    gray[..., :3] = x[None, :, None]
    gray[..., 3] = 255
    make_master(sid, [white, black, veil, gray], fps, True)
    compiled = make_compiled(sid, w, h, fps, n, 8, True,
                             [("hevc_main10", "_flat", True), ("prores_422hq", "_flat", True)], None)
    cmds = encode.build_commands(compiled)
    check("[aplat 8 bits] build_commands : clés {profile, output, argv}, argv écrit dans output (relatif à la racine)",
          all(set(c) == {"profile", "output", "argv"} and c["argv"][-1] == Path(c["output"]).relative_to(ROOT).as_posix()
              and c["argv"][0] == config.FFMPEG for c in cmds),
          f"clés {sorted(cmds[0])} ; dernier argument {cmds[0]['argv'][-1]}")
    t0 = time.perf_counter()
    hevc, prores = encode.encode_scene(compiled, log=lambda *_: None)
    TIMINGS[sid] = time.perf_counter() - t0
    ideal_alpha = 64 + 876 * x.astype(float) / 255
    # HEVC (crf 18, avec perte) : ±2 codes, extrémités de rampe comprises ; ProRes 422 HQ : ±1 et
    # extrémités exactes (64 / 940).
    for name, path, fmt, tol, exact_ends in (("HEVC", hevc, "yuv420p10le", 2.0, False),
                                             ("ProRes 422 HQ", prores, "yuv422p10le", 1.0, True)):
        fr = decode_planes(path, fmt, w, h)
        wy, by = np.unique(fr[0]["Y"]), np.unique(fr[1]["Y"])
        uv = np.unique(np.concatenate([fr[i][c].ravel() for i in (0, 1) for c in ("U", "V")]))
        check(f"[aplat 8 bits] {name} : blanc opaque aplati -> Y = 940 (tous les pixels)", wy.tolist() == [940],
              str(wy.tolist()[:8]))
        check(f"[aplat 8 bits] {name} : noir opaque aplati -> Y = 64", by.tolist() == [64], str(by.tolist()[:8]))
        check(f"[aplat 8 bits] {name} : chroma du blanc/noir = 512", uv.tolist() == [512], str(uv.tolist()[:8]))
        ya = fr[2]["Y"][h // 2].astype(float)
        ends_tol = 0 if exact_ends else tol
        check(f"[aplat 8 bits] {name} : blanc × alpha 0..255 -> Y = 64 + 876·a/255 (écart max ≤ {tol:g}), "
              f"alpha 0 -> 64, alpha 255 -> 940 (±{ends_tol:g})",
              float(np.abs(ya - ideal_alpha).max()) <= tol and abs(ya[0] - 64) <= ends_tol
              and abs(ya[-1] - 940) <= ends_tol,
              f"écart max {float(np.abs(ya - ideal_alpha).max()):.2f}, Y[0] {int(ya[0])}, Y[255] {int(ya[-1])}")
        yg = fr[3]["Y"][h // 2].astype(float)
        check(f"[aplat 8 bits] {name} : gris opaque v -> Y = 64 + 876·v/255 (écart max ≤ {tol:g})",
              float(np.abs(yg - ideal_alpha).max()) <= tol, f"écart max {float(np.abs(yg - ideal_alpha).max()):.2f}")

    # argv de build_commands lancé TEL QUEL (cwd = racine) : il produit bien le fichier `output`.
    ref = decoded_md5(prores, "0:v:0")
    prores.unlink()
    r = run(cmds[1]["argv"], cwd=str(config.ROOT))
    ok = r.returncode == 0 and prores.is_file()
    check("[aplat 8 bits] argv de build_commands lancé tel quel depuis la racine -> fichier output créé, "
          "images identiques (md5 décodé)", ok and decoded_md5(prores, "0:v:0") == ref,
          "" if ok else r.stderr.decode(errors="replace")[-300:])


def scenario_fps30() -> None:
    """D non décimal (100 images à 30 i/s = 3,333… s) : le calage audio doit rester exact."""
    sid, w, h, fps, n = "t15_fps30", 64, 64, 30, 100
    gray = np.full((h, w, 4), 128, np.uint8)
    gray[..., 3] = 255
    make_master(sid, [gray] * n, fps, False)
    audio = TEST_DIR / "audio" / "sine_1.0s.wav"
    if not audio.is_file():
        make_sine(audio, 1.0)
    compiled = make_compiled(sid, w, h, fps, n, 8, False,
                             [("prores_422hq", "", False), ("hevc_main10", "", False)], audio)
    argv = " ".join(encode.build_commands(compiled)[0]["argv"])
    check("[30 i/s] D = 3.333333 (résolution microseconde de FFmpeg) dans apad/atrim",
          "apad=whole_dur=3.333333,atrim=end=3.333333" in argv)
    check("[30 i/s] keyint=60:min-keyint=30", "keyint=60:min-keyint=30" in " ".join(encode.build_commands(compiled)[1]["argv"]))
    prores, hevc = encode.encode_scene(compiled, log=lambda *_: None)
    ns = audio_samples(prores)
    check("[30 i/s] audio ProRes : exactement 160000 échantillons (100 × 1600)", ns == 160000, f"{ns}")
    vs = video_stream(probe(prores))
    check("[30 i/s] ProRes : 100 images, 30/1", vs.get("nb_read_frames") == "100" and vs.get("r_frame_rate") == "30/1",
          f"{vs.get('nb_read_frames')} images, {vs.get('r_frame_rate')}")
    ph = probe(hevc)
    au = next(s for s in ph["streams"] if s["codec_type"] == "audio")
    dur = float(au.get("duration", "nan"))
    check("[30 i/s] audio HEVC (AAC) : durée à ±(1 image + 25 ms) de 3,333 s", abs(dur - n / fps) <= 1 / fps + 0.025,
          f"durée {dur:.6f} s, {audio_samples(hevc)} échantillons décodés")


def scenario_errors() -> None:
    compiled = make_compiled("t15_absent", 64, 64, 25, 5, 8, False, [("hevc_main10", "", False)], None)
    try:
        encode.encode_scene(compiled, log=lambda *_: None)
        check("[erreurs] séquence maître absente refusée", False, "aucune erreur")
    except encode.EncodeError as exc:
        check("[erreurs] séquence maître absente refusée avec message actionnable", "composition" in str(exc),
              str(exc).splitlines()[0])
    check("[durée] D exact : 165/25 -> 6.6, 150/30 -> 5, 72/24 -> 3, 50/25 -> 2, 100/30 -> 3.333333",
          [encode.duration_text(*a) for a in ((165, 25), (150, 30), (72, 24), (50, 25), (100, 30))]
          == ["6.6", "5", "3", "2", "3.333333"])

    # Maître périmé : opaque 8 bits alors que la scène est alpha 16 bits -> refus (jamais d'encodage faux).
    sid = "t15_err"
    gray = np.full((16, 16, 4), 128, np.uint8)
    gray[..., 3] = 255
    make_master(sid, [gray] * 2, 25, False)
    stale = make_compiled(sid, 16, 16, 25, 2, 16, True, [("prores_4444", "", False)], None)
    try:
        encode.encode_scene(stale, log=lambda *_: None)
        check("[erreurs] maître périmé (profondeur/alpha) refusé", False, "aucune erreur : maître faux encodé")
    except encode.EncodeError as exc:
        msg = str(exc)
        check("[erreurs] maître périmé (profondeur/alpha) refusé avec message actionnable",
              "périmée" in msg and "depth" in msg and "alpha" in msg and "composition" in msg, msg[:220])

    # FFmpeg introuvable -> EncodeError en français (pas de FileNotFoundError brute).
    ok_compiled = make_compiled(sid, 16, 16, 25, 2, 8, False, [("prores_422hq", "", False)], None)
    saved = config.FFMPEG
    config.FFMPEG = "ffmpeg_introuvable_xyz"
    try:
        encode.encode_scene(ok_compiled, log=lambda *_: None)
        check("[erreurs] FFmpeg introuvable -> EncodeError", False, "aucune erreur")
    except encode.EncodeError as exc:
        check("[erreurs] FFmpeg introuvable -> EncodeError actionnable (MOGRAPH_FFMPEG)",
              "introuvable" in str(exc) and "MOGRAPH_FFMPEG" in str(exc), str(exc)[:160])
    except Exception as exc:  # noqa: BLE001 - on veut justement détecter une exception brute
        check("[erreurs] FFmpeg introuvable -> EncodeError", False, f"exception brute {type(exc).__name__}: {exc}")
    finally:
        config.FFMPEG = saved

    # Livrable ouvert dans un autre programme (Windows : remplacement impossible) -> EncodeError, pas de .partial.
    out = encode.encode_scene(ok_compiled, log=lambda *_: None)[0]
    size = out.stat().st_size
    with open(out, "rb"):
        try:
            encode.encode_scene(ok_compiled, log=lambda *_: None)
            info("livrable ouvert : remplacement accepté par le système (pas de verrou) ")
            locked_ok = True
        except encode.EncodeError as exc:
            locked_ok = "fermez" in str(exc).lower()
            print(f"      message : {str(exc)[:200]}")
        except Exception as exc:  # noqa: BLE001
            locked_ok = False
            print(f"      exception brute {type(exc).__name__}: {exc}")
    check("[erreurs] livrable verrouillé -> EncodeError actionnable (« fermez ... »), livrable intact, aucun .partial",
          locked_ok and out.stat().st_size == size and not list(out.parent.glob("*.partial.*")))


def main() -> int:
    if TEST_DIR.exists():
        shutil.rmtree(TEST_DIR)
    TEST_DIR.mkdir(parents=True)
    bash = find_bash()
    info(f"bash utilisé pour rejouer le script : {bash}")
    for fn, args in ((scenario_levels, (bash,)), (scenario_gradient, ()), (scenario_alpha, ()), (scenario_alpha8, ()), (scenario_flatten8, ()),
                     (scenario_fps30, ()), (scenario_errors, ())):
        try:
            fn(*args)
        except Exception:
            traceback.print_exc()
            check(f"{fn.__name__} s'exécute sans exception", False)
    print()
    info("temps d'encodage : " + ", ".join(f"{k} {v:.2f} s" for k, v in TIMINGS.items()))
    if FAILS:
        print(f"ÉCHEC : {len(FAILS)} contrôle(s) en échec : {FAILS}")
        return 1
    print("SUCCÈS : tous les contrôles de l'étape 15 passent.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
