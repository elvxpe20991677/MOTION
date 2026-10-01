"""Vérification de la phase 4 : voix off ElevenLabs (pipeline/voice.py), word:N, sous-titres, mixage.

Lancement depuis la racine :  .venv/Scripts/python.exe tests/verify_step40_voice.py
AUCUN caractère ElevenLabs n'est consommé : l'API est remplacée par un faux serveur local (même
contrat que « text-to-speech with timestamps » et « user/subscription »), la clé et la voix sont des
valeurs de test, le vrai .env n'est jamais lu (MOGRAPH_ENV_FILE pointe vers un fichier inexistant).
Isolé : build/_tests/step40/, dossier IA assets/_tests/ai40/, verrou et journal de test.
Contrôles :
  A. estimation (--dry-run) sans appel ; garde-fous : budget, quota restant, confirmation ;
  B. génération : un seul appel, requête conforme, alignement -> mots, gel (source api), scène mise à
     jour, journal de consommation ; rejeu = cache, 0 caractère ;
  C. compilation : word:N, marqueur [[id]], sous-titres de la voix, texte modifié refusé ;
  D. mixage : ducking exact (unitaire), rendu complet (all) déterministe, QC CONFORME avec le son ;
  E. erreurs HTTP traduites ; la clé et l'identifiant de voix n'apparaissent nulle part.
"""

from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TEST = ROOT / "build" / "_tests" / "step40"
AI = ROOT / "assets" / "_tests" / "ai40"
KEY, VOICE = "cle-de-test-0123456789", "voix-de-test-abcdef"
os.environ.update({
    "MOGRAPH_BUILD_DIR": str(TEST / "build"), "MOGRAPH_OUT_DIR": str(TEST / "out"),
    "MOGRAPH_AI_ASSETS_DIR": str(AI), "MOGRAPH_ASSETS_LOCK": str(TEST / "assets.lock.json"),
    "MOGRAPH_VOICE_USAGE_LOG": str(TEST / "voice_usage.jsonl"), "MOGRAPH_ENV_FILE": str(TEST / "pas_de_env"),
    "ELEVENLABS_API_KEY": KEY, "ELEVENLABS_VOICE_ID": VOICE, "MOGRAPH_VOICE_BUDGET": "1000",
    "MOGRAPH_VOICE_CONFIRM_ABOVE": "80", "PYTHONIOENCODING": "utf-8"})
sys.path.insert(0, str(ROOT))

import numpy as np  # noqa: E402

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

FAILS: list[str] = []
OUTPUTS: list[str] = []          # toutes les sorties des commandes : la clé ne doit y figurer nulle part
CHAR_S = 0.06                    # durée d'un caractère dans la fausse synthèse


def check(label: str, ok: bool, detail: str = "") -> bool:
    print(("PASS " if ok else "FAIL ") + label + (f" -- {detail}" if detail else ""), flush=True)
    if not ok:
        FAILS.append(label)
    return ok


# ---------------------------------------------------------------------------------------------------
# Faux serveur ElevenLabs
# ---------------------------------------------------------------------------------------------------

class Fake:
    limit = 10_000
    used = 0
    calls: list[dict] = []
    history: list[dict] = []
    fail_next: int | None = None


def _mp3(seconds: float) -> bytes:
    from pipeline import config
    r = subprocess.run([config.FFMPEG, "-v", "error", "-f", "lavfi", "-i", f"sine=frequency=330:duration={seconds:.3f}",
                        "-ac", "1", "-ar", "44100", "-c:a", "libmp3lame", "-b:a", "128k", "-f", "mp3", "pipe:1"],
                       capture_output=True, check=True)
    return r.stdout


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # silence
        pass

    def _send(self, code: int, obj: dict) -> None:
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.headers.get("xi-api-key") != KEY:
            return self._send(401, {"detail": {"status": "invalid_api_key", "message": "Invalid API key"}})
        if self.path.startswith("/v1/history"):
            return self._send(200, {"history": list(reversed(Fake.history))})
        if self.path.startswith("/v1/user/subscription"):
            return self._send(200, {"tier": "test", "character_count": Fake.used, "character_limit": Fake.limit,
                                    "next_character_count_reset_unix": 1792600000, "can_extend_character_limit": False,
                                    "status": "active"})
        self._send(404, {"detail": "not found"})

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n).decode("utf-8"))
        if self.headers.get("xi-api-key") != KEY:
            return self._send(401, {"detail": {"status": "invalid_api_key", "message": "Invalid API key"}})
        if Fake.fail_next:
            code, Fake.fail_next = Fake.fail_next, None
            return self._send(code, {"detail": {"message": "erreur simulée"}})
        Fake.calls.append({"path": self.path, "body": body})
        text = body["text"]
        # Comme le vrai service : le décompte (ici la moitié) diffère du nombre de caractères envoyés,
        # et le compteur agrégé de l'abonnement n'est PAS mis à jour tout de suite.
        billed = (len(text) + 1) // 2
        Fake.history.append({"text": text, "model_id": body.get("model_id"), "date_unix": int(time.time()),
                             "character_count_change_from": Fake.used, "character_count_change_to": Fake.used + billed})
        starts = [round(i * CHAR_S, 3) for i in range(len(text))]
        ends = [round((i + 1) * CHAR_S, 3) for i in range(len(text))]
        align = {"characters": list(text), "character_start_times_seconds": starts, "character_end_times_seconds": ends}
        self._send(200, {"audio_base64": base64.b64encode(_mp3(len(text) * CHAR_S + 0.2)).decode(),
                         "alignment": align, "normalized_alignment": align})


SERVER = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
os.environ["MOGRAPH_ELEVENLABS_API"] = f"http://127.0.0.1:{SERVER.server_address[1]}"

from pipeline import assets, audio, config, qc, voice  # noqa: E402  (après l'environnement)
from pipeline import scene as scene_mod  # noqa: E402

PY = sys.executable
QC = {"safe_zone": "title", "safe_zone_tolerance_s": 0.5, "allow_black_s": 0, "determinism_frames": 3,
      "loudness_target_lufs": -14}
TEXT = "Bienvenue chez Ondea. [[logo]] Le son, enfin libre."      # 42 caractères envoyés (marqueur retiré)


def mograph(*args: str, timeout: float = 1200) -> subprocess.CompletedProcess:
    # stdin vide : comportement « sans terminal » identique quel que soit le lanceur du test.
    cp = subprocess.run([PY, str(ROOT / "mograph.py"), *args], cwd=ROOT, env=dict(os.environ), capture_output=True,
                        stdin=subprocess.DEVNULL, text=True, encoding="utf-8", errors="replace", timeout=timeout)
    OUTPUTS.append(cp.stdout + cp.stderr)
    return cp


def tail(cp, n=3) -> str:
    return " | ".join((cp.stdout + cp.stderr).strip().splitlines()[-n:])


def scene(sid: str, text: str = TEXT, *, layers=None, duration=4.0, extra=None) -> Path:
    d = {"version": "1.0", "id": sid, "title": sid, "brief": "test phase 4", "seed": 7, "preset": "flat_riso",
         "format": {"aspect": "16:9", "resolution": "1080p", "fps": 25, "alpha": False},
         "outputs": [{"profile": "hevc_main10"}], "qc": QC,
         "voiceover": {"text": text, "start": 0.4},
         "provenance": {"ai_disclosure": {"ai_used": True, "roles": ["voice"], "date": "2026-10-01",
                                          "tools": [{"name": "ElevenLabs", "model": "eleven_multilingual_v2", "role": "voix off"}]}},
         "shots": [{"id": "a", "duration": duration, "layers": layers or [{"id": "t", "type": "text", "text": "Ondea"}]}]}
    d.update(extra or {})
    p = TEST / "scenes" / f"{sid}.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
    return p


def usage() -> list[dict]:
    p = TEST / "voice_usage.jsonl"
    return [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines()] if p.is_file() else []


# ---------------------------------------------------------------------------------------------------
def test_guards() -> None:
    print("== A. Estimation et garde-fous (aucun appel de synthèse) ==")
    p = scene("v_garde")
    cp = mograph("voice", str(p), "--dry-run")
    check("--dry-run : estimation (42 caractères, quota lu), aucun appel de synthèse, journal « dry_run »",
          cp.returncode == 0 and "42 caractère" in cp.stdout and not Fake.calls and usage()[-1].get("dry_run") is True,
          tail(cp))
    cp = mograph("voice", str(p), "--budget", "10")
    check("budget de la commande dépassé : refus (code 1), aucun appel", cp.returncode == 1 and "budget" in cp.stderr
          and not Fake.calls, tail(cp, 1))
    Fake.used = Fake.limit - 20
    cp = mograph("voice", str(p))
    check("quota restant insuffisant : refus (code 1), aucun appel", cp.returncode == 1 and "restant" in cp.stderr
          and not Fake.calls, tail(cp, 1))
    Fake.used = 0
    long_p = scene("v_long", "Une phrase assez longue pour franchir le seuil de confirmation fixé à quatre-vingts caractères.")
    cp = mograph("voice", str(long_p))
    check("au-delà du seuil sans --yes et sans terminal : refus (« confirmez … --yes »), aucun appel",
          cp.returncode == 1 and "--yes" in cp.stderr and not Fake.calls, tail(cp, 1))
    try:
        voice.generate(long_p, ask=lambda prompt: "n", log=lambda m: None)
        check("confirmation refusée (« n ») : annulé", False, "accepté")
    except voice.VoiceError as exc:
        check("confirmation refusée (« n ») : annulé, aucun appel", "annulée" in str(exc) and not Fake.calls, str(exc)[:80])


def test_generate() -> Path:
    print("== B. Génération, gel, cache ==")
    p = scene("v_ok")
    cp = mograph("voice", str(p))
    raw = json.loads(p.read_text(encoding="utf-8"))
    vo = raw["voiceover"]
    call = Fake.calls[-1] if Fake.calls else {}
    body = call.get("body", {})
    check("génération : code 0, UN appel with-timestamps, format mp3_44100_128 en paramètre, voix dans le chemin",
          cp.returncode == 0 and len(Fake.calls) == 1 and "/with-timestamps?output_format=mp3_44100_128" in call.get("path", "")
          and f"/v1/text-to-speech/{VOICE}/" in call.get("path", ""), tail(cp))
    check("requête : texte sans marqueur [[logo]], modèle par défaut, ni clé ni voix ni format dans le corps",
          body.get("text") == "Bienvenue chez Ondea. Le son, enfin libre." and body.get("model_id") == "eleven_multilingual_v2"
          and not ({"voice_id", "output_format", "xi-api-key"} & set(body)), str(body)[:160])
    a, al = config.ROOT / vo.get("asset", "?"), config.ROOT / vo.get("alignment", "?")
    lock = assets.load_lock()
    ea, el = lock["assets"].get(vo.get("asset")), lock["assets"].get(vo.get("alignment"))
    check("son et alignement gelés sous voice/ (source api, outil ElevenLabs, prompt = texte, coût noté), scène mise à jour",
          a.is_file() and al.is_file() and ea and el and ea["provenance"]["source"] == "api"
          and ea["provenance"]["tool"] == "ElevenLabs" and ea["provenance"]["prompt"] == body.get("text")
          and "caractères" in (ea["provenance"].get("cost") or "") and ea["kind"] == "audio" and el["kind"] == "text",
          str(vo))
    doc = json.loads(al.read_text(encoding="utf-8"))
    w = doc["words"]
    check("alignement -> 7 mots, temps du 1er caractère au dernier (« Ondea. » de 0,9 à 1,26 s), positions",
          len(w) == 7 and w[2]["text"] == "Ondea." and abs(w[2]["start"] - 0.9) < 1e-6 and abs(w[2]["end"] - 1.26) < 1e-6
          and w[3]["char_start"] == 22, str(w[:4]))
    u = usage()[-1]
    check("journal : 42 caractères estimés, 21 décomptés lus dans l'historique (compteur agrégé en retard), pas de cache",
          u["chars_estimated"] == 42 and u["chars_billed"] == 21 and u["cache"] is False and "quota_remaining_before" in u
          and "21 caractères ElevenLabs décomptés (42 envoyés)" in (ea["provenance"].get("cost") or ""), str(u))
    n_calls = len(Fake.calls)
    cp = mograph("voice", str(p))
    check("rejeu : cache, 0 caractère consommé, aucun nouvel appel",
          cp.returncode == 0 and "cache" in cp.stdout and len(Fake.calls) == n_calls and usage()[-1]["chars_billed"] == 0,
          tail(cp, 1))
    return p


def test_compile(p: Path) -> None:
    print("== C. Compilation : word:N, marqueur, sous-titres de la voix ==")
    raw = json.loads(p.read_text(encoding="utf-8"))
    raw["shots"][0]["layers"] = [
        {"id": "t", "type": "text", "text": "ONDEA", "anim": [{"at": "word:2", "dur": 0.3, "from": {"opacity": 0}, "to": {"opacity": 1}}]},
        {"id": "l", "type": "text", "text": "logo", "y": "30%", "anim": [{"at": "marker:logo-0.1", "dur": 0.2, "from": {"opacity": 0}, "to": {"opacity": 1}}]},
        {"id": "st", "type": "subtitles", "voice": True}]
    p.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
    c = scene_mod.compile_scene(p)
    vo = c["voiceover"]
    spec = c["shots"][0]["spec"]
    t = next(l for l in spec["layers"] if l["id"] == "t")["tweens"][0]
    lg = next(l for l in spec["layers"] if l["id"] == "l")["tweens"][0]
    subs = [l for l in spec["layers"] if l["id"].startswith("st-")]
    check("word:2 = début du 3e mot + départ de la voix (0,4 + 0,9 = 1,3 s)", abs(t["start"] - 1.3) < 1e-6, str(t["start"]))
    check("marqueur [[logo]] = début du mot suivant (« Le », 0,4 + 1,32 = 1,72 s), utilisable comme marker:logo",
          abs(vo["markers"][0]["time"] - 1.72) < 1e-6 and abs(lg["start"] - 1.62) < 1e-6, f"{vo['markers']} {lg['start']}")
    check("sous-titres de la voix : 2 répliques coupées en fin de phrase, textes exacts",
          [l["text"] for l in subs] == ["Bienvenue chez Ondea.", "Le son, enfin libre."], str([l["text"] for l in subs]))
    check("bloc audio : mixage build/…/audio/mix.wav, normalisation par défaut, assets voix relevés comme IA",
          c["audio"]["mix"].endswith("mix.wav") and c["audio"]["normalize"] is True and c["audio"]["src"] is None
          and sum(1 for a in c["assets"] if a["ai"]) == 2)
    raw["voiceover"]["text"] = TEXT + " Ajout."
    p2 = TEST / "scenes" / "v_change.json"
    p2.write_text(json.dumps({**raw, "id": "v_change"}, ensure_ascii=False), encoding="utf-8")
    cp = mograph("validate", str(p2))
    check("texte de la voix modifié sans régénérer : REFUS (« a changé depuis la génération »)",
          cp.returncode == 1 and "a changé depuis la génération" in cp.stderr, tail(cp, 1))
    bad = dict(raw, id="v_mot")
    bad["voiceover"] = {**raw["voiceover"], "text": TEXT}
    bad["shots"] = [{"id": "a", "duration": 4.0, "layers": [{"id": "t", "type": "text", "text": "x",
                                                              "anim": [{"at": "word:99", "from": {"opacity": 0}, "to": {"opacity": 1}}]}]}]
    p3 = TEST / "scenes" / "v_mot.json"
    p3.write_text(json.dumps(bad, ensure_ascii=False), encoding="utf-8")
    cp = mograph("validate", str(p3))
    check("word:99 hors de la voix : REFUS (« la voix off compte 7 mots »)",
          cp.returncode == 1 and "compte 7 mots" in cp.stderr, tail(cp, 1))


def test_mix_and_render(p: Path) -> None:
    print("== D. Mixage (ducking) et rendu complet ==")
    # Unitaire : voix muette + musique constante -> le mixage EST la musique × gain de ducking.
    n = audio.SAMPLE_RATE * 3
    w = audio.duck_envelope(n, [(1.0, 1.5)], attack=0.1, release=0.2)
    sr = audio.SAMPLE_RATE
    check("enveloppe : 0 avant l'attaque, rampe à mi-attaque (0,5), 1 pendant la parole, 0,5 à mi-relâchement, 0 après",
          w[int(0.85 * sr)] == 0 and abs(w[int(0.95 * sr)] - 0.5) < 1e-3 and w[int(1.2 * sr)] == 1
          and abs(w[int(1.6 * sr)] - 0.5) < 1e-3 and w[int(1.8 * sr)] == 0)
    mus = TEST / "musique.wav"
    sil = TEST / "silence.wav"
    subprocess.run([config.FFMPEG, "-v", "error", "-y", "-f", "lavfi", "-i", "sine=frequency=220:duration=3",
                    "-ar", "48000", "-ac", "2", "-af", "volume=0.5", str(mus)], check=True)
    subprocess.run([config.FFMPEG, "-v", "error", "-y", "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo",
                    "-t", "3", str(sil)], check=True)
    fake = {"frames": 75, "duration": 3.0, "format": {"fps": 25},
            "voiceover": {"asset": str(sil), "start": 0.0, "gain_db": 0.0, "duck": {"db": -12.0, "attack": 0.1, "release": 0.2},
                          "words": [{"text": "a", "start": 1.0, "end": 1.5}]},
            "audio": {"mix": str(TEST / "mix_unitaire.wav")}, "assets": []}
    out = audio.build_mix(fake, mus, log=lambda m: None)
    m = audio._decode_stereo(out, n)
    ref = audio._decode_stereo(mus, n)
    rms = lambda x: float(np.sqrt(np.mean(x ** 2)))  # noqa: E731
    ratio_in = rms(m[int(1.1 * sr):int(1.4 * sr)]) / rms(ref[int(1.1 * sr):int(1.4 * sr)])
    ratio_out = rms(m[int(0.2 * sr):int(0.7 * sr)]) / rms(ref[int(0.2 * sr):int(0.7 * sr)])
    check("mixage : musique à -12 dB pendant la parole (rapport 0,251), intacte hors parole (1,0)",
          abs(ratio_in - 10 ** (-12 / 20)) < 0.003 and abs(ratio_out - 1) < 0.003, f"{ratio_in:.4f} / {ratio_out:.4f}")
    stamp = out.stat().st_mtime_ns
    audio.build_mix(fake, mus, log=lambda m: None)
    check("mixage à jour non refait (fiche mix.json)", out.stat().st_mtime_ns == stamp)
    # Rendu complet : voix + musique de test (synth), porte de déterminisme, QC avec le son.
    raw = json.loads(p.read_text(encoding="utf-8"))
    # Piste test générée sous assets/_tests/ (ignoré par git), jamais dans le vrai assets/audio/.
    raw["audio"] = {"bpm": 120, "src": "assets/_tests/v40_beat.wav", "synth": {"kind": "beat"}}
    p.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
    cp = mograph("all", str(p), "--workers", "2")
    rep = json.loads((TEST / "out" / "v_ok" / "qc_report.json").read_text(encoding="utf-8")) \
        if (TEST / "out" / "v_ok" / "qc_report.json").is_file() else {"checks": []}
    by = {c["id"]: c for c in rep["checks"]}
    det = json.loads((TEST / "build" / "v_ok" / "determinism.json").read_text(encoding="utf-8")) \
        if (TEST / "build" / "v_ok" / "determinism.json").is_file() else {}
    check("all : code 0, déterminisme 100 %, QC CONFORME (piste son, loudness -14 LUFS, assets gelés, provenance)",
          cp.returncode == 0 and det.get("ok") is True and rep.get("verdict") == "CONFORME"
          and by.get("hevc_main10.audio", {}).get("status") == "PASS"
          and by.get("hevc_main10.loudness", {}).get("status") == "PASS" and by.get("scene.ai_lock", {}).get("status") == "PASS",
          f"{tail(cp, 2)} ; loudness={by.get('hevc_main10.loudness', {}).get('actual')}")
    md = (TEST / "out" / "v_ok" / "qc_report.md").read_text(encoding="utf-8") if (TEST / "out" / "v_ok" / "qc_report.md").is_file() else ""
    check("qc_report.md : provenance de la voix (ElevenLabs / eleven_multilingual_v2, source api)",
          "ElevenLabs / eleven_multilingual_v2" in md and "source api" in md)


def test_errors_and_secrets() -> None:
    print("== E. Erreurs HTTP et secrets ==")
    p = scene("v_err", "Autre texte court.")
    Fake.fail_next = 422
    cp = mograph("voice", str(p))
    check("erreur 422 de l'API : message français, code 1", cp.returncode == 1 and "422" in cp.stderr
          and "requête refusée" in cp.stderr, tail(cp, 1))
    env = dict(os.environ, ELEVENLABS_API_KEY="mauvaise-cle")
    cp = subprocess.run([PY, str(ROOT / "mograph.py"), "voice", "--quota"], cwd=ROOT, env=env, capture_output=True,
                        text=True, encoding="utf-8", errors="replace")
    OUTPUTS.append(cp.stdout + cp.stderr)
    check("clé refusée (401) : « vérifiez ELEVENLABS_API_KEY », clé non affichée",
          cp.returncode == 1 and "401" in cp.stderr and "mauvaise-cle" not in cp.stderr, tail(cp, 1))
    files = [TEST / "voice_usage.jsonl", TEST / "assets.lock.json", *sorted((AI / "voice").glob("*.json")),
             *sorted((TEST / "scenes").glob("*.json")), *sorted((TEST / "out").rglob("qc_report.*"))]
    blob = "\n".join(OUTPUTS) + "\n".join(f.read_text(encoding="utf-8", errors="replace") for f in files if f.is_file())
    check("la clé API n'apparaît dans aucune sortie, journal, verrou, alignement, scène ni rapport", KEY not in blob)
    check("l'identifiant de voix de .env n'apparaît qu'en empreinte (fichiers ET sorties console, erreurs comprises)",
          VOICE not in blob and "empreinte" in blob)


def main() -> int:
    assert TEST.parent.name == "_tests" and AI.parent.name == "_tests"
    for d in (TEST, AI):
        if d.exists():
            shutil.rmtree(d)
    TEST.mkdir(parents=True)
    if not check("environnement isolé : faux serveur local, verrou, dossier IA et .env de test",
                 config.ELEVENLABS_API_BASE.startswith("http://127.0.0.1:") and config.ASSETS_LOCK == (TEST / "assets.lock.json").resolve()
                 and config.AI_ASSETS_DIR == AI.resolve() and not config.ENV_FILE.exists()):
        print("ÉCHEC : isolation impossible, arrêt avant tout appel.")
        return 1
    th = threading.Thread(target=SERVER.serve_forever, daemon=True)
    th.start()
    t0 = time.perf_counter()
    try:
        test_guards()
        p = test_generate()
        test_compile(p)
        test_mix_and_render(p)
        test_errors_and_secrets()
    except Exception as exc:  # noqa: BLE001 — rapporté comme échec, jamais masqué
        import traceback
        traceback.print_exc()
        check("les contrôles s'exécutent sans exception", False, f"{type(exc).__name__} : {exc}"[:200])
    finally:
        SERVER.shutdown()
    print(f"\nDurée : {time.perf_counter() - t0:.0f} s ; appels de synthèse simulés : {len(Fake.calls)} ; "
          "caractères ElevenLabs réels consommés : 0")
    if FAILS:
        print(f"ÉCHEC : {len(FAILS)} contrôle(s) : {FAILS}")
        return 1
    print("SUCCÈS : voix off conforme (phase 4).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
