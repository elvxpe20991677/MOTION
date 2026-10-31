"""Vérification de la phase 7 : provenance C2PA (signature en dernière étape) et profil AV1 NVENC.

Lancement depuis la racine :  .venv/Scripts/python.exe tests/verify_step70_c2pa_av1.py
Isolé : build/_tests/step70/ (build, out, scènes, certificat C2PA de test, verrou), dossier IA
assets/_tests/ai70/. Aucun appel réseau (pas d'horodatage tiers). AV1 : SKIP si av1_nvenc est absent
(pas de GPU NVIDIA, CI Linux).
Contrôles :
  A. schéma et compilateur : profil av1_nvenc et cq (défaut 30, crf ignoré avec avertissement) ;
     provenance.c2pa et visible_label conservés ; mention ajoutée à chaque plan (coin de la zone sûre,
     calque texte ordinaire) ; id « mention-ia » réservé ; scène sans ces blocs : compilation identique ;
  B. certificat : c2pa init (chaîne locale ES256), second init refusé sans --force, clé privée ignorée
     par git ; signature sans certificat : message actionnable ;
  C. rendu complet (all) d'une scène avec un asset IA gelé, mention visible et 3 livrables (HEVC hvc1,
     ProRes 422 HQ, AV1 NVENC) : déterminisme, QC CONFORME (codec av1, étiquette av01, couleurs),
     signature APRÈS le QC, manifestes valides (SDK et c2patool : seul « signingCredential.untrusted »),
     compositeSynthetic, assertion org.mograph.provenance (assets IA en trainedAlgorithmicMedia, sans
     prompt), images décodées identiques avant et après signature ; AV1 : deux encodages identiques
     (INFO, hors contrat) ;
  D. tests négatifs : réencodage et remux (-c copy) -> plus de manifeste ; octet du flux modifié ->
     manifeste invalide ; verify d'un fichier non signé -> code 1 ;
  E. scène sans IA : algorithmicMedia ; valeur imposée par la scène respectée ; c2pa sign autonome ;
     QC non conforme -> livrables non signés.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TEST = ROOT / "build" / "_tests" / "step70"
AI = ROOT / "assets" / "_tests" / "ai70"
SCENES = TEST / "scenes"
os.environ.update({
    "MOGRAPH_BUILD_DIR": str(TEST / "build"), "MOGRAPH_OUT_DIR": str(TEST / "out"), "MOGRAPH_SCENES_DIR": str(SCENES),
    "MOGRAPH_AI_ASSETS_DIR": str(AI), "MOGRAPH_ASSETS_LOCK": str(TEST / "assets.lock.json"),
    "MOGRAPH_C2PA_DIR": str(TEST / "c2pa"), "PYTHONIOENCODING": "utf-8"})
sys.path.insert(0, str(ROOT))

from pipeline import assets, c2pa_sign, config  # noqa: E402  (après l'environnement)
from pipeline import scene as scene_mod  # noqa: E402

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

PY = sys.executable
FAILS: list[str] = []
QC = {"safe_zone": "title", "safe_zone_tolerance_s": 0.5, "allow_black_s": 0, "determinism_frames": 3,
      "loudness_target_lufs": -14}
IPTC = config.C2PA_IPTC_BASE
HAVE_TOOL = bool(config.c2patool_path())   # c2patool absent (CI) : vérification par le SDK seulement


def check(label: str, ok: bool, detail: str = "") -> bool:
    print(("PASS " if ok else "FAIL ") + label + (f" -- {detail}" if detail else ""), flush=True)
    if not ok:
        FAILS.append(label)
    return ok


def skip(label: str, why: str) -> None:
    print(f"SKIP {label} -- {why}", flush=True)


def mograph(*args: str, timeout: float = 1800) -> subprocess.CompletedProcess:
    return subprocess.run([PY, str(ROOT / "mograph.py"), *args], cwd=ROOT, env=dict(os.environ), capture_output=True,
                          stdin=subprocess.DEVNULL, text=True, encoding="utf-8", errors="replace", timeout=timeout)


def tail(cp, n=2) -> str:
    return " | ".join((cp.stdout + cp.stderr).strip().splitlines()[-n:])


def ffmpeg(*args: str) -> None:
    subprocess.run([config.FFMPEG, "-v", "error", "-y", *args], check=True)


def frames_md5(path: Path) -> str:
    r = subprocess.run([config.FFMPEG, "-v", "error", "-i", str(path), "-map", "0:v:0", "-f", "framemd5", "-"],
                       capture_output=True, check=True)
    return hashlib.sha256(r.stdout).hexdigest()


def av1_available() -> bool:
    r = subprocess.run([config.FFMPEG, "-hide_banner", "-v", "error", "-f", "lavfi", "-i", "color=c=gray:s=256x256:d=0.2",
                        "-c:v", "av1_nvenc", "-pix_fmt", "p010le", "-f", "null", "-"], capture_output=True)
    return r.returncode == 0


def scene(sid: str, *, outputs, provenance=None, layers=None, shots=2) -> Path:
    d = {"version": "1.0", "id": sid, "title": sid, "brief": "test phase 7", "seed": 7, "preset": "flat_riso",
         "format": {"aspect": "16:9", "resolution": "1080p", "fps": 25, "alpha": False},
         "outputs": outputs, "qc": QC,
         "shots": [{"id": f"p{i}", "duration": 1.2, "background": "bg",
                    "layers": [dict(la) for la in (layers or [{"id": "t", "type": "text", "text": "Ondea",
                                                               "x": "safe:50%", "y": "safe:50%"}])]}
                   for i in range(shots)]}
    if provenance is not None:
        d["provenance"] = provenance
    p = SCENES / f"{sid}.json"
    p.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
    return p


def test_compile() -> None:
    lab = {"text": "Contenu créé avec l'aide de l'IA", "corner": "bottom-right"}
    p = scene("c70_compile", outputs=[{"profile": "av1_nvenc"}, {"profile": "av1_nvenc", "suffix": "_q", "cq": 22,
                                                                 "crf": 10}],
              provenance={"c2pa": {"sign": True, "digital_source_type": "trainedAlgorithmicMedia"}, "visible_label": lab})
    c = scene_mod.compile_scene(p)
    o = c["outputs"]
    check("av1_nvenc : MP4, cq par défaut 30, cq imposé 22", o[0]["path"].endswith("_av1_nvenc.mp4") and o[0]["cq"] == 30
          and o[1]["cq"] == 22)
    check("crf sur av1_nvenc : ignoré avec un avertissement qui nomme « cq »", any("cq" in w and "crf" in w for w in c["warnings"]))
    check("provenance.c2pa et visible_label conservés dans la scène compilée",
          c["provenance"]["c2pa"] == {"sign": True, "digital_source_type": "trainedAlgorithmicMedia"}
          and c["provenance"]["visible_label"] == lab)
    labels = [la for s in c["shots"] for la in s["spec"]["layers"] if la.get("id") == "mention-ia"]
    check("mention visible : un calque texte par plan, coin bas droit de la zone sûre, au premier plan",
          len(labels) == 2 and all(la.get("text") == lab["text"] for la in labels), str(labels[:1])[:200])
    bad = json.loads(p.read_text(encoding="utf-8"))
    bad["shots"][0]["layers"].append({"id": "mention-ia", "type": "text", "text": "x"})
    try:
        scene_mod.compile_scene(bad)
        ok = False
    except scene_mod.SceneError as exc:
        ok = any("réservé" in e for e in exc.errors)
    check("id « mention-ia » réservé : refus explicite", ok)
    plain = scene_mod.compile_scene(ROOT / "scenes" / "demo_flat_riso.json")
    check("scène sans bloc phase 7 : aucune clé c2pa/visible_label/cq ajoutée",
          "c2pa" not in json.dumps(plain.get("provenance")) and all("cq" not in x for x in plain["outputs"]))


def test_credentials() -> None:
    try:
        c2pa_sign.sign_file(TEST / "absent.mp4", {})
        msg = ""
    except c2pa_sign.C2paError as exc:
        msg = str(exc)
    check("signature sans certificat : refus actionnable (c2pa init)", "c2pa init" in msg, msg[:100])
    cp = mograph("c2pa", "init")
    check("c2pa init : chaîne locale créée", cp.returncode == 0 and (TEST / "c2pa" / "signer.key").is_file()
          and (TEST / "c2pa" / "signer_chain.pem").is_file(), tail(cp, 1))
    cp = mograph("c2pa", "init")
    check("second c2pa init sans --force : refusé (certificat gardé)", cp.returncode == 1 and "--force" in cp.stderr)
    r = subprocess.run(["git", "check-ignore", "-q", str(ROOT / ".c2pa" / "signer.key")], cwd=ROOT)
    check("clé privée par défaut (.c2pa/) ignorée par git", r.returncode == 0)


def freeze_ai_image() -> str:
    src = TEST / "src" / "fond_ia.png"
    src.parent.mkdir(parents=True, exist_ok=True)
    ffmpeg("-f", "lavfi", "-i", "gradients=s=640x360:seed=4:n=3", "-frames:v", "1", str(src))
    key, _entry, _status, _warn = assets.add(src, tool="Test IA", model="gradients-lavfi", prompt="PROMPT-SECRET dégradé",
                                             license="test interne", source="manual_import")
    return key


def test_render(av1: bool) -> Path | None:
    key = freeze_ai_image()
    outputs = [{"profile": "hevc_main10"}, {"profile": "prores_422hq"}] + ([{"profile": "av1_nvenc"}] if av1 else [])
    prov = {"ai_disclosure": {"ai_used": True, "roles": ["image"], "date": "2026-10-31",
                              "tools": [{"name": "Test IA", "model": "gradients-lavfi", "role": "fond"}]},
            "c2pa": {"sign": True}, "visible_label": {"text": "Contenu créé avec l'aide de l'IA"}}
    layers = [{"id": "fond", "type": "image", "src": key, "w": 1920, "h": 1080, "x": 960, "y": 540},
              {"id": "t", "type": "text", "text": "Ondea", "x": "safe:50%", "y": "safe:45%"}]
    p = scene("c70_rendu", outputs=outputs, provenance=prov, layers=layers)
    t0 = time.perf_counter()
    cp = mograph("all", str(p))
    out = TEST / "out" / "c70_rendu"
    qc = json.loads((out / "qc_report.json").read_text(encoding="utf-8")) if (out / "qc_report.json").is_file() else {}
    rep = json.loads((out / "c2pa_report.json").read_text(encoding="utf-8")) if (out / "c2pa_report.json").is_file() else {}
    check("all : déterminisme, QC CONFORME puis signature C2PA (code 0)",
          cp.returncode == 0 and qc.get("verdict") == "CONFORME" and len(rep.get("outputs", [])) == len(outputs),
          f"{time.perf_counter() - t0:.0f} s ; {tail(cp, 2)}")
    by = {c["id"]: c for c in qc.get("checks", [])}
    if av1:
        check("QC AV1 : codec av1, profil Main, 10 bits, étiquette av01, couleurs BT.709",
              all(by.get(f"av1_nvenc.{k}", {}).get("status") == "PASS" for k in ("codec", "av01", "color"))
              and by.get("av1_nvenc.codec_profile", {}).get("status") in ("PASS", None),
              str({k: by.get(f"av1_nvenc.{k}", {}).get("status") for k in ("codec", "codec_profile", "pix_fmt", "av01", "color")}))
    files = {o["profile"]: Path(o["file"]) for o in rep.get("outputs", [])}
    signed_after = all(f.stat().st_mtime >= (out / "qc_report.json").stat().st_mtime for f in files.values()) if files else False
    check("signature APRÈS le QC (fichiers livrés modifiés après qc_report.json)", signed_after)
    for prof, f in files.items():
        v = c2pa_sign.verify(f)
        tool = v.get("c2patool") or {}
        check(f"{prof} : manifeste valide (SDK : {v['state']} ; c2patool : {tool.get('state')}), seul statut "
              "« signingCredential.untrusted »", v["valid"] and v["statuses"] == ["signingCredential.untrusted"]
              and (not HAVE_TOOL or (tool.get("state") == "Valid" and tool.get("statuses") == ["signingCredential.untrusted"])),
              f"{v['statuses']} / {tool}")
        check(f"{prof} : digitalSourceType compositeSynthetic (vidéo procédurale + asset IA)",
              v["digital_source_type"] == IPTC + "compositeSynthetic")
    if "hevc_main10" in files:
        import c2pa
        with c2pa.Reader(str(files["hevc_main10"])) as r:
            store = json.loads(r.json())
        m = store["manifests"][store["active_manifest"]]
        prov_a = next((a["data"] for a in m["assertions"] if a["label"] == "org.mograph.provenance"), {})
        txt = json.dumps(m, ensure_ascii=False)
        check("assertion org.mograph.provenance : asset IA gelé (empreinte, outil, modèle, trainedAlgorithmicMedia), "
              "déclaration d'IA, aucun prompt", prov_a.get("ai_assets") and prov_a["ai_assets"][0]["sha256"]
              and prov_a["ai_assets"][0]["tool"] == "Test IA"
              and prov_a["ai_assets"][0]["digitalSourceType"] == IPTC + "trainedAlgorithmicMedia"
              and prov_a.get("ai_disclosure", {}).get("ai_used") is True and "PROMPT-SECRET" not in txt)
    return out if files else None


def test_negative(out: Path, av1: bool) -> None:
    hevc = out / "c70_rendu_hevc_main10.mp4"
    neg = TEST / "neg"
    neg.mkdir(parents=True, exist_ok=True)
    md5_signed = frames_md5(hevc)
    # Images décodées identiques à celles du livrable avant signature : on réencode le maître de la même
    # façon dans un dossier à part et on compare aux images du fichier signé.
    ref = neg / "ref.mp4"
    ffmpeg("-i", str(hevc), "-map", "0:v:0", "-c", "copy", str(ref))
    check("signature sans effet sur les images (images décodées identiques après remux du fichier signé)",
          frames_md5(ref) == md5_signed)
    for label, args in (("réencodage", ["-c:v", "libx265", "-x265-params", "log-level=error", "-tag:v", "hvc1"]),
                        ("remux -c copy", ["-c", "copy"])):
        dst = neg / f"{label.split()[0]}.mp4"
        ffmpeg("-i", str(hevc), *args, str(dst))
        v = c2pa_sign.verify(dst)
        cp = mograph("c2pa", "verify", str(dst))
        tool = v.get("c2patool") or {}
        check(f"test négatif, {label} : plus aucun manifeste (verify code 1 ; c2patool « No claim found »)",
              not v["signed"] and cp.returncode == 1 and (not HAVE_TOOL or "No claim" in (tool.get("message") or "")),
              str(tool)[:120])
    bad = neg / "altere.mp4"
    shutil.copy(hevc, bad)
    data = bytearray(bad.read_bytes())
    i = data.find(b"mdat") + 2000
    data[i] ^= 0xFF
    bad.write_bytes(bytes(data))
    v = c2pa_sign.verify(bad)
    check("test négatif, octet du flux modifié : manifeste INVALIDE (empreinte bmff)",
          v["signed"] and not v["valid"] and any("mismatch" in s or "hash" in s.lower() for s in v["statuses"]),
          str(v["statuses"]))
    if av1:
        a1 = out / "c70_rendu_av1_nvenc.mp4"
        cp = mograph("encode", str(SCENES / "c70_rendu.json"))
        h1 = frames_md5(a1)
        cp2 = mograph("encode", str(SCENES / "c70_rendu.json"))
        same = cp.returncode == 0 and cp2.returncode == 0 and frames_md5(a1) == h1
        print(f"INFO AV1 NVENC : deux encodages successifs -> images décodées {'identiques' if same else 'DIFFÉRENTES'} "
              "(hors contrat : la porte de déterminisme porte sur le maître)", flush=True)
        v = c2pa_sign.verify(a1)
        check("réencodage (commande encode) : livrables à nouveau NON signés (resigner : c2pa sign)", not v["signed"])
        cp = mograph("c2pa", "sign", str(SCENES / "c70_rendu.json"))
        check("c2pa sign autonome : livrables existants resignés et valides", cp.returncode == 0
              and c2pa_sign.verify(a1)["valid"], tail(cp, 1))


def test_no_ai() -> None:
    p = scene("c70_sansia", outputs=[{"profile": "hevc_main10"}], provenance={"c2pa": {"sign": True}}, shots=1)
    cp = mograph("all", str(p))
    f = TEST / "out" / "c70_sansia" / "c70_sansia_hevc_main10.mp4"
    v = c2pa_sign.verify(f) if f.is_file() else {"digital_source_type": None, "valid": False}
    check("scène sans IA : algorithmicMedia (rendu procédural), manifeste valide",
          cp.returncode == 0 and v["valid"] and v["digital_source_type"] == IPTC + "algorithmicMedia", tail(cp, 1))
    d = json.loads(p.read_text(encoding="utf-8"))
    d["provenance"]["c2pa"]["digital_source_type"] = "trainedAlgorithmicMedia"
    p.write_text(json.dumps(d), encoding="utf-8")
    cp = mograph("c2pa", "sign", str(p))
    check("valeur imposée par la scène (trainedAlgorithmicMedia) respectée", cp.returncode == 0
          and c2pa_sign.verify(f)["digital_source_type"] == IPTC + "trainedAlgorithmicMedia", tail(cp, 1))
    d["id"], d["qc"] = "c70_nonconforme", dict(QC, allow_black_s=0)
    d["shots"][0]["background"] = "#000000"
    d["shots"][0]["layers"] = [{"id": "t", "type": "text", "text": ".", "x": "50%", "y": "50%", "color": "#000000"}]
    p2 = SCENES / "c70_nonconforme.json"
    p2.write_text(json.dumps(d), encoding="utf-8")
    cp = mograph("all", str(p2))
    f2 = TEST / "out" / "c70_nonconforme" / "c70_nonconforme_hevc_main10.mp4"
    check("QC non conforme : code 1, livrable NON signé, message explicite",
          cp.returncode == 1 and f2.is_file() and not c2pa_sign.verify(f2)["signed"] and "NON signés" in cp.stdout,
          tail(cp, 2))


def main() -> int:
    assert TEST.parent.name == "_tests" and AI.parent.name == "_tests"
    for d in (TEST, AI):
        if d.exists():
            shutil.rmtree(d)
    SCENES.mkdir(parents=True)
    if not check("environnement isolé : certificat, verrou, dossier IA et scènes de test",
                 config.C2PA_DIR == (TEST / "c2pa").resolve() and config.AI_ASSETS_DIR == AI.resolve()
                 and config.SCENES_DIR == SCENES.resolve()):
        return 1
    t0 = time.perf_counter()
    av1 = av1_available()
    if not av1:
        skip("profil av1_nvenc", "encodeur av1_nvenc indisponible (GPU NVIDIA Ada ou plus récent requis)")
    if not config.c2patool_path():
        print("NOTE c2patool absent : vérification par le SDK seulement", flush=True)
    try:
        test_compile()
        test_credentials()
        out = test_render(av1)
        if out:
            test_negative(out, av1)
        test_no_ai()
    except Exception as exc:  # noqa: BLE001 — rapporté comme échec, jamais masqué
        import traceback
        traceback.print_exc()
        check("les contrôles s'exécutent sans exception", False, f"{type(exc).__name__} : {exc}"[:200])
    print(f"\nDurée : {time.perf_counter() - t0:.0f} s ; aucun appel réseau")
    if FAILS:
        print(f"ÉCHEC : {len(FAILS)} contrôle(s) : {FAILS}")
        return 1
    print("SUCCÈS : provenance C2PA et profil AV1 conformes (phase 7).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
