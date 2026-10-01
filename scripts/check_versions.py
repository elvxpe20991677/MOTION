#!/usr/bin/env python3
"""Vérifie que les outils installés correspondent aux versions de référence (versions.json).

Usage (depuis la racine, avec le Python de l'orchestrateur) :
    .venv/Scripts/python.exe scripts/check_versions.py                      # machine de rendu
    .venv/bin/python scripts/check_versions.py --profile ci-linux --no-blender

Profils :
  reference : machine de rendu et job CI Windows : FFmpeg ET ffprobe = « ffmpeg.reference_version_string »
              (chaîne complète : 9.0.2-full_build-www.gyan.dev, pas seulement le numéro) ;
  ci-linux  : jobs CI Ubuntu : FFmpeg ET ffprobe = « ffmpeg.ci_linux_version_string » (écart documenté).
Tout le reste est exigé à l'identique dans les deux profils : Python, paquets pip (constraints.txt),
Chromium, paquets npm (package.json), bpy et son venv (constraints-blender.txt). Node et uv sont
sans effet sur les pixels : un écart n'est qu'une NOTE, sauf avec --strict (CI) où c'est un FAIL.
Sortie : lignes PASS / FAIL / NOTE / SKIP ; code ≠ 0 si au moins un FAIL.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from importlib import metadata
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pipeline import config  # noqa: E402  (après l'ajout de la racine au chemin d'import)

RESULTS: list[str] = []


def report(status: str, label: str, detail: str = "") -> None:
    RESULTS.append(status)
    print(f"{status} {label}" + (f" -- {detail}" if detail else ""), flush=True)


def _run(argv: list[str], timeout: int = 120) -> tuple[int, str]:
    try:
        r = subprocess.run(argv, cwd=ROOT, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=timeout)
        return r.returncode, (r.stdout or "") + (r.stderr or "")
    except (OSError, subprocess.SubprocessError) as exc:
        return -1, f"(indisponible : {exc})"


def _pins(path: Path) -> dict[str, str]:
    """« paquet==version » d'un fichier de contraintes (commentaires ignorés), noms normalisés."""
    pins = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if "==" in line:
            name, ver = line.split("==", 1)
            pins[re.sub(r"[-_.]+", "-", name).lower()] = ver.strip()
    return pins


def check_ffmpeg(ref: dict, profile: str) -> None:
    # Chaîne de version COMPLÈTE (ex. « 9.0.2-full_build-www.gyan.dev ») : un instantané git
    # « n9.0.2-17-g… » ou une build « essentials » a le même numéro mais d'autres bibliothèques.
    want = ref["reference_version_string"] if profile == "reference" else ref["ci_linux_version_string"]
    hint = ref.get("reference_build", "") if profile == "reference" else ref.get("ci_linux_note", "")
    for tool, exe in (("ffmpeg", config.FFMPEG), ("ffprobe", config.FFPROBE)):
        _, out = _run([exe, "-hide_banner", "-version"])
        m = re.search(rf"{tool} version (\S+)", out)
        got = m.group(1) if m else None
        first = out.splitlines()[0] if out else ""
        if got == want:
            report("PASS", f"{tool} {want} (profil {profile})", first)
        else:
            report("FAIL", f"{tool} {want} attendu (profil {profile})",
                   f"trouvé « {first or 'aucun ' + tool} » ({exe}) ; référence : {hint}")
    _, filters = _run([config.FFMPEG, "-hide_banner", "-filters"])
    _, encoders = _run([config.FFMPEG, "-hide_banner", "-encoders"])
    miss_f = [f for f in ref["required_filters"] if not re.search(rf"\s{re.escape(f)}\s", filters)]
    miss_e = [e for e in ref["required_encoders"] if not re.search(rf"\s{re.escape(e)}\s", encoders)]
    if miss_f or miss_e:
        report("FAIL", "FFmpeg : filtres et encodeurs requis",
               f"absents : {miss_f + miss_e} (build « full » nécessaire : libx265, prores_ks, libzimg)")
    else:
        report("PASS", "FFmpeg : filtres et encodeurs requis",
               ", ".join(ref["required_filters"] + ref["required_encoders"]))


def check_python(want: str) -> None:
    got = ".".join(map(str, sys.version_info[:3]))
    report("PASS" if got == want else "FAIL", f"Python orchestrateur {want}", f"trouvé {got} ({sys.executable})")


def check_pip(constraints: Path) -> None:
    bad = []
    pins = _pins(constraints)
    for name, want in sorted(pins.items()):
        try:
            got = metadata.version(name)
        except metadata.PackageNotFoundError:
            got = None
        if got != want:
            bad.append(f"{name} {got or 'absent'} (attendu {want})")
    report("PASS" if not bad else "FAIL", f"paquets pip conformes à {constraints.name} ({len(pins)})",
           "; ".join(bad) if bad else "toutes versions exactes")


def check_chromium(want: str) -> None:
    code, out = _run([sys.executable, "-c",
                      "from playwright.sync_api import sync_playwright\n"
                      "with sync_playwright() as p:\n"
                      "    b = p.chromium.launch(); print(b.version); b.close()"], timeout=300)
    got = out.strip().splitlines()[-1] if out.strip() else ""
    report("PASS" if code == 0 and got == want else "FAIL", f"Chromium {want} (playwright)",
           f"trouvé {got or out.strip()[-200:]}")


def check_npm() -> None:
    deps = json.loads((ROOT / "package.json").read_text(encoding="utf-8")).get("dependencies", {})
    bad = []
    for name, want in sorted(deps.items()):
        pj = config.NODE_MODULES / name / "package.json"
        got = json.loads(pj.read_text(encoding="utf-8")).get("version") if pj.is_file() else None
        if got != want:
            bad.append(f"{name} {got or 'absent'} (attendu {want})")
    report("PASS" if not bad else "FAIL", f"paquets npm conformes à package.json ({len(deps)})",
           "; ".join(bad) if bad else "toutes versions exactes (lancez « npm ci » sinon)")


def check_blender(want_py: str, want_bpy: str, constraints: Path) -> None:
    if not config.BLENDER_PYTHON.is_file():
        report("FAIL", "venv Blender", f"{config.BLENDER_PYTHON} absent : uv venv --python {want_py} .venv-blender")
        return
    pins = _pins(constraints)
    code, out = _run([str(config.BLENDER_PYTHON), "-c",
                      "import json, sys\nfrom importlib import metadata\n"
                      f"names = {sorted(pins)!r}\n"
                      "v = {}\n"
                      "for n in names:\n"
                      "    try: v[n] = metadata.version(n)\n"
                      "    except metadata.PackageNotFoundError: v[n] = None\n"
                      "print(json.dumps({'python': '.'.join(map(str, sys.version_info[:3])), 'pkgs': v}))"], timeout=300)
    try:
        info = json.loads(out.strip().splitlines()[-1])
    except (ValueError, IndexError):
        report("FAIL", "venv Blender lisible", out.strip()[-300:])
        return
    report("PASS" if info["python"] == want_py else "FAIL", f"Python du venv Blender {want_py}", f"trouvé {info['python']}")
    bad = [f"{n} {g or 'absent'} (attendu {pins[n]})" for n, g in sorted(info["pkgs"].items()) if g != pins[n]]
    report("PASS" if not bad and info["pkgs"].get("bpy") == want_bpy else "FAIL",
           f"bpy {want_bpy} et venv Blender conformes à {constraints.name} ({len(pins)})",
           "; ".join(bad) if bad else "toutes versions exactes")


def check_note(tool: str, argv: list[str], want: str, *, strict: bool = False) -> None:
    miss = "FAIL" if strict else "NOTE"
    exe = shutil.which(argv[0])
    if not exe:
        report(miss, f"{tool} {want}", "introuvable dans le PATH (sans effet sur les pixels)")
        return
    _, out = _run([exe, *argv[1:]])
    got = re.search(r"(\d+\.\d+\.\d+)", out)
    got = got.group(1) if got else out.strip()[:60]
    report("PASS" if got == want else miss, f"{tool} {want}",
           f"trouvé {got}" + ("" if got == want else " (sans effet sur les pixels)"))


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--profile", choices=("reference", "ci-linux"), default="reference")
    p.add_argument("--no-blender", action="store_true", help="ne pas contrôler le venv Blender (job sans bpy)")
    p.add_argument("--no-chromium", action="store_true", help="ne pas lancer Chromium")
    p.add_argument("--strict", action="store_true",
                   help="un écart de Node ou d'uv devient un FAIL (CI : versions épinglées dans le workflow)")
    args = p.parse_args()
    ref = json.loads((ROOT / "versions.json").read_text(encoding="utf-8"))
    check_ffmpeg(ref["ffmpeg"], args.profile)
    check_python(ref["python"])
    check_pip(ROOT / ref["pip_constraints"])
    if args.no_chromium:
        report("SKIP", "Chromium", "--no-chromium")
    else:
        check_chromium(ref["chromium"])
    check_npm()
    if args.no_blender:
        report("SKIP", "venv Blender", "--no-blender")
    else:
        check_blender(ref["blender_python"], ref["bpy"], ROOT / ref["blender_constraints"])
    check_note("Node", ["node", "--version"], ref["node"], strict=args.strict)
    check_note("uv", ["uv", "--version"], ref["uv"], strict=args.strict)
    fails = RESULTS.count("FAIL")
    print(f"\n{'VERSIONS CONFORMES' if not fails else f'{fails} écart(s) de version'} (profil {args.profile})")
    return 0 if not fails else 1


if __name__ == "__main__":
    sys.exit(main())
