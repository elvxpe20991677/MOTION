"""Voix off ElevenLabs : génération UNE fois, garde-fou de quota, cache, alignement mot à mot, gel.

Principe (feuille de route, CONTRACT §11.5) : le rendu n'appelle jamais une IA. « mograph voice »
génère la voix d'un bloc ``voiceover`` de la scène par l'API ElevenLabs « text-to-speech with
timestamps » (l'alignement caractère par caractère est renvoyé par le MÊME appel : aucun coût
supplémentaire), en déduit les mots (début, fin), puis GÈLE le son et l'alignement sous
assets/ai/voice/ (verrou assets.lock.json, source « api »). La scène reçoit les chemins gelés.

Garde-fou : budget de caractères par commande (config.VOICE_BUDGET_CHARS, --budget), quota restant
lu sur le compte AVANT l'appel, confirmation au-delà de config.VOICE_CONFIRM_ABOVE, cache par
empreinte de la requête (texte + voix + modèle + réglages + format) : relancer coûte 0 caractère.
Chaque commande est journalisée dans config.VOICE_USAGE_LOG. La clé API n'est jamais affichée.
"""

from __future__ import annotations

import base64
import datetime as _dt
import hashlib
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from pipeline import config

ALIGN_CONTRACT = "mograph-voice-alignment/1"
_MARKER_RE = re.compile(r"\[\[([A-Za-z_][A-Za-z0-9_]*)\]\]")
_WORDISH = re.compile(r"\w", re.UNICODE)


class VoiceError(RuntimeError):
    """Erreur destinée à l'utilisateur (message en français, actionnable, sans la clé)."""


# ---------------------------------------------------------------------------
# Configuration locale (.env) — la clé n'est jamais affichée ni écrite ailleurs
# ---------------------------------------------------------------------------

def _env_value(name: str) -> str | None:
    """Variable d'environnement, sinon ligne NAME=valeur de config.ENV_FILE (jamais affichée)."""
    if os.environ.get(name):
        return os.environ[name]
    if config.ENV_FILE.is_file():
        for line in config.ENV_FILE.read_text(encoding="utf-8-sig").splitlines():
            s = line.strip()
            if s.startswith("#") or "=" not in s:
                continue
            k, v = s.split("=", 1)
            if k.strip().removeprefix("export ").strip() == name:
                v = v.strip().strip('"').strip("'")
                return v or None
    return None


def api_key() -> str:
    key = _env_value("ELEVENLABS_API_KEY")
    if not key:
        raise VoiceError("Clé ElevenLabs absente : copiez .env.example en .env et renseignez ELEVENLABS_API_KEY "
                         "(le fichier .env n'est jamais versionné).")
    return key


def _voice_label(voice_id: str, from_env: bool) -> str:
    # Identifiant venu de .env : seule son empreinte est consignée (règle : rien de .env dans le dépôt).
    if from_env:
        return f"voix par défaut de .env (empreinte {hashlib.sha256(voice_id.encode()).hexdigest()[:10]})"
    return f"voice_id {voice_id}"


# ---------------------------------------------------------------------------
# Texte, marqueurs, requête
# ---------------------------------------------------------------------------

def split_markers(text: str) -> tuple[str, list[tuple[str, int]]]:
    """Retire les marqueurs « [[id]] » du texte dit ; renvoie (texte envoyé à l'IA, [(id, position)]).

    La position est l'indice, dans le texte nettoyé, du caractère qui suit le marqueur : le marqueur
    vaudra le début du mot suivant (marker:id). Un seul espace est conservé autour d'un marqueur retiré.
    """
    out, marks, i = [], [], 0
    for m in _MARKER_RE.finditer(text):
        out.append(text[i:m.start()])
        i = m.end()
        clean = "".join(out)
        if clean.endswith((" ", "\n")) and text[i:i + 1] in (" ", "\n"):
            i += 1
        marks.append((m.group(1), len(clean)))
    out.append(text[i:])
    return "".join(out).strip(), marks


def request_params(vo: dict) -> tuple[dict, str, bool]:
    """Paramètres EXACTS de la requête TTS (servent aussi d'empreinte de cache), libellé de voix, voix de .env ?"""
    text, _ = split_markers(vo["text"])
    if not text:
        raise VoiceError("voiceover.text est vide une fois les marqueurs [[id]] retirés.")
    voice_id, from_env = vo.get("voice_id"), False
    if not voice_id:
        voice_id, from_env = _env_value("ELEVENLABS_VOICE_ID"), True
        if not voice_id:
            raise VoiceError("Aucune voix : ajoutez voiceover.voice_id à la scène, ou ELEVENLABS_VOICE_ID dans .env.")
    params = {"text": text, "model_id": vo.get("model") or config.VOICE_DEFAULT_MODEL, "voice_id": voice_id,
              "output_format": config.VOICE_OUTPUT_FORMAT}
    if vo.get("settings"):
        params["voice_settings"] = dict(sorted(vo["settings"].items()))
    if vo.get("language"):
        params["language_code"] = vo["language"]
    if vo.get("seed") is not None:
        params["seed"] = int(vo["seed"])
    return params, _voice_label(voice_id, from_env), from_env


def request_key(params: dict) -> str:
    return hashlib.sha256(json.dumps(params, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
                          .encode("utf-8")).hexdigest()


def billed_chars(params: dict) -> int:
    """Caractères décomptés par ElevenLabs : ceux du texte envoyé (espaces et ponctuation compris)."""
    return len(params["text"])


# ---------------------------------------------------------------------------
# API (bibliothèque standard ; erreurs traduites, jamais d'en-tête dans un message)
# ---------------------------------------------------------------------------

def _http(method: str, path: str, key: str, body: dict | None = None) -> dict:
    url = config.ELEVENLABS_API_BASE.rstrip("/") + path
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"xi-api-key": key, "Accept": "application/json",
                                          **({"Content-Type": "application/json"} if data else {})})
    try:
        with urllib.request.urlopen(req, timeout=config.VOICE_HTTP_TIMEOUT_S) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = json.loads(exc.read().decode("utf-8", "replace")).get("detail", "")
        except (ValueError, AttributeError):
            pass
        if isinstance(detail, dict):
            detail = detail.get("message") or detail.get("status") or ""
        hints = {401: "clé refusée : vérifiez ELEVENLABS_API_KEY dans .env",
                 402: "quota ou abonnement insuffisant",
                 404: "voix ou modèle introuvable : vérifiez voiceover.voice_id et voiceover.model",
                 422: "requête refusée (texte, réglages ou format)",
                 429: "trop de requêtes ou quota atteint : réessayez plus tard"}
        # Chemin cité SANS l'identifiant de voix (il peut venir de .env, jamais affiché).
        shown = re.sub(r"/text-to-speech/[^/]+/", "/text-to-speech/<voix>/", path.split("?")[0])
        raise VoiceError(f"ElevenLabs a répondu {exc.code} sur {shown} : "
                         f"{hints.get(exc.code, 'erreur du service')}{' — ' + str(detail)[:200] if detail else ''}.") from None
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise VoiceError(f"ElevenLabs injoignable ({type(exc).__name__}) : vérifiez la connexion, puis relancez.") from None


def subscription(key: str) -> dict:
    """Quota du compte (lecture seule, aucune consommation)."""
    s = _http("GET", "/v1/user/subscription", key)
    used, limit = int(s.get("character_count") or 0), int(s.get("character_limit") or 0)
    reset = s.get("next_character_count_reset_unix")
    return {"tier": s.get("tier"), "used": used, "limit": limit, "remaining": max(0, limit - used),
            "reset": _dt.datetime.fromtimestamp(reset, _dt.timezone.utc).date().isoformat() if reset else None,
            "can_extend": bool(s.get("can_extend_character_limit"))}


def billed_from_history(key: str, params: dict, since_unix: float) -> int | None:
    """Caractères RÉELLEMENT décomptés pour cette génération, lus dans l'historique du compte
    (GET /v1/history : character_count_change_from / _to) ; None si l'élément n'y figure pas encore.

    Le compteur agrégé de /v1/user/subscription peut rester en retard plusieurs minutes (mesuré le
    01/10/2026 : inchangé juste après un appel décompté 11 caractères) : il ne sert pas à mesurer.
    """
    try:
        h = _http("GET", "/v1/history?page_size=10", key)
    except VoiceError:
        return None
    for item in h.get("history") or []:
        if (item.get("text") == params["text"] and item.get("model_id") == params["model_id"]
                and float(item.get("date_unix") or 0) >= since_unix - 120):
            a, b = item.get("character_count_change_from"), item.get("character_count_change_to")
            if isinstance(a, int) and isinstance(b, int):
                return b - a
    return None


def _tts(key: str, params: dict) -> tuple[bytes, dict]:
    body = {k: v for k, v in params.items() if k not in ("voice_id", "output_format")}
    path = (f"/v1/text-to-speech/{urllib.parse.quote(params['voice_id'], safe='')}/with-timestamps"
            f"?output_format={urllib.parse.quote(params['output_format'])}")
    r = _http("POST", path, key, body)
    try:
        audio = base64.b64decode(r["audio_base64"])
        alignment = r["alignment"]
    except (KeyError, TypeError, ValueError) as exc:
        raise VoiceError(f"Réponse ElevenLabs inattendue (champ manquant : {exc}).") from None
    if not audio:
        raise VoiceError("ElevenLabs a renvoyé un son vide.")
    return audio, alignment


# ---------------------------------------------------------------------------
# Alignement -> mots, mots -> sous-titres
# ---------------------------------------------------------------------------

def words_from_alignment(text: str, alignment: dict) -> list[dict]:
    """Mots (début, fin, position dans le texte) depuis l'alignement CARACTÈRE par caractère.

    Un mot = suite de caractères non blancs ; la ponctuation isolée (« — », « … ») est rattachée au
    mot précédent, pour que word:N compte les mots prononcés.
    """
    chars = alignment.get("characters") or []
    t0 = alignment.get("character_start_times_seconds") or []
    t1 = alignment.get("character_end_times_seconds") or []
    if "".join(chars) != text or not (len(chars) == len(t0) == len(t1)):
        raise VoiceError("Alignement ElevenLabs incohérent avec le texte envoyé (caractères différents) : relancez "
                         "« mograph voice » ; si cela persiste, simplifiez la ponctuation du texte.")
    words: list[dict] = []
    i, n = 0, len(text)
    while i < n:
        if text[i].isspace():
            i += 1
            continue
        j = i
        while j < n and not text[j].isspace():
            j += 1
        token = text[i:j]
        w = {"text": token, "start": round(float(t0[i]), 6), "end": round(float(t1[j - 1]), 6),
             "char_start": i, "char_end": j}
        if not _WORDISH.search(token) and words:
            prev = words[-1]
            prev.update({"text": text[prev["char_start"]:j], "end": max(prev["end"], w["end"]), "char_end": j})
        else:
            words.append(w)
        i = j
    if not words:
        raise VoiceError("Aucun mot dans l'alignement renvoyé.")
    return words


def cues_from_words(words: list[dict]) -> list[tuple[float, float, str]]:
    """Répliques de sous-titres (temps globaux) : coupe après une fin de phrase, sur un silence, ou
    quand la réplique dépasserait config.VOICE_SUB_MAX_CHARS caractères ou VOICE_SUB_MAX_S secondes."""
    cues, cur = [], []

    def flush():
        if cur:
            cues.append([cur[0]["start"], cur[-1]["end"], " ".join(w["text"] for w in cur)])
            cur.clear()

    for w in words:
        if cur:
            text_len = len(" ".join(x["text"] for x in cur)) + 1 + len(w["text"])
            if (cur[-1]["text"].endswith((".", "!", "?", "…", ";", ":"))
                    or w["start"] - cur[-1]["end"] > config.VOICE_SUB_GAP_S
                    or text_len > config.VOICE_SUB_MAX_CHARS
                    or w["end"] - cur[0]["start"] > config.VOICE_SUB_MAX_S):
                flush()
        cur.append(w)
    flush()
    # Tenue après la fin de la réplique (lecture confortable), sans chevaucher la suivante.
    for k, c in enumerate(cues):
        nxt = cues[k + 1][0] if k + 1 < len(cues) else c[1] + config.VOICE_SUB_HOLD_S
        c[1] = round(min(c[1] + config.VOICE_SUB_HOLD_S, nxt), 6)
    return [(c[0], c[1], c[2]) for c in cues]


# ---------------------------------------------------------------------------
# Génération (commande « mograph voice »)
# ---------------------------------------------------------------------------

def _interactive() -> bool:
    """Vrai seulement devant un vrai terminal : sous Windows, NUL (stdin vide) se déclare « tty » ;
    GetConsoleMode, lui, n'accepte qu'une vraie console."""
    try:
        if sys.stdin is None or not sys.stdin.isatty():
            return False
        if config.IS_WINDOWS:
            import ctypes
            import msvcrt

            mode = ctypes.c_ulong()
            return bool(ctypes.windll.kernel32.GetConsoleMode(msvcrt.get_osfhandle(sys.stdin.fileno()), ctypes.byref(mode)))
        return True
    except (AttributeError, ValueError, OSError):
        return False


def _log_usage(entry: dict) -> None:
    path = config.VOICE_USAGE_LOG
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n")


def _paths(scene_id: str, key: str) -> tuple[Path, Path]:
    ext = config.VOICE_OUTPUT_FORMAT.split("_", 1)[0]
    base = config.AI_ASSETS_DIR / "voice" / f"{scene_id}_{key[:12]}"
    return base.with_suffix("." + ext), base.with_name(base.name + ".words.json")


def _write_atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def generate(scene_path: Path, *, yes: bool = False, dry_run: bool = False, budget: int | None = None,
             log=print, ask=None) -> dict:
    """Génère (ou retrouve en cache) la voix du bloc voiceover de la scène, la gèle et l'inscrit dans la scène.

    Renvoie un bilan {status, chars, ...} ; status ∈ {« cache », « généré », « estimation »}.
    Lève VoiceError si un garde-fou refuse (budget, quota, confirmation absente) : rien n'est consommé.
    """
    from pipeline import assets

    raw = json.loads(scene_path.read_text(encoding="utf-8-sig"))
    vo = raw.get("voiceover")
    if not vo or not vo.get("text"):
        raise VoiceError(f"{scene_path.name} n'a pas de bloc « voiceover » avec un « text » : ajoutez-le (README §3).")
    scene_id = raw.get("id") or scene_path.stem
    params, voice_label, _ = request_params(vo)
    key = request_key(params)
    chars = billed_chars(params)
    audio_path, align_path = _paths(scene_id, key)
    rel = lambda p: assets.lock_key(p)  # noqa: E731 — clé du verrou = chemin écrit dans la scène
    base_entry = {"date": _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "scene": scene_id,
                  "request_sha256": key, "model": params["model_id"], "voice": voice_label, "chars_estimated": chars}

    # 1) Cache : même requête déjà générée et gelée -> 0 caractère.
    cached = False
    if audio_path.is_file() and align_path.is_file():
        try:
            cached = json.loads(align_path.read_text(encoding="utf-8")).get("request_sha256") == key
        except (OSError, ValueError):
            cached = False
    if cached:
        for p in (audio_path, align_path):
            st = assets.status_of(p)
            if st == "modifié":
                raise VoiceError(f"{rel(p)} a été modifié depuis son gel : restaurez-le, ou supprimez-le (avec son "
                                 "entrée du verrou) pour régénérer.")
            if st == "non verrouillé":
                # Fichiers écrits mais gel interrompu : ce sont bien les sorties de cette requête, on les gèle.
                _freeze(p, params, voice_label, chars, key, tier=None)
        _write_scene(scene_path, raw, rel(audio_path), rel(align_path))
        _log_usage({**base_entry, "cache": True, "chars_billed": 0})
        log(f"cache : voix déjà générée ({rel(audio_path)}) — 0 caractère consommé.")
        return {"status": "cache", "chars": 0, "asset": rel(audio_path), "alignment": rel(align_path)}

    # 2) Garde-fous AVANT tout appel payant.
    limit = config.VOICE_BUDGET_CHARS if budget is None else int(budget)
    key_api = api_key()
    quota = subscription(key_api)
    log(f"estimation : {chars} caractère(s) ({params['model_id']}, {voice_label}) ; quota {quota['tier']} : "
        f"{quota['remaining']} restant(s) sur {quota['limit']} (remise à zéro le {quota['reset']}) ; budget de la "
        f"commande : {limit}.")
    if dry_run:
        _log_usage({**base_entry, "cache": False, "chars_billed": 0, "dry_run": True, "quota_remaining_before": quota["remaining"]})
        return {"status": "estimation", "chars": chars, "quota": quota}
    if chars > limit:
        raise VoiceError(f"{chars} caractères dépassent le budget de la commande ({limit}) : raccourcissez le texte ou "
                         f"relancez avec --budget {chars} (budget par défaut : MOGRAPH_VOICE_BUDGET).")
    if chars > quota["remaining"]:
        raise VoiceError(f"{chars} caractères demandés, {quota['remaining']} restant(s) sur le quota {quota['tier']} "
                         f"(remise à zéro le {quota['reset']}) : génération refusée, rien n'a été consommé.")
    if chars > config.VOICE_CONFIRM_ABOVE and not yes:
        prompt = (f"Consommer {chars} caractères ElevenLabs sur les {quota['remaining']} restants ? [o/N] ")
        if ask is None and not _interactive():
            raise VoiceError(f"{chars} caractères (> seuil de confirmation {config.VOICE_CONFIRM_ABOVE}) : confirmez en "
                             "relançant avec --yes (aucune confirmation possible sans terminal).")
        try:
            answer = (ask or input)(prompt)
        except (EOFError, KeyboardInterrupt):
            answer = ""  # entrée fermée ou interruption : pas de confirmation, rien n'est consommé
        if str(answer).strip().lower() not in ("o", "oui", "y", "yes"):
            raise VoiceError("Génération annulée : rien n'a été consommé.")

    # 3) Appel unique, gel, inscription dans la scène, journal.
    t_call = _dt.datetime.now(_dt.timezone.utc).timestamp()
    audio, alignment = _tts(key_api, params)
    words = words_from_alignment(params["text"], alignment)
    doc = {"contract": ALIGN_CONTRACT, "text": params["text"], "request_sha256": key, "model": params["model_id"],
           "voice": voice_label, "output_format": params["output_format"],
           "duration": round(max(w["end"] for w in words), 6), "characters_billed": chars, "words": words}
    _write_atomic(audio_path, audio)
    _write_atomic(align_path, (json.dumps(doc, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
    billed = billed_from_history(key_api, params, t_call)
    for p in (audio_path, align_path):
        _freeze(p, params, voice_label, chars, key, tier=quota["tier"], billed=billed)
    _write_scene(scene_path, raw, rel(audio_path), rel(align_path))
    _log_usage({**base_entry, "cache": False, "chars_billed": billed, "quota_remaining_before": quota["remaining"]})
    log(f"généré : {rel(audio_path)} ({len(words)} mots, {doc['duration']} s) — "
        + (f"{billed} caractère(s) décompté(s) par ElevenLabs (historique du compte)" if billed is not None
           else f"au plus {chars} caractère(s) (décompte pas encore visible dans l'historique)")
        + f" ; restant avant l'appel : {quota['remaining']}.")
    return {"status": "généré", "chars": billed if billed is not None else chars, "billed": billed, "estimated": chars,
            "asset": rel(audio_path), "alignment": rel(align_path), "words": len(words)}


def _freeze(path: Path, params: dict, voice_label: str, chars: int, key: str, tier: str | None,
            billed: int | None = None) -> None:
    from pipeline import assets

    cost = (f"{billed} caractères ElevenLabs décomptés ({chars} envoyés)" if billed is not None
            else f"au plus {chars} caractères ElevenLabs (décompte non lu)")
    assets.add(path, tool="ElevenLabs", model=params["model_id"], prompt=params["text"],
               license=f"Conditions d'utilisation ElevenLabs{f' (offre {tier})' if tier else ''}",
               seed=params.get("seed"), cost=cost, source="api",
               notes=f"{voice_label} ; requête {key[:12]} ; with-timestamps")


def _write_scene(scene_path: Path, raw: dict, asset: str, alignment: str) -> None:
    vo = raw["voiceover"]
    if vo.get("asset") == asset and vo.get("alignment") == alignment:
        return
    vo["asset"], vo["alignment"] = asset, alignment
    tmp = scene_path.with_name(scene_path.name + ".tmp")
    tmp.write_text(json.dumps(raw, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    os.replace(tmp, scene_path)


def load_alignment(path: Path) -> dict:
    """Alignement gelé, vérifié (contrat, mots ordonnés)."""
    try:
        doc = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise VoiceError(f"Alignement illisible {path} ({exc}).") from None
    words = doc.get("words") if isinstance(doc, dict) else None
    if doc.get("contract") != ALIGN_CONTRACT or not isinstance(words, list) or not words:
        raise VoiceError(f"{path} n'est pas un alignement mograph ({ALIGN_CONTRACT}) : régénérez-le (mograph voice).")
    for w in words:
        if not (isinstance(w, dict) and isinstance(w.get("text"), str) and 0 <= float(w.get("start", -1)) <= float(w.get("end", -1))):
            raise VoiceError(f"{path} : mot mal formé ({str(w)[:80]}).")
    return doc
