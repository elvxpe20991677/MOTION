"""Vérification de la phase 5 : musique gelée (import manuel) et repères (mograph beats --write).

Lancement depuis la racine :  .venv/Scripts/python.exe tests/verify_step50_music.py
Aucune IA n'est appelée : la « musique d'IA » est une piste de test générée puis encodée en MP3, gelée
comme le serait un téléchargement Eleven Music (source manual_import). Isolé : build/_tests/step50/,
dossier IA assets/_tests/ai50/, verrou de test.
Contrôles :
  A. reproductibilité : analyse identique au bit près dans 3 processus, WAV et MP3, tempo non entier ;
  B. gel exigé : musique sous le dossier IA non gelée -> beats REFUSE ; gelée -> analysée ;
  C. beats --write : bloc audio (src = clé du verrou, bpm, offset, drop, fiche d'analyse), autres
     marqueurs gardés, synth retiré, BOM accepté, écriture atomique ; --write sans --scene refusé ;
     --json --write = un seul objet JSON ; ancien drop retiré ; options bornées et reprises de la scène ;
     scène illisible refusée AVANT l'analyse ; piste hors du projet refusée ; scène en lecture seule ;
  D. dérive temps par temps : tempo fixe (court, intro muette, 5 min) < 5 ms sans avertissement ;
     tempo variable, ralenti final, demi-temps > 40 ms ; morceau trop court : « non mesurée » (null) ;
     unitaire sur une enveloppe synthétique ; seuil normatif 40 ms ;
  E. compilation : beat:N calé sur la musique gelée ; fichier analysé modifié -> avertissement dont la
     commande de relance fonctionne telle quelle ; grille retouchée à la main -> avertissement ; musique
     gelée modifiée -> REFUS (verrou) ;
  F. rendu complet (all) avec la musique gelée MP3 : déterminisme, QC CONFORME, provenance au rapport.
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
TEST = ROOT / "build" / "_tests" / "step50"
AI = ROOT / "assets" / "_tests" / "ai50"
PLAIN = ROOT / "assets" / "_tests" / "m50"          # musiques NON IA (hors dossier IA) du test
os.environ.update({
    "MOGRAPH_BUILD_DIR": str(TEST / "build"), "MOGRAPH_OUT_DIR": str(TEST / "out"),
    "MOGRAPH_AI_ASSETS_DIR": str(AI), "MOGRAPH_ASSETS_LOCK": str(TEST / "assets.lock.json"),
    "PYTHONIOENCODING": "utf-8"})
sys.path.insert(0, str(ROOT))

from pipeline import assets, audio, config  # noqa: E402  (après l'environnement)
from pipeline import scene as scene_mod  # noqa: E402

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

PY = sys.executable
FAILS: list[str] = []
QC = {"safe_zone": "title", "safe_zone_tolerance_s": 0.5, "allow_black_s": 0, "determinism_frames": 3,
      "loudness_target_lufs": -14}
LICENSE = "Conditions Eleven Music, offre payante : usage commercial hors film, TV, radio et jeux « Studio »"


def check(label: str, ok: bool, detail: str = "") -> bool:
    print(("PASS " if ok else "FAIL ") + label + (f" -- {detail}" if detail else ""), flush=True)
    if not ok:
        FAILS.append(label)
    return ok


def mograph(*args: str, timeout: float = 1200) -> subprocess.CompletedProcess:
    return subprocess.run([PY, str(ROOT / "mograph.py"), *args], cwd=ROOT, env=dict(os.environ), capture_output=True,
                          stdin=subprocess.DEVNULL, text=True, encoding="utf-8", errors="replace", timeout=timeout)


def tail(cp, n=2) -> str:
    return " | ".join((cp.stdout + cp.stderr).strip().splitlines()[-n:])


def ffmpeg(*args: str) -> None:
    subprocess.run([config.FFMPEG, "-v", "error", "-y", *args], check=True)


def scene(sid: str, *, audio_block=None, layers=None, duration=4.0, bom=False) -> Path:
    d = {"version": "1.0", "id": sid, "title": sid, "brief": "test phase 5", "seed": 7, "preset": "flat_riso",
         "format": {"aspect": "16:9", "resolution": "1080p", "fps": 25, "alpha": False},
         "outputs": [{"profile": "hevc_main10"}], "qc": QC,
         "provenance": {"ai_disclosure": {"ai_used": True, "roles": ["music"], "date": "2026-10-03",
                                          "tools": [{"name": "ElevenLabs Music", "model": "music_v1", "role": "musique"}]}},
         "shots": [{"id": "a", "duration": duration, "layers": layers or [{"id": "t", "type": "text", "text": "Ondea"}]}]}
    if audio_block is not None:
        d["audio"] = audio_block
    p = TEST / "scenes" / f"{sid}.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(("﻿" if bom else "") + json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
    return p


def clicks(path: Path, times, duration: float, extra=None) -> Path:
    """Grosses caisses (sinus 60 Hz amorti) aux instants donnés ; extra = [(instant, gain)]."""
    import numpy as np
    sr = 48000
    tt = np.arange(int(0.12 * sr)) / sr
    k = (np.sin(2 * np.pi * 60 * tt) * np.exp(-tt / 0.035)).astype(np.float32)
    y = np.zeros(int(duration * sr) + sr, np.float32)
    for at, g in [(x, 1.0) for x in times] + list(extra or []):
        i = int(round(at * sr))
        if 0 <= i < y.size:
            seg = k[: y.size - i]
            y[i:i + seg.size] += g * seg
    subprocess.run([config.FFMPEG, "-v", "error", "-y", "-f", "f32le", "-ar", str(sr), "-ac", "1", "-i", "pipe:0",
                    "-c:a", "pcm_s16le", str(path)], input=(y[: int(duration * sr)] * 0.5).tobytes(), check=True)
    return path


def make_tracks() -> dict[str, Path]:
    PLAIN.mkdir(parents=True, exist_ok=True)
    src = TEST / "src"
    src.mkdir(parents=True, exist_ok=True)
    t = {}
    t["c973_wav"] = audio.synth_beat(src / "c973.wav", 97.3, 30, offset=0.25)
    t["c973_mp3"] = src / "c973.mp3"
    ffmpeg("-i", str(t["c973_wav"]), "-c:a", "libmp3lame", "-b:a", "192k", "-ar", "44100", str(t["c973_mp3"]))
    c120 = audio.synth_beat(src / "c120.wav", 120, 16)
    c126 = audio.synth_beat(src / "c126.wav", 126, 16)
    lst = src / "liste.txt"
    lst.write_text(f"file '{c120.as_posix()}'\nfile '{c126.as_posix()}'\n", encoding="utf-8")
    t["var"] = PLAIN / "variable.wav"
    ffmpeg("-f", "concat", "-safe", "0", "-i", str(lst), "-c", "copy", str(t["var"]))
    # « Téléchargement Eleven Music » simulé : MP3 44,1 kHz avec une montée à 8 s (drop).
    t["music"] = src / "musique_eleven.mp3"
    ffmpeg("-i", str(c120), "-af", "volume='if(lt(t,8),0.25,1)':eval=frame", "-c:a", "libmp3lame", "-b:a", "192k",
           "-ar", "44100", str(t["music"]))
    t["plain"] = PLAIN / "piste.wav"
    shutil.copyfile(c120, t["plain"])
    t["nodrop"] = PLAIN / "sans_drop.wav"
    shutil.copyfile(c120, t["nodrop"])
    return t


def test_reproducible(t: dict) -> None:
    print("== A. Reproductibilité de l'analyse (3 processus) ==")
    for key in ("c973_wav", "c973_mp3"):
        outs = [mograph("beats", str(t[key]), "--json") for _ in range(3)]
        js = [o.stdout for o in outs]
        r = json.loads(js[0])["analyse"] if outs[0].returncode == 0 else {}
        check(f"{key} : sortie JSON identique au bit près dans 3 processus, 97,3 BPM (± 0,1), temps 0 à 0,25 s (± 10 ms)",
              all(o.returncode == 0 for o in outs) and len(set(js)) == 1 and abs(r.get("bpm", 0) - 97.3) <= 0.1
              and abs(r.get("offset", 9) - 0.25) <= 0.010, str(r))


def test_freeze(t: dict) -> str:
    print("== B. Gel exigé sous le dossier IA ==")
    loose = AI / "audio" / "pas_gelee.mp3"
    loose.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(t["music"], loose)
    cp = mograph("beats", str(loose))
    check("musique sous le dossier IA non gelée : beats REFUSE (code 1, « assets add »)",
          cp.returncode == 1 and "assets add" in cp.stderr and "non gelée" in cp.stderr, tail(cp, 1))
    loose.unlink()
    cp = mograph("assets", "add", str(t["music"]), "--tool", "ElevenLabs Music", "--model", "music_v1",
                 "--prompt", "Electro 120 BPM, montée à 8 s", "--license", LICENSE, "--source", "manual_import",
                 "--cost", "1 génération (crédits de l'abonnement)", "--kind", "audio")
    key = assets.lock_key(AI / "audio" / t["music"].name)
    e = assets.load_lock()["assets"].get(key) or {}
    check("import manuel gelé (assets add) : clé sous audio/, source manual_import, outil et licence consignés",
          cp.returncode == 0 and e.get("kind") == "audio" and e["provenance"]["source"] == "manual_import"
          and e["provenance"]["license"] == LICENSE, tail(cp, 1))
    cp = mograph("beats", key)
    check("musique gelée : analysée (120 BPM, drop à 8 s)", cp.returncode == 0 and "120" in cp.stdout and "drop à 8.0 s" in cp.stdout,
          tail(cp, 1))
    return key


def test_write(key: str) -> Path:
    print("== C. beats --write ==")
    cp = mograph("beats", key, "--write")
    check("--write sans --scene : refus (code 1)", cp.returncode == 1 and "--scene" in cp.stderr, tail(cp, 1))
    # normalize : l'extrait (4 s, avant la montée) est plus calme que la cible de loudness.
    p = scene("m_ok", bom=True, audio_block={"bpm": 90, "synth": {"kind": "beat"}, "normalize": True,
                                             "markers": [{"id": "logo", "time": 1.0}, {"id": "drop", "time": 3.0}]},
              layers=[{"id": "t", "type": "text", "text": "Ondea",
                       "anim": [{"at": "beat:4", "dur": 0.3, "from": {"opacity": 0}, "to": {"opacity": 1}}]}])
    cp = mograph("beats", key, "--scene", str(p), "--write")
    raw = p.read_bytes()
    au = json.loads(raw.decode("utf-8")).get("audio") or {}
    an = au.get("analysis") or {}
    marks = {m["id"]: m["time"] for m in au.get("markers", [])}
    check("bloc écrit : src = clé du verrou, 120 BPM, synth retiré, marqueur logo gardé, drop remplacé (8 s)",
          cp.returncode == 0 and au.get("src") == key and abs(au.get("bpm", 0) - 120) < 0.1 and "synth" not in au
          and marks.get("logo") == 1.0 and marks.get("drop") == 8.0, json.dumps(au, ensure_ascii=False)[:200])
    check("fiche d'analyse : générateur, empreinte = SHA-256 du fichier gelé, confiance, dérive, plage de tempo",
          an.get("generator") == audio.BEATS_GENERATOR_ID and an.get("sha256") == assets.file_sha256(config.ROOT / key)
          and an.get("confidence", 0) >= 0.9 and an.get("drift_ms", 99) < 5 and an.get("bpm_range") == [60.0, 200.0], str(an))
    check("scène réécrite en UTF-8 sans BOM, aucun fichier temporaire laissé, scène valide",
          not raw.startswith(b"\xef\xbb\xbf") and not list(p.parent.glob("*.tmp"))
          and mograph("validate", str(p)).returncode == 0)
    check("fiche d'analyse : grille mesurée (bpm, offset, beats_per_bar) = bloc écrit",
          an.get("bpm") == au.get("bpm") and an.get("offset") == au.get("offset") and an.get("beats_per_bar") == 4, str(an))
    return p


def test_options(key: str, t: dict) -> None:
    print("== C bis. Options, --json, drop, scène illisible, hors projet, lecture seule ==")
    pj = scene("m_json", audio_block={"bpm": 120})
    cp = mograph("beats", key, "--scene", str(pj), "--write", "--json")
    try:
        obj = json.loads(cp.stdout)
    except ValueError:
        obj = {}
    check("--json --write : stdout = UN objet JSON (analyse, audio, written), message sur stderr",
          cp.returncode == 0 and len(cp.stdout.strip().splitlines()) == 1 and obj.get("written") == str(pj.resolve())
          and obj.get("audio", {}).get("src") == key and "Bloc audio écrit" in cp.stderr, cp.stdout[:120])
    pd = scene("m_drop", audio_block={"bpm": 90, "markers": [{"id": "drop", "time": 3.0}]})
    rel = t["nodrop"].relative_to(config.ROOT).as_posix()
    cp = mograph("beats", rel, "--scene", str(pd), "--write")
    au = json.loads(pd.read_text(encoding="utf-8"))["audio"]
    check("nouvelle analyse sans drop : l'ancien drop (seul marqueur) est retiré", cp.returncode == 0 and "markers" not in au,
          str(au.get("markers")))
    for opts, label in ((["--beats-per-bar", "17"], "mesure 17"), (["--bpm-max", "inf"], "bpm-max infini"),
                        (["--bpm-min", "200", "--bpm-max", "100"], "plage inversée"), (["--bpm-min", "0"], "bpm-min nul"),
                        (["--beats-per-bar", "0"], "mesure 0")):
        before = pd.read_bytes()
        cp = mograph("beats", rel, "--scene", str(pd), "--write", *opts)
        check(f"option invalide ({label}) : REFUS en français (code 1), sans trace, scène intacte",
              cp.returncode == 1 and "hors bornes" in cp.stderr and "Traceback" not in cp.stderr and pd.read_bytes() == before,
              tail(cp, 1))
    pk = scene("m_keep", audio_block={"bpm": 120})
    mograph("beats", rel, "--scene", str(pk), "--write", "--beats-per-bar", "3", "--bpm-min", "100", "--bpm-max", "140")
    cp = mograph("beats", rel, "--scene", str(pk), "--write")
    au = json.loads(pk.read_text(encoding="utf-8"))["audio"]
    check("relance sans options : mesure (3) et plage (100–140) reprises de la scène",
          cp.returncode == 0 and au.get("beats_per_bar") == 3 and au["analysis"].get("bpm_range") == [100.0, 140.0], str(au)[:160])
    bad = TEST / "scenes" / "m_bad.json"
    bad.write_text('{"id": "m_bad", "audio": {', encoding="utf-8")
    cp = mograph("beats", rel, "--scene", str(bad), "--write")
    check("scène JSON invalide : REFUS avant l'analyse (rien sur stdout), sans trace",
          cp.returncode == 1 and "JSON invalide" in cp.stderr and not cp.stdout.strip() and "Traceback" not in cp.stderr, tail(cp, 1))
    out_dir = TEST / "hors_projet"
    out_dir.mkdir(exist_ok=True)
    outside = Path(os.environ.get("TEMP", str(TEST))) / "mograph_step50_hors_projet.mp3"
    shutil.copyfile(config.ROOT / key, outside)
    try:
        cp = mograph("beats", str(outside), "--scene", str(pj), "--write")
    finally:
        outside.unlink(missing_ok=True)
    check("piste hors du projet + --write : REFUS (« hors du projet »), copie d'un fichier gelé -> clé gelée proposée",
          cp.returncode == 1 and "hors du projet" in cp.stderr and key in cp.stderr, tail(cp, 1))
    ro = scene("m_ro", audio_block={"bpm": 120})
    os.chmod(ro, 0o444)
    try:
        cp = mograph("beats", rel, "--scene", str(ro), "--write")
    finally:
        os.chmod(ro, 0o666)
    check("scène en lecture seule : REFUS en français, sans trace, aucun temporaire laissé",
          cp.returncode == 1 and "lecture seule" in cp.stderr and "Traceback" not in cp.stderr
          and not list(ro.parent.glob("m_ro.json*.tmp")), tail(cp, 1))


def test_drift(t: dict) -> None:
    print("== D. Dérive : tempo fixe / tempo variable ==")
    import numpy as np
    check("seuil normatif de dérive : 40 ms (une image à 25 i/s, CONTRACT §11.6)", config.TEMPO_DRIFT_WARN_MS == 40.0)
    r = audio.analyse_tempo(t["c973_wav"])
    check("tempo fixe : dérive < 5 ms", r["drift_ms"] < 5, str(r["drift_ms"]))
    rise = np.zeros(20_000)
    for k in range(40):
        rise[int(k * 500 - (60 if k >= 20 else 0)) if k else 0] = 1.0
    d = audio._drift(audio._beat_deviations(rise, 500.0, 0.0))
    check("unitaire : impulsions décalées de -60 unités dans la 2e moitié -> dérive 60", d is not None and abs(d - 60) < 1e-9, str(d))
    sd = TEST / "src"
    P = 0.5
    rit = [k * P for k in range(48)]
    tt = rit[-1]
    for k in range(8):
        tt += 0.5 + 0.25 * (k + 1) / 8
        rit.append(tt)
    cases = {"intro muette (6 s) puis 120 BPM fixe": ([6.0 + k * P for k in range(50)], False),
             "5 min à 123,7 BPM fixe": (list(np.arange(0.25, 300, 60 / 123.7)), False),
             "ralenti final sur 8 temps": (rit, True),
             "un temps de 750 ms (décalage d'un demi-temps)": ([k * P for k in range(32)] + [16.75 + k * P for k in range(32)], True)}
    for k, (label, (times, variable)) in enumerate(cases.items()):
        f = clicks(sd / f"drift{k}.wav", times, max(times) + 1.0)
        cp = mograph("beats", str(f), "--json")
        r = json.loads(cp.stdout)["analyse"] if cp.returncode == 0 else {}
        warned = "tempo variable" in cp.stderr
        ok = (r.get("drift_ms") or 0) > config.TEMPO_DRIFT_WARN_MS and warned if variable else \
            (r.get("drift_ms") is not None and r["drift_ms"] < 5 and not warned)
        if k == 1:
            ok = ok and abs(r.get("bpm", 0) - 123.7) <= 0.02
        check(f"{label} : {'dérive > 40 ms et avertissement' if variable else 'dérive < 5 ms, aucun avertissement'}", ok,
              f"bpm {r.get('bpm')} dérive {r.get('drift_ms')}")
    f = clicks(sd / "court.wav", [0.1, 0.6, 1.1], 1.4)
    ps = scene("m_court", audio_block={"bpm": 120})
    cp = mograph("beats", str(f.relative_to(config.ROOT)) if f.is_relative_to(config.ROOT) else str(f), "--scene", str(ps), "--write")
    an = (json.loads(ps.read_text(encoding="utf-8")).get("audio") or {}).get("analysis") or {}
    check("morceau trop court : dérive « non mesurée » (null), scène valide",
          cp.returncode == 0 and "non mesurée" in cp.stdout and "drift_ms" in an and an["drift_ms"] is None
          and mograph("validate", str(ps)).returncode == 0, tail(cp, 1))
    p = scene("m_var", audio_block={"bpm": 120})
    cp = mograph("beats", str(t["var"]), "--scene", str(p), "--write")
    an = (json.loads(p.read_text(encoding="utf-8")).get("audio") or {}).get("analysis") or {}
    check("tempo variable (120 puis 126 BPM) : dérive > 40 ms, avertissement « tempo variable » (marqueurs conseillés)",
          cp.returncode == 0 and an.get("drift_ms", 0) > config.TEMPO_DRIFT_WARN_MS and "tempo variable" in cp.stderr
          and "marker:" in cp.stderr, f"dérive {an.get('drift_ms')} ; {tail(cp, 1)}")
    c = scene_mod.compile_scene(p)
    check("compilation : avertissement « tempo variable, rupture ou syncope » sur audio.analysis",
          any("tempo variable, rupture ou syncope" in w for w in c.get("warnings", [])), str(c.get("warnings"))[:200])


def test_compile(key: str, p_ok: Path, t: dict) -> None:
    print("== E. Compilation ==")
    c = scene_mod.compile_scene(p_ok)
    au = c["audio"]
    tw = c["shots"][0]["spec"]["layers"][0]["tweens"][0]
    want = au["offset"] + 4 * 60.0 / au["bpm"]
    check("beat:4 calé sur la musique gelée (offset + 4 × 60 / bpm), musique relevée comme asset IA",
          abs(tw["start"] - round(want, 6)) < 2e-3 and any(a["ai"] and a["path"] == key for a in c["assets"]),
          f"{tw['start']} / {want}")
    # Musique NON IA analysée puis remplacée : avertissement « relancez beats ».
    pp = scene("m_plain", audio_block={"bpm": 120})
    rel = t["plain"].relative_to(config.ROOT).as_posix()
    mograph("beats", rel, "--scene", str(pp), "--write")
    c = scene_mod.compile_scene(pp)
    clean = not any("autre version" in w for w in c.get("warnings", []))
    audio.synth_beat(t["plain"], 120, 16, offset=0.1)
    c = scene_mod.compile_scene(pp)
    w = next((w for w in c.get("warnings", []) if "autre version" in w), "")
    check("fichier analysé remplacé depuis beats --write : avertissement « autre version … relancez beats »",
          clean and "--write" in w, str(c.get("warnings"))[:200])
    redo = w.split("relancez « ", 1)[-1].rsplit(" »", 1)[0]
    import shlex
    argv = shlex.split(redo, posix=False)
    cp = mograph(*[a.strip('"') for a in argv[2:]]) if argv[:2] == ["python", "mograph.py"] else None
    c = scene_mod.compile_scene(pp)
    check("la commande de relance proposée fonctionne telle quelle et efface l'avertissement",
          cp is not None and cp.returncode == 0 and not any("autre version" in x for x in c.get("warnings", [])),
          redo if cp is None else tail(cp, 1))
    raw = json.loads(pp.read_text(encoding="utf-8"))
    raw["audio"]["bpm"] = 121
    pm = TEST / "scenes" / "m_main.json"
    pm.write_text(json.dumps({**raw, "id": "m_main"}, ensure_ascii=False), encoding="utf-8")
    c = scene_mod.compile_scene(pm)
    check("grille retouchée à la main (bpm 121) : avertissement « modifié(s) à la main », pas de dérive annoncée",
          any("modifié(s) à la main" in x for x in c.get("warnings", [])), str(c.get("warnings"))[:200])
    f = config.ROOT / key
    data = f.read_bytes()
    f.write_bytes(data[:-1] + bytes([data[-1] ^ 1]))
    cp = mograph("validate", str(p_ok))
    f.write_bytes(data)
    check("musique gelée modifiée d'un octet : REFUS (« modifié depuis son gel »)",
          cp.returncode == 1 and "modifié depuis son gel" in cp.stderr, tail(cp, 1))


def test_render(p_ok: Path) -> None:
    print("== F. Rendu complet avec la musique gelée ==")
    cp = mograph("all", str(p_ok), "--workers", "2")
    out = TEST / "out" / "m_ok"
    rep = json.loads((out / "qc_report.json").read_text(encoding="utf-8")) if (out / "qc_report.json").is_file() else {"checks": []}
    by = {c["id"]: c for c in rep["checks"]}
    det = json.loads((TEST / "build" / "m_ok" / "determinism.json").read_text(encoding="utf-8")) \
        if (TEST / "build" / "m_ok" / "determinism.json").is_file() else {}
    check("all : code 0, déterminisme 100 %, QC CONFORME (piste son, loudness, verrou)",
          cp.returncode == 0 and det.get("ok") is True and rep.get("verdict") == "CONFORME"
          and by.get("hevc_main10.audio", {}).get("status") == "PASS" and by.get("hevc_main10.loudness", {}).get("status") == "PASS"
          and by.get("scene.ai_lock", {}).get("status") == "PASS",
          f"{tail(cp, 2)} ; loudness={by.get('hevc_main10.loudness', {}).get('actual')}")
    md = (out / "qc_report.md").read_text(encoding="utf-8") if (out / "qc_report.md").is_file() else ""
    check("qc_report.md : provenance de la musique (ElevenLabs Music / music_v1, import manuel, licence)",
          "ElevenLabs Music / music_v1" in md and "manual_import" in md and "Eleven Music" in md)


def main() -> int:
    assert TEST.parent.name == "_tests" and AI.parent.name == "_tests" and PLAIN.parent.name == "_tests"
    for d in (TEST, AI, PLAIN):
        if d.exists():
            shutil.rmtree(d)
    TEST.mkdir(parents=True)
    if not check("environnement isolé : verrou et dossier IA de test",
                 config.ASSETS_LOCK == (TEST / "assets.lock.json").resolve() and config.AI_ASSETS_DIR == AI.resolve()):
        return 1
    t0 = time.perf_counter()
    try:
        t = make_tracks()
        test_reproducible(t)
        key = test_freeze(t)
        p = test_write(key)
        test_options(key, t)
        test_drift(t)
        test_compile(key, p, t)
        test_render(p)
    except Exception as exc:  # noqa: BLE001 — rapporté comme échec, jamais masqué
        import traceback
        traceback.print_exc()
        check("les contrôles s'exécutent sans exception", False, f"{type(exc).__name__} : {exc}"[:200])
    print(f"\nDurée : {time.perf_counter() - t0:.0f} s ; aucune IA appelée")
    if FAILS:
        print(f"ÉCHEC : {len(FAILS)} contrôle(s) : {FAILS}")
        return 1
    print("SUCCÈS : musique gelée et repères conformes (phase 5).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
