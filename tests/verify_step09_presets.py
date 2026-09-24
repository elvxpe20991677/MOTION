"""Vérification de l'étape 9 : les 6 presets.

Contrôles (chacun affiche PASS/FAIL, code de sortie ≠ 0 au moindre échec) :
  1. chaque preset valide schema/preset.schema.json (Registry referencing avec scene.schema.json pour
     les $ref croisés vers #/$defs/animProps, material, light, world) ;
  2. chaque fichier de police déclaré existe dans node_modules ;
  3. chaque (famille, graisse) de typography est déclarée dans fonts ;
  4. chaque ease (6 alias + étapes des moves) est résoluble : alias (une indirection), customEase du
     preset, ease GSAP 3 valide ou steps(N) ; aucun ease linéaire ; tracés CustomEase bien formés ;
  5. palettes EXACTES de la SPEC, moves / textures / rendu / blender demandés par la mission ;
  6. le chargeur du compilateur (pipeline.scene.load_preset) accepte le preset et complète les
     unicodeRange depuis les CSS @fontsource.

Lancement depuis la racine : .venv/Scripts/python.exe tests/verify_step09_presets.py
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

# Racine du projet ajoutée au chemin d'import : le test se lance depuis la racine sans installation.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import jsonschema  # noqa: E402
import referencing  # noqa: E402
from referencing.jsonschema import DRAFT202012  # noqa: E402

from pipeline import config  # noqa: E402

PRESET_IDS = ["flat_riso", "kinetic_signal", "vhs_glitch", "handmade_chalk", "clay_3d", "photoreal_3d"]

# Palettes imposées mot pour mot par la SPEC (étape 9) : nom de rôle -> hexadécimal.
SPEC_PALETTES = {
    "flat_riso": {"bg": "#EEF1F6", "fg": "#1B1F4A", "primary": "#2F5BEA", "secondary": "#FF4F9A", "accent": "#FFC93C"},
    "kinetic_signal": {"bg": "#2A1FE6", "fg": "#FFFFFF", "primary": "#FF4B23"},
    "vhs_glitch": {"bg": "#0D0A1C", "primary": "#37F2FF", "secondary": "#FF2BD1", "accent": "#FFB21C"},
    "handmade_chalk": {"bg": "#26352C", "fg": "#F1EEE4", "primary": "#F4D35E", "secondary": "#F28FAD", "accent": "#8FC9EA"},
    "clay_3d": {"bg": "#E9E4FF", "primary": "#8C7BFF", "secondary": "#FF9F7A", "accent": "#62D2B4", "butter": "#FFD66B"},
    "photoreal_3d": {"bg": "#1E2024", "primary": "#C9CED6", "secondary": "#D4A373"},
}

# Couples (famille, graisse) imposés par la SPEC pour la typographie display / body.
SPEC_TYPO = {
    "flat_riso": {("Bricolage Grotesque", 800), ("DM Sans", 500)},
    "kinetic_signal": {("Anton", 400), ("Inter Tight", 600)},
    "vhs_glitch": {("VT323", 400), ("Space Mono", 400)},
    "handmade_chalk": {("Caveat", 700), ("Patrick Hand", 400)},
    "clay_3d": {("Fraunces", 800), ("DM Sans", 500)},
    "photoreal_3d": {("Inter Tight", 300), ("Inter Tight", 600)},
}

SPEC_MOVES = {
    "flat_riso": {"rise", "chars_up", "pop", "wipe_in", "draw", "slide_anticipate", "exit_down"},
    "kinetic_signal": {"slam", "chars_drop", "slide_anticipate", "punch_out"},
    "vhs_glitch": {"type_on", "flicker_in", "slam_step", "jitter_out", "rise"},
    "handmade_chalk": {"draw", "write_on", "wobble_in", "pop", "exit_down"},
    "clay_3d": {"rise", "chars_up", "pop", "settle_in", "exit_down"},
    "photoreal_3d": {"fade_up", "wipe_in", "chars_fade", "exit_fade"},
}

ALIASES = ("default", "enter", "exit", "emphasis", "anticipate", "settle")
# Grammaire des eases GSAP 3 (CONTRACT §3.6.5), réécrite ici indépendamment du compilateur pour
# que ce test vérifie les presets sans dépendre de l'implémentation qu'il est censé contrôler.
GSAP_RE = re.compile(
    r"^(none|linear|power[0-4]|sine|expo|circ|back|elastic|bounce|quad|cubic|quart|quint|strong)"
    r"(?:\.(in|out|inOut))?(?:\(([^()]*)\))?$"
)
STEPS_RE = re.compile(r"^steps\((\d+)\)$")
LINEAR_FAMILIES = {"none", "linear", "power0"}
PARAMS_MAX = {"back": 1, "elastic": 2}

failures = 0


def check(ok: bool, label: str, detail: str = "") -> bool:
    """Affiche une ligne PASS/FAIL et comptabilise les échecs."""
    global failures
    if ok:
        print(f"PASS {label}")
    else:
        failures += 1
        print(f"FAIL {label}" + (f" — {detail}" if detail else ""))
    return ok


def load(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def build_validator():
    """Validateur preset avec un Registry contenant scene.schema.json (références croisées)."""
    scene_schema = load(config.SCHEMA_DIR / "scene.schema.json")
    preset_schema = load(config.SCHEMA_DIR / "preset.schema.json")
    jsonschema.Draft202012Validator.check_schema(preset_schema)
    registry = referencing.Registry().with_resources(
        [
            (s["$id"], referencing.Resource.from_contents(s, default_specification=DRAFT202012))
            for s in (scene_schema, preset_schema)
        ]
    )
    return jsonschema.Draft202012Validator(preset_schema, registry=registry)


def ease_problem(ease: str, preset: dict) -> str | None:
    """None si l'ease est résoluble et non linéaire, sinon l'explication du problème."""
    eases = preset["motion"]["eases"]
    customs = preset["motion"].get("customEases", {})
    name = ease
    for _ in range(2):  # alias -> valeur, avec au plus une indirection supplémentaire
        if name in ALIASES:
            name = eases[name]
    if name in ALIASES:
        return f"alias « {ease} » : plus d'une indirection"
    if name in customs:
        return None
    compact = name.replace(" ", "")
    m = STEPS_RE.match(compact)
    if m:
        return None if int(m.group(1)) >= 1 else "steps(0)"
    m = GSAP_RE.match(compact)
    if not m:
        return f"« {ease} » (→ « {name} ») n'est ni un alias, ni un customEase, ni un ease GSAP"
    fam, _direction, params = m.groups()
    if fam in LINEAR_FAMILIES:
        return f"« {ease} » est linéaire (interdit dans un preset)"
    if params is not None:
        toks = [t for t in params.split(",") if t != ""]
        if len(toks) > PARAMS_MAX.get(fam, 0):
            return f"« {ease} » : trop de paramètres pour {fam}"
        try:
            [float(t) for t in toks]
        except ValueError:
            return f"« {ease} » : paramètre non numérique"
    return None


def custom_path_problem(path: str) -> str | None:
    nums = [float(x) for x in re.findall(r"-?(?:\d+\.?\d*|\.\d+)", path)]
    if len(nums) < 8 or len(nums) % 2 or (len(nums) - 2) % 6:
        return "nombre de coordonnées incompatible avec M x,y C (x1,y1 x2,y2 x,y)+"
    pts = list(zip(nums[0::2], nums[1::2]))
    if pts[0] != (0.0, 0.0) or pts[-1] != (1.0, 1.0):
        return "le tracé doit partir de 0,0 et finir en 1,1"
    anchors = [pts[0]] + pts[3::3]
    if any(b[0] < a[0] for a, b in zip(anchors, anchors[1:])):
        return "abscisses des points d'ancrage non croissantes"
    return None


def main() -> int:
    # Sortie redirigée sous Windows = cp1252 par défaut : on force l'UTF-8 pour les accents et « → ».
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    validator = build_validator()
    anim_props = set(load(config.SCHEMA_DIR / "scene.schema.json")["$defs"]["animProps"]["properties"])
    presets: dict[str, dict] = {}

    print("== 1. Validation par schéma ==")
    for pid in PRESET_IDS:
        path = config.PRESETS_DIR / f"{pid}.json"
        if not check(path.is_file(), f"{pid} : fichier {path.relative_to(ROOT)} présent"):
            continue
        data = load(path)
        presets[pid] = data
        errs = sorted(validator.iter_errors(data), key=lambda e: list(map(str, e.absolute_path)))
        check(not errs, f"{pid} : conforme à preset.schema.json",
              "; ".join(f"{'/'.join(map(str, e.absolute_path))}: {e.message}" for e in errs[:5]))
        check(data.get("$schema") == "../schema/preset.schema.json", f"{pid} : $schema pointe vers ../schema/preset.schema.json")
        check(data.get("id") == pid, f"{pid} : id cohérent avec le nom de fichier")

    print("== 2. Fichiers de police ==")
    for pid, data in presets.items():
        for font in data["fonts"]:
            f = config.NODE_MODULES / font["file"]
            expected = re.fullmatch(r"@fontsource/([a-z0-9-]+)/files/\1-latin-(\d{3})-normal\.woff2", font["file"])
            check(f.is_file(), f"{pid} : {font['family']} {font['weight']} -> {font['file']} existe", str(f))
            check(bool(expected) and int(expected.group(2)) == font["weight"],
                  f"{pid} : {font['file']} = sous-ensemble latin de la graisse {font['weight']}")

    print("== 3. Typographie déclarée dans fonts ==")
    for pid, data in presets.items():
        declared = {(f["family"], f["weight"]) for f in data["fonts"] if f.get("style", "normal") == "normal"}
        used = {(data["typography"][s]["family"], data["typography"][s]["weight"]) for s in ("display", "body")}
        for fam, w in sorted(used):
            check((fam, w) in declared, f"{pid} : typography {fam} {w} déclarée dans fonts")
        check(used == SPEC_TYPO[pid], f"{pid} : typographie conforme à la SPEC {sorted(SPEC_TYPO[pid])}", str(sorted(used)))
        for s in ("display", "body"):
            t = data["typography"][s]
            check(all(k in t for k in ("family", "weight", "tracking", "lineHeight", "case", "size")),
                  f"{pid} : typography.{s} entièrement définie (family, weight, tracking, lineHeight, case, size)")

    print("== 4. Easings résolubles ==")
    for pid, data in presets.items():
        motion = data["motion"]
        for name, path in motion["customEases"].items():
            prob = custom_path_problem(path)
            check(prob is None, f"{pid} : customEase {name} bien formé", prob or "")
        for alias in ALIASES:
            prob = ease_problem(alias, data)
            check(prob is None, f"{pid} : alias {alias} -> {motion['eases'][alias]}", prob or "")
        for move, steps in motion["moves"].items():
            for i, st in enumerate(steps):
                prob = ease_problem(st.get("ease", "default"), data)
                check(prob is None, f"{pid} : moves.{move}[{i}].ease = {st.get('ease', 'default')}", prob or "")
                keys = set(st.get("from", {})) | set(st.get("to", {}))
                check(keys <= anim_props, f"{pid} : moves.{move}[{i}] n'anime que des propriétés animables", str(keys - anim_props))
                if keys & {"draw", "fill", "stroke"}:
                    check(st.get("target") == "shape", f"{pid} : moves.{move}[{i}] draw/fill/stroke ciblent 'shape'")

    print("== 5. Conformité à la mission ==")
    for pid, data in presets.items():
        pal = data["palette"]
        for role, hexa in SPEC_PALETTES[pid].items():
            check(pal.get(role, "").upper() == hexa, f"{pid} : palette.{role} = {hexa}", str(pal.get(role)))
        check(len(pal) > 5, f"{pid} : couleurs libres en plus des 5 rôles ({len(pal) - 5})")
        check(SPEC_MOVES[pid] <= set(data["motion"]["moves"]), f"{pid} : moves {sorted(SPEC_MOVES[pid])}",
              str(sorted(SPEC_MOVES[pid] - set(data["motion"]["moves"]))))
        for key in ("grid", "margin", "type_scale", "notes"):
            check(key in data["composition"], f"{pid} : composition.{key} défini")
        check(data["color"] == {"working": "srgb", "output": "rec709"}, f"{pid} : color srgb -> rec709")

    if "flat_riso" in presets:
        p = presets["flat_riso"]
        check(p["motion"]["customEases"] == {"flatSettle": "M0,0 C0.12,0.9 0.24,1 1,1",
                                             "flatAnticipate": "M0,0 C0.35,-0.28 0.2,1.12 1,1"},
              "flat_riso : customEases flatSettle et flatAnticipate exacts")
        check(p["textures"]["grain"]["amount"] == 0.07 and p["textures"]["paper"]["amount"] == 0.08
              and p["textures"]["paper"]["blend"] == "multiply", "flat_riso : grain 0.07, papier 0.08 multiply")
        check(p["transitions"] == {"default": "crossfade", "crossfade": {"dur": 0.4}}, "flat_riso : fondu 0,4 s par défaut")
        chars_up = p["motion"]["moves"]["chars_up"][0]
        check(chars_up["target"] == "chars" and chars_up["from"]["y"] == "110%" and chars_up["to"]["y"] == 0
              and "opacity" in chars_up["from"], "flat_riso : chars_up = chars, y 110% -> 0 + opacité")
        pop = p["motion"]["moves"]["pop"][0]
        check(pop["from"] == {"scale": 0} and pop["to"] == {"scale": 1} and pop["ease"] == "emphasis"
              and p["motion"]["eases"]["emphasis"].startswith("back.out"), "flat_riso : pop scale 0 -> 1, ease emphasis = back.out")
        wipe = p["motion"]["moves"]["wipe_in"][0]
        check(wipe["from"] == {"reveal": 1} and wipe["to"] == {"reveal": 0}, "flat_riso : wipe_in reveal 1 -> 0")
        draw = p["motion"]["moves"]["draw"][0]
        check(draw["target"] == "shape" and draw["from"] == {"draw": 0} and draw["to"] == {"draw": 1}, "flat_riso : draw sur la forme 0 -> 1")
        check(any(s.get("ease") == "anticipate" for s in p["motion"]["moves"]["slide_anticipate"])
              and p["motion"]["eases"]["anticipate"] == "flatAnticipate", "flat_riso : slide_anticipate utilise anticipate = flatAnticipate")
    if "kinetic_signal" in presets:
        p = presets["kinetic_signal"]
        slam = p["motion"]["moves"]["slam"]
        first = slam[0]
        check(first["from"].get("scale", 0) >= 2 and first["to"].get("scale") == 1 and "opacity" in first["from"]
              and first["ease"] == "power4.in", "kinetic_signal : slam étape 1 = impact scale ~2,4 -> 1 + opacité, power4.in")
        check(len(slam) >= 3 and slam[1]["to"].get("scaleX", 1) > 1 and slam[1]["to"].get("scaleY", 1) < 1
              and slam[1].get("offset") == first["dur"], "kinetic_signal : slam étape 2 = écrasement à l'impact")
        check(slam[2]["to"] == {"scaleX": 1, "scaleY": 1} and slam[2]["ease"].startswith("back.out"), "kinetic_signal : slam étape 3 = retour à 1 en back.out")
        check(p["typography"]["display"]["case"] == "upper", "kinetic_signal : Anton en capitales")
        check(p["render"]["motion_blur"] == {"enabled": True, "samples": 8, "shutter": 0.5}, "kinetic_signal : flou de mouvement 8 échantillons")
        check(p["transitions"]["default"] == "cut", "kinetic_signal : coupe franche par défaut")
    if "vhs_glitch" in presets:
        p = presets["vhs_glitch"]
        check(all(STEPS_RE.match(p["motion"]["eases"][a]) for a in ALIASES), "vhs_glitch : les 6 alias sont des steps(n)")
        t = p["textures"]
        check(t["grain"]["amount"] == 0.16 and t["scanlines"] and t["tracking"] and t["vignette"], "vhs_glitch : grain 0.16 + balayage + tracking + vignette")
        check(p["render"]["motion_blur"]["enabled"] is False, "vhs_glitch : pas de flou de mouvement")
        check(p["motion"]["moves"]["type_on"][0]["target"] == "chars", "vhs_glitch : type_on par caractère")
        ss = p["motion"]["moves"]["slam_step"][0]
        check("scale" in ss["from"] and "opacity" in ss["from"], "vhs_glitch : slam_step étape 1 = impact")
        check({("Space Mono", 400), ("Space Mono", 700)} <= {(f["family"], f["weight"]) for f in p["fonts"]}, "vhs_glitch : Space Mono 400/700 déclarées")
    if "handmade_chalk" in presets:
        p = presets["handmade_chalk"]
        check(p["motion"]["frame_step"] == 2, "handmade_chalk : animation tenue deux images (frame_step 2)")
        b = p["textures"]["boil"]
        check(bool(b) and b.get("step") == 2 and "amount" in b and "freq" in b, "handmade_chalk : boil (amount, freq, step 2)")
        check(p["textures"]["paper"]["blend"] == "screen", "handmade_chalk : papier en mode screen")
    if "clay_3d" in presets:
        b = presets["clay_3d"]["blender"]
        check(b["view_transform"] == "AgX" and b["look"] == "Base Contrast", "clay_3d : AgX + Base Contrast")
        check(b["bounces"] == {"total": 6, "diffuse": 3, "glossy": 2, "transmission": 0, "transparent": 4, "volume": 0}
              and b["caustics"] is False, "clay_3d : rebonds 6/3/2/0 sans caustiques")
        check(b["samples"] == 128 and b["adaptive_threshold"] == 0.02 and b["denoise"] is True and b["device"] == "auto",
              "clay_3d : 128 échantillons, seuil adaptatif 0.02, débruitage, device auto")
        check(b["lens"] == 50 and b["fstop"] == 5.6 and b["motion_blur_shutter"] == 0.5, "clay_3d : 50 mm f/5.6, obturateur 0.5")
        check(b["world"]["color"].upper() == presets["clay_3d"]["palette"]["bg"].upper(), "clay_3d : monde = palette.bg")
        mats = b["materials"]
        want = {"lilac": "#8C7BFF", "peach": "#FF9F7A", "mint": "#62D2B4", "butter": "#FFD66B"}
        check(all(mats[k]["base_color"].upper() == v for k, v in want.items()) and "floor" in mats, "clay_3d : matériaux lilac/peach/mint/butter + floor")
        check(all(mats[k]["roughness"] == 0.5 and mats[k]["sheen"] == 0.35 for k in want), "clay_3d : pâte mate veloutée (roughness 0.5, sheen 0.35)")
        rig = b["light_rig"]
        check(len(rig) == 3 and all(l["type"] == "AREA" and l["shape"] == "DISK" for l in rig)
              and {l["id"] for l in rig} == {"key", "fill", "rim"}, "clay_3d : 3 AREA DISK (key, fill, rim)")
    if "photoreal_3d" in presets:
        b = presets["photoreal_3d"]["blender"]
        check(b["samples"] == 512 and b["look"] == "Medium High Contrast" and b["fstop"] == 2.8, "photoreal_3d : 512 éch., Medium High Contrast, f/2.8")
        check(isinstance(b["world"]["sky"], dict) or b["world"]["sky"] is True, "photoreal_3d : ciel physique")
        mats = b["materials"]
        check(mats["aluminium"]["metallic"] == 1 and mats["aluminium"]["anisotropic"] > 0, "photoreal_3d : aluminium anisotrope")
        check(mats["brass"]["base_color"].upper() == "#D4A373" and mats["brass"]["metallic"] == 1, "photoreal_3d : laiton #D4A373")
        check(mats["ceramic"]["coat"] > 0 and mats["ceramic"]["metallic"] == 0, "photoreal_3d : céramique laquée (coat)")
        check("graphite" in mats, "photoreal_3d : graphite")

    print("== 6. Chargement par le compilateur ==")
    try:
        from pipeline import scene  # import tardif : les contrôles 1-5 restent indépendants du compilateur
    except Exception as exc:  # noqa: BLE001 - on veut afficher toute cause d'échec d'import
        check(False, "import pipeline.scene", repr(exc))
        scene = None
    if scene is not None:
        for pid in PRESET_IDS:
            try:
                loaded = scene.load_preset(pid)
            except scene.SceneError as exc:
                check(False, f"{pid} : load_preset", str(exc))
                continue
            ok = all(f.get("unicodeRange", "").startswith("U+") for f in loaded["fonts"])
            check(ok, f"{pid} : load_preset complète unicodeRange depuis le CSS @fontsource")
            if pid == "flat_riso":
                print(f"      exemple : {loaded['fonts'][0]['family']} {loaded['fonts'][0]['weight']} -> "
                      f"{loaded['fonts'][0]['unicodeRange'][:60]}…")

    print()
    if failures:
        print(f"ÉCHEC : {failures} contrôle(s) en échec.")
        return 1
    print("SUCCÈS : les 6 presets sont conformes.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
