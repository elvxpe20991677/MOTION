"""Rendu par lot et déclinaisons (python mograph.py batch …).

- Plusieurs scènes : chemins, ids, ou motifs (« scenes/promo_*.json », développés ici : PowerShell ne
  développe pas les jokers pour les programmes externes).
- Déclinaisons : UNE scène modèle + un CSV. Chaque « {{colonne}} » de la scène est remplacé par la
  valeur de la ligne ; une valeur seule dans sa chaîne (« "{{valeur}}" ») devient un nombre si elle en
  est un (données de graphique). La colonne « id » (facultative) nomme la vidéo, sinon <id>_001, …
  Les scènes générées sont écrites dans build/_variants/<id modèle>/ pour relecture.
"""

from __future__ import annotations

import csv
import glob
import json
import re
import time
import unicodedata
from pathlib import Path

from pipeline import config

_PLACEHOLDER = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_ -]*?)\s*\}\}")


class BatchError(ValueError):
    """Lot impossible (scène ou CSV introuvable, colonne manquante)."""


def _resolve(arg: str) -> list[Path]:
    """Chemin, id de scenes/, ou motif glob -> liste de fichiers .json."""
    p = Path(arg)
    if any(ch in arg for ch in "*?["):
        hits = sorted(Path(h) for h in glob.glob(arg if p.is_absolute() else str(config.ROOT / arg)))
        if not hits:
            hits = sorted(Path(h) for h in glob.glob(arg))
        if not hits:
            raise BatchError(f"aucune scène ne correspond au motif « {arg} ».")
        return [h.resolve() for h in hits]
    if p.suffix.lower() == ".json" and p.exists():
        return [p.resolve()]
    cand = config.SCENES_DIR / (arg if arg.endswith(".json") else f"{arg}.json")
    if cand.exists():
        return [cand.resolve()]
    raise BatchError(f"scène introuvable : « {arg} » (chemin .json, id de scenes/ ou motif).")


def slug(text: str) -> str:
    """Texte libre -> id valide (minuscules ASCII, chiffres, « - », « _ » jamais doublé)."""
    t = unicodedata.normalize("NFKD", str(text)).encode("ascii", "ignore").decode("ascii").lower()
    t = re.sub(r"[^a-z0-9_-]+", "_", t)
    t = re.sub(r"_+", "_", t).strip("_-")
    return t[:48] or "x"


def _convert(value: str):
    v = value.strip()
    if re.fullmatch(r"-?\d+", v):
        return int(v)
    if re.fullmatch(r"-?\d+[.,]\d+", v):
        return float(v.replace(",", "."))
    return value


def substitute(obj, row: dict, used: set[str]):
    """Remplace récursivement les {{colonne}} ; `used` reçoit les colonnes rencontrées."""
    if isinstance(obj, dict):
        return {k: substitute(v, row, used) for k, v in obj.items()}
    if isinstance(obj, list):
        return [substitute(v, row, used) for v in obj]
    if isinstance(obj, str):
        full = _PLACEHOLDER.fullmatch(obj)
        if full:
            key = full.group(1)
            used.add(key)
            if key not in row:
                raise KeyError(key)
            return _convert(row[key])

        def rep(m):
            used.add(m.group(1))
            if m.group(1) not in row:
                raise KeyError(m.group(1))
            return str(row[m.group(1)])
        return _PLACEHOLDER.sub(rep, obj)
    return obj


def read_rows(csv_path: Path) -> list[dict]:
    """CSV UTF-8 (BOM d'Excel accepté), séparateur « , », « ; » ou tabulation détecté."""
    if not csv_path.is_file():
        raise BatchError(f"fichier de données introuvable : {csv_path}")
    text = csv_path.read_text(encoding="utf-8-sig")
    try:
        dialect = csv.Sniffer().sniff(text.split("\n", 1)[0], delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    rows = [{(k or "").strip(): (v or "") for k, v in r.items()} for r in csv.DictReader(text.splitlines(), dialect=dialect)]
    rows = [r for r in rows if any(v.strip() for v in r.values())]
    if not rows:
        raise BatchError(f"{csv_path.name} : aucune ligne de données (1re ligne = noms de colonnes).")
    return rows


def expand(scenes: list[str], *, data: str | None = None) -> list[tuple[str, object]]:
    """[(libellé, scène)] où scène est un chemin (Path) ou une scène générée (dict)."""
    paths: list[Path] = []
    for arg in scenes:
        paths.extend(_resolve(arg))
    if not data:
        return [(p.stem, str(p)) for p in paths]
    if len(paths) != 1:
        raise BatchError("--data exige UNE seule scène modèle (qui contient les {{colonnes}}).")
    base = json.loads(paths[0].read_text(encoding="utf-8"))
    if not _PLACEHOLDER.search(json.dumps(base, ensure_ascii=False)):
        raise BatchError(f"la scène modèle {paths[0].name} ne contient aucun {{{{colonne}}}} : rien à décliner.")
    rows = read_rows(Path(data) if Path(data).is_absolute() else (config.ROOT / data if (config.ROOT / data).exists() else Path(data)))
    out_dir = config.BUILD_DIR / "_variants" / str(base.get("id", paths[0].stem))
    out_dir.mkdir(parents=True, exist_ok=True)
    jobs, seen = [], set()
    for n, row in enumerate(rows, start=1):
        used: set[str] = set()
        try:
            scene = substitute({k: v for k, v in base.items() if k != "id"}, row, used)
        except KeyError as exc:
            raise BatchError(f"ligne {n + 1} du CSV : colonne « {exc.args[0]} » absente ; colonnes : "
                             f"{', '.join(row)}.") from None
        sid = f"{base['id']}_{slug(row['id'])}" if row.get("id", "").strip() else f"{base['id']}_{n:03d}"
        sid = sid[:64]
        if sid in seen:
            raise BatchError(f"ligne {n + 1} du CSV : id « {sid} » en double (colonne id).")
        seen.add(sid)
        scene = {"id": sid, **scene}
        (out_dir / f"{sid}.json").write_text(json.dumps(scene, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        jobs.append((sid, scene))
    return jobs


def write_report(results: list[tuple[str, int, float]], *, draft: bool) -> Path:
    """build/_batch/lot-<date>.json : scène, code, durée, verdict QC et livrables."""
    rows = []
    for label, code, dt in results:
        sid = f"{label}_draft" if draft and not label.endswith("_draft") else label
        out = config.scene_out_dir(sid)
        verdict = None
        qc_json = out / "qc_report.json"
        if qc_json.is_file():
            try:
                verdict = json.loads(qc_json.read_text(encoding="utf-8")).get("verdict")
            except (OSError, json.JSONDecodeError):
                verdict = None
        files = sorted(str(p) for p in out.glob("*") if p.suffix in (".mp4", ".mov")) if out.is_dir() else []
        rows.append({"scene": sid, "exit_code": code, "seconds": round(dt, 1), "qc": verdict, "files": files})
    d = config.BUILD_DIR / "_batch"
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"lot-{time.strftime('%Y%m%d-%H%M%S')}.json"
    path.write_text(json.dumps({"draft": draft, "results": rows}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path
