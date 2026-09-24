"""Piste audio de test déterministe (FFmpeg aevalsrc) et mesures audio.

- synth_beat         : grosse caisse sur chaque temps (accent sur le 1er temps de la mesure),
                       charleston sur les contretemps ; WAV 48 kHz pcm_s24le bit à bit reproductible,
                       durée exacte à l'échantillon, loudness calée sur une cible EBU R128.
- measure_loudness   : ebur128 (true peak) -> {integrated_lufs, true_peak_dbtp, lra}.
- detect_onsets      : attaques par enveloppe d'énergie (numpy), lecture WAV 16/24/32 bits.
- ensure_scene_audio : génère la piste d'une scène compilée si elle est absente et que audio.synth
                       est défini.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import subprocess
from fractions import Fraction
from pathlib import Path

import numpy as np

from pipeline import config

# ---------------------------------------------------------------------------
# Constantes de la piste de test (elles ne touchent pas l'image, seulement la piste générée)
# ---------------------------------------------------------------------------

# Toute la piste est à 48 kHz : c'est la fréquence exigée par les livrables (config.AUDIO_RATE).
SAMPLE_RATE: int = config.AUDIO_RATE

# Plafond de crête vraie de la piste générée : 1 dB de marge sous le seuil QC (-1 dBTP), car
# l'encodage AAC fait remonter les crêtes intersamples.
TRUE_PEAK_CEILING_DBTP: float = -2.0

# Marge de prédiction : ebur128 n'affiche qu'un dixième de dB, on garde 0,2 dB de sécurité
# entre la crête prédite (mesure + gain) et le plafond.
TRUE_PEAK_PREDICTION_MARGIN_DB: float = 0.2

# Tolérance de loudness de la piste générée (plus stricte que la QC à ±1 LU).
LOUDNESS_TOLERANCE_LU: float = 0.5

# Densités de synthèse essayées dans l'ordre : (saturation tanh, tenue de l'enveloppe en fraction
# de temps). Plus la densité monte, plus le rapport crête/loudness baisse (mesuré : ~10 dB -> ~5 dB),
# ce qui permet d'atteindre des cibles fortes sans dépasser le plafond de crête et sans limiteur.
SYNTH_LEVELS: tuple[tuple[float, float], ...] = (
    (1.5, 0.12),
    (3.0, 0.20),
    (6.0, 0.30),
    (12.0, 0.40),
)

# Au-delà (cibles de -7,5 à -5 LUFS, admises par le schéma), densifier ne suffit plus : un
# rapport crête/loudness < 3 dB exige une grosse caisse plus tenue ET un limiteur. Dernier recours
# seulement (les cibles usuelles gardent des attaques non limitées) : niveaux (saturation, tenue)
# essayés dans l'ordre, puis gain avant limiteur cherché par dichotomie déterministe.
LIMITED_LEVELS: tuple[tuple[float, float], ...] = (
    (12.0, 0.55),
    (12.0, 0.65),
)
# Plafond d'échantillon du limiteur : -3,5 dBFS donne une crête VRAIE mesurée de -2,7/-2,8 dBTP
# (le limiteur travaille sur les échantillons, les crêtes intersamples remontent de ~0,7 dB).
LIMITER_CEILING_DBFS: float = -3.5
# Attaque 5 ms (anticipation) / relâchement 50 ms : pas d'écrêtage audible sur une grosse caisse à
# 70-180 Hz ; latency=1 compense le retard d'anticipation (attaques restées à 1,3 ms près, mesuré).
LIMITER_ATTACK_MS: float = 5.0
LIMITER_RELEASE_MS: float = 50.0
# Recherche du gain d'entrée du limiteur : au plus 12 dB de dépassement, 12 pas de dichotomie
# (résolution 0,003 dB, bien sous le dixième de dB affiché par ebur128).
LIMITER_SEARCH_RANGE_DB: float = 12.0
LIMITER_SEARCH_STEPS: int = 12
LIMITER_AIM_LU: float = 0.05

# Plus petite piste dont la loudness intégrée est mesurable : BS.1770 intègre des blocs de 400 ms.
MIN_MEASURABLE_S: float = 0.4
# ebur128 affiche -70 LUFS (seuil de porte absolu) quand aucun bloc n'est mesurable.
UNMEASURABLE_LUFS: float = -69.9

# Timbre de la grosse caisse : balayage 180 -> 70 Hz (70 Hz reste peu atténué par la pondération K).
KICK_F_START_HZ: float = 180.0
KICK_F_END_HZ: float = 70.0
KICK_SWEEP_RATE: float = 25.0          # 1/s : vitesse de descente de la hauteur
KICK_ACCENT: float = 1.0               # amplitude du 1er temps de la mesure
KICK_NORMAL: float = 0.7               # amplitude des autres temps (-3 dB)
# L'enveloppe tombe à -60 dB à 80 % du temps : l'énergie repasse sous le seuil de détection avant
# le temps suivant quel que soit le tempo (décroissance proportionnelle à la période).
KICK_DECAY_END_FRACTION: float = 0.8
HAT_AMPLITUDE: float = 0.35
HAT_DECAY_END_FRACTION: float = 0.3    # charleston éteint (-60 dB) à 30 % du temps après le contretemps

# Détection d'attaques.
ONSET_WINDOW_S: float = 0.020          # 20 ms > période de 70 Hz : enveloppe sans ondulation
ONSET_HOP_S: float = 0.001             # pas de 1 ms, puis affinage à l'échantillon
ONSET_HYSTERESIS_DB: float = 6.0       # réarmement 6 dB sous le seuil : pas de double détection

GENERATOR_ID: str = "mograph-beat/1"   # change si l'algorithme de synthèse change (régénération)

_FFMPEG_BASE: list[str] = [config.FFMPEG, "-nostdin", "-hide_banner", "-nostats"]


class AudioError(RuntimeError):
    """Erreur audio destinée à l'utilisateur (message en français, actionnable)."""


# ---------------------------------------------------------------------------
# Outils internes
# ---------------------------------------------------------------------------

def _num(x: float) -> str:
    """Nombre pour une expression FFmpeg : repr() est l'écriture décimale la plus courte qui
    redonne exactement le même double, donc le texte (et la piste) est reproductible."""
    return repr(float(x))


def _run_ffmpeg(argv: list[str], what: str) -> str:
    """Lance FFmpeg, renvoie stderr ; lève AudioError avec la fin du journal en cas d'échec."""
    try:
        # subprocess.run attend la fin du processus et le tue si une exception interrompt l'attente.
        proc = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              stdin=subprocess.DEVNULL)
    except OSError as exc:
        # FileNotFoundError / PermissionError de CreateProcess : exécutable absent ou bloqué.
        raise AudioError(
            f"FFmpeg introuvable ou impossible à lancer ({config.FFMPEG} : {exc}) pendant : {what}. Installez "
            "FFmpeg (build complet avec ebur128) dans le PATH ou définissez MOGRAPH_FFMPEG."
        ) from exc
    err = proc.stderr.decode("utf-8", errors="replace")
    if proc.returncode != 0:
        tail = "\n".join(err.strip().splitlines()[-15:])
        raise AudioError(
            f"Échec de FFmpeg (code {proc.returncode}) pendant : {what}.\n"
            f"Commande : {' '.join(argv)}\nFin du journal :\n{tail}"
        )
    return err


_SUMMARY_RE = {
    "integrated_lufs": re.compile(r"^\s*I:\s+(-?(?:\d+(?:\.\d+)?|inf))\s+LUFS", re.M),
    "lra": re.compile(r"^\s*LRA:\s+(-?(?:\d+(?:\.\d+)?|inf))\s+LU\b", re.M),
    "true_peak_dbtp": re.compile(r"^\s*Peak:\s+(-?(?:\d+(?:\.\d+)?|inf))\s+dBFS", re.M),
}


def _parse_ebur128(stderr: str, what: str) -> dict:
    """Lit le résumé final du filtre ebur128 (dernier bloc « Summary: » du journal)."""
    idx = stderr.rfind("Summary:")
    if idx < 0:
        raise AudioError(f"Résumé ebur128 absent du journal FFmpeg ({what}) : vérifiez que le fichier "
                         "contient bien une piste audio décodable.")
    block = stderr[idx:]
    out: dict[str, float] = {}
    for key, rx in _SUMMARY_RE.items():
        m = rx.search(block)
        if not m:
            raise AudioError(f"Valeur « {key} » introuvable dans le résumé ebur128 ({what}).")
        out[key] = float(m.group(1))   # float('-inf') accepte le « -inf » d'un silence
    return {"integrated_lufs": out["integrated_lufs"], "true_peak_dbtp": out["true_peak_dbtp"],
            "lra": out["lra"]}


def _beat_expression(bpm: float, beats_per_bar: int, offset: float, drive: float,
                     hold_frac: float) -> str:
    """Expression aevalsrc d'UN canal.

    Le temps est calculé à partir de l'index d'échantillon n (entier exact en double) plutôt que
    de t = n/s arrondi : les temps tombent exactement sur les échantillons attendus.
    Variables st/ld : 0 u (échantillons depuis l'offset), 1 index du temps, 2 temps écoulé dans le
    temps (s), 3 accent, 4 grosse caisse, 5 temps écoulé depuis le contretemps, 6 bruit courant,
    7 bruit précédent, 8 bruit dérivé ; 9 = état du générateur random() (LCG déterministe de FFmpeg).
    """
    period = 60.0 / bpm
    period_samples = 60.0 * SAMPLE_RATE / bpm
    hold = hold_frac * period
    # -60 dB (facteur 1000) atteints à KICK_DECAY_END_FRACTION du temps, tenue comprise.
    kick_decay = math.log(1000.0) / (KICK_DECAY_END_FRACTION * period - hold)
    hat_decay = math.log(1000.0) / (HAT_DECAY_END_FRACTION * period)
    df = KICK_F_START_HZ - KICK_F_END_HZ
    # Phase intégrée exacte du balayage exponentiel f(t) = f1 + (f0 - f1) e^(-k t).
    phase = (f"2*PI*({_num(KICK_F_END_HZ)}*ld(2)+{_num(df)}*(1-exp(-ld(2)*{_num(KICK_SWEEP_RATE)}))"
             f"/{_num(KICK_SWEEP_RATE)})")
    return (
        f"st(0,n-{_num(offset * SAMPLE_RATE)});"
        f"st(1,floor(ld(0)/{_num(period_samples)}));"
        f"st(2,(ld(0)-ld(1)*{_num(period_samples)})/{_num(SAMPLE_RATE)});"
        f"st(3,if(eq(mod(ld(1),{int(beats_per_bar)}),0),{_num(KICK_ACCENT)},{_num(KICK_NORMAL)}));"
        # tanh(drive·sin)/tanh(drive) : saturation douce normalisée (crête 1) qui densifie le son.
        f"st(4,gte(ld(0),0)*ld(3)*if(lt(ld(2),{_num(hold)}),1,exp(-(ld(2)-{_num(hold)})*{_num(kick_decay)}))"
        f"*tanh({_num(drive)}*sin({phase}))/tanh({_num(drive)}));"
        f"st(5,ld(2)-{_num(period / 2.0)});"
        # Bruit blanc dérivé (x[n] - x[n-1]) : passe-haut simple qui donne un timbre de charleston.
        f"st(6,random(9));st(8,ld(6)-ld(7));st(7,ld(6));"
        f"ld(4)+gte(ld(0),0)*gte(ld(5),0)*{_num(HAT_AMPLITUDE)}*exp(-ld(5)*{_num(hat_decay)})*ld(8)"
    )


def _source_graph(bpm: float, beats_per_bar: int, offset: float, n_samples: int,
                  drive: float, hold_frac: float) -> str:
    """Chaîne lavfi : aevalsrc stéréo (même signal sur les deux canaux) tronquée à n échantillons.

    L'expression est ENTRE APOSTROPHES : sinon ses virgules seraient lues comme des séparateurs
    de filtres (et ses points-virgules comme des séparateurs de chaînes).
    """
    expr = _beat_expression(bpm, beats_per_bar, offset, drive, hold_frac)
    # Source un peu plus longue que nécessaire, puis atrim=end_sample : durée exacte à l'échantillon.
    src_dur = (n_samples + 4096) / SAMPLE_RATE
    return (f"aevalsrc=exprs='{expr}|{expr}':s={SAMPLE_RATE}:c=stereo:d={_num(src_dur)},"
            f"atrim=end_sample={n_samples}")


def _measure_graph(graph: str, what: str) -> dict:
    """Loudness d'une chaîne lavfi sans rien écrire (sortie null, calcul en double, pas d'écrêtage)."""
    err = _run_ffmpeg(_FFMPEG_BASE + ["-f", "lavfi", "-i", f"{graph},ebur128=peak=true:framelog=quiet",
                                      "-f", "null", "-"], what)
    return _parse_ebur128(err, what)


def _measure_file_chain(path: Path, chain: str, what: str) -> dict:
    """Loudness d'un fichier après une chaîne de filtres (sortie null)."""
    err = _run_ffmpeg(_FFMPEG_BASE + ["-i", str(path), "-af", f"{chain},ebur128=peak=true:framelog=quiet",
                                      "-f", "null", "-"], what)
    return _parse_ebur128(err, what)


def _check_measurable(m: dict, duration: float, offset: float) -> None:
    """Refus actionnable d'une piste dont la loudness intégrée n'est pas mesurable (-70 LUFS)."""
    if not math.isfinite(m["integrated_lufs"]) or m["integrated_lufs"] <= UNMEASURABLE_LUFS:
        raise AudioError(
            f"Loudness de la piste synthétique non mesurable ({m['integrated_lufs']} LUFS) : la piste "
            f"(durée {duration} s, premier temps à {offset} s) contient moins de 400 ms de son. Vérifiez que "
            "audio.offset est bien inférieur à la durée de la scène."
        )


def _limiter_chain(gain_db: float) -> str:
    """Gain d'entrée puis limiteur à paramètres FIXES (déterministe : même entrée -> mêmes bits)."""
    return (f"volume=volume={gain_db:.3f}dB:precision=double,"
            f"alimiter=limit={10.0 ** (LIMITER_CEILING_DBFS / 20.0):.6f}:attack={_num(LIMITER_ATTACK_MS)}:"
            f"release={_num(LIMITER_RELEASE_MS)}:level=0:asc=0:latency=1")


def _search_limited(source: Path, target_lufs: float, what: str) -> tuple[float, dict, float]:
    """Gain d'entrée du limiteur qui amène la loudness à target_lufs : (gain, mesure, loudness max).

    Dichotomie déterministe sur [g0, g0 + LIMITER_SEARCH_RANGE_DB], g0 = gain qui suffirait sans
    limiteur : la loudness après limiteur croît avec le gain d'entrée (en saturant), et la suite des
    essais ne dépend que de l'entrée, donc deux générations choisissent le même gain.
    """
    unity = _measure_file_chain(source, "anull", what)
    lo = round(target_lufs - unity["integrated_lufs"], 3)
    hi = lo + LIMITER_SEARCH_RANGE_DB
    m_hi = _measure_file_chain(source, _limiter_chain(hi), what)
    best = (abs(m_hi["integrated_lufs"] - target_lufs), hi, m_hi)
    if m_hi["integrated_lufs"] < target_lufs - LIMITER_AIM_LU:
        # Même 12 dB au-dessus, la cible n'est pas atteinte : ce niveau de synthèse sature.
        return hi, m_hi, m_hi["integrated_lufs"]
    for _ in range(LIMITER_SEARCH_STEPS):
        mid = round((lo + hi) / 2.0, 3)
        m = _measure_file_chain(source, _limiter_chain(mid), what)
        # Égalité départagée par le gain le plus faible : moins de limitation, choix reproductible.
        best = min(best, (abs(m["integrated_lufs"] - target_lufs), mid, m), key=lambda b: (b[0], b[1]))
        if abs(m["integrated_lufs"] - target_lufs) <= LIMITER_AIM_LU:
            break
        if m["integrated_lufs"] < target_lufs:
            lo = mid
        else:
            hi = mid
    return best[1], best[2], m_hi["integrated_lufs"]


def _write_wav(input_args: list[str], filter_args: list[str], out_path: Path) -> None:
    """Rendu final WAV 48 kHz pcm_s24le sans métadonnées variables, écriture atomique."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_path.with_name(out_path.name + ".tmp")
    argv = _FFMPEG_BASE + ["-y", *input_args, *filter_args,
                           # -map_metadata -1 et les drapeaux bitexact suppriment la chaîne de version
                           # « Lavf... » (bloc LIST/INFO) : deux générations donnent le même md5 et ne
                           # dépendent pas de la build.
                           "-map_metadata", "-1", "-bitexact", "-fflags", "+bitexact", "-flags:a", "+bitexact",
                           "-c:a", "pcm_s24le", "-ar", str(SAMPLE_RATE),
                           # -f wav explicite : l'extension .tmp ne permet pas à FFmpeg de deviner le format.
                           "-f", "wav", str(tmp)]
    try:
        _run_ffmpeg(argv, f"génération de {out_path.name}")
        # Écriture atomique : le fichier final n'existe jamais à moitié écrit.
        os.replace(tmp, out_path)
    finally:
        if tmp.exists():
            tmp.unlink()


# ---------------------------------------------------------------------------
# API publique
# ---------------------------------------------------------------------------

def synth_beat(out_path, bpm: float, duration: float, *, beats_per_bar: int = 4,
               offset: float = 0.0, target_lufs: float = -14.0) -> Path:
    """Génère la piste de test : WAV 48 kHz stéréo pcm_s24le, bit à bit reproductible.

    Durée = round(duration × 48000) échantillons exactement. Loudness intégrée = target_lufs ± 0,5 LU,
    crête vraie ≤ -2 dBTP (marge pour l'AAC). Méthode déterministe : mesure ebur128 de la synthèse,
    gain en dB arrondi au centième, rendu final, re-mesure ; si la crête prédite dépasse, la synthèse
    est densifiée (niveaux SYNTH_LEVELS) plutôt que limitée, ce qui garde des attaques nettes. Cibles
    très fortes (au-delà d'environ -7,5 LUFS) : grosse caisse plus tenue + limiteur à paramètres
    fixes (LIMITED_LEVELS), gain d'entrée trouvé par dichotomie, puis la même re-vérification.
    """
    out_path = Path(out_path)
    if not (isinstance(bpm, (int, float)) and 0 < bpm <= 400):
        raise AudioError(f"Tempo invalide ({bpm!r}) : audio.bpm doit être compris entre 0 (exclu) et 400.")
    if not (isinstance(duration, (int, float)) and duration > 0):
        raise AudioError(f"Durée invalide ({duration!r}) : la piste doit durer plus de 0 s.")
    if int(beats_per_bar) < 1:
        raise AudioError(f"beats_per_bar invalide ({beats_per_bar!r}) : entier ≥ 1 attendu.")
    if offset < 0:
        raise AudioError(f"offset invalide ({offset!r}) : le premier temps ne peut pas précéder 0 s.")
    if not -70.0 < target_lufs < 0.0:
        raise AudioError(f"Cible de loudness invalide ({target_lufs!r} LUFS) : valeur attendue entre "
                         "-70 et 0 LUFS (usuellement -14 réseaux, -23 diffusion).")
    n_samples = int(round(duration * SAMPLE_RATE))
    if n_samples < 1:
        raise AudioError(f"Durée trop courte ({duration!r} s) : moins d'un échantillon à {SAMPLE_RATE} Hz.")
    if n_samples < int(round(MIN_MEASURABLE_S * SAMPLE_RATE)):
        # Refus explicite plutôt qu'un calage faux : sous 400 ms, ebur128 renvoie -70 LUFS et aucun
        # gain ne peut viser une loudness intégrée.
        raise AudioError(
            f"Piste trop courte ({n_samples / SAMPLE_RATE:.3f} s) : la loudness intégrée EBU R128 se mesure "
            f"sur des blocs de 400 ms, une piste de moins de {MIN_MEASURABLE_S} s ne peut pas être calée sur "
            f"{target_lufs} LUFS. Allongez la scène à au moins {MIN_MEASURABLE_S} s, ou retirez audio.synth et "
            "fournissez votre propre fichier audio.src."
        )

    # 1) Densités sans limiteur : mesure à gain unité (sortie null, calcul en double, pas d'écrêtage).
    tried = []
    for drive, hold_frac in SYNTH_LEVELS:
        graph = _source_graph(bpm, beats_per_bar, offset, n_samples, drive, hold_frac)
        m = _measure_graph(graph, "mesure de la piste synthétique")
        _check_measurable(m, duration, offset)
        # Gain arrondi au centième de dB : une valeur décimale courte et reproductible.
        gain_db = round(target_lufs - m["integrated_lufs"], 2)
        predicted_peak = m["true_peak_dbtp"] + gain_db
        tried.append(f"densité {drive}/{hold_frac} : crête prédite {predicted_peak:+.1f} dBTP")
        if predicted_peak <= TRUE_PEAK_CEILING_DBTP - TRUE_PEAK_PREDICTION_MARGIN_DB:
            _write_wav(["-f", "lavfi", "-i", graph + f",volume=volume={gain_db:.2f}dB:precision=double"], [],
                       out_path)
            break
    else:
        # 2) Cible trop forte pour une synthèse non limitée : grosse caisse tenue + limiteur fixe.
        # La synthèse à gain unité est rendue UNE fois en flottant 64 bits (sans perte : aevalsrc
        # calcule en double) : chaque essai de gain relit ce fichier au lieu de réévaluer
        # l'expression (3,5 s par passe pour 60 s de piste, mesuré).
        source = out_path.with_name(out_path.name + ".source.tmp")
        out_path.parent.mkdir(parents=True, exist_ok=True)
        chosen = None
        try:
            for drive, hold_frac in LIMITED_LEVELS:
                graph = _source_graph(bpm, beats_per_bar, offset, n_samples, drive, hold_frac)
                _run_ffmpeg(_FFMPEG_BASE + ["-y", "-f", "lavfi", "-i", graph, "-c:a", "pcm_f64le",
                                            "-f", "wav", str(source)], "rendu intermédiaire de la piste")
                gain_db, m, loudest = _search_limited(source, target_lufs, "calage du limiteur")
                _check_measurable(m, duration, offset)
                tried.append(f"densité {drive}/{hold_frac} + limiteur : au plus {loudest:+.1f} LUFS")
                # Marge de 0,2 LU sur la tolérance : la re-mesure du fichier 24 bits peut différer
                # d'un dixième de la mesure en double.
                if abs(m["integrated_lufs"] - target_lufs) <= LOUDNESS_TOLERANCE_LU - 0.2 and \
                        m["true_peak_dbtp"] <= TRUE_PEAK_CEILING_DBTP - TRUE_PEAK_PREDICTION_MARGIN_DB:
                    chosen = gain_db
                    _write_wav(["-i", str(source)], ["-af", _limiter_chain(gain_db)], out_path)
                    break
        finally:
            if source.exists():
                source.unlink()
        if chosen is None:
            raise AudioError(
                f"Impossible d'atteindre {target_lufs} LUFS avec une crête vraie ≤ {TRUE_PEAK_CEILING_DBTP} dBTP "
                f"({'; '.join(tried)}). Choisissez une cible moins forte (audio.synth.target_lufs, par ex. -14)."
            )

    # 3) Re-vérification sur le fichier réellement écrit (24 bits).
    final = measure_loudness(out_path)
    if abs(final["integrated_lufs"] - target_lufs) > LOUDNESS_TOLERANCE_LU or \
            final["true_peak_dbtp"] > TRUE_PEAK_CEILING_DBTP:
        raise AudioError(
            f"Piste générée hors cible : {final['integrated_lufs']} LUFS (cible {target_lufs} ± "
            f"{LOUDNESS_TOLERANCE_LU}), crête {final['true_peak_dbtp']} dBTP (max {TRUE_PEAK_CEILING_DBTP}). "
            f"Fichier conservé pour inspection : {out_path}."
        )
    return out_path


def measure_loudness(path) -> dict:
    """Loudness EBU R128 d'un fichier (1re piste audio) : {integrated_lufs, true_peak_dbtp, lra}.

    Filtre ebur128 avec peak=true : crête vraie mesurée sur un signal suréchantillonné ×4
    (BS.1770), c'est la valeur que vérifient les plateformes et la QC.
    """
    path = Path(path)
    if not path.is_file():
        raise AudioError(f"Fichier introuvable pour la mesure de loudness : {path}.")
    err = _run_ffmpeg(_FFMPEG_BASE + ["-i", str(path), "-map", "0:a:0", "-af",
                                      "ebur128=peak=true:framelog=quiet", "-f", "null", "-"],
                      f"mesure de loudness de {path.name}")
    return _parse_ebur128(err, f"mesure de loudness de {path.name}")


def read_wav(path) -> tuple[np.ndarray, int]:
    """Lit un WAV PCM (entier 8/16/24/32 bits ou flottant 32/64 bits, WAVE_FORMAT_EXTENSIBLE compris).

    Renvoie (échantillons float64 de forme (n, canaux) dans [-1, 1], fréquence). Lecteur RIFF
    maison : le module wave de la bibliothèque standard ne décode pas le 24 bits en tableau et
    numpy ne lit pas directement des entiers sur 3 octets.
    """
    path = Path(path)
    data = path.read_bytes()
    if len(data) < 12 or data[0:4] != b"RIFF" or data[8:12] != b"WAVE":
        raise AudioError(f"{path} n'est pas un fichier WAV (en-tête RIFF/WAVE absent).")
    pos = 12
    fmt = None
    pcm = None
    while pos + 8 <= len(data):
        cid = data[pos:pos + 4]
        size = int.from_bytes(data[pos + 4:pos + 8], "little")
        body = data[pos + 8:pos + 8 + size]
        if cid == b"fmt ":
            tag = int.from_bytes(body[0:2], "little")
            channels = int.from_bytes(body[2:4], "little")
            rate = int.from_bytes(body[4:8], "little")
            bits = int.from_bytes(body[14:16], "little")
            if tag == 0xFFFE and len(body) >= 26:
                tag = int.from_bytes(body[24:26], "little")   # sous-format (2 premiers octets du GUID)
            fmt = (tag, channels, rate, bits)
        elif cid == b"data":
            pcm = body
        pos += 8 + size + (size & 1)   # les blocs RIFF sont alignés sur 2 octets
    if fmt is None or pcm is None:
        raise AudioError(f"{path} : bloc « fmt » ou « data » manquant, fichier WAV incomplet.")
    tag, channels, rate, bits = fmt
    width = bits // 8
    usable = len(pcm) - len(pcm) % (width * channels)
    raw = pcm[:usable]
    if tag == 1 and bits == 24:
        b = np.frombuffer(raw, dtype=np.uint8).reshape(-1, 3).astype(np.int32)
        v = b[:, 0] | (b[:, 1] << 8) | (b[:, 2] << 16)
        v = (v ^ 0x800000) - 0x800000           # extension de signe du 24 bits
        x = v.astype(np.float64) / float(1 << 23)
    elif tag == 1 and bits == 16:
        x = np.frombuffer(raw, dtype="<i2").astype(np.float64) / 32768.0
    elif tag == 1 and bits == 32:
        x = np.frombuffer(raw, dtype="<i4").astype(np.float64) / float(1 << 31)
    elif tag == 1 and bits == 8:
        x = (np.frombuffer(raw, dtype=np.uint8).astype(np.float64) - 128.0) / 128.0
    elif tag == 3 and bits in (32, 64):
        x = np.frombuffer(raw, dtype="<f4" if bits == 32 else "<f8").astype(np.float64)
    else:
        raise AudioError(f"{path} : format WAV non pris en charge (code {tag}, {bits} bits). "
                         "Convertissez en PCM 24 bits : ffmpeg -i in -c:a pcm_s24le out.wav")
    return x.reshape(-1, channels), rate


def _box_same(x: np.ndarray, length: int) -> np.ndarray:
    """Moyenne glissante CENTRÉE (même longueur, sans retard) par somme cumulée : O(n)."""
    if length <= 1:
        return x.copy()
    c = np.concatenate(([0.0], np.cumsum(x)))
    half = length // 2
    idx = np.arange(x.size)
    lo = np.clip(idx - half, 0, x.size)
    hi = np.clip(idx - half + length, 0, x.size)
    return (c[hi] - c[lo]) / length


def detect_onsets(path, *, threshold_db: float = -30.0, lowpass_hz: float | None = 250.0) -> list[float]:
    """Temps (s) des attaques d'un WAV.

    Méthode : mono (moyenne des canaux) -> passe-bas optionnel (2 moyennes glissantes centrées ;
    250 Hz par défaut = bande de la grosse caisse, le charleston des contretemps est ignoré : on
    obtient les TEMPS, comme un détecteur de pulsation) -> enveloppe d'énergie sur 20 ms tous les
    1 ms, en dB relatifs au maximum -> une attaque est un passage au-dessus de threshold_db après un
    passage sous (threshold_db - 6 dB) -> affinage au premier échantillon dont l'amplitude dépasse
    le seuil. lowpass_hz=None : pleine bande (le charleston devient aussi une attaque).
    """
    x, rate = read_wav(path)
    if x.size == 0:
        return []
    y = x.mean(axis=1)
    if lowpass_hz:
        # Moyenne glissante de L échantillons : -3 dB vers 0,443·fs/L ; appliquée deux fois
        # (réponse triangulaire) pour mieux rejeter les aigus, sans décalage temporel car centrée.
        length = max(1, int(round(0.443 * rate / float(lowpass_hz))))
        y = _box_same(_box_same(y, length), length)
    win = max(1, int(round(ONSET_WINDOW_S * rate)))
    hop = max(1, int(round(ONSET_HOP_S * rate)))
    power = _box_same(y * y, win)
    centers = np.arange(0, y.size, hop)
    env = power[centers]
    peak = float(env.max())
    if peak <= 0.0:
        return []
    with np.errstate(divide="ignore"):
        env_db = 10.0 * np.log10(env / peak)
    rearm_db = threshold_db - ONSET_HYSTERESIS_DB
    amp_threshold = math.sqrt(peak) * 10.0 ** (threshold_db / 20.0)
    onsets: list[float] = []
    armed = True
    for i, level in enumerate(env_db):
        if armed and level >= threshold_db:
            c = int(centers[i])
            lo = max(0, c - win // 2 - hop)
            hi = min(y.size, c + win // 2 + 1)
            above = np.nonzero(np.abs(y[lo:hi]) >= amp_threshold)[0]
            sample = lo + int(above[0]) if above.size else c
            onsets.append(sample / rate)
            armed = False
        elif not armed and level < rearm_db:
            armed = True
    return onsets


def _scene_samples(compiled: dict) -> int:
    """Nombre d'échantillons de la scène : frames × 48000 / fps, exact pour les fps autorisés."""
    frames = compiled.get("frames")
    fps = (compiled.get("format") or {}).get("fps")
    if isinstance(frames, int) and isinstance(fps, int) and fps > 0:
        exact = Fraction(frames * SAMPLE_RATE, fps)
        if exact.denominator == 1:
            return int(exact)
    return int(round(float(compiled["duration"]) * SAMPLE_RATE))


def _sha256_file(path: Path) -> str:
    """Empreinte SHA-256 du contenu d'un fichier (lecture par blocs : mémoire constante)."""
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def _read_sidecar(sidecar: Path) -> dict | None:
    """Fiche <src>.synth.json {params, wav_sha256} ; None si illisible ou d'un autre format."""
    try:
        record = json.loads(sidecar.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(record, dict) or not isinstance(record.get("params"), dict) \
            or not isinstance(record.get("wav_sha256"), str):
        return None
    return record


def ensure_scene_audio(compiled: dict, *, log=print) -> Path | None:
    """Garantit la présence de la piste audio d'une scène compilée.

    - pas de bloc audio, ou bloc « tempo seul » (audio.bpm sans src ni synth, pour les références
      beat:) : None, livrables sans son ;
    - audio.src présent sur disque : conservé tel quel, SAUF s'il est encore exactement la piste
      générée par ce module (empreinte SHA-256 notée dans <src>.synth.json) avec d'autres paramètres :
      il est alors régénéré pour rester calé. Un fichier remplacé par l'utilisateur n'est jamais écrasé ;
    - absent et audio.synth défini : généré (durée de la scène, bpm/offset/beats_per_bar de la scène) ;
    - absent sans synth : erreur actionnable.
    """
    audio = compiled.get("audio")
    if not audio:
        return None
    synth = audio.get("synth")
    src_value = audio.get("src")
    if not src_value:
        if synth:
            raise AudioError(
                "audio.synth est défini mais la scène compilée n'indique aucun chemin audio.src : recompilez la "
                "scène (le compilateur choisit alors assets/audio/<scène>_beat.wav) ou ajoutez \"src\" au bloc audio."
            )
        # Schéma : un bloc audio peut ne porter que le tempo (références beat:) sans aucune piste.
        log("[audio] tempo seul (audio.bpm sans piste ni synth) : aucune piste à préparer, livrables sans son.")
        return None
    src = Path(src_value)
    if not synth:
        if src.is_file():
            return src
        raise AudioError(
            f"Fichier audio introuvable : {src}. Placez le fichier à cet emplacement (chemin relatif à "
            "la racine du projet dans la scène) ou ajoutez \"synth\": {\"kind\": \"beat\"} au bloc audio "
            "pour générer une piste de test."
        )
    bpm = audio.get("bpm")
    if not bpm:
        raise AudioError("audio.synth exige audio.bpm (tempo de la piste de test) : ajoutez par ex. "
                         "\"bpm\": 120 au bloc audio de la scène.")
    target = audio.get("target_lufs")
    if target is None:
        target = synth.get("target_lufs")
    if target is None:
        target = (compiled.get("qc") or {}).get("loudness_target_lufs", -14.0)
    n_samples = _scene_samples(compiled)
    params = {
        "generator": GENERATOR_ID,
        "bpm": float(bpm),
        "samples": n_samples,
        "sample_rate": SAMPLE_RATE,
        "beats_per_bar": int(audio.get("beats_per_bar") or 4),
        "offset": float(audio.get("offset") or 0.0),
        "target_lufs": float(target),
    }
    sidecar = src.with_name(src.name + ".synth.json")
    if src.is_file():
        if not sidecar.is_file():
            log(f"[audio] {src.name} existe (fourni, non généré) : conservé tel quel.")
            return src
        record = _read_sidecar(sidecar)
        # Le fichier n'est « à nous » que s'il a gardé l'empreinte notée à la génération : le nom
        # seul ne prouve rien (l'utilisateur a pu y copier sa propre musique).
        if record is None or record["wav_sha256"] != _sha256_file(src):
            log(f"[audio] avertissement : {src.name} n'est plus la piste générée par le pipeline (fichier remplacé "
                f"par l'utilisateur) : conservé tel quel, NON régénéré. Pour régénérer la piste de test, supprimez "
                f"{src.name} ; pour faire taire cet avertissement, supprimez {sidecar.name}.")
            return src
        if record["params"] == params:
            log(f"[audio] {src.name} à jour (paramètres de synthèse identiques).")
            return src
        log(f"[audio] {src.name} (piste de test générée) a d'autres paramètres : régénération.")
    log(f"[audio] génération de {src} : {params['bpm']} BPM, {params['beats_per_bar']} temps/mesure, "
        f"offset {params['offset']} s, {n_samples} échantillons, cible {params['target_lufs']} LUFS.")
    synth_beat(src, params["bpm"], n_samples / SAMPLE_RATE, beats_per_bar=params["beats_per_bar"],
               offset=params["offset"], target_lufs=params["target_lufs"])
    m = measure_loudness(src)
    log(f"[audio] {src.name} : {m['integrated_lufs']} LUFS, crête vraie {m['true_peak_dbtp']} dBTP.")
    # Fiche écrite juste après la piste (écriture atomique) : empreinte du fichier réellement produit.
    tmp = sidecar.with_name(sidecar.name + ".tmp")
    tmp.write_text(json.dumps({"params": params, "wav_sha256": _sha256_file(src)}, indent=2, sort_keys=True) + "\n",
                   encoding="utf-8")
    os.replace(tmp, sidecar)
    return src


# ---------------------------------------------------------------------------
# Détection du tempo, de la phase, de la mesure et du « drop » (mograph beats)
# ---------------------------------------------------------------------------

# Analyse à 11 025 Hz mono : suffisant pour les attaques (grosse caisse, caisse claire), 4 fois moins
# de calcul qu'à 44,1 kHz. Trame 1024 (93 ms), pas 128 (11,6 ms) : résolution de phase < 6 ms.
TEMPO_RATE = 11_025
TEMPO_FRAME = 1024
TEMPO_HOP = 128
# A priori log-normal centré sur 120 BPM (1 octave d'écart-type) : départage les erreurs d'octave
# (60 / 120 / 240) comme les détecteurs usuels, sans empêcher un 90 ou un 174 bien marqués.
TEMPO_PRIOR_BPM = 120.0
TEMPO_PRIOR_OCTAVES = 1.0


def _decode_mono(path, rate: int = TEMPO_RATE) -> np.ndarray:
    """N'importe quel fichier audio/vidéo -> échantillons mono float32 à `rate` Hz (FFmpeg)."""
    argv = _FFMPEG_BASE + ["-v", "error", "-i", str(path), "-vn", "-ac", "1", "-ar", str(rate),
                           "-f", "f32le", "-acodec", "pcm_f32le", "pipe:1"]
    try:
        r = subprocess.run(argv, capture_output=True, timeout=600)
    except (OSError, subprocess.SubprocessError) as exc:
        raise AudioError(f"FFmpeg indisponible pour décoder {path} : {exc}") from exc
    if r.returncode != 0:
        raise AudioError(f"Décodage audio impossible ({path}) : {r.stderr.decode(errors='replace').strip()[-300:]}")
    return np.frombuffer(r.stdout, dtype=np.float32)


def _onset_strength(y: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Flux spectral positif (log-magnitude) par trame ; renvoie (flux large bande, flux < 200 Hz)."""
    if y.size < TEMPO_FRAME * 4:
        raise AudioError("Extrait audio trop court pour estimer un tempo (au moins 1 s).")
    n = 1 + (y.size - TEMPO_FRAME) // TEMPO_HOP
    idx = np.arange(TEMPO_FRAME)[None, :] + TEMPO_HOP * np.arange(n)[:, None]
    frames = y[idx] * np.hanning(TEMPO_FRAME).astype(np.float32)[None, :]
    mag = np.abs(np.fft.rfft(frames, axis=1)).astype(np.float32)
    logm = np.log1p(1000.0 * mag)
    flux = np.maximum(0.0, np.diff(logm, axis=0))
    low_bins = max(2, int(200 * TEMPO_FRAME / TEMPO_RATE))
    wide = np.concatenate([[0.0], flux.sum(axis=1)])
    low = np.concatenate([[0.0], flux[:, :low_bins].sum(axis=1)])
    # Soustraction d'une moyenne glissante (~0,5 s) : on garde les attaques, pas le niveau global.
    k = max(1, int(0.5 * TEMPO_RATE / TEMPO_HOP))
    kern = np.ones(k, dtype=np.float64) / k
    wide = np.maximum(0.0, wide - np.convolve(wide, kern, mode="same"))
    low = np.maximum(0.0, low - np.convolve(low, kern, mode="same"))
    return wide, low


def _interp(sig: np.ndarray, pos: np.ndarray) -> np.ndarray:
    return np.interp(pos, np.arange(sig.size), sig, left=0.0, right=0.0)


def _rise_step(rate: int) -> int:
    """Pas de l'enveloppe fine en échantillons (≈ 1 ms ; exactement step / rate secondes)."""
    return max(1, int(round(rate / 1000)))


def _rise_envelope(y: np.ndarray, rate: int, lowpass_hz: float | None) -> np.ndarray:
    """Montée d'énergie à 1 ms de résolution (dérivée positive de la puissance lissée sur 10 ms) :
    son maximum tombe sur l'instant d'attaque, sans le retard d'une trame spectrale."""
    x = y.astype(np.float64)
    if lowpass_hz:
        length = max(1, int(round(0.443 * rate / float(lowpass_hz))))
        x = _box_same(_box_same(x, length), length)
    power = _box_same(x * x, max(1, int(round(0.010 * rate))))
    amp = np.sqrt(power[::_rise_step(rate)])
    rise = np.maximum(0.0, np.diff(amp, prepend=amp[:1]))
    return rise / (rise.max() or 1.0)


def analyse_tempo(path, *, bpm_min: float = 60.0, bpm_max: float = 200.0, beats_per_bar: int = 4) -> dict:
    """Tempo (BPM), temps 0 (offset = premier temps FORT), confiance et « drop » d'un morceau à tempo fixe.

    Méthode : (1) période par autocorrélation du flux spectral (large bande + basses), pondérée par un
    a priori centré sur 120 BPM ; (2) période et phase affinées sur l'enveloppe de montée des basses à
    1 ms (grosse caisse) ; (3) tempo divisé par deux si les basses ne frappent qu'un temps sur deux ;
    (4) premier temps de mesure = position où les basses frappent le plus fort ; (5) drop = plus forte
    hausse d'énergie (≥ 3 dB) d'une mesure à la suivante. Déterministe : même fichier -> même résultat.
    """
    y = _decode_mono(path)
    duration = y.size / TEMPO_RATE
    wide, low = _onset_strength(y)
    fps_env = TEMPO_RATE / TEMPO_HOP
    env = wide / (wide.max() or 1.0) + 2.0 * low / (low.max() or 1.0)

    # 1. Période approximative (trames spectrales).
    lag_min = int(math.floor(60.0 * fps_env / bpm_max))
    lag_max = int(math.ceil(60.0 * fps_env / bpm_min))
    e = env - env.mean()
    ac = np.correlate(e, e, mode="full")[e.size - 1:]
    ac = ac / (ac[0] or 1.0)
    lags = np.arange(max(1, lag_min), min(lag_max, ac.size - 2) + 1)
    if lags.size == 0:
        raise AudioError("Extrait trop court pour la plage de tempo demandée.")
    bpms = 60.0 * fps_env / lags
    prior = np.exp(-0.5 * (np.log2(bpms / TEMPO_PRIOR_BPM) / TEMPO_PRIOR_OCTAVES) ** 2)
    score = np.array([sum(ac[m * L] for m in (1, 2, 3, 4) if m * L < ac.size) for L in lags]) * prior
    beat_s = float(lags[int(np.argmax(score))]) / fps_env

    # 2. Affinage fin (1 ms) : période ± 2 % et phase sur l'enveloppe de montée (basses, puis large bande
    # si le morceau n'a pas de basses marquées).
    rise = _rise_envelope(y, TEMPO_RATE, 150.0)
    if float(rise.sum()) <= 0.0:
        rise = _rise_envelope(y, TEMPO_RATE, None)
    ms = np.arange(rise.size, dtype=np.float64)
    # Unité de l'enveloppe fine : un pas = step / rate s (0,998 ms à 11 025 Hz), PAS exactement 1 ms.
    unit_s = _rise_step(TEMPO_RATE) / TEMPO_RATE
    beat_units = beat_s / unit_s

    def fit(period_ms: float) -> tuple[float, float]:
        n = int((rise.size - 1) // period_ms)
        if n < 2:
            return -1.0, 0.0
        grid = period_ms * np.arange(n)
        best = (-1.0, 0.0)
        for ph in np.arange(0.0, period_ms, 1.0):
            v = float(np.interp(ph + grid, ms, rise).sum()) / n
            if v > best[0]:
                best = (v, ph)
        return best

    best = (-1.0, beat_units, 0.0)
    for pm in np.linspace(beat_units * 0.98, beat_units * 1.02, 81):
        v, ph = fit(pm)
        if v > best[0]:
            best = (v, pm, ph)
    _, period_ms, phase_ms = best

    # 3. Octave : si un temps sur deux porte l'essentiel des basses, le vrai tempo est moitié.
    def beat_strengths(period: float, phase: float) -> np.ndarray:
        n = int((rise.size - 1 - phase) // period)
        return np.interp(phase + period * np.arange(n), ms, rise)

    bs = beat_strengths(period_ms, phase_ms)
    if bs.size >= 8 and 60.0 / (2 * period_ms * unit_s) >= bpm_min:
        even, odd = float(bs[0::2].mean()), float(bs[1::2].mean())
        if min(even, odd) < 0.35 * max(even, odd):
            if odd > even:
                phase_ms += period_ms
            period_ms *= 2
            bs = beat_strengths(period_ms, phase_ms)

    # 4. Premier temps de la mesure : position où les basses frappent le plus fort.
    bar_scores = [float(bs[k::beats_per_bar].mean()) if bs[k::beats_per_bar].size else 0.0
                  for k in range(beats_per_bar)]
    down = int(np.argmax(bar_scores))
    beat_s = period_ms * unit_s
    bpm = 60.0 / beat_s
    bar_len = beat_s * beats_per_bar
    offset = ((phase_ms + down * period_ms) * unit_s) % bar_len
    # Temps fort à quelques ms AVANT 0 (arrondi de phase) : c'est 0, pas la fin de la 1re mesure.
    if offset > bar_len - 0.02:
        offset = max(0.0, offset - bar_len)

    # Confiance : part des temps de la grille qui tombent sur l'attaque la plus forte de LEUR voisinage
    # (± une demi-période) ; indépendante du niveau (une intro calme ne compte pas comme un raté).
    hits, total_b = 0, 0
    half, tol = period_ms / 2, max(2.0, 0.05 * period_ms)
    b_pos = phase_ms
    while b_pos + half < rise.size:
        lo, hi = int(max(0, b_pos - half)), int(b_pos + half)
        win = rise[lo:hi]
        if win.size and win.max() > 0:
            total_b += 1
            if abs((lo + int(np.argmax(win))) - b_pos) <= tol:
                hits += 1
        b_pos += period_ms
    confidence = hits / total_b if total_b else 0.0

    # 5. Drop : plus forte hausse d'énergie RMS d'une mesure à la suivante (≥ 3 dB).
    bar_s = beat_s * beats_per_bar
    drop = None
    n_bars = int((duration - offset) // bar_s)
    if n_bars >= 3:
        rms = []
        for k in range(n_bars):
            a = int((offset + k * bar_s) * TEMPO_RATE)
            b = int((offset + (k + 1) * bar_s) * TEMPO_RATE)
            seg = y[a:b].astype(np.float64)
            rms.append(10 * math.log10(float(np.mean(seg * seg)) + 1e-12))
        jumps = np.diff(rms)
        k = int(np.argmax(jumps))
        if jumps[k] >= 3.0:
            drop = round(float(offset + (k + 1) * bar_s), 3)

    return {"bpm": round(float(bpm), 2), "offset": round(float(offset), 3), "beats_per_bar": int(beats_per_bar),
            "confidence": round(confidence, 3), "drop": drop, "duration": round(float(duration), 3)}
