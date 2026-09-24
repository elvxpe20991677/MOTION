"""Vérification de l'étape 11 : compilateur de scène (pipeline/scene.py).

Contrôles principaux (SPEC étape 11) :
  (1) une scène valide riche compile ; summary() affiche le nombre d'images et les fondus ;
  (2) une durée de 1,01 s à 25 i/s est REFUSÉE avec explication et valeurs proches ;
  (3) un ease « none » est REFUSÉ (et accepté avec allowLinear, émis « none ») ;
  (4) une référence beat: sans bpm est REFUSÉE.
Contrôles complémentaires : sync:end (valeurs numériques), fenêtres de glitch on_beats, total
d'images = somme − fondus, interpolations Blender posées sur la clé de départ (back, période
elastic en images, customEase -> BACK EASE_IN_OUT 1.2), continuité from/to complète, conformité
aux schémas internes, refus actionnables (alpha, glyphes, police, grow en dépassement, cibles et
propriétés, plaque, glitch, chevauchements...), déterminisme de la compilation, write_compiled.

Les messages d'erreur RÉELS sont affichés sous chaque refus (lignes « » »).
Lancement depuis la racine : .venv/Scripts/python.exe tests/verify_step11_compiler.py
Fichiers temporaires : build/_tests/11/ uniquement.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TEST_DIR = ROOT / "build" / "_tests" / "11"
# build/ et out/ redirigés AVANT l'import de la config : write_compiled et les chemins de plaques
# ne touchent ainsi jamais aux vrais dossiers du projet pendant le test.
os.environ["MOGRAPH_BUILD_DIR"] = str(TEST_DIR / "build")
os.environ["MOGRAPH_OUT_DIR"] = str(TEST_DIR / "out")
sys.path.insert(0, str(ROOT))

import jsonschema  # noqa: E402
import referencing  # noqa: E402
from referencing.jsonschema import DRAFT202012  # noqa: E402

from pipeline import config  # noqa: E402
from pipeline import scene  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures"
EPS = 1e-9
failures = 0


def check(ok: bool, label: str, detail: str = "") -> bool:
    global failures
    if ok:
        print(f"PASS {label}")
    else:
        failures += 1
        print(f"FAIL {label}" + (f" — {detail}" if detail else ""))
    return ok


def close(a, b, eps: float = EPS) -> bool:
    return abs(float(a) - float(b)) <= eps


def fixture(name: str) -> dict:
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


def show_errors(exc: scene.SceneError, limit: int = 6) -> None:
    for msg in exc.errors[:limit]:
        print(f"      » {msg}")
    if len(exc.errors) > limit:
        print(f"      » … ({len(exc.errors) - limit} autre(s))")


def expect_refused(source, label: str, must_contain: list[str]) -> scene.SceneError | None:
    """La compilation doit échouer avec une SceneError dont le texte contient chaque fragment."""
    try:
        scene.compile_scene(source)
    except scene.SceneError as exc:
        text = str(exc)
        missing = [m for m in must_contain if m not in text]
        check(not missing, f"REFUS {label}", f"fragments absents du message : {missing}")
        show_errors(exc)
        return exc
    check(False, f"REFUS {label}", "la scène a été ACCEPTÉE")
    return None


def mutated(base: dict, fn) -> dict:
    raw = copy.deepcopy(base)
    fn(raw)
    return raw


def layer_by_id(spec: dict, lid: str) -> dict:
    return next(layer for layer in spec["layers"] if layer["id"] == lid)


def build_validators():
    """Validateurs indépendants du compilateur pour les deux contrats internes."""
    docs = [json.loads(p.read_text(encoding="utf-8")) for p in (
        config.SCHEMA_DIR / "scene.schema.json", config.SCHEMA_DIR / "preset.schema.json",
        config.SCHEMA_DIR / "internal" / "compiled_shot.schema.json",
        config.SCHEMA_DIR / "internal" / "blender_job.schema.json")]
    reg = referencing.Registry().with_resources(
        [(d["$id"], referencing.Resource.from_contents(d, default_specification=DRAFT202012)) for d in docs])
    return (jsonschema.Draft202012Validator(docs[2], registry=reg),
            jsonschema.Draft202012Validator(docs[3], registry=reg))


def continuity_problems(spec: dict) -> list[str]:
    """Chaque tween : mêmes clés from/to ; chaque chaîne (calque, cible, prop) : from = to précédent,
    aucun chevauchement ; initial = from de la première tween ; tweens triées par start."""
    probs = []
    for layer in spec["layers"]:
        starts = [t["start"] for t in layer["tweens"]]
        if starts != sorted(starts):
            probs.append(f"{layer['id']} : tweens non triées par start")
        chains: dict[tuple[str, str], list[dict]] = {}
        for t in layer["tweens"]:
            if set(t["from"]) != set(t["to"]):
                probs.append(f"{layer['id']} : from/to de clés différentes {sorted(t['from'])} / {sorted(t['to'])}")
            for p in t["from"]:
                chains.setdefault((t["target"], p), []).append(t)
        for (tgt, p), chain in chains.items():
            if layer["initial"].get(tgt, {}).get(p, "ABSENT") != chain[0]["from"][p]:
                probs.append(f"{layer['id']}/{tgt}/{p} : initial ≠ from de la première tween")
            for a, b in zip(chain, chain[1:]):
                if b["from"][p] != a["to"][p]:
                    probs.append(f"{layer['id']}/{tgt}/{p} : from {b['from'][p]} ≠ to précédent {a['to'][p]}")
                if b["start"] < a["start"] + a["dur"] - EPS:
                    probs.append(f"{layer['id']}/{tgt}/{p} : chevauchement")
        declared = {(t["target"], p) for t in layer["tweens"] for p in t["from"]}
        for tgt, props in layer["initial"].items():
            for p in props:
                if (tgt, p) not in declared:
                    probs.append(f"{layer['id']} : initial.{tgt}.{p} sans tween")
    return probs


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    TEST_DIR.mkdir(parents=True, exist_ok=True)
    shot_validator, job_validator = build_validators()
    rich = fixture("scene_rich_flat")

    # ------------------------------------------------------------------------------------------
    print("== (1) Scène valide riche ==")
    try:
        compiled = scene.compile_scene(FIXTURES / "scene_rich_flat.json")
    except scene.SceneError as exc:
        check(False, "scene_rich_flat compile", str(exc))
        print(f"\nÉCHEC : {failures} contrôle(s).")
        return 1
    check(True, "scene_rich_flat compile")
    print()
    print(scene.summary(compiled))
    print()
    text = scene.summary(compiled)
    check("155 images" in text and "Fondus      : 1" in text and "fondu 10 images" in text,
          "summary() affiche le nombre d'images (155) et le fondu (10 images)")
    check(compiled["contract"] == "mograph-scene/1" and compiled["source"] == str((FIXTURES / "scene_rich_flat.json").resolve()),
          "contrat mograph-scene/1 et source absolue")

    shots = compiled["shots"]
    total = sum(s["frames"] for s in shots) - sum(s["fade_in_frames"] for s in shots)
    check([s["frames"] for s in shots] == [75, 90] and [s["fade_in_frames"] for s in shots] == [0, 10],
          "images par plan 75 / 90, fondu 0,4 s = 10 images")
    check([s["global_start"] for s in shots] == [0, 65], "départs globaux 0 et 65 (75 − 10)")
    check(compiled["frames"] == total == 155 and close(compiled["duration"], 6.2),
          f"total = somme − fondus = {total} images (6,2 s)")

    for s in shots:
        errs = list(shot_validator.iter_errors(s["spec"]))
        check(not errs, f"plan {s['id']} conforme à compiled_shot.schema.json", "; ".join(e.message for e in errs[:3]))
        for job in s["plates"]:
            errs = list(job_validator.iter_errors(job))
            check(not errs, f"tâche {s['id']}/{job['plate_id']} conforme à blender_job.schema.json", "; ".join(e.message for e in errs[:3]))

    s0, s1 = shots[0]["spec"], shots[1]["spec"]
    for s in shots:
        expected = int(hashlib.sha256(f"42:{s['id']}".encode()).hexdigest()[:8], 16)
        check(s["spec"]["seed"] == expected, f"graine du plan {s['id']} = sha256('42:{s['id']}')[:8]")
    check(s0["background"] == "#EEF1F6" and s1["background"] == "#EEF1F6", "fond = palette.bg résolu en hexadécimal")
    check(s0["custom_eases"] == {"flatSettle": "M0,0 C0.12,0.9 0.24,1 1,1", "flatAnticipate": "M0,0 C0.35,-0.28 0.2,1.12 1,1"},
          "custom_eases du preset transmis au runtime")
    check(all(f["url"].startswith("http://mograph.render/node_modules/@fontsource/") and f["unicode_range"].startswith("U+")
              for f in s0["fonts"]), "polices : URL servie + unicode_range")

    g0, g1 = s0["textures"]["grain"], s1["textures"]["grain"]
    check(g0 == {"amount": 0.1, "size": 1, "blend": "overlay", "monochrome": True} and g1 is None,
          "fx.grain : nombre -> amount (0,1), false -> null")
    check(s0["textures"]["paper"]["blend"] == "multiply" and s0["textures"]["vignette"] is None, "textures de base du preset conservées")

    titre = layer_by_id(s0, "titre")
    check(close(titre["x"], 96) and close(titre["y"], 345.6) and titre["anchor"] == [0.0, 0.5],
          "position safe:0% / safe:30% -> 96 / 345,6 px, ancre left", f"{titre['x']}, {titre['y']}")
    check(titre["split"] == {"chars": True, "words": False, "lines": False}, "split.chars déduit de la tween chars_up")
    cu = [t for t in titre["tweens"] if t["target"] == "chars"][0]
    check(cu["from"] == {"y": "110%", "opacity": 0} and cu["to"] == {"y": 0, "opacity": 1} and cu["ease"] == "power3.out"
          and close(cu["stagger"], 0.03) and close(cu["start"], 0.2), "chars_up : y 110% -> 0 + opacité, alias enter résolu, stagger 0,03")
    check(titre["font"] == {"family": "Bricolage Grotesque", "weight": 800, "size": 132.0, "line_height": 0.95,
                            "tracking": -0.02, "color": "#1B1F4A"}, "police du calque résolue (display + surcharges)")

    pastille = layer_by_id(s0, "pastille")
    pop = pastille["tweens"][0]
    check(close(pop["start"], 0.5) and close(pop["start"] + pop["dur"], 1.0) and pop["ease"] == "back.out(1.7)"
          and pop["from"] == {"scaleX": 0, "scaleY": 0},
          "sync:end : pop (0,5 s) finit exactement sur beat:2 = 1,0 s -> départ 0,5 s ; scale -> scaleX/scaleY")
    rot = pastille["tweens"][1]
    check(rot["ease"] == "elastic.out(1,0.4)" and rot["from"] == {"rotation": 0.0} and rot["to"] == {"rotation": 90},
          "ease GSAP normalisé (espaces retirés), from déduit de la rotation du calque")

    cadre = layer_by_id(s0, "cadre")
    draw = cadre["tweens"][0]
    check(draw["target"] == "shape" and close(draw["dur"], 1.2) and draw["from"] == {"draw": 0},
          "move draw remis à l'échelle (dur 1,2 s)")
    stroke = cadre["tweens"][1]
    check(stroke["from"] == {"stroke": "#2F5BEA"} and stroke["to"] == {"stroke": "#FF4F9A"},
          "stroke : from = couleur du calque, to = nom de palette résolu")
    sous = layer_by_id(s0, "sous_titre")
    check(sous["text"] == "Édition 2026 — données réelles" and sous["font"]["family"] == "DM Sans"
          and [t for t in sous["tweens"] if "color" in t["from"]][0]["from"] == {"color": "#1B1F4A"},
          "texte body + tween color partant de la couleur du calque")

    impact = layer_by_id(s1, "impact")
    sa = [t for t in impact["tweens"] if "x" in t["from"] or "opacity" in t["from"]][:2]
    check(all(close(t["start"], 0.9) for t in sa) and close(sa[0]["start"] + sa[0]["dur"], 1.6),
          "sync:end avec marker:drop (4,2 s global − 2,6 s) : fin de la 1re étape = 1,6 s, départ 0,9 s",
          str([(t["start"], t["dur"]) for t in sa]))
    check(sa[0]["ease"] == "flatAnticipate", "slide_anticipate : alias anticipate -> customEase flatAnticipate conservé")
    ex = impact["tweens"][-1]
    check(close(ex["start"], 3.0) and ex["from"] == {"y": 0, "opacity": 1} and ex["to"] == {"y": 60, "opacity": 0},
          "exit_down : from déduit par continuité (y 0, opacité 1)")

    accroche = layer_by_id(s1, "accroche")
    check(accroche["split"] == {"chars": False, "words": True, "lines": False} and accroche["tweens"][0]["target"] == "words"
          and close(accroche["tweens"][0]["stagger"], 0.08), "target/stagger de la tween remplacent ceux du move")
    lin = layer_by_id(s1, "defilement")["tweens"][0]
    check(lin["ease"] == "none", "(3) ease « none » ACCEPTÉ avec allowLinear, émis « none »")

    barres = layer_by_id(s1, "barres")
    check(barres["grow"] == {"start": 0.3, "dur": 1.0, "stagger": 0.035, "ease": "power3.out"},
          "chart grow : start, dur long, stagger du preset, ease", str(barres["grow"]))
    check([d["color"] for d in barres["data"]] == ["#2F5BEA", "#FF4F9A", "#FFC93C", "#FF4F9A"]
          and barres["max"] == 41 and close(barres["gap"], 45) and barres["value_format"] == {"decimals": 0, "prefix": "", "suffix": " %"},
          "chart : couleurs (data > colors cyclique), max, gap 20 % du pas, valueFormat")
    check(barres["tweens"][0]["target"] == "bars" and close(barres["tweens"][0]["stagger"], 0.035),
          "tween sur les barres avec stagger par défaut du preset")

    glitch = s1["glitch"]
    want = [[0.4, 0.12], [1.4, 0.15], [1.6, 0.2], [2.4, 0.12], [3.4, 0.12]]
    ok = len(glitch["windows"]) == len(want) and all(close(a[0], b[0]) and close(a[1], b[1]) for a, b in zip(glitch["windows"], want))
    check(ok, "glitch : on_beats 6..14 pas 2 (locaux 0,4 / 1,4 / 2,4 / 3,4 ; beat 14 hors plan) + marker:drop, triées, "
              "fusion [1,4+0,12] ∪ [1,45+0,1] = [1,4 ; 0,15]", str(glitch["windows"]))
    check(glitch["colors"] == ["#2F5BEA", "#FF4F9A"] and glitch["layers"] == ["barres", "impact"] and glitch["mode"] == "both",
          "glitch : couleurs par défaut (primary, secondary), calques, mode")
    check(s0["glitch"] is None, "plan sans glitch -> null")

    problems = [p for s in shots for p in continuity_problems(s["spec"])]
    check(not problems, "continuité from/to complète sur tous les calques (mêmes clés, from = to précédent, initial)",
          "; ".join(problems[:5]))

    # --- plaque 3D -------------------------------------------------------------------------------
    job = shots[1]["plates"][0]
    check(job["width"] == 960 and job["height"] == 540, "plaque : 1920×1080 × dsf 1 × plate_scale 0,5 = 960×540")
    check(s1["plates"]["balle"] == {"url": "http://mograph.render/build/test_rich_flat/plates/chiffres__balle/",
                                    "frames": 90, "width": 960, "height": 540}, "plates du plan : URL servie + dimensions réelles")
    check(Path(job["out_dir"]) == config.plate_dir("test_rich_flat", "chiffres", "balle") and job["frames"] == 90 and job["fps"] == 25,
          "tâche : out_dir absolu, 90 images, 25 i/s")
    check(job["seed"] == int(hashlib.sha256(b"42:chiffres:balle").hexdigest()[:8], 16) & 0x7FFFFFFF, "graine Blender de la plaque")
    check(job["render"]["samples"] == 16 and job["render"]["device"] == "auto" and job["render"]["look"] == "None"
          and job["render"]["bounces"]["total"] == 8, "rendu : quality.blender_samples, défauts du contrat (preset sans blender)")
    balle = next(o for o in job["objects"] if o["id"] == "balle")
    loc = next(c for c in balle["channels"] if c["prop"] == "location")["keys"]
    check([k["frame"] for k in loc] == [0.0, 12.5, 14.5, 22.5], "images FLOTTANTES des clés (0 / 12,5 / 14,5 / 22,5)")
    check([k["value"][2] for k in loc] == [3.9, 0.5, 0.36, 0.5], "z de la sphère : 3,9 -> impact 0,5 -> écrasement 0,36 -> 0,5")
    check(loc[0]["out"] == {"interpolation": "CUBIC", "easing": "EASE_IN"},
          "clé 0 : out = ease de la clé 1 (power2.in -> CUBIC EASE_IN), posé sur la clé de DÉPART")
    check(loc[1]["out"] == {"interpolation": "BACK", "easing": "EASE_OUT", "back": 2.5}, "back.out(2.5) -> BACK EASE_OUT back 2,5")
    check(loc[2]["out"]["interpolation"] == "ELASTIC" and loc[2]["out"]["easing"] == "EASE_OUT" and loc[2]["out"]["amplitude"] == 0
          and close(loc[2]["out"]["period"], 0.35 * (22.5 - 14.5)), "elastic.out(1,0.35) -> amplitude 0, période 0,35 × 8 images = 2,8",
          str(loc[2]["out"]))
    check(loc[3]["out"] is None, "dernière clé : out = null")
    sc = next(c for c in balle["channels"] if c["prop"] == "scale")["keys"]
    check(sc[0]["value"] == [1.0, 1.0, 1.0] and sc[1]["value"] == [1.25, 1.25, 0.72], "scale nombre -> vec3")
    check(sc[0]["out"] == {"interpolation": "BACK", "easing": "EASE_IN_OUT", "back": 1.2}
          and sc[1]["out"] == {"interpolation": "BACK", "easing": "EASE_IN_OUT", "back": 1.2},
          "customEase (direct et via l'alias settle) -> BACK EASE_IN_OUT 1,2")
    rotk = next(c for c in balle["channels"] if c["prop"] == "rotation")["keys"]
    check(close(rotk[1]["frame"], 35.0) and rotk[1]["value"] == [0.0, 0.0, 180.0] and rotk[0]["out"] == {"interpolation": "QUAD", "easing": "EASE_IN_OUT"},
          "clé en beat:8 (4 s global − 2,6 s = 1,4 s = image 35), rotation en degrés, power1 -> QUAD")
    cam = job["camera"]
    check(cam["channels"][0]["keys"][0]["out"] == {"interpolation": "SINE", "easing": "EASE_IN_OUT"} and cam["lens"] == 40
          and cam["fstop"] is None and cam["sensor_width"] == 36, "caméra : sine.inOut -> SINE, lens, fstop null, capteur 36 mm")
    sun = next(light for light in job["lights"] if light["id"] == "sun")
    check(sun["location"] == [0.0, 0.0, 10.0] and sun["shape"] == "DISK" and sun["color"] == "#FFFFFF", "lumière SUN : défauts du contrat")
    sol = next(o for o in job["objects"] if o["id"] == "sol")
    check(sol["material"]["name"] == "sol_mat" and sol["material"]["base_color"] == "#F7F4EC" and sol["material"]["coat_roughness"] == 0.03,
          "matériau en ligne : nom <objet>_mat, couleur de palette résolue, défauts complétés")
    check(job["world"] == {"color": "#EEF1F6", "strength": 1.0, "sky": None} and job["transparent"] is False, "monde = palette.bg, opaque")

    check(compiled["audio"]["src"] == str((config.AUDIO_DIR / "test_rich_flat_beat.wav").resolve())
          and compiled["audio"]["target_lufs"] == -14 and compiled["audio"]["markers"] == [{"id": "drop", "time": 4.2}],
          "audio : piste synth dans assets/audio, cible LUFS, marqueurs")
    check([Path(o["path"]).name for o in compiled["outputs"]] == ["test_rich_flat_hevc_main10.mp4", "test_rich_flat_master_prores_422hq.mov"]
          and compiled["outputs"][0]["crf"] == 18 and compiled["outputs"][1]["crf"] is None,
          "livrables : out/<scène>/<id><suffix>_<profil>, crf HEVC par défaut")
    check(compiled["depth"] == 8 and compiled["motion_blur"]["enabled"] is False, "profondeur 8 bits sans flou de mouvement")

    # --- déterminisme, API utilitaires ----------------------------------------------------------
    again = scene.compile_scene(FIXTURES / "scene_rich_flat.json")
    check(scene.sha256_json(again) == scene.sha256_json(compiled), f"compilation déterministe (sha256 {scene.sha256_json(compiled)[:16]}…)")
    check(scene.canonical_json({"b": 1, "a": "é"}) == '{"a":"é","b":1}'.encode("utf-8"), "canonical_json : clés triées, compact, UTF-8")
    try:
        scene.validate_scene(rich)
        check(True, "validate_scene(dict valide) -> None")
    except scene.SceneError as exc:
        check(False, "validate_scene(dict valide) -> None", str(exc))
    out_path = scene.write_compiled(compiled)
    check(out_path == TEST_DIR / "build" / "test_rich_flat" / "compiled.json" and json.loads(out_path.read_text(encoding="utf-8")) == compiled
          and not out_path.with_name("compiled.json.tmp").exists(), f"write_compiled -> {out_path.relative_to(ROOT)} (atomique, relu identique)")

    # ------------------------------------------------------------------------------------------
    print("== sync:end et temps musicaux (kinetic_signal) ==")
    kin = scene.compile_scene(FIXTURES / "scene_kinetic_sync.json")
    ks = kin["shots"]
    w1 = layer_by_id(ks[0]["spec"], "w1")["tweens"]
    w2 = layer_by_id(ks[0]["spec"], "w2")["tweens"]
    check([t["start"] for t in w1] == [0.55, 0.75, 0.81] and close(w1[0]["start"] + w1[0]["dur"], 0.75),
          "slam sur beat:1 (offset 0,25 + 0,5 = 0,75 s) : départs 0,55 / 0,75 / 0,81, impact à 0,75 s", str([t["start"] for t in w1]))
    check(all(close(a, b) for a, b in zip([t["start"] for t in w2], [0.95, 1.25, 1.34]))
          and all(close(a, b) for a, b in zip([t["dur"] for t in w2], [0.3, 0.09, 0.48])) and close(w2[0]["start"] + w2[0]["dur"], 1.25),
          "slam dur 0,87 (k = 1,5) sur beat:2 : durées 0,3 / 0,09 / 0,48, départs 0,95 / 1,25 / 1,34", str([(t["start"], t["dur"]) for t in w2]))
    check(w1[0]["ease"] == "power4.in" and w1[1]["from"] == {"scaleX": 1, "scaleY": 1} and w1[1]["to"] == {"scaleX": 1.14, "scaleY": 0.82},
          "slam : impact power4.in puis écrasement enchaîné depuis scale 1")
    leg = layer_by_id(ks[1]["spec"], "legende")
    check(leg["text"] == "BIENTÔT" and close(leg["tweens"][0]["start"], 0.5) and leg["split"]["chars"],
          "casse appliquée par le compilateur ; beat:4.5 = 2,5 s global = 0,5 s local du plan 2")
    check(layer_by_id(ks[0]["spec"], "w1")["text"] == "SIGNAL", "display Anton en capitales (case du style)")
    check(kin["frames"] == 90 and ks[1]["fade_in_frames"] == 0 and ks[1]["global_start"] == 60,
          "durée beats:4 = 60 images ; transition par défaut du preset = coupe")
    check(kin["motion_blur"] == {"enabled": True, "samples": 8, "shutter": 0.5} and kin["depth"] == 16,
          "flou de mouvement du preset -> profondeur 16 bits")
    # Contrat §4 : audio = null | {src absolu, …} ; un bloc « tempo seul » ne sert qu'à caler les temps.
    check(kin["audio"] is None and any("MUETS" in w for w in kin["warnings"]),
          "audio sans fichier : tempo seul -> audio null (livrables muets) + avertissement",
          str(kin["audio"]))

    # ------------------------------------------------------------------------------------------
    print("== Plaques et preset 3D (clay_3d) ==")
    clay = scene.compile_scene(FIXTURES / "scene_clay_plate.json")
    cj = clay["shots"][0]["plates"][0]
    check(cj["width"] == 540 and cj["height"] == 540 and cj["render"]["look"] == "Base Contrast" and cj["render"]["samples"] == 128
          and cj["render"]["bounces"] == {"total": 6, "diffuse": 3, "glossy": 2, "transmission": 0, "transparent": 4, "volume": 0},
          "plaque clay : 540×540, look et rebonds du preset")
    check([light["id"] for light in cj["lights"]] == ["key", "fill", "rim"] and all(light["shape"] == "DISK" for light in cj["lights"]),
          "lumières = light_rig du preset (3 disques)")
    mats = {o["id"]: o["material"] for o in cj["objects"]}
    check(mats["balle"]["name"] == "lilac" and mats["balle"]["base_color"] == "#8C7BFF" and mats["balle"]["sheen"] == 0.35
          and mats["balle"]["ior"] == 1.45, "matériau nommé du preset + défauts complétés")
    anneau = next(o for o in cj["objects"] if o["id"] == "anneau")
    check(anneau["scale"] == [0.9, 0.9, 0.9] and close(anneau["minor"], 0.225), "scale nombre -> vec3 ; minor de tore par défaut")
    check(cj["camera"]["lens"] == 50 and cj["camera"]["fstop"] == 5.6 and cj["world"]["color"] == "#E9E4FF" and cj["world"]["sky"] is None,
          "caméra et monde par défaut du preset (50 mm f/5,6, fond = palette.bg)")
    hj = clay["shots"][1]["plates"][0]
    check(hj["world"]["sky"]["sun_elevation"] == 20 and hj["world"]["sky"]["sun_size"] == 0.545 and hj["camera"]["fstop"] is None
          and hj["width"] == 270, "ciel fusionné sur les défauts ; fstop null explicite respecté")
    check(clay["shots"][1]["fade_in_frames"] == 12 and clay["frames"] == 72, "fondu par défaut du preset (0,5 s = 12 images à 24 i/s)")
    probs = [p for s in clay["shots"] for p in continuity_problems(s["spec"])] + \
            [p for s in kin["shots"] for p in continuity_problems(s["spec"])]
    check(not probs, "continuité from/to sur les fixtures kinetic et clay", "; ".join(probs[:5]))
    for comp in (kin, clay):
        for s in comp["shots"]:
            bad = list(shot_validator.iter_errors(s["spec"])) + [e for j in s["plates"] for e in job_validator.iter_errors(j)]
            check(not bad, f"{comp['scene_id']}/{s['id']} conforme aux schémas internes", "; ".join(e.message for e in bad[:3]))

    # ------------------------------------------------------------------------------------------
    print("== (2) (3) (4) Refus de la SPEC ==")
    expect_refused(FIXTURES / "scene_bad_duration.json", "(2) durée 1,01 s à 25 i/s",
                   ["shots[0].duration", "1,01 s à 25 i/s = 25,25 images", "1,00 s", "1,04 s"])
    expect_refused(FIXTURES / "scene_bad_linear.json", "(3) ease « none » sans allowLinear",
                   ["shots[0].layers[0].anim[0].ease", "linéaire", "allowLinear"])
    expect_refused(FIXTURES / "scene_bad_beat_nobpm.json", "(4) beat: sans audio.bpm",
                   ["shots[0].layers[0].anim[0].at", "beat:2", "audio.bpm"])

    print("== Refus complémentaires ==")
    expect_refused(mutated(rich, lambda r: r["format"].update(alpha=True)), "alpha sans prores_4444 (et fond coloré)",
                   ["prores_4444", "format.alpha est actif"])
    expect_refused(mutated(rich, lambda r: r["outputs"].append({"profile": "prores_4444"})), "prores_4444 sans alpha",
                   ["prores_4444 sans format.alpha"])
    expect_refused(mutated(rich, lambda r: r["shots"][0]["layers"][4].update(text="Ω — données")), "caractère hors unicode-range",
                   ["shots[0].layers[4].text", "U+03A9", "DM Sans"])
    expect_refused(mutated(rich, lambda r: r["shots"][0]["layers"][0].update(weight=300)), "police absente (graisse non déclarée)",
                   ["police manquante", "Bricolage Grotesque", "graisses disponibles : 800"])
    expect_refused(mutated(rich, lambda r: r["shots"][1]["layers"][1]["grow"].update(ease="back.out(1.7)")), "chart grow avec back.out",
                   ["shots[1].layers[1].grow.ease", "dépasse 1"])
    expect_refused(mutated(rich, lambda r: r["shots"][1]["layers"][1]["grow"].update(ease="anticipate")),
                   "chart grow avec un customEase qui sort de [0, 1]", ["flatAnticipate", "dépasse 1"])
    ok_settle = scene.compile_scene(mutated(rich, lambda r: r["shots"][1]["layers"][1]["grow"].update(ease="settle")))
    check(layer_by_id(ok_settle["shots"][1]["spec"], "barres")["grow"]["ease"] == "flatSettle",
          "chart grow avec un customEase borné (flatSettle) accepté")
    expect_refused(mutated(rich, lambda r: r["shots"][0]["layers"][0]["anim"].append({"to": {"draw": 0.5}, "at": 2.9, "dur": 0.05})),
                   "draw sur un calque text", ["« draw » ne s'anime que sur un calque shape"])
    expect_refused(mutated(rich, lambda r: r["shots"][0]["layers"][3]["anim"].append({"to": {"fill": "primary"}, "at": 2.0, "dur": 0.3})),
                   "fill sur la cible self d'une forme", ["\"target\": \"shape\""])
    expect_refused(mutated(rich, lambda r: r["shots"][0]["layers"][3]["anim"].append({"to": {"color": "primary"}, "at": 2.0, "dur": 0.3})),
                   "color sur une forme", ["« color » ne s'anime que sur un calque text", "« fill » ou « stroke »"])
    expect_refused(mutated(rich, lambda r: r["shots"][0]["layers"][1]["anim"].append({"move": "chars_up", "at": 2.0})),
                   "target chars sur une forme", ["la cible « chars » n'existe que pour un calque text"])
    expect_refused(mutated(rich, lambda r: r["shots"][0]["layers"][0]["anim"].append({"target": "bars", "to": {"opacity": 0.5}, "at": 1.0})),
                   "target bars sur un texte", ["la cible « bars » n'existe que pour un calque chart"])
    expect_refused(mutated(rich, lambda r: r["shots"][1]["layers"][2]["anim"].append({"target": "shape", "to": {"opacity": 0.5}, "at": 2.0})),
                   "target shape sur un texte", ["la cible « shape » n'existe que pour un calque shape"])
    expect_refused(mutated(rich, lambda r: r["shots"][0]["layers"][1].update(id="titre")), "id de calque en double", ["« titre » en double"])
    expect_refused(mutated(rich, lambda r: r["shots"][1]["layers"][0].update(plate="ballon")), "plaque référencée inexistante",
                   ["plaque « ballon » inexistante", "« balle »"])
    expect_refused(mutated(rich, lambda r: r["shots"][1]["fx"]["glitch"]["layers"].append("fantome")), "glitch.layers inconnu",
                   ["glitch.layers", "« fantome »"])
    expect_refused(mutated(rich, lambda r: r["shots"][0]["layers"][3]["anim"].append({"to": {"rotation": 45}, "at": 1.5, "dur": 0.5})),
                   "chevauchement de deux tweens sur la même propriété", ["chevauchement sur « rotation »", "1,2 s → 1,8 s"])
    expect_refused(mutated(rich, lambda r: r["shots"][0]["layers"][3]["anim"][0].update(at=0.2)), "sync:end qui démarre avant 0",
                   ["démarre à -0,3 s", "sync"])
    expect_refused(mutated(rich, lambda r: r["shots"][0].update(transition_in="crossfade")), "fondu sur le premier plan", ["premier plan"])
    expect_refused(mutated(rich, lambda r: r["shots"][1]["transition_in"].update(dur=0.41)), "fondu non entier",
                   ["0,41 s à 25 i/s = 10,25 images"])
    expect_refused(mutated(rich, lambda r: r["shots"][1]["transition_in"].update(dur=3.2)), "fondu plus long qu'un plan",
                   ["fondu de 80 images plus long"])
    expect_refused(mutated(rich, lambda r: r["shots"][0]["layers"][2].update(stroke="violet")), "couleur de palette inconnue",
                   ["couleur « violet » inconnue", "primary"])
    expect_refused(mutated(rich, lambda r: r["shots"][1]["layers"][2]["anim"][0].update(at="marker:dorp")), "marqueur inconnu",
                   ["marqueur « dorp » inconnu", "« drop »"])
    expect_refused(mutated(rich, lambda r: r["shots"][0]["layers"][0]["anim"][0].update(move="chars_upp")), "mouvement inconnu",
                   ["mouvement « chars_upp » inconnu", "« chars_up »"])
    expect_refused(mutated(rich, lambda r: r["shots"][0]["layers"][3]["anim"][1].update(ease="power2.outt")), "ease inconnu",
                   ["easing « power2.outt » inconnu", "« power2.out »"])
    expect_refused(mutated(rich, lambda r: r["shots"][0]["layers"][4].update(colr="accent")), "propriété inconnue (schéma)",
                   ["shots[0].layers[4] (plan « intro », calque « sous_titre »)", "propriété inconnue « colr »", "« color »"])
    expect_refused(mutated(rich, lambda r: r["shots"][0]["layers"][0]["anim"][0].update(at="beat4")), "référence de temps mal formée (schéma)",
                   ["shots[0].layers[0].anim[0].at", "référence de temps invalide"])
    expect_refused(mutated(rich, lambda r: r["format"].update(fps=29)), "fps hors liste (schéma)", ["format.fps", "valeurs possibles"])
    lin3d = mutated(rich, lambda r: r["shots"][1]["plates"][0]["objects"][1]["anim"][2]["keys"][1].update(ease="linear"))
    expect_refused(lin3d, "clé 3D linéaire sans allowLinear", ["keys[1].ease", "allowLinear"])
    lin3d["shots"][1]["plates"][0]["objects"][1]["anim"][2]["keys"][1]["allowLinear"] = True
    ok3d = scene.compile_scene(lin3d)
    rk = next(c for c in next(o for o in ok3d["shots"][1]["plates"][0]["objects"] if o["id"] == "balle")["channels"] if c["prop"] == "rotation")
    check(rk["keys"][0]["out"] == {"interpolation": "LINEAR", "easing": "AUTO"}, "clé 3D linéaire avec allowLinear -> LINEAR AUTO")
    expect_refused(mutated(rich, lambda r: r["shots"][1]["plates"][0]["objects"][1].update(material="gold")), "matériau nommé inconnu",
                   ["matériau « gold » inconnu"])
    expect_refused(mutated(rich, lambda r: r["shots"][1]["plates"][0]["objects"][1]["material"].update(base_color="#FF000080")),
                   "couleur 3D avec alpha", ["material.base_color", "pas de canal alpha"])
    nobpm = fixture("scene_kinetic_sync")
    del nobpm["audio"]
    expect_refused(nobpm, "beats:N et beat: sans bpm (plusieurs erreurs regroupées)", ["shots[0].duration", "beats:4", "beat:1", "beat:4.5"])
    multi = mutated(rich, lambda r: (r["shots"][0]["layers"][2].update(stroke="violet"),
                                     r["shots"][0]["layers"][0]["anim"][0].update(move="rize"),
                                     r["shots"][0]["layers"][4].update(text="Ω")))
    exc = expect_refused(multi, "erreurs regroupées dans UNE SceneError", ["violet", "rize", "U+03A9"])
    check(exc is not None and len(exc.errors) >= 3 and str(exc).count("• ") == len(exc.errors),
          "SceneError.errors liste chaque erreur ; str(e) = puces jointes par des retours à la ligne")
    expect_refused(mutated(rich, lambda r: r.update(preset_overrides={"motion": {"eases": {"default": "none"}}})),
                   "preset_overrides rendant le preset linéaire", ["après preset_overrides", "motion.eases.default", "linéaire"])
    expect_refused(mutated(rich, lambda r: r.update(preset_overrides={"fonts": [
        {"family": "Bricolage Grotesque", "weight": 800, "file": "@fontsource/bricolage-grotesque/files/bricolage-grotesque-latin-900-normal.woff2"},
        {"family": "DM Sans", "weight": 500, "file": "@fontsource/dm-sans/files/dm-sans-latin-500-normal.woff2"}]})),
                   "police absente sur disque (preset_overrides)", ["fichier de police introuvable", "latin-900"])
    expect_refused(mutated(rich, lambda r: r.update(preset="flat_rizo")), "preset inconnu", ["preset « flat_rizo » introuvable", "flat_riso"])
    try:
        scene.validate_scene(mutated(rich, lambda r: r["shots"][0].update(duration=1.01)))
        check(False, "validate_scene lève SceneError sur une scène invalide")
    except scene.SceneError:
        check(True, "validate_scene lève SceneError sur une scène invalide")

    print("== Règles de rendu ==")
    chalk = {
        "version": "1.0", "id": "test_chalk_mb", "title": "Chalk + flou demandé", "preset": "handmade_chalk",
        "format": {"aspect": "16:9", "resolution": "1080p", "fps": 24},
        "quality": {"motion_blur": {"enabled": True, "samples": 6}},
        "outputs": [{"profile": "prores_422hq"}],
        "shots": [{"id": "tableau", "duration": 1.0, "layers": [
            {"id": "trait", "type": "shape", "shape": "path", "w": 400, "h": 200, "d": "M0,100 C100,0 300,200 400,100",
             "fill": "none", "stroke": "primary", "boil": True, "anim": [{"move": "draw", "at": 0.1}]}]}],
    }
    cc = scene.compile_scene(chalk)
    check(cc["frame_step"] == 2 and cc["motion_blur"]["enabled"] is False and cc["depth"] == 8
          and any("flou de mouvement désactivé" in w for w in cc["warnings"]),
          "frame_step 2 => flou de mouvement désactivé + avertissement", str(cc["warnings"]))
    trait = cc["shots"][0]["spec"]["layers"][0]
    check(trait["boil"] == {"amount": 2.0, "freq": 0.03, "step": 2} and cc["shots"][0]["spec"]["textures"]["paper"]["blend"] == "screen",
          "boil: true -> textures.boil du preset ; papier en mode screen")
    alpha_ok = mutated(rich, lambda r: (r["format"].update(alpha=True), r["outputs"].append({"profile": "prores_4444"}),
                                        r["shots"][0].pop("background")))
    ca = scene.compile_scene(alpha_ok)
    check(all(s["spec"]["background"] is None for s in ca["shots"]) and all(j["transparent"] for s in ca["shots"] for j in s["plates"])
          and [o["alpha_flatten"] for o in ca["outputs"]] == [True, True, False],
          "alpha : fonds transparents, plaques transparentes, livrables opaques aplatis (alpha_flatten)")

    print("== Chaque move de chaque preset se compile ==")
    # « impact » : l'opacité passe par slide_anticipate (0 -> 1) puis exit_down (1 -> 0) ; on casse le
    # from de exit_down pour prouver que le contrôle de continuité n'est pas vacant.
    broken = copy.deepcopy(compiled["shots"][1]["spec"])
    layer_by_id(broken, "impact")["tweens"][-1]["from"]["opacity"] = 0.5
    found = continuity_problems(broken)
    check(any("opacity" in p and "to précédent" in p for p in found),
          "auto-contrôle : une rupture de continuité volontaire est bien détectée", str(found))
    for pid in ("flat_riso", "kinetic_signal", "vhs_glitch", "handmade_chalk", "clay_3d", "photoreal_3d"):
        preset = scene.load_preset(pid)
        layers = []
        for n, (move, steps) in enumerate(preset["motion"]["moves"].items()):
            targets = {s.get("target", "self") for s in steps}
            if "shape" in targets:
                layer = {"id": f"m{n}", "type": "shape", "shape": "ellipse", "w": 120, "h": 120, "fill": "none", "stroke": "primary"}
            else:
                layer = {"id": f"m{n}", "type": "text", "text": "Élan 42", "style": "display"}
            layer.update(x=f"safe:{10 + 8 * n}%", y="safe:50%", anim=[{"move": move, "at": 0.1}])
            layers.append(layer)
        raw = {"version": "1.0", "id": f"test_moves_{pid}", "title": f"Tous les moves de {pid}", "preset": pid,
               "format": {"aspect": "16:9", "resolution": "1080p", "fps": 24}, "outputs": [{"profile": "hevc_main10"}],
               "shots": [{"id": "moves", "duration": 2.0, "layers": layers}]}
        try:
            cm = scene.compile_scene(raw)
        except scene.SceneError as exc:
            check(False, f"{pid} : les {len(layers)} moves se compilent", str(exc))
            continue
        spec = cm["shots"][0]["spec"]
        bad = [e.message for e in shot_validator.iter_errors(spec)] + continuity_problems(spec)
        check(not bad and all(layer["tweens"] for layer in spec["layers"]),
              f"{pid} : les {len(layers)} moves ({', '.join(preset['motion']['moves'])}) se compilent, schéma et continuité OK",
              "; ".join(bad[:3]))
    photo = {"version": "1.0", "id": "test_photoreal_sky", "title": "Photoreal ciel", "preset": "photoreal_3d",
             "format": {"aspect": "16:9", "resolution": "4k", "fps": 24}, "quality": {"plate_scale": 0.25},
             "outputs": [{"profile": "prores_422hq"}],
             "shots": [{"id": "produit", "duration": 1.0,
                        "plates": [{"id": "studio", "world": {"sky": True},
                                    "camera": {"location": [0, -5, 1.2], "target": [0, 0, 0.6]},
                                    "objects": [{"id": "boitier", "primitive": "rounded_cube", "material": "aluminium"},
                                                {"id": "bague", "primitive": "torus", "material": "brass"}]}],
                        "layers": [{"id": "plaque", "type": "sequence", "plate": "studio"},
                                   {"id": "legende", "type": "text", "style": "body", "text": "Série limitée",
                                    "x": "safe:0%", "y": "safe:90%", "anchor": "left", "anim": [{"move": "chars_fade", "at": 0.1}]}]}]}
    cp = scene.compile_scene(photo)
    pj = cp["shots"][0]["plates"][0]
    check(pj["world"]["sky"] == {"sun_elevation": 28.0, "sun_rotation": 135.0, "altitude": 0.0, "air": 1.0, "dust": 0.6,
                                 "ozone": 1.0, "sun_size": 0.545, "sun_intensity": 0.8}
          and pj["render"]["samples"] == 512 and pj["render"]["look"] == "Medium High Contrast"
          and pj["camera"]["fstop"] == 2.8 and pj["camera"]["lens"] == 85,
          "photoreal : sky true -> réglages ciel du preset, 512 éch., Medium High Contrast, 85 mm f/2,8")
    check(pj["width"] == 960 and pj["height"] == 540, "plaque 4K : 1920×1080 × dsf 2 × plate_scale 0,25 = 960×540")
    mats = {o["id"]: o["material"] for o in pj["objects"]}
    check(mats["boitier"]["anisotropic"] == 0.65 and mats["bague"]["base_color"] == "#D4A373"
          and layer_by_id(cp["shots"][0]["spec"], "legende")["text"] == "SÉRIE LIMITÉE",
          "photoreal : aluminium anisotrope, laiton, légende en capitales (body case upper)")

    print("== Calques image ==")
    # Aucune image n'existe sous assets/ et le test n'écrit que sous build/_tests/11 : on y redirige
    # temporairement le dossier d'images servi (même mécanisme que la route Playwright), puis on restaure.
    import cv2  # noqa: E402 - import local : seul ce bloc a besoin d'écrire un PNG
    import numpy as np  # noqa: E402
    fake_assets = TEST_DIR / "assets" / "images"
    fake_assets.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(fake_assets / "logo test.png"), np.full((8, 8, 3), 200, np.uint8))
    saved = (config.ASSETS_DIR, config.SERVED_PREFIXES["/assets/"])
    config.ASSETS_DIR = TEST_DIR / "assets"
    config.SERVED_PREFIXES["/assets/"] = TEST_DIR / "assets"
    try:
        def with_image(src):
            return mutated(rich, lambda r: r["shots"][0]["layers"].append(
                {"id": "logo", "type": "image", "src": src, "w": 160, "h": 90, "anim": [{"move": "pop", "at": 0.5}]}))
        ci = scene.compile_scene(with_image("build/_tests/11/assets/images/logo test.png"))
        logo = layer_by_id(ci["shots"][0]["spec"], "logo")
        check(logo["src"] == "http://mograph.render/assets/images/logo%20test.png" and logo["fit"] == "cover"
              and not list(shot_validator.iter_errors(ci["shots"][0]["spec"])),
              "image sous assets/ : URL servie encodée, fit cover par défaut", logo["src"])
        expect_refused(with_image("build/_tests/11/assets/images/absente.png"), "image introuvable", ["image introuvable"])
        expect_refused(with_image("pipeline/config.py"), "image hors de assets/", ["doit se trouver sous assets/"])
        expect_refused(with_image("build/_tests/11/assets/images/logo.tiff"), "format d'image non servi", ["format d'image « .tiff » non servi"])
    finally:
        config.ASSETS_DIR, config.SERVED_PREFIXES["/assets/"] = saved

    print()
    if failures:
        print(f"ÉCHEC : {failures} contrôle(s) en échec.")
        return 1
    print("SUCCÈS : compilateur conforme (étape 11).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
