"""Musique originale de la publicité fictive ONDÉA One : 30 s, 120 BPM, fa mineur (libre de droits).

    .venv\\Scripts\\python.exe scripts\\musique_ondea_one.py        -> assets/audio/ondea_one_musique.wav

Entièrement synthétisée (numpy) et déterministe : relancer le script redonne le même fichier au bit
près. WAV 48 kHz, stéréo, 24 bits. La scène scenes/pub_ondea_one.json s'y cale (bpm 120, offset 0,
marqueurs « drop » 8,0 s et « final » 26,0 s).

Structure (mesures de 2 s, grille Fm - Db - Ab - Eb) :
  1-2   intro étouffée (filtre fermé), whooshs sur les temps 2, 4, 6 (mouvements fouettés de l'image)
  3-4   montée : le filtre s'ouvre, roulement de caisse claire qui accélère, riser, silence à 7,75 s
  5-10  drop à 8,0 s : groove complet, basse pompée, nappes, accords en contretemps, arpège, break 19,5 s
  11-12 pause (packshot) : demi-temps, nappes et arpège
  13    remontée : roulement, riser, cymbale inversée
  14-15 coup final à 26,0 s, accord tenu et queue de réverbération jusqu'à 30 s
"""

from __future__ import annotations

import wave
from pathlib import Path

import numpy as np

SR = 48000
BPM = 120
BEAT = 60 / BPM
DUR = 30.0
N = int(DUR * SR)
ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "assets" / "audio" / "ondea_one_musique.wav"
rng = np.random.default_rng(20260924)

# Accord de chaque mesure (15 mesures de 2 s) et voicings (MIDI).
CHORDS = ["Fm", "Db", "Ab", "Eb", "Fm", "Db", "Ab", "Eb", "Fm", "Db", "Ab", "Eb", "Eb", "Fm", "Fm"]
PAD = {"Fm": [53, 56, 60, 65], "Db": [53, 56, 61, 65], "Ab": [51, 56, 60, 63], "Eb": [51, 55, 58, 63]}
BASS = {"Fm": 41, "Db": 37, "Ab": 32, "Eb": 39}
ARP = {"Fm": [72, 77, 80, 84], "Db": [73, 77, 80, 85], "Ab": [72, 75, 80, 84], "Eb": [70, 75, 79, 82]}
ARP_STEPS = [0, 2, 1, 3, 2, 1, 3, 2, 0, 2, 1, 3, 2, 3, 1, 2]


def mtof(m: float) -> float:
    return 440.0 * 2 ** ((m - 69) / 12)


def bt(beat: float) -> float:
    """Temps (s) d'un temps musical."""
    return beat * BEAT


def tvec(n: int) -> np.ndarray:
    return np.arange(n) / SR


# ------------------------------------------------------------------------------------------------
# Filtres (FFT, phase nulle) et bus stéréo
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
    """Passe-bas variable dans le temps (cutoff_at(t) -> Hz), fenêtre racine de Hann, 75 % de recouvrement."""
    win = np.sqrt(np.hanning(nfft + 1)[:-1])
    pad = np.pad(x, ((0, 0), (nfft, nfft)))
    n_frames = (pad.shape[1] - nfft) // hop + 1
    f = np.fft.rfftfreq(nfft, 1 / SR)
    out = np.zeros_like(pad)
    for c in range(pad.shape[0]):
        idx = np.arange(nfft)[None, :] + hop * np.arange(n_frames)[:, None]
        frames = pad[c][idx] * win
        spec = np.fft.rfft(frames, axis=1)
        centers = (hop * np.arange(n_frames) + nfft / 2 - nfft) / SR
        fc = np.maximum(np.array([cutoff_at(t) for t in centers]), 20.0)
        g = 1 / np.sqrt(1 + (f[None, :] / fc[:, None]) ** 8)
        y = np.fft.irfft(spec * g, n=nfft, axis=1) * win
        np.add.at(out[c], idx, y)
    return out[:, nfft:nfft + x.shape[1]] / 2.0  # somme des fenêtres de Hann à 75 % = 2


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


# ------------------------------------------------------------------------------------------------
# Instruments
# ------------------------------------------------------------------------------------------------

def kick(dur: float = 0.5) -> np.ndarray:
    n = int(dur * SR)
    t = tvec(n)
    f0, f1, k = 160.0, 46.0, 30.0
    body = np.sin(2 * np.pi * (f1 * t + (f0 - f1) / k * (1 - np.exp(-k * t)))) * np.exp(-6.5 * t)
    click = fft_filter(rng.standard_normal(n) * np.exp(-t * 500), lo=1500) * 0.35
    y = np.tanh(1.8 * (body + click)) / np.tanh(1.8)
    y[:48] *= np.linspace(0, 1, 48)
    return y


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
    y += 0.6 * np.sin(2 * np.pi * 185 * t) * np.exp(-t * 38)
    return y / np.max(np.abs(y))


def hat(open_: bool = False) -> np.ndarray:
    dur = 0.32 if open_ else 0.06
    n = int(dur * SR)
    t = tvec(n)
    y = fft_filter(rng.standard_normal(n), lo=7500) * np.exp(-t * (11 if open_ else 75))
    return y / np.max(np.abs(y))


def crash(dur: float = 2.6) -> np.ndarray:
    n = int(dur * SR)
    t = tvec(n)
    y = fft_filter(rng.standard_normal((2, n)), lo=3800, hi=15000) * np.exp(-t * 2.0)
    return y / np.max(np.abs(y))


def impact(dur: float = 2.6) -> np.ndarray:
    n = int(dur * SR)
    t = tvec(n)
    f = 36 + 70 * np.exp(-t * 8)
    boom = np.sin(2 * np.pi * np.cumsum(f) / SR) * np.exp(-t * 2.0)
    nz = fft_filter(rng.standard_normal(n), hi=2200) * np.exp(-t * 10) * 0.6
    y = np.tanh(1.6 * (boom + nz))
    return y / np.max(np.abs(y))


def riser(dur: float, f_lo: float = 300.0, f_hi: float = 14000.0) -> np.ndarray:
    """Bruit dont le filtre s'ouvre + sinus qui monte ; intensité croissante (fin abrupte)."""
    n = int(dur * SR)
    t = tvec(n)
    p = t / dur
    noise = stft_lowpass(rng.standard_normal((2, n)), lambda s: f_lo * (f_hi / f_lo) ** min(max(s / dur, 0), 1))
    tone_f = 180 * (8 ** p)
    tone = np.sin(2 * np.pi * np.cumsum(tone_f) / SR) * 0.35
    y = noise + tone[None, :]
    y *= (p ** 2.2)[None, :]
    y[:, -96:] *= np.linspace(1, 0, 96)
    return y / np.max(np.abs(y))


def whoosh(dur: float = 0.42) -> np.ndarray:
    """Souffle qui culmine à 80 % de sa durée et traverse de gauche à droite."""
    n = int(dur * SR)
    t = tvec(n)
    p = t / dur
    env = np.where(p < 0.8, (p / 0.8) ** 2, np.exp(-(p - 0.8) * 25))
    y = stft_lowpass(rng.standard_normal((2, n)), lambda s: 600 + 9000 * np.sin(np.pi * min(max(s / dur, 0), 1)))
    y = y.mean(axis=0) * env
    a = (np.clip(p * 1.2 - 0.1, 0, 1) * 2 - 1 + 1) * np.pi / 4
    st = np.vstack([y * np.cos(a), y * np.sin(a)])
    return st / np.max(np.abs(st))


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


def bass_note(midi: int, dur: float) -> np.ndarray:
    n = int((dur + 0.03) * SR)
    t = tvec(n)
    f = mtof(midi)
    y = 0.8 * saw(f, n, harm=18, bright=1.1, decay_per_harm=7.0) + 0.55 * np.sin(2 * np.pi * f / 2 * t)
    env = np.minimum(1, t / 0.004) * np.where(t < dur, 1.0, np.exp(-(t - dur) * 120))
    return 1.4 * y * env


def pad_chord(midis: list[int], dur: float, attack: float = 0.25, release: float = 0.7, harm: int = 14,
              bright: float = 1.35) -> np.ndarray:
    n = int((dur + release) * SR)
    t = tvec(n)
    out = np.zeros((2, n))
    for m in midis:
        for pan, cents in ((-0.7, -9.0), (0.0, 0.0), (0.7, 9.0)):
            v = saw(mtof(m) * 2 ** (cents / 1200), n, harm=harm, bright=bright, phase=rng.uniform(0, 2 * np.pi))
            a = (pan + 1) * np.pi / 4
            out += np.vstack([v * np.cos(a), v * np.sin(a)])
    env = np.minimum(1, t / attack) * np.where(t < dur, 1.0, np.exp(-(t - dur) * 6))
    return out * env / (len(midis) * 3)


def stab(midis: list[int], dur: float = 0.16) -> np.ndarray:
    n = int((dur + 0.2) * SR)
    t = tvec(n)
    out = np.zeros((2, n))
    for m in midis:
        for pan, cents in ((-0.5, -7.0), (0.5, 7.0)):
            v = saw(mtof(m + 12) * 2 ** (cents / 1200), n, harm=20, bright=1.0, decay_per_harm=5.0)
            a = (pan + 1) * np.pi / 4
            out += np.vstack([v * np.cos(a), v * np.sin(a)])
    env = np.minimum(1, t / 0.003) * np.exp(-t * 11)
    return out * env / (len(midis) * 2)


def pluck(midi: int, dur: float = 0.24) -> np.ndarray:
    n = int(dur * SR)
    t = tvec(n)
    f = mtof(midi)
    y = np.zeros(n)
    for k in range(1, 14, 2):
        if k * f < 15000:
            y += np.sin(2 * np.pi * k * f * t) / k * np.exp(-t * (6 + 4 * k))
    return y * np.minimum(1, t / 0.002) * np.exp(-t * 9)


def reverb_ir(rt60: float = 1.9, length: float = 2.6, predelay: float = 0.022) -> np.ndarray:
    n = int(length * SR)
    t = tvec(n)
    ir = rng.standard_normal((2, n)) * np.exp(-6.91 * t / rt60)
    ir = fft_filter(ir, lo=180, hi=6500)
    ir[:, : int(predelay * SR)] = 0
    return ir / np.sqrt(np.sum(ir ** 2, axis=1, keepdims=True))


def convolve(x: np.ndarray, ir: np.ndarray) -> np.ndarray:
    L = 1 << int(np.ceil(np.log2(x.shape[1] + ir.shape[1])))
    return np.fft.irfft(np.fft.rfft(x, L) * np.fft.rfft(ir, L), L)[:, : x.shape[1]]


def duck_envelope(kick_times: list[float], depth: float = 0.72, release: float = 0.16) -> np.ndarray:
    """Pompage (sidechain) : gain ramené à 1 - depth à chaque grosse caisse, remontée exponentielle."""
    g = np.ones(N)
    t = tvec(N)
    for k in sorted(kick_times):
        i0 = int(k * SR)
        i1 = min(N, i0 + int(0.5 * SR))
        seg = 1 - depth * np.exp(-(t[i0:i1] - k) / release)
        g[i0:i1] = np.minimum(g[i0:i1], seg)
    return g


# ------------------------------------------------------------------------------------------------
# Arrangement
# ------------------------------------------------------------------------------------------------

def compose() -> np.ndarray:
    drums, bass, pads, keys, fx = Bus(), Bus(), Bus(), Bus(), Bus()
    rev_send = Bus()
    K, CL, SN, CH, OH = kick(), clap(), snare(), hat(False), hat(True)
    kicks: list[float] = []

    def chord(bar: int) -> str:
        return CHORDS[min(bar, len(CHORDS) - 1)]

    # --- Batterie ---------------------------------------------------------------------------
    for b in range(0, 60):
        tb = bt(b)
        if b < 8:                                   # intro : grosse caisse sourde, charleston discret
            drums.add(tb, K, 0.55)
            kicks.append(tb)
            drums.add(tb + BEAT / 2, CH, 0.10, pan=0.3)
        elif b < 15:                                # montée
            drums.add(tb, K, 0.8)
            kicks.append(tb)
            for s in range(4):
                drums.add(tb + s * BEAT / 4, CH, 0.07 + 0.03 * (s == 2), pan=0.25)
            if b in (9, 11):
                drums.add(tb, CL, 0.42)
                rev_send.add(tb, CL, 0.25)
        elif 16 <= b < 40:                          # drop
            drums.add(tb, K, 0.85)
            kicks.append(tb)
            if b % 2 == 1:
                drums.add(tb, CL, 0.42)
                drums.add(tb, SN, 0.18)
                rev_send.add(tb, CL, 0.3)
            for s in range(4):
                vel = (0.10, 0.06, 0.12, 0.07)[s]
                drums.add(tb + s * BEAT / 4, CH, vel, pan=0.3 if s % 2 else -0.15)
            drums.add(tb + BEAT / 2, OH, 0.11, pan=-0.3)
        elif 40 <= b < 48:                          # pause : demi-temps
            if b % 2 == 0:
                drums.add(tb, K, 0.75)
                kicks.append(tb)
            if b % 4 == 2:
                drums.add(tb, CL, 0.45)
                rev_send.add(tb, CL, 0.35)
            drums.add(tb + BEAT / 2, CH, 0.09, pan=0.3)
        elif 48 <= b < 52:                          # remontée
            drums.add(tb, K, 0.85)
            kicks.append(tb)
            drums.add(tb + BEAT / 2, OH, 0.08, pan=-0.3)

    # Roulements de caisse claire (croches, doubles, triples) et break du drop.
    def roll(start_beat: float, end_beat: float, step: float, v0: float, v1: float) -> None:
        n = int(round((end_beat - start_beat) / step))
        for i in range(n):
            p = i / max(n - 1, 1)
            drums.add(bt(start_beat + i * step), SN, v0 + (v1 - v0) * p, pan=0.1)
            rev_send.add(bt(start_beat + i * step), SN, 0.12 * (v0 + (v1 - v0) * p))

    roll(12, 14, 0.5, 0.18, 0.3)
    roll(14, 15, 0.25, 0.3, 0.42)
    roll(15, 15.5, 0.125, 0.42, 0.55)
    roll(39, 40, 0.25, 0.3, 0.5)
    roll(48, 50, 0.5, 0.2, 0.32)
    roll(50, 51, 0.25, 0.32, 0.45)
    roll(51, 52, 0.125, 0.45, 0.6)

    # Cymbales, impacts, cymbales inversées, riser, whooshs.
    CR = crash()
    fx.add(bt(16), CR, 0.32)
    fx.add(bt(32), CR, 0.24)
    fx.add(bt(52), CR, 0.36)
    IM = impact()
    fx.add(bt(16), IM, 0.75)
    fx.add(bt(52), IM, 0.85)
    fx.add(bt(20), IM[: int(1.2 * SR)] * np.exp(-tvec(int(1.2 * SR)) * 3), 0.35)   # coup sur le nom
    rev = crash(1.6)[:, ::-1]
    fx.add(bt(16) - rev.shape[1] / SR, rev, 0.3)
    fx.add(bt(52) - rev.shape[1] / SR, rev, 0.32)
    fx.add(bt(8), riser(bt(15.5) - bt(8)), 0.34)
    fx.add(bt(48), riser(bt(52) - bt(48) - 0.02, 400, 12000), 0.3)
    fx.add(bt(36), riser(bt(40) - bt(36), 800, 9000), 0.16)
    WH = whoosh()
    for b in (2, 4, 6, 30, 32, 34):                 # mouvements fouettés de la caméra
        fx.add(bt(b) - 0.8 * WH.shape[1] / SR, WH, 0.24)
    fx.add(bt(24) - 0.34, WH, 0.18)                 # entrée des chiffres

    # --- Basse ------------------------------------------------------------------------------
    for b in range(8, 52):
        c = chord(int(bt(b) // 2))
        root = BASS[c]
        if b < 16:
            if b < 15:
                bass.add(bt(b), bass_note(root, BEAT * 0.45), 0.34)
                bass.add(bt(b) + BEAT / 2, bass_note(root, BEAT * 0.45), 0.26)
        elif b < 40:
            pat = (0, 0, 12, 0)
            for s in range(2):
                note = root + pat[(2 * b + s) % 4]
                bass.add(bt(b) + s * BEAT / 2, bass_note(note, BEAT * 0.46), 0.36)
        elif b < 48:
            if b % 2 == 0:
                bass.add(bt(b), bass_note(root, BEAT * 1.9), 0.33)
        else:
            bass.add(bt(b), bass_note(root, BEAT * 0.45), 0.32)
            bass.add(bt(b) + BEAT / 2, bass_note(root, BEAT * 0.45), 0.28)
    bass.add(bt(52), bass_note(BASS["Fm"], 2.4), 0.4)

    # --- Nappes, accords en contretemps, arpège -----------------------------------------------
    for bar in range(15):
        c = CHORDS[bar]
        t0 = bar * 2.0
        if bar in (13, 14):
            continue
        gain = {0: 0.5, 1: 0.52, 2: 0.6, 3: 0.68}.get(bar, 0.6)
        if 10 <= bar <= 12:
            gain = 0.68
        pc = pad_chord(PAD[c], 2.0)
        pads.add(t0, pc, gain)
        rev_send.add(t0, pc, gain * 0.4)
    final = pad_chord(PAD["Fm"], 2.6, attack=0.02, release=1.4, harm=22, bright=1.1)
    pads.add(bt(52), final, 0.95)
    rev_send.add(bt(52), final, 0.5)

    for b in range(16, 40):
        c = chord(int(bt(b) // 2))
        keys.add(bt(b) + BEAT / 2, stab(PAD[c]), 0.2)
    for step in range(0, int(round(26.0 / (BEAT / 4)))):   # doubles croches de 0 à 26 s
        tsec = step * BEAT / 4
        bar = int(tsec // 2)
        c = CHORDS[bar]
        m = ARP[c][ARP_STEPS[step % 16]]
        if tsec < 8.0:
            if tsec >= bt(15.5):
                continue
            vel = 0.08 + 0.04 * (tsec / 8.0)
        elif tsec < 20.0:
            vel = 0.1 + (0.03 if step % 4 == 0 else 0.0)
        else:
            vel = 0.08
        p = pluck(m)
        pan = -0.35 if step % 2 else 0.35
        keys.add(tsec, p, vel, pan=pan)
        for e in range(1, 4):                       # écho ping-pong à la croche pointée
            keys.add(tsec + e * 0.375, p, vel * 0.42 ** e, pan=-pan if e % 2 else pan)
        rev_send.add(tsec, p, vel * 0.35)
    for i, m in enumerate([84, 80, 77, 72, 68, 65]):   # arpège final descendant
        keys.add(bt(52) + 0.25 + i * 0.25, pluck(m, 0.6), 0.1 - 0.012 * i, pan=0.3 * (-1) ** i)
        rev_send.add(bt(52) + 0.25 + i * 0.25, pluck(m, 0.6), 0.06)

    # --- Traitements --------------------------------------------------------------------------
    duck = duck_envelope([k for k in kicks if k >= bt(8)], depth=0.7, release=0.15)
    bass.x *= duck
    pads.x *= 0.35 + 0.65 * duck
    keys.x *= 0.6 + 0.4 * duck

    music = drums.x + bass.x + pads.x + keys.x
    wet = convolve(rev_send.x + 0.25 * keys.x, reverb_ir())

    # Intro étouffée puis ouverture du filtre pendant la montée ; silence avant le drop.
    def cutoff(t: float) -> float:
        if t < bt(8):
            return 750.0
        if t < bt(15.5):
            p = (t - bt(8)) / (bt(15.5) - bt(8))
            return 750.0 * (20000.0 / 750.0) ** (p ** 1.6)
        return 20000.0

    music = stft_lowpass(music, cutoff)
    gap = (tvec(N) >= bt(15.5)) & (tvec(N) < bt(16))
    music[:, gap] *= 0.0
    wet[:, gap] *= 0.25

    mix = music + 0.55 * wet + fx.x
    # Queue finale : fondu des 0,6 dernières secondes.
    fade = np.ones(N)
    i0 = int((DUR - 0.6) * SR)
    fade[i0:] = np.linspace(1, 0, N - i0) ** 2
    mix *= fade
    # Bus maître : saturation douce puis crête à -1 dBFS (la normalisation EBU R128 est faite à l'encodage).
    mix = np.tanh(1.25 * mix / np.max(np.abs(mix)))
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
    for name, (a, b) in {"intro": (0, 4), "montée": (4, 7.75), "silence": (7.75, 8), "drop": (8, 20),
                         "pause": (20, 24), "remontée": (24, 26), "final": (26, 30)}.items():
        seg = mix[:, int(a * SR):int(b * SR)]
        print(f"  {name:<9} {a:>5.2f}-{b:<5.2f} s : RMS {20 * np.log10(np.sqrt(np.mean(seg ** 2)) + 1e-9):6.1f} dBFS")


if __name__ == "__main__":
    main()
