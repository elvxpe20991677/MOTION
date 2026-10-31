"""Provenance C2PA des livrables (phase 7, CONTRACT §11.8) : signature en TOUTE DERNIÈRE étape.

Un manifeste C2PA vit dans le conteneur (boîte JUMBF « uuid » d'un MP4/MOV) et lie, par une empreinte
(c2pa.hash.bmff), le contenu du fichier à ses assertions. Tout réencodage, et même un simple remux
(« ffmpeg -c copy »), produit un nouveau fichier SANS manifeste : la signature vient donc après
l'encodage et après le QC, sur le fichier livré tel quel ; on ne modifie plus le fichier ensuite.

Certificat : chaîne LOCALE (autorité « mograph CA locale » + certificat de signature ES256, EKU
emailProtection), créée par « python mograph.py c2pa init » dans config.C2PA_DIR (jamais versionnée).
Elle n'est reconnue par aucune liste de confiance : les lecteurs affichent « signingCredential.untrusted »
(signature intègre, signataire non reconnu). Certificat de signature valable 10 ans.

Horodatage RFC 3161 ACTIF par défaut (config.C2PA_TSA_URL, DigiCert ; « MOGRAPH_C2PA_TSA=off » pour
signer hors ligne) : le manifeste porte une date de signature certifiée par un tiers. Autorité injoignable
-> signature SANS horodatage, signalée (journal et rapport « timestamped »: false), jamais silencieuse.
Mesuré (SDK 0.38.0, c2patool 0.28.4, réglages par défaut) : un manifeste horodaté devient lui aussi
« Invalid » (signingCredential.expired) après l'expiration du certificat, l'autorité d'horodatage n'étant
pas dans une liste de confiance configurée ; l'horodatage sert de preuve de date pour un validateur qui
fait confiance à DigiCert.

Préconditions de signature (jamais de manifeste sur un fichier que mograph n'a pas produit et contrôlé) :
fiche d'encodage out/<scène>/encode_record.json dont l'empreinte de scène est celle de la scène
actuelle, chaque livrable identique à l'octet près à celui encodé (ou à sa dernière version signée,
pour resigner), et qc_report.json CONFORME écrit APRÈS cet encodage.

Manifeste : c2pa.actions (c2pa.created, digitalSourceType IPTC de la vidéo, logiciel mograph) et une
assertion « org.mograph.provenance » (scène, auteurs, déclaration d'usage de l'IA, assets IA gelés avec
empreinte, outil, modèle, licence et digitalSourceType trainedAlgorithmicMedia chacun ; jamais les prompts).
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import subprocess
from pathlib import Path

from pipeline import config

C2PA_CONTRACT = "mograph-c2pa/1"
CA_NAME = "mograph CA locale"
SIGNER_NAME = "mograph signature"
MIME = {".mp4": "video/mp4", ".mov": "video/quicktime"}
# Seul statut toléré : le certificat local n'est dans aucune liste de confiance (attendu).
UNTRUSTED_OK = {"signingCredential.untrusted"}


class C2paError(RuntimeError):
    """Signature ou vérification impossible (message en français, actionnable)."""


def _paths() -> dict[str, Path]:
    d = config.C2PA_DIR
    return {"dir": d, "ca": d / "ca.pem", "chain": d / "signer_chain.pem", "key": d / "signer.key"}


def init_credentials(*, force: bool = False, log=print) -> dict[str, Path]:
    """Crée l'autorité locale et le certificat de signature (ES256). Refus si déjà présents, sauf force."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

    p = _paths()
    if p["key"].exists() and not force:
        raise C2paError(f"un certificat de signature existe déjà ({p['chain']}) : gardez-le (les fichiers déjà signés "
                        "restent liés à lui) ou relancez avec --force pour le remplacer.")
    now = _dt.datetime.now(_dt.timezone.utc)
    ca_key = ec.generate_private_key(ec.SECP256R1())
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, CA_NAME),
                         x509.NameAttribute(NameOID.ORGANIZATION_NAME, "mograph")])
    ca = (x509.CertificateBuilder().subject_name(ca_name).issuer_name(ca_name).public_key(ca_key.public_key())
          .serial_number(x509.random_serial_number()).not_valid_before(now - _dt.timedelta(days=1))
          .not_valid_after(now + _dt.timedelta(days=3650))
          .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
          .add_extension(x509.KeyUsage(False, False, False, False, False, True, True, False, False), critical=True)
          .add_extension(x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()), critical=False)
          .sign(ca_key, hashes.SHA256()))
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, SIGNER_NAME),
                      x509.NameAttribute(NameOID.ORGANIZATION_NAME, "mograph")])
    leaf = (x509.CertificateBuilder().subject_name(name).issuer_name(ca_name).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - _dt.timedelta(days=1))
            .not_valid_after(now + _dt.timedelta(days=3650))
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(x509.KeyUsage(True, False, False, False, False, False, False, False, False), critical=True)
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.EMAIL_PROTECTION]), critical=False)
            .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False)
            .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
            .sign(ca_key, hashes.SHA256()))
    p["dir"].mkdir(parents=True, exist_ok=True)
    pem = serialization.Encoding.PEM
    p["ca"].write_bytes(ca.public_bytes(pem))
    p["chain"].write_bytes(leaf.public_bytes(pem) + ca.public_bytes(pem))
    # La clé de l'autorité n'est pas conservée : elle ne servira plus (nouvelle chaîne = c2pa init --force).
    tmp = p["key"].with_suffix(".key.tmp")
    tmp.write_bytes(key.private_bytes(pem, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    os.replace(tmp, p["key"])
    log(f"[c2pa] certificat de signature créé : {p['chain']} (clé privée {p['key']}, à ne jamais partager ni versionner)")
    return p


def _signer(tsa: str | None = None):
    import c2pa

    p = _paths()
    if not (p["chain"].is_file() and p["key"].is_file()):
        raise C2paError(f"certificat de signature absent ({p['dir']}) : lancez une fois « python mograph.py c2pa init ».")
    info = c2pa.C2paSignerInfo(alg=c2pa.C2paSigningAlg.ES256, sign_cert=p["chain"].read_bytes(),
                               private_key=p["key"].read_bytes(), ta_url=tsa.encode("utf-8") if tsa else None)
    return c2pa.Signer.from_info(info)


def digital_source_type(compiled: dict) -> str:
    """Type de source IPTC de la vidéo : valeur imposée par la scène, sinon compositeSynthetic (« mix ou
    composite de plusieurs éléments, dont au moins un d'IA générative ») si la vidéo contient un asset IA
    ou déclare l'usage d'une IA, sinon algorithmicMedia (« créé purement par un algorithme, sans données
    d'apprentissage » : le rendu procédural de mograph)."""
    prov = compiled.get("provenance") or {}
    forced = (prov.get("c2pa") or {}).get("digital_source_type")
    if forced:
        return config.C2PA_IPTC_BASE + forced
    ai = any(a.get("ai") for a in compiled.get("assets") or []) or bool((prov.get("ai_disclosure") or {}).get("ai_used"))
    return config.C2PA_IPTC_BASE + ("compositeSynthetic" if ai else "algorithmicMedia")


def manifest_for(compiled: dict, output: dict) -> dict:
    """Définition du manifeste d'UN livrable (déterministe pour une scène et un verrou donnés)."""
    from pipeline import assets as assets_mod

    path = Path(output["path"])
    prov = compiled.get("provenance") or {}
    lock = assets_mod.load_lock()["assets"]
    ai_assets = []
    for a in compiled.get("assets") or []:
        if not a.get("ai"):
            continue
        pv = (lock.get(a["path"]) or {}).get("provenance") or {}
        ai_assets.append({"path": a["path"], "sha256": a.get("sha256"), "kinds": a.get("kinds"),
                          "tool": pv.get("tool"), "model": pv.get("model"), "date": pv.get("date"),
                          "source": pv.get("source"), "license": pv.get("license"),
                          "digitalSourceType": config.C2PA_IPTC_BASE + "trainedAlgorithmicMedia"})
    return {
        "claim_generator_info": [{"name": "mograph", "version": C2PA_CONTRACT}],
        "title": path.name,
        "format": MIME[path.suffix.lower()],
        "assertions": [
            {"label": "c2pa.actions", "data": {"actions": [
                {"action": "c2pa.created", "digitalSourceType": digital_source_type(compiled),
                 "softwareAgent": {"name": "mograph"}}]}},
            {"label": "org.mograph.provenance", "data": {
                "contract": C2PA_CONTRACT, "scene_id": compiled["scene_id"], "title": compiled.get("title"),
                "profile": output["profile"], "authors": prov.get("authors") or [],
                "ai_disclosure": prov.get("ai_disclosure"), "ai_assets": ai_assets}},
        ],
    }


def _signed_copy(path: Path, manifest: dict, log=print) -> tuple[Path, str | None]:
    """Copie signée de `path` (même dossier, même extension) ; le livrable n'est pas encore touché.

    Renvoie (copie, None) si horodatée (ou horodatage désactivé), (copie, raison) si l'autorité
    d'horodatage n'a pas répondu et que la signature a été faite sans."""
    import c2pa

    path = Path(path)
    if path.suffix.lower() not in MIME:
        raise C2paError(f"{path.name} : conteneur non pris en charge pour la signature (MP4 ou MOV).")
    tmp = path.with_name(f"{path.stem}.c2pa-{os.getpid()}{path.suffix.lower()}")

    def attempt(tsa):
        tmp.unlink(missing_ok=True)
        with _signer(tsa) as signer, c2pa.Builder(json.dumps(manifest, ensure_ascii=False)) as builder:
            builder.sign_file(str(path), str(tmp), signer)

    note = None
    try:
        try:
            attempt(config.C2PA_TSA_URL)
        except (c2pa.C2paError, OSError) as exc:
            if not config.C2PA_TSA_URL:
                raise
            note = f"autorité d'horodatage {config.C2PA_TSA_URL} injoignable ({str(exc)[:160]})"
            log(f"[c2pa] AVERTISSEMENT {path.name} : {note} ; signature SANS horodatage.")
            attempt(None)
    except (c2pa.C2paError, OSError) as exc:
        tmp.unlink(missing_ok=True)
        raise C2paError(f"signature de {path.name} impossible : {exc}") from None
    return tmp, note


def sign_file(path: Path, manifest: dict) -> None:
    """Signe `path` sur place (copie signée puis remplacement atomique)."""
    tmp, _note = _signed_copy(path, manifest)
    try:
        os.replace(tmp, path)
    except OSError as exc:
        tmp.unlink(missing_ok=True)
        raise C2paError(f"remplacement de {Path(path).name} impossible ({exc}) : fichier ouvert dans un autre "
                        "programme ? Fermez-le puis relancez.") from None


def verify(path: Path) -> dict:
    """Lecture et validation du manifeste (SDK C2PA ; c2patool en plus s'il est installé).

    Renvoie {file, signed, valid, state, statuses, digital_source_type, signer, c2patool}. valid = manifeste
    présent, état « Valid »/« Trusted » et aucun statut d'échec hormis le certificat non reconnu (attendu)."""
    import c2pa

    path = Path(path)
    out = {"file": str(path), "signed": False, "valid": False, "state": None, "statuses": [],
           "digital_source_type": None, "signer": None, "signed_at": None, "c2patool": None}
    try:
        with c2pa.Reader(str(path)) as r:
            store = json.loads(r.json())
    except Exception as exc:  # noqa: BLE001 — fichier sans manifeste, illisible ou corrompu
        out["error"] = str(exc)[:300]
        return _with_c2patool(out, path)
    out["signed"] = True
    out["state"] = store.get("validation_state")
    out["statuses"] = [s.get("code") for s in store.get("validation_status") or []]
    m = (store.get("manifests") or {}).get(store.get("active_manifest")) or {}
    for a in m.get("assertions") or []:
        if a.get("label", "").startswith("c2pa.actions"):
            for act in (a.get("data") or {}).get("actions") or []:
                if act.get("action") == "c2pa.created":
                    out["digital_source_type"] = act.get("digitalSourceType")
    out["signer"] = (m.get("signature_info") or {}).get("common_name")
    # Date certifiée par l'autorité d'horodatage (absente d'une signature non horodatée).
    out["signed_at"] = (m.get("signature_info") or {}).get("time")
    out["valid"] = out["state"] in ("Valid", "Trusted") and set(out["statuses"]) <= UNTRUSTED_OK
    return _with_c2patool(out, path)


def _with_c2patool(out: dict, path: Path) -> dict:
    tool = config.c2patool_path()
    if not tool:
        out["c2patool"] = {"available": False}
        return out
    try:
        r = subprocess.run([tool, str(path)], capture_output=True, text=True, encoding="utf-8", errors="replace",
                           timeout=120, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError) as exc:
        out["c2patool"] = {"available": False, "error": str(exc)[:200]}
        return out
    res = {"available": True, "exit": r.returncode}
    try:
        doc = json.loads(r.stdout)
        res["state"] = doc.get("validation_state")
        res["statuses"] = [s.get("code") for s in doc.get("validation_status") or []]
    except ValueError:
        res["message"] = (r.stderr or r.stdout).strip()[-200:]
    out["c2patool"] = res
    return out


def check_signable(compiled: dict) -> None:
    """Refuse (C2paError) de signer des livrables que mograph n'a pas produits et contrôlés pour la scène
    ACTUELLE : fiche d'encodage, empreinte de scène, empreinte de chaque fichier, QC CONFORME postérieur."""
    from pipeline import encode

    out_dir = config.scene_out_dir(compiled["scene_id"])
    rec_path, qc_path = out_dir / encode.ENCODE_RECORD, out_dir / "qc_report.json"
    redo = f"relancez « python mograph.py all {compiled['scene_id']} »"
    try:
        rec = json.loads(rec_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise C2paError(f"aucune fiche d'encodage ({rec_path.name}) : les livrables n'ont pas été produits par mograph "
                        f"pour cette scène ; {redo}.") from None
    if rec.get("scene_fingerprint") != encode.compiled_fingerprint(compiled):
        raise C2paError(f"la scène a changé depuis l'encodage des livrables (contenu, assets ou réglages) : le manifeste "
                        f"les décrirait faussement ; {redo}.")
    try:
        prev = json.loads((out_dir / "c2pa_report.json").read_text(encoding="utf-8"))
        signed = {Path(o["file"]).name: o.get("sha256") for o in prev.get("outputs") or []}
    except (OSError, ValueError):
        signed = {}
    for o in compiled["outputs"]:
        path = Path(o["path"])
        if not path.is_file():
            raise C2paError(f"livrable absent : {path} ; {redo}.")
        sha = encode.file_sha256(path)
        if sha not in (rec.get("outputs", {}).get(path.name), signed.get(path.name)):
            raise C2paError(f"{path.name} n'est pas le fichier encodé par mograph (empreinte différente : remplacé, "
                            f"remuxé ou modifié) ; {redo}.")
    try:
        qc = json.loads(qc_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise C2paError(f"aucun rapport QC ({qc_path.name}) : la signature exige un QC CONFORME ; {redo}.") from None
    if qc.get("verdict") != "CONFORME" or qc.get("scene_id") != compiled["scene_id"] \
            or qc_path.stat().st_mtime < rec_path.stat().st_mtime:
        raise C2paError(f"QC non conforme ou antérieur au dernier encodage ({qc_path.name} : {qc.get('verdict')}) : "
                        f"lancez « python mograph.py qc {compiled['scene_id']} » puis resignez, ou {redo}.")


def sign_outputs(compiled: dict, *, log=print) -> dict:
    """Signe TOUS les livrables de la scène ou aucun : préconditions (check_signable), copies signées et
    vérifiées, puis remplacements ; écrit out/<scène>/c2pa_report.json (+ .md) avec l'état réel.

    Lève C2paError si une précondition, une signature ou une vérification échoue (aucun livrable modifié
    dans ces cas)."""
    check_signable(compiled)
    out_dir = config.scene_out_dir(compiled["scene_id"])
    report = {"contract": C2PA_CONTRACT, "scene_id": compiled["scene_id"],
              "date_utc": _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
              "digital_source_type": digital_source_type(compiled), "timestamp_authority": config.C2PA_TSA_URL,
              "outputs": []}
    failures, temps = [], []
    try:
        for o in compiled["outputs"]:
            path = Path(o["path"])
            tmp, note = _signed_copy(path, manifest_for(compiled, o), log=log)
            temps.append((tmp, path, o))
            v = verify(tmp)
            tool = v.get("c2patool") or {}
            tool_ok = not tool.get("available") or (tool.get("exit") == 0
                                                     and set(tool.get("statuses") or []) <= UNTRUSTED_OK)
            if not (v["valid"] and tool_ok):
                failures.append(f"{path.name} ({v['state']}, {', '.join(v['statuses'])})")
            report["outputs"].append({"profile": o["profile"], **v, "file": str(path),
                                      "timestamped": bool(v.get("signed_at")), "timestamp_note": note})
        if failures:
            raise C2paError(f"vérification C2PA en échec : {', '.join(failures)} ; aucun livrable n'a été modifié.")
        for (tmp, path, o), entry in zip(temps, report["outputs"]):
            try:
                os.replace(tmp, path)
            except OSError as exc:
                raise C2paError(f"remplacement de {path.name} impossible ({exc}) : fichier ouvert dans un autre "
                                "programme ? Les livrables déjà remplacés sont signés ; fermez-le puis relancez "
                                "« c2pa sign ».") from None
            entry["sha256"] = encode_sha(path)
            log(f"[c2pa] {path.name} : signé, {entry['state']}"
                + (f" ({', '.join(entry['statuses'])})" if entry["statuses"] else "")
                + (f" ; c2patool : {entry['c2patool'].get('state')}" if (entry.get('c2patool') or {}).get("available")
                   else " ; c2patool absent"))
    finally:
        for tmp, _path, _o in temps:
            Path(tmp).unlink(missing_ok=True)
        report["outputs"] = [e for e in report["outputs"] if e.get("sha256")]
        _write_report(out_dir, compiled, report)
    return report


def encode_sha(path: Path) -> str:
    from pipeline import encode

    return encode.file_sha256(path)


def _write_report(out_dir: Path, compiled: dict, report: dict) -> None:
    """Rapport FIDÈLE : seuls les livrables effectivement signés y figurent (avec leur empreinte) ; aucun
    rapport si rien n'a été signé (un rapport précédent est retiré)."""
    if not report["outputs"]:
        for name in ("c2pa_report.json", "c2pa_report.md"):
            (out_dir / name).unlink(missing_ok=True)
        return
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "c2pa_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    lines = [f"# Provenance C2PA — {compiled['scene_id']}", "",
             f"Type de source (IPTC) : `{report['digital_source_type']}`. Certificat local, non reconnu par les "
             "listes de confiance (`signingCredential.untrusted` attendu). Tout réencodage ou remux supprime le "
             "manifeste : ne plus modifier ces fichiers.", ""]
    for o in report["outputs"]:
        lines.append(f"- {Path(o['file']).name} : {o['state']} ({', '.join(o['statuses']) or 'aucun statut'})"
                     + (f", c2patool {o['c2patool'].get('state')}" if (o.get('c2patool') or {}).get('available') else ""))
    (out_dir / "c2pa_report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
