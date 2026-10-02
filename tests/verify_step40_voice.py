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
  D. mixage : ducking exact et voix placée/gain (unitaire), rendu complet (all) déterministe, mixage
     livré = musique duckée + voix, QC CONFORME avec le son ;
  E. erreurs HTTP traduites, détails de l'API masqués, clé invalide ; .env et variables invalides ;
  F. confirmation acceptée, composition de l'empreinte, cache partagé entre scènes, --dry-run sans effet ;
  G. voix gelée disparue (refus sans appel, --regenerate), réponse payée conservée et reprise, réponse
     illisible, historique illisible, Ctrl+C, deux commandes simultanées ;
  H. justesse : marqueurs en tête, ponctuation ouvrante, fin de phrase guillemetée, accents décomposés,
     départ de voix négatif ou après la fin, refus documentés, décalage de l'aperçu ;
  la clé et l'identifiant de voix n'apparaissent nulle part (contrôle final).
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
    fail_detail = None             # détail renvoyé avec fail_next
    post_mode: str | None = None   # un seul appel : « html » (200 illisible), « badalign », « slow »
    history_broken = False
    extra_s = 0.0                  # allonge le son : une régénération ne redonne pas les mêmes octets


def _mp3(seconds: float) -> bytes:
    from pipeline import config
    r = subprocess.run([config.FFMPEG, "-v", "error", "-f", "lavfi", "-i", f"sine=frequency=330:duration={seconds:.3f}",
                        "-ac", "1", "-ar", "44100", "-c:a", "libmp3lame", "-b:a", "128k", "-f", "mp3", "pipe:1"],
                       capture_output=True, check=True)
    return r.stdout


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # silence
        pass

    def _send(self, code: int, obj, raw: bytes | None = None) -> None:
        body = raw if raw is not None else json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json" if raw is None else "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.headers.get("xi-api-key") != KEY:
            return self._send(401, {"detail": {"status": "invalid_api_key", "message": "Invalid API key"}})
        if self.path.startswith("/v1/history"):
            if Fake.history_broken:
                return self._send(200, None, raw=b"<html>Bad gateway</html>")
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
            detail, Fake.fail_detail = Fake.fail_detail or {"message": "erreur simulée"}, None
            return self._send(code, {"detail": detail})
        Fake.calls.append({"path": self.path, "body": body})
        mode, Fake.post_mode = Fake.post_mode, None
        text = body["text"]
        # Comme le vrai service : le décompte (ici la moitié) diffère du nombre de caractères envoyés,
        # et le compteur agrégé de l'abonnement n'est PAS mis à jour tout de suite.
        billed = (len(text) + 1) // 2
        Fake.history.append({"text": text, "model_id": body.get("model_id"), "date_unix": int(time.time()),
                             "character_count_change_from": Fake.used, "character_count_change_to": Fake.used + billed})
        if mode == "html":
            return self._send(200, None, raw=b"<html>Bad gateway</html>")
        if mode == "slow":
            time.sleep(1.5)
        align = fake_alignment(text)
        if mode == "badalign":
            align = {k: v[:-1] for k, v in align.items()}
        self._send(200, {"audio_base64": base64.b64encode(_mp3(len(text) * CHAR_S + 0.2 + Fake.extra_s)).decode(),
                         "alignment": align, "normalized_alignment": align})


def fake_alignment(text: str) -> dict:
    return {"characters": list(text), "character_start_times_seconds": [round(i * CHAR_S, 3) for i in range(len(text))],
            "character_end_times_seconds": [round((i + 1) * CHAR_S, 3) for i in range(len(text))]}


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
    check("alignement -> 7 mots, temps du 1er caractère au dernier (« Ondea. » de 0,9 à 1,26 s), positions ; "
          "characters_sent = 42 (caractères ENVOYÉS, pas le décompte)",
          len(w) == 7 and w[2]["text"] == "Ondea." and abs(w[2]["start"] - 0.9) < 1e-6 and abs(w[2]["end"] - 1.26) < 1e-6
          and w[3]["char_start"] == 22 and doc.get("characters_sent") == 42 and "characters_billed" not in doc, str(w[:4]))
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
    cues = voice.cues_from_words(vo["words"])
    want = [(0.4, 1.72, "Bienvenue chez Ondea."), (1.72, 3.17, "Le son, enfin libre.")]
    check("temps des répliques : début au 1er mot, tenue 0,25 s coupée au début de la suivante (0,4–1,72 / 1,72–3,17 s)",
          len(cues) == 2 and all(abs(a[0] - b[0]) < 1e-6 and abs(a[1] - b[1]) < 1e-6 and a[2] == b[2] for a, b in zip(cues, want)),
          str(cues))
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
    # Refus documentés (CONTRACT §11.5).
    plain = {k: v for k, v in raw.items() if k not in ("voiceover", "audio")}
    cases = [
        ("marqueur [[logo]] aussi défini dans audio.markers", {**raw, "voiceover": {**raw["voiceover"], "text": TEXT},
                                                               "audio": {"bpm": 120, "markers": [{"id": "logo", "time": 1.0}]}},
         "deux fois"),
        ("word:0 sans voix off", {**plain, "shots": [{"id": "a", "duration": 2.0, "layers": [{"id": "t", "type": "text", "text": "x",
                                  "anim": [{"at": "word:0", "from": {"opacity": 0}, "to": {"opacity": 1}}]}]}]}, "exige une voix off"),
        ("sous-titres voice:true sans voix off", {**plain, "shots": [{"id": "a", "duration": 2.0, "layers": [
            {"id": "st", "type": "subtitles", "voice": True}]}]}, "sous-titres de la voix off demandés"),
        ("voix off pas encore générée", {**raw, "voiceover": {"text": TEXT}}, "pas encore générée"),
    ]
    for k, (label, d, needle) in enumerate(cases):
        pk = TEST / "scenes" / f"v_refus{k}.json"
        pk.write_text(json.dumps({**d, "id": f"v_refus{k}"}, ensure_ascii=False), encoding="utf-8")
        cp = mograph("validate", str(pk))
        check(f"{label} : REFUS (« {needle} »)", cp.returncode == 1 and needle in cp.stderr, tail(cp, 1))


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
    check("mixage à jour non refait (fiche mix.wav.json)", out.stat().st_mtime_ns == stamp)
    # Voix AUDIBLE (sinus 330 Hz gelé de v_ok), départ 0,4 s, gain -6 dB, sans musique : le mixage EST la voix placée.
    vmp3 = config.ROOT / json.loads(p.read_text(encoding="utf-8"))["voiceover"]["asset"]
    fake2 = {**fake, "voiceover": {**fake["voiceover"], "asset": str(vmp3), "start": 0.4, "gain_db": -6.0},
             "audio": {"mix": str(TEST / "mix_voix.wav")}}
    m2 = audio._decode_stereo(audio.build_mix(fake2, None, log=lambda m: None), n)
    v = audio._decode_stereo(vmp3, n)
    off, g6 = int(round(0.4 * sr)), 10 ** (-6 / 20)
    check("voix placée à start (silence avant 0,4 s), gain -6 dB, échantillons = source décalée (à 1e-4 près)",
          float(np.max(np.abs(m2[:off]))) == 0.0 and rms(v[:sr]) > 0.01
          and float(np.max(np.abs(m2[off:off + sr] - v[:sr] * g6))) < 1e-4,
          f"avant={float(np.max(np.abs(m2[:off]))):.2e} écart={float(np.max(np.abs(m2[off:off + sr] - v[:sr] * g6))):.2e}")
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
    # Mixage livré : musique duckée + voix placée (aucune voix perdue ni décalée), puis voix dans le livrable.
    c = scene_mod.compile_scene(p)
    vo = c["voiceover"]
    ns = audio._scene_samples(c)
    musf = audio._decode_stereo(c["audio"]["src"], ns)
    vv = audio._decode_stereo(vo["asset"], ns)
    placed = np.zeros((ns, 2), np.float32)
    o = int(round(vo["start"] * sr))
    placed[o:] = vv[:ns - o] * np.float32(10 ** (vo["gain_db"] / 20))
    wv = audio.duck_envelope(ns, audio.speech_intervals(vo["words"], config.VOICE_DUCK_MERGE_S),
                             float(vo["duck"]["attack"]), float(vo["duck"]["release"]))
    exp = musf * (1 + wv * (np.float32(10 ** (vo["duck"]["db"] / 20)) - 1))[:, None] + placed
    pk = float(np.max(np.abs(exp)))
    exp = exp * np.float32(0.999 / pk) if pk > 0.999 else exp
    got = audio._decode_stereo(c["audio"]["mix"], ns) if Path(c["audio"]["mix"]).is_file() else np.zeros_like(exp)
    check("mixage livré de v_ok = musique duckée + voix placée à 0,4 s (à 1e-4 près)",
          float(np.max(np.abs(got - exp))) < 1e-4, f"écart max {float(np.max(np.abs(got - exp))):.2e}")
    mp4 = TEST / "out" / "v_ok" / "v_ok_hevc_main10.mp4"
    if mp4.is_file():
        x = audio._decode_stereo(mp4, ns).mean(axis=1)

        def e330(a: float, b: float) -> float:
            seg = x[int(a * sr):int(b * sr)]
            tt = np.arange(len(seg)) / sr
            return float(np.hypot(np.dot(seg, np.sin(2 * np.pi * 330 * tt)), np.dot(seg, np.cos(2 * np.pi * 330 * tt))) / len(seg))
        before, during = e330(0.0, 0.35), e330(0.6, 2.4)
        check("livrable : la voix (330 Hz) est présente après 0,4 s et absente avant", during > 5 * max(before, 1e-6),
              f"avant={before:.5f} pendant={during:.5f}")
    else:
        check("livrable : la voix (330 Hz) est présente après 0,4 s et absente avant", False, f"{mp4} absent")
    md = (TEST / "out" / "v_ok" / "qc_report.md").read_text(encoding="utf-8") if (TEST / "out" / "v_ok" / "qc_report.md").is_file() else ""
    check("qc_report.md : provenance de la voix (ElevenLabs / eleven_multilingual_v2, source api)",
          "ElevenLabs / eleven_multilingual_v2" in md and "source api" in md)


def test_errors_and_secrets() -> None:
    print("== E. Erreurs HTTP, détails masqués, clé et variables invalides ==")
    p = scene("v_err", "Autre texte court.")
    Fake.fail_next = 422
    cp = mograph("voice", str(p))
    check("erreur 422 de l'API : message français, code 1", cp.returncode == 1 and "422" in cp.stderr
          and "requête refusée" in cp.stderr, tail(cp, 1))
    Fake.fail_next, Fake.fail_detail = 404, {"status": "voice_not_found",
                                             "message": f"A voice with the voice_id {VOICE} was not found."}
    cp = mograph("voice", str(p))
    check("404 dont le détail cite la voix de .env : voix masquée (« <voix> »), indice ELEVENLABS_VOICE_ID",
          cp.returncode == 1 and VOICE not in cp.stderr and "<voix>" in cp.stderr and "ELEVENLABS_VOICE_ID" in cp.stderr, tail(cp, 1))
    Fake.fail_next, Fake.fail_detail = 422, [{"type": "string_too_short", "loc": ["path", "voice_id"], "msg": "bad",
                                              "input": VOICE}]
    cp = mograph("voice", str(p))
    check("422 de validation (liste) : seuls champ et message affichés, jamais « input »",
          cp.returncode == 1 and VOICE not in cp.stderr and "path.voice_id : bad" in cp.stderr, tail(cp, 1))
    for label, k, ok_code in (("clé suivie d'un retour à la ligne (Get-Content -Raw)", KEY + "\r\n", 0),
                              ("clé contenant un espace", "cle avec espace 0123", 1)):
        env = dict(os.environ, ELEVENLABS_API_KEY=k)
        cp = subprocess.run([PY, str(ROOT / "mograph.py"), "voice", "--quota"], cwd=ROOT, env=env, capture_output=True,
                            stdin=subprocess.DEVNULL, text=True, encoding="utf-8", errors="replace")
        OUTPUTS.append(cp.stdout + cp.stderr)
        check(f"{label} : code {ok_code}, aucune trace, valeur jamais affichée",
              cp.returncode == ok_code and "Traceback" not in cp.stderr and k.strip() not in cp.stdout + cp.stderr
              and (ok_code == 0 or "caractères invalides" in cp.stderr), tail(cp, 1))
    for var, val in (("MOGRAPH_VOICE_BUDGET", "abc"), ("MOGRAPH_VOICE_CONFIRM_ABOVE", "1e3")):
        env = dict(os.environ, **{var: val})
        cps = [subprocess.run([PY, str(ROOT / "mograph.py"), *a], cwd=ROOT, env=env, capture_output=True,
                              stdin=subprocess.DEVNULL, text=True, encoding="utf-8", errors="replace")
               for a in (("voice", str(p), "--dry-run"), ("validate", str(TEST / "scenes" / "v_ok.json")))]
        check(f"{var}={val} : « mograph voice » refuse en français, les autres commandes ne plantent pas",
              cps[0].returncode == 1 and f"{var} doit être un entier" in cps[0].stderr
              and not any("Traceback" in c.stderr for c in cps), tail(cps[0], 1))
    env = dict(os.environ, MOGRAPH_VOICE_BUDGET="10")
    n = len(Fake.calls)
    cp = subprocess.run([PY, str(ROOT / "mograph.py"), "voice", str(p)], cwd=ROOT, env=env, capture_output=True,
                        stdin=subprocess.DEVNULL, text=True, encoding="utf-8", errors="replace")
    check("MOGRAPH_VOICE_BUDGET=10 (sans --budget) : refus « budget », aucun appel",
          cp.returncode == 1 and "budget" in cp.stderr and len(Fake.calls) == n, tail(cp, 1))
    cp = mograph("voice", str(p), "--budget", "-5")
    check("--budget négatif : refusé par la ligne de commande (code 2)", cp.returncode == 2 and "entier ≥ 0" in cp.stderr,
          tail(cp, 1))
    big = scene("v_geant", "Texte. " * 1500)
    cp = mograph("voice", str(big), "--budget", "20000", "--yes", "--dry-run")
    check("texte au-delà de la limite d'une requête du modèle (10 000) : refus dès l'estimation",
          cp.returncode == 1 and "limite d'une requête" in cp.stderr, tail(cp, 1))
    # Exception inattendue à la frontière du CLI : trace et message sans la clé.
    import contextlib
    import io
    import mograph as cli
    orig = voice.generate
    voice.generate = lambda *a, **k: (_ for _ in ()).throw(RuntimeError(f"panne imprévue {KEY}"))
    buf = io.StringIO()
    try:
        with contextlib.redirect_stderr(buf):
            code = cli.main(["voice", str(p)])
    finally:
        voice.generate = orig
    OUTPUTS.append(buf.getvalue())
    check("exception imprévue : code 1, trace et message ÉCHEC sans la clé", code == 1 and KEY not in buf.getvalue()
          and "<secret>" in buf.getvalue(), buf.getvalue().strip().splitlines()[-1][:120])
    env = dict(os.environ, ELEVENLABS_API_KEY="mauvaise-cle")
    cp = subprocess.run([PY, str(ROOT / "mograph.py"), "voice", "--quota"], cwd=ROOT, env=env, capture_output=True,
                        text=True, encoding="utf-8", errors="replace")
    OUTPUTS.append(cp.stdout + cp.stderr)
    check("clé refusée (401) : « vérifiez ELEVENLABS_API_KEY », clé non affichée",
          cp.returncode == 1 and "401" in cp.stderr and "mauvaise-cle" not in cp.stderr, tail(cp, 1))


def test_env_file() -> None:
    print("== E bis. Lecture de .env (fichier de test, jamais le vrai) ==")
    f = TEST / "env_test.env"
    cases = {"commentaire en fin de ligne": ("ELEVENLABS_API_KEY=abc123 # cle perso\n", "abc123"),
             "guillemets puis commentaire": ('ELEVENLABS_API_KEY="abc123" # cle perso\n', "abc123"),
             "doublon : la dernière l'emporte": ("ELEVENLABS_API_KEY=ancienne\nELEVENLABS_API_KEY=nouvelle\n", "nouvelle"),
             "valeur vide puis valeur": ("ELEVENLABS_API_KEY=\nELEVENLABS_API_KEY=abc123\n", "abc123"),
             "export + tabulation": ("export\tELEVENLABS_API_KEY=abc123\n", "abc123"),
             "« # » collé dans la valeur": ("ELEVENLABS_API_KEY=abc#123\n", "abc#123")}
    old = config.ENV_FILE
    config.ENV_FILE = f
    try:
        for label, (content, want) in cases.items():
            f.write_text(content, encoding="utf-8")
            got = voice._read_env_file().get("ELEVENLABS_API_KEY")
            check(f".env : {label}", got == want, repr(got))
        f.write_bytes("# clé ElevenLabs\nELEVENLABS_API_KEY=abc\n".encode("cp1252"))
        try:
            voice._read_env_file()
            check(".env en ANSI : message français", False, "accepté")
        except voice.VoiceError as exc:
            check(".env en ANSI : message français « réenregistrez-le en UTF-8 »", "UTF-8" in str(exc), str(exc)[:80])
    finally:
        config.ENV_FILE = old
        f.unlink(missing_ok=True)


def test_secrets_final() -> None:
    print("== Contrôle final des secrets ==")
    files = [TEST / "voice_usage.jsonl", TEST / "assets.lock.json", *sorted((AI / "voice").glob("*.json")),
             *sorted((TEST / "scenes").glob("*.json")), *sorted((TEST / "out").rglob("qc_report.*"))]
    blob = "\n".join(OUTPUTS) + "\n".join(f.read_text(encoding="utf-8", errors="replace") for f in files if f.is_file())
    check("la clé API n'apparaît dans aucune sortie, journal, verrou, alignement, scène ni rapport", KEY not in blob)
    check("l'identifiant de voix de .env n'apparaît qu'en empreinte (fichiers ET sorties console, erreurs comprises)",
          VOICE not in blob and "empreinte" in blob)


def _strip_voice(raw: dict, **changes) -> dict:
    vo = {k: v for k, v in raw["voiceover"].items() if k not in ("asset", "alignment")}
    return {**raw, "voiceover": {**vo, **changes}}


def test_key_and_cache() -> None:
    print("== F. Confirmation acceptée, empreinte de la requête, cache partagé, --dry-run sans effet ==")
    n = len(Fake.calls)
    cp = mograph("voice", str(TEST / "scenes" / "v_long.json"), "--yes")
    check("au-delà du seuil avec --yes : accepté, exactement un appel", cp.returncode == 0 and len(Fake.calls) == n + 1,
          tail(cp, 1))
    p = scene("v_oui", "Une seconde phrase assez longue pour franchir le seuil de confirmation fixé à quatre-vingts.")
    r = voice.generate(p, ask=lambda prompt: "o", log=lambda m: None)
    check("confirmation « o » : accepté, un appel", r["status"] == "généré" and len(Fake.calls) == n + 2, str(r)[:80])
    p80 = scene("v_80", "Seuil strict " + "a" * 67)
    cp = mograph("voice", str(p80))
    check("exactement au seuil (80 caractères) sans --yes : accepté (seuil strict)",
          cp.returncode == 0 and len(Fake.calls) == n + 3, tail(cp, 1))
    # Composition de l'empreinte : chaque paramètre de la requête compte.
    base_p = scene("v_cle", "Empreinte de la requête.")
    cp = mograph("voice", str(base_p))
    raw = json.loads(base_p.read_text(encoding="utf-8"))
    first = raw["voiceover"]["asset"]
    n = len(Fake.calls)
    seen = {first}
    for label, change in (("voice_id", {"voice_id": "autre-voix-de-test"}), ("model", {"model": "eleven_flash_v2_5"}),
                          ("settings", {"settings": {"stability": 1}}), ("seed", {"seed": 3})):
        pk = TEST / "scenes" / "v_cle.json"
        pk.write_text(json.dumps(_strip_voice(raw, **change), ensure_ascii=False), encoding="utf-8")
        cp = mograph("voice", str(pk))
        asset = json.loads(pk.read_text(encoding="utf-8"))["voiceover"].get("asset")
        check(f"empreinte : changer {label} -> un nouvel appel, un nouveau fichier",
              cp.returncode == 0 and len(Fake.calls) == n + 1 and asset not in seen, tail(cp, 1))
        seen.add(asset)
        n = len(Fake.calls)
    pk = TEST / "scenes" / "v_cle.json"
    pk.write_text(json.dumps(_strip_voice(raw, settings={"stability": 1.0}), ensure_ascii=False), encoding="utf-8")
    cp = mograph("voice", str(pk))
    check("réglage 1 puis 1.0 : même empreinte, cache (0 appel)", cp.returncode == 0 and "cache" in cp.stdout
          and len(Fake.calls) == n, tail(cp, 1))
    pk.write_text(json.dumps(_strip_voice(raw), ensure_ascii=False), encoding="utf-8")
    cp = mograph("voice", str(pk))
    check("retour à la requête d'origine : cache, 0 appel, fichier d'origine", cp.returncode == 0 and len(Fake.calls) == n
          and json.loads(pk.read_text(encoding="utf-8"))["voiceover"]["asset"] == first, tail(cp, 1))
    bis = TEST / "scenes" / "v_cle_bis.json"
    bis.write_text(json.dumps({**_strip_voice(raw), "id": "v_cle_bis"}, ensure_ascii=False), encoding="utf-8")
    cp = mograph("voice", str(bis))
    check("même requête depuis une AUTRE scène : cache partagé, 0 appel, même fichier gelé",
          cp.returncode == 0 and "cache" in cp.stdout and len(Fake.calls) == n
          and json.loads(bis.read_text(encoding="utf-8"))["voiceover"]["asset"] == first, tail(cp, 1))
    # --dry-run sur une voix en cache : n'écrit ni la scène ni le verrou.
    ter = TEST / "scenes" / "v_cle_ter.json"
    ter.write_text(json.dumps({**_strip_voice(raw), "id": "v_cle_ter"}, ensure_ascii=False), encoding="utf-8")
    before = (ter.read_bytes(), config.ASSETS_LOCK.read_bytes())
    cp = mograph("voice", str(ter), "--dry-run")
    u = usage()[-1]
    check("--dry-run sur une voix en cache : « en cache », scène et verrou inchangés octet pour octet, journal dry_run",
          cp.returncode == 0 and "en cache" in cp.stdout and (ter.read_bytes(), config.ASSETS_LOCK.read_bytes()) == before
          and u.get("dry_run") is True and u.get("cache") is True, tail(cp, 1))


def test_robustness() -> None:
    print("== G. Voix disparue, réponse payée conservée, réponses illisibles, Ctrl+C, concurrence ==")
    p = scene("v_perdu", "Voix qui va disparaître.")
    mograph("voice", str(p))
    vo = json.loads(p.read_text(encoding="utf-8"))["voiceover"]
    media = [config.ROOT / vo["asset"], config.ROOT / vo["alignment"]]
    for f in media:
        f.unlink()
    n = len(Fake.calls)
    cp = mograph("voice", str(p))
    check("voix gelée disparue : REFUS sans appel (« restaurez », « --regenerate »), sans trace",
          cp.returncode == 1 and len(Fake.calls) == n and "restaurez" in cp.stderr and "--regenerate" in cp.stderr
          and "Traceback" not in cp.stderr, tail(cp, 1))
    cpv = mograph("validate", str(p))
    check("validate : « restaurez-le », régénération seulement par --regenerate", cpv.returncode == 1
          and "restaurez-le" in cpv.stderr and "--regenerate" in cpv.stderr, tail(cpv, 1))
    Fake.extra_s = 0.3
    cp = mograph("voice", str(p), "--regenerate")
    Fake.extra_s = 0.0
    e = assets.load_lock()["assets"].get(vo["asset"]) or {}
    cpv = mograph("validate", str(p))
    check("--regenerate : un appel, mêmes chemins regelés (remplacement tracé), validate OK",
          cp.returncode == 0 and len(Fake.calls) == n + 1 and e.get("replaces") and cpv.returncode == 0
          and json.loads(p.read_text(encoding="utf-8"))["voiceover"]["asset"] == vo["asset"], tail(cp, 1))
    data = media[0].read_bytes()
    media[0].write_bytes(data[:-1] + bytes([data[-1] ^ 1]))
    n = len(Fake.calls)
    cp = mograph("voice", str(p))
    check("fichier gelé modifié d'un octet : REFUS sans appel (« modifié »)", cp.returncode == 1 and "modifié" in cp.stderr
          and len(Fake.calls) == n, tail(cp, 1))
    media[0].write_bytes(data)
    # 200 illisible après l'envoi : l'appel a pu être décompté.
    ph = scene("v_html", "Réponse illisible.")
    Fake.post_mode = "html"
    cp = mograph("voice", str(ph))
    u = usage()[-1]
    check("réponse 200 illisible : message français « a pu être décompté », sans trace, appel journalisé",
          cp.returncode == 1 and "illisible" in cp.stderr and "a pu être décompté" in cp.stderr
          and "Traceback" not in cp.stderr and u.get("scene") == "v_html" and u.get("erreur"), tail(cp, 1))
    # Alignement inexploitable : la réponse payée est conservée, la relance ne repaie pas.
    pa = scene("v_align", "Alignement incomplet.")
    Fake.post_mode = "badalign"
    n = len(Fake.calls)
    cp = mograph("voice", str(pa))
    pend = sorted((AI / "voice").glob(".*.reponse.json"))
    u = usage()[-1]
    check("alignement inexploitable après l'appel : REFUS, réponse conservée, appel journalisé",
          cp.returncode == 1 and "conservée" in cp.stderr and len(pend) == 1 and len(Fake.calls) == n + 1
          and u.get("scene") == "v_align" and u.get("cache") is False, tail(cp, 1))
    cp = mograph("voice", str(pa))
    check("relance : même refus, AUCUN nouvel appel (réponse payée réutilisée)", cp.returncode == 1
          and len(Fake.calls) == n + 1, tail(cp, 1))
    if pend:
        d = json.loads(pend[0].read_text(encoding="utf-8"))
        d["alignment"] = fake_alignment("Alignement incomplet.")
        pend[0].write_text(json.dumps(d), encoding="utf-8")
    cp = mograph("voice", str(pa))
    vo = json.loads(pa.read_text(encoding="utf-8"))["voiceover"]
    check("réponse conservée exploitable : reprise sans appel, gelée, réponse en attente supprimée",
          cp.returncode == 0 and "reprise" in cp.stdout and len(Fake.calls) == n + 1 and vo.get("asset")
          and not any(f.exists() for f in pend) and assets.status_of(config.ROOT / vo["asset"]) == "ok", tail(cp, 1))
    # Historique illisible : génération réussie, décompte null.
    ps = scene("v_histo", "Historique illisible.")
    Fake.history_broken = True
    cp = mograph("voice", str(ps))
    Fake.history_broken = False
    vo = json.loads(ps.read_text(encoding="utf-8"))["voiceover"]
    e = assets.load_lock()["assets"].get(vo.get("asset"), {})
    check("historique illisible : génération réussie, chars_billed null journalisé, coût « décompte non lu »",
          cp.returncode == 0 and usage()[-1].get("chars_billed") is None
          and "décompte non lu" in (e.get("provenance") or {}).get("cost", ""), tail(cp, 1))
    # Ctrl+C pendant l'appel : journalisé, message clair.
    import contextlib
    import io
    import mograph as cli
    orig = voice._tts
    voice._tts = lambda k, prm: (_ for _ in ()).throw(KeyboardInterrupt())
    buf = io.StringIO()
    try:
        with contextlib.redirect_stderr(buf):
            code = cli.main(["voice", str(scene("v_ctrlc", "Interruption pendant l'appel."))])
    finally:
        voice._tts = orig
    check("Ctrl+C pendant l'appel : code 1, « a pu être décompté », ligne de journal « interrompu »",
          code == 1 and "a pu être décompté" in buf.getvalue() and usage()[-1].get("erreur") == "interrompu pendant l'appel",
          buf.getvalue().strip()[-120:])
    # Deux commandes simultanées sur la même requête : un seul appel.
    pc = scene("v_concur", "Deux commandes en même temps.")
    Fake.post_mode = "slow"
    n = len(Fake.calls)
    procs = [subprocess.Popen([PY, str(ROOT / "mograph.py"), "voice", str(pc)], cwd=ROOT, env=dict(os.environ),
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL, text=True,
                              encoding="utf-8", errors="replace") for _ in range(2)]
    outs = [pr.communicate(timeout=300) for pr in procs]
    OUTPUTS.extend(o[0] + o[1] for o in outs)
    check("deux « mograph voice » simultanés : un seul appel, l'autre attend puis trouve le cache",
          all(pr.returncode == 0 for pr in procs) and len(Fake.calls) == n + 1 and sum("cache" in o[0] for o in outs) == 1,
          " / ".join(o[0].strip().splitlines()[-1][:60] for o in outs if o[0].strip()))


def test_timing(p: Path) -> None:
    print("== H. Justesse : marqueurs, ponctuation, sous-titres, départ de la voix, aperçu ==")
    for text, want in ((" Bonjour [[a]] monde.", ("Bonjour monde.", [("a", 8)])),
                       ("  Intro.\n\n[[deux]] Suite ici.", ("Intro.\n\nSuite ici.", [("deux", 8)])),
                       ("[[a]] [[b]] Bonjour le monde.", ("Bonjour le monde.", [("a", 0), ("b", 0)]))):
        got = voice.split_markers(text)
        check(f"marqueurs : {text.strip()[:24]!r} -> positions dans le texte final", got == want, str(got))
    for text, want in (("« Bonjour », dit-elle.", ["« Bonjour »,", "dit-elle."]),
                       ("— Bonjour ! Comment ?", ["— Bonjour !", "Comment ?"]),
                       ("Il dit « vraiment » oui", ["Il", "dit", "« vraiment »", "oui"])):
        got = [w["text"] for w in voice.words_from_alignment(text, fake_alignment(text))]
        check(f"mots prononcés : {text!r} -> word:0 = premier mot dit", got == want, str(got))
    text = "Puis « C'est fini. » Ensuite autre."
    cues = [c[2] for c in voice.cues_from_words(voice.words_from_alignment(text, fake_alignment(text)))]
    check("sous-titres : coupe après « fini. » » (ponctuation fermante après le point)",
          cues == ["Puis « C'est fini. »", "Ensuite autre."], str(cues))
    nfd = {"characters": ["C", "a", "f", "e\u0301", " ", "n", "o", "i", "r"],
           "character_start_times_seconds": [i * 0.1 for i in range(9)],
           "character_end_times_seconds": [(i + 1) * 0.1 for i in range(9)]}
    try:
        w = voice.words_from_alignment("Café noir", nfd)
        check("accent décomposé dans l'alignement : temps par point de code, sans erreur",
              [(x["text"], x["start"]) for x in w] == [("Café", 0.0), ("noir", 0.5)], str(w))
    except Exception as exc:  # noqa: BLE001
        check("accent décomposé dans l'alignement : temps par point de code, sans erreur", False, repr(exc))
    raw = json.loads(p.read_text(encoding="utf-8"))
    for sid, change, needle in (("v_neg", {"voiceover": {**raw["voiceover"], "start": "beat:0-0.5"}, "audio": {"bpm": 120}},
                                 "avant le début de la vidéo"),
                                ("v_tard", {"voiceover": {**raw["voiceover"], "start": 5.0},
                                            "shots": [{"id": "a", "duration": 2.0, "layers": [{"id": "t", "type": "text", "text": "x"}]}]},
                                 "démarre à 5 s")):
        pk = TEST / "scenes" / f"{sid}.json"
        pk.write_text(json.dumps({**raw, **change, "id": sid}, ensure_ascii=False), encoding="utf-8")
        cp = mograph("validate", str(pk))
        check(f"départ de voix {change['voiceover']['start']!r} : REFUS (« {needle} »)", cp.returncode == 1 and needle in cp.stderr,
              tail(cp, 1))
    from pipeline import preview
    api = preview._State(p, log=lambda m: None).api()
    check("aperçu sans musique : voix servie avec son décalage (audio_offset = voiceover.start = 0,4 s)",
          (api.get("audio") or "").endswith(".mp3") and api.get("audio_offset") == 0.4, str({k: api.get(k) for k in ("audio", "audio_offset", "error")}))


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
        test_timing(p)
        test_mix_and_render(p)
        test_errors_and_secrets()
        test_env_file()
        test_key_and_cache()
        test_robustness()
        test_secrets_final()
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
