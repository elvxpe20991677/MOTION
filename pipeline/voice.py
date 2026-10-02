"""Voix off ElevenLabs : génération UNE fois, garde-fou de quota, cache, alignement mot à mot, gel.

Principe (feuille de route, CONTRACT §11.5) : le rendu n'appelle jamais une IA. « mograph voice »
génère la voix d'un bloc ``voiceover`` de la scène par l'API ElevenLabs « text-to-speech with
timestamps » (l'alignement caractère par caractère est renvoyé par le MÊME appel : aucun coût
supplémentaire), en déduit les mots (début, fin), puis GÈLE le son et l'alignement sous
assets/ai/voice/ (verrou assets.lock.json, source « api »). La scène reçoit les chemins gelés.

Garde-fou : budget de caractères par commande (config.VOICE_BUDGET_CHARS, --budget), limite par
requête du modèle, quota restant lu sur le compte AVANT l'appel, confirmation au-delà de
config.VOICE_CONFIRM_ABOVE, cache par empreinte de la requête (texte + voix + modèle + réglages +
langue + graine + format), partagé entre scènes : rejouer une requête déjà gelée coûte 0 caractère.
Une voix déjà gelée dont les fichiers ont disparu n'est JAMAIS régénérée sans --regenerate. La
réponse payée est conservée dès sa réception : un échec ultérieur ne fait pas repayer. Chaque
commande est journalisée dans config.VOICE_USAGE_LOG. La clé API n'est jamais affichée.
"""

from __future__ import annotations

import base64
import contextlib
import datetime as _dt
import hashlib
import http.client
import json
import os
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from pipeline import config

ALIGN_CONTRACT = "mograph-voice-alignment/1"
PENDING_CONTRACT = "mograph-voice-response/1"
_MARKER_RE = re.compile(r"\[\[([A-Za-z_][A-Za-z0-9_]*)\]\]")
_WORDISH = re.compile(r"\w", re.UNICODE)
# Ponctuation ouvrante : rattachée au mot SUIVANT (« Bonjour », — Bonjour, (note) ; le reste au précédent.
_OPENING = set("«‹“‘„‚([{¿¡—–-")
# Fin de phrase : ponctuation finale, éventuellement suivie de ponctuation fermante (« fini. » », « fini.) »).
_SENTENCE_END = re.compile(r"[.!?…;:][\s  »›”’\"')\]}]*$")
_SECRET_NAMES = ("ELEVENLABS_API_KEY", "ELEVENLABS_VOICE_ID")


class VoiceError(RuntimeError):
    """Erreur destinée à l'utilisateur (message en français, actionnable, sans la clé)."""


class ApiRefused(VoiceError):
    """Le service a refusé la requête (réponse HTTP d'erreur, ou connexion impossible) : rien n'a été produit."""


# ---------------------------------------------------------------------------
# Configuration locale (.env) — la clé n'est jamais affichée ni écrite ailleurs
# ---------------------------------------------------------------------------

def _read_env_file() -> dict[str, str]:
    """Paires NOM=valeur de config.ENV_FILE, lues comme un shell / python-dotenv : guillemets assortis
    retirés, commentaire « # » de fin de ligne ignoré (valeur sans guillemets), « export » accepté,
    la DERNIÈRE occurrence non vide l'emporte."""
    if not config.ENV_FILE.is_file():
        return {}
    try:
        lines = config.ENV_FILE.read_text(encoding="utf-8-sig").splitlines()
    except UnicodeDecodeError:
        raise VoiceError(f"{config.ENV_FILE.name} illisible en UTF-8 (enregistré en ANSI ?) : réenregistrez-le en "
                         "UTF-8.") from None
    except OSError as exc:
        raise VoiceError(f"{config.ENV_FILE.name} illisible ({type(exc).__name__}).") from None
    out: dict[str, str] = {}
    for line in lines:
        s = line.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        k, v = s.split("=", 1)
        k, v = re.sub(r"^export\s+", "", k.strip()).strip(), v.strip()
        if v[:1] in ("'", '"'):
            end = v.find(v[0], 1)
            v = v[1:end] if end > 0 else v[1:]
        else:
            v = re.split(r"\s#", v, maxsplit=1)[0].strip()
        if v:
            out[k] = v
    return out


def _env_value(name: str) -> str | None:
    """Variable d'environnement (blancs de bord retirés), sinon config.ENV_FILE ; jamais affichée."""
    v = (os.environ.get(name) or "").strip()
    return v or _read_env_file().get(name)


def api_key() -> str:
    key = _env_value("ELEVENLABS_API_KEY")
    if not key:
        raise VoiceError("Clé ElevenLabs absente : copiez .env.example en .env et renseignez ELEVENLABS_API_KEY "
                         "(le fichier .env n'est jamais versionné).")
    if not re.fullmatch(r"[!-~]+", key):
        raise VoiceError("ELEVENLABS_API_KEY contient des caractères invalides (espace, retour à la ligne, accent ?) : "
                         "corrigez-la dans .env ou dans l'environnement (valeur non affichée).")
    return key


def secret_values() -> list[str]:
    """Valeurs à ne jamais afficher (clé, voix par défaut), sans jamais lever d'erreur."""
    vals: set[str] = set()
    try:
        env = _read_env_file()
    except VoiceError:
        env = {}
    for n in _SECRET_NAMES:
        for v in (os.environ.get(n), env.get(n)):
            if v:
                vals.update({v, v.strip()})
    return sorted((v for v in vals if len(v) >= 6), key=len, reverse=True)


def redact(text: str) -> str:
    """Texte (message, trace) sans aucune valeur secrète de l'environnement ou de .env."""
    for v in secret_values():
        text = text.replace(v, "<secret>")
    return text


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

    La position est l'indice, dans le texte nettoyé FINAL (blancs de bord retirés), du caractère qui
    suit le marqueur : le marqueur vaudra le début du mot suivant (marker:id). Un seul blanc est
    conservé autour d'un marqueur retiré (marqueurs consécutifs compris).
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
    joined = "".join(out)
    final = joined.strip()
    lead = len(joined) - len(joined.lstrip())
    return final, [(name, min(max(0, pos - lead), len(final))) for name, pos in marks]


def _num(v):
    # 1 et 1.0 : même réglage, même empreinte (une retouche cosmétique ne fait pas repayer).
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else v


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
        params["voice_settings"] = {k: _num(v) for k, v in sorted(vo["settings"].items())}
    if vo.get("language"):
        params["language_code"] = vo["language"]
    if vo.get("seed") is not None:
        params["seed"] = int(vo["seed"])
    return params, _voice_label(voice_id, from_env), from_env


def request_key(params: dict) -> str:
    return hashlib.sha256(json.dumps(params, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
                          .encode("utf-8")).hexdigest()


def billed_chars(params: dict) -> int:
    """Caractères ENVOYÉS (texte, espaces et ponctuation compris) : majorant du décompte, sert aux
    garde-fous. Le décompte réel est lu après l'appel dans l'historique du compte (billed_from_history) :
    mesuré le 01/10/2026, 11 caractères décomptés pour 27 envoyés."""
    return len(params["text"])


# ---------------------------------------------------------------------------
# API (bibliothèque standard ; erreurs traduites, jamais d'en-tête dans un message)
# ---------------------------------------------------------------------------

def _shown(path: str) -> str:
    # Chemin cité SANS l'identifiant de voix (il peut venir de .env, jamais affiché).
    return re.sub(r"/text-to-speech/[^/]+/", "/text-to-speech/<voix>/", path.split("?")[0])


def _http(method: str, path: str, key: str, body: dict | None = None, hide: tuple[str, ...] = ()) -> dict:
    url = config.ELEVENLABS_API_BASE.rstrip("/") + path
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"xi-api-key": key, "Accept": "application/json",
                                          **({"Content-Type": "application/json"} if data else {})})
    try:
        with urllib.request.urlopen(req, timeout=config.VOICE_HTTP_TIMEOUT_S) as r:
            payload = r.read()
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = json.loads(exc.read().decode("utf-8", "replace")).get("detail", "")
        except (ValueError, AttributeError, OSError, http.client.HTTPException):
            pass
        if isinstance(detail, dict):
            detail = detail.get("message") or detail.get("status") or ""
        elif isinstance(detail, list):
            # Erreur de validation : champ et message seulement (jamais « input », qui recopie la valeur).
            detail = " ; ".join(f"{'.'.join(map(str, d.get('loc') or []))} : {d.get('msg')}"
                                for d in detail if isinstance(d, dict))
        detail = str(detail)
        for v in (key, *hide):
            if v:
                detail = detail.replace(v, "<voix>" if v != key else "<clé>")
        detail = redact(detail)
        hints = {400: "requête invalide (texte trop long pour le modèle, réglages ou langue ?)",
                 401: "clé refusée : vérifiez ELEVENLABS_API_KEY dans .env",
                 402: "quota ou abonnement insuffisant",
                 404: "voix ou modèle introuvable : vérifiez voiceover.voice_id (ou ELEVENLABS_VOICE_ID de .env) et "
                      "voiceover.model",
                 422: "requête refusée (texte, réglages ou format)",
                 429: "trop de requêtes ou quota atteint : réessayez plus tard"}
        raise ApiRefused(f"ElevenLabs a répondu {exc.code} sur {_shown(path)} : "
                         f"{hints.get(exc.code, 'erreur du service')}{' — ' + detail[:200] if detail else ''}.") from None
    except urllib.error.URLError as exc:
        # Connexion impossible (avant tout envoi complet) : rien n'a été produit.
        raise ApiRefused(f"ElevenLabs injoignable ({type(exc.reason).__name__}) : vérifiez la connexion, puis "
                         "relancez.") from None
    except (TimeoutError, OSError, ValueError, http.client.HTTPException) as exc:
        # Réponse perdue en cours de route (délai, coupure) : le service a pu traiter la requête.
        raise VoiceError(f"Réponse ElevenLabs perdue sur {_shown(path)} ({type(exc).__name__}).") from None
    try:
        return json.loads(payload.decode("utf-8"))
    except ValueError:
        raise VoiceError(f"Réponse ElevenLabs illisible sur {_shown(path)} (pas du JSON : panne passagère du "
                         "service ?).") from None


def subscription(key: str) -> dict:
    """Quota du compte (lecture seule, aucune consommation)."""
    s = _http("GET", "/v1/user/subscription", key)
    used, limit = int(s.get("character_count") or 0), int(s.get("character_limit") or 0)
    reset = s.get("next_character_count_reset_unix")
    return {"tier": s.get("tier"), "used": used, "limit": limit, "remaining": max(0, limit - used),
            "reset": _dt.datetime.fromtimestamp(reset, _dt.timezone.utc).date().isoformat() if reset else None,
            "can_extend": bool(s.get("can_extend_character_limit"))}


def quota_now(key: str) -> dict:
    """Quota corrigé du retard du compteur agrégé : utilisé = max(compteur de l'abonnement, compteur
    après le dernier élément de l'historique de l'heure écoulée). Lecture seule."""
    q = subscription(key)
    try:
        h = _http("GET", "/v1/history?page_size=5", key)
        recent = [item.get("character_count_change_to") for item in h.get("history") or []
                  if float(item.get("date_unix") or 0) >= time.time() - 3600]
    except (VoiceError, AttributeError, TypeError, ValueError):
        recent = []
    used = max([q["used"], *(v for v in recent if isinstance(v, int))])
    return {**q, "used": used, "remaining": max(0, q["limit"] - used), "lag": used - q["used"]}


def billed_from_history(key: str, params: dict, since_unix: float) -> int | None:
    """Caractères RÉELLEMENT décomptés pour cette génération, lus dans l'historique du compte
    (GET /v1/history : character_count_change_from / _to) ; None si l'élément n'y figure pas encore
    ou si l'historique est illisible (ne lève jamais d'erreur : l'appel payant a déjà eu lieu).

    Le compteur agrégé de /v1/user/subscription peut rester en retard plusieurs minutes (mesuré le
    01/10/2026 : inchangé juste après un appel décompté 11 caractères) : il ne sert pas à mesurer.
    """
    try:
        h = _http("GET", "/v1/history?page_size=10", key)
        for item in h.get("history") or []:
            if (item.get("text") == params["text"] and item.get("model_id") == params["model_id"]
                    and float(item.get("date_unix") or 0) >= since_unix - 120):
                a, b = item.get("character_count_change_from"), item.get("character_count_change_to")
                if isinstance(a, int) and isinstance(b, int):
                    return b - a
    except Exception:  # noqa: BLE001 — mesure facultative : l'absence de décompte est consignée (null)
        return None
    return None


def _tts(key: str, params: dict) -> tuple[bytes, dict]:
    body = {k: v for k, v in params.items() if k not in ("voice_id", "output_format")}
    path = (f"/v1/text-to-speech/{urllib.parse.quote(params['voice_id'], safe='')}/with-timestamps"
            f"?output_format={urllib.parse.quote(params['output_format'])}")
    r = _http("POST", path, key, body, hide=(params["voice_id"],))
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

def _char_times(text: str, alignment: dict) -> tuple[list, list]:
    """Temps de début et de fin de CHAQUE point de code du texte envoyé.

    Un élément de alignment.characters peut porter plusieurs points de code (accent décomposé, émoji
    composé) : ses temps sont répétés pour chacun. Le service peut aussi normaliser un caractère
    (apostrophe typographique…) : seule compte la correspondance position par position.
    """
    chars = alignment.get("characters") or []
    t0 = alignment.get("character_start_times_seconds") or []
    t1 = alignment.get("character_end_times_seconds") or []
    if not (len(chars) == len(t0) == len(t1)) or not chars:
        raise VoiceError("Alignement ElevenLabs incomplet (listes de caractères et de temps de longueurs différentes).")
    for norm in (lambda c: c, lambda c: unicodedata.normalize("NFC", c)):
        s0, s1 = [], []
        for c, a, b in zip(chars, t0, t1):
            k = len(norm(str(c)))
            s0 += [a] * k
            s1 += [b] * k
        if len(s0) == len(text):
            return s0, s1
    raise VoiceError(f"Alignement ElevenLabs incohérent avec le texte envoyé ({len(chars)} caractères alignés pour "
                     f"{len(text)} envoyés) ; si cela persiste, simplifiez la ponctuation du texte.")


def words_from_alignment(text: str, alignment: dict) -> list[dict]:
    """Mots (début, fin, position dans le texte) depuis l'alignement CARACTÈRE par caractère.

    Un mot = suite de caractères non blancs. Pour que word:N compte les mots PRONONCÉS, la ponctuation
    isolée est rattachée à un mot : la ponctuation ouvrante (« ( — …) et celle qui précède le premier
    mot au mot SUIVANT, toute autre (» , … !) au mot précédent.
    """
    t0, t1 = _char_times(text, alignment)
    words: list[dict] = []
    pending: int | None = None          # début d'une ponctuation en attente du mot suivant
    i, n = 0, len(text)
    while i < n:
        if text[i].isspace():
            i += 1
            continue
        j = i
        while j < n and not text[j].isspace():
            j += 1
        token = text[i:j]
        if _WORDISH.search(token):
            s = i if pending is None else pending
            words.append({"text": text[s:j], "start": round(float(t0[s]), 6), "end": round(float(t1[j - 1]), 6),
                          "char_start": s, "char_end": j})
            pending = None
        elif words and pending is None and not set(token) <= _OPENING:
            prev = words[-1]
            prev.update({"text": text[prev["char_start"]:j], "end": max(prev["end"], round(float(t1[j - 1]), 6)),
                         "char_end": j})
        elif pending is None:
            pending = i
        i = j
    if pending is not None and words:
        prev = words[-1]                 # ponctuation ouvrante en toute fin : rattachée au dernier mot
        prev.update({"text": text[prev["char_start"]:n].rstrip(), "end": max(prev["end"], round(float(t1[n - 1]), 6)),
                     "char_end": len(text.rstrip())})
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
            if (_SENTENCE_END.search(cur[-1]["text"])
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


def _voice_dir() -> Path:
    return config.AI_ASSETS_DIR / "voice"


def _ext() -> str:
    return config.VOICE_OUTPUT_FORMAT.split("_", 1)[0]


def _paths(key: str) -> tuple[Path, Path]:
    """Fichiers d'une NOUVELLE génération : nommés par l'empreinte seule (cache partagé entre scènes)."""
    base = _voice_dir() / key[:16]
    return base.with_suffix("." + _ext()), base.with_name(base.name + ".words.json")


def _pending_path(key: str) -> Path:
    # Nom en « . » : ignoré par « assets verify » (fichier de travail, jamais référencé par une scène).
    return _voice_dir() / f".{key[:16]}.reponse.json"


def _write_atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def _doc_key(align_path: Path) -> str | None:
    try:
        return json.loads(align_path.read_text(encoding="utf-8")).get("request_sha256")
    except (OSError, ValueError, AttributeError):
        return None


def _known_pairs(key: str, lock: dict) -> list[tuple[Path, Path]]:
    """Paires (son, alignement) déjà produites pour cette requête, quelle que soit la scène : nom par
    empreinte, entrées du verrou (même requête), puis fichiers du disque (gel interrompu)."""
    from pipeline import assets

    pairs = [_paths(key)]
    tag = f"requête {key[:12]}"
    for k, e in sorted(lock["assets"].items()):
        if k.endswith(".words.json") and tag in str((e.get("provenance") or {}).get("notes") or ""):
            al = config.ROOT / k
            pairs.append((al.with_name(al.name[:-len(".words.json")] + "." + _ext()), al))
    if _voice_dir().is_dir():
        for al in sorted(_voice_dir().glob(f"*{key[:12]}*.words.json")):
            pairs.append((al.with_name(al.name[:-len(".words.json")] + "." + _ext()), al))
    seen, out = set(), []
    for a, al in pairs:
        k = assets.lock_key(al)
        if k not in seen:
            seen.add(k)
            out.append((a, al))
    return out


@contextlib.contextmanager
def _request_lock(key: str, log):
    """Une seule génération à la fois pour une requête donnée : le second processus attend, puis trouve
    la voix en cache (aucune double consommation)."""
    guard = _voice_dir() / f".{key[:16]}.lck"
    guard.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + config.VOICE_HTTP_TIMEOUT_S + 60
    told = False
    while True:
        try:
            fd = os.open(guard, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            break
        except FileExistsError:
            if time.monotonic() > deadline:
                raise VoiceError(f"Une autre commande « mograph voice » génère déjà cette voix ({guard.name}) ; si ce "
                                 f"n'est pas le cas, supprimez {guard} puis relancez.") from None
            if not told:
                log("une autre commande génère cette même voix : attente de sa fin…")
                told = True
            time.sleep(0.1)
    try:
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
        yield
    finally:
        with contextlib.suppress(OSError):
            guard.unlink()


def generate(scene_path: Path, *, yes: bool = False, dry_run: bool = False, budget: int | None = None,
             regenerate: bool = False, log=print, ask=None) -> dict:
    """Génère (ou retrouve en cache) la voix du bloc voiceover de la scène, la gèle et l'inscrit dans la scène.

    Renvoie un bilan {status, chars, ...} ; status ∈ {« cache », « reprise », « généré », « estimation »}.
    Lève VoiceError si un garde-fou refuse (budget, limite du modèle, quota, confirmation absente, voix
    gelée disparue sans --regenerate) : rien n'est consommé.
    """
    from pipeline import assets

    if config.VOICE_ENV_ERRORS:
        raise VoiceError(" ; ".join(config.VOICE_ENV_ERRORS))
    raw = json.loads(scene_path.read_text(encoding="utf-8-sig"))
    vo = raw.get("voiceover")
    if not vo or not vo.get("text"):
        raise VoiceError(f"{scene_path.name} n'a pas de bloc « voiceover » avec un « text » : ajoutez-le (README §3).")
    params, voice_label, _ = request_params(vo)
    key = request_key(params)
    chars = billed_chars(params)
    cap = config.VOICE_MAX_CHARS_PER_REQUEST.get(params["model_id"])
    if cap is not None and chars > cap:
        raise VoiceError(f"{chars} caractères dépassent la limite d'une requête du modèle {params['model_id']} ({cap}) : "
                         "découpez le texte en plusieurs scènes ou blocs.")
    ctx = {"scene_path": scene_path, "raw": raw, "params": params, "voice_label": voice_label, "key": key,
           "chars": chars, "base": {"scene": raw.get("id") or scene_path.stem, "request_sha256": key,
                                    "model": params["model_id"], "voice": voice_label, "chars_estimated": chars}}
    try:
        if dry_run:
            return _generate(ctx, yes=yes, dry_run=True, budget=budget, regenerate=regenerate, log=log, ask=ask)
        with _request_lock(key, log):
            return _generate(ctx, yes=yes, dry_run=False, budget=budget, regenerate=regenerate, log=log, ask=ask)
    except assets.AssetError as exc:
        raise VoiceError(str(exc)) from None


def _now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _generate(ctx: dict, *, yes, dry_run, budget, regenerate, log, ask) -> dict:
    from pipeline import assets

    params, key, chars = ctx["params"], ctx["key"], ctx["chars"]
    rel = assets.lock_key  # clé du verrou = chemin écrit dans la scène
    lock = assets.load_lock()
    pairs = _known_pairs(key, lock)

    # 1) Cache : même requête déjà générée et gelée (dans n'importe quelle scène) -> 0 caractère.
    stale = []
    for a, al in pairs:
        st = {p: assets.status_of(p, lock) for p in (a, al)}
        if a.is_file() and al.is_file() and all(s in ("ok", "non verrouillé") for s in st.values()) \
                and _doc_key(al) == key:
            if dry_run:
                _log_usage({"date": _now(), **ctx["base"], "cache": True, "chars_billed": 0, "dry_run": True})
                log(f"en cache : {rel(a)} — 0 caractère (estimation ; gel et scène complétés sans --dry-run).")
                return {"status": "estimation", "chars": 0, "cache": True}
            for p, s in st.items():
                if s == "non verrouillé":
                    # Fichiers écrits mais gel interrompu : ce sont bien les sorties de cette requête, on les gèle.
                    _freeze(p, ctx, tier=None, billed=None)
            _write_scene(ctx["scene_path"], ctx["raw"], rel(a), rel(al))
            _log_usage({"date": _now(), **ctx["base"], "cache": True, "chars_billed": 0})
            log(f"cache : voix déjà générée ({rel(a)}) — 0 caractère consommé.")
            return {"status": "cache", "chars": 0, "asset": rel(a), "alignment": rel(al)}
        stale += [(p, s, (lock["assets"].get(rel(p)) or {}).get("added")) for p, s in st.items() if s in ("absent", "modifié")]

    # 2) Réponse déjà payée mais pas finalisée (échec ou interruption après l'appel) : reprise sans appel.
    pending = _pending_path(key)
    if pending.is_file():
        if dry_run:
            log(f"réponse déjà reçue ({pending.name}) : sera finalisée sans nouvel appel — 0 caractère.")
            return {"status": "estimation", "chars": 0, "pending": True}
        doc = json.loads(pending.read_text(encoding="utf-8"))
        if doc.get("contract") == PENDING_CONTRACT and doc.get("request_sha256") == key:
            log(f"reprise de la réponse déjà reçue ({pending.name}) : aucun nouvel appel.")
            # Réponse payée : elle remplace au besoin une voix gelée disparue (dans ce cas, elle n'a pu être
            # demandée qu'avec --regenerate).
            r = _finish(ctx, doc, pairs, stale, regenerate or bool(stale), log)
            _log_usage({"date": _now(), **ctx["base"], "cache": True, "chars_billed": 0, "reprise": True})
            return {**r, "status": "reprise", "chars": 0}

    if stale and not regenerate:
        p, s, added = stale[0]
        msg = (f"cette voix a déjà été générée et gelée{f' le {added[:10]}' if added else ''} ({rel(p)}), mais le fichier "
               f"est {s} : restaurez-le depuis votre sauvegarde (les médias de assets/ai/ ne sont pas versionnés), sans "
               f"aucune consommation ; pour la régénérer quand même (nouvelle consommation de {chars} caractères au plus), "
               "relancez avec --regenerate.")
        if not dry_run:
            raise VoiceError(msg[0].upper() + msg[1:])
        log("attention : " + msg)

    # 3) Garde-fous AVANT tout appel payant.
    limit = config.VOICE_BUDGET_CHARS if budget is None else int(budget)
    key_api = api_key()
    quota = quota_now(key_api)
    log(f"estimation : {chars} caractère(s) envoyé(s) ({params['model_id']}, {ctx['voice_label']}) ; quota "
        f"{quota['tier']} : {quota['remaining']} restant(s) sur {quota['limit']} (remise à zéro le {quota['reset']})"
        + (f", dont {quota['lag']} décompté(s) récemment pas encore reportés au compteur" if quota["lag"] else "")
        + f" ; budget de la commande : {limit}.")
    if dry_run:
        _log_usage({"date": _now(), **ctx["base"], "cache": False, "chars_billed": 0, "dry_run": True,
                    "quota_remaining_before": quota["remaining"]})
        return {"status": "estimation", "chars": chars, "quota": quota}
    if chars > limit:
        raise VoiceError(f"{chars} caractères dépassent le budget de la commande ({limit}) : raccourcissez le texte ou "
                         f"relancez avec --budget {chars} (budget par défaut : MOGRAPH_VOICE_BUDGET).")
    if chars > quota["remaining"]:
        raise VoiceError(f"{chars} caractères demandés, {quota['remaining']} restant(s) sur le quota {quota['tier']} "
                         f"(remise à zéro le {quota['reset']}) : génération refusée, rien n'a été consommé.")
    if chars > config.VOICE_CONFIRM_ABOVE and not yes:
        prompt = (f"Consommer jusqu'à {chars} caractères ElevenLabs sur les {quota['remaining']} restants ? [o/N] ")
        if ask is None and not _interactive():
            raise VoiceError(f"{chars} caractères (> seuil de confirmation {config.VOICE_CONFIRM_ABOVE}) : confirmez en "
                             "relançant avec --yes (aucune confirmation possible sans terminal).")
        try:
            answer = (ask or input)(prompt)
        except (EOFError, KeyboardInterrupt):
            answer = ""  # entrée fermée ou interruption : pas de confirmation, rien n'est consommé
        if str(answer).strip().lower() not in ("o", "oui", "y", "yes"):
            raise VoiceError("Génération annulée : rien n'a été consommé.")

    # 4) Appel unique ; la réponse payée est conservée AVANT toute exploitation, puis journalisée.
    t_call = _dt.datetime.now(_dt.timezone.utc).timestamp()
    entry = {"date": _now(), **ctx["base"], "cache": False, "quota_remaining_before": quota["remaining"]}
    try:
        audio, alignment = _tts(key_api, params)
    except ApiRefused:
        raise                                   # refus explicite du service : rien n'a été produit
    except KeyboardInterrupt:
        # Ctrl+C pendant l'appel : décompte inconnu (pas de nouvelle requête réseau : sortie immédiate).
        _log_usage({**entry, "chars_billed": None, "erreur": "interrompu pendant l'appel"})
        raise
    except VoiceError as exc:
        billed = billed_from_history(key_api, params, t_call)
        _log_usage({**entry, "chars_billed": billed, "erreur": "réponse perdue après l'envoi"})
        raise VoiceError(f"{exc} L'appel a pu être décompté ("
                         + (f"{billed} caractère(s) d'après l'historique" if billed is not None else "décompte inconnu")
                         + ") : vérifiez « python mograph.py voice --quota » avant de relancer.") from None
    billed = billed_from_history(key_api, params, t_call)
    doc = {"contract": PENDING_CONTRACT, "request_sha256": key, "date": _now(), "billed": billed, "tier": quota["tier"],
           "audio_base64": base64.b64encode(audio).decode("ascii"), "alignment": alignment}
    _write_atomic(pending, json.dumps(doc, ensure_ascii=False).encode("utf-8"))
    _log_usage({**entry, "chars_billed": billed})
    r = _finish(ctx, doc, pairs, stale, regenerate, log)
    log(f"généré : {r['asset']} ({r['words']} mots, {r['duration']} s) — "
        + (f"{billed} caractère(s) décompté(s) par ElevenLabs (historique du compte)" if billed is not None
           else f"au plus {chars} caractère(s) (décompte pas encore visible dans l'historique)")
        + f" ; restant avant l'appel : {quota['remaining']}.")
    return {**r, "status": "généré", "chars": billed if billed is not None else chars, "billed": billed, "estimated": chars}


def _finish(ctx: dict, doc: dict, pairs, stale, regenerate: bool, log) -> dict:
    """Réponse reçue -> mots, fichiers gelés, scène. Échec : la réponse reste conservée (aucun nouvel appel)."""
    from pipeline import assets

    params, key = ctx["params"], ctx["key"]
    pending = _pending_path(key)
    try:
        words = words_from_alignment(params["text"], doc["alignment"])
        audio = base64.b64decode(doc["audio_base64"])
    except (VoiceError, KeyError, TypeError, ValueError) as exc:
        raise VoiceError(f"{exc} La réponse payée est conservée ({pending.name}) : une relance la réutilisera sans "
                         "nouvel appel.") from None
    audio_path, align_path = _target(pairs, stale, regenerate)
    out = {"contract": ALIGN_CONTRACT, "text": params["text"], "request_sha256": key, "model": params["model_id"],
           "voice": ctx["voice_label"], "output_format": params["output_format"],
           "duration": round(max(w["end"] for w in words), 6), "characters_sent": ctx["chars"], "words": words}
    _write_atomic(audio_path, audio)
    _write_atomic(align_path, (json.dumps(out, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
    for p in (audio_path, align_path):
        _freeze(p, ctx, tier=doc.get("tier"), billed=doc.get("billed"), replace=regenerate)
    _write_scene(ctx["scene_path"], ctx["raw"], assets.lock_key(audio_path), assets.lock_key(align_path))
    pending.unlink(missing_ok=True)
    return {"asset": assets.lock_key(audio_path), "alignment": assets.lock_key(align_path), "words": len(words),
            "duration": out["duration"]}


def _target(pairs, stale, regenerate: bool) -> tuple[Path, Path]:
    """Chemins à écrire : ceux de la voix gelée disparue en cas de --regenerate (les scènes qui les citent
    restent valables), sinon le nom par empreinte."""
    if regenerate and stale:
        from pipeline import assets

        bad = {assets.lock_key(p) for p, _, _ in stale}
        for a, al in pairs:
            if assets.lock_key(a) in bad or assets.lock_key(al) in bad:
                return a, al
    return pairs[0]


def _freeze(path: Path, ctx: dict, tier: str | None, billed: int | None = None, replace: bool = False) -> None:
    from pipeline import assets

    params, chars, key = ctx["params"], ctx["chars"], ctx["key"]
    cost = (f"{billed} caractères ElevenLabs décomptés ({chars} envoyés)" if billed is not None
            else f"au plus {chars} caractères ElevenLabs (décompte non lu)")
    assets.add(path, tool="ElevenLabs", model=params["model_id"], prompt=params["text"],
               license=f"Conditions d'utilisation ElevenLabs{f' (offre {tier})' if tier else ''}",
               seed=params.get("seed"), cost=cost, source="api", replace=replace,
               notes=f"{ctx['voice_label']} ; requête {key[:12]} ; with-timestamps")


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
