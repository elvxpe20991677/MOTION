#!/usr/bin/env python3
"""Aller-retour d'un maître mograph par DaVinci Resolve (phase 9) : plage et métadonnées couleur conservées ?

Usage (Resolve Studio LANCÉ, Préférences > Système > Général > « External scripting using » = Local) :
    .venv/Scripts/python.exe scripts/resolve_roundtrip.py out/<scène>/<scène>_prores_422hq.mov [--force-reencode] [--keep]

Passe UNIQUEMENT par l'API de scripting officielle de Blackmagic (README et DaVinciResolveScript.pyi
livrés avec Resolve, Support/Developer/Scripting) ; le serveur MCP de Resolve 21.1 n'est pas utilisé
(non confirmé par une documentation officielle, CONTRACT §11.10). Ne touche à AUCUN projet existant : crée
un projet dédié « mograph_aller_retour_<date> », y importe le fichier, rend un ProRes 422 HQ « Same as
Project » dans build/_resolve/<date>/, puis compare source et retour (ffprobe, signalstats, PSNR) et
écrit build/_resolve/<date>/aller_retour.json. Le projet est conservé avec --keep, sinon supprimé
(c'est le seul projet que le script ait créé).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pipeline import config  # noqa: E402

COLOR_KEYS = ("color_primaries", "color_transfer", "color_space", "color_range")


def connect():
    """Objet Resolve (API de scripting officielle) ; SystemExit actionnable sinon."""
    if config.IS_WINDOWS:
        api = Path(os.environ.get("PROGRAMDATA", r"C:\ProgramData")) / "Blackmagic Design" / "DaVinci Resolve" / \
            "Support" / "Developer" / "Scripting"
        lib = Path(os.environ.get("PROGRAMFILES", r"C:\Program Files")) / "Blackmagic Design" / "DaVinci Resolve" / \
            "fusionscript.dll"
    else:
        api, lib = Path("/opt/resolve/Developer/Scripting"), Path("/opt/resolve/libs/Fusion/fusionscript.so")
    os.environ.setdefault("RESOLVE_SCRIPT_API", str(api))
    os.environ.setdefault("RESOLVE_SCRIPT_LIB", str(lib))
    sys.path.append(str(api / "Modules"))
    try:
        import DaVinciResolveScript as dvr  # noqa: N813 (nom du module officiel)
    except ImportError as exc:
        raise SystemExit(f"Module de scripting Resolve introuvable ({exc}) : installez DaVinci Resolve Studio.")
    resolve = dvr.scriptapp("Resolve")
    if resolve is None:
        raise SystemExit("Resolve injoignable : lancez DaVinci Resolve Studio (complètement chargé) et réglez "
                         "Préférences > Système > Général > « External scripting using » sur Local. Ce réglage "
                         "de sécurité relève de l'utilisateur : le script ne le modifie pas.")
    return resolve


def probe(path: Path) -> dict:
    r = subprocess.run([config.FFPROBE, "-v", "error", "-select_streams", "v:0", "-count_frames", "-show_entries",
                        "stream=codec_name,profile,pix_fmt,width,height,r_frame_rate,nb_read_frames,"
                        + ",".join(COLOR_KEYS) + ":stream_tags=timecode:format_tags=timecode",
                        "-of", "json", str(path)], capture_output=True, text=True, check=True)
    d = json.loads(r.stdout)
    s = d["streams"][0]
    s["timecode"] = (s.pop("tags", {}) or {}).get("timecode") or (d.get("format", {}).get("tags") or {}).get("timecode")
    return s


def levels(path: Path) -> dict:
    """Min/max de Y, U, V et moyenne de Y (codes 10 bits) sur toutes les images : plage effectivement utilisée."""
    r = subprocess.run([config.FFMPEG, "-v", "error", "-i", str(path), "-vf",
                        "format=yuv422p10le,signalstats,metadata=print:file=-", "-f", "null", "-"],
                       capture_output=True, text=True, errors="replace")
    vals: dict[str, list[float]] = {}
    for k, v in re.findall(r"lavfi\.signalstats\.(YMIN|YMAX|UMIN|UMAX|VMIN|VMAX|YAVG)=([0-9.]+)", r.stdout):
        vals.setdefault(k, []).append(float(v))
    out = {k: (min(v) if k.endswith("MIN") else max(v)) for k, v in vals.items() if k != "YAVG"}
    out["YAVG"] = round(sum(vals.get("YAVG", [0])) / max(1, len(vals.get("YAVG", []))), 3)
    return out


def psnr(a: Path, b: Path, src_filter: str = "") -> float | None:
    """PSNR moyen (yuv422p10le) entre a, éventuellement transformé par src_filter, et b."""
    pre = f"{src_filter}," if src_filter else ""
    r = subprocess.run([config.FFMPEG, "-v", "info", "-i", str(a), "-i", str(b), "-lavfi",
                        f"[0:v]{pre}format=yuv422p10le[x];[1:v]format=yuv422p10le[y];[x][y]psnr", "-f", "null", "-"],
                       capture_output=True, text=True, errors="replace")
    m = re.search(r"average:([0-9.]+|inf)", r.stderr)
    return None if not m else (99.0 if m.group(1) == "inf" else float(m.group(1)))


def _stream_md5(path: Path) -> str:
    r = subprocess.run([config.FFMPEG, "-v", "error", "-i", str(path), "-map", "0:v:0", "-c", "copy", "-f", "md5", "-"],
                       capture_output=True, text=True)
    return r.stdout.strip()


# Hypothèses de plage testées contre le retour : niveaux conservés, expansion vidéo -> pleine, compression
# pleine -> vidéo. La plage est conservée si l'hypothèse « conservés » explique le mieux le retour, avec une
# marge : une statistique globale (moyenne, extrêmes) ne suffit pas, car une conversion de plage a un point
# fixe vers le gris moyen (Y ≈ 445 en 10 bits) et passerait inaperçue sur une image peu contrastée.
# Conversions explicites sur les codes 10 bits (BT.709 : Y 64..940, C 64..960 autour de 512) : les filtres scale
# et zscale ne convertissent pas une source déjà en yuv422p10le (mesuré : aucune variation).
_EXP = ("format=yuv422p10le,lutyuv=y='clip((val-64)*1023/876,0,1023)':u='clip((val-512)*1023/896+512,0,1023)'"
        ":v='clip((val-512)*1023/896+512,0,1023)'")
_CMP = ("format=yuv422p10le,lutyuv=y='val*876/1023+64':u='(val-512)*896/1023+512':v='(val-512)*896/1023+512'")
RANGE_HYPOTHESES = {"expansion_tv_vers_pc": _EXP, "compression_pc_vers_tv": _CMP}
RANGE_MARGIN_DB = 3.0


def compare(src: Path, dst: Path) -> dict:
    """Comparaison source / retour (sans Resolve) : étiquettes, images, timecode, plage, flux, PSNR."""
    a, b = probe(src), probe(dst)
    la, lb = levels(src), levels(dst)
    same = psnr(src, dst)
    alt = {k: psnr(src, dst, f) for k, f in RANGE_HYPOTHESES.items()}
    best_alt = max((v for v in alt.values() if v is not None), default=0.0)
    return {
        "returned": str(dst), "probe_source": a, "probe_returned": b,
        "color_tags_kept": all(a.get(k) == b.get(k) for k in COLOR_KEYS),
        "format_kept": all(a.get(k) == b.get(k) for k in ("codec_name", "profile", "pix_fmt", "width", "height")),
        "frames_kept": a.get("nb_read_frames") == b.get("nb_read_frames"),
        "timecode_kept": a.get("timecode") == b.get("timecode"),
        "levels_source": la, "levels_returned": lb,
        "extremes_identical": all(la[k] == lb.get(k) for k in la),
        "psnr_db": same, "psnr_range_hypotheses_db": alt,
        "range_kept": a.get("color_range") == b.get("color_range") and same is not None
                      and same >= best_alt + RANGE_MARGIN_DB,
        "bitstream_identical": _stream_md5(src) == _stream_md5(dst),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("master", help="maître ProRes 422 HQ (.mov) produit par mograph")
    ap.add_argument("--keep", action="store_true", help="garder le projet Resolve créé")
    ap.add_argument("--force-reencode", action="store_true",
                    help="appliquer un étalonnage CDL NEUTRE au plan : Resolve ne peut plus recopier le flux et passe "
                         "par sa chaîne couleur (cas d'un étalonnage réel)")
    args = ap.parse_args()
    src = Path(args.master).resolve()
    if not src.is_file():
        raise SystemExit(f"Fichier introuvable : {src}")
    info = probe(src)
    if info.get("codec_name") != "prores" or info.get("profile") != "HQ":
        raise SystemExit(f"Source {info.get('codec_name')} {info.get('profile')} : seul un maître ProRes 422 HQ "
                         "(livrable prores_422hq) est pris en charge ; un ProRes 4444 avec alpha n'est pas vérifié par ce "
                         "script (le retour serait un 422 HQ sans alpha).")
    num, den = (int(x) for x in info["r_frame_rate"].split("/"))
    fps = num / den
    frames = int(info.get("nb_read_frames") or 0)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    out_dir = config.BUILD_DIR / "_resolve" / stamp
    out_dir.mkdir(parents=True, exist_ok=True)

    resolve = connect()
    pm = resolve.GetProjectManager()
    name = f"mograph_aller_retour_{stamp}"
    if name in (pm.GetProjectListInCurrentFolder() or []):
        raise SystemExit(f"Un projet « {name} » existe déjà : relancez dans une seconde.")
    report = {"source": str(src), "resolve": resolve.GetVersionString(), "project": name, "kept": args.keep,
              "deleted": False}
    proj = pm.CreateProject(name)
    if not proj:
        raise SystemExit(f"Création du projet Resolve « {name} » impossible.")
    failure = None
    try:
        proj.SetSetting("timelineFrameRate", f"{fps:g}")
        proj.SetSetting("timelineResolutionWidth", str(info["width"]))
        proj.SetSetting("timelineResolutionHeight", str(info["height"]))
        # Données sous le noir / au-dessus du blanc nominal conservées (sinon Resolve les écrête au rendu traité).
        report["retain_superwhite_set"] = bool(proj.SetSetting("videoDataLevelsRetainSubblockAndSuperWhiteData", "1"))
        report["project_color"] = {k: proj.GetSetting(k) for k in
                                   ("colorScienceMode", "colorSpaceTimeline", "colorSpaceOutput", "videoDataLevels",
                                    "videoDataLevelsRetainSubblockAndSuperWhiteData")}
        mp = proj.GetMediaPool()
        items = mp.ImportMedia([str(src)])
        if not items:
            raise SystemExit("Import du fichier dans Resolve refusé.")
        report["clip_properties"] = {k: items[0].GetClipProperty(k) for k in
                                     ("Video Codec", "Data Level", "Input Color Space", "Frames", "FPS")}
        tl = mp.CreateTimelineFromClips("aller_retour", items)
        if not tl:
            raise SystemExit("Création de la timeline impossible.")
        # Timecode de départ de la timeline = celui de la source (sinon le retour repart de 00:00:00:00).
        if info.get("timecode"):
            report["timeline_timecode_set"] = bool(tl.SetStartTimecode(info["timecode"]))
        if args.force_reencode:
            item = (tl.GetItemListInTrack("video", 1) or [None])[0]
            report["neutral_cdl_applied"] = bool(item and item.SetCDL({"NodeIndex": "1", "Slope": "1.0 1.0 1.0",
                                                                       "Offset": "0.0 0.0 0.0", "Power": "1.0 1.0 1.0",
                                                                       "Saturation": "1.0"}))
        codecs = proj.GetRenderCodecs("mov") or {}
        report["render_codecs"] = list(codecs)
        codec = next((v for k, v in codecs.items() if "422 HQ" in k), None)
        if not codec or not proj.SetCurrentRenderFormatAndCodec("mov", codec):
            raise SystemExit(f"Codec ProRes 422 HQ introuvable parmi {list(codecs)}.")
        ok = proj.SetRenderSettings({"SelectAllFrames": True, "TargetDir": str(out_dir), "CustomName": "retour",
                                     "ExportVideo": True, "ExportAudio": False, "FormatWidth": int(info["width"]),
                                     "FormatHeight": int(info["height"]), "FrameRate": float(fps),
                                     "ColorSpaceTag": "Same as Project", "GammaTag": "Same as Project"})
        if not ok:
            raise SystemExit("Réglages de rendu refusés par Resolve.")
        job = proj.AddRenderJob()
        if not job or not proj.StartRendering([job]):
            raise SystemExit("Lancement du rendu refusé par Resolve.")
        t0 = time.perf_counter()
        limit = 60 + 2 * max(frames, 1)
        while proj.IsRenderingInProgress():
            if time.perf_counter() - t0 > limit:
                proj.StopRendering()
                raise SystemExit(f"Rendu Resolve non terminé en {limit} s (boîte de dialogue, média hors ligne ?) : "
                                 "arrêté.")
            time.sleep(1)
        report["render_status"] = proj.GetRenderJobStatus(job)
        report["render_seconds"] = round(time.perf_counter() - t0, 1)
    except SystemExit as exc:
        failure = str(exc)
        report["error"] = failure
    finally:
        pm.SaveProject()
        if not args.keep:
            closed = bool(pm.CloseProject(proj))
            removed = bool(pm.DeleteProject(name))
            report["deleted"] = closed and removed and name not in (pm.GetProjectListInCurrentFolder() or [])
        if failure is None:
            dst = next(iter(sorted(out_dir.glob("retour*.mov"))), None)
            if dst is None:
                report["error"] = failure = "aucun fichier rendu par Resolve"
            else:
                report.update(compare(src, dst))
        (out_dir / "aller_retour.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n",
                                                   encoding="utf-8")
    print(json.dumps({k: report.get(k) for k in ("resolve", "render_status", "error", "deleted", "color_tags_kept",
                                                 "format_kept", "frames_kept", "range_kept", "extremes_identical",
                                                 "timecode_kept", "bitstream_identical", "psnr_db",
                                                 "psnr_range_hypotheses_db")},
                     ensure_ascii=False, indent=1))
    print(f"Rapport : {out_dir / 'aller_retour.json'}")
    if not args.keep and not report["deleted"]:
        print(f"ATTENTION : le projet Resolve « {name} » n'a pas pu être supprimé : supprimez-le à la main.",
              file=sys.stderr)
    keys = ("color_tags_kept", "format_kept", "range_kept", "frames_kept", "timecode_kept")
    return 0 if failure is None and all(report.get(k) for k in keys) and (args.keep or report["deleted"]) else 1


if __name__ == "__main__":
    sys.exit(main())
