#!/usr/bin/env python3
"""Installe c2patool (vérification indépendante des manifestes C2PA, phase 7) dans .tools/ (non versionné).

Usage (depuis la racine) :  .venv/Scripts/python.exe scripts/install_c2patool.py
Télécharge l'archive OFFICIELLE de la version épinglée dans versions.json (github.com/contentauth/c2patool,
licence MIT OU Apache-2.0), vérifie son empreinte SHA-256 AVANT extraction (refus sinon), puis extrait
dans .tools/c2patool-<version>/ ; config.c2patool_path() la trouve ensuite. Rien n'est exécuté avant la
vérification de l'empreinte. Windows x86_64 et Linux x86_64.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import sys
import tarfile
import tempfile
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def main() -> int:
    ref = json.loads((ROOT / "versions.json").read_text(encoding="utf-8"))["c2patool"]
    plat = "windows" if sys.platform == "win32" else "linux" if sys.platform.startswith("linux") else None
    if plat is None:
        print(f"Plateforme {sys.platform} non prise en charge : installez c2patool à la main (MOGRAPH_C2PATOOL).")
        return 1
    url, want = ref[f"{plat}_url"], ref[f"{plat}_sha256"]
    dest = ROOT / ".tools" / f"c2patool-{ref['version']}"
    exe = dest / "c2patool" / ("c2patool.exe" if plat == "windows" else "c2patool")
    if exe.is_file():
        print(f"Déjà installé : {exe}")
        return 0
    with tempfile.TemporaryDirectory(prefix="c2patool_") as tmp:
        archive = Path(tmp) / Path(url).name
        print(f"Téléchargement : {url}")
        with urllib.request.urlopen(url, timeout=120) as r, open(archive, "wb") as fh:
            shutil.copyfileobj(r, fh)
        got = hashlib.sha256(archive.read_bytes()).hexdigest()
        if got != want:
            print(f"EMPREINTE REFUSÉE : {got} au lieu de {want} (versions.json). Rien n'a été extrait.")
            return 1
        dest.mkdir(parents=True, exist_ok=True)
        if archive.suffix == ".zip":
            with zipfile.ZipFile(archive) as z:
                z.extractall(dest)
        else:
            with tarfile.open(archive) as t:
                t.extractall(dest, filter="data")
    if not exe.is_file():
        found = next(dest.rglob(exe.name), None)
        if found is None:
            print(f"Archive inattendue : {exe.name} introuvable dans {dest}.")
            return 1
        exe.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(found), exe)
    if plat == "linux":
        exe.chmod(0o755)
    print(f"Installé (empreinte vérifiée) : {exe}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
