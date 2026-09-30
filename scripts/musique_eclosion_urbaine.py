"""Musique originale du film concept « Éclosion Urbaine » : 31 s, 120 BPM, ré majeur (libre de droits).

    .venv\\Scripts\\python.exe scripts\\musique_eclosion_urbaine.py   -> assets/audio/eclosion_urbaine_musique.wav

Entièrement synthétisée (numpy) et déterministe : relancer le script redonne le même fichier au bit
près. WAV 48 kHz, stéréo, 24 bits. Les instants des événements ne sont PAS recopiés à la main : ils
sont lus dans le générateur de la scène (scene_eclosion_urbaine.compute_events()), si bien qu'une
retouche de l'image déplace le son avec elle. Déroulé :

  0-5 s     La graine : nappe douce qui s'ouvre ; sifflement de chute ; impact CRISTALLIN à 1,0 s ;
            glissando de l'argile qui s'étire, tintements des gemmes qui germent, glissando de verre sous
            « L'ORIGINE » ; arpège doux pendant le recul.
  5-11,5 s  L'expansion : souffle du recul ; craquements du sol qui se fend ; un coup accordé par vague
            d'immeubles (temps 12 à 18) ; grosse caisse, charleston, basse ; un « ping » par nœud du réseau
            qui s'allume et des bips de données sur les paquets ; roulement et montée jusqu'à 11,5 s.
  11,5-12 s Le piqué : la musique se retient, grand souffle de la plongée qui culmine à la vitesse
            maximale de la caméra (11,69 s) ; souffle léger quand elle se pose dans la rue (12,8 s).
  12-20 s   Le flux : impact et groove complet à l'allumage des néons ; la musique ACCÉLÈRE ; chaque mot
            claque sur son apparition (12,5 / 14,04 / 15,54 s) et son souffle culmine à l'image exacte
            où il passe la caméra (14 / 15,5 / 17 s) ; cloches à l'arrêt devant l'arbre (18 s), montée.
  20-24 s   Le morphing : arrêt de bande (apesanteur), éclatement scintillant, ASPIRATION en réverbération
            inversée jusqu'à l'œil (23,2 s), BATTEMENT DE CŒUR grave et déclic du diaphragme (23,5 s).
  24-31 s   L'outro : dernier battement (24,5 s), clin d'œil, souffle de la forme qui se gonfle, accord
            final SUSPENDU (ré sus2), tintements du slogan lettre par lettre ; fondu de 30 à 31 s.
"""

from __future__ import annotations

import sys
import wave
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from scene_eclosion_urbaine import compute_events  # noqa: E402

EV = compute_events()

SR = 48000
BPM = 120
BEAT = 60 / BPM
DUR = float(EV["end"])
N = int(DUR * SR)
ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "assets" / "audio" / "eclosion_urbaine_musique.wav"
rng = np.random.default_rng(20260925)

# Grille harmonique : un accord par mesure de 2 s (MIDI), basse, arpège.
CHORDS = ["D", "G", "Bm", "Asus", "D", "G", "Bm", "G", "D", "Asus"]
PAD = {"D": [50, 57, 61, 64, 66], "G": [55, 59, 62, 66, 69], "Bm": [47, 54, 61, 62, 66],
       "Asus": [45, 52, 57, 62, 64], "Dsus": [50, 57, 62, 64, 69]}
BASS = {"D": 38, "G": 43, "Bm": 35, "Asus": 33, "Dsus": 38}
ARP = {"D": [74, 78, 81, 85], "G": [74, 79, 83, 86], "Bm": [73, 74, 78, 81], "Asus": [74, 76, 81, 83],
       "Dsus": [74, 76, 81, 86]}
ARP_STEPS = [0, 2, 1, 3, 2, 1, 3, 0, 1, 3, 2, 0, 3, 1, 2, 3]
PENTA = [74, 76, 78, 81, 83, 86, 88, 90, 93, 95]


def mtof(m: float) -> float:
    return 440.0 * 2 ** ((m - 69) / 12)


def bt(beat: float) -> float:
    return beat * BEAT


def tvec(n: int) -> np.ndarray:
    return np.arange(n) / SR


def chord_at(t: float) -> str:
    return CHORDS[min(int(t // 2), len(CHORDS) - 1)]


# ------------------------------------------------------------------------------------------------
# Filtres et bus
# ------------------------------------------------------------------------------------------------

def fft_filter(x: np.ndarray, lo: float | None = None, hi: float | None = None, order: int = 4) -> np.ndarray:
    n = x.shape[-1]
    f = np.fft.rfftfreq(n, 1 / SR)
    g = np.ones_like(f)
    if hi:
        g /= np.sqrt(1 + (f / hi) ** (2 * order))
    if lo:
        g /= np.sqrt(1 + (lo / np.maximum(f, 1e-3)) ** (2 * order))
    return np.fft.irfft(np.fft.rfft(x, axis=-1) * g, n=n, axis=-1)


def stft_lowpass(x: np.ndarray, cutoff_at, nfft: int = 2048, hop: int = 512) -> np.ndarray:
    """Passe-bas variable (cutoff_at(t) -> Hz), fenêtre racine de Hann, 75 % de recouvrement."""
    win = np.sqrt(np.hanning(nfft + 1)[:-1])
    pad = np.pad(x, ((0, 0), (nfft, nfft)))
    n_frames = (pad.shape[1] - nfft) // hop + 1
    f = np.fft.rfftfreq(nfft, 1 / SR)
    out = np.zeros_like(pad)
    idx = np.arange(nfft)[None, :] + hop * np.arange(n_frames)[:, None]
    centers = (hop * np.arange(n_frames) + nfft / 2 - nfft) / SR
    fc = np.maximum(np.array([cutoff_at(t) for t in centers]), 20.0)
    g = 1 / np.sqrt(1 + (f[None, :] / fc[:, None]) ** 8)
    for c in range(pad.shape[0]):
        frames = pad[c][idx] * win
        y = np.fft.irfft(np.fft.rfft(frames, axis=1) * g, n=nfft, axis=1) * win
        np.add.at(out[c], idx, y)
    return out[:, nfft:nfft + x.shape[1]] / 2.0


class Bus:
    def __init__(self) -> None:
        self.x = np.zeros((2, N))

    def add(self, t0: float, sig: np.ndarray, gain: float = 1.0, pan: float = 0.0) -> None:
        i0 = int(round(t0 * SR))
        if sig.ndim == 1:
            a = (pan + 1) * np.pi / 4
            sig = np.vstack([sig * np.cos(a), sig * np.sin(a)])
        if i0 >= N:
            return
        s = sig[:, max(0, -i0):]
        i0 = max(0, i0)
        n = min(s.shape[1], N - i0)
        self.x[:, i0:i0 + n] += gain * s[:, :n]


def env_ar(n: int, attack: float, release: float, hold: float | None = None) -> np.ndarray:
    t = tvec(n)
    a = np.minimum(1.0, t / max(attack, 1e-4))
    if hold is None:
        return a * np.exp(-t / max(release, 1e-4))
    return a * np.where(t < hold, 1.0, np.exp(-(t - hold) / max(release, 1e-4)))


# ------------------------------------------------------------------------------------------------
# Instruments
# ------------------------------------------------------------------------------------------------

def bell(freq: float, dur: float = 2.4, bright: float = 1.0) -> np.ndarray:
    """Cloche de verre : partiels inharmoniques, les aigus s'éteignent plus vite."""
    n = int(dur * SR)
    t = tvec(n)
    y = np.zeros(n)
    for ratio, amp, dec in ((1.0, 1.0, 2.2), (2.0, 0.45, 3.5), (2.76, 0.5 * bright, 5.0), (4.07, 0.28 * bright, 7.5),
                            (5.4, 0.2 * bright, 10.0), (6.8, 0.12 * bright, 14.0)):
        f = freq * ratio
        if f < 16000:
            y += amp * np.sin(2 * np.pi * f * t + ratio) * np.exp(-t * dec)
    y *= np.minimum(1, t / 0.0015)
    return y / np.max(np.abs(y))


def glass_tick(freq: float) -> np.ndarray:
    return bell(freq, 0.5, 1.3) * np.exp(-tvec(int(0.5 * SR)) * 7)


def crystal_impact() -> np.ndarray:
    """Impact cristallin : deux cloches aiguës + éclat de bruit filtré + petit choc grave."""
    n = int(3.2 * SR)
    t = tvec(n)
    y = np.zeros((2, n))
    for f, g, pan in ((mtof(93), 1.0, -0.25), (mtof(100), 0.7, 0.3), (mtof(86), 0.55, 0.0)):
        b = np.zeros(n)
        bb = bell(f, 3.2, 1.2)
        b[:len(bb)] = bb
        a = (pan + 1) * np.pi / 4
        y += g * np.vstack([b * np.cos(a), b * np.sin(a)])
    shard = fft_filter(rng.standard_normal((2, n)) * np.exp(-t * 60), lo=5000, hi=16000) * 0.5
    thump = np.sin(2 * np.pi * (70 * t + 40 / 25 * (1 - np.exp(-25 * t)))) * np.exp(-t * 14) * 0.9
    y += shard + thump[None, :]
    return y / np.max(np.abs(y))


def fall_whistle(dur: float) -> np.ndarray:
    n = int(dur * SR)
    t = tvec(n)
    p = t / dur
    f = 2600 * (0.35 ** p)
    y = np.sin(2 * np.pi * np.cumsum(f) / SR) * (p ** 1.5) * 0.6
    air = fft_filter(rng.standard_normal(n), lo=3000, hi=9000) * (p ** 2) * 0.35
    return (y + air) * np.minimum(1, (1 - p) * 40 + 0.0)


def kick(dur: float = 0.5, f0: float = 150.0, f1: float = 44.0) -> np.ndarray:
    n = int(dur * SR)
    t = tvec(n)
    k = 30.0
    body = np.sin(2 * np.pi * (f1 * t + (f0 - f1) / k * (1 - np.exp(-k * t)))) * np.exp(-6.5 * t)
    click = fft_filter(rng.standard_normal(n) * np.exp(-t * 500), lo=1500) * 0.3
    y = np.tanh(1.8 * (body + click)) / np.tanh(1.8)
    y[:48] *= np.linspace(0, 1, 48)
    return y


def tom(freq: float, dur: float = 0.55) -> np.ndarray:
    """Coup accordé (tom électronique) : sinus qui chute d'une quarte, attaque de bruit."""
    n = int(dur * SR)
    t = tvec(n)
    f = freq * (1 + 0.35 * np.exp(-t * 28))
    y = np.sin(2 * np.pi * np.cumsum(f) / SR) * np.exp(-t * 7.5)
    y += fft_filter(rng.standard_normal(n) * np.exp(-t * 90), lo=800, hi=6000) * 0.25
    return np.tanh(1.4 * y) / np.tanh(1.4)


def clap(dur: float = 0.4) -> np.ndarray:
    n = int(dur * SR)
    t = tvec(n)
    env = np.zeros(n)
    for dt in (0.0, 0.011, 0.023):
        i = int(dt * SR)
        env[i:] += np.exp(-t[: n - i] * 140)
    env += 0.4 * np.exp(-t * 16)
    y = fft_filter(rng.standard_normal(n) * env, lo=900, hi=7000)
    return y / np.max(np.abs(y))


def snare(dur: float = 0.25) -> np.ndarray:
    n = int(dur * SR)
    t = tvec(n)
    y = fft_filter(rng.standard_normal(n), lo=350, hi=9000) * np.exp(-t * 24)
    y += 0.6 * np.sin(2 * np.pi * 190 * t) * np.exp(-t * 38)
    return y / np.max(np.abs(y))


def hat(open_: bool = False) -> np.ndarray:
    dur = 0.3 if open_ else 0.05
    n = int(dur * SR)
    t = tvec(n)
    y = fft_filter(rng.standard_normal(n), lo=7500) * np.exp(-t * (12 if open_ else 85))
    return y / np.max(np.abs(y))


def crackle(dur: float = 0.09) -> np.ndarray:
    """Craquement sec (dalle qui se fend)."""
    n = int(dur * SR)
    t = tvec(n)
    y = fft_filter(rng.standard_normal(n), lo=1200, hi=7000) * np.exp(-t * 55)
    y += 0.5 * np.sin(2 * np.pi * 180 * t) * np.exp(-t * 60)
    return y / np.max(np.abs(y))


def blip(freq: float, dur: float = 0.09) -> np.ndarray:
    n = int(dur * SR)
    t = tvec(n)
    return np.sin(2 * np.pi * freq * t) * np.minimum(1, t / 0.002) * np.exp(-t * 45)


def crash(dur: float = 2.6) -> np.ndarray:
    n = int(dur * SR)
    t = tvec(n)
    y = fft_filter(rng.standard_normal((2, n)), lo=3800, hi=15000) * np.exp(-t * 2.0)
    return y / np.max(np.abs(y))


def impact(dur: float = 1.8) -> np.ndarray:
    n = int(dur * SR)
    t = tvec(n)
    f = 38 + 70 * np.exp(-t * 9)
    boom = np.sin(2 * np.pi * np.cumsum(f) / SR) * np.exp(-t * 2.6)
    nz = fft_filter(rng.standard_normal(n), hi=2400) * np.exp(-t * 12) * 0.6
    y = np.tanh(1.6 * (boom + nz))
    return y / np.max(np.abs(y))


def riser(dur: float, f_lo: float = 300.0, f_hi: float = 13000.0) -> np.ndarray:
    n = int(dur * SR)
    t = tvec(n)
    p = t / dur
    noise = stft_lowpass(rng.standard_normal((2, n)), lambda s: f_lo * (f_hi / f_lo) ** min(max(s / dur, 0), 1))
    tone = np.sin(2 * np.pi * np.cumsum(170 * (8 ** p)) / SR) * 0.3
    y = noise + tone[None, :]
    y *= (p ** 2.2)[None, :]
    y[:, -96:] *= np.linspace(1, 0, 96)
    return y / np.max(np.abs(y))


def whoosh(dur: float = 0.5, peak: float = 0.8, sweep: bool = True) -> np.ndarray:
    """Souffle qui culmine à peak × dur. sweep : traverse de gauche à droite ; sinon centré et large
    (deux bruits décorrélés), pour ce qui passe au centre de l'image."""
    n = int(dur * SR)
    t = tvec(n)
    p = t / dur
    env = np.where(p < peak, (p / peak) ** 2, np.exp(-(p - peak) * 22))
    y = stft_lowpass(rng.standard_normal((2, n)), lambda s: 500 + 9000 * np.sin(np.pi * min(max(s / dur, 0), 1)))
    if sweep:
        y = y.mean(axis=0) * env
        a = np.clip(p * 1.2 - 0.1, 0, 1) * np.pi / 2
        st = np.vstack([y * np.cos(a), y * np.sin(a)])
    else:
        st = y * env[None, :]
    return st / np.max(np.abs(st))


def stretch_gliss(dur: float) -> np.ndarray:
    """L'argile qui s'étire : glissando doux et rond (sinus + octave) qui monte d'une quinte."""
    n = int(dur * SR)
    t = tvec(n)
    p = t / dur
    f = mtof(62) * 2 ** (7 * p ** 1.4 / 12)
    ph = 2 * np.pi * np.cumsum(f) / SR
    y = (np.sin(ph) + 0.3 * np.sin(2 * ph)) * np.sin(np.pi * p) ** 1.5
    return y / np.max(np.abs(y))


def saw(freq: float, n: int, harm: int = 16, bright: float = 1.0, phase: float = 0.0,
        decay_per_harm: float = 0.0) -> np.ndarray:
    t = tvec(n)
    out = np.zeros(n)
    hmax = int(min(harm, 15000 // max(freq, 1)))
    for k in range(1, hmax + 1):
        a = 1.0 / k ** bright
        if decay_per_harm:
            out += a * np.sin(2 * np.pi * k * freq * t + k * phase) * np.exp(-t * decay_per_harm * (k - 1))
        else:
            out += a * np.sin(2 * np.pi * k * freq * t + k * phase)
    return out * (2 / np.pi)


def pad_chord(midis: list[int], dur: float, attack: float = 0.6, release: float = 1.2, harm: int = 10,
              bright: float = 1.6) -> np.ndarray:
    n = int((dur + release * 3) * SR)
    t = tvec(n)
    out = np.zeros((2, n))
    for m in midis:
        for pan, cents in ((-0.7, -8.0), (0.0, 0.0), (0.7, 8.0)):
            v = saw(mtof(m) * 2 ** (cents / 1200), n, harm=harm, bright=bright, phase=rng.uniform(0, 2 * np.pi))
            a = (pan + 1) * np.pi / 4
            out += np.vstack([v * np.cos(a), v * np.sin(a)])
    env = np.minimum(1, t / attack) * np.where(t < dur, 1.0, np.exp(-(t - dur) / release))
    return out * env / (len(midis) * 3)


def bass_note(midi: int, dur: float) -> np.ndarray:
    n = int((dur + 0.03) * SR)
    t = tvec(n)
    f = mtof(midi)
    y = 0.8 * saw(f, n, harm=16, bright=1.1, decay_per_harm=8.0) + 0.6 * np.sin(2 * np.pi * f / 2 * t)
    env = np.minimum(1, t / 0.004) * np.where(t < dur, 1.0, np.exp(-(t - dur) * 120))
    return 1.3 * y * env


def pluck(midi: int, dur: float = 0.22) -> np.ndarray:
    n = int(dur * SR)
    t = tvec(n)
    f = mtof(midi)
    y = np.zeros(n)
    for k in range(1, 12, 2):
        if k * f < 15000:
            y += np.sin(2 * np.pi * k * f * t) / k * np.exp(-t * (6 + 5 * k))
    return y * np.minimum(1, t / 0.002) * np.exp(-t * 10)


def heartbeat() -> np.ndarray:
    """Battement grave « lub-dub » : deux chocs sourds, le second plus doux, 0,28 s plus tard."""
    n = int(1.0 * SR)
    t = tvec(n)
    y = np.zeros(n)
    for dt, f0, g in ((0.0, 62.0, 1.0), (0.28, 54.0, 0.72)):
        i = int(dt * SR)
        tt = t[: n - i]
        f = f0 * (1 + 0.6 * np.exp(-tt * 30))
        y[i:] += g * np.sin(2 * np.pi * np.cumsum(f) / SR) * np.minimum(1, tt / 0.006) * np.exp(-tt * 11)
    body = fft_filter(rng.standard_normal(n) * np.exp(-t * 25), hi=180) * 0.8
    y = np.tanh(1.5 * (y + body))
    return y / np.max(np.abs(y))


def shutter() -> np.ndarray:
    """Déclic de diaphragme : deux clics métalliques rapprochés."""
    n = int(0.12 * SR)
    t = tvec(n)
    y = np.zeros(n)
    for dt, g in ((0.0, 1.0), (0.024, 0.6)):
        i = int(dt * SR)
        tt = t[: n - i]
        y[i:] += g * (fft_filter(rng.standard_normal(n - i), lo=2500, hi=11000) * np.exp(-tt * 180)
                      + 0.4 * np.sin(2 * np.pi * 3400 * tt) * np.exp(-tt * 120))
    return y / np.max(np.abs(y))


def sparkle_burst(dur: float = 1.4, count: int = 90) -> np.ndarray:
    """Éclatement scintillant : une pluie de petits tintements (particules)."""
    n = int(dur * SR)
    out = np.zeros((2, n))
    for k in range(count):
        t0 = (k / count) ** 1.8 * dur * 0.7 + rng.uniform(0, 0.02)
        m = float(rng.choice(PENTA)) + 12 * int(rng.integers(0, 2))
        g = 0.9 * (1 - k / count) + 0.1
        tick = glass_tick(mtof(m))
        i0 = int(t0 * SR)
        L = min(len(tick), n - i0)
        a = (rng.uniform(-0.9, 0.9) + 1) * np.pi / 4
        out[0, i0:i0 + L] += g * tick[:L] * np.cos(a)
        out[1, i0:i0 + L] += g * tick[:L] * np.sin(a)
    t = tvec(n)
    out += fft_filter(rng.standard_normal((2, n)) * np.exp(-t * 6), lo=3000, hi=12000) * 0.25
    return out / np.max(np.abs(out))


def reverb_ir(rt60: float = 2.2, length: float = 3.0, predelay: float = 0.02, lo: float = 180, hi: float = 7000) -> np.ndarray:
    n = int(length * SR)
    t = tvec(n)
    ir = rng.standard_normal((2, n)) * np.exp(-6.91 * t / rt60)
    ir = fft_filter(ir, lo=lo, hi=hi)
    ir[:, : int(predelay * SR)] = 0
    return ir / np.sqrt(np.sum(ir ** 2, axis=1, keepdims=True))


def convolve(x: np.ndarray, ir: np.ndarray) -> np.ndarray:
    L = 1 << int(np.ceil(np.log2(x.shape[1] + ir.shape[1])))
    return np.fft.irfft(np.fft.rfft(x, L) * np.fft.rfft(ir, L), L)[:, : x.shape[1] + ir.shape[1] - 1]


def reverse_reverb(dur: float) -> np.ndarray:
    """Aspiration : la réverbération d'un accord et d'un souffle, retournée (monte jusqu'à la coupure)."""
    n = int(dur * SR)
    src = np.zeros((2, n))
    ch = pad_chord(PAD["D"] + [69, 74], 0.25, attack=0.005, release=0.2, harm=16, bright=1.2)
    src[:, :ch.shape[1]] += ch[:, :n] * 1.5
    t = tvec(n)
    src += fft_filter(rng.standard_normal((2, n)) * np.exp(-t * 20), lo=400, hi=9000) * 0.6
    wet = convolve(src, reverb_ir(rt60=dur * 0.9, length=dur, predelay=0.0, lo=120, hi=9000))[:, :n]
    y = wet[:, ::-1] * np.linspace(0.2, 1.0, n) ** 1.5
    y[:, -64:] *= np.linspace(1, 0, 64)
    return y / np.max(np.abs(y))


def tape_stop(x: np.ndarray, t0: float, length: float) -> np.ndarray:
    """Arrêt de bande à partir de t0 : la lecture ralentit jusqu'à zéro en `length` s (hauteur qui chute)."""
    i0 = int(t0 * SR)
    n = int(length * SR)
    out = x.copy()
    tt = np.arange(n) / SR
    pos = t0 + tt - tt ** 2 / (2 * length)
    src = pos * SR
    for c in range(x.shape[0]):
        seg = np.interp(src, np.arange(x.shape[1]), x[c])
        out[c, i0:i0 + n] = seg * np.linspace(1, 0.0, n) ** 0.6
    out[:, i0 + n:] = 0
    return out


def duck_envelope(kick_times: list[float], depth: float = 0.6, release: float = 0.15) -> np.ndarray:
    g = np.ones(N)
    t = tvec(N)
    for k in sorted(kick_times):
        i0 = int(k * SR)
        i1 = min(N, i0 + int(0.5 * SR))
        g[i0:i1] = np.minimum(g[i0:i1], 1 - depth * np.exp(-(t[i0:i1] - k) / release))
    return g


# ------------------------------------------------------------------------------------------------
# Arrangement
# ------------------------------------------------------------------------------------------------

def compose() -> np.ndarray:
    drums, bass, pads, keys, fx = Bus(), Bus(), Bus(), Bus(), Bus()
    rev_send = Bus()
    tail = Bus()          # événements après l'arrêt de bande (apesanteur -> fin), hors traitement de la musique
    K, CL, SN, CH, OH = kick(), clap(), snare(), hat(False), hat(True)
    kicks: list[float] = []
    T_DROP, T_GRAV = EV["drop"], EV["gravity"]

    # === 0-5 s : la graine ==================================================================
    for bar, t0 in enumerate((0.0, 2.0, 4.0)):
        c = CHORDS[bar]
        pc = pad_chord(PAD[c], 2.0, attack=1.2 if bar == 0 else 0.5, release=1.0)
        pads.add(t0, pc, (0.32, 0.4, 0.44)[bar])
        rev_send.add(t0, pc, 0.25)
    fx.add(EV["impact"] - 0.54, fall_whistle(0.54), 0.12, pan=-0.1)
    CI = crystal_impact()
    fx.add(EV["impact"], CI, 0.62)
    rev_send.add(EV["impact"], CI, 0.5)
    SG = stretch_gliss(0.75)                                                           # la goutte s'étire
    keys.add(EV["hatch"] - 0.2, SG, 0.07)
    rev_send.add(EV["hatch"] - 0.2, SG, 0.1)
    for k, t_gem in enumerate(EV["gems"]):                                             # les gemmes germent
        m = (86, 90, 93, 88, 95)[k % 5]
        g = glass_tick(mtof(m))
        keys.add(t_gem, g, 0.22, pan=(m - 90) / 12)
        rev_send.add(t_gem, g, 0.2)
    for k, t_l in enumerate(EV["origine"]):                                            # « L'ORIGINE »
        m = (74, 76, 78, 81, 83, 86, 88, 90, 93)[k % 9]
        g = glass_tick(mtof(m))
        keys.add(t_l, g, 0.09 + 0.01 * k, pan=-0.4 + 0.1 * k)
        rev_send.add(t_l, g, 0.12)
    for step in range(int(3.0 / (BEAT / 2))):                                         # arpège doux 3-5 s
        ts = 3.0 + step * BEAT / 2
        c = chord_at(ts)
        p = pluck(ARP[c][ARP_STEPS[step % 16]], 0.3)
        keys.add(ts, p, 0.05 + 0.02 * step / 6, pan=0.3 if step % 2 else -0.3)
        rev_send.add(ts, p, 0.06)

    # === 5-11,5 s : l'expansion ==============================================================
    fx.add(4.9, whoosh(2.0, 0.62), 0.26)                                               # le recul s'accélère
    for k in range(26):                                                                # sol qui se fend
        tc = EV["fracture"] + (k / 26) ** 1.3 * 0.95 + float(rng.uniform(0, 0.03))
        fx.add(tc, crackle(), 0.16 * (1 - 0.5 * k / 26), pan=float(rng.uniform(-0.8, 0.8)))
    for bar, t0 in ((3, 6.0), (4, 8.0), (5, 10.0)):
        c = CHORDS[bar]
        pc = pad_chord(PAD[c], 2.0, attack=0.3, release=0.8)
        pads.add(t0, pc, 0.42 + 0.04 * (bar - 3))
        rev_send.add(t0, pc, 0.25)
    tom_notes = [45, 47, 50, 52, 54, 57, 59, 62, 64]                                   # vagues d'immeubles
    for k, tb in enumerate(EV["waves"]):
        T = tom(mtof(tom_notes[k % len(tom_notes)]))
        drums.add(tb, T, 0.3 + 0.035 * k, pan=0.25 * ((-1) ** k))
        rev_send.add(tb, T, 0.15)
    for b in range(14, 23):                                                            # grosse caisse 7-11 s
        drums.add(bt(b), K, 0.45 + 0.04 * (b - 14))
        kicks.append(bt(b))
    for b in range(16, 23):                                                            # charleston
        sub = 2 if b < 19 else 4
        for s_ in range(sub):
            drums.add(bt(b) + s_ * BEAT / sub, CH, 0.05 + 0.02 * (s_ % 2 == 0) + 0.004 * (b - 16), pan=0.3)
    for b in range(16, 23):                                                            # basse pulsée
        c = chord_at(bt(b))
        for s_ in range(2):
            bass.add(bt(b) + s_ * BEAT / 2, bass_note(BASS[c] + (12 if s_ else 0), BEAT * 0.4), 0.26 + 0.01 * (b - 16))
    for k, tn in enumerate(EV["net_nodes"]):                                           # nœuds qui s'allument
        m = PENTA[(3 + k) % len(PENTA)] + 12
        keys.add(tn, blip(mtof(m), 0.12), 0.1, pan=float(rng.uniform(-0.6, 0.6)))
        g = glass_tick(mtof(m))
        keys.add(tn, g, 0.05, pan=float(rng.uniform(-0.6, 0.6)))
        rev_send.add(tn, g, 0.06)
    for tp in EV["packets"]:                                                           # paquets de données
        keys.add(tp, blip(mtof(float(rng.choice(PENTA)) + 24), 0.05), 0.028, pan=float(rng.uniform(-0.8, 0.8)))

    def roll(start_beat: float, end_beat: float, step: float, v0: float, v1: float) -> None:
        n = int(round((end_beat - start_beat) / step))
        for i in range(n):
            p = i / max(n - 1, 1)
            drums.add(bt(start_beat + i * step), SN, v0 + (v1 - v0) * p, pan=0.1)
            rev_send.add(bt(start_beat + i * step), SN, 0.1 * (v0 + (v1 - v0) * p))

    roll(19, 21, 0.5, 0.1, 0.2)
    roll(21, 22, 0.25, 0.2, 0.3)
    roll(22, 23, 0.125, 0.3, 0.46)
    fx.add(9.5, riser(T_DROP - 9.5), 0.3)
    dive = whoosh(1.0, 0.84)                                                           # le piqué : culmine
    fx.add(EV["dive_peak"] - 0.84, dive, 0.5)                                          # à la vitesse maximale
    rev_send.add(EV["dive_peak"] - 0.84, dive, 0.15)
    land = whoosh(0.6, 0.7, sweep=False)                                               # la caméra se pose
    fx.add(EV["landing"] - 0.42, land, 0.12)

    # === 12-20 s : le flux ====================================================================
    CR = crash()
    fx.add(T_DROP, CR, 0.28)
    fx.add(T_DROP, impact(), 0.62)
    zap_n = int(0.7 * SR)                                                              # les néons s'allument
    zt = tvec(zap_n)
    zap = fft_filter(rng.standard_normal(zap_n), lo=2000, hi=9000) * np.exp(-zt * 5) * (0.6 + 0.4 * np.sin(2 * np.pi * 50 * zt))
    fx.add(T_DROP, zap / np.max(np.abs(zap)), 0.08)
    for b in range(24, 40):
        tb = bt(b)
        drums.add(tb, K, 0.86)
        kicks.append(tb)
        if b % 2 == 1:
            drums.add(tb, CL, 0.4)
            rev_send.add(tb, CL, 0.25)
        sub = 4 if b < 32 else 8                                                       # accélération
        if b >= 38:
            sub = 12
        for s_ in range(sub):
            v = (0.08, 0.05, 0.1, 0.05)[s_ % 4] * (1.0 if sub == 4 else 0.8)
            drums.add(tb + s_ * BEAT / sub, CH, v, pan=0.3 if s_ % 2 else -0.15)
        drums.add(tb + BEAT / 2, OH, 0.08, pan=-0.3)
        c = chord_at(tb)
        pat = (0, 12, 0, 7)
        for s_ in range(4):
            bass.add(tb + s_ * BEAT / 4, bass_note(BASS[c] + pat[s_], BEAT / 4 * 0.8), 0.3)
    for bar in range(6, 10):
        c = CHORDS[bar]
        pc = pad_chord(PAD[c], 2.0, attack=0.05, release=0.5, harm=14, bright=1.3)
        pads.add(bar * 2.0, pc, 0.42)
        rev_send.add(bar * 2.0, pc, 0.2)
    t = T_DROP
    step = 0
    while t < T_GRAV - 1e-9:                                                           # arpège qui accélère
        div = 4 if t < 16.0 else 6 if t < 18.0 else 8
        c = chord_at(t)
        m = ARP[c][ARP_STEPS[step % 16]] + (12 if (step % 8 == 7 and t >= 16.0) else 0)
        p = pluck(m, 0.18)
        keys.add(t, p, 0.075, pan=-0.35 if step % 2 else 0.35)
        keys.add(t + 0.375, p, 0.075 * 0.35, pan=0.35 if step % 2 else -0.35)
        rev_send.add(t, p, 0.03)
        t += BEAT / div
        step += 1
    for t_rev, t_pass in zip(EV["word_reveals"], EV["word_passes"]):
        # apparition : claquement accordé sur l'image où le mot surgit
        fx.add(t_rev, impact(1.0), 0.26)
        stab = pad_chord([m + 12 for m in PAD[chord_at(t_rev)][:4]], 0.12, attack=0.004, release=0.18, harm=14, bright=1.1)
        keys.add(t_rev, stab, 0.5)
        rev_send.add(t_rev, stab, 0.35)
        # passage : le souffle culmine à l'image exacte où le mot traverse la caméra, puis s'éteint net
        wh = whoosh(0.8, 0.9, sweep=False)
        fx.add(t_pass - 0.9 * 0.8, wh, 0.42)
        fx.add(t_pass, crash(0.6), 0.06)
    for k, m in enumerate((81, 86, 90)):                                               # arrêt devant l'arbre
        b_ = bell(mtof(m), 2.6, 0.9)
        keys.add(EV["stop"] + 0.04 * k, b_, 0.1, pan=-0.3 + 0.3 * k)
        rev_send.add(EV["stop"] + 0.04 * k, b_, 0.2)
    fx.add(EV["stop"], riser(T_GRAV - EV["stop"], 500, 14000), 0.24)                  # montée (dronie)

    # === 20-24 s : le morphing (après l'arrêt de bande) ========================================
    air_n = int(1.2 * SR)
    air = fft_filter(rng.standard_normal((2, air_n)), lo=600, hi=5000) * np.sin(np.pi * tvec(air_n) / 1.2)[None, :]
    tail.add(T_GRAV + 0.05, air / np.max(np.abs(air)), 0.05)
    drone = pad_chord([26, 38, 45], EV["blob"] - (T_GRAV + 0.2), attack=1.5, release=1.0, harm=6, bright=2.0)
    tail.add(T_GRAV + 0.2, fft_filter(drone, hi=260), 0.55)
    SB = sparkle_burst()
    tail.add(EV["burst"], SB, 0.42)
    t_rr = EV["burst"] + 0.05
    RR = reverse_reverb(EV["suction_end"] - t_rr)
    tail.add(t_rr, RR, 0.62)
    tail.add(EV["suction_end"], blip(mtof(74), 0.15), 0.06)                            # l'œil se forme
    HB = heartbeat()
    SH = shutter()
    for tb in EV["heartbeats"]:
        tail.add(tb, HB, 0.95)
        tail.add(tb + 0.01, SH, 0.1, pan=0.2)

    # === 24-31 s : l'outro ======================================================================
    tail.add(EV["blink"], blip(mtof(86), 0.12), 0.08)                                  # clin d'œil
    tail.add(EV["blink"] + 0.12, blip(mtof(90), 0.12), 0.06)
    tail.add(EV["close"], blip(mtof(81), 0.2), 0.05)
    swell_n = int(1.4 * SR)
    tt = tvec(swell_n)
    swell = np.sin(2 * np.pi * np.cumsum(55 + 40 * tt / 1.4) / SR) * np.sin(np.pi * tt / 1.4) ** 2
    tail.add(EV["blob"] - 0.3, swell, 0.35)
    final = pad_chord(PAD["Dsus"] + [76], DUR - EV["blob"] - 0.4, attack=0.9, release=1.6, harm=12, bright=1.5)
    tail.add(EV["blob"], final, 0.62)
    for k, m in enumerate((86, 88, 90, 93)):
        b_ = bell(mtof(m), 4.0, 0.8)
        tail.add(EV["blob"] + 0.15 + 0.18 * k, b_, 0.09, pan=-0.3 + 0.2 * k)
    for k, tl in enumerate(EV["slogan"]):                                              # slogan lettre à lettre
        m = PENTA[min(k // 2, len(PENTA) - 1)]
        tail.add(tl, glass_tick(mtof(m + 12)), 0.035)
    tail.add(EV["slogan"][-1] + 0.35, bell(mtof(98), 3.0, 1.0), 0.12)

    # --- traitements ---------------------------------------------------------------------------
    duck = duck_envelope([k for k in kicks if k >= 7.0], depth=0.55, release=0.14)
    bass.x *= duck
    pads.x *= 0.4 + 0.6 * duck
    keys.x *= 0.7 + 0.3 * duck

    music = drums.x + bass.x + pads.x + keys.x
    ir = reverb_ir()
    wet = convolve(rev_send.x + 0.2 * keys.x, ir)[:, :N]

    # Ouverture du filtre : nappe voilée au début, pleine brillance à l'allumage des néons.
    def cutoff(t: float) -> float:
        if t < 1.0:
            return 900.0
        if t < T_DROP:
            p = (t - 1.0) / (T_DROP - 1.0)
            return 900.0 * (18000.0 / 900.0) ** (p ** 1.4)
        return 18000.0

    music = stft_lowpass(music, cutoff)
    # la musique se retient pendant la chute libre du piqué (fondus courts, pas de coupure sèche)
    tv = tvec(N)
    hold = np.interp(tv, [11.46, 11.54, T_DROP - 0.02, T_DROP], [1.0, 0.08, 0.08, 1.0])
    music *= hold[None, :]
    wet *= (0.5 + 0.5 * hold)[None, :]
    mix = music + 0.5 * wet + fx.x
    # apesanteur : arrêt de bande sur tout ce qui joue, puis la queue (morphing, outro)
    mix = tape_stop(mix, T_GRAV - 0.02, 0.42)
    tail_wet = convolve(tail.x, reverb_ir(rt60=3.2, length=4.0, predelay=0.03, lo=150, hi=8000))[:, :N]
    mix = mix + tail.x + 0.4 * tail_wet
    fade = np.ones(N)
    i0 = int((DUR - 1.0) * SR)
    fade[i0:] = np.linspace(1, 0, N - i0) ** 1.5
    mix *= fade
    mix = np.tanh(1.2 * mix / np.max(np.abs(mix)))
    mix *= 10 ** (-1 / 20) / np.max(np.abs(mix))
    return mix


def write_wav(path: Path, x: np.ndarray) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    q = np.ascontiguousarray(np.clip(np.round(x.T * (2 ** 23 - 1)), -(2 ** 23), 2 ** 23 - 1).astype("<i4"))
    b = q.view(np.uint8).reshape(-1, 4)[:, :3].tobytes()
    with wave.open(str(path), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(3)
        w.setframerate(SR)
        w.writeframes(b)


def main() -> None:
    mix = compose()
    write_wav(OUT, mix)
    rms = np.sqrt(np.mean(mix ** 2))
    print(f"{OUT} : {DUR:.1f} s, {SR} Hz, stéréo 24 bits, crête {20 * np.log10(np.max(np.abs(mix))):.1f} dBFS, "
          f"RMS {20 * np.log10(rms):.1f} dBFS")
    for name, (a, b) in {"graine": (0, 5), "expansion": (5, 11.5), "piqué": (11.5, 12), "flux": (12, 20),
                         "aspiration": (21, 23.2), "cœur": (23.5, 25.75), "outro": (25.75, 31)}.items():
        seg = mix[:, int(a * SR):int(b * SR)]
        print(f"  {name:<10} {a:>5.2f}-{b:<5.2f} s : RMS {20 * np.log10(np.sqrt(np.mean(seg ** 2)) + 1e-9):6.1f} dBFS")


if __name__ == "__main__":
    main()
