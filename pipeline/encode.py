"""Encodage FFmpeg de la séquence maître (SPEC étape 15, CONTRACT §7).

Livrables : HEVC Main10 (tag hvc1, AAC 320k), ProRes 422 HQ et ProRes 4444 (PCM 24 bits, timecode).
Conversion couleur commune config.ZSCALE (RVB plein -> YUV limité Rec.709, diffusion d'erreur).
Chaque commande est aussi écrite dans out/<scene>/encode_commands.sh (bash, depuis la racine).
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import time
from decimal import Decimal
from fractions import Fraction
from pathlib import Path

from pipeline import config

# Arguments communs : -y écrase le fichier partiel d'un essai précédent, -nostdin empêche FFmpeg
# de consommer l'entrée standard (et de se bloquer) quand il est lancé par un script.
_COMMON = ["-y", "-hide_banner", "-nostdin"]
# Étiquettes couleur Rec.709 plage limitée : identiques pour tous les profils (QC : bt709 ×3 + tv).
_COLOR_TAGS = ["-color_primaries", "bt709", "-color_trc", "bt709", "-colorspace", "bt709", "-color_range", "tv"]
_PIX_FMT = {"hevc_main10": "yuv420p10le", "prores_422hq": "yuv422p10le", "prores_4444": "yuva444p10le"}

# Le plan ALPHA ne passe jamais par les convertisseurs de FFmpeg 6.1 (Ubuntu 24.04), mesurés
# inexacts : swscale rgba -> gbrap décale l'alpha ≥ 128 de +1, rgba -> rgba64le donne 65532 pour
# 255 ; vf_zscale traite l'alpha comme une plage limitée (255 -> 1020 en 10 bits). Seul le chemin
# COULEUR de zscale est exact (v × 257, v × 1023 / 255 arrondi). On extrait donc l'alpha tel quel
# (extractplanes, lecture directe du PNG), on le recopie dans R = G = B (mergeplanes) et on le
# convertit par ce chemin couleur. Exact au bit près sur 6.1.1 comme sur 8.x, en 8 et 16 bits.
# Formats par profondeur du maître : sortie du décodeur PNG, plans RVB natifs, alpha recopié
# (même boutisme que la sortie d'extractplanes, exigé par mergeplanes).
_MASTER_FMT = {8: ("rgba", "gbrp", "gbrp"), 16: ("rgba64be", "gbrp16le", "gbrp16be")}
_FULL = "zscale=rangein=full:range=full"
# Recopie des maps par défaut de mergeplanes (tous les plans <- plan 0 de l'entrée 0) : l'option
# « mapping » est dépréciée depuis FFmpeg 6.
_ALPHA_AS_RGB = "format={packed},extractplanes=a,mergeplanes=format={alpha},format={planar}"


def _alpha_as_rgb(depth: int) -> str:
    packed, planar, alpha = _MASTER_FMT[depth]
    return _ALPHA_AS_RGB.format(packed=packed, planar=planar, alpha=alpha)


def _flatten_graph(depth: int, tail: str) -> str:
    """Aplatissement sur noir (livrable opaque d'une scène à alpha) puis `tail` (ZSCALE, format).

    Équivalent exact du préfixe du contrat « format=rgba64le,premultiply=inplace=1 » : RVB et
    alpha étendus en 16 bits (v × 257, identité sur un maître 16 bits), puis premultiply à deux
    entrées (RVB × alpha / 65535, plan 0 du 2e flux = alpha).
    """
    planar = _MASTER_FMT[depth][1]
    return (f"split[c][a];[c]format={planar},{_FULL},format=gbrp16le[c16];"
            f"[a]{_alpha_as_rgb(depth)},{_FULL},format=gbrp16le[a16];"
            f"[c16][a16]premultiply=inplace=0,{tail}")


def _alpha_graph(depth: int) -> str:
    """ProRes 4444 : couleur par config.ZSCALE, alpha plein 10 bits exact, fusionnés en yuva444p10le.

    Alpha dans R = G = B puis matrice 709 en plage pleine : Y = alpha (Kr + Kg + Kb = 1).
    mergeplanes impose le même format à ses deux entrées : yuv444p10le.
    """
    planar = _MASTER_FMT[depth][1]
    return (f"split[c][a];[c]format={planar},{config.ZSCALE},format=yuv444p10le[yuv];"
            f"[a]{_alpha_as_rgb(depth)},{_FULL}:matrix=709,format=yuv444p10le[a10];"
            f"[yuv][a10]mergeplanes=map0s=0:map0p=0:map1s=0:map1p=1:map2s=0:map2p=2:"
            f"map3s=1:map3p=0:format=yuva444p10le")


class EncodeError(RuntimeError):
    """Erreur d'encodage destinée à l'utilisateur (message en français, actionnable)."""


# ---------------------------------------------------------------------------
# Outils
# ---------------------------------------------------------------------------

def duration_text(frames: int, fps: int) -> str:
    """D = frames / fps en texte décimal.

    Exact quand la fraction a un développement décimal fini (tous les cas à 25/50 i/s et les
    multiples de 3 images à 30/60 i/s...). Sinon, 6 décimales : FFmpeg lit les durées en
    microsecondes (AV_TIME_BASE), puis apad/atrim arrondissent au plus proche échantillon ; comme
    48000/fps est entier pour tous les fps autorisés, le nombre d'échantillons reste exact.
    """
    q = Fraction(frames, fps)
    den = q.denominator
    for p in (2, 5):
        while den % p == 0:
            den //= p
    if den == 1:
        text = format(Decimal(q.numerator) / Decimal(q.denominator), "f")
        if "." in text:
            text = text.rstrip("0").rstrip(".")
        return text
    return f"{float(q):.6f}"


def _rel(path: Path | str) -> str:
    """Chemin relatif à la racine en séparateurs « / » (lisible par FFmpeg ET par bash) ; chemin
    absolu « C:/... » si le fichier est sur un autre volume (build/ ou out/ déplacés)."""
    p = Path(path).resolve()
    try:
        return p.relative_to(config.ROOT).as_posix()
    except ValueError:
        return p.as_posix()


def _partial_path(output: Path) -> Path:
    """Fichier d'écriture temporaire : même extension (FFmpeg en déduit le conteneur), renommé
    en fin d'encodage (écriture atomique : un livrable n'existe jamais à moitié écrit)."""
    return output.with_name(f"{output.stem}.partial{output.suffix}")


def _video_codec_args(profile: str, fps: int, crf: int) -> list[str]:
    if profile == "hevc_main10":
        # keyint/min-keyint en valeurs numériques (x265 n'évalue pas « 2*fps ») : une image clé
        # toutes les 2 s au plus, pas plus d'une par seconde ; aq-mode 3 protège les aplats sombres
        # du banding, no-sao évite le lissage des textures fines (grain).
        x265 = (f"colorprim=bt709:transfer=bt709:colormatrix=bt709:range=limited:aq-mode=3:no-sao=1:"
                f"keyint={2 * fps}:min-keyint={fps}")
        return ["-c:v", "libx265", "-preset", "slow", "-crf", str(crf), "-pix_fmt", "yuv420p10le",
                "-profile:v", "main10", "-x265-params", x265, "-tag:v", "hvc1", *_COLOR_TAGS,
                # faststart : index en tête (lecture web immédiate) ; write_colr : atome colr.
                "-movflags", "+faststart+write_colr"]
    if profile == "prores_422hq":
        # prores_ks profil 3 = 422 HQ ; vendor apl0 : fichier reconnu comme ProRes Apple par les NLE.
        return ["-c:v", "prores_ks", "-profile:v", "3", "-vendor", "apl0", "-pix_fmt", "yuv422p10le",
                *_COLOR_TAGS, "-movflags", "+write_colr", "-timecode", config.TIMECODE_START]
    if profile == "prores_4444":
        # Profil 4 = 4444 ; alpha_bits 16 : alpha codé sans perte de précision.
        return ["-c:v", "prores_ks", "-profile:v", "4", "-vendor", "apl0", "-pix_fmt", "yuva444p10le",
                "-alpha_bits", "16", *_COLOR_TAGS, "-movflags", "+write_colr",
                "-timecode", config.TIMECODE_START]
    raise EncodeError(f"Profil de sortie inconnu « {profile} » : profils disponibles "
                      f"{', '.join(config.PROFILES)}.")


def _audio_args(profile: str, duration: str) -> list[str]:
    # apad complète le silence, atrim coupe l'excédent : la piste dure exactement D (= vidéo).
    af = ["-af", f"apad=whole_dur={duration},atrim=end={duration}"]
    if profile == "hevc_main10":
        return af + ["-c:a", "aac", "-b:a", "320k", "-ar", str(config.AUDIO_RATE)]
    # ProRes : PCM 24 bits, le format de travail des logiciels de montage (aucune perte).
    return af + ["-c:a", "pcm_s24le", "-ar", str(config.AUDIO_RATE)]


# ---------------------------------------------------------------------------
# API publique
# ---------------------------------------------------------------------------

def build_commands(compiled: dict) -> list[dict]:
    """Une commande FFmpeg par livrable : [{profile, output, argv}] (CONTRACT §4).

    argv[0] = config.FFMPEG ; argv écrit directement dans `output` (dernier argument). Les chemins
    d'argv sont RELATIFS À LA RACINE du projet (CONTRACT §7, identiques à encode_commands.sh) :
    lancer argv avec cwd = config.ROOT. encode_scene exécute la même commande en remplaçant
    seulement la sortie par un fichier « .partial » renommé après succès (écriture atomique).
    """
    scene_id = compiled["scene_id"]
    fps = int(compiled["format"]["fps"])
    frames = int(compiled["frames"])
    # Profondeur UNIQUE du maître (compose.py) : elle fixe les formats des chaînes alpha.
    depth = int(compiled["depth"])
    if depth not in _MASTER_FMT:
        raise EncodeError(f"Profondeur maître invalide ({depth}) : 8 ou 16 attendus (recompilez la scène).")
    duration = duration_text(frames, fps)
    pattern = config.master_dir(scene_id) / config.FRAME_PATTERN_FFMPEG
    audio = compiled.get("audio")
    audio_src = Path(audio["src"]) if audio and audio.get("src") else None

    commands = []
    for out in compiled["outputs"]:
        profile = out["profile"]
        if profile not in config.PROFILES:
            raise EncodeError(f"Profil de sortie inconnu « {profile} » : profils disponibles "
                              f"{', '.join(config.PROFILES)}.")
        output = Path(out["path"])
        crf = out.get("crf")
        crf = config.HEVC_CRF_DEFAULT if crf is None else int(crf)
        vf = f"{config.ZSCALE},format={_PIX_FMT[profile]}"
        if out.get("alpha_flatten"):
            vf = _flatten_graph(depth, vf)
        elif profile == "prores_4444":
            vf = _alpha_graph(depth)
        argv = [config.FFMPEG, *_COMMON,
                # -framerate (option d'entrée image2) fixe la cadence exacte, sans conversion ;
                # -start_number 0 : la séquence maître commence à 000000.png.
                "-framerate", str(fps), "-start_number", "0", "-i", _rel(pattern)]
        if audio_src is not None:
            argv += ["-i", _rel(audio_src)]
        argv += ["-map", "0:v:0"]
        if audio_src is not None:
            argv += ["-map", "1:a:0"]
        # -frames:v N : nombre d'images exact même si des images en trop traînaient dans le dossier.
        argv += ["-frames:v", str(frames), "-vf", vf]
        argv += _video_codec_args(profile, fps, crf)
        if audio_src is not None:
            argv += _audio_args(profile, duration)
        argv.append(_rel(output))
        commands.append({"profile": profile, "output": str(output), "argv": argv})
    return commands


def _executed_argv(cmd: dict) -> list[str]:
    """argv réellement lancé : celui de build_commands, sortie remplacée par le fichier partiel.

    Une seule fonction sert à l'exécution ET au script : encode_commands.sh contient donc
    exactement les commandes exécutées.
    """
    argv = list(cmd["argv"])
    if argv[-1] != _rel(cmd["output"]):
        raise EncodeError(f"Commande interne incohérente pour {cmd['profile']} : le dernier argument "
                          f"({argv[-1]}) n'est pas la sortie {cmd['output']} (erreur de programmation).")
    argv[-1] = _rel(_partial_path(Path(cmd["output"])))
    return argv


def _script_text(compiled: dict, commands: list[dict], script_dir: Path) -> str:
    """Script bash qui rejoue exactement les encodages depuis la racine du projet."""
    try:
        # Chemin relatif script -> racine (« ../.. » pour out/<scene>/) : juste aussi quand
        # MOGRAPH_OUT_DIR déplace out/ ailleurs sous la racine.
        to_root = Path(os.path.relpath(config.ROOT, script_dir)).as_posix()
        cd = f'cd "$(dirname "$SCRIPT_PATH")/{to_root}"'
    except ValueError:
        # out/ sur un autre volume que la racine : chemin absolu (Git Bash accepte « C:/... »).
        cd = f"cd {shlex.quote(config.ROOT.as_posix())}"
    lines = [
        "#!/usr/bin/env bash",
        f"# Commandes d'encodage exactes de la scène « {compiled['scene_id']} » (pipeline/encode.py).",
        "# À lancer depuis n'importe où (bash out/<scene>/encode_commands.sh) : le script se place",
        "# à la racine du projet, tous les chemins sont relatifs à cette racine.",
        f"# Images : {compiled['frames']} à {compiled['format']['fps']} i/s ; "
        f"durée {duration_text(int(compiled['frames']), int(compiled['format']['fps']))} s.",
        "set -euo pipefail",
        # Sous Windows, bash peut recevoir « C:\\...\\encode_commands.sh » : dirname ne comprend que
        # les « / », d'où la conversion des antislashs avant de remonter à la racine.
        'SCRIPT_PATH="${0//\\\\//}"',
        cd,
        '# FFmpeg du PATH, ou celui imposé par MOGRAPH_FFMPEG (même règle que pipeline/config.py).',
        'FFMPEG="${MOGRAPH_FFMPEG:-ffmpeg}"',
        "",
    ]
    for cmd in commands:
        out_rel = _rel(cmd["output"])
        part_rel = _rel(_partial_path(Path(cmd["output"])))
        lines.append(f"# {cmd['profile']} -> {out_rel}")
        # Citations POSIX de chaque argument (shlex.join) : la chaîne de filtres reste un seul argument.
        lines.append(f'"$FFMPEG" {shlex.join(_executed_argv(cmd)[1:])}')
        lines.append(f"mv -f {shlex.quote(part_rel)} {shlex.quote(out_rel)}")
        lines.append("")
    return "\n".join(lines)


def _check_master(compiled: dict) -> None:
    scene_id = compiled["scene_id"]
    master = config.master_dir(scene_id)
    mpath = master / "manifest.json"
    hint = "lancez d'abord la composition (python mograph.py all <scène>, étape séquence maître)"
    if not mpath.is_file():
        raise EncodeError(f"Séquence maître absente ({mpath}) : {hint}.")
    try:
        man = json.loads(mpath.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise EncodeError(f"Manifeste maître illisible ({mpath} : {exc}) : {hint}.") from exc
    frames = int(compiled["frames"])
    alpha = bool(compiled["format"].get("alpha", False))
    # Tout ce qui change le fichier livré est comparé : un maître opaque 8 bits encodé pour une
    # scène alpha 16 bits (ou celui d'une autre scène) donnerait un livrable faux sans erreur.
    want = {"scene_id": scene_id, "frames": frames, "fps": compiled["format"]["fps"],
            "width": compiled["output"]["width"], "height": compiled["output"]["height"],
            "depth": int(compiled["depth"]), "alpha": alpha}
    have = {k: man.get(k) for k in want}
    if have != want:
        def fmt(d: dict) -> str:
            return (f"scène {d['scene_id']}, {d['frames']} images à {d['fps']} i/s en {d['width']}x{d['height']}, "
                    f"{d['depth']} bits, alpha={d['alpha']}")
        diff = ", ".join(k for k in want if have[k] != want[k])
        raise EncodeError(f"Séquence maître périmée ({diff} différent) : le maître contient {fmt(have)} ; la scène "
                          f"attend {fmt(want)} : {hint}.")
    missing = [g for g in range(frames) if not (master / config.FRAME_PATTERN.format(g)).is_file()]
    if missing:
        raise EncodeError(f"{len(missing)} image(s) maître manquante(s) (première : {missing[0]}) dans {master} : {hint}.")


def encode_scene(compiled: dict, *, log=print) -> list[Path]:
    """Encode tous les livrables ; écrit out/<scene>/encode_commands.sh ; renvoie les chemins produits."""
    scene_id = compiled["scene_id"]
    _check_master(compiled)
    audio = compiled.get("audio")
    if audio and audio.get("src") and not Path(audio["src"]).is_file():
        raise EncodeError(f"Piste audio absente : {audio['src']}. Générez-la (audio.synth + "
                          "pipeline.audio.ensure_scene_audio) ou placez le fichier avant l'encodage.")
    commands = build_commands(compiled)

    out_dir = config.scene_out_dir(scene_id)
    out_dir.mkdir(parents=True, exist_ok=True)
    # Le script est écrit AVANT l'encodage : en cas d'échec, les commandes exactes sont déjà là.
    script = out_dir / "encode_commands.sh"
    tmp = script.with_name(script.name + ".tmp")
    # newline="\n" : fins de ligne LF, sinon bash sous Windows lirait « \r » dans les arguments.
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(_script_text(compiled, commands, out_dir))
    os.replace(tmp, script)
    os.chmod(script, 0o755)

    logs = config.scene_build_dir(scene_id) / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    produced: list[Path] = []
    for cmd in commands:
        output = Path(cmd["output"])
        partial = _partial_path(output)
        argv = _executed_argv(cmd)
        output.parent.mkdir(parents=True, exist_ok=True)
        _remove_partial(partial, output)
        log_path = logs / f"encode_{output.stem}.log"
        log(f"[encode] {cmd['profile']} -> {output}")
        t0 = time.perf_counter()
        with open(log_path, "wb") as log_fh:
            log_fh.write((shlex.join(argv) + "\n\n").encode("utf-8"))
            log_fh.flush()
            # Journal FFmpeg dans un fichier plutôt qu'en mémoire : un encodage 4K long produit
            # beaucoup de lignes de progression.
            try:
                proc = subprocess.Popen(argv, cwd=config.ROOT, stdin=subprocess.DEVNULL,
                                        stdout=subprocess.DEVNULL, stderr=log_fh)
            except OSError as exc:
                # FileNotFoundError / PermissionError de CreateProcess : exécutable absent ou bloqué.
                raise EncodeError(
                    f"FFmpeg introuvable ou impossible à lancer ({config.FFMPEG} : {exc}). Installez FFmpeg "
                    "8.x build complet (libx265, prores_ks, zscale) dans le PATH, ou définissez la variable "
                    "d'environnement MOGRAPH_FFMPEG avec le chemin de ffmpeg.exe, puis relancez l'encodage."
                ) from exc
            try:
                code = proc.wait()
            finally:
                if proc.poll() is None:
                    # Interruption (Ctrl+C...) : on arrête CE processus (PID connu), rien d'autre.
                    proc.kill()
                    proc.wait()
        if code != 0:
            tail = "\n".join(log_path.read_text(encoding="utf-8", errors="replace").strip().splitlines()[-15:])
            _remove_partial(partial, output, quiet=True)
            raise EncodeError(
                f"Échec de FFmpeg (code {code}) pour le livrable {cmd['profile']} ({output.name}).\n"
                f"Journal complet : {log_path}\nCommande rejouable : bash {script}\nFin du journal :\n{tail}"
            )
        if not partial.is_file() or partial.stat().st_size == 0:
            _remove_partial(partial, output, quiet=True)
            raise EncodeError(f"FFmpeg n'a produit aucun fichier pour {cmd['profile']} : voir {log_path}.")
        try:
            os.replace(partial, output)
        except OSError as exc:
            # Sous Windows, un fichier ouvert par un lecteur ou par Resolve ne peut pas être remplacé.
            _remove_partial(partial, output, quiet=True)
            raise EncodeError(
                f"Impossible de remplacer {output} ({exc}). Le livrable est probablement ouvert dans un autre "
                "programme (lecteur vidéo, DaVinci Resolve, aperçu de l'explorateur) : fermez-le puis relancez "
                f"l'encodage (python mograph.py encode {scene_id})."
            ) from exc
        log(f"[encode] {output.name} : {output.stat().st_size / 1e6:.1f} Mo en {time.perf_counter() - t0:.1f} s")
        produced.append(output)
    return produced


def _remove_partial(partial: Path, output: Path, *, quiet: bool = False) -> None:
    """Supprime le fichier partiel d'un essai précédent (ou de l'essai en échec).

    quiet=True : nettoyage après une autre erreur, un échec ici ne doit pas masquer la cause réelle.
    """
    try:
        partial.unlink(missing_ok=True)
    except OSError as exc:
        if quiet:
            return
        raise EncodeError(
            f"Impossible de supprimer le fichier partiel {partial} ({exc}) avant d'encoder {output.name} : "
            "fermez le programme qui l'utilise puis relancez l'encodage."
        ) from exc
