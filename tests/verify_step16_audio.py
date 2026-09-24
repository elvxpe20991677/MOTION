"""Vérification de l'étape 16 : pipeline/audio.py (piste de test aevalsrc + mesures).

Lancement (depuis la racine) : .venv/Scripts/python.exe tests/verify_step16_audio.py
Fichiers temporaires : build/_tests/step16/ (recréé à chaque lancement).
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
TEST_DIR = ROOT / "build" / "_tests" / "step16"
os.environ["MOGRAPH_BUILD_DIR"] = str(TEST_DIR / "build")
os.environ["MOGRAPH_OUT_DIR"] = str(TEST_DIR / "out")
sys.path.insert(0, str(ROOT))

import numpy as np  # noqa: E402

from pipeline import audio  # noqa: E402
from pipeline import config  # noqa: E402

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
FAILS: list[str] = []
TIMINGS: dict[str, float] = {}
TOL_S = 0.005


def check(label: str, ok: bool, detail: str = "") -> bool:
    print(f"{'PASS' if ok else 'FAIL'} {label}" + (f" -- {detail}" if detail else ""))
    if not ok:
        FAILS.append(label)
    return ok


def info(text: str) -> None:
    print(f"INFO {text}")


def md5(path: Path) -> str:
    return hashlib.md5(path.read_bytes()).hexdigest()


def wav_samples(path: Path) -> int:
    x, rate = audio.read_wav(path)
    assert rate == 48000
    return x.shape[0]


def timed(name: str, fn, *a, **kw):
    t0 = time.perf_counter()
    r = fn(*a, **kw)
    TIMINGS[name] = time.perf_counter() - t0
    return r


def compare_onsets(label: str, found: list[float], expected: list[float]) -> None:
    ok = len(found) == len(expected)
    err = max((abs(f - e) for f, e in zip(found, expected)), default=float("inf"))
    check(f"{label} : {len(expected)} attaques aux temps attendus (tolérance 5 ms)", ok and err <= TOL_S,
          f"{len(found)} trouvées, écart max {err * 1000:.2f} ms ; premières : {[round(t, 4) for t in found[:6]]}")


def scenario_beat_120() -> None:
    a, b = TEST_DIR / "beat_a.wav", TEST_DIR / "beat_b.wav"
    timed("synth 8 s (1re génération)", audio.synth_beat, a, 120, 8.0)
    timed("synth 8 s (2e génération)", audio.synth_beat, b, 120, 8.0)
    ma, mb = md5(a), md5(b)
    check("[120 BPM] deux générations -> même md5", ma == mb, f"{ma} / {mb}")
    n = wav_samples(a)
    check("[120 BPM] durée exacte : 8 s = 384000 échantillons", n == 384000, f"{n} échantillons")

    p = subprocess.run([config.FFPROBE, "-v", "error", "-show_streams", "-show_format", "-of", "json", str(a)],
                       capture_output=True)
    pr = json.loads(p.stdout)
    st = pr["streams"][0]
    check("[120 BPM] WAV pcm_s24le 48 kHz stéréo", (st["codec_name"], st["sample_rate"], st["channels"])
          == ("pcm_s24le", "48000", 2), f"{st['codec_name']} {st['sample_rate']} Hz {st['channels']} canaux")
    raw = a.read_bytes()
    check("[120 BPM] -bitexact : aucune chaîne de version (pas de bloc LIST/INFO « Lavf »)",
          b"LIST" not in raw[:200] and b"Lavf" not in raw and "encoder" not in pr["format"].get("tags", {}))

    loud = audio.measure_loudness(a)
    check("[120 BPM] loudness intégrée = -14 LUFS ±0,5", abs(loud["integrated_lufs"] + 14.0) <= 0.5, str(loud))
    check("[120 BPM] true peak ≤ -2 dBTP (marge AAC)", loud["true_peak_dbtp"] <= -2.0, f"{loud['true_peak_dbtp']} dBTP")

    onsets = timed("detect_onsets 8 s", audio.detect_onsets, a)
    compare_onsets("[120 BPM] attaques 0, 0,5, 1,0, 1,5 ... 7,5 s", onsets, [k * 0.5 for k in range(16)])

    # Accent du 1er temps de la mesure et charleston des contretemps, mesurés sur le signal.
    x, rate = audio.read_wav(a)
    y = x.mean(axis=1)
    per = int(0.5 * rate)
    peaks = [float(np.abs(y[k * per:k * per + int(0.1 * rate)]).max()) for k in range(16)]
    acc = [peaks[k] for k in range(16) if k % 4 == 0]
    oth = [peaks[k] for k in range(16) if k % 4 != 0]
    check("[120 BPM] accent : 1er temps de chaque mesure plus fort que les autres (≥ +2 dB)",
          min(acc) / max(oth) >= 10 ** (2 / 20), f"crêtes accent {min(acc):.3f}, autres {max(oth):.3f} "
          f"({20 * np.log10(min(acc) / max(oth)):+.2f} dB)")
    hf = np.diff(y) ** 2          # dérivée : met en avant les aigus (charleston), écrase la grosse caisse
    win = int(0.03 * rate)
    off = [hf[k * per + per // 2:k * per + per // 2 + win].mean() for k in range(16)]
    quarter = [hf[k * per + per // 4:k * per + per // 4 + win].mean() for k in range(16)]
    ratio_db = 10 * np.log10(min(off) / max(max(quarter), 1e-20))
    check("[120 BPM] charleston sur les contretemps : énergie aiguë au contretemps ≫ au quart de temps (> 20 dB)",
          ratio_db > 20, f"{ratio_db:.1f} dB")
    full = audio.detect_onsets(a, lowpass_hz=None)
    info(f"attaques en pleine bande (lowpass_hz=None) : {len(full)} ; le charleston est masqué par la queue de la "
         f"grosse caisse au-dessus de -30 dB")


def scenario_variants() -> None:
    c = TEST_DIR / "beat_100_offset.wav"
    timed("synth 100 BPM offset 0,25 s", audio.synth_beat, c, 100, 5.0, beats_per_bar=3, offset=0.25)
    exp = [0.25 + k * 0.6 for k in range(8) if 0.25 + k * 0.6 < 5.0]
    compare_onsets("[100 BPM, offset 0,25 s, 3/4] attaques 0,25 + 0,6·k", audio.detect_onsets(c), exp)
    check("[100 BPM] durée exacte : 5 s = 240000 échantillons", wav_samples(c) == 240000, f"{wav_samples(c)}")

    d = TEST_DIR / "beat_23.wav"
    timed("synth -23 LUFS", audio.synth_beat, d, 120, 4.0, target_lufs=-23.0)
    loud = audio.measure_loudness(d)
    check("[cible -23 LUFS] loudness ±0,5 et crête ≤ -2 dBTP",
          abs(loud["integrated_lufs"] + 23.0) <= 0.5 and loud["true_peak_dbtp"] <= -2.0, str(loud))

    e = TEST_DIR / "beat_frac.wav"
    audio.synth_beat(e, 120, 100 / 30)
    check("[durée] 100 images à 30 i/s (3,333… s) -> exactement 160000 échantillons", wav_samples(e) == 160000,
          f"{wav_samples(e)}")

    graph = audio._source_graph(120, 4, 0.0, 48000, *audio.SYNTH_LEVELS[0])
    check("[filtre] expression aevalsrc ENTRE APOSTROPHES (ses virgules ne séparent pas les filtres)",
          graph.startswith("aevalsrc=exprs='") and "'|'" not in graph and graph.count("'") == 2 and "," in graph.split("'")[1])

    # Cible la plus forte admise par le schéma (-5 LUFS) : grosse caisse tenue + limiteur déterministe.
    f1, f2 = TEST_DIR / "beat_m5_a.wav", TEST_DIR / "beat_m5_b.wav"
    timed("synth -5 LUFS (limiteur)", audio.synth_beat, f1, 120, 4.0, target_lufs=-5.0)
    audio.synth_beat(f2, 120, 4.0, target_lufs=-5.0)
    loud5 = audio.measure_loudness(f1)
    check("[cible -5 LUFS] atteinte (limiteur) : loudness ±0,5 et crête ≤ -2 dBTP",
          abs(loud5["integrated_lufs"] + 5.0) <= 0.5 and loud5["true_peak_dbtp"] <= -2.0, str(loud5))
    check("[cible -5 LUFS] deux générations -> même md5 (limiteur déterministe)", md5(f1) == md5(f2))
    check("[cible -5 LUFS] durée exacte : 4 s = 192000 échantillons", wav_samples(f1) == 192000, f"{wav_samples(f1)}")
    compare_onsets("[cible -5 LUFS] attaques toujours à 0, 0,5 ... 3,5 s", audio.detect_onsets(f1),
                   [k * 0.5 for k in range(8)])
    m4a = TEST_DIR / "beat_m5.m4a"
    p = subprocess.run([config.FFMPEG, "-v", "error", "-nostdin", "-y", "-i", str(f1), "-c:a", "aac", "-b:a", "320k",
                        "-ar", "48000", str(m4a)], capture_output=True)
    la = audio.measure_loudness(m4a) if p.returncode == 0 else {"true_peak_dbtp": float("nan")}
    check("[cible -5 LUFS] après AAC 320k (comme le livrable HEVC) : crête vraie ≤ -1 dBTP (seuil QC)",
          la["true_peak_dbtp"] <= config.TRUE_PEAK_MAX_DBTP, str(la))
    check("[cible -5 LUFS] aucun fichier intermédiaire (.tmp) laissé", not list(TEST_DIR.glob("*.tmp")))

    # Hors schéma (-2 LUFS) : refus actionnable.
    try:
        audio.synth_beat(TEST_DIR / "trop_fort.wav", 120, 2.0, target_lufs=-2.0)
        check("[cible -2 LUFS] refusée (crête vraie impossible à tenir)", False, "aucune erreur")
    except audio.AudioError as exc:
        check("[cible -2 LUFS] refusée avec message actionnable", "cible moins forte" in str(exc), str(exc)[:160])

    # Piste < 400 ms : message dédié (loudness intégrée non mesurable), pas une fausse histoire de crête.
    try:
        audio.synth_beat(TEST_DIR / "court.wav", 120, 0.3)
        check("[durée 0,3 s] refusée", False, "aucune erreur")
    except audio.AudioError as exc:
        check("[durée 0,3 s] refusée avec message dédié (blocs de 400 ms, allonger la scène)",
              "400 ms" in str(exc) and "Allongez" in str(exc) and "crête" not in str(exc), str(exc)[:160])
    g = TEST_DIR / "court_ok.wav"
    audio.synth_beat(g, 120, 0.4)
    lg = audio.measure_loudness(g)
    check("[durée 0,4 s] acceptée et calée (-14 LUFS ±0,5)", abs(lg["integrated_lufs"] + 14.0) <= 0.5, str(lg))


def compiled_with_audio(src: Path | None, *, bpm=120.0, synth=True, frames=100, fps=25) -> dict:
    return {
        "contract": "mograph-scene/1", "scene_id": "t16_scene", "format": {"fps": fps, "alpha": False},
        "frames": frames, "duration": frames / fps, "qc": {"loudness_target_lufs": -14.0},
        "audio": None if src is None else {"src": str(src), "bpm": bpm, "offset": 0.0, "beats_per_bar": 4,
                                            "markers": [], "synth": {"kind": "beat"} if synth else None,
                                            "target_lufs": -14.0},
    }


def scenario_ensure() -> None:
    quiet = lambda *_: None  # noqa: E731
    check("[ensure] scène sans audio -> None", audio.ensure_scene_audio(compiled_with_audio(None), log=quiet) is None)
    src = TEST_DIR / "assets" / "audio" / "scene_beat.wav"
    got = timed("ensure_scene_audio (génération 4 s)", audio.ensure_scene_audio, compiled_with_audio(src), log=print)
    check("[ensure] piste absente + synth -> générée (100 images à 25 i/s = 192000 échantillons)",
          got == src and src.is_file() and wav_samples(src) == 192000, f"{wav_samples(src) if src.is_file() else 'absente'}")
    onsets = audio.detect_onsets(src)
    compare_onsets("[ensure] bpm de la scène respecté (120 BPM)", onsets, [k * 0.5 for k in range(8)])
    before = (src.stat().st_mtime_ns, md5(src))
    audio.ensure_scene_audio(compiled_with_audio(src), log=quiet)
    check("[ensure] second appel : piste existante conservée (non régénérée)",
          (src.stat().st_mtime_ns, md5(src)) == before)
    audio.ensure_scene_audio(compiled_with_audio(src, bpm=90.0), log=print)
    check("[ensure] bpm modifié -> piste générée par le pipeline régénérée",
          md5(src) != before[1] and abs(audio.detect_onsets(src)[1] - 60 / 90) <= TOL_S)
    sidecar = src.with_name(src.name + ".synth.json")
    rec = json.loads(sidecar.read_text(encoding="utf-8")) if sidecar.is_file() else {}
    check("[ensure] fiche .synth.json : paramètres + empreinte SHA-256 de la piste générée",
          rec.get("wav_sha256") == hashlib.sha256(src.read_bytes()).hexdigest() and rec.get("params", {}).get("bpm") == 90.0,
          str({k: (v if k != "params" else "...") for k, v in rec.items()}))

    # L'utilisateur remplace la piste générée par SA musique (même nom), puis change le bpm :
    # le fichier ne doit JAMAIS être écrasé (perte de données), un avertissement l'explique.
    subprocess.run([config.FFMPEG, "-v", "error", "-nostdin", "-y", "-f", "lavfi", "-i", "sine=f=330:d=2",
                    "-c:a", "pcm_s24le", "-ar", "48000", str(src)], check=True)
    user_md5 = md5(src)
    logs: list[str] = []
    got = audio.ensure_scene_audio(compiled_with_audio(src, bpm=97.0), log=logs.append)
    check("[ensure] piste générée remplacée par l'utilisateur + bpm modifié -> fichier utilisateur conservé",
          got == src and md5(src) == user_md5, f"md5 {'inchangé' if md5(src) == user_md5 else 'MODIFIÉ'}")
    check("[ensure] ... avec un avertissement actionnable", any("remplacé" in m and "NON régénéré" in m for m in logs),
          logs[0][:160] if logs else "aucun message")

    # Bloc audio « tempo seul » (schéma : bpm sans src, pour les références beat:) -> None, pas d'exception.
    tempo_only = compiled_with_audio(src, synth=False)
    tempo_only["audio"]["src"] = None
    logs = []
    check("[ensure] tempo seul (src = None, sans synth) -> None + message", audio.ensure_scene_audio(tempo_only, log=logs.append)
          is None and any("tempo seul" in m for m in logs), logs[0] if logs else "aucun message")
    del tempo_only["audio"]["src"]
    check("[ensure] tempo seul (clé src absente) -> None", audio.ensure_scene_audio(tempo_only, log=quiet) is None)
    synth_no_src = compiled_with_audio(src)
    synth_no_src["audio"]["src"] = None
    try:
        audio.ensure_scene_audio(synth_no_src, log=quiet)
        check("[ensure] synth sans chemin src refusé", False, "aucune erreur")
    except audio.AudioError as exc:
        check("[ensure] synth sans chemin src refusé avec message actionnable (recompiler)", "recompilez" in str(exc),
              str(exc)[:120])

    user = TEST_DIR / "assets" / "audio" / "user.wav"
    shutil.copyfile(TEST_DIR / "beat_a.wav", user)
    m0 = md5(user)
    audio.ensure_scene_audio(compiled_with_audio(user, bpm=60.0), log=quiet)
    check("[ensure] fichier fourni par l'utilisateur (sans .synth.json) jamais écrasé", md5(user) == m0)
    try:
        audio.ensure_scene_audio(compiled_with_audio(TEST_DIR / "absent.wav", synth=False), log=quiet)
        check("[ensure] piste absente sans synth refusée", False, "aucune erreur")
    except audio.AudioError as exc:
        check("[ensure] piste absente sans synth refusée avec message actionnable", "synth" in str(exc), str(exc)[:120])
    try:
        audio.ensure_scene_audio(compiled_with_audio(TEST_DIR / "absent2.wav", bpm=None), log=quiet)
        check("[ensure] synth sans bpm refusé", False, "aucune erreur")
    except audio.AudioError as exc:
        check("[ensure] synth sans bpm refusé avec message actionnable", "bpm" in str(exc), str(exc)[:120])


def main() -> int:
    if TEST_DIR.exists():
        shutil.rmtree(TEST_DIR)
    TEST_DIR.mkdir(parents=True)
    for fn in (scenario_beat_120, scenario_variants, scenario_ensure):
        try:
            fn()
        except Exception:
            traceback.print_exc()
            check(f"{fn.__name__} s'exécute sans exception", False)
    print()
    info("temps : " + ", ".join(f"{k} {v:.2f} s" for k, v in TIMINGS.items()))
    if FAILS:
        print(f"ÉCHEC : {len(FAILS)} contrôle(s) en échec : {FAILS}")
        return 1
    print("SUCCÈS : tous les contrôles de l'étape 16 passent.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
