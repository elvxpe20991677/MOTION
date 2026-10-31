"""QC visuelle par vision sur les planches contact (phase 6) : AVERTISSEMENTS seulement.

Claude (claude -p, abonnement, outil « Read » seul) regarde chaque planche contact
(out/<scène>/planche_contact.png, et planche_alpha.png d'une scène alpha) et signale les défauts
visibles : texte coupé ou illisible, chevauchements, contraste, cadrage, images vides, incohérences.
Jamais bloquant : le verdict du QC automatique (qc_report) et la porte de déterminisme n'en dépendent
pas ; un échec de l'appel donne un avertissement, pas une erreur. Résultat : out/<scène>/vision_qc.json
et vision_qc.md. CONTRACT §11.7.
"""

from __future__ import annotations

import datetime as _dt
import json
from pathlib import Path

from pipeline import claude_cli, config

VISION_CONTRACT = "mograph-vision-qc/1"

SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["summary", "issues"],
    "properties": {
        "summary": {"type": "string", "description": "une phrase : impression d'ensemble"},
        "issues": {"type": "array", "maxItems": 20, "items": {
            "type": "object", "additionalProperties": False, "required": ["severity", "where", "problem", "suggestion"],
            "properties": {
                "severity": {"enum": ["info", "attention"]},
                "where": {"type": "string", "description": "vignette (n° d'image / timecode écrit dessus) ou zone"},
                "problem": {"type": "string"},
                "suggestion": {"type": "string", "description": "correction concrète dans la scène JSON"}}}}},
}

SYSTEM = ("Tu es superviseur qualité en motion design. Tu examines des planches contact (vignettes d'une vidéo, "
          "chaque vignette porte son numéro d'image et son timecode). Signale UNIQUEMENT des défauts visibles : "
          "texte coupé, illisible ou trop petit, contraste insuffisant, éléments qui se chevauchent ou sortent du "
          "cadre, vignette vide ou noire inattendue, incohérence de style. Pas d'avis de goût. Réponds en français, "
          "uniquement par la sortie structurée ; liste vide si rien à signaler.")


def review(compiled: dict, sheets: list[Path], *, log=print, runner=None) -> dict:
    """Relecture visuelle des planches ; écrit vision_qc.json / .md ; ne lève jamais d'erreur bloquante."""
    runner = runner or claude_cli.run
    out_dir = config.scene_out_dir(compiled["scene_id"])
    report = {"contract": VISION_CONTRACT, "scene_id": compiled["scene_id"],
              "date": _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "blocking": False,
              "sheets": [], "warnings": []}
    if config.CLAUDE_ENV_ERRORS:
        report["warnings"] += [f"relecture visuelle non lancée : {e}" for e in config.CLAUDE_ENV_ERRORS]
        sheets = []
    for sheet in sheets:
        sheet = Path(sheet).resolve()
        prompt = (f"Lis l'image {sheet.as_posix()} avec l'outil Read (planche contact de la scène "
                  f"« {compiled['scene_id']} », {compiled['format']['aspect']}, {compiled['frames']} images) et "
                  "rapporte les défauts visibles.")
        try:
            obj, meta = runner(prompt, SCHEMA, system=SYSTEM, tools=["Read"], add_dirs=[sheet.parent])
            issues = [{k: str(i.get(k, "")) for k in ("severity", "where", "problem", "suggestion")}
                      for i in obj["issues"]]
            entry = {"sheet": sheet.name, "model": meta.get("model"), "summary": str(obj["summary"]), "issues": issues}
            n = sum(1 for i in issues if i["severity"] == "attention")
            report["sheets"].append(entry)
            log(f"[vision] {sheet.name} : {len(issues)} remarque(s) dont {n} à vérifier — {entry['summary']}")
        except (claude_cli.ClaudeError, KeyError, TypeError, AttributeError) as exc:
            # Jamais bloquant : CLI absente, non connectée, refusée par le garde-fou ou réponse hors schéma.
            report["warnings"].append(f"{sheet.name} : relecture visuelle indisponible ({exc})")
            log(f"[vision] AVERTISSEMENT {sheet.name} : relecture visuelle indisponible ({exc})")
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "vision_qc.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    lines = [f"# Relecture visuelle — {compiled['scene_id']}", "",
             "Avertissements indicatifs (Claude, vision) : jamais bloquants, hors QC automatique et hors porte de "
             "déterminisme.", ""]
    for s in report["sheets"]:
        lines += [f"## {s['sheet']} ({s.get('model')})", "", s["summary"], ""]
        lines += [f"- **{i['severity']}** — {i['where']} : {i['problem']} → {i['suggestion']}" for i in s["issues"]] \
            or ["- rien à signaler"]
        lines.append("")
    lines += [f"- {w}" for w in report["warnings"]]
    (out_dir / "vision_qc.md").write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    return report
