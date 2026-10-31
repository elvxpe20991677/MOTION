#!/usr/bin/env python3
"""Compare deux rendus d'une même plaque 3D (PNG RGBA 16 bits) : essai d'une autre version de Blender (phase 2).

Usage (depuis la racine) :
    .venv/Scripts/python.exe scripts/compare_plates.py <dossier_A> <dossier_B> [--json rapport.json]
Les dossiers contiennent les images NNNNNN.png d'une plaque (build/<scène>/plates/<plan>__<plaque>/).
Rendre les deux versions dans des build/ SÉPARÉS, par exemple :
    MOGRAPH_BUILD_DIR=build/_essai_blender52/ref_5.0.1/build python mograph.py plates demo_clay_3d
    MOGRAPH_BLENDER_VENV=.venv-blender52 MOGRAPH_BUILD_DIR=build/_essai_blender52/essai_5.2.3/build \\
        python mograph.py plates demo_clay_3d
Statistiques sur TOUTES les images (RVB ; échelle 16 bits) : images identiques au bit près, PSNR (min,
médian), écart absolu maximal (et image), écart moyen en niveaux 8 bits, part des pixels qui s'écartent de
plus d'un niveau 8 bits.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

import cv2
import numpy as np


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("a")
    ap.add_argument("b")
    ap.add_argument("--json", default=None)
    args = ap.parse_args()
    a_dir, b_dir = Path(args.a), Path(args.b)
    names = sorted(p.name for p in a_dir.glob("[0-9]" * 6 + ".png"))
    if not names:
        print(f"Aucune image NNNNNN.png dans {a_dir}.")
        return 1
    per, identical, missing = [], 0, []
    sum_abs, n_px, n_px_gt1 = 0.0, 0, 0
    for name in names:
        pb = b_dir / name
        if not pb.is_file():
            missing.append(name)
            continue
        a = cv2.imread(str(a_dir / name), cv2.IMREAD_UNCHANGED)
        b = cv2.imread(str(pb), cv2.IMREAD_UNCHANGED)
        if a is None or b is None or a.shape != b.shape:
            missing.append(name)
            continue
        if hashlib.sha256(a.tobytes()).digest() == hashlib.sha256(b.tobytes()).digest():
            identical += 1
        mx = float(np.iinfo(a.dtype).max)
        d = np.abs(a[..., :3].astype(np.float64) - b[..., :3].astype(np.float64))
        mse = float(np.mean(d ** 2))
        per.append({"image": name, "psnr_db": 99.0 if mse == 0 else round(10 * np.log10(mx * mx / mse), 3),
                    "max_abs": int(d.max())})
        sum_abs += float(d.sum()) * 255 / mx
        n_px += d.size
        px = d.max(-1) * 255 / mx > 1
        n_px_gt1 += int(px.sum())
        n_px_img = px.size
    worst = max(per, key=lambda s: s["max_abs"]) if per else None
    psnrs = sorted(s["psnr_db"] for s in per)
    summary = {"a": str(a_dir), "b": str(b_dir), "images": len(names), "compared": len(per), "missing": missing,
               "identical": identical, "psnr_min_db": psnrs[0] if psnrs else None,
               "psnr_median_db": float(np.median(psnrs)) if psnrs else None,
               "max_abs_16bit": worst["max_abs"] if worst else None, "max_abs_image": worst["image"] if worst else None,
               "mean_abs_8bit": round(sum_abs / n_px, 4) if n_px else None,
               "pct_pixels_gt_1_level_8bit": round(100 * n_px_gt1 / (n_px_img * len(per)), 3) if per else None,
               "per_image": per}
    print(json.dumps({k: v for k, v in summary.items() if k != "per_image"}, ensure_ascii=False, indent=1))
    if args.json:
        Path(args.json).write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
