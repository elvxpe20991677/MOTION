/*
 * clock.js — horloge virtuelle et hasard déterministe du runtime mograph.
 *
 * Injecté par pipeline/web_render.py via context.add_init_script, APRÈS la définition de
 * window.__MOGRAPH_SEED__ / window.__MOGRAPH_SHOT__ et AVANT tout script de la page : GSAP,
 * CustomEase et runtime.js ne voient donc jamais l'horloge ni le Math.random réels.
 *
 * Garanties :
 *   - Math.random = mulberry32(__MOGRAPH_SEED__) : même graine => même suite de nombres.
 *   - Date.now(), new Date(), Date() et performance.now() lisent une horloge VIRTUELLE que seul
 *     le pilote fait avancer (window.__MOGRAPH_CLOCK__.set(ms)) ; époque = 2025-01-01T00:00:00Z.
 *   - requestAnimationFrame n'est PAS modifié : le pilote en a besoin pour attendre la peinture.
 */
(function () {
  "use strict";

  // Garde : un init script est réexécuté dans chaque document (iframes comprises) ; on n'installe
  // l'horloge qu'une fois par fenêtre pour ne pas empiler les enveloppes.
  if (window.__MOGRAPH_CLOCK__) {
    return;
  }

  // Époque transmise par le pilote (config.VIRTUAL_EPOCH_MS) ; la valeur de repli est la même
  // constante, utile seulement si clock.js est chargé hors pilote (débogage manuel).
  var EPOCH_MS = typeof window.__MOGRAPH_EPOCH_MS__ === "number" ? window.__MOGRAPH_EPOCH_MS__ : 1735689600000;

  // Graine uint32 : un nombre invalide retombe sur 0 plutôt que de produire NaN partout.
  var seed = Number(window.__MOGRAPH_SEED__);
  seed = Number.isFinite(seed) ? (seed >>> 0) : 0;

  // mulberry32 : PRNG 32 bits minuscule, rapide et bien distribué, entièrement défini par sa graine
  // (aucun état caché du moteur JS), donc identique d'un lancement à l'autre.
  function mulberry32(a) {
    return function () {
      a = (a + 0x6D2B79F5) | 0;
      var t = Math.imul(a ^ (a >>> 15), 1 | a);
      t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
      return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
    };
  }

  var random = mulberry32(seed);
  // defineProperty plutôt qu'une affectation : la propriété reste modifiable (configurable) mais
  // n'est pas énumérable, comme l'originale.
  Object.defineProperty(Math, "random", { value: random, writable: true, configurable: true, enumerable: false });

  // Temps virtuel en millisecondes depuis le début du plan ; 0 tant que le pilote n'a rien réglé.
  var virtualMs = 0;

  var clock = {
    set: function (ms) {
      var v = Number(ms);
      if (!Number.isFinite(v)) {
        throw new Error("__MOGRAPH_CLOCK__.set : valeur non numérique (" + String(ms) + ")");
      }
      virtualMs = v;
      return virtualMs;
    },
    get: function () {
      return virtualMs;
    },
    epoch: EPOCH_MS,
    mulberry32: mulberry32
  };
  Object.defineProperty(window, "__MOGRAPH_CLOCK__", { value: clock, writable: false, configurable: false, enumerable: false });

  // ---- Date virtuelle -------------------------------------------------------------------------
  var RealDate = window.Date;

  function nowMs() {
    return EPOCH_MS + virtualMs;
  }

  // Fonction constructeur (et non classe) : Date() appelé sans new doit renvoyer une chaîne,
  // ce qu'une classe ES6 interdit.
  function VirtualDate() {
    var args = Array.prototype.slice.call(arguments);
    if (!new.target) {
      return new RealDate(nowMs()).toString();
    }
    // Reflect.construct conserve new.target : une sous-classe de Date (rare) reste correcte.
    return args.length === 0
      ? Reflect.construct(RealDate, [nowMs()], new.target)
      : Reflect.construct(RealDate, args, new.target);
  }
  VirtualDate.prototype = RealDate.prototype;
  VirtualDate.now = function now() {
    return nowMs();
  };
  VirtualDate.UTC = RealDate.UTC;
  VirtualDate.parse = RealDate.parse;
  Object.defineProperty(VirtualDate.prototype, "constructor", { value: VirtualDate, writable: true, configurable: true, enumerable: false });
  window.Date = VirtualDate;

  // ---- performance.now virtuel ----------------------------------------------------------------
  // Propriété propre sur l'objet performance : masque Performance.prototype.now sans le détruire.
  // Origine 0 = début du plan ; GSAP lit ce temps pour son ticker (endormi pendant le rendu).
  try {
    Object.defineProperty(window.performance, "now", {
      value: function now() {
        return virtualMs;
      },
      writable: true,
      configurable: true,
      enumerable: false
    });
  } catch (e) {
    // Environnement exotique où performance est gelé : on signale plutôt que de rendre au hasard.
    window.__MOGRAPH_ERROR__ = "clock.js : impossible de remplacer performance.now (" + (e && e.message) + ")";
  }
})();
