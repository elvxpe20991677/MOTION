"""« mograph brief "<idée>" » : une idée -> une scène JSON valide, écrite par Claude (abonnement, claude -p).

Claude remplit un schéma SIMPLIFIÉ adapté aux LLM (sous-ensemble 2D du schéma de scène : plans, textes
et formes, mouvements et couleurs DU preset choisi, énumérés dans le schéma), que ce module convertit
en scène complète (format, livrables, QC, storyboard, déclaration d'usage de l'IA). La scène est
ensuite VALIDÉE par le compilateur ; en cas de refus, les messages du compilateur (écrits pour être
corrigés sans autre explication) sont renvoyés à Claude. AU PLUS config.BRIEF_MAX_ITERATIONS appels.
La sortie versionnée est la scène JSON (pas le prompt). CONTRACT §11.7.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
from pathlib import Path

from pipeline import claude_cli, config

BRIEF_CONTRACT = "mograph-brief/1"
PRESETS_2D = ("flat_riso", "kinetic_signal", "vhs_glitch", "handmade_chalk")
FPS = 25
ID_PATTERN = "^[a-z0-9]+(?:[-_][a-z0-9]+)*$"


class BriefError(RuntimeError):
    """Brief impossible (message en français) ; .attempt = dernière scène refusée, s'il y en a une."""

    def __init__(self, message: str, attempt: dict | None = None, errors: list[str] | None = None):
        super().__init__(message)
        self.attempt, self.errors = attempt, errors or []


def _preset(preset_id: str) -> dict:
    if preset_id not in PRESETS_2D:
        raise BriefError(f"preset « {preset_id} » non pris en charge par brief (2D seulement) : {', '.join(PRESETS_2D)}.")
    return json.loads((config.PRESETS_DIR / f"{preset_id}.json").read_text(encoding="utf-8"))


def brief_schema(preset_id: str) -> dict:
    """Schéma simplifié (« mograph-brief/1 ») pour le preset donné : ses mouvements et ses couleurs."""
    p = _preset(preset_id)
    moves = sorted((p.get("motion") or {}).get("moves") or {})
    colors = sorted(p.get("palette") or {})
    pos = {"type": "string", "description": "« safe:N% » (zone sûre, conseillé pour le texte) ou « N% » du cadre"}
    layer = {"type": "object", "additionalProperties": False, "required": ["id", "type", "x", "y", "move", "at"],
             "properties": {
                 "id": {"type": "string", "pattern": ID_PATTERN},
                 "type": {"enum": ["text", "shape"]},
                 "text": {"type": "string", "description": "texte (type text) : 7 mots au plus"},
                 "style": {"enum": ["display", "body"]},
                 "size": {"type": "number", "description": "taille en px (1080p) : titre ≥ 90, corps ≥ 36"},
                 "color": {"enum": colors},
                 "shape": {"enum": ["rect", "ellipse", "line"]},
                 "w": {"type": "number"}, "h": {"type": "number"},
                 "x": pos, "y": pos,
                 "anchor": {"enum": ["center", "left", "right", "top", "bottom"]},
                 "move": {"enum": moves},
                 "at": {"type": "number", "description": "début de l'animation, en secondes LOCALES au plan"}}}
    shot = {"type": "object", "additionalProperties": False, "required": ["id", "intent", "duration", "layers"],
            "properties": {
                "id": {"type": "string", "pattern": ID_PATTERN},
                "intent": {"type": "string", "description": "une seule intention par plan"},
                "duration": {"type": "number", "description": f"secondes, multiple de {1 / FPS:g} (×{FPS} entier)"},
                "background": {"enum": colors},
                "transition": {"enum": ["cut", "crossfade", "wipe", "push"]},
                "layers": {"type": "array", "minItems": 1, "maxItems": 6, "items": layer}}}
    return {"type": "object", "additionalProperties": False, "required": ["id", "title", "brief", "shots"],
            "properties": {"id": {"type": "string", "pattern": ID_PATTERN, "maxLength": 48},
                           "title": {"type": "string"}, "brief": {"type": "string"},
                           "shots": {"type": "array", "minItems": 1, "maxItems": 6, "items": shot}}}


def system_prompt(preset_id: str, aspect: str) -> str:
    p = _preset(preset_id)
    return (
        "Tu es directeur artistique motion design. Tu remplis le schéma « mograph-brief/1 » d'une scène "
        f"pour le moteur mograph, preset « {preset_id} » (couleurs et mouvements imposés par le schéma), format "
        f"{aspect}, 1080p, {FPS} images/s. Règles STRICTES : durée de chaque plan multiple de {1 / FPS:g} s ; "
        "« at » en secondes LOCALES au plan, l'animation doit finir avant la fin du plan ; textes positionnés "
        "en « safe:N% » avec au moins 2 % de marge au bord ; titres ≥ 90 px, corps ≥ 36 px ; 7 mots au plus "
        "à l'écran à la fois ; ids en minuscules (lettres, chiffres, « - » ou « _ »), uniques ; une intention "
        "par plan ; 2 à 4 plans de 2 à 5 s. Les formes « line » se dessinent avec le mouvement « draw » si le "
        f"preset l'a. Ambiance du preset : {p.get('description') or p.get('name') or preset_id}. "
        "Réponds uniquement par la sortie structurée.")


def _frames(d: float) -> float:
    return max(1, round(float(d) * FPS)) / FPS


def to_scene(b: dict, *, preset_id: str, aspect: str, model: str, date: str | None = None) -> dict:
    """Brief structuré -> scène complète du schéma mograph (déterministe : même brief -> même scène)."""
    date = date or _dt.date.today().isoformat()
    seed = int(hashlib.sha256(b["id"].encode("utf-8")).hexdigest()[:8], 16) % 1_000_000
    shots = []
    for i, s in enumerate(b["shots"]):
        layers = []
        for la in s["layers"]:
            out = {"id": la["id"], "type": la["type"], "x": la["x"], "y": la["y"]}
            if la.get("anchor"):
                out["anchor"] = la["anchor"]
            if la["type"] == "text":
                out.update({"text": la.get("text") or la["id"], "style": la.get("style", "display"),
                            "size": la.get("size", 96), "color": la.get("color", "fg")})
            else:
                shape = la.get("shape", "rect")
                out.update({"shape": shape, "w": la.get("w", 300), "h": la.get("h", 12 if shape == "line" else 300)})
                if shape == "line":
                    out.update({"stroke": la.get("color", "primary"), "strokeWidth": 10})
                else:
                    out["fill"] = la.get("color", "primary")
            out["anim"] = [{"move": la["move"], "at": float(la["at"])}]
            layers.append(out)
        shot = {"id": s["id"], "duration": _frames(s["duration"]), "layers": layers}
        if s.get("background"):
            shot["background"] = s["background"]
        if i and s.get("transition") and s["transition"] != "cut":
            shot["transition_in"] = {"type": s["transition"], "dur": 0.4, "direction": "left"}
        shots.append(shot)
    return {
        "$schema": "../schema/scene.schema.json", "version": "1.0", "id": b["id"], "title": b["title"], "brief": b["brief"], "seed": seed, "preset": preset_id,
        "format": {"aspect": aspect, "resolution": "1080p", "fps": FPS, "alpha": False},
        "storyboard": [{"shot": s["id"], "intent": s["intent"]} for s in b["shots"]],
        "provenance": {"ai_disclosure": {"ai_used": True, "roles": ["scene", "text"], "date": date,
                                         "tools": [{"name": "Claude Code (claude -p)", "model": model,
                                                    "role": "scène générée par mograph brief"}]}},
        "outputs": [{"profile": "hevc_main10"}],
        "qc": {"safe_zone": "title", "safe_zone_tolerance_s": 0.5, "allow_black_s": 0, "determinism_frames": 4,
               "loudness_target_lufs": -14},
        "shots": shots,
    }


def generate(idea: str, *, preset_id: str = "flat_riso", aspect: str = "16:9", scene_id: str | None = None,
             max_iterations: int | None = None, model: str | None = None, log=print, runner=None) -> tuple[dict, dict]:
    """Idée -> scène VALIDE. Boucle bornée : appel Claude, conversion, validation par le compilateur,
    correction à partir des messages de refus. Renvoie (scène, bilan {iterations, model, history}).
    Lève BriefError après max_iterations essais refusés (bilan et dernière scène dans l'exception)."""
    from pipeline import scene as scene_mod

    if not idea.strip():
        raise BriefError("Idée vide : décrivez la vidéo voulue (sujet, ton, message, durée).")
    n_max = int(max_iterations or config.BRIEF_MAX_ITERATIONS)
    if not 1 <= n_max <= config.BRIEF_MAX_ITERATIONS:
        raise BriefError(f"Nombre d'essais {n_max} hors bornes (1 à {config.BRIEF_MAX_ITERATIONS}).")
    schema = brief_schema(preset_id)
    system = system_prompt(preset_id, aspect)
    runner = runner or claude_cli.run
    prompt = f"Idée de la vidéo : {idea.strip()}"
    if scene_id:
        prompt += f"\nIdentifiant imposé de la scène : {scene_id}"
    history, last, errors = [], None, []
    for it in range(1, n_max + 1):
        # Chaque appel compte : une CLI en échec arrête la boucle (ClaudeError), sans nouvel essai.
        obj, meta = runner(prompt, schema, system=system, model=model)
        scene = None
        try:
            if scene_id:
                obj["id"] = scene_id
            scene = to_scene(obj, preset_id=preset_id, aspect=aspect, model=meta.get("model", "inconnu"))
            scene_mod.validate_scene(scene)
        except scene_mod.SceneError as exc:
            errors = list(exc.errors)
        except (KeyError, TypeError, ValueError, AttributeError) as exc:
            # Sortie hors du schéma simplifié (la CLI le valide déjà : cas d'une CLI ancienne ou d'un faux).
            errors = [f"sortie non conforme au schéma mograph-brief/1 ({type(exc).__name__} : {exc}) : respecte "
                      "exactement le schéma."]
        else:
            history.append({"iteration": it, "errors": 0, "model": meta.get("model")})
            log(f"[brief] essai {it}/{n_max} : scène VALIDE ({len(scene['shots'])} plan(s)).")
            return scene, {"iterations": it, "model": meta.get("model"), "history": history}
        history.append({"iteration": it, "errors": len(errors), "model": meta.get("model")})
        log(f"[brief] essai {it}/{n_max} : {len(errors)} erreur(s) du compilateur"
            + (" ; correction demandée." if it < n_max else "."))
        last = scene if scene is not None else obj
        if it < n_max:
            prompt = ("Ta proposition (schéma mograph-brief/1) a été REFUSÉE par le compilateur. Corrige-la et "
                      "renvoie-la complète.\nProposition :\n" + json.dumps(obj, ensure_ascii=False)
                      + "\nMessages du compilateur (chemins shots[i].layers[j] = mêmes plans et calques) :\n"
                      + "\n".join(f"- {e}" for e in errors[:25]) + f"\nIdée d'origine : {idea.strip()}")
    raise BriefError(f"Scène toujours refusée après {n_max} essai(s) : {len(errors)} erreur(s).", attempt=last,
                     errors=errors)
