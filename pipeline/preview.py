"""Aperçu en direct d'une scène dans le navigateur : python mograph.py preview <scène>.

Un serveur local (127.0.0.1 uniquement) sert une page de contrôle et une page par plan. Chaque page
de plan est EXACTEMENT celle du rendu (runtime.js, polices, horloge virtuelle, graine) : la page de
contrôle positionne chaque plan sur l'image voulue avec window.__seek, au rythme du temps réel
(images sautées si le poste ne suit pas). Aucun fichier n'est écrit hors de build/.

- Sauvegarder la scène (ou un preset, un .srt) recompile et recharge l'aperçu en gardant l'image.
- Erreur de compilation : message affiché par-dessus l'aperçu (celui de « validate »).
- Plaque 3D pas encore rendue : image transparente à la place (bandeau d'avertissement).
- Transitions : fondu, volet et poussée reproduits en CSS (approximation fidèle du mélange final).
- Son : la piste de la scène joue en synchronisation (lecture, pause, déplacement).
L'image affichée n'est pas une garantie au pixel près (navigateur de bureau, GPU) : seul le rendu
(`all`) fait foi.
"""

from __future__ import annotations

import json
import os
import struct
import threading
import time
import urllib.parse
import webbrowser
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from pipeline import config

# PNG 1×1 transparent (plaque 3D non rendue) construit à la main : aucune dépendance, 67 octets.
def _png_1x1() -> bytes:
    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
    ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 6, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(b"\x00\x00\x00\x00\x00")) \
        + chunk(b"IEND", b"")


PLACEHOLDER_PNG = _png_1x1()


class _State:
    """Scène compilée courante, recompilée quand un fichier source change."""

    def __init__(self, scene_path: Path, log) -> None:
        self.scene_path = scene_path
        self.log = log
        self.lock = threading.Lock()
        self.version = 0
        self.stamp: tuple | None = None
        self.compiled: dict | None = None
        self.error: str | None = None
        self.warnings: list[str] = []

    def _sources_stamp(self) -> tuple:
        files = [self.scene_path, *sorted(config.PRESETS_DIR.glob("*.json"))]
        for ext in config.SUBTITLE_EXTENSIONS:
            files += sorted(config.ASSETS_DIR.rglob(f"*{ext}"))
        out = []
        for f in files:
            try:
                st = f.stat()
                out.append((str(f), st.st_mtime_ns, st.st_size))
            except OSError:
                out.append((str(f), None, None))
        return tuple(out)

    def refresh(self) -> None:
        with self.lock:
            stamp = self._sources_stamp()
            if stamp == self.stamp:
                return
            self.stamp = stamp
            from pipeline import media
            from pipeline import scene as scene_mod
            try:
                compiled = scene_mod.compile_scene(self.scene_path)
                for i in range(len(compiled["shots"])):
                    media.ensure_shot_media(compiled, i, log=self.log)
                self.compiled, self.error = compiled, None
                self.warnings = list(compiled.get("warnings") or [])
                self.log(f"aperçu : scène compilée ({compiled['frames']} images)")
            except scene_mod.SceneError as exc:
                self.error = str(exc)
                self.log("aperçu : scène refusée (message affiché dans le navigateur)")
            except Exception as exc:  # noqa: BLE001 — affiché dans l'aperçu plutôt que d'arrêter le serveur
                self.error = f"{type(exc).__name__} : {exc}"
            self.version += 1

    def api(self) -> dict:
        self.refresh()
        c = self.compiled
        out = {"version": self.version, "error": self.error, "warnings": self.warnings}
        if c is None:
            return out
        shots = []
        missing_plates = []
        for s in c["shots"]:
            for job in s.get("plates") or []:
                d = Path(job["out_dir"])
                n = len(list(d.glob("[0-9]" * 6 + ".png"))) if d.exists() else 0
                if n < int(job["frames"]):
                    missing_plates.append(f"{s['id']}/{job['plate_id']} ({n}/{job['frames']})")
            tr = s.get("transition") or {}
            shots.append({"id": s["id"], "index": s["index"], "start": s["global_start"], "frames": s["frames"],
                          "fade": s["fade_in_frames"], "transition": tr.get("type", "crossfade" if s["fade_in_frames"] else "cut"),
                          "direction": tr.get("direction", "left"), "url": f"/shot/{s['index']}?v={self.version}"})
        audio = c.get("audio") or None
        audio_url = None
        if audio and audio.get("src"):
            p = Path(audio["src"]).resolve()
            try:
                audio_url = "/assets/" + urllib.parse.quote(p.relative_to(config.ASSETS_DIR.resolve()).as_posix())
            except ValueError:
                audio_url = None
        out.update({"scene_id": c["scene_id"], "title": c["title"], "fps": c["format"]["fps"], "frames": c["frames"],
                    "canvas": c["canvas"], "alpha": c["format"]["alpha"], "shots": shots,
                    "audio": audio_url, "missing_plates": missing_plates})
        return out

    def shot_html(self, index: int) -> str | None:
        c = self.compiled
        if c is None or not (0 <= index < len(c["shots"])):
            return None
        spec = c["shots"][index]["spec"]
        # URLs du rendu (origine fictive) -> chemins du serveur local ; « </ » échappé dans le JSON inline.
        spec_json = json.dumps(spec, ensure_ascii=False).replace(config.ORIGIN, "").replace("</", "<\\/")
        return _SHOT_PAGE.format(seed=int(spec["seed"]), epoch=int(config.VIRTUAL_EPOCH_MS), spec=spec_json)


_SHOT_PAGE = """<!DOCTYPE html>
<html lang="fr"><head><meta charset="utf-8">
<link rel="stylesheet" href="/runtime/runtime.css">
<style>html,body{{margin:0;background:transparent;overflow:hidden}}</style>
<script>
window.__MOGRAPH_SEED__ = {seed};
window.__MOGRAPH_EPOCH_MS__ = {epoch};
window.__MOGRAPH_SHOT__ = {spec};
</script>
<script src="/runtime/clock.js"></script>
</head><body>
<div id="mg-root"></div>
<script src="/node_modules/gsap/dist/gsap.min.js"></script>
<script src="/node_modules/gsap/dist/CustomEase.min.js"></script>
<script src="/runtime/runtime.js"></script>
<script>
(function () {{
  // Pilote de l'aperçu : la page de contrôle demande une image, on répond quand elle est peinte.
  var chain = Promise.resolve();
  window.addEventListener("message", function (ev) {{
    var m = ev.data || {{}};
    if (m.cmd !== "seek") return;
    chain = chain.then(function () {{
      if (window.__MOGRAPH_READY__ !== true) return;
      return window.__seek(m.frame, 0);
    }}).then(function () {{
      parent.postMessage({{ack: m.id, shot: window.__MOGRAPH_SHOT__.shot_id}}, "*");
    }}, function (e) {{
      parent.postMessage({{ack: m.id, error: String(e && e.message || e)}}, "*");
    }});
  }});
  var t = setInterval(function () {{
    if (typeof window.__MOGRAPH_ERROR__ === "string") {{
      clearInterval(t); parent.postMessage({{error: window.__MOGRAPH_ERROR__, shot: window.__MOGRAPH_SHOT__.shot_id}}, "*");
    }} else if (window.__MOGRAPH_READY__ === true) {{
      clearInterval(t); parent.postMessage({{ready: window.__MOGRAPH_SHOT__.shot_id}}, "*");
    }}
  }}, 30);
}})();
</script>
</body></html>
"""

_CONTROL_PAGE = """<!DOCTYPE html>
<html lang="fr"><head><meta charset="utf-8"><title>mograph — aperçu</title>
<style>
:root { color-scheme: dark; --bg:#141418; --panel:#1f1f26; --fg:#ececf1; --mute:#9a9aa8; --acc:#7c9cff; --err:#ff6b6b; --warn:#ffc857; }
* { box-sizing: border-box; }
body { margin:0; background:var(--bg); color:var(--fg); font:14px/1.4 system-ui, sans-serif; height:100vh; display:flex; flex-direction:column; }
header { padding:8px 14px; display:flex; gap:12px; align-items:baseline; background:var(--panel); }
header b { font-size:15px; } header span { color:var(--mute); }
#viewport { flex:1; position:relative; overflow:hidden; display:flex; align-items:center; justify-content:center; }
#stage { position:relative; transform-origin:0 0; box-shadow:0 0 0 1px #333; }
#stage.checker { background: repeating-conic-gradient(#555 0% 25%, #3a3a3a 0% 50%) 0 0/32px 32px; }
#stage iframe { position:absolute; left:0; top:0; border:0; visibility:hidden; background:transparent; }
#bar { padding:8px 14px; background:var(--panel); display:flex; gap:10px; align-items:center; }
#bar button { background:#2c2c36; color:var(--fg); border:1px solid #3a3a46; border-radius:6px; padding:5px 10px; cursor:pointer; font-size:14px; }
#bar button.on { border-color:var(--acc); color:var(--acc); }
#scrub { flex:1; accent-color: var(--acc); }
#tc { font-variant-numeric: tabular-nums; min-width: 190px; text-align:right; color:var(--mute); }
#msg { position:absolute; left:16px; right:16px; top:16px; white-space:pre-wrap; font:13px/1.45 ui-monospace, monospace;
       padding:12px 14px; border-radius:8px; display:none; max-height:70%; overflow:auto; z-index:10; }
#msg.err { display:block; background:#2a1416; border:1px solid var(--err); color:#ffd6d6; }
#msg.warn { display:block; background:#2a2414; border:1px solid var(--warn); color:#ffeec2; }
</style></head><body>
<header><b id="title">…</b><span id="meta"></span></header>
<div id="viewport"><div id="msg"></div><div id="stage"></div></div>
<div id="bar">
  <button id="play" title="Espace">▶ Lecture</button>
  <button id="prev" title="←">◀︎ 1</button><button id="next" title="→">1 ▶︎</button>
  <button id="loop" class="on" title="L">Boucle</button>
  <input id="scrub" type="range" min="0" max="0" value="0">
  <span id="tc">0</span>
</div>
<audio id="audio" preload="auto"></audio>
<script>
"use strict";
let S = null, frames = {}, ready = {}, frameNow = 0, playing = false, busy = false, pending = null;
let seq = 0, waiters = {}, version = -1, t0 = 0, f0 = 0;
const stage = document.getElementById("stage"), scrub = document.getElementById("scrub");
const audio = document.getElementById("audio"), msg = document.getElementById("msg");
const smooth = (x) => x * x * (3 - 2 * x);

function tc(g) { const f = g % S.fps, s = Math.floor(g / S.fps); return `${String(Math.floor(s/60)).padStart(2,"0")}:${String(s%60).padStart(2,"0")}:${String(f).padStart(2,"0")}`; }
function showMsg(text, kind) { msg.textContent = text || ""; msg.className = text ? kind : ""; }

window.addEventListener("message", (ev) => {
  const m = ev.data || {};
  if (m.ack !== undefined && waiters[m.ack]) { waiters[m.ack](m); delete waiters[m.ack]; }
  if (m.ready) { ready[m.ready] = true; if (!playing) show(frameNow); }
  if (m.error) showMsg(`Plan « ${m.shot} » : ${m.error}`, "err");
});

function seekShot(shot, local) {
  const f = frames[shot.id]; if (!f || !ready[shot.id]) return Promise.resolve();
  return new Promise((res) => { const id = ++seq; waiters[id] = res; f.contentWindow.postMessage({cmd:"seek", frame: local, id}, "*"); });
}

async function show(g) {
  if (!S) return;
  if (busy) { pending = g; return; }
  busy = true;
  g = Math.max(0, Math.min(S.frames - 1, g));
  const act = S.shots.filter((s) => g >= s.start && g < s.start + s.frames);
  await Promise.all(act.map((s) => seekShot(s, g - s.start)));
  const W = S.canvas.width, H = S.canvas.height;
  for (const s of S.shots) {
    const f = frames[s.id]; if (!f) continue;
    const pos = act.indexOf(s);
    if (pos < 0) { f.style.visibility = "hidden"; continue; }
    f.style.visibility = "visible"; f.style.opacity = 1; f.style.clipPath = "none"; f.style.transform = "none";
    f.style.zIndex = String(s.index);
    const k = g - s.start;
    if (pos > 0 && s.fade && k < s.fade) {
      const w = smooth((k + 1) / (s.fade + 1)), d = s.direction;
      if (s.transition === "wipe") {
        const r = (1 - w) * 100;
        f.style.clipPath = d === "left" ? `inset(0 0 0 ${r}%)` : d === "right" ? `inset(0 ${r}% 0 0)` : d === "up" ? `inset(${r}% 0 0 0)` : `inset(0 0 ${r}% 0)`;
      } else if (s.transition === "push") {
        const prev = act[pos - 1], pf = frames[prev.id];
        const dx = d === "left" ? -1 : d === "right" ? 1 : 0, dy = d === "up" ? -1 : d === "down" ? 1 : 0;
        const off = Math.round(w * (dx ? W : H));
        pf.style.transform = `translate(${dx * off}px, ${dy * off}px)`;
        f.style.transform = `translate(${-dx * ((dx ? W : H) - off)}px, ${-dy * ((dy ? H : W) - off)}px)`;
      } else {
        f.style.opacity = w;
      }
    }
  }
  frameNow = g; scrub.value = g;
  document.getElementById("tc").textContent = `${tc(g)}   image ${g} / ${S.frames - 1}`;
  busy = false;
  if (pending !== null) { const p = pending; pending = null; show(p); }
}

function tick() {
  if (!playing) return;
  const g = f0 + Math.floor((performance.now() - t0) / 1000 * S.fps);
  if (g >= S.frames) {
    if (document.getElementById("loop").classList.contains("on")) { start(0); }
    else { stop(); show(S.frames - 1); return; }
  } else if (g !== frameNow && !busy) { show(g); }
  requestAnimationFrame(tick);
}
function start(from) {
  f0 = from; t0 = performance.now(); playing = true;
  document.getElementById("play").textContent = "❚❚ Pause";
  if (S.audio) { audio.currentTime = from / S.fps; audio.play().catch(() => {}); }
  requestAnimationFrame(tick);
}
function stop() { playing = false; document.getElementById("play").textContent = "▶ Lecture"; audio.pause(); }

function fit() {
  if (!S) return;
  const vp = document.getElementById("viewport");
  const sc = Math.min((vp.clientWidth - 24) / S.canvas.width, (vp.clientHeight - 24) / S.canvas.height);
  stage.style.width = S.canvas.width + "px"; stage.style.height = S.canvas.height + "px";
  stage.style.transform = `scale(${sc})`;
  stage.style.marginRight = (S.canvas.width * (sc - 1)) + "px"; stage.style.marginBottom = (S.canvas.height * (sc - 1)) + "px";
}

function build(data) {
  const keep = S ? frameNow : 0;
  S = data; frames = {}; ready = {}; stage.innerHTML = "";
  document.getElementById("title").textContent = `${data.scene_id} — ${data.title}`;
  document.getElementById("meta").textContent = `${data.canvas.width}×${data.canvas.height} · ${data.fps} i/s · ${data.frames} images · ${data.shots.length} plan(s)`;
  stage.classList.toggle("checker", !!data.alpha);
  for (const s of data.shots) {
    const f = document.createElement("iframe");
    f.width = data.canvas.width; f.height = data.canvas.height; f.src = s.url;
    stage.appendChild(f); frames[s.id] = f;
  }
  scrub.max = data.frames - 1;
  if (data.audio) { if (!audio.src.endsWith(data.audio)) audio.src = data.audio; } else { audio.removeAttribute("src"); }
  frameNow = Math.min(keep, data.frames - 1);
  fit();
}

async function poll() {
  try {
    const r = await fetch(`/api/scene?v=${version}`, {cache: "no-store"});
    const data = await r.json();
    if (data.version !== version) {
      version = data.version;
      if (data.error) { showMsg("Scène refusée :\\n" + data.error, "err"); }
      else {
        const notes = [];
        if (data.missing_plates.length) notes.push("Plaques 3D non rendues (transparentes dans l'aperçu) : " + data.missing_plates.join(", ") + "\\n→ python mograph.py plates <scène>");
        if (data.warnings.length) notes.push("Avertissements :\\n- " + data.warnings.join("\\n- "));
        showMsg(notes.join("\\n\\n"), notes.length ? "warn" : "");
        build(data);
      }
    }
  } catch (e) { showMsg("Serveur d'aperçu injoignable (arrêté ?)", "err"); }
  setTimeout(poll, 800);
}

document.getElementById("play").onclick = () => playing ? stop() : start(frameNow >= S.frames - 1 ? 0 : frameNow);
document.getElementById("prev").onclick = () => { stop(); show(frameNow - 1); };
document.getElementById("next").onclick = () => { stop(); show(frameNow + 1); };
document.getElementById("loop").onclick = (e) => e.target.classList.toggle("on");
scrub.oninput = () => { stop(); show(+scrub.value); };
window.addEventListener("resize", fit);
window.addEventListener("keydown", (e) => {
  if (!S) return;
  if (e.code === "Space") { e.preventDefault(); document.getElementById("play").click(); }
  else if (e.code === "ArrowLeft") { stop(); show(frameNow - (e.shiftKey ? S.fps : 1)); }
  else if (e.code === "ArrowRight") { stop(); show(frameNow + (e.shiftKey ? S.fps : 1)); }
  else if (e.code === "Home") { stop(); show(0); }
  else if (e.code === "KeyL") { document.getElementById("loop").click(); }
});
// Accès pour les tests automatiques.
window.__preview = { show: (g) => show(g), state: () => ({ frame: frameNow, version, ready: Object.keys(ready), error: msg.className === "err" ? msg.textContent : null }) };
poll();
</script></body></html>
"""


def _make_handler(state: _State):
    prefixes = [(pfx, Path(base).resolve()) for pfx, base in config.SERVED_PREFIXES.items()]
    build_root = config.BUILD_DIR.resolve()

    class Handler(BaseHTTPRequestHandler):
        server_version = "mograph-preview"

        def log_message(self, fmt, *args):  # silencieux : le terminal reste lisible
            return

        def _send(self, code: int, body: bytes, ctype: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802 — nom imposé par http.server
            parts = urllib.parse.urlsplit(self.path)
            path = urllib.parse.unquote(parts.path)
            if path in ("/", "/index.html"):
                return self._send(200, _CONTROL_PAGE.encode("utf-8"), "text/html; charset=utf-8")
            if path == "/api/scene":
                return self._send(200, json.dumps(state.api(), ensure_ascii=False).encode("utf-8"),
                                  "application/json; charset=utf-8")
            if path.startswith("/shot/"):
                try:
                    html = state.shot_html(int(path[len("/shot/"):]))
                except ValueError:
                    html = None
                if html is None:
                    return self._send(404, b"plan inconnu", "text/plain; charset=utf-8")
                return self._send(200, html.encode("utf-8"), "text/html; charset=utf-8")
            if path == "/favicon.ico":
                return self._send(204, b"", "image/x-icon")
            for pfx, base in prefixes:
                if not path.startswith(pfx):
                    continue
                target = (base / path[len(pfx):]).resolve()
                if base != target and base not in target.parents:
                    break  # traversée « ../ » refusée
                if target.is_file():
                    mime = config.MIME_TYPES.get(target.suffix.lower(), "application/octet-stream")
                    return self._send(200, target.read_bytes(), mime)
                # Image de plaque 3D pas encore rendue : transparente, l'aperçu reste utilisable.
                if build_root in target.parents and "plates" in target.parts and target.suffix == ".png":
                    return self._send(200, PLACEHOLDER_PNG, "image/png")
                break
            return self._send(404, b"introuvable", "text/plain; charset=utf-8")

    return Handler


def serve(scene_path: Path, *, port: int = 8765, open_browser: bool = True, log=print,
          ready_event: threading.Event | None = None, stop_event: threading.Event | None = None) -> None:
    """Lance le serveur d'aperçu (bloquant) ; Ctrl+C pour arrêter."""
    state = _State(Path(scene_path).resolve(), log)
    state.refresh()
    httpd = ThreadingHTTPServer(("127.0.0.1", port), _make_handler(state))
    httpd.daemon_threads = True
    url = f"http://127.0.0.1:{httpd.server_address[1]}/"
    log(f"Aperçu : {url}  (Ctrl+C pour arrêter ; sauvegardez la scène pour recharger)")
    if state.error:
        log("La scène est actuellement refusée : le message s'affiche dans l'aperçu.")
    if open_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    if ready_event is not None:
        ready_event.url = url  # type: ignore[attr-defined]
        ready_event.set()
    if stop_event is not None:
        threading.Thread(target=lambda: (stop_event.wait(), httpd.shutdown()), daemon=True).start()
    try:
        httpd.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        log("Aperçu arrêté.")
    finally:
        httpd.server_close()
