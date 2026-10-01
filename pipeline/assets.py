"""Assets IA gelés : verrou assets.lock.json (empreinte + provenance), ajout, vérification, contrôles.

Principe (feuille de route, CONTRACT §11.4) : toute sortie d'IA (clip, image, musique, voix, mesh,
matte, texte) est générée UNE fois, rangée sous config.AI_ASSETS_DIR, identifiée par son SHA-256 et
sa provenance (outil, modèle, date, prompt, graine, licence, coût) dans config.ASSETS_LOCK. Le
rendu n'appelle jamais une IA : il ÉCHOUE si un asset IA utilisé n'est pas verrouillé ou si son
empreinte diffère. Le contrôle est fait à la compilation, avant l'extraction vidéo et le rendu web
de chaque plan, avant l'encodage et par le QC.
"""

from __future__ import annotations

import datetime as _dt
import functools
import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Iterable

from jsonschema import Draft202012Validator

from pipeline import config

LOCK_CONTRACT = "mograph-assets-lock/1"
SOURCES = ("manual_import", "api", "local")


class AssetError(RuntimeError):
    """Erreur destinée à l'utilisateur (message en français, actionnable)."""


# ---------------------------------------------------------------------------
# Chemins, empreintes, nature
# ---------------------------------------------------------------------------

def file_sha256(path: Path | str) -> str:
    """SHA-256 des octets du fichier (lecture par blocs : les clips vidéo peuvent peser des centaines de Mo)."""
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def is_ai_path(path: Path | str) -> bool:
    """Vrai si le fichier est sous config.AI_ASSETS_DIR (donc soumis au verrou)."""
    p = Path(path)
    p = (p if p.is_absolute() else config.ROOT / p).resolve()
    return p.is_relative_to(config.AI_ASSETS_DIR.resolve())


def lock_key(path: Path | str) -> str:
    """Clé du verrou : chemin relatif à la racine, séparateurs « / » (identique sous Windows et Linux)."""
    p = Path(path)
    p = (p if p.is_absolute() else config.ROOT / p).resolve()
    if not p.is_relative_to(config.AI_ASSETS_DIR.resolve()):
        raise AssetError(f"{p} n'est pas sous {_rel(config.AI_ASSETS_DIR)} : seuls les assets IA y sont verrouillés.")
    return p.relative_to(config.ROOT).as_posix()


def _rel(path: Path) -> str:
    try:
        return Path(path).resolve().relative_to(config.ROOT).as_posix()
    except ValueError:
        return str(path)


def kind_for(path: Path | str) -> str:
    ext = Path(path).suffix.lower()
    for kind, exts in config.AI_ASSET_KINDS.items():
        if ext in exts:
            return kind
    return "other"


# ---------------------------------------------------------------------------
# Verrou
# ---------------------------------------------------------------------------

@functools.lru_cache(maxsize=1)
def _validator() -> Draft202012Validator:
    schema = json.loads((config.SCHEMA_DIR / "assets_lock.schema.json").read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


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
        lock = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AssetError(f"Verrou illisible {path} ({exc}) : restaurez-le depuis git (git checkout -- {path.name}).") from exc
    errors = sorted(_validator().iter_errors(lock), key=lambda e: list(e.absolute_path))
    if errors:
        detail = " ; ".join(f"{'/'.join(map(str, e.absolute_path)) or '(racine)'} : {e.message}" for e in errors[:5])
        raise AssetError(f"Verrou {path} non conforme à schema/assets_lock.schema.json : {detail}")
    return lock


def save_lock(lock: dict, lock_path: Path | str | None = None) -> Path:
    """Écrit le verrou (validé, clés triées, LF) de façon atomique."""
    errors = list(_validator().iter_errors(lock))
    if errors:
        raise AssetError(f"Verrou invalide, non écrit : {errors[0].message}")
    path = _lock_path(lock_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    # Clés triées : un ajout ne déplace pas les autres entrées, les diffs git restent lisibles.
    tmp.write_text(json.dumps(lock, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n")
    os.replace(tmp, path)
    return path


# ---------------------------------------------------------------------------
# Ajout (gel d'une sortie d'IA)
# ---------------------------------------------------------------------------

def _probe(path: Path) -> dict:
    """Caractéristiques informatives d'une vidéo ou d'un son (ffprobe) ; {} si indisponible."""
    try:
        r = subprocess.run([config.FFPROBE, "-v", "error", "-show_entries",
                            "stream=codec_type,codec_name,width,height,r_frame_rate,pix_fmt:format=duration",
                            "-of", "json", str(path)], capture_output=True, text=True, timeout=120)
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
    s = str(seed).strip()
    return int(s) if s.lstrip("-").isdigit() else s


def add(src: Path | str, *, tool: str, model: str, prompt: str, license: str, date: str | None = None,
        seed=None, cost: str | None = None, notes: str | None = None, tool_version: str | None = None,
        source: str = "manual_import", kind: str | None = None, dest: str | None = None, replace: bool = False,
        lock_path: Path | str | None = None) -> tuple[str, dict, str]:
    """Gèle une sortie d'IA : copie sous config.AI_ASSETS_DIR (si besoin), empreinte, provenance.

    Renvoie (clé, entrée, statut) avec statut ∈ {« ajouté », « inchangé », « remplacé »}. Un fichier
    déjà verrouillé avec une AUTRE empreinte n'est jamais écrasé sans replace=True.
    """
    srcp = Path(src)
    srcp = (srcp if srcp.is_absolute() else Path.cwd() / srcp).resolve()
    if not srcp.is_file():
        raise AssetError(f"Fichier introuvable : {srcp}.")
    for name, value in (("--tool", tool), ("--model", model), ("--prompt", prompt), ("--license", license)):
        if not str(value or "").strip():
            raise AssetError(f"{name} est obligatoire : la provenance d'un asset IA doit être complète.")
    if source not in SOURCES:
        raise AssetError(f"--source « {source} » invalide : {', '.join(SOURCES)}.")
    gen_date = date or _dt.datetime.now(_dt.timezone.utc).date().isoformat()
    try:
        _dt.date.fromisoformat(gen_date)
    except ValueError as exc:
        raise AssetError(f"--date « {gen_date} » invalide : format AAAA-MM-JJ.") from exc
    kind = kind or kind_for(srcp)
    ai_root = config.AI_ASSETS_DIR.resolve()
    if srcp.is_relative_to(ai_root):
        target = srcp
    else:
        sub = Path(dest) if dest else Path(kind) / srcp.name
        if sub.is_absolute() or ".." in sub.parts:
            raise AssetError(f"--dest « {dest} » doit être un chemin relatif sous {_rel(ai_root)} (sans « .. »).")
        target = (ai_root / sub).resolve()
    key = lock_key(target)
    digest = file_sha256(srcp)
    lock = load_lock(lock_path)
    old = lock["assets"].get(key)
    if target != srcp and target.exists() and file_sha256(target) != digest and not replace:
        raise AssetError(f"{key} existe déjà avec un autre contenu : choisissez un autre nom (--dest) ou remplacez-le "
                         "en connaissance de cause (--replace).")
    if old is not None and old["sha256"] != digest and not replace:
        raise AssetError(f"{key} est déjà gelé avec une autre empreinte ({old['sha256'][:12]}…) : un asset gelé ne change "
                         "pas ; enregistrez la nouvelle version sous un autre nom, ou utilisez --replace (les scènes qui "
                         "l'utilisent seront re-rendues).")
    if target != srcp:
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(target.name + ".tmp")
        shutil.copy2(srcp, tmp)
        os.replace(tmp, target)
        if file_sha256(target) != digest:
            raise AssetError(f"Copie de {srcp} vers {target} altérée (empreinte différente) : recommencez.")
    provenance = {"tool": tool.strip(), "model": model.strip(), "date": gen_date, "prompt": prompt.strip(),
                  "license": license.strip(), "seed": _parse_seed(seed), "cost": cost, "source": source,
                  "notes": notes}
    if tool_version:
        provenance["tool_version"] = tool_version
    entry = {"sha256": digest, "bytes": target.stat().st_size, "kind": kind,
             "added": _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "provenance": provenance}
    media = _probe(target) if kind in ("video", "audio") else {}
    if media:
        entry["media"] = media
    if old is not None and old["sha256"] == digest:
        # Même fichier déjà gelé : on garde la date d'ajout d'origine, la provenance peut être complétée.
        entry["added"] = old["added"]
        status = "inchangé" if old.get("provenance") == provenance else "provenance mise à jour"
    elif old is not None:
        entry["replaces"] = old["sha256"]
        status = "remplacé"
    else:
        status = "ajouté"
    lock["assets"][key] = entry
    save_lock(lock, lock_path)
    return key, entry, status


# ---------------------------------------------------------------------------
# Vérification
# ---------------------------------------------------------------------------

def _problem(key: str, entry: dict | None, lock_name: str) -> str | None:
    """Message actionnable si l'asset IA « key » n'est pas utilisable tel que gelé, sinon None."""
    path = config.ROOT / key
    if entry is None:
        return (f"asset IA « {key} » absent du verrou {lock_name} : gelez-le une fois (python mograph.py assets add "
                f"{key} --tool … --model … --prompt … --license …) ; le rendu n'appelle jamais une IA.")
    if not path.is_file():
        return (f"asset IA « {key} » introuvable sur le disque (gelé le {entry['added'][:10]}, SHA-256 "
                f"{entry['sha256'][:12]}…) : les médias de assets/ai/ ne sont pas versionnés, replacez le fichier "
                "d'origine.")
    got = file_sha256(path)
    if got != entry["sha256"]:
        return (f"asset IA « {key} » modifié depuis son gel (SHA-256 attendu {entry['sha256'][:12]}…, trouvé "
                f"{got[:12]}…) : restaurez le fichier gelé, ou gelez la nouvelle version en connaissance de cause "
                "(python mograph.py assets add … --replace).")
    return None


def problems_for(paths: Iterable[Path | str], lock: dict | None = None,
                 lock_path: Path | str | None = None) -> list[str]:
    """Problèmes des assets IA parmi `paths` (les autres sont ignorés) : non verrouillés, absents, modifiés."""
    lock = lock if lock is not None else load_lock(lock_path)
    name = _lock_path(lock_path).name
    out = []
    for p in paths:
        if not is_ai_path(p):
            continue
        key = lock_key(p)
        msg = _problem(key, lock["assets"].get(key), name)
        if msg:
            out.append(msg)
    return out


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
    """État de chaque entrée du verrou + fichiers de AI_ASSETS_DIR non verrouillés."""
    lock = load_lock(lock_path)
    name = _lock_path(lock_path).name
    rows = []
    for key, entry in sorted(lock["assets"].items()):
        msg = _problem(key, entry, name)
        if msg is None:
            status = "ok"
        elif "introuvable" in msg:
            status = "absent"
        else:
            status = "modifié"
        rows.append({"path": key, "status": status, "detail": msg or "", "entry": entry})
    root = config.AI_ASSETS_DIR
    if root.is_dir():
        for f in sorted(root.rglob("*")):
            if not f.is_file() or f.name.startswith(".") or f.name == "README.md" or f.suffix == ".tmp":
                continue
            key = lock_key(f)
            if key not in lock["assets"]:
                rows.append({"path": key, "status": "non verrouillé", "entry": None,
                             "detail": _problem(key, None, name)})
    return rows


def entries_for(paths: Iterable[str], lock_path: Path | str | None = None) -> dict[str, dict | None]:
    """Entrées du verrou des chemins donnés (None si absent) : provenance pour le rapport QC."""
    lock = load_lock(lock_path)
    return {p: lock["assets"].get(p) for p in paths}
