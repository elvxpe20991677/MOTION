"""Génère la scène « Éclosion Urbaine » (film concept, ~31 s) : scenes/eclosion_urbaine.json.

    .venv\\Scripts\\python.exe scripts\\scene_eclosion_urbaine.py            -> scène + dégradé SVG
    .venv\\Scripts\\python.exe scripts\\scene_eclosion_urbaine.py --report   -> + contrôles (trajets, collisions)

Le JSON est trop volumineux pour être écrit à la main (ville de ~1 100 objets, ~2 000 particules,
caméra clé par clé, calques 2D calés sur la projection 3D) : ce script en est la SOURCE, toute
retouche se fait ici puis on régénère. Déterministe (graines fixes) : relancé, il redonne le même
fichier au bit près. La musique (scripts/musique_eclosion_urbaine.py) lit ses repères dans
compute_events() : image et son sont calés sur les mêmes instants.

Plans (24 i/s, 120 BPM). De 0 à 24 s, la caméra est UN SEUL plan-séquence (focale 28 mm) : les
coupes entre plaques tombent sur des images identiques et ne se voient pas.
  graine     0-5 s     la graine d'argile tombe, s'étire et se fige en tronc à facettes, la cime germe ;
                       ondes, réticule, graphiques, « L'ORIGINE » le long de la branche ; léger recul
  expansion  5-11,5 s  recul en diagonale vers une vue plongeante ; le sol se fend, la ville pousse comme
                       un graphique ; un réseau de données s'allume depuis l'arbre (paquets, pings, étiquettes)
  flux       11,5-20 s le drone marque un temps puis pique dans la ville (drop à 12 s, les néons s'allument),
                       file dans une grande rue droit vers l'arbre ; SYNERGIE, DURABLE, CONNECTÉ claquent sur
                       les temps et s'étirent au passage ; arrêt au carrefour, montée et recul qui révèlent la ville
  morphing   20-24 s   apesanteur, explosion en ~2 000 particules, orbite de 180°, œil vectoriel
  outro      24-31 s   dernier battement, clin d'œil, forme douce qui ondule, « Visionnez Demain. »

Conventions : temps GLOBAUX (s) dans ce script, convertis en temps locaux au plan à l'écriture.
Caméra : une clé par image, interpolation linéaire (allowLinear) : le trajet est exactement celui
calculé ici, ce qui permet de projeter les points 3D (formule de Blender : capteur 36 mm ajusté en
largeur, contrainte TRACK_TO, haut = +Z) et d'y caler les calques 2D.
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
SCENE_ID = "eclosion_urbaine"
SCENE_PATH = ROOT / "scenes" / f"{SCENE_ID}.json"
GRADIENT_REL = "assets/images/eclosion_degrade_peche.svg"

FPS = 24
W, H = 1920, 1080
SENSOR = 36.0
BPM = 120
SHOTS = {"graine": (0.0, 5.0), "expansion": (5.0, 11.5), "flux": (11.5, 20.0),
         "morphing": (20.0, 24.0), "outro": (24.0, 31.0)}
T_END = 31.0
LENS_3D = 28.0

# Repères (secondes globales), partagés avec la musique par compute_events().
T_IMPACT = 1.0
T_PULL = 3.3          # la caméra quitte l'arbre
T_SWOOP = 11.0        # fin du recul, le drone pique
T_DROP = 12.0         # entrée dans la rue, les néons s'allument
T_GLIDE = 12.8        # posé dans la rue : vol stable
T_DEC = 17.0          # freinage avant le carrefour
T_STOP = 18.0         # arrêt au carrefour, montée et recul
T_GRAVITY = 20.0      # la ville perd sa gravité
T_ORBIT = 21.0        # orbite de 180° autour de l'œil
T_EYE = 23.2          # l'œil vectoriel prend le relais
HEARTBEATS = [23.5, 24.5]
T_BLINK = 24.9
T_CLOSE = 25.45
T_BLOB = 25.75
T_SLOGAN = 26.9
NET_T0 = 8.5          # le réseau s'allume depuis l'arbre (temps 17)

# Couleurs (sRGB). Vue « Standard » (couleurs exactes, pastels et néons vifs ; AgX les délavait) et
# plaques TRANSPARENTES : le bleu du fond est celui des calques 2D (BG), identique d'un plan à l'autre ;
# le sol (albédo SOL) est calibré pour rendre ce même bleu sous la lumière des plaques (horizon invisible).
DEEP = "#1D2B7B"
INK = "#1B2466"
MINT = "#5EE3B8"
MINT_DARK = "#2FBF95"
MINT_LIGHT = "#A8F0D8"
CORAL = "#FF7A68"
YELLOW = "#FFE033"
WHITE = "#FFFFFF"
BG_RENDERED = "#DAE7FB"
SOL = "#D3DFEB"   # mesuré : rend #DAE7FB (±1) sous le soleil et le monde des plaques
WORLD = {"color": BG_RENDERED, "strength": 0.8}

# id, texte, apparition (sur un temps), passage = dernière image visible (sur un temps), couleur
WORDS = [("synergie", "SYNERGIE", 12.5, 14.0, YELLOW),
         ("durable", "DURABLE", 14.04, 15.5, MINT),
         ("connecte", "CONNECTÉ", 15.54, 17.0, CORAL)]

rng_city = np.random.default_rng(250925)
rng_fx = np.random.default_rng(92025)
rng_detail = np.random.default_rng(51025)
rng_net = np.random.default_rng(3107)

EVENTS: dict = {}


# =====================================================================================================
# Outils
# =====================================================================================================

def r6(x: float) -> float:
    v = round(float(x), 6)
    return 0.0 if v == 0 else v


def r4(x: float) -> float:
    v = round(float(x), 4)
    return 0.0 if v == 0 else v


def r2(x: float) -> float:
    v = round(float(x), 2)
    return 0.0 if v == 0 else v


def vec(a) -> list[float]:
    return [r4(x) for x in a]


def local(shot: str, t: float) -> float:
    return r6(t - SHOTS[shot][0])


def frames_between(t0: float, t1: float) -> list[float]:
    """Instants des images k/FPS dans [t0, t1] (bornes incluses)."""
    k0 = math.ceil(t0 * FPS - 1e-6)
    k1 = math.floor(t1 * FPS + 1e-6)
    return [k / FPS for k in range(k0, k1 + 1)]


def smootherstep(a: float, b: float, x: float) -> float:
    """Quintique : vitesse ET accélération nulles aux deux bords (départ et arrivée sans à-coup)."""
    p = min(max((x - a) / (b - a), 0.0), 1.0)
    return p * p * p * (10 - 15 * p + 6 * p * p)


def smoothstep(a: float, b: float, x: float) -> float:
    if b == a:
        return 1.0 if x >= b else 0.0
    p = min(max((x - a) / (b - a), 0.0), 1.0)
    return p * p * (3 - 2 * p)


def ease(name: str, p: float) -> float:
    """Courbes GSAP (mêmes formules que Blender pour les familles utilisées ici)."""
    p = min(max(p, 0.0), 1.0)
    if name in ("none", "linear"):
        return p
    if name.startswith("back.out"):
        s = float(name[9:-1]) if "(" in name else 1.70158
        q = p - 1
        return 1 + (s + 1) * q ** 3 + s * q ** 2
    fam, d = name.split(".")
    if fam == "sine":
        return {"in": 1 - math.cos(p * math.pi / 2), "out": math.sin(p * math.pi / 2),
                "inOut": -(math.cos(math.pi * p) - 1) / 2}[d]
    if fam.startswith("power"):
        n = int(fam[5:]) + 1
        if d == "in":
            return p ** n
        if d == "out":
            return 1 - (1 - p) ** n
        return 2 ** (n - 1) * p ** n if p < 0.5 else 1 - (-2 * p + 2) ** n / 2
    if fam == "expo":
        if d == "out":
            return 1.0 if p >= 1 else 1 - 2 ** (-10 * p)
        if d == "in":
            return 0.0 if p <= 0 else 2 ** (10 * p - 10)
    raise ValueError(name)


def pchip(xs, ys, m0: float = 0.0, m1: float = 0.0):
    """Interpolation cubique monotone (Fritsch-Carlson), pentes imposées aux extrémités."""
    xs = np.asarray(xs, float)
    ys = np.asarray(ys, float)
    h = np.diff(xs)
    delta = np.diff(ys) / h
    m = np.zeros_like(ys)
    m[0], m[-1] = m0, m1
    for k in range(1, len(xs) - 1):
        if delta[k - 1] * delta[k] <= 0:
            m[k] = 0.0
        else:
            w1 = 2 * h[k] + h[k - 1]
            w2 = h[k] + 2 * h[k - 1]
            m[k] = (w1 + w2) / (w1 / delta[k - 1] + w2 / delta[k])

    def f(x: float) -> float:
        if x <= xs[0]:
            return float(ys[0])
        if x >= xs[-1]:
            return float(ys[-1])
        k = int(np.searchsorted(xs, x) - 1)
        t = (x - xs[k]) / h[k]
        h00 = 2 * t ** 3 - 3 * t ** 2 + 1
        h10 = t ** 3 - 2 * t ** 2 + t
        h01 = -2 * t ** 3 + 3 * t ** 2
        h11 = t ** 3 - t ** 2
        return float(h00 * ys[k] + h10 * h[k] * m[k] + h01 * ys[k + 1] + h11 * h[k] * m[k + 1])
    return f


def bezier(P, u: float) -> np.ndarray:
    P = [np.asarray(p, float) for p in P]
    a = 1 - u
    return a ** 3 * P[0] + 3 * a * a * u * P[1] + 3 * a * u * u * P[2] + u ** 3 * P[3]


def norm(v) -> np.ndarray:
    v = np.asarray(v, float)
    return v / np.linalg.norm(v)


def lerp(a, b, w: float):
    return np.asarray(a, float) + (np.asarray(b, float) - np.asarray(a, float)) * w


def euler_for_dir(v) -> list[float]:
    """Rotation XYZ (degrés) qui aligne l'axe Z local d'un cylindre sur la direction v."""
    v = norm(v)
    a = -math.asin(max(-1.0, min(1.0, v[1])))
    b = math.atan2(v[0], v[2])
    return [math.degrees(a), math.degrees(b), 0.0]


class ArcPath:
    """Courbe de Bézier cubique échantillonnée, parcourue à l'abscisse curviligne s."""

    def __init__(self, P, n: int = 3000):
        self.pts = np.array([bezier(P, u) for u in np.linspace(0, 1, n)])
        self.s = np.concatenate([[0.0], np.cumsum(np.linalg.norm(np.diff(self.pts, axis=0), axis=1))])
        self.length = float(self.s[-1])

    def at(self, s: float) -> np.ndarray:
        s = min(max(s, 0.0), self.length)
        return np.array([np.interp(s, self.s, self.pts[:, k]) for k in range(3)])


# =====================================================================================================
# Caméra : un plan-séquence de 0 à 24 s (28 mm), segments raccordés en position ET en vitesse
# =====================================================================================================

def basis(loc, tgt):
    f = norm(np.asarray(tgt, float) - np.asarray(loc, float))
    r = norm(np.cross(f, [0.0, 0.0, 1.0]))
    u = np.cross(r, f)
    return f, r, u


def project(P, loc, tgt, lens=LENS_3D):
    """Point monde -> (x, y) écran en px logiques + profondeur (distance le long de l'axe de visée)."""
    f, r, u = basis(loc, tgt)
    d = np.asarray(P, float) - np.asarray(loc, float)
    zc = float(d @ f)
    fpx = lens / SENSOR * W
    return np.array([W / 2 + fpx * float(d @ r) / zc, H / 2 - fpx * float(d @ u) / zc]), zc


def unproject_plane(sx, sy, loc, tgt, p0, n, lens=LENS_3D):
    """Point écran -> intersection du rayon avec le plan (p0, n)."""
    f, r, u = basis(loc, tgt)
    fpx = lens / SENSOR * W
    ray = norm(f + r * (sx - W / 2) / fpx - u * (sy - H / 2) / fpx)
    loc = np.asarray(loc, float)
    t = float((np.asarray(p0, float) - loc) @ n) / float(ray @ n)
    return loc + t * ray


# 1. l'arbre (fixe), puis recul en diagonale jusqu'à la vue plongeante (arrêt complet à T_SWOOP)
C_TREE = np.array([0.0, -5.88, 1.58])
T_TREE = np.array([0.0, 0.0, 1.45])
T_ISO = np.array([0.6, 1.2, 0.4])
_az, _el = math.radians(45.0), math.radians(38.0)
C_ISO = T_ISO + 40.6 * np.array([-math.sin(_az) * math.cos(_el), -math.cos(_az) * math.cos(_el), math.sin(_el)])
PULL = [C_TREE, C_TREE + np.array([0.0, -5.6, 0.63]), np.array([-12.0, -30.0, 14.0]), C_ISO]
U_PULL = pchip([T_PULL, 5.0, 6.0, 7.0, 8.0, 9.4, T_SWOOP], [0.0, 0.05, 0.30, 0.62, 0.845, 0.968, 1.0], 0.0, 0.0)

# 2. le piqué : spirale descendante autour d'un point O de l'axe de la rue y = -2,5. La caméra tourne d'abord
#    EN HAUTEUR (40° d'azimut, la ville pivote comme sur un plateau), puis descend dans l'axe de la rue et s'y
#    pose, cap à l'est, à la vitesse du vol : aucun mouvement latéral au ras des immeubles (v1 : bouillie floue).
GLIDE_Y, GLIDE_Z, GLIDE_X0, V_GLIDE = -2.5, 1.5, -26.0, 5.0
LOOK = np.array([4.0, 0.0, -0.45])          # regard en vol : droit devant, un peu plongeant
SW_O = np.array([0.6, GLIDE_Y])
_rel_iso = C_ISO[:2] - SW_O
SW_R0 = float(np.linalg.norm(_rel_iso))
SW_TH0 = math.atan2(-_rel_iso[0], -_rel_iso[1])     # position = O + r (-sin θ, -cos θ) : θ = cap vers O
SW_R1 = float(SW_O[0] - GLIDE_X0)
SW_TH1 = math.pi / 2
T_SW_TURN = 12.25     # fin de la rotation (la caméra est dans l'axe de la rue, encore à ~5 u de haut)
T_SW_LOOK = 12.4      # fin du redressement du regard


def swoop_loc(t: float) -> np.ndarray:
    T = T_GLIDE - T_SWOOP
    tau = min(max((t - T_SWOOP) / T, 0.0), 1.0)
    th = SW_TH0 + (SW_TH1 - SW_TH0) * smootherstep(T_SWOOP, T_SW_TURN, t)
    # rayon : Hermite cubique, vitesse nulle au départ, -V_GLIDE à l'arrivée (on avance vers l'est)
    h00, h01, h11 = 2 * tau ** 3 - 3 * tau ** 2 + 1, -2 * tau ** 3 + 3 * tau ** 2, tau ** 3 - tau ** 2
    r = h00 * SW_R0 + h01 * SW_R1 + h11 * T * (-V_GLIDE)
    z = C_ISO[2] + (GLIDE_Z - C_ISO[2]) * smootherstep(T_SWOOP, T_GLIDE, t)
    return np.array([SW_O[0] - r * math.sin(th), SW_O[1] - r * math.cos(th), z])

# 3. le vol stable (x croissant), freinage en cosinus jusqu'à l'arrêt au carrefour x = -2,5
_X_DEC = GLIDE_X0 + V_GLIDE * (T_DEC - T_GLIDE)


def glide_x(t: float) -> float:
    if t <= T_DEC:
        return GLIDE_X0 + V_GLIDE * (t - T_GLIDE)
    T = T_STOP - T_DEC
    tau = min((t - T_DEC) / T, 1.0)
    return _X_DEC + V_GLIDE * T * (tau / 2 + math.sin(math.pi * tau) / (2 * math.pi))


P_STOP = np.array([glide_x(T_STOP), GLIDE_Y, GLIDE_Z])
TREE_VIEW = np.array([0.0, 0.0, 1.6])
CITY_VIEW = np.array([0.0, 0.0, 2.5])

# 4. montée et recul (« dronie ») : le regard reste sur l'arbre puis sur la ville
DRONIE = ArcPath([P_STOP, P_STOP + np.array([-0.3, 0.0, 4.8]), np.array([-13.0, -1.8, 16.0]),
                  np.array([-26.0, -1.0, 21.0])])

# 5. orbite de 180° autour de l'œil qui se forme, puis arrêt face à lui
EYE_C = np.array([0.0, 0.0, 5.5])
EYE_DIST = 12.8            # à 28 mm, même taille d'œil à l'écran qu'avant (16 u à 35 mm)
_rel = DRONIE.at(DRONIE.length) - EYE_C
ORB_DIST0 = float(np.linalg.norm(_rel))
ORB_EL0 = math.asin(_rel[2] / ORB_DIST0)
ORB_AZ0 = math.atan2(_rel[0], -_rel[1])


def slerp(a, b, w: float) -> np.ndarray:
    """Interpolation sphérique entre deux directions : le regard tourne à vitesse angulaire régulière."""
    a, b = norm(a), norm(b)
    om = math.acos(max(-1.0, min(1.0, float(a @ b))))
    if om < 1e-6:
        return a
    return (math.sin((1 - w) * om) * a + math.sin(w * om) * b) / math.sin(om)


AIM = 10.0                  # distance du point visé quand le regard est donné par une direction
F_ISO = norm(T_ISO - C_ISO)
F_GLIDE = norm(LOOK)


def cam(t: float):
    """(position, cible) de la caméra à l'instant global t.

    Pendant le piqué et le vol, le regard est une DIRECTION interpolée (slerp, dérivées nulles aux raccords) :
    viser un point proche pendant que la caméra passe à côté faisait pivoter l'image d'un coup.
    """
    if t <= T_PULL:
        return C_TREE.copy(), T_TREE.copy()
    if t <= T_SWOOP:
        loc = bezier(PULL, U_PULL(t))
        tgt = T_TREE + (T_ISO - T_TREE) * ease("sine.inOut", smoothstep(T_PULL, 9.6, t) ** 0.8)
        return loc, tgt
    if t <= T_GLIDE:
        loc = swoop_loc(t)
        return loc, loc + AIM * slerp(F_ISO, F_GLIDE, smootherstep(T_SWOOP, T_SW_LOOK, t))
    if t <= T_STOP:
        loc = np.array([glide_x(t), GLIDE_Y, GLIDE_Z])
        d = float(np.linalg.norm(TREE_VIEW - loc))
        return loc, loc + d * slerp(F_GLIDE, TREE_VIEW - loc, smoothstep(T_DEC, T_STOP, t))
    if t <= T_ORBIT:
        loc = DRONIE.at(DRONIE.length * ease("sine.inOut", (t - T_STOP) / (T_ORBIT - T_STOP)))
        tgt = lerp(TREE_VIEW, CITY_VIEW, smoothstep(18.8, 20.8, t))
        return loc, tgt
    a = ease("sine.inOut", smoothstep(T_ORBIT, T_EYE, t))
    az = ORB_AZ0 + math.pi * a
    el = ORB_EL0 * (1 - a)
    dist = ORB_DIST0 + (EYE_DIST - ORB_DIST0) * a
    loc = EYE_C + dist * np.array([math.sin(az) * math.cos(el), -math.cos(az) * math.cos(el), math.sin(el)])
    tgt = EYE_C + (CITY_VIEW - EYE_C) * (1 - a)
    return loc, tgt


def proj(_shot: str, t: float, P):
    loc, tgt = cam(t)
    return project(P, loc, tgt)


# =====================================================================================================
# Ville
# =====================================================================================================

PITCH = 5.0
RB = 4            # blocs bâtis : |i|, |j| <= 4
RT = 6            # dalles qui se fendent : |i|, |j| <= 6
TILE_CUT = 3.2    # côté d'une dalle après la fracture (rues de 1,8)
PLINTH = 0.4      # hauteur des dalles : fond des rues à z = -0,4
HALF_IN = 1.45    # demi-côté constructible d'un bloc

TREE_PARTS: list[dict] = []
BUILDINGS: list[dict] = []


def part(pid, prim, loc, scale, mat, *, rot=(0, 0, 0), seg=None, flat=False, bevel=None, role="body",
         base=0.0, cls="white"):
    p = {"id": pid, "prim": prim, "loc": np.asarray(loc, float), "scale": np.asarray(scale, float),
         "rot": list(rot), "mat": mat, "seg": seg, "flat": flat, "bevel": bevel, "role": role, "base": base,
         "cls": cls}
    return p


CLAY_CLASS = {"blanc": "white", "corail": "coral", "jaune": "yellow", "menthe": "mint", "bleu_profond": "deep",
              "vegetal": "mint", "vegetal_clair": "mint", "menthe_claire": "mint", "menthe_foncee": "mint",
              "tronc": "coral", "graine": "coral", "verre_menthe": "mint", "verre_bleu": "white",
              "verre_corail": "coral", "solaire": "deep", "balise": "yellow"}


def build_tree():
    """Arbre low-poly à l'origine : tronc hexagonal (même argile que la graine), branches, gemmes à facettes."""
    TREE_PARTS.clear()
    TREE_PARTS.append(part("arbre_tronc", "cylinder", [0, 0, 0.625], [0.3, 0.3, 1.25], "graine", seg=6, flat=True,
                           cls="coral"))
    branches = [("arbre_branche_d1", [0.06, 0, 0.92], [0.78, 0, 1.2], 0.13),
                ("arbre_branche_d2", [0.74, 0, 1.18], [1.36, 0, 1.7], 0.12),
                ("arbre_branche_g", [-0.05, 0, 1.08], [-0.68, 0, 1.58], 0.12)]
    for pid, a, b, dia in branches:
        a, b = np.array(a, float), np.array(b, float)
        L = float(np.linalg.norm(b - a))
        p = part(pid, "cylinder", (a + b) / 2, [dia, dia, L], "graine", rot=euler_for_dir(b - a), seg=6, flat=True,
                 cls="coral")
        p["a"], p["b"] = a, b
        TREE_PARTS.append(p)
    gems = [("arbre_cime", [0.08, 0.05, 2.1], 1.5, "menthe", 7, [12, 20, 8]),
            ("arbre_cime_haute", [-0.42, 0.18, 2.42], 0.98, "menthe_foncee", 7, [30, -10, 40]),
            ("arbre_gemme_d", [1.44, 0.0, 1.83], 0.7, "menthe_claire", 6, [5, 25, -15]),
            ("arbre_gemme_g", [-0.78, 0.0, 1.7], 0.62, "jaune", 6, [-20, 10, 30]),
            ("arbre_fruit", [0.62, -0.08, 2.58], 0.5, "jaune", 6, [40, 0, 10])]
    for pid, loc, d, mat, seg, rot in gems:
        TREE_PARTS.append(part(pid, "sphere", loc, [d, d, d * 0.92], mat, rot=rot, seg=seg, flat=True, role="gem",
                               cls=CLAY_CLASS[mat]))


def _height(lo_hi: tuple[float, float]) -> float:
    return float(rng_city.uniform(*lo_hi))


def build_city():
    BUILDINGS.clear()
    colors = ["blanc", "corail", "jaune", "menthe", "bleu_profond"]
    weights = [0.36, 0.2, 0.14, 0.14, 0.16]
    for i in range(-RB, RB + 1):
        for j in range(-RB, RB + 1):
            if i == 0 and j == 0:
                continue
            cx, cy = i * PITCH, j * PITCH
            ring = max(abs(i), abs(j))
            low = (i, j) in {(-1, -1), (-1, 0), (0, -1), (1, -1), (-1, 1)}
            kinds = ["tour", "jumelles", "vegetale", "ilot", "parc"]
            kw = [0.34, 0.22, 0.17, 0.17, 0.10]
            if low:
                kinds, kw = ["ilot", "parc", "vegetale"], [0.5, 0.3, 0.2]
            kind = str(rng_city.choice(kinds, p=np.array(kw) / sum(kw)))
            if ring == 1 and not low:
                hr = (2.6, 5.2)
            elif low:
                hr = (1.0, 2.4)
            else:
                hr = {2: (3.4, 6.8), 3: (4.0, 8.6), 4: (3.0, 7.4)}[ring]
            bid = f"b{i + RB}{j + RB}"
            dist = math.hypot(i, j)
            if kind == "tour":
                w, d = rng_city.uniform(1.6, 2.4), rng_city.uniform(1.6, 2.4)
                ox = rng_city.uniform(-HALF_IN + w / 2, HALF_IN - w / 2)
                oy = rng_city.uniform(-HALF_IN + d / 2, HALF_IN - d / 2)
                add_glass_tower(bid + "_t", cx + ox, cy + oy, w, d, _height(hr), dist)
            elif kind == "jumelles":
                along_x = bool(rng_city.integers(0, 2))
                for n, sgn in enumerate((-1, 1)):
                    w, d = (1.25, 2.5) if along_x else (2.5, 1.25)
                    ox, oy = (sgn * 0.72, 0.0) if along_x else (0.0, sgn * 0.72)
                    h = _height(hr) * (0.75 if n else 1.0)
                    add_glass_tower(f"{bid}_j{n}", cx + ox, cy + oy, w, d, h, dist)
            elif kind == "vegetale":
                dia = rng_city.uniform(1.7, 2.3)
                add_green_tower(bid + "_v", cx, cy, dia, _height((hr[0] * 0.8, hr[1] * 0.85)), dist)
            elif kind == "ilot":
                for a in (-1, 1):
                    for b in (-1, 1):
                        w = rng_city.uniform(1.0, 1.3)
                        d = rng_city.uniform(1.0, 1.3)
                        h = rng_city.uniform(0.7, 1.6 if low else 2.6)
                        mat = str(rng_city.choice(colors, p=weights))
                        add_small(f"{bid}_i{(a + 1) // 2}{(b + 1) // 2}", cx + a * 0.72, cy + b * 0.72, w, d, h, mat, dist)
            else:
                n = int(rng_city.integers(3, 6))
                spots = [(-0.8, -0.8), (0.8, -0.7), (-0.7, 0.8), (0.75, 0.8), (0.0, 0.05)]
                for k in range(n):
                    sx, sy = spots[k]
                    add_park_tree(f"{bid}_p{k}", cx + sx + rng_city.uniform(-0.15, 0.15),
                                  cy + sy + rng_city.uniform(-0.15, 0.15), dist)


def _new_building(bid, x, y, h, dist, w, d, kind):
    b = {"id": bid, "x": x, "y": y, "h": h, "dist": dist, "w": w, "d": d, "kind": kind, "parts": []}
    BUILDINGS.append(b)
    return b


def _solar_panels(b, x, y, w, d, h, n):
    """Rangée de panneaux solaires inclinés vers le sud sur un toit plat."""
    for k in range(n):
        py = y + (k - (n - 1) / 2) * 0.42
        b["parts"].append(part(f"{b['id']}_sol{k}", "cube", [x, py, h + 0.13], [w * 0.72, 0.3, 0.025], "solaire",
                               rot=[28, 0, 0], role="top", base=0.13, cls="deep"))


def add_glass_tower(bid, x, y, w, d, h, dist):
    b = _new_building(bid, x, y, h, dist, w, d, "tour")
    glass = str(rng_city.choice(["verre_menthe", "verre_bleu", "verre_corail", "verre_bleu"], p=[0.4, 0.3, 0.1, 0.2]))
    b["parts"].append(part(f"{bid}_verre", "cube", [x, y, h / 2], [w, d, h], glass, cls=CLAY_CLASS[glass]))
    # montants d'angle : avec les dalles d'étage, ils dessinent le cadre de la façade de verre
    for k, (sx_, sy_) in enumerate(((-1, -1), (1, -1), (1, 1), (-1, 1))):
        b["parts"].append(part(f"{bid}_m{k}", "cube", [x + sx_ * (w / 2 + 0.004), y + sy_ * (d / 2 + 0.004), h / 2],
                               [0.07, 0.07, h], "blanc", cls="white"))
    step = 1.05
    k = 1
    while k * step < h - 0.45:
        z = k * step
        b["parts"].append(part(f"{bid}_n{k}", "cube", [x, y, z], [w * 1.02, d * 1.02, 0.045], "blanc", role="band",
                               base=z, cls="white"))
        k += 1
    garden = []
    for n in range(int(rng_city.integers(1, 3))):
        dd = rng_city.uniform(0.45, 0.8)
        px = x + rng_city.uniform(-w / 2 + dd / 2, w / 2 - dd / 2)
        py = y + rng_city.uniform(-d / 2 + dd / 2, d / 2 - dd / 2)
        mat = "vegetal" if rng_city.random() < 0.6 else "vegetal_clair"
        rot = list(rng_city.uniform(0, 90, 3))
        garden.append(part(f"{bid}_toit{n}", "sphere", [px, py, h + dd * 0.3], [dd, dd, dd * 0.8], mat,
                           rot=rot, seg=7, flat=True, role="top", base=dd * 0.3, cls="mint"))
    if rng_detail.random() < 0.38:          # toit solaire à la place du jardin (tirage séparé : ville inchangée)
        _solar_panels(b, x, y, w, d, h, 3 if min(w, d) >= 1.9 else 2)
    else:
        b["parts"] += garden


def add_green_tower(bid, x, y, dia, h, dist):
    b = _new_building(bid, x, y, h, dist, dia, dia, "vegetale")
    body = "blanc" if rng_city.random() < 0.6 else "corail"
    b["parts"].append(part(f"{bid}_fut", "cylinder", [x, y, h / 2], [dia, dia, h], body, seg=12, cls=CLAY_CLASS[body]))
    n = int(rng_city.integers(7, 12))
    for k in range(n):
        a = rng_city.uniform(0, 2 * math.pi)
        z = rng_city.uniform(0.45, h - 0.25)
        dd = rng_city.uniform(0.38, 0.72)
        rr = dia / 2 * 0.93
        mat = "vegetal" if rng_city.random() < 0.55 else "vegetal_clair"
        b["parts"].append(part(f"{bid}_f{k}", "sphere", [x + rr * math.cos(a), y + rr * math.sin(a), z], [dd, dd, dd],
                               mat, rot=list(rng_city.uniform(0, 90, 3)), seg=6, flat=True, role="side", base=z,
                               cls="mint"))
    dd = dia * 0.62
    b["parts"].append(part(f"{bid}_cime", "sphere", [x, y, h + dd * 0.25], [dd, dd, dd * 0.7], "vegetal",
                           rot=list(rng_city.uniform(0, 90, 3)), seg=8, flat=True, role="top", base=dd * 0.25,
                           cls="mint"))


def add_small(bid, x, y, w, d, h, mat, dist):
    b = _new_building(bid, x, y, h, dist, w, d, "petit")
    b["parts"].append(part(f"{bid}_bloc", "cube", [x, y, h / 2], [w, d, h], mat, cls=CLAY_CLASS[mat]))
    if rng_city.random() < 0.55:
        dd = min(w, d) * 0.55
        b["parts"].append(part(f"{bid}_toit", "sphere", [x, y, h + dd * 0.28], [dd, dd, dd * 0.75], "vegetal_clair",
                               rot=list(rng_city.uniform(0, 90, 3)), seg=6, flat=True, role="top", base=dd * 0.28,
                               cls="mint"))
    elif rng_detail.random() < 0.5:
        _solar_panels(b, x, y, w, d, h, 2)


def add_park_tree(bid, x, y, dist):
    h = float(rng_city.uniform(0.4, 0.6))
    b = _new_building(bid, x, y, h, dist, 0.8, 0.8, "parc")
    b["parts"].append(part(f"{bid}_tronc", "cylinder", [x, y, h / 2], [0.14, 0.14, h], "tronc", seg=5, flat=True,
                           cls="coral"))
    if rng_city.random() < 0.5:
        hh = float(rng_city.uniform(0.9, 1.3))
        b["parts"].append(part(f"{bid}_cime", "cone", [x, y, h + hh / 2 - 0.05], [0.8, 0.8, hh], "vegetal",
                               rot=[0, 0, float(rng_city.uniform(0, 60))], seg=6, flat=True, role="top",
                               base=hh / 2 - 0.05, cls="mint"))
    else:
        dd = float(rng_city.uniform(0.75, 1.0))
        b["parts"].append(part(f"{bid}_cime", "sphere", [x, y, h + dd * 0.4], [dd, dd, dd * 0.9], "vegetal_clair",
                               rot=list(rng_city.uniform(0, 90, 3)), seg=6, flat=True, role="top", base=dd * 0.4,
                               cls="mint"))


def tiles():
    out = []
    for i in range(-RT, RT + 1):
        for j in range(-RT, RT + 1):
            out.append({"id": f"dalle_{i + RT:02d}_{j + RT:02d}", "i": i, "j": j, "x": i * PITCH, "y": j * PITCH,
                        "dist": math.hypot(i, j), "built": max(abs(i), abs(j)) <= RB})
    return out


TILES: list[dict] = []
EDGE = (RT + 0.5) * PITCH       # bord de la zone fracturée (32,5)


# =====================================================================================================
# Réseau de données (plan 2) : nœuds sur des antennes de toit, graphe, activation depuis l'arbre
# =====================================================================================================

NETWORK: dict = {}


def build_network():
    """Choisit les tours-nœuds (hautes, bien réparties, visibles de la vue plongeante), leur ajoute une antenne
    et construit le graphe : arbre couvrant minimal + deux plus proches voisins, parcours depuis l'arbre."""
    cands = sorted([b for b in BUILDINGS if b["kind"] in ("tour", "vegetale") and b["h"] > 2.8], key=lambda b: -b["h"])
    nodes = []
    for b in cands:
        tip = np.array([b["x"], b["y"], b["h"] + 0.86])
        xy, _ = proj("expansion", 10.0, tip)
        if not (150 < xy[0] < W - 150 and 130 < xy[1] < H - 120):
            continue
        if any(math.hypot(b["x"] - n["x"], b["y"] - n["y"]) < 5.2 for n in nodes):
            continue
        nodes.append(b)
        if len(nodes) >= 17:
            break
    for b in nodes:
        # le toit devient un support d'antenne : on retire jardin et panneaux
        b["parts"] = [p for p in b["parts"] if p["role"] != "top"]
        b["parts"].append(part(f"{b['id']}_mat", "cylinder", [b["x"], b["y"], b["h"] + 0.4], [0.05, 0.05, 0.8], "blanc",
                               seg=6, flat=True, role="top", base=0.4, cls="white"))
        b["parts"].append(part(f"{b['id']}_balise", "sphere", [b["x"], b["y"], b["h"] + 0.86], [0.14, 0.14, 0.14],
                               "balise", seg=10, role="top", base=0.86, cls="yellow"))
    pts = [np.array([0.05, 0.0, 2.95])] + [np.array([b["x"], b["y"], b["h"] + 0.86]) for b in nodes]
    n = len(pts)
    d2 = np.array([[math.hypot(pts[a][0] - pts[b][0], pts[a][1] - pts[b][1]) for b in range(n)] for a in range(n)])
    # arbre couvrant minimal (Prim) depuis l'arbre
    inside, edges = {0}, []
    while len(inside) < n:
        a, b = min(((a, b) for a in inside for b in range(n) if b not in inside), key=lambda e: d2[e[0], e[1]])
        edges.append((a, b))
        inside.add(b)
    extra = []
    for a in range(n):
        for b in np.argsort(d2[a])[1:3]:
            b = int(b)
            if d2[a, b] < 15.0 and (a, b) not in edges and (b, a) not in edges and (b, a) not in extra and (a, b) not in extra:
                extra.append((a, b))
    depth = {0: 0}
    parent = {0: None}
    frontier = [0]
    while frontier:
        nxt = []
        for a in frontier:
            for (u, v) in edges:
                for p, c in ((u, v), (v, u)):
                    if p == a and c not in depth:
                        depth[c] = depth[a] + 1
                        parent[c] = a
                        nxt.append(c)
        frontier = nxt
    NETWORK.update({"nodes": nodes, "pts": pts, "tree_edges": [(parent[c], c) for c in range(1, n)], "extra": extra,
                    "depth": depth})


# =====================================================================================================
# Objets de plaque
# =====================================================================================================

def ground_static() -> list[dict]:
    """Fond des rues + quatre grandes dalles extérieures (sol continu jusqu'à l'horizon)."""
    far = 220.0
    objs = [obj_dict("rue_fond", "plane", [0, 0, -PLINTH], [2 * far, 2 * far, 1], "rue")]
    slabs = [("sol_nord", [0, (EDGE + far) / 2, -PLINTH / 2], [2 * far, far - EDGE, PLINTH]),
             ("sol_sud", [0, -(EDGE + far) / 2, -PLINTH / 2], [2 * far, far - EDGE, PLINTH]),
             ("sol_est", [(EDGE + far) / 2, 0, -PLINTH / 2], [far - EDGE, 2 * EDGE, PLINTH]),
             ("sol_ouest", [-(EDGE + far) / 2, 0, -PLINTH / 2], [far - EDGE, 2 * EDGE, PLINTH])]
    for pid, loc, sc in slabs:
        objs.append(obj_dict(pid, "cube", loc, sc, "sol"))
    return objs


def obj_dict(oid, prim, loc, scale, mat, *, rot=None, seg=None, flat=False, anim=None, bevel=None):
    o = {"id": oid, "primitive": prim}   # taille 1 : les dimensions sont portées par scale
    if bevel is not None:
        o["bevel"] = r4(bevel)
    if seg:
        o["segments"] = int(seg)
    if flat:
        o["shading"] = "flat"
    o["location"] = vec(loc)
    if rot is not None and any(abs(x) > 1e-9 for x in rot):
        o["rotation"] = vec(rot)
    o["scale"] = vec(scale)
    o["material"] = mat
    if anim:
        o["anim"] = anim
    return o


def key(shot: str, t: float, v, ease_name: str | None = None, *, linear: bool = False) -> dict:
    k = {"t": local(shot, t), "v": vec(v) if not isinstance(v, (int, float)) else r4(v)}
    if ease_name:
        k["ease"] = ease_name
    if linear:
        k["allowLinear"] = True
    return k


def chan(prop: str, keys: list[dict]) -> dict:
    # La première clé ne porte jamais d'ease (aucun segment n'arrive sur elle).
    if keys:
        keys[0].pop("ease", None)
        keys[0].pop("allowLinear", None)
    return {"prop": prop, "keys": keys}


def part_obj(p, anim=None) -> dict:
    return obj_dict(p["id"], p["prim"], p["loc"], p["scale"], p["mat"], rot=p["rot"], seg=p["seg"], flat=p["flat"],
                    anim=anim)


TINY = 0.001


def grown_parts(shot: str, b: dict, t0: float, dur: float, grow_ease: str) -> list[dict]:
    """Croissance « barre de graphique » : chaque pièce part du sol et suit le toit (même courbe)."""
    out = []
    h = b["h"]
    for p in b["parts"]:
        x, y, z = p["loc"]
        sx, sy, sz = p["scale"]
        if p["role"] == "body":
            # l'emprise s'ouvre en 0,08 s depuis un point, puis la barre monte (rien de visible avant t0)
            anim = [chan("location", [key(shot, t0, [x, y, 0.0005]), key(shot, t0 + 0.08, [x, y, z * 0.03], "power2.out"),
                                      key(shot, t0 + dur, [x, y, z], grow_ease)]),
                    chan("scale", [key(shot, t0, [TINY, TINY, TINY]), key(shot, t0 + 0.08, [sx, sy, sz * 0.03], "power2.out"),
                                   key(shot, t0 + dur, [sx, sy, sz], grow_ease)])]
        elif p["role"] in ("band", "side"):
            # Étages et feuillages : portés par la barre qui monte, ils éclosent quand le toit les dépasse.
            frac = p["base"] / h
            t_a = t0 + 0.05 + 0.5 * frac * dur
            anim = [chan("location", [key(shot, t0, [x, y, frac * h * 0.002]), key(shot, t0 + dur, [x, y, z], grow_ease)]),
                    chan("scale", [key(shot, t0, [TINY, TINY, TINY]),
                                   key(shot, t_a, [TINY, TINY, TINY], "power1.out"),
                                   key(shot, t_a + 0.25, [sx, sy, sz], "back.out(2)")])]
        else:  # top : suit le toit puis éclot quand la barre a fini de monter
            anim = [chan("location", [key(shot, t0, [x, y, p["base"]]), key(shot, t0 + dur, [x, y, z], grow_ease)]),
                    chan("scale", [key(shot, t0 + dur - 0.12, [TINY, TINY, TINY]),
                                   key(shot, t0 + dur + 0.3, [sx, sy, sz], "back.out(2.2)")])]
        out.append(part_obj(p, anim))
    return out


# =====================================================================================================
# Calques 2D : suivi de points 3D
# =====================================================================================================

def lin_tweens(shot: str, times: list[float], values, first_from: bool = True) -> list[dict]:
    """Une tween linéaire par intervalle entre images consécutives : valeurs exactes à chaque image."""
    tw_ = []
    for n in range(len(times) - 1):
        a, b = times[n], times[n + 1]
        va, vb = values(a), values(b)
        # durée = différence des débuts ARRONDIS : la fin tombe exactement sur le début suivant
        t = {"at": local(shot, a), "dur": r6(local(shot, b) - local(shot, a))}
        if n == 0 and first_from:
            t["from"] = va
        t["to"] = vb
        t["ease"] = "none"
        t["allowLinear"] = True
        tw_.append(t)
    return tw_


def sample_times(t0: float, t1: float, step: int = 1) -> list[float]:
    fr = frames_between(t0, t1)
    times = fr[::step]
    if times[-1] < fr[-1] - 1e-9:
        times.append(fr[-1])
    return times


def tracked(shot: str, P, t0: float, t1: float, base_xy, *, s0: float | None = None, step: int = 1,
            extra=None) -> list[dict]:
    """Tweens (x, y, scaleX, scaleY) qui collent un calque au point 3D P de t0 à t1.

    base_xy : position du calque au repos (x, y du calque) ; s0 : profondeur de référence (échelle 1).
    extra(t) -> dict de propriétés supplémentaires (étirements, rotation) fusionnées à chaque image.
    """
    def values(t):
        xy, z = proj(shot, t, P)
        v = {"x": r2(xy[0] - base_xy[0]), "y": r2(xy[1] - base_xy[1])}
        if s0 is not None:
            s = s0 / z
            v["scaleX"] = r4(s)
            v["scaleY"] = r4(s)
        if extra:
            for k2, val in extra(t).items():
                if k2 in ("scaleX", "scaleY") and k2 in v:
                    v[k2] = r4(v[k2] * val)
                else:
                    v[k2] = val
        return v
    return lin_tweens(shot, sample_times(t0, t1, step), values)


def tw(shot: str, t: float, dur: float, frm: dict | None, to: dict, ease_name: str = "power2.out", **kw) -> dict:
    # durée = écart des instants ARRONDIS : une étape qui commence à t + dur ne chevauche pas celle-ci
    d = {"at": local(shot, t), "dur": r6(local(shot, t + dur) - local(shot, t))}
    if frm is not None:
        d["from"] = frm
    d["to"] = to
    d["ease"] = ease_name
    d.update(kw)
    return d


def circle_d(cx, cy, r) -> str:
    return (f"M {r2(cx - r)} {r2(cy)} a {r2(r)} {r2(r)} 0 1 0 {r2(2 * r)} 0 "
            f"a {r2(r)} {r2(r)} 0 1 0 {r2(-2 * r)} 0 Z")


def smooth_closed_d(pts) -> str:
    """Contour fermé lissé (Catmull-Rom -> Bézier cubiques) passant par les points."""
    n = len(pts)
    P = [np.asarray(p, float) for p in pts]
    d = [f"M {r2(P[0][0])} {r2(P[0][1])}"]
    for i in range(n):
        p0, p1, p2, p3 = P[i - 1], P[i], P[(i + 1) % n], P[(i + 2) % n]
        c1 = p1 + (p2 - p0) / 6
        c2 = p2 - (p3 - p1) / 6
        d.append(f"C {r2(c1[0])} {r2(c1[1])} {r2(c2[0])} {r2(c2[1])} {r2(p2[0])} {r2(p2[1])}")
    return " ".join(d) + " Z"


def poly_d(pts) -> str:
    return "M " + " L ".join(f"{r2(x)} {r2(y)}" for x, y in pts) + " Z"


def shape(lid, kind, x, y, w, h, *, anchor="center", z=10, fill="none", stroke="none", sw=0, d=None, points=None,
          opacity=None, anim=None, blend=None, linecap=None, radius=None) -> dict:
    L = {"id": lid, "type": "shape", "shape": kind, "x": r2(x), "y": r2(y), "anchor": anchor, "z": z,
         "w": r2(w), "h": r2(h), "fill": fill, "stroke": stroke}
    if sw:
        L["strokeWidth"] = sw
    if radius:
        L["radius"] = radius
    if linecap:
        L["linecap"] = linecap
    if d is not None:
        L["d"] = d
    if points is not None:
        L["points"] = [[r2(a), r2(b)] for a, b in points]
    if opacity is not None:
        L["opacity"] = opacity
    if blend:
        L["blend"] = blend
    L["anim"] = anim or []
    return L


def text(lid, txt, x, y, *, size, style="display", weight=None, color=INK, z=12, anchor="center", align="center",
         tracking=None, anim=None, safe=None, rotation=None, opacity=None, blend=None) -> dict:
    L = {"id": lid, "type": "text", "text": txt, "style": style, "size": size, "color": color, "align": align,
         "x": r2(x), "y": r2(y), "anchor": anchor, "z": z}
    if weight:
        L["weight"] = weight
    if tracking is not None:
        L["tracking"] = tracking
    if rotation:
        L["rotation"] = r2(rotation)
    if opacity is not None:
        L["opacity"] = opacity
    if blend:
        L["blend"] = blend
    if safe is not None:
        L["safe"] = safe
    L["anim"] = anim or []
    return L


# =====================================================================================================
# Plan 1 : la graine
# =====================================================================================================

def plate_light(sun_loc=(-22.0, -30.0, 40.0), energy=2.2):
    return [{"id": "soleil", "type": "SUN", "energy": energy, "angle": 6, "color": "#FFF1E4",
             "location": list(sun_loc), "target": [0, 0, 0]}]


def camera_block(shot: str, t0: float, t1: float, *, fstop=None, static_until: float | None = None) -> dict:
    """Caméra : une clé par image (linéaire) ; avant static_until une seule clé suffit (plan fixe)."""
    loc0, tgt0 = cam(t0)
    times = frames_between(t0, t1)
    if static_until is not None:   # plan fixe : une clé au début, puis chaque image dès la dernière image fixe
        times = [t0] + [t for t in times if t >= static_until - 1.0 / FPS - 1e-9 and t > t0]
    lk, tk = [], []
    for t in times:
        loc, tgt = cam(t)
        lk.append(key(shot, t, loc, "none", linear=True))
        tk.append(key(shot, t, tgt, "none", linear=True))
    return {"location": vec(loc0), "target": vec(tgt0), "lens": LENS_3D, "fstop": fstop,
            "anim": [chan("location", lk), chan("target", tk)]}


# Éclosion : la graine s'écrase, s'étire en goutte, le tronc se forme dedans (même argile), puis la
# goutte s'amincit à l'intérieur du tronc à facettes ; la cime germe du sommet de la goutte.
GEM_SPROUT = {  # id : (départ, durée, point de germination)
    "arbre_cime": (1.72, 0.52, [0.0, 0.0, 1.5]),
    "arbre_cime_haute": (1.84, 0.48, [-0.1, 0.05, 1.7]),
    "arbre_gemme_g": (1.98, 0.42, [-0.68, 0.0, 1.58]),
    "arbre_gemme_d": (2.08, 0.42, [1.36, 0.0, 1.7]),
    "arbre_fruit": (2.14, 0.42, [0.3, 0.0, 2.2]),
}
BRANCH_T = {"arbre_branche_d1": 1.8, "arbre_branche_g": 1.88, "arbre_branche_d2": 1.94}


def tree_objects(shot: str, *, grow: bool) -> list[dict]:
    out = []
    for p in TREE_PARTS:
        if not grow:
            out.append(part_obj(p))
            continue
        x, y, z = p["loc"]
        sx, sy, sz = p["scale"]
        if p["id"] == "arbre_tronc":
            anim = [chan("location", [key(shot, 1.38, [x, y, 0.0005]), key(shot, 1.8, [x, y, z], "power2.out")]),
                    chan("scale", [key(shot, 1.38, [TINY, TINY, TINY]), key(shot, 1.8, [sx, sy, sz], "power2.out")])]
        elif p["id"].startswith("arbre_branche"):
            t0 = BRANCH_T[p["id"]]
            anim = [chan("location", [key(shot, t0, p["a"]), key(shot, t0 + 0.4, p["loc"], "power3.out")]),
                    chan("scale", [key(shot, t0, [TINY, TINY, TINY]), key(shot, t0 + 0.4, [sx, sy, sz], "power3.out")])]
        else:
            t0, dur, src = GEM_SPROUT[p["id"]]
            anim = [chan("location", [key(shot, t0, src), key(shot, t0 + dur, p["loc"], "power3.out")]),
                    chan("scale", [key(shot, t0, [TINY, TINY, TINY]), key(shot, t0 + dur, [sx, sy, sz], "back.out(1.4)")]),
                    chan("rotation", [key(shot, t0, [p["rot"][0] - 12, p["rot"][1], p["rot"][2] - 18]),
                                      key(shot, t0 + dur + 0.1, p["rot"], "power2.out")])]
        out.append(part_obj(p, anim))
    return out


def seed_object(shot: str) -> dict:
    """Graine (diamètre 0,8) : chute, écrasement, étirement en goutte, amincissement dans le tronc."""
    anim = [
        chan("location", [key(shot, 0.0, [0, 0, 7.6]), key(shot, 1.0, [0, 0, 0.4], "power2.in"),
                          key(shot, 1.08, [0, 0, 0.25], "power2.out"), key(shot, 1.3, [0, 0, 0.56], "power2.out"),
                          key(shot, 1.75, [0, 0, 0.84], "sine.inOut"), key(shot, 2.05, [0, 0, 0.6], "power2.inOut")]),
        chan("scale", [key(shot, 0.0, [0.76, 0.76, 0.86]), key(shot, 1.0, [0.8, 0.8, 0.8], "sine.in"),
                       key(shot, 1.08, [1.04, 1.04, 0.5], "power2.out"), key(shot, 1.3, [0.62, 0.62, 1.12], "power2.out"),
                       key(shot, 1.75, [0.34, 0.34, 1.68], "sine.inOut"), key(shot, 2.05, [0.22, 0.22, 1.2], "power2.inOut"),
                       key(shot, 2.1, [TINY, TINY, TINY], "power1.in")]),
    ]
    return obj_dict("graine", "sphere", [0, 0, 7.6], [0.76, 0.76, 0.86], "graine", seg=48, anim=anim)


def tile_objects(shot: str, mode: str) -> list[dict]:
    out = []
    for tl in TILES:
        x, y = tl["x"], tl["y"]
        if mode == "full":
            out.append(obj_dict(tl["id"], "cube", [x, y, -PLINTH / 2], [PITCH, PITCH, PLINTH], "sol"))
        elif mode == "fracture":
            t0 = 5.32 + 0.11 * tl["dist"]
            dz = 0.0 if tl["built"] else float(rng_fx.uniform(-0.06, 0.05))
            anim = [chan("scale", [key(shot, t0, [PITCH, PITCH, PLINTH]),
                                   key(shot, t0 + 0.5, [TILE_CUT, TILE_CUT, PLINTH], "back.out(1.6)")]),
                    chan("location", [key(shot, t0, [x, y, -PLINTH / 2]),
                                      key(shot, t0 + 0.14, [x, y, -PLINTH / 2 + 0.09], "power2.out"),
                                      key(shot, t0 + 0.5, [x, y, -PLINTH / 2 + dz], "power2.inOut")])]
            tl["dz"] = dz
            out.append(obj_dict(tl["id"], "cube", [x, y, -PLINTH / 2 + dz], [TILE_CUT, TILE_CUT, PLINTH], "sol", anim=anim))
        else:
            out.append(obj_dict(tl["id"], "cube", [x, y, -PLINTH / 2 + tl.get("dz", 0.0)], [TILE_CUT, TILE_CUT, PLINTH], "sol"))
    return out


def shot_graine() -> dict:
    sh = "graine"
    objects = ground_static() + tile_objects(sh, "full") + tree_objects(sh, grow=True) + [seed_object(sh)]
    plate = {"id": "germination", "transparent": True, "world": WORLD,
             "camera": camera_block(sh, 0.0, 5.0, fstop=5.6, static_until=T_PULL), "lights": plate_light(),
             "objects": objects}
    layers = [{"id": "plaque", "type": "sequence", "plate": "germination", "z": 0}]

    # Repères écran de l'arbre (caméra fixe jusqu'à 3,3 s).
    base_xy, _ = proj(sh, 0.0, [0, 0, 0])
    centre_xy, _ = proj(sh, 0.0, [0.18, 0, 1.45])
    plane_n = np.array([0.0, 1.0, 0.0])

    def anchor3d(xy):
        loc, tgt = cam(0.0)
        return unproject_plane(xy[0], xy[1], loc, tgt, [0, 0, 0], plane_n)

    def follow(L, xy, t_from=T_PULL, t_to=5.0):
        P = anchor3d(xy)
        _, zc = proj(sh, 0.0, P)
        L["anim"] += tracked(sh, P, t_from, t_to, xy, s0=zc)

    # 1. Jaillissement : traits fins qui partent du point d'impact.
    rays = [(-172, 120), (-158, 150), (-141, 104), (-124, 160), (-108, 118), (-92, 170), (-76, 124), (-59, 158),
            (-42, 110), (-25, 150), (-9, 120), (-188, 96), (8, 96), (-100, 90)]
    for n, (ang, length) in enumerate(rays):
        a = math.radians(ang)
        r0 = 34 + 6 * (n % 3)
        px = base_xy[0] + r0 * math.cos(a)
        py = base_xy[1] - 6 + r0 * math.sin(a)
        col = YELLOW if n % 2 == 0 else CORAL
        L = shape(f"jet_{n}", "line", px, py, length, 0, anchor="left", z=14, stroke=col, sw=4,
                  points=[[0, 0], [length, 0]])
        L["rotation"] = ang
        dx, dy = 70 * math.cos(a), 70 * math.sin(a)
        t0 = T_IMPACT + 0.012 * n
        L["anim"] = [tw(sh, t0, 0.2, {"draw": 0}, {"draw": 1}, "power3.out", target="shape"),
                     tw(sh, t0 + 0.14, 0.46, {"x": 0, "y": 0}, {"x": r2(dx * 1.6), "y": r2(dy * 1.6)}, "power2.out"),
                     tw(sh, t0 + 0.26, 0.34, {"opacity": 1}, {"opacity": 0}, "power1.in")]
        layers.append(L)

    # 2. Ondes au sol (ellipses en perspective autour du point d'impact).
    for n, (col, rx) in enumerate([(YELLOW, 300), (CORAL, 420), (DEEP, 540)]):
        ry = rx * 0.16
        L = shape(f"onde_{n}", "ellipse", base_xy[0], base_xy[1] - 4, 2 * rx, 2 * ry, z=6, stroke=col, sw=3)
        t0 = T_IMPACT + 0.02 + 0.1 * n
        # opacité 0 jusqu'à l'impact (un calque montre l'état « from » de sa 1re tween avant qu'elle démarre)
        L["anim"] = [tw(sh, t0 - 1.0 / FPS, 1.0 / FPS, {"opacity": 0}, {"opacity": 0.95}, "steps(1)"),
                     tw(sh, t0, 0.95, {"scaleX": 0.12, "scaleY": 0.12}, {"scaleX": 1, "scaleY": 1}, "power2.out"),
                     tw(sh, t0, 0.95, None, {"opacity": 0}, "power1.in")]
        layers.append(L)

    # 3. Cercles concentriques autour de l'arbre (réticule), graduations.
    cx, cy = centre_xy
    L = shape("cercle_jaune", "path", cx, cy, 2 * 420 + 8, 2 * 420 + 8, z=8, stroke=YELLOW, sw=3,
              d=circle_d(424, 424, 420))
    L["rotation"] = 90   # le tracé part du point gauche : tourné d'un quart de tour, il naît en haut
    L["anim"] = [tw(sh, 1.18, 1.05, {"draw": 0}, {"draw": 1}, "power2.inOut", target="shape")]
    follow(L, (cx, cy))
    L["anim"].append(tw(sh, 4.5, 0.42, {"opacity": 1}, {"opacity": 0}, "power1.in"))
    layers.append(L)

    arc_r = 458
    arc_frac = 0.72
    a0, a1 = math.radians(-90), math.radians(-90 + 360 * arc_frac)
    arc = (f"M {r2(arc_r + 4 + arc_r * math.cos(a0))} {r2(arc_r + 4 + arc_r * math.sin(a0))} "
           f"A {arc_r} {arc_r} 0 1 1 {r2(arc_r + 4 + arc_r * math.cos(a1))} {r2(arc_r + 4 + arc_r * math.sin(a1))}")
    L = shape("jauge_corail", "path", cx, cy, 2 * arc_r + 8, 2 * arc_r + 8, z=8, stroke=CORAL, sw=4, d=arc,
              linecap="round")
    L["anim"] = [tw(sh, 1.42, 1.1, {"draw": 0}, {"draw": 1}, "power3.inOut", target="shape"),
                 tw(sh, 1.42, 3.2, {"rotation": -40}, {"rotation": 18}, "sine.inOut")]
    follow(L, (cx, cy))
    L["anim"].append(tw(sh, 4.45, 0.42, {"opacity": 1}, {"opacity": 0}, "power1.in"))
    layers.append(L)

    ticks = []
    for k in range(72):
        a = 2 * math.pi * k / 72
        r_in, r_out = (432, 446) if k % 6 == 0 else (436, 442)
        ticks.append(f"M {r2(450 + r_in * math.cos(a))} {r2(450 + r_in * math.sin(a))} "
                     f"L {r2(450 + r_out * math.cos(a))} {r2(450 + r_out * math.sin(a))}")
    L = shape("graduations", "path", cx, cy, 900, 900, z=7, stroke=DEEP, sw=2, d=" ".join(ticks), linecap="butt")
    L["anim"] = [tw(sh, 1.7, 0.5, {"opacity": 0, "rotation": -12}, {"opacity": 0.55, "rotation": 0}, "power2.out")]
    follow(L, (cx, cy))
    L["anim"].append(tw(sh, 4.4, 0.4, {"opacity": 0.55}, {"opacity": 0}, "power1.in"))
    layers.append(L)

    # 4. Graphiques de données : barres qui montent (gauche), courbe (droite).
    bx, by = cx - 640, cy + 110
    heights = [44, 70, 58, 104, 146]
    for n, hgt in enumerate(heights):
        col = [YELLOW, CORAL, MINT, CORAL, YELLOW][n]
        L = shape(f"barre_{n}", "rect", bx + n * 26, by, 16, hgt, anchor="bottom", z=10, fill=col, stroke=DEEP, sw=2)
        L["anim"] = [tw(sh, 1.95 + 0.07 * n, 0.55, {"scaleY": 0}, {"scaleY": 1}, "power3.out")]
        follow(L, (bx + n * 26, by))
        L["anim"].append(tw(sh, 4.35, 0.4, {"opacity": 1}, {"opacity": 0}, "power1.in"))
        layers.append(L)
    L = shape("barres_socle", "line", bx - 12, by + 1, 140, 0, anchor="left", z=10, stroke=DEEP, sw=3,
              points=[[0, 0], [140, 0]])
    L["anim"] = [tw(sh, 1.88, 0.4, {"draw": 0}, {"draw": 1}, "power2.out", target="shape")]
    follow(L, (bx - 12, by + 1))
    L["anim"].append(tw(sh, 4.35, 0.4, {"opacity": 1}, {"opacity": 0}, "power1.in"))
    layers.append(L)
    L = text("barres_titre", "CROISSANCE", bx + 52, by + 34, size=22, style="body", color=INK, z=11)
    L["anim"] = [tw(sh, 2.3, 0.35, {"opacity": 0, "y": 8}, {"opacity": 1, "y": 0}, "power2.out")]
    follow(L, (bx + 52, by + 34), T_PULL, 5.0)
    L["anim"].append(tw(sh, 4.35, 0.4, {"opacity": 1}, {"opacity": 0}, "power1.in"))
    layers.append(L)

    sx0, sy0 = cx + 520, cy - 150
    curve = [(0, 96), (26, 88), (52, 92), (78, 70), (104, 74), (130, 46), (156, 38), (182, 14), (208, 4)]
    dpath = "M " + " L ".join(f"{x} {y}" for x, y in curve)
    L = shape("courbe", "path", sx0, sy0, 212, 100, anchor="top-left", z=10, stroke=MINT_DARK, sw=4, d=dpath,
              linecap="round")
    L["anim"] = [tw(sh, 2.05, 0.8, {"draw": 0}, {"draw": 1}, "power2.inOut", target="shape")]
    follow(L, (sx0, sy0))
    L["anim"].append(tw(sh, 4.4, 0.4, {"opacity": 1}, {"opacity": 0}, "power1.in"))
    layers.append(L)
    L = shape("courbe_point", "ellipse", sx0 + 208, sy0 + 4, 16, 16, z=11, fill=YELLOW, stroke=DEEP, sw=3)
    L["anim"] = [tw(sh, 2.8, 0.35, {"scaleX": 0, "scaleY": 0}, {"scaleX": 1, "scaleY": 1}, "back.out(2.4)")]
    follow(L, (sx0 + 208, sy0 + 4))
    L["anim"].append(tw(sh, 4.4, 0.4, {"opacity": 1}, {"opacity": 0}, "power1.in"))
    layers.append(L)
    L = text("courbe_titre", "ÉNERGIE", sx0 + 106, sy0 + 132, size=22, style="body", color=INK, z=11)
    L["anim"] = [tw(sh, 2.45, 0.35, {"opacity": 0, "y": 8}, {"opacity": 1, "y": 0}, "power2.out")]
    follow(L, (sx0 + 106, sy0 + 132), T_PULL, 5.0)
    L["anim"].append(tw(sh, 4.4, 0.4, {"opacity": 1}, {"opacity": 0}, "power1.in"))
    layers.append(L)

    # 5. « L'ORIGINE » le long de la branche droite (lettres posées sur une courbe sous la branche).
    pts = [proj(sh, 0.0, p)[0] for p in ([0.1, 0, 0.9], [0.78, 0, 1.2], [1.38, 0, 1.72])]
    off = 58.0
    P0, P1, P2 = [np.asarray(p, float) for p in pts]

    def bez2(u):
        return (1 - u) ** 2 * P0 + 2 * (1 - u) * u * P1 + u * u * P2

    def normal_down(u):
        d = 2 * (1 - u) * (P1 - P0) + 2 * u * (P2 - P1)
        d = d / np.linalg.norm(d)
        return np.array([-d[1], d[0]]) if d[0] >= 0 else np.array([d[1], -d[0]])

    us = np.linspace(0, 1, 400)
    cpts = np.array([bez2(u) + off * normal_down(u) for u in us])
    cl = np.concatenate([[0.0], np.cumsum(np.linalg.norm(np.diff(cpts, axis=0), axis=1))])
    letters = ["L", "’", "O", "R", "I", "G", "I", "N", "E"]
    adv = {"L": 0.56, "’": 0.26, "O": 0.78, "R": 0.64, "I": 0.3, "G": 0.76, "N": 0.74, "E": 0.6}
    size = 60
    track = 0.05
    widths = [(adv[c] + track) * size for c in letters]
    total = sum(widths)
    start = (cl[-1] - total) / 2 + 10
    acc = start
    for n, (c, wdt) in enumerate(zip(letters, widths)):
        sc = acc + wdt / 2
        acc += wdt
        k = int(np.searchsorted(cl, sc))
        k = min(max(k, 1), len(cl) - 1)
        p = cpts[k]
        tang = cpts[min(k + 1, len(cpts) - 1)] - cpts[k - 1]
        ang = math.degrees(math.atan2(tang[1], tang[0]))
        L = text(f"origine_{n}", c, p[0], p[1], size=size, weight=700, color=INK, z=16, rotation=ang)
        t0 = 2.22 + 0.06 * n
        L["anim"] = [tw(sh, t0, 0.55, {"opacity": 0, "blur": 10, "scaleX": 0.55, "scaleY": 0.55, "y": 18},
                        {"opacity": 1, "blur": 0, "scaleX": 1, "scaleY": 1, "y": 0}, "power3.out")]
        P3 = anchor3d(p)
        _, zc = proj(sh, 0.0, P3)
        L["anim"] += tracked(sh, P3, T_PULL, 5.0, p, s0=zc)
        L["anim"].append(tw(sh, 4.48, 0.42, {"opacity": 1}, {"opacity": 0}, "power1.in"))   # fini avant la coupe
        layers.append(L)

    EVENTS["gems"] = sorted(v[0] for v in GEM_SPROUT.values())
    EVENTS["origine"] = [2.22 + 0.06 * n for n in range(len(letters))]
    return {"id": sh, "duration": 5.0, "background": BG_RENDERED, "plates": [plate], "layers": layers}


# =====================================================================================================
# Plan 2 : l'expansion (et le réseau de données)
# =====================================================================================================

GROW_START = 6.0


WAVES = 6


def building_start(b: dict) -> float:
    """Vagues sur les temps 12 à 18 (6,0 à 9,0 s) selon la distance au centre : la ville est complète quand
    le réseau s'allume."""
    g = min(WAVES, int(b["dist"] / 5.66 * WAVES + 0.001))
    jitter = float(rng_fx.uniform(0.0, 0.07))
    return GROW_START + 0.5 * g + jitter


def _dots_d(L0: float, spacing: float, y: float) -> str:
    n = max(3, int(L0 / spacing))
    return " ".join(f"M {r2(L0 * q / n)} {y} l 0.01 0" for q in range(n + 1))


def network_layers(sh: str) -> list[dict]:
    """Réseau de données : l'arbre s'allume, les liens se propagent de proche en proche (pings), puis des
    paquets lumineux circulent de nœud en nœud ; étiquettes de données sur quelques nœuds."""
    nodes, pts = NETWORK["nodes"], NETWORK["pts"]
    n = len(pts)
    t_fade = T_SWOOP - 0.05
    track_t1 = t_fade + 0.32
    # instants d'activation : parcours depuis l'arbre, jamais avant que la tour ait fini de pousser
    t_act = {0: NET_T0}
    for (p, c) in sorted(NETWORK["tree_edges"], key=lambda e: NETWORK["depth"][e[1]]):
        b = nodes[c - 1]
        done = b["t_grow"] + 0.55 + 0.35
        t_act[c] = max(t_act[p] + 0.26 + float(rng_net.uniform(0, 0.05)), done + 0.05)
    EVENTS["net_nodes"] = sorted(t_act.values())
    layers: list[dict] = []

    def screen(k, t):
        return proj(sh, t, pts[k])[0]

    # liens : double trait pointillé (contour bleu + cœur clair), révélés de la source vers la cible
    edges = [(p, c, t_act[p], max(t_act[c] - t_act[p] - 0.02, 0.18)) for p, c in NETWORK["tree_edges"]]
    edges += [(a, b, max(t_act[a], t_act[b]) + 0.12, 0.3) for a, b in NETWORK["extra"]]
    for k, (a, b, t_on, dur) in enumerate(edges):
        a0, b0 = screen(a, t_on), screen(b, t_on)
        L0 = float(np.linalg.norm(b0 - a0))
        rot0 = math.degrees(math.atan2(b0[1] - a0[1], b0[0] - a0[0]))

        def vals(t, a=a, b=b, a0=a0, L0=L0):
            pa, pb = screen(a, t), screen(b, t)
            s = float(np.linalg.norm(pb - pa)) / L0
            return {"x": r2(pa[0] - a0[0]), "y": r2(pa[1] - a0[1]), "rotation": r2(math.degrees(math.atan2(pb[1] - pa[1], pb[0] - pa[0]))),
                    "scaleX": r4(s), "scaleY": r4(s)}
        for suffix, col, sw, z in (("_fond", INK, 8, 12), ("", "#FFF6B8", 4, 13)):
            L = shape(f"lien_{k}{suffix}", "path", a0[0], a0[1], L0, 12, anchor=[0, 0.5], z=z, stroke=col, sw=sw,
                      d=_dots_d(L0, 13.0, 6), linecap="round")
            L["rotation"] = r2(rot0)
            L["anim"] = lin_tweens(sh, sample_times(t_on, track_t1, 2), vals)
            L["anim"] += [tw(sh, t_on, dur, {"reveal": 1}, {"reveal": 0}, "power1.inOut", revealDir="right"),
                          tw(sh, t_fade, 0.3, {"opacity": 1}, {"opacity": 0}, "power1.in")]
            layers.append(L)

    # nœuds : pastille + ping à l'activation
    for k in range(n):
        t0 = t_act[k]
        p0 = screen(k, t0 - 0.05)

        def nvals(t, k=k, p0=p0):
            p = screen(k, t)
            return {"x": r2(p[0] - p0[0]), "y": r2(p[1] - p0[1])}
        track = lin_tweens(sh, sample_times(t0 - 0.05, track_t1, 2), nvals)
        L = shape(f"noeud_{k}", "ellipse", p0[0], p0[1], 24, 24, z=15, fill=CORAL if k == 0 else YELLOW, stroke=INK, sw=4)
        L["anim"] = list(track) + [tw(sh, t0, 0.35, {"scaleX": 0, "scaleY": 0}, {"scaleX": 1, "scaleY": 1}, "back.out(2.6)"),
                                   tw(sh, t_fade, 0.3, {"opacity": 1}, {"opacity": 0}, "power1.in")]
        layers.append(L)
        L = shape(f"ping_{k}", "ellipse", p0[0], p0[1], 24, 24, z=14, stroke=YELLOW if k else CORAL, sw=3)
        L["anim"] = [dict(t) for t in track] + [
            tw(sh, t0 - 1.0 / FPS, 1.0 / FPS, {"opacity": 0}, {"opacity": 1}, "steps(1)"),
            tw(sh, t0, 0.55, {"scaleX": 0.6, "scaleY": 0.6}, {"scaleX": 3.4, "scaleY": 3.4}, "power2.out"),
            tw(sh, t0, 0.55, None, {"opacity": 0}, "power1.in")]
        layers.append(L)

    # paquets : petites capsules lumineuses qui voyagent de nœud en nœud (marches aléatoires)
    adj = {k: [] for k in range(n)}
    for a, b in NETWORK["tree_edges"] + NETWORK["extra"]:
        adj[a].append(b)
        adj[b].append(a)
    hop = 0.34
    q = 0
    packet_times: list[float] = []
    for _attempt in range(120):
        if q >= 24:
            break
        start = int(rng_net.integers(0, n))
        path = [start]
        for _ in range(int(rng_net.integers(2, 5))):
            nxt = [c for c in adj[path[-1]] if len(path) < 2 or c != path[-2]]
            if not nxt:
                break
            path.append(int(nxt[int(rng_net.integers(0, len(nxt)))]))
        t0 = max(max(t_act[k] for k in path) + 0.1, 9.35 + float(rng_net.uniform(0, 1.2)))
        t1 = t0 + hop * (len(path) - 1)
        if t1 > t_fade + 0.2 or len(path) < 2:
            continue
        t0 = (math.floor(t0 * FPS) + 0.5) / FPS

        def pos(t, path=path, t0=t0):
            u = min(max((t - t0) / hop, 0.0), len(path) - 1 - 1e-9)
            i = int(u)
            f = u - i
            pa, pb = screen(path[i], t), screen(path[i + 1], t)
            return pa + (pb - pa) * f, math.degrees(math.atan2(pb[1] - pa[1], pb[0] - pa[0]))
        p0, r0 = pos(t0)

        def pvals(t, p0=p0, pos=pos):
            p, rot = pos(t)
            return {"x": r2(p[0] - p0[0]), "y": r2(p[1] - p0[1]), "rotation": r2(rot)}
        times = sample_times(t0, t1, 1)
        for suffix, kind, w_, h_, col, z, extra in (("_halo", "ellipse", 34, 18, YELLOW, 16, {"opacity": 0.75}),
                                                     ("", "rect", 20, 8, WHITE, 17, {})):
            L = shape(f"paquet_{q}{suffix}", kind, p0[0], p0[1], w_, h_, z=z, fill=col, stroke=INK if not suffix else "none",
                      sw=2 if not suffix else 0, radius=4 if kind == "rect" else None)
            L["rotation"] = r2(r0)
            op = extra.get("opacity", 1)
            L["anim"] = lin_tweens(sh, times, pvals) + [
                tw(sh, t0 - 0.5 / FPS, 1.0 / FPS, {"opacity": 0}, {"opacity": op}, "steps(1)"),
                tw(sh, t1, 1.0 / FPS, {"opacity": op}, {"opacity": 0}, "steps(1)")]
            if suffix:
                L["anim"].append(tw(sh, t0, 0.2, {"blur": 5}, {"blur": 5}, "none", allowLinear=True))
            layers.append(L)
        packet_times += [t0 + hop * h for h in range(len(path))]   # départ puis arrivée à chaque nœud
        q += 1
    EVENTS["packets"] = sorted(packet_times)

    # étiquettes de données (Space Mono) reliées à leur nœud
    labels = {0: "NŒUD 00 · ORIGINE"}
    others = sorted(range(1, n), key=lambda k: -screen(k, 10.0)[1])
    for k, txt in zip([others[1], others[len(others) // 2], others[-2]], ["FLUX 12,4 GO/S", "1 204 CAPTEURS",
                                                                           "LATENCE 2 MS"]):
        labels[k] = txt
    for k, txt in labels.items():
        t0 = t_act[k] + 0.2
        p0 = screen(k, t0)
        right = p0[0] < W * 0.66
        tw_w = 22 + 11.0 * len(txt)
        ox, oy = (58, -58) if right else (-58, -58)
        bx = p0[0] + ox if right else p0[0] + ox - tw_w
        by = p0[1] + oy - 34

        def lvals(t, k=k, p0=p0):
            p = screen(k, t)
            return {"x": r2(p[0] - p0[0]), "y": r2(p[1] - p0[1])}
        track = lin_tweens(sh, sample_times(t0, track_t1, 2), lvals)
        L = shape(f"repere_{k}", "line", p0[0], p0[1], abs(ox), abs(oy), anchor=[0, 1] if right else [1, 1], z=18,
                  stroke=INK, sw=2, points=[[0, abs(oy)], [abs(ox), 0]] if right else [[abs(ox), abs(oy)], [0, 0]])
        L["anim"] = list(track) + [tw(sh, t0, 0.16, {"draw": 0}, {"draw": 1}, "power2.out", target="shape"),
                                   tw(sh, t_fade, 0.3, {"opacity": 1}, {"opacity": 0}, "power1.in")]
        layers.append(L)
        L = shape(f"etiquette_{k}", "rect", bx, by, tw_w, 34, anchor="top-left", z=19, fill=WHITE, stroke=INK, sw=2, radius=5)
        L["anim"] = [dict(t) for t in track] + [tw(sh, t0 + 0.12, 0.18, {"scaleX": 0}, {"scaleX": 1}, "power3.out"),
                                                tw(sh, t_fade, 0.3, {"opacity": 1}, {"opacity": 0}, "power1.in")]
        layers.append(L)
        L = text(f"etiquette_{k}_texte", txt, bx + 11, by + 17, size=18, style="body", color=INK, z=20, anchor="left",
                 align="left", tracking=0.02)
        L["anim"] = [dict(t) for t in track] + [
            tw(sh, t0 + 0.2, 1.0 / FPS, {"opacity": 0}, {"opacity": 1}, "steps(1)", target="chars", stagger=0.028),
            tw(sh, t_fade, 0.3, {"opacity": 1}, {"opacity": 0}, "power1.in")]
        layers.append(L)
    return layers


def shot_expansion() -> dict:
    sh = "expansion"
    t0, t1 = SHOTS[sh]
    objects = ground_static() + tile_objects(sh, "fracture") + tree_objects(sh, grow=False)
    for b in BUILDINGS:
        b["t_grow"] = building_start(b)
        objects += grown_parts(sh, b, b["t_grow"], 0.55, "back.out(1.25)")
    plate = {"id": "ville_iso", "transparent": True, "world": WORLD,
             "camera": camera_block(sh, t0, t1), "lights": plate_light(), "objects": objects}
    layers = [{"id": "plaque", "type": "sequence", "plate": "ville_iso", "z": 0}]
    layers += network_layers(sh)
    EVENTS["waves"] = [GROW_START + 0.5 * g for g in range(WAVES + 1)]
    return {"id": sh, "duration": r6(t1 - t0), "background": BG_RENDERED, "transition_in": "cut", "plates": [plate],
            "layers": layers}


# =====================================================================================================
# Plan 3 : le flux (piqué, vol stable, néons, mots)
# =====================================================================================================

STREETS = [-17.5, -12.5, -7.5, -2.5, 2.5, 7.5, 12.5, 17.5]
RIVER_MATS = ["riviere_menthe", "riviere_corail", "riviere_jaune"]
NEON_MATS = {"riviere_menthe": "neon_menthe", "riviere_corail": "neon_corail", "riviere_jaune": "neon_jaune"}
CORE_MATS = {"riviere_menthe": "coeur_menthe", "riviere_corail": "coeur_corail", "riviere_jaune": "coeur_jaune"}
RIVERS: list[dict] = []


def build_rivers():
    RIVERS.clear()
    fixed = {("x", -2.5): "riviere_menthe", ("y", -2.5): "riviere_corail", ("y", 2.5): "riviere_jaune"}
    for n, s in enumerate(STREETS):
        for axis, zoff, shift in (("y", 0.012, 0), ("x", 0.016, 1)):
            mat = fixed.get((axis, s), RIVER_MATS[(n + shift) % 3])
            RIVERS.append({"id": f"riviere_{'x' if axis == 'y' else 'y'}{n}", "axis": axis, "c": s, "mat": mat,
                           "z": -PLINTH + zoff})


def river_objects(sh: str | None = None, ignite: bool = False) -> list[dict]:
    """Rivière = tube de néon : corps coloré (saturé) + cœur étroit presque blanc, un peu au-dessus.
    ignite : les néons s'allument au drop, depuis le centre de la ville vers les bords."""
    out = []
    for rv in RIVERS:
        core = CORE_MATS[rv["mat"]]
        L = 2 * EDGE
        if rv["axis"] == "y":
            specs = [(rv["id"], [rv["c"], 0, rv["z"]], [0.5, L, 0.02], rv["mat"]),
                     (rv["id"] + "_coeur", [rv["c"], 0, rv["z"] + 0.006], [0.1, L, 0.02], core)]
        else:
            specs = [(rv["id"], [0, rv["c"], rv["z"]], [L, 0.5, 0.02], rv["mat"]),
                     (rv["id"] + "_coeur", [0, rv["c"], rv["z"] + 0.006], [L, 0.1, 0.02], core)]
        for oid, loc, sc, mat in specs:
            anim = None
            if ignite:
                t0 = T_DROP + 0.035 * abs(rv["c"])
                off = [sc[0], TINY, sc[2]] if rv["axis"] == "y" else [TINY, sc[1], sc[2]]
                anim = [chan("scale", [key(sh, t0, off), key(sh, t0 + 0.55, sc, "power2.out")])]
            out.append(obj_dict(oid, "cube", loc, sc, mat, anim=anim))
    return out


def pulse_objects(sh: str) -> list[dict]:
    """Impulsions de néon qui filent le long des rivières (une traversée chacune, 18 à 30 u/s).

    Apparition et disparition (échelle, interpolation constante) entre deux obturations (k + 0,5 image) :
    le flou de mouvement ne voit jamais le saut. Rien avant l'allumage des néons, rien après 19,6 s.
    """
    out = []
    n_pulses = 120
    t_first, t_last = T_DROP + 0.45, 19.6
    for k in range(n_pulses):
        rv = RIVERS[int(rng_fx.integers(0, len(RIVERS)))]
        speed = float(rng_fx.uniform(18, 30))
        length = float(rng_fx.uniform(1.6, 3.2))
        sign = 1 if rng_fx.random() < 0.5 else -1
        t0 = t_first - 1.5 + (k / n_pulses) * (t_last - t_first + 1.2) + float(rng_fx.uniform(-0.25, 0.25))
        travel = 2 * EDGE + 4
        t1 = t0 + travel / speed
        start, end = -sign * (EDGE + 2), sign * (EDGE + 2)
        z = rv["z"] + 0.035
        if rv["axis"] == "y":
            p0, p1, sc = np.array([rv["c"], start, z]), np.array([rv["c"], end, z]), [0.17, length, 0.05]
        else:
            p0, p1, sc = np.array([start, rv["c"], z]), np.array([end, rv["c"], z]), [length, 0.17, 0.05]
        if t1 <= t_first + 3 / FPS or t0 >= t_last - 3 / FPS:
            continue
        if t0 < t_first:
            p0 = p0 + (t_first - t0) / (t1 - t0) * (p1 - p0)
            t0 = t_first
        t0 = (math.floor(t0 * FPS) + 0.5) / FPS
        t_end = min((math.floor(t1 * FPS) + 0.5) / FPS, t_last)
        p_end = p0 + (t_end - t0) / (t1 - t0) * (p1 - p0)
        anim = [chan("location", [key(sh, t0, p0), key(sh, t_end, p_end, "none", linear=True)]),
                chan("scale", [key(sh, SHOTS[sh][0], [TINY] * 3), key(sh, t0, sc, "steps(1)"),
                               key(sh, t_end, [TINY] * 3, "steps(1)")])]
        out.append(obj_dict(f"impulsion_{k}", "rounded_cube", p0, sc, "impulsion_" + rv["mat"].split("_")[1], bevel=0.3,
                            anim=anim))
    return out


WORD_WINDOWS: dict[str, tuple[float, float]] = {}


def shot_flux() -> dict:
    sh = "flux"
    t0s, t1s = SHOTS[sh]
    objects = ground_static() + tile_objects(sh, "cut") + tree_objects(sh, grow=False)
    for b in BUILDINGS:
        objects += [part_obj(p) for p in b["parts"]]
    objects += river_objects(sh, ignite=True) + pulse_objects(sh)
    plate = {"id": "rues", "transparent": True, "world": WORLD, "camera": camera_block(sh, t0s, t1s),
             "lights": plate_light(), "objects": objects}
    layers = [{"id": "plaque", "type": "sequence", "plate": "rues", "z": 0}]
    # Lueur des néons : la plaque elle-même, floue, en superposition (screen) ; nulle aux deux coupes.
    layers.append({"id": "lueur", "type": "sequence", "plate": "rues", "z": 1, "blend": "screen", "opacity": 0,
                   "anim": [tw(sh, T_DROP, 0.5, {"opacity": 0}, {"opacity": 0.16}, "power1.out"),
                            tw(sh, 19.2, 0.6, None, {"opacity": 0}, "power1.in"),
                            tw(sh, T_DROP, 0.2, {"blur": 14}, {"blur": 14}, "none", allowLinear=True)]})

    s0 = 2.4           # échelle 1 à 2,4 u de profondeur : le mot fait ~1,45 u de large (la rue en fait 1,8)
    for wid, word, t_rev, t_pass, col in WORDS:
        # le mot est posé à 0,45 u devant la caméra de l'instant du passage : c'est sa dernière image visible
        loc_p, _ = cam(t_pass)
        P = loc_p + np.array([0.45, 0.0, 0.0])
        base, _ = proj(sh, t_rev, P)
        WORD_WINDOWS[word] = (t_rev, t_pass)

        def warp(t, P=P, t_rev=t_rev):
            _, zc = proj(sh, t, P)
            k = smoothstep(1.4, 0.45, zc)
            pop = 0.55 + 0.45 * ease("back.out(2)", (t - t_rev) / 0.2)
            return {"scaleX": pop * (1 + 0.55 * k), "scaleY": pop * (1 - 0.18 * k), "skewX": r2(-9 * k)}

        for layer_id, z, blur, op, blend, colr, anch in (
                (f"mot_{wid}_lueur", 17, 16, 0.75, "screen", col, None),
                (f"mot_{wid}_ombre", 18, 0, 1, None, INK, [0.488, 0.44]),
                (f"mot_{wid}", 19, 0, 1, None, col, None)):
            L = text(layer_id, word, base[0], base[1], size=170, weight=700, color=colr, z=z, safe=False, blend=blend,
                     tracking=0.02)
            if anch:
                L["anchor"] = anch
            L["anim"] = tracked(sh, P, t_rev, t_pass, base, s0=s0, extra=warp)
            # apparition franche sur le temps, disparition à l'image suivant le passage
            L["anim"] += [tw(sh, t_rev, 0.1, {"opacity": 0}, {"opacity": op}, "power1.out"),
                          tw(sh, t_pass, 1.0 / FPS, {"opacity": op}, {"opacity": 0}, "steps(1)")]
            if blur:
                L["anim"].append(tw(sh, t_rev, 0.2, {"blur": blur}, {"blur": blur}, "none", allowLinear=True))
            else:
                L["anim"].append(tw(sh, t_pass - 0.3, 0.3, {"blur": 0}, {"blur": 9}, "power2.in"))
                if not anch:
                    L["anim"].append(tw(sh, t_pass - 0.42, 0.34, {"scaleY": 1}, {"scaleY": 1.28}, "sine.inOut",
                                        target="chars", stagger=0.02))
            layers.append(L)
    EVENTS["word_reveals"] = [w[2] for w in WORDS]
    EVENTS["word_passes"] = [w[3] for w in WORDS]
    return {"id": sh, "duration": r6(t1s - t0s), "background": BG_RENDERED, "transition_in": "cut",
            "plates": [plate], "layers": layers}


# =====================================================================================================
# Plans 4 et 5 : l'œil
# =====================================================================================================

EYE_X, EYE_Y = W / 2, H / 2
EYE_HALF_W, EYE_HALF_H = 500.0, 220.0
IRIS_R, RING_R, PUPIL_R = 184.0, 190.0, 74.0
BLADES = 7


def lid_y(x: float, upper: bool) -> float:
    """Paupière (Bézier quadratique) : ordonnée écran pour une abscisse écran."""
    u = ((x - EYE_X) / EYE_HALF_W + 1) / 2
    dy = 2 * EYE_HALF_H * 2 * u * (1 - u)
    return EYE_Y - dy if upper else EYE_Y + dy


def almond_d() -> str:
    x0, x1 = EYE_X - EYE_HALF_W, EYE_X + EYE_HALF_W
    return (f"M {r2(x0)} {r2(EYE_Y)} Q {r2(EYE_X)} {r2(EYE_Y - 2 * EYE_HALF_H)} {r2(x1)} {r2(EYE_Y)} "
            f"Q {r2(EYE_X)} {r2(EYE_Y + 2 * EYE_HALF_H)} {r2(x0)} {r2(EYE_Y)} Z")


def blade_points(k: int) -> list[tuple[float, float]]:
    step = 2 * math.pi / BLADES
    a = k * step - math.pi / 2
    twist = 0.62 * step
    pts = [(EYE_X + IRIS_R * math.cos(a + q * step / 4), EYE_Y + IRIS_R * math.sin(a + q * step / 4)) for q in range(5)]
    pts += [(EYE_X + PUPIL_R * math.cos(a + step + twist), EYE_Y + PUPIL_R * math.sin(a + step + twist)),
            (EYE_X + PUPIL_R * math.cos(a + twist), EYE_Y + PUPIL_R * math.sin(a + twist))]
    return pts


def pupil_points() -> list[tuple[float, float]]:
    step = 2 * math.pi / BLADES
    return [(EYE_X + PUPIL_R * 1.02 * math.cos(k * step - math.pi / 2 + 0.62 * step),
             EYE_Y + PUPIL_R * 1.02 * math.sin(k * step - math.pi / 2 + 0.62 * step)) for k in range(BLADES)]


def eye_targets() -> list[tuple[float, float, str]]:
    """Points d'arrivée des particules (écran) avec leur classe de couleur."""
    pts = []
    for upper in (True, False):
        for x in np.arange(EYE_X - EYE_HALF_W + 6, EYE_X + EYE_HALF_W - 5, 11.0):
            pts.append((float(x), lid_y(float(x), upper), "deep"))
    n_ring = int(2 * math.pi * RING_R / 10.5)
    for k in range(n_ring):
        a = 2 * math.pi * k / n_ring
        pts.append((EYE_X + RING_R * math.cos(a), EYE_Y + RING_R * math.sin(a), "coral"))
    step = 12.5
    for y in np.arange(EYE_Y - EYE_HALF_H + 8, EYE_Y + EYE_HALF_H - 7, step * 0.866):
        row = int(round((y - EYE_Y) / (step * 0.866)))
        for x in np.arange(EYE_X - EYE_HALF_W + 18 + (step / 2 if row % 2 else 0), EYE_X + EYE_HALF_W - 17, step):
            x, y2 = float(x), float(y)
            if not (lid_y(x, True) + 9 < y2 < lid_y(x, False) - 9):
                continue
            r = math.hypot(x - EYE_X, y2 - EYE_Y)
            if RING_R - 7 < r < RING_R + 8:
                continue
            if math.hypot(x - (EYE_X + 70), y2 - (EYE_Y - 78)) < 26:
                pts.append((x, y2, "yellow"))
            elif r < PUPIL_R - 4:
                pts.append((x, y2, "deep"))
            elif r < RING_R - 7:
                pts.append((x, y2, "mint"))
            else:
                pts.append((x, y2, "white"))
    return pts


def eye_layers() -> list[dict]:
    """Œil vectoriel : calques plein cadre (ancre au centre = centre de l'œil)."""
    full = dict(x=W / 2, y=H / 2, w=W, h=H)
    layers = [
        shape("oeil_sclere", "path", **full, z=20, fill=WHITE, d=almond_d()),
        shape("oeil_iris", "path", **full, z=21, fill="#3FD6A7", d=circle_d(EYE_X, EYE_Y, IRIS_R + 2)),
    ]
    for k in range(BLADES):
        layers.append(shape(f"oeil_lame_{k}", "path", **full, z=22, fill=MINT if k % 2 == 0 else "#8DEDCF",
                            stroke=DEEP, sw=3, d=poly_d(blade_points(k))))
    layers += [
        shape("oeil_pupille", "path", **full, z=23, fill=INK, d=poly_d(pupil_points())),
        shape("oeil_anneau", "path", **full, z=24, stroke=CORAL, sw=12, d=circle_d(EYE_X, EYE_Y, RING_R)),
        shape("oeil_reflet", "path", **full, z=25, fill=WHITE, d=circle_d(EYE_X + 70, EYE_Y - 78, 24)),
        shape("oeil_reflet_petit", "path", **full, z=25, fill=WHITE, d=circle_d(EYE_X + 104, EYE_Y - 42, 9)),
        shape("oeil_contour", "path", **full, z=26, stroke=DEEP, sw=12, d=almond_d()),
    ]
    return layers


def iris_angle(t: float) -> float:
    """Rotation du diaphragme : un « clic » de 46° à chaque battement de cœur, dérive de 9° ensuite."""
    angle = 0.0
    for tb in HEARTBEATS:
        if t >= tb + 1.0:
            angle += 55.0
        elif t >= tb + 0.34:
            angle += 46.0 + 9.0 * ease("sine.inOut", (t - tb - 0.34) / 0.66)
        elif t >= tb:
            angle += 46.0 * ease("expo.out", (t - tb) / 0.34)
    return angle


def iris_tweens(shot: str, t0: float, t1: float) -> list[dict]:
    """Rotation échantillonnée image par image : exacte, et continue à travers la coupe 4 -> 5."""
    return lin_tweens(shot, sample_times(t0, t1, 1), lambda t: {"rotation": r2(iris_angle(t))})


def particles(sh: str) -> tuple[list[dict], dict[str, float], dict[str, float]]:
    """Particules (plan 4) et temps d'éclatement des pièces de la ville."""
    targets = eye_targets()
    loc_end, tgt_end = cam(T_EYE)
    n_plane = norm(np.asarray(loc_end) - np.asarray(tgt_end))

    def to_world(x, y):
        return unproject_plane(x, y, loc_end, tgt_end, EYE_C, n_plane)

    sources: dict[str, list[tuple]] = {c: [] for c in ("white", "deep", "mint", "coral", "yellow")}
    burst: dict[str, float] = {}
    lift: dict[str, float] = {}
    dmax = max(b["dist"] for b in BUILDINGS)
    for b in BUILDINGS:
        tb = 20.9 + 0.32 * b["dist"] / dmax + float(rng_fx.uniform(0, 0.06))
        dz = float(rng_fx.uniform(1.0, 3.4))
        burst[b["id"]] = tb
        lift[b["id"]] = dz
        for p in b["parts"]:
            c = np.array(p["loc"]) + np.array([0, 0, dz])
            hs = np.array(p["scale"]) / 2
            sources[p["cls"]].append((c, hs, tb, float(np.prod(p["scale"])) + 0.05))
    for p in TREE_PARTS:
        sources[p["cls"]].append((np.array(p["loc"]) + [0, 0, 0.6], np.array(p["scale"]) / 2, 20.95, 0.6))
    for tl in TILES:
        tb = 20.95 + 0.3 * tl["dist"] / (RT * 1.42)
        burst[tl["id"]] = tb
        lift[tl["id"]] = float(rng_fx.uniform(0.4, 1.6))
        c = np.array([tl["x"], tl["y"], -PLINTH / 2 + lift[tl["id"]]])
        if tl["dist"] < 5.2:
            sources["white"].append((c, np.array([1.6, 1.6, 0.2]), tb, 0.5))
    for s_ in STREETS:                                     # le fond des rues (bleu profond) éclate aussi
        for q in range(9):
            along = -20.0 + 5.0 * q
            for c in (np.array([s_, along, -PLINTH + 0.05]), np.array([along, s_, -PLINTH + 0.05])):
                sources["deep"].append((c, np.array([0.8, 0.8, 0.04]), 20.95 + 0.25 * abs(along) / 20.0, 0.3))
    for rv in RIVERS:
        cls = {"riviere_menthe": "mint", "riviere_corail": "coral", "riviere_jaune": "yellow"}[rv["mat"]]
        for q in range(6):
            along = -20 + 8 * q
            c = np.array([rv["c"], along, rv["z"]]) if rv["axis"] == "y" else np.array([along, rv["c"], rv["z"]])
            sources[cls].append((c, np.array([0.3, 3.5, 0.05]) if rv["axis"] == "y" else np.array([3.5, 0.3, 0.05]),
                                 20.95, 0.4, NEON_MATS[rv["mat"]]))
        burst[rv["id"]] = 20.95
    mats = {"white": "p_blanc", "deep": "p_bleu", "mint": "p_menthe", "coral": "p_corail", "yellow": "p_jaune"}
    objs = []
    order = np.argsort([math.hypot(x - EYE_X, y - EYE_Y) for x, y, _ in targets])
    for rank, idx in enumerate(order):
        x, y, cls = targets[idx]
        srcs = sources[cls] or sources["white"]
        wts = np.array([s[3] for s in srcs])
        s = srcs[int(rng_fx.choice(len(srcs), p=wts / wts.sum()))]
        c, hs, tb = s[0], s[1], s[2]
        mat = s[4] if len(s) > 4 and rng_fx.random() < 0.7 else mats[cls]
        p0 = c + hs * rng_fx.uniform(-1, 1, 3)
        out_dir = norm(p0 - np.array([0.0, 0.0, -2.0]) + rng_fx.normal(0, 0.6, 3))
        p1 = p0 + out_dir * float(rng_fx.uniform(1.0, 3.2)) + np.array([0, 0, float(rng_fx.uniform(0.6, 2.0))])
        rel = p1 - EYE_C
        ang = math.atan2(rel[1], rel[0]) + math.radians(float(rng_fx.uniform(55, 95)))
        rad = math.hypot(rel[0], rel[1]) * float(rng_fx.uniform(0.35, 0.55))
        p2 = EYE_C + np.array([rad * math.cos(ang), rad * math.sin(ang), rel[2] * 0.45 + float(rng_fx.uniform(-1, 1))])
        q = to_world(x, y)
        t_mid = 22.05 + float(rng_fx.uniform(-0.2, 0.2))
        t_land = 22.8 + 0.35 * rank / len(order) + float(rng_fx.uniform(-0.04, 0.04))
        dia = 0.105 if cls != "deep" else 0.115
        t_fade = T_EYE + 0.22 + 0.15 * rank / len(order)
        big = dia * float(rng_fx.uniform(2.2, 3.0))
        anim = [chan("location", [key(sh, tb, p0), key(sh, tb + 0.55, p1, "expo.out"), key(sh, t_mid, p2, "sine.inOut"),
                                  key(sh, t_land, q, "power3.out")]),
                chan("scale", [key(sh, 20.0, [TINY] * 3), key(sh, tb, [big] * 3, "steps(1)"),
                               key(sh, t_mid, [big * 0.8] * 3, "sine.inOut"), key(sh, t_land, [dia] * 3, "power2.out"),
                               key(sh, t_fade, [dia] * 3, "sine.inOut"), key(sh, t_fade + 0.24, [TINY] * 3, "power2.in")])]
        objs.append(obj_dict(f"p{rank:04d}", "sphere", p0, [dia] * 3, mat, seg=8, anim=anim))
    return objs, burst, lift


def shot_morphing() -> dict:
    sh = "morphing"
    t0s, t1s = SHOTS[sh]
    parts_objs, burst, lift = particles(sh)
    objects = []
    # fond des rues : un carré par dalle (pas de grand plan), qui éclate avec sa dalle ; la ville flotte, seule
    for tl in TILES:
        tb = burst[tl["id"]] + 0.04
        objects.append(obj_dict("rue_" + tl["id"][6:], "cube", [tl["x"], tl["y"], -PLINTH - 0.02], [PITCH, PITCH, 0.04], "rue",
                                anim=[chan("scale", [key(sh, tb, [PITCH, PITCH, 0.04]),
                                                     key(sh, tb + 0.16, [TINY] * 3, "power2.in")])]))
    for o in tile_objects(sh, "cut"):
        tid = o["id"]
        x, y, z = o["location"]
        tb = burst[tid]
        dz = lift[tid]
        o["anim"] = [chan("location", [key(sh, 20.1, [x, y, z]), key(sh, tb, [x, y, z + dz], "sine.in")]),
                     chan("scale", [key(sh, tb, o["scale"]), key(sh, tb + 0.14, [TINY] * 3, "power2.in")])]
        objects.append(o)
    for p in TREE_PARTS:
        o = part_obj(p)
        x, y, z = o["location"]
        o["anim"] = [chan("location", [key(sh, 20.05, [x, y, z]), key(sh, 20.95, [x, y, z + 0.6], "sine.in")]),
                     chan("scale", [key(sh, 20.95, o["scale"]), key(sh, 21.08, [TINY] * 3, "power2.in")])]
        objects.append(o)
    for b in BUILDINGS:
        tb, dz = burst[b["id"]], lift[b["id"]]
        for p in b["parts"]:
            o = part_obj(p)
            x, y, z = o["location"]
            o["anim"] = [chan("location", [key(sh, 20.05 + 0.1 * float(rng_fx.random()), [x, y, z]),
                                           key(sh, tb, [x, y, z + dz], "sine.in")]),
                         chan("scale", [key(sh, tb, o["scale"]), key(sh, tb + 0.13, [TINY] * 3, "power2.in")])]
            objects.append(o)
    for o in river_objects():
        o["anim"] = [chan("scale", [key(sh, 20.95, o["scale"]), key(sh, 21.05, [TINY] * 3, "power2.in")])]
        objects.append(o)
    objects += parts_objs
    plate = {"id": "particules", "transparent": True, "world": WORLD,
             "camera": camera_block(sh, t0s, t1s), "lights": plate_light(), "objects": objects}
    layers = [{"id": "plaque", "type": "sequence", "plate": "particules", "z": 0}]

    order = ["oeil_sclere", "oeil_iris", "oeil_lame", "oeil_pupille", "oeil_anneau", "oeil_reflet"]
    for L in eye_layers():
        lid = L["id"]
        if lid == "oeil_contour":
            L["anim"] = [tw(sh, T_EYE, 0.36, {"draw": 0}, {"draw": 1}, "power2.inOut", target="shape")]
        else:
            rank = next(n for n, k in enumerate(order) if lid.startswith(k))
            L["anim"] = [tw(sh, T_EYE + 0.04 + 0.02 * rank, 0.18, {"opacity": 0}, {"opacity": 1}, "power1.out")]
        if lid.startswith("oeil_lame") or lid == "oeil_pupille":
            L["anim"] += iris_tweens(sh, HEARTBEATS[0], t1s)
        # battement : l'œil entier « bat » avec le cœur, la pupille un peu plus (saut puis retour)
        amp = 1.1 if lid == "oeil_pupille" else 1.025
        L["anim"].append(tw(sh, T_EYE, 0.3, {"scaleX": 0.97, "scaleY": 0.97}, {"scaleX": 1, "scaleY": 1}, "power2.out"))
        L["anim"].append(tw(sh, HEARTBEATS[0], 0.4, {"scaleX": amp, "scaleY": amp}, {"scaleX": 1, "scaleY": 1}, "power3.out"))
        layers.append(L)
    for n, (sx, sy) in enumerate([(-1, -1), (1, -1), (1, 1), (-1, 1)]):
        cxp, cyp = EYE_X + sx * 640, EYE_Y + sy * 318
        L = shape(f"viseur_{n}", "line", cxp, cyp, 70, 70, anchor="top-left", z=27, stroke=DEEP, sw=5,
                  points=[[0, 70], [0, 0], [70, 0]], linecap="square")
        L["rotation"] = {(-1, -1): 0, (1, -1): 90, (1, 1): 180, (-1, 1): 270}[(sx, sy)]
        L["anim"] = [tw(sh, T_EYE + 0.2 + 0.05 * n, 0.35, {"draw": 0}, {"draw": 1}, "power2.out", target="shape")]
        layers.append(L)
    EVENTS.update({"gravity": T_GRAVITY, "burst": 20.95, "suction_end": T_EYE, "heartbeats": list(HEARTBEATS)})
    return {"id": sh, "duration": r6(t1s - t0s), "background": BG_RENDERED, "transition_in": "cut", "plates": [plate],
            "layers": layers}


def shot_outro() -> dict:
    sh = "outro"
    t0s, t1s = SHOTS[sh]
    layers = [{"id": "degrade", "type": "image", "src": GRADIENT_REL, "w": W, "h": H, "x": W / 2, "y": H / 2, "z": 1,
               "anim": [tw(sh, T_CLOSE + 0.1, 0.42, {"opacity": 0}, {"opacity": 1}, "sine.inOut")]}]
    ang0 = iris_angle(t0s)
    for L in eye_layers():
        lid = L["id"]
        L["anim"] = []
        rotated = lid.startswith("oeil_lame") or lid == "oeil_pupille"
        if rotated:
            L["rotation"] = r2(ang0)
            L["anim"] += iris_tweens(sh, t0s, T_CLOSE + 0.3)
        amp = 1.1 if lid == "oeil_pupille" else 1.025
        # Échelle 1 dès la coupe : sans cette première étape neutre, l'état initial du calque serait le « from »
        # du battement (1,025) et l'œil sauterait à la coupe.
        L["anim"].append(tw(sh, t0s, 0.25, {"scaleX": 1, "scaleY": 1}, {"scaleX": 1, "scaleY": 1}, "none",
                            allowLinear=True))
        # dernier battement, puis clin d'œil, puis fermeture lente : l'œil se réduit à un trait
        L["anim"].append(tw(sh, HEARTBEATS[1], 0.36, {"scaleX": amp, "scaleY": amp}, {"scaleX": 1, "scaleY": 1},
                            "power3.out"))
        if rotated:
            # calques tournés : GSAP applique l'échelle AVANT la rotation, un scaleY écraserait selon un axe
            # tourné ; le diaphragme disparaît donc derrière les paupières par un fondu très court.
            L["anim"] += [tw(sh, T_BLINK - 0.02, 0.05, {"opacity": 1}, {"opacity": 0}, "power1.in"),
                          tw(sh, T_BLINK + 0.3, 0.1, None, {"opacity": 1}, "power1.out"),
                          tw(sh, T_CLOSE - 0.02, 0.1, None, {"opacity": 0}, "power1.in")]
        else:
            L["anim"] += [tw(sh, T_BLINK, 0.12, None, {"scaleY": 0.06}, "power2.in"),
                          tw(sh, T_BLINK + 0.12, 0.2, None, {"scaleY": 1}, "power2.out"),
                          tw(sh, T_CLOSE, 0.3, None, {"scaleY": 0.05}, "power2.in"),
                          tw(sh, T_BLOB, 1.0 / FPS, {"opacity": 1}, {"opacity": 0}, "steps(1)")]
        layers.append(L)
    for n, (sx, sy) in enumerate([(-1, -1), (1, -1), (1, 1), (-1, 1)]):
        cxp, cyp = EYE_X + sx * 640, EYE_Y + sy * 318
        L = shape(f"viseur_{n}", "line", cxp, cyp, 70, 70, anchor="top-left", z=27, stroke=DEEP, sw=5,
                  points=[[0, 70], [0, 0], [70, 0]], linecap="square")
        L["rotation"] = {(-1, -1): 0, (1, -1): 90, (1, 1): 180, (-1, 1): 270}[(sx, sy)]
        L["anim"] = [tw(sh, T_CLOSE - 0.3, 0.3, {"opacity": 1}, {"opacity": 0}, "power1.in")]
        layers.append(L)

    # Forme douce : trois formes organiques (et leur ombre) qui naissent du trait de l'œil fermé, puis
    # tournent à des vitesses différentes et respirent : leur silhouette commune ondule sans jamais se répéter.
    cy_blob = 462
    blobs = [  # id, rayon, harmoniques [(ordre, amplitude, phase)], couleur, décalage, retard, flou, opacité, tour
        ("forme_ombre", 262, [(2, 0.10, 0.3), (3, 0.07, 1.1), (5, 0.03, 2.0)], "#E86F5E", (0, 30), 0.0, 30, 0.3, 14),
        ("forme_creme", 256, [(2, 0.11, 0.3), (3, 0.07, 1.1), (5, 0.03, 2.0)], "#FFF4EA", (0, 0), 0.0, 2, 1.0, 14),
        ("forme_menthe", 168, [(2, 0.16, 2.2), (3, 0.09, 0.4), (4, 0.05, 1.7)], "#7EE8CB", (-78, -22), 0.07, 2, 0.94, -38),
        ("forme_jaune", 142, [(2, 0.14, 4.1), (3, 0.1, 2.6), (5, 0.05, 0.9)], "#FFE36B", (92, 30), 0.14, 2, 0.94, 52),
    ]
    for n, (bid, rad, harm, col, (ox, oy), dly, blur, op, turn) in enumerate(blobs):
        box = 2 * rad * 1.45
        c = box / 2
        pts = []
        for k in range(72):
            th = 2 * math.pi * k / 72
            rr = rad * (1 + sum(a * math.cos(o * th + ph) for o, a, ph in harm))
            pts.append((c + rr * math.cos(th), c + rr * math.sin(th)))
        d = smooth_closed_d(pts)
        x0, y0 = W / 2 + ox, cy_blob + oy
        L = shape(bid, "path", x0, y0, box, box, z=30 + n, fill=col, d=d)
        a = [tw(sh, T_BLOB, 1.0 / FPS, {"opacity": 0}, {"opacity": op}, "steps(1)"),
             tw(sh, T_BLOB + dly, 0.95, {"scaleX": r4(1000 / (2 * rad)), "scaleY": 0.03, "y": r2(EYE_Y - y0)},
                {"scaleX": 1, "scaleY": 1, "y": 0}, "back.out(1.3)"),
             tw(sh, T_BLOB, T_END - T_BLOB, {"rotation": 0}, {"rotation": turn}, "sine.inOut")]
        a.append(tw(sh, T_BLOB, 0.2, {"blur": blur}, {"blur": blur}, "none", allowLinear=True))
        period = [2.6, 2.6, 1.9, 2.3][n]
        amp = [0.04, 0.04, 0.09, 0.1][n]
        t = T_BLOB + dly + 0.95
        flip = 1
        while t < T_END - 1e-6:
            dur = min(period / 2, T_END - t)
            a.append(tw(sh, t, dur, None, {"scaleX": r4(1 + amp * flip), "scaleY": r4(1 - amp * 0.7 * flip),
                                            "x": r2(10 * flip * (1 if n % 2 else -1)), "y": r2(-8 * flip)}, "sine.inOut"))
            t += dur
            flip = -flip
        L["anim"] = a
        layers.append(L)

    slogan_txt = "Visionnez Demain."
    slogan = text("slogan", slogan_txt, W / 2, 866, size=104, weight=500, color=INK, z=40, tracking=-0.01)
    slogan["anim"] = [tw(sh, T_SLOGAN, 1.0 / FPS, {"opacity": 0}, {"opacity": 1}, "steps(1)", target="chars", stagger=0.07)]
    layers.append(slogan)
    EVENTS.update({"blink": T_BLINK, "close": T_CLOSE, "blob": T_BLOB,
                   "slogan": [T_SLOGAN + 1.0 / FPS + 0.07 * k for k in range(len(slogan_txt))], "end": T_END})
    return {"id": sh, "duration": r6(t1s - t0s), "background": BG_RENDERED, "transition_in": "cut", "layers": layers}


# =====================================================================================================
# Scène
# =====================================================================================================

def preset_overrides() -> dict:
    clay = {"roughness": 0.5, "specular": 0.4, "sheen": 0.35, "sheen_roughness": 0.45, "subsurface": 0.04}
    glossy = {"roughness": 0.08, "specular": 0.75, "coat": 1.0, "coat_roughness": 0.02}
    mats = {
        "sol": {"base_color": SOL, "roughness": 1.0, "specular": 0.0, "sheen": 0.0},
        "rue": {"base_color": "#2E3D96", "roughness": 0.6, "specular": 0.3},
        "graine": {"base_color": "#FF7A68", **clay, "sheen": 0.5},
        "tronc": {"base_color": "#F27B63", **clay},
        "menthe": {"base_color": "#5EDDB0", **clay},
        "menthe_claire": {"base_color": "#A2EFD2", **clay},
        "menthe_foncee": {"base_color": "#2FB98C", **clay},
        "jaune": {"base_color": "#FFD83D", **clay},
        "corail": {"base_color": "#FF8A76", **clay},
        "blanc": {"base_color": "#F6F3EE", **clay},
        "bleu_profond": {"base_color": "#2B3B9A", **clay},
        "vegetal": {"base_color": "#43CF95", **clay, "roughness": 0.6},
        "vegetal_clair": {"base_color": "#8FE6B5", **clay, "roughness": 0.6},
        "verre_menthe": {"base_color": "#86E3C8", **glossy},
        "verre_bleu": {"base_color": "#9CB8F4", **glossy},
        "verre_corail": {"base_color": "#FFA898", **glossy},
        "solaire": {"base_color": "#2A3C9C", "roughness": 0.18, "specular": 0.8, "coat": 0.6, "coat_roughness": 0.05},
        "balise": {"base_color": "#FFE033", "emission": "#FFE033", "emission_strength": 1.0, "roughness": 0.4},
        "riviere_menthe": {"base_color": "#3DFFC8", "emission": "#3DFFC8", "emission_strength": 0.85, "roughness": 0.3},
        "riviere_corail": {"base_color": "#FF5E57", "emission": "#FF5E57", "emission_strength": 0.9, "roughness": 0.3},
        "riviere_jaune": {"base_color": "#FFE81A", "emission": "#FFE81A", "emission_strength": 0.85, "roughness": 0.3},
        "coeur_menthe": {"base_color": "#C9FFEF", "emission": "#C9FFEF", "emission_strength": 1.5},
        "coeur_corail": {"base_color": "#FFD2CC", "emission": "#FFD2CC", "emission_strength": 1.5},
        "coeur_jaune": {"base_color": "#FFF6BF", "emission": "#FFF6BF", "emission_strength": 1.5},
        "impulsion_menthe": {"base_color": "#E0FFF6", "emission": "#E0FFF6", "emission_strength": 3.2},
        "impulsion_corail": {"base_color": "#FFE3DE", "emission": "#FFE3DE", "emission_strength": 3.2},
        "impulsion_jaune": {"base_color": "#FFFBE0", "emission": "#FFFBE0", "emission_strength": 3.2},
        "neon_menthe": {"base_color": "#3DFFC8", "emission": "#3DFFC8", "emission_strength": 0.95},
        "neon_corail": {"base_color": "#FF5E57", "emission": "#FF5E57", "emission_strength": 1.0},
        "neon_jaune": {"base_color": "#FFE81A", "emission": "#FFE81A", "emission_strength": 0.95},
        "p_blanc": {"base_color": "#FFFFFF", **clay},
        "p_bleu": {"base_color": "#24318A", **clay},
        "p_menthe": {"base_color": "#4FE0B2", **clay},
        "p_corail": {"base_color": "#FF7A68", **clay},
        "p_jaune": {"base_color": "#FFE033", **clay},
    }
    return {
        "palette": {"bg": BG_RENDERED, "fg": INK, "primary": MINT, "secondary": CORAL, "accent": YELLOW,
                    "deep": DEEP, "mint_dark": MINT_DARK, "mint_light": MINT_LIGHT, "peach": "#FFD3BC"},
        "typography": {
            "display": {"family": "Space Grotesk", "weight": 700, "tracking": -0.01, "lineHeight": 1.0, "case": "none",
                        "size": 120},
            "body": {"family": "Space Mono", "weight": 400, "tracking": 0.12, "lineHeight": 1.2, "case": "none",
                     "size": 22},
        },
        "fonts": [
            {"family": "Space Grotesk", "weight": 500, "style": "normal",
             "file": "@fontsource/space-grotesk/files/space-grotesk-latin-500-normal.woff2"},
            {"family": "Space Grotesk", "weight": 700, "style": "normal",
             "file": "@fontsource/space-grotesk/files/space-grotesk-latin-700-normal.woff2"},
            {"family": "Space Mono", "weight": 400, "style": "normal",
             "file": "@fontsource/space-mono/files/space-mono-latin-400-normal.woff2"},
        ],
        "textures": {"grain": {"amount": 0.035, "size": 1, "blend": "soft-light", "monochrome": True},
                     "vignette": {"amount": 0.1, "softness": 0.85, "color": DEEP}},
        "blender": {
            "samples": 128,
            "view_transform": "Standard",
            "look": "None",
            "exposure": 0,
            "bounces": {"total": 10, "diffuse": 3, "glossy": 3, "transmission": 8, "transparent": 4, "volume": 0},
            "world": WORLD,
            "materials": mats,
            "light_rig": plate_light(),
        },
    }


def storyboard() -> list[dict]:
    return [
        {"shot": "graine", "intent": "Poser l'origine : une graine d'argile qui devient un arbre, et la donnée qui naît avec lui.",
         "notes": "0:00-0:05 · fond uni bleu très clair ; la graine tombe (impact sur le temps 2), s'écrase, s'étire en "
                  "goutte et se fige en tronc à facettes ; la cime germe de son sommet ; traits, ondes, réticule, "
                  "graphiques ; « L'ORIGINE » coule le long de la branche ; caméra fixe puis léger recul (28 mm)."},
        {"shot": "expansion", "intent": "Faire grandir la ville comme un graphique, puis la connecter.",
         "notes": "0:05-0:11 · recul en diagonale vers la vue plongeante ; le sol se fend ; les tours poussent par vagues "
                  "(temps 12 à 20) ; le réseau s'allume depuis l'arbre : liens de proche en proche, pings, paquets de "
                  "données qui circulent, étiquettes ; la caméra marque un temps."},
        {"shot": "flux", "intent": "Plonger dans la ville connectée, sans coupure.",
         "notes": "0:11-0:20 · même plan-séquence : le drone pique (drop à 12 s, les néons s'allument), entre dans la rue "
                  "y = -2,5 et file droit vers l'arbre ; SYNERGIE, DURABLE, CONNECTÉ claquent sur les temps (12,5 / "
                  "14 / 15,5 s) et s'étirent au passage (14 / 15,5 / 17 s) ; arrêt au carrefour, montée et recul."},
        {"shot": "morphing", "intent": "Transformer la ville en regard.",
         "notes": "0:20-0:24 · apesanteur, explosion en ~2 000 particules, orbite de 180° plus vive ; l'œil vectoriel "
                  "prend le relais à 23,2 s, le diaphragme claque sur le battement de 23,5 s."},
        {"shot": "outro", "intent": "Conclure en douceur et signer.",
         "notes": "0:24-0:31 · dernier battement, clin d'œil, l'œil se ferme en un trait qui se gonfle en forme douce "
                  "sur dégradé pêche-corail ; « Visionnez Demain. » lettre par lettre ; note finale suspendue."},
    ]


def gradient_svg() -> str:
    return """<svg xmlns="http://www.w3.org/2000/svg" width="1920" height="1080" viewBox="0 0 1920 1080">
  <defs>
    <linearGradient id="peche" x1="0" y1="0" x2="1" y2="1">
      <stop offset="0" stop-color="#FFE3D1"/>
      <stop offset="0.5" stop-color="#FFBFA6"/>
      <stop offset="1" stop-color="#FF8F7B"/>
    </linearGradient>
    <radialGradient id="halo" cx="0.5" cy="0.43" r="0.46">
      <stop offset="0" stop-color="#FFF3EA" stop-opacity="0.9"/>
      <stop offset="1" stop-color="#FFF3EA" stop-opacity="0"/>
    </radialGradient>
  </defs>
  <rect width="1920" height="1080" fill="url(#peche)"/>
  <rect width="1920" height="1080" fill="url(#halo)"/>
</svg>
"""


def build_scene() -> dict:
    EVENTS.clear()
    ts = frames_between(T_SWOOP, T_GLIDE)
    sp = [(float(np.linalg.norm(swoop_loc(b) - swoop_loc(a))), (a + b) / 2) for a, b in zip(ts, ts[1:])]
    EVENTS["dive_peak"] = round(max(sp)[1], 4)
    EVENTS["landing"] = T_GLIDE
    EVENTS.update({"impact": T_IMPACT, "hatch": 1.3, "pull": T_PULL, "fracture": 5.32, "swoop": T_SWOOP,
                   "drop": T_DROP, "stop": T_STOP, "dronie": T_STOP})
    build_tree()
    build_city()
    TILES.clear()
    TILES.extend(tiles())
    build_network()
    build_rivers()
    shots = [shot_graine(), shot_expansion(), shot_flux(), shot_morphing(), shot_outro()]
    return {
        "$schema": "../schema/scene.schema.json",
        "version": "1.0",
        "id": SCENE_ID,
        "title": "Éclosion Urbaine — film concept",
        "brief": ("Film concept « Éclosion Urbaine », ~31 s, 16:9. Lumineux, pastel et néon (menthe, corail, jaune "
                  "électrique, bleu profond) ; 3D pâte à modeler (plaques Cycles) et interfaces 2D. Rythme organique au "
                  "début, rapide et technologique ensuite. Graine -> arbre low-poly -> ville qui pousse comme un "
                  "graphique et se connecte -> piqué et vol dans la rue, mots cinétiques -> explosion en particules qui "
                  "forment un œil vectoriel -> forme douce et slogan « Visionnez Demain. ». Plan-séquence 3D de 0 à 24 s. "
                  "Scène générée par scripts/scene_eclosion_urbaine.py (source), musique scripts/musique_eclosion_urbaine.py."),
        "seed": 2509,
        "preset": "clay_3d",
        "preset_overrides": preset_overrides(),
        "format": {"aspect": "16:9", "resolution": "1080p", "fps": FPS, "alpha": False},
        "quality": {"plate_scale": 1, "blender_samples": 128,
                    # 16 sous-images : à 6, les traits rapides (clin d'œil, mots au passage) se dédoublaient
                    "motion_blur": {"enabled": True, "samples": 16, "shutter": 0.5}},
        "audio": {"src": "assets/audio/eclosion_urbaine_musique.wav", "bpm": BPM, "offset": 0, "beats_per_bar": 4,
                  "normalize": True,
                  "markers": [{"id": "impact", "time": T_IMPACT}, {"id": "fracture", "time": 5.0},
                              {"id": "pique", "time": T_SWOOP}, {"id": "drop", "time": T_DROP},
                              {"id": "apesanteur", "time": T_GRAVITY}, {"id": "oeil", "time": T_EYE},
                              {"id": "outro", "time": SHOTS["outro"][0]}]},
        "storyboard": storyboard(),
        "outputs": [{"profile": "hevc_main10", "crf": 18}, {"profile": "prores_422hq"}],
        "qc": {"safe_zone": "title", "safe_zone_tolerance_s": 0.5, "allow_black_s": 0, "determinism_frames": 8,
               "loudness_target_lufs": -14},
        "shots": shots,
    }


def compute_events() -> dict:
    """Repères de la musique (mêmes calculs que la scène) : scripts/musique_eclosion_urbaine.py."""
    build_scene()
    return dict(EVENTS)


# =====================================================================================================
# Écriture (JSON compact par objet / calque, lisible au niveau des plans) et contrôles
# =====================================================================================================

def _dump(o, level: int, path: tuple) -> str:
    ind = "  " * level
    compact = json.dumps(o, ensure_ascii=False, separators=(", ", ": "))
    leaf = (len(path) >= 2 and path[-2] in ("objects", "layers")) or len(compact) <= 110
    if leaf or not isinstance(o, (dict, list)):
        return compact
    if isinstance(o, dict):
        items = [f'{ind}  {json.dumps(k, ensure_ascii=False)}: {_dump(v, level + 1, path + (k,))}' for k, v in o.items()]
        return "{\n" + ",\n".join(items) + f"\n{ind}}}"
    items = [f"{ind}  {_dump(v, level + 1, path + (n,))}" for n, v in enumerate(o)]
    return "[\n" + ",\n".join(items) + f"\n{ind}]"


def collisions(t0: float = 10.6, t1: float = 21.0, margin: float = 0.35) -> list[tuple[float, float, str]]:
    """Plus courte distance caméra - volumes des bâtiments (ville entière poussée), image par image."""
    lo, hi, ids = [], [], []
    for b in BUILDINGS:
        for p in b["parts"]:
            if p["role"] == "body":
                lo.append(p["loc"] - p["scale"] / 2)
                hi.append(p["loc"] + p["scale"] / 2)
                ids.append(p["id"])
    lo, hi = np.array(lo), np.array(hi)
    out = []
    for t in frames_between(t0, t1):
        c, _ = cam(t)
        d = np.linalg.norm(np.maximum(np.maximum(lo - c, 0), c - hi), axis=1)
        k = int(np.argmin(d))
        out.append((t, float(d[k]), ids[k]))
    return [o for o in out if o[1] < margin], min(out, key=lambda o: o[1])


def report(scene: dict) -> None:
    print("== Contrôles ==")
    for sh in scene["shots"]:
        n_obj = sum(len(p["objects"]) for p in sh.get("plates", []))
        n_tw = sum(len(L.get("anim", [])) for L in sh["layers"])
        print(f"  {sh['id']:<10} {sh['duration']:>5} s  objets 3D {n_obj:>5}  calques {len(sh['layers']):>3}  tweens {n_tw:>5}")
    print(f"  bâtiments {len(BUILDINGS)}, pièces {sum(len(b['parts']) for b in BUILDINGS)}, dalles {len(TILES)}, "
          f"nœuds du réseau {len(NETWORK['pts'])}, liens {len(NETWORK['tree_edges']) + len(NETWORK['extra'])}")
    ts = frames_between(T_SWOOP, T_GLIDE)
    sp = [(float(np.linalg.norm(swoop_loc(b) - swoop_loc(a))) * FPS, a) for a, b in zip(ts, ts[1:])]
    vmax, tmax = max(sp)
    print(f"  piqué : pointe {vmax:.1f} u/s à {tmax:.2f} s ; altitude à 12,0 s {swoop_loc(12.0)[2]:.1f} u, "
          f"à {T_SW_TURN} s {swoop_loc(T_SW_TURN)[2]:.1f} u ; vol à {V_GLIDE} u/s ; "
          f"arrêt en {P_STOP.round(2).tolist()} ; montée {DRONIE.length:.1f} u")
    for t in (0.0, 3.3, 5.0, 7.0, 9.4, 11.0, 11.5, 12.0, 12.6, 14.0, 17.0, 18.0, 19.5, 20.0, 21.0, 22.1, 23.2, 24.0):
        loc, tgt = cam(t)
        f, _, _ = basis(loc, tgt)
        print(f"  caméra {t:>5.2f} s : {loc.round(2).tolist()}  regard {f.round(2).tolist()}")
    # continuité : vitesse de part et d'autre des raccords
    for tj in (T_PULL, T_SWOOP, T_GLIDE, T_STOP, T_ORBIT, T_EYE):
        e = 1e-3
        v1 = (cam(tj)[0] - cam(tj - e)[0]) / e
        v2 = (cam(tj + e)[0] - cam(tj)[0]) / e
        print(f"  raccord {tj:>5.2f} s : vitesse avant {np.linalg.norm(v1):6.2f}, après {np.linalg.norm(v2):6.2f} u/s")
    for word, (t_r, t_p) in WORD_WINDOWS.items():
        print(f"  mot {word:<9} apparition {t_r} s, passage {t_p} s")
    bad, worst = collisions()
    print(f"  collisions (< 0,35 u d'un bâtiment) : {len(bad)} image(s) ; plus proche {worst[1]:.2f} u à {worst[0]:.2f} s ({worst[2]})")
    for t, d, pid in bad[:8]:
        print(f"    {t:.3f} s : {d:.2f} u de {pid}")
    xy, zc = project(EYE_C, *cam(T_EYE))
    print(f"  centre de l'œil ({T_EYE} s) : {xy.round(2).tolist()} à {zc:.2f} u")
    print(f"  repères musique : {json.dumps({k: v for k, v in EVENTS.items() if not isinstance(v, list)})}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--report", action="store_true", help="affiche les contrôles de trajets, raccords et collisions")
    args = ap.parse_args()
    scene = build_scene()
    text_out = _dump(scene, 0, ()) + "\n"
    SCENE_PATH.write_text(text_out, encoding="utf-8")
    grad = ROOT / GRADIENT_REL
    grad.parent.mkdir(parents=True, exist_ok=True)
    grad.write_text(gradient_svg(), encoding="utf-8")
    print(f"{SCENE_PATH.relative_to(ROOT)} : {len(text_out) / 1e6:.2f} Mo ; {GRADIENT_REL}")
    if args.report:
        report(scene)


if __name__ == "__main__":
    main()
