"""Assets IA gelés : verrou assets.lock.json (empreinte + provenance), ajout, vérification, contrôles.

Principe (feuille de route, CONTRACT §11.4) : toute sortie d'IA (clip, image, musique, voix, mesh,
matte, texte) est générée UNE fois, rangée sous config.AI_ASSETS_DIR, identifiée par son SHA-256 et
sa provenance (outil, modèle, date, prompt, graine, licence, coût) dans config.ASSETS_LOCK. Le
rendu n'appelle jamais une IA : il ÉCHOUE si un asset IA utilisé n'est pas verrouillé ou si son
empreinte diffère. Le contrôle est fait à la compilation, avant l'extraction vidéo et le rendu web
de chaque plan, avant l'encodage et par le QC.
"""

from __future__ import annotations

import contextlib
import datetime as _dt
import functools
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import time
from pathlib import Path
from typing import Iterable

from jsonschema import Draft202012Validator

from pipeline import config

LOCK_CONTRACT = "mograph-assets-lock/1"
SOURCES = ("manual_import", "api", "local")
KINDS = (*config.AI_ASSET_KINDS, "other")
# Attente maximale du verrou inter-processus d'écriture (deux « assets add » simultanés).
LOCK_WAIT_S = 30.0
_DATE_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
_SEED_RE = re.compile(r"-?[0-9]+")


class AssetError(RuntimeError):
    """Erreur destinée à l'utilisateur (message en français, actionnable)."""


# ---------------------------------------------------------------------------
# Chemins canoniques
# ---------------------------------------------------------------------------

def _strip_prefix(s: str) -> str:
    """Retire les préfixes Win32 « étendus » (\\\\?\\, \\\\?\\UNC\\, \\\\.\\) : sans cela, un même fichier
    aurait deux formes et échapperait à la comparaison avec le dossier IA."""
    if s.startswith("\\\\?\\UNC\\"):
        return "\\\\" + s[8:]
    if s.startswith(("\\\\?\\", "\\\\.\\")):
        return s[4:]
    return s


def _abs(path: Path | str) -> Path:
    """Chemin absolu LEXICAL normalisé (sans suivre les liens), relatif à la racine du projet si besoin."""
    p = Path(_strip_prefix(str(path)))
    p = p if p.is_absolute() else config.ROOT / p
    return Path(os.path.normpath(_strip_prefix(str(p))))


def _real(path: Path | str) -> Path:
    """Chemin RÉSOLU (liens et jonctions suivis), sans préfixe étendu."""
    return Path(_strip_prefix(os.path.realpath(str(_abs(path)))))


def _under(child: Path, parent: Path) -> bool:
    # normcase : sous Windows, « Assets\\AI » et « assets\\ai » sont le même dossier.
    c, p = os.path.normcase(str(child)), os.path.normcase(str(parent)).rstrip("\\/")
    return c == p or c.startswith(p + os.sep)


def _ai_root() -> Path:
    root = _real(config.AI_ASSETS_DIR)
    if not _under(root, _real(config.ASSETS_DIR)):
        raise AssetError(f"Le dossier des assets IA ({root}) doit être sous {config.ASSETS_DIR} (seul dossier servi au "
                         "navigateur) : corrigez MOGRAPH_AI_ASSETS_DIR.")
    return root


def ai_location(path: Path | str) -> str:
    """« in » : fichier IA (sous le dossier IA, une fois résolu) ; « link_out » : chemin écrit sous le dossier
    IA mais qui en sort par un lien ou une jonction ; « out » : fichier ordinaire."""
    root = _ai_root()
    if _under(_real(path), root):
        return "in"
    return "link_out" if _under(_abs(path), root) or _under(_abs(path), _abs(config.AI_ASSETS_DIR)) else "out"


def is_ai_path(path: Path | str) -> bool:
    """Vrai si le fichier relève du verrou (sous le dossier IA, ou y prétend par un lien)."""
    return ai_location(path) != "out"


def _rel(path: Path | str) -> str:
    p = _real(path)
    try:
        return p.relative_to(_real(config.ROOT)).as_posix()
    except ValueError:
        return p.as_posix()


def lock_key(path: Path | str) -> str:
    """Clé du verrou : chemin RÉSOLU relatif à la racine, séparateurs « / » (identique sous Windows et Linux)."""
    loc = ai_location(path)
    if loc == "out":
        raise AssetError(f"{_abs(path)} n'est pas sous {_rel(config.AI_ASSETS_DIR)} : seuls les assets IA y sont verrouillés.")
    if loc == "link_out":
        raise AssetError(f"{_abs(path)} est un lien ou une jonction qui sort de {_rel(config.AI_ASSETS_DIR)} : un asset IA "
                         "doit être un vrai fichier de ce dossier (copiez-le, puis gelez-le).")
    return _rel(path)


def _cmd_path(key: str) -> str:
    """Chemin cité pour un copier-coller dans le terminal (espaces, accents)."""
    return shlex.quote(key) if os.name != "nt" else (f'"{key}"' if any(c in key for c in ' &()^%!"') else key)


def file_sha256(path: Path | str) -> str:
    """SHA-256 des octets du fichier (lecture par blocs : les clips vidéo peuvent peser des centaines de Mo)."""
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def kind_for(path: Path | str) -> str:
    ext = Path(path).suffix.lower()
    for kind, exts in config.AI_ASSET_KINDS.items():
        if ext in exts:
            return kind
    return "other"


# ---------------------------------------------------------------------------
# Verrou (fichier)
# ---------------------------------------------------------------------------

@functools.lru_cache(maxsize=1)
def _validator() -> Draft202012Validator:
    schema = json.loads((config.SCHEMA_DIR / "assets_lock.schema.json").read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


def _explain(err) -> str:
    """Erreur de schéma du verrou en français (emplacement assets[« clé »].champ)."""
    parts = list(err.absolute_path)
    where = "(racine)"
    if parts[:1] == ["assets"] and len(parts) >= 2:
        where = f"assets[« {parts[1]} »]" + "".join(f".{p}" for p in parts[2:])
    elif parts:
        where = ".".join(map(str, parts))
    v, val = err.validator, err.validator_value
    if v == "required":
        missing = [r for r in val if isinstance(err.instance, dict) and r not in err.instance]
        msg = f"champ obligatoire « {', '.join(missing) or val} » manquant"
    elif v == "additionalProperties":
        extra = sorted(set(err.instance) - set((err.schema.get("properties") or {}))) if isinstance(err.instance, dict) else []
        msg = f"champ inconnu « {', '.join(extra)} »"
    elif v == "const":
        msg = f"valeur attendue « {val} »"
    elif v == "enum":
        msg = f"valeur « {err.instance} » invalide ; valeurs possibles : {', '.join(map(str, val))}"
    elif v == "pattern":
        msg = f"« {err.instance} » n'a pas le format attendu"
    elif v == "propertyNames":
        msg = "clé de chemin invalide (relatif à la racine, « / », sans « .. »)"
    elif v == "type":
        msg = f"type attendu {val}, reçu {type(err.instance).__name__}"
    elif v in ("minLength", "minimum"):
        msg = "valeur vide ou trop petite"
    else:
        msg = err.message
    return f"{where} : {msg}"


def empty_lock() -> dict:
    return {"contract": LOCK_CONTRACT, "assets": {}}


def _lock_path(lock_path: Path | str | None) -> Path:
    return Path(lock_path) if lock_path else config.ASSETS_LOCK


def load_lock(lock_path: Path | str | None = None) -> dict:
    """Verrou validé contre schema/assets_lock.schema.json ; fichier absent = verrou vide."""
    path = _lock_path(lock_path)
    if not path.is_file():
        return empty_lock()
    try:
        # utf-8-sig : un verrou réenregistré par le Bloc-notes ou PowerShell 5.1 (BOM) reste lisible.
        text = path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError) as exc:
        raise AssetError(f"Verrou illisible {path} ({exc}) : enregistrez-le en UTF-8.") from exc
    try:
        lock = json.loads(text)
    except json.JSONDecodeError as exc:
        raise AssetError(f"Verrou {path} : JSON invalide ligne {exc.lineno}, colonne {exc.colno} ({exc.msg}) ; "
                         f"corrigez-le, ou restaurez la version commitée (git checkout -- {path.name}).") from exc
    errors = sorted(_validator().iter_errors(lock), key=lambda e: list(map(str, e.absolute_path)))
    if errors:
        raise AssetError(f"Verrou {path} non conforme à schema/assets_lock.schema.json : "
                         + " ; ".join(list(dict.fromkeys(_explain(e) for e in errors))[:5]))
    return lock


def save_lock(lock: dict, lock_path: Path | str | None = None) -> Path:
    """Écrit le verrou (validé, clés triées, LF) de façon atomique."""
    errors = sorted(_validator().iter_errors(lock), key=lambda e: list(map(str, e.absolute_path)))
    if errors:
        raise AssetError("Verrou invalide, non écrit : " + " ; ".join(list(dict.fromkeys(_explain(e) for e in errors))[:5]))
    path = _lock_path(lock_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Nom temporaire propre au processus : deux écritures concurrentes ne se marchent pas dessus.
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    # Clés triées : un ajout ne déplace pas les autres entrées, les diffs git restent lisibles.
    tmp.write_text(json.dumps(lock, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n")
    os.replace(tmp, path)
    return path


@contextlib.contextmanager
def _exclusive(lock_path: Path | str | None):
    """Verrou inter-processus (fichier créé en exclusivité) autour de lecture → modification → écriture."""
    guard = _lock_path(lock_path).with_name(_lock_path(lock_path).name + ".lck")
    guard.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + LOCK_WAIT_S
    while True:
        try:
            fd = os.open(guard, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            break
        except FileExistsError:
            if time.monotonic() > deadline:
                raise AssetError(f"Verrou occupé depuis plus de {LOCK_WAIT_S:g} s ({guard}) : une autre commande « assets » "
                                 "est en cours ; si ce n'est pas le cas, supprimez ce fichier puis relancez.")
            time.sleep(0.05)
    try:
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
        yield
    finally:
        with contextlib.suppress(OSError):
            guard.unlink()


# ---------------------------------------------------------------------------
# Ajout (gel d'une sortie d'IA)
# ---------------------------------------------------------------------------

def _probe(path: Path) -> dict:
    """Caractéristiques informatives d'une vidéo ou d'un son (ffprobe) ; {} si illisible."""
    try:
        r = subprocess.run([config.FFPROBE, "-v", "error", "-show_entries",
                            "stream=codec_type,codec_name,width,height,r_frame_rate,pix_fmt:format=duration",
                            "-of", "json", str(path)], capture_output=True, text=True, encoding="utf-8",
                           errors="replace", stdin=subprocess.DEVNULL, timeout=120)
        data = json.loads(r.stdout or "{}") if r.returncode == 0 else {}
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
        return {}
    out: dict = {}
    streams = data.get("streams") or []
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    first = video or (streams[0] if streams else None)
    if first:
        out["codec"] = str(first.get("codec_name") or "")
    if video:
        out.update({"width": int(video.get("width") or 0), "height": int(video.get("height") or 0),
                    "fps": str(video.get("r_frame_rate") or ""), "pix_fmt": str(video.get("pix_fmt") or "")})
        # Plan alpha : formats « yuva », « rgba », « argb », « gbrap », « ya »…
        out["alpha"] = any(tag in out["pix_fmt"] for tag in ("yuva", "rgba", "argb", "bgra", "abgr", "gbrap", "ya"))
    try:
        dur = float((data.get("format") or {}).get("duration") or 0)
        if dur > 0:
            out["duration_s"] = round(dur, 6)
    except ValueError:
        pass
    return {k: v for k, v in out.items() if v not in ("", None)}


def _parse_seed(seed):
    if seed is None or seed == "":
        return None
    if isinstance(seed, int):
        return seed
    s = str(seed).strip()
    return int(s) if _SEED_RE.fullmatch(s) else s


def add(src: Path | str, *, tool: str, model: str, prompt: str, license: str, date: str | None = None,
        seed=None, cost: str | None = None, notes: str | None = None, tool_version: str | None = None,
        source: str | None = None, kind: str | None = None, dest: str | None = None, replace: bool = False,
        lock_path: Path | str | None = None) -> tuple[str, dict, str, list[str]]:
    """Gèle une sortie d'IA : copie sous config.AI_ASSETS_DIR (si besoin), empreinte, provenance.

    Renvoie (clé, entrée, statut, avertissements) ; statut ∈ {« ajouté », « inchangé », « remplacé »,
    « provenance mise à jour »}. Tout est validé AVANT de toucher au disque ; un asset déjà gelé avec
    une AUTRE empreinte n'est jamais écrasé sans replace=True (et il est restauré si l'écriture du
    verrou échoue). Re-geler le même fichier complète la provenance : seules les options données
    remplacent les valeurs existantes.
    """
    warnings: list[str] = []
    srcp = _abs(src if Path(str(src)).is_absolute() else Path.cwd() / src)
    if not srcp.is_file():
        raise AssetError(f"Fichier introuvable : {srcp}.")
    for name, value in (("--tool", tool), ("--model", model), ("--prompt", prompt), ("--license", license)):
        if not str(value or "").strip():
            raise AssetError(f"{name} est obligatoire : la provenance d'un asset IA doit être complète.")
    if source is not None and source not in SOURCES:
        raise AssetError(f"--source « {source} » invalide : {', '.join(SOURCES)}.")
    if kind is not None and kind not in KINDS:
        raise AssetError(f"--kind « {kind} » invalide : {', '.join(KINDS)}.")
    if date is not None:
        try:
            if not _DATE_RE.fullmatch(date):
                raise ValueError
            _dt.date.fromisoformat(date)
        except ValueError as exc:
            raise AssetError(f"--date « {date} » invalide : format AAAA-MM-JJ, date réelle (ex. 2026-09-30).") from exc
    root = _ai_root()
    loc = ai_location(srcp)
    if loc == "link_out":
        lock_key(srcp)  # lève l'erreur explicite « lien ou jonction »
    if loc == "in":
        if dest:
            raise AssetError(f"{_rel(srcp)} est déjà sous {_rel(root)} : --dest ne déplace pas un asset ; déplacez le "
                             "fichier vous-même puis relancez sans --dest.")
        # Fichier gelé en place (atteint éventuellement par une jonction) : source = cible résolue.
        target = srcp = _real(srcp)
    else:
        sub = Path(dest) if dest else Path(kind or kind_for(srcp)) / srcp.name
        if dest is not None and (not str(dest).strip() or sub.is_absolute() or ".." in sub.parts
                                 or str(dest).endswith(("/", "\\")) or not sub.name):
            raise AssetError(f"--dest « {dest} » doit être un chemin de FICHIER relatif sous {_rel(root)} (sans « .. », "
                             "sans séparateur final), ex. video/foret.mp4.")
        target = Path(os.path.normpath(root / sub))
        if target.is_dir():
            raise AssetError(f"--dest « {dest} » désigne un dossier ({target}) : donnez aussi le nom du fichier.")
    key = _rel(target)
    digest = file_sha256(srcp)
    file_kind = kind or kind_for(srcp)
    media = _probe(srcp) if file_kind in ("video", "audio") else {}
    if file_kind in ("video", "audio") and not media:
        warnings.append(f"ffprobe ne lit pas {srcp.name} : vérifiez que le fichier est complet (téléchargement "
                        "interrompu ?) avant de l'utiliser dans une scène.")
    now = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    with _exclusive(lock_path):
        lock = load_lock(lock_path)
        old = lock["assets"].get(key)
        if target != srcp and target.exists() and file_sha256(target) != digest and not replace:
            raise AssetError(f"{key} existe déjà avec un autre contenu : choisissez un autre nom (--dest) ou remplacez-le "
                             "en connaissance de cause (--replace).")
        if old is not None and old["sha256"] != digest and not replace:
            raise AssetError(f"{key} est déjà gelé avec une autre empreinte ({old['sha256'][:12]}…) : un asset gelé ne "
                             "change pas ; gelez la nouvelle version sous un autre nom, ou utilisez --replace (les "
                             "scènes qui l'utilisent seront re-rendues).")
        same = old is not None and old["sha256"] == digest
        base = dict(old["provenance"]) if same else {}
        given = {"tool": tool.strip(), "model": model.strip(), "prompt": prompt.strip(), "license": license.strip(),
                 "date": date, "seed": _parse_seed(seed) if seed is not None else None, "cost": cost, "notes": notes,
                 "tool_version": tool_version, "source": source}
        provenance = {**base, **{k: v for k, v in given.items() if v is not None}}
        provenance.setdefault("date", _dt.datetime.now(_dt.timezone.utc).date().isoformat())
        provenance.setdefault("source", "manual_import")
        for k in ("seed", "cost", "notes"):
            provenance.setdefault(k, None)
        entry = {"sha256": digest, "bytes": srcp.stat().st_size, "kind": file_kind, "added": old["added"] if same else now,
                 "provenance": provenance}
        if media:
            entry["media"] = media
        if same and old.get("replaces"):
            entry["replaces"] = old["replaces"]
        elif old is not None and not same:
            entry["replaces"] = old["sha256"]
        new_lock = {**lock, "assets": {**lock["assets"], key: entry}}
        errors = list(_validator().iter_errors(new_lock))
        if errors:
            raise AssetError("Entrée refusée, rien n'a été écrit : "
                             + " ; ".join(list(dict.fromkeys(_explain(e) for e in errors))[:5]))
        if same:
            changed = sorted(k for k in set(entry) | set(old) if k != "added" and entry.get(k) != old.get(k))
            status = "inchangé" if not changed else "provenance mise à jour"
            if changed and changed != ["provenance"]:
                status = "mis à jour (" + ", ".join(changed) + ")"
        else:
            status = "remplacé" if old is not None else "ajouté"
        # Écriture : copie vers un temporaire vérifié, puis bascule ; l'ancien fichier est gardé jusqu'à ce
        # que le verrou soit écrit (restauré sinon : les médias de assets/ai/ ne sont pas versionnés).
        backup = None
        if target != srcp:
            target.parent.mkdir(parents=True, exist_ok=True)
            tmp = target.with_name(f"{target.name}.{os.getpid()}.tmp")
            shutil.copy2(srcp, tmp)
            if file_sha256(tmp) != digest:
                tmp.unlink(missing_ok=True)
                raise AssetError(f"Copie de {srcp} altérée (empreinte différente) : recommencez.")
            if target.exists():
                backup = target.with_name(f"{target.name}.{os.getpid()}.bak")
                os.replace(target, backup)
            os.replace(tmp, target)
        try:
            save_lock(new_lock, lock_path)
        except Exception:
            if backup is not None:
                os.replace(backup, target)
            elif target != srcp:
                target.unlink(missing_ok=True)
            raise
        if backup is not None:
            backup.unlink(missing_ok=True)
    return key, entry, status, warnings


# ---------------------------------------------------------------------------
# Vérification
# ---------------------------------------------------------------------------

def _problem(key: str, entry: dict | None, lock_name: str) -> tuple[str, str]:
    """(statut, message) : « ok », « non verrouillé », « absent », « modifié »."""
    path = config.ROOT / key
    if entry is None:
        return "non verrouillé", (f"asset IA « {key} » absent du verrou {lock_name} : gelez-le une fois (python mograph.py "
                                  f"assets add {_cmd_path(key)} --tool … --model … --prompt … --license …) ; le rendu "
                                  "n'appelle jamais une IA.")
    if not path.is_file():
        return "absent", (f"asset IA « {key} » introuvable sur le disque (gelé le {entry['added'][:10]}, SHA-256 "
                          f"{entry['sha256'][:12]}…) : les médias de assets/ai/ ne sont pas versionnés, replacez le "
                          "fichier d'origine.")
    got = file_sha256(path)
    if got != entry["sha256"]:
        return "modifié", (f"asset IA « {key} » modifié depuis son gel (SHA-256 attendu {entry['sha256'][:12]}…, trouvé "
                           f"{got[:12]}…) : restaurez le fichier gelé, ou gelez la nouvelle version en connaissance de "
                           "cause (python mograph.py assets add … --replace).")
    return "ok", ""


def problems_for(paths: Iterable[Path | str], lock: dict | None = None,
                 lock_path: Path | str | None = None) -> list[str]:
    """Problèmes des assets IA parmi `paths` (les autres sont ignorés) : liens sortants, non verrouillés,
    absents, modifiés."""
    lock = lock if lock is not None else load_lock(lock_path)
    name = _lock_path(lock_path).name
    out = []
    for p in paths:
        loc = ai_location(p)
        if loc == "out":
            continue
        try:
            key = lock_key(p)
        except AssetError as exc:
            out.append(str(exc))
            continue
        status, msg = _problem(key, lock["assets"].get(key), name)
        if status != "ok":
            out.append(msg)
    return out


def status_of(path: Path | str, lock: dict | None = None, lock_path: Path | str | None = None) -> str:
    """Statut d'un fichier IA : « ok », « non verrouillé », « absent », « modifié » (AssetError si hors du
    dossier IA ou lien sortant)."""
    lock = lock if lock is not None else load_lock(lock_path)
    key = lock_key(path)
    return _problem(key, lock["assets"].get(key), _lock_path(lock_path).name)[0]


def ai_paths_of(compiled: dict) -> list[str]:
    """Assets IA relevés par le compilateur (compiled["assets"], CONTRACT §11.2)."""
    return [a["path"] for a in (compiled.get("assets") or []) if a.get("ai")]


def check_compiled(compiled: dict, *, stage: str) -> None:
    """Lève AssetError si un asset IA de la scène compilée n'est pas utilisable tel que gelé.

    Appelé juste avant chaque étape qui lit un asset (rendu web d'un plan, encodage) : un fichier
    modifié entre la compilation et le rendu (rendu détaché long) est ainsi refusé.
    """
    problems = problems_for(ai_paths_of(compiled))
    if problems:
        raise AssetError(f"{stage} refusé : " + " ; ".join(problems))


def verify(lock_path: Path | str | None = None) -> list[dict]:
    """État de chaque entrée du verrou + fichiers et liens du dossier IA non verrouillés."""
    lock = load_lock(lock_path)
    name = _lock_path(lock_path).name
    rows = []
    for key, entry in sorted(lock["assets"].items()):
        try:
            canonical = lock_key(config.ROOT / key)
        except AssetError as exc:
            rows.append({"path": key, "status": "clé invalide", "detail": str(exc), "entry": entry})
            continue
        if canonical != key:
            rows.append({"path": key, "status": "clé invalide", "entry": entry,
                         "detail": f"clé non normalisée (attendu « {canonical} ») : corrigez-la dans {name}."})
            continue
        status, msg = _problem(key, entry, name)
        rows.append({"path": key, "status": status, "detail": msg, "entry": entry})
    root = config.AI_ASSETS_DIR
    if root.is_dir():
        for f in sorted(root.rglob("*")):
            if f.is_dir() and not f.is_symlink() and ai_location(f) == "in":
                continue
            if f.name.startswith(".") or f.name == "README.md" or f.suffix in (".tmp", ".bak", ".lck"):
                continue
            if ai_location(f) == "link_out":
                rows.append({"path": _abs(f).relative_to(_abs(config.ROOT)).as_posix(), "status": "lien hors du dossier IA",
                             "entry": None, "detail": f"{f} sort du dossier IA par un lien ou une jonction : supprimez-le."})
                continue
            if not f.is_file():
                continue
            key = lock_key(f)
            if key not in lock["assets"]:
                rows.append({"path": key, "status": "non verrouillé", "entry": None, "detail": _problem(key, None, name)[1]})
    return rows


def entries_for(paths: Iterable[str], lock_path: Path | str | None = None) -> dict[str, dict | None]:
    """Entrées du verrou des chemins donnés (None si absent) : provenance pour le rapport QC."""
    lock = load_lock(lock_path)
    return {p: lock["assets"].get(p) for p in paths}
