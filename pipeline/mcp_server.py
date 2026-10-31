"""Serveur MCP local « mograph » (stdio, JSON-RPC 2.0, bibliothèque standard seule) — phase 6.

Expose à un agent (Claude Code, Claude Desktop…) les commandes de mograph sous forme d'outils à
sorties JSON structurées : validate, preview, draft, all, status, beats, sheets. Chaque outil lance
« mograph.py » dans un sous-processus (la sortie standard du serveur reste réservée au protocole) et
renvoie {content: [texte JSON], structuredContent, isError}. Les erreurs d'exécution (scène refusée,
confirmation absente…) sont des résultats isError=true avec un message actionnable ; les erreurs de
protocole (outil inconnu, paramètres mal formés) sont des erreurs JSON-RPC. Aucune action qui remplace
des fichiers (rendus, écriture d'une scène) sans le paramètre « confirm »: true. Spécification MCP
2025-11-25 (modelcontextprotocol.io) : cycle initialize / notifications/initialized, tools/list,
tools/call, ping ; chaque tools/call s'exécute dans son propre fil (ping toujours servi) et
notifications/cancelled arrête son sous-processus (aucune réponse n'est alors envoyée). CONTRACT §11.7.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
import time

from pipeline import config
from pipeline.claude_cli import kill_tree

# 2025-03-26 n'est pas proposée : elle impose de recevoir des lots JSON-RPC, que ce serveur ne traite pas.
PROTOCOL_VERSIONS = ("2025-11-25", "2025-06-18")
SERVER_INFO = {"name": "mograph", "title": "MOTION (mograph)", "version": "1.0.0"}
INSTRUCTIONS = (
    "Outils du pipeline de motion design déterministe MOTION. Démarche : validate (corriger avec les messages "
    "renvoyés, écrits pour être appliqués tels quels) -> draft (préversion, confirm: true) -> status (avancement) "
    "-> sheets (planches contact, vision: true pour une relecture visuelle indicative) -> all (rendu final, "
    "confirm: true). Les rendus sont détachés : suivez-les avec status. Une scène = chemin d'un .json ou id de "
    "scenes/. Règles d'écriture d'une scène : docs/GUIDE_SCENES.md.")

_SCENE = {"type": "string", "description": "chemin d'un fichier .json de scène, ou id d'une scène de scenes/"}
_CONFIRM = {"type": "boolean", "description": "true obligatoire : l'action remplace des fichiers (rendus ou scène)"}
TOOLS = [
    {"name": "validate", "title": "Valider une scène",
     "description": "Valide et compile une scène : format, images, plans ; erreurs et avertissements actionnables. "
                    "layout: true contrôle aussi les zones sûres sur quelques images rendues.",
     "inputSchema": {"type": "object", "additionalProperties": False, "required": ["scene"],
                     "properties": {"scene": _SCENE, "layout": {"type": "boolean"}}},
     "annotations": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}},
    {"name": "preview", "title": "Aperçu en direct",
     "description": "Démarre l'aperçu navigateur de la scène (rechargé à chaque sauvegarde) dans un processus "
                    "détaché ; renvoie l'URL.",
     "inputSchema": {"type": "object", "additionalProperties": False, "required": ["scene"],
                     "properties": {"scene": _SCENE, "port": {"type": "integer", "minimum": 1024, "maximum": 65535}}},
     "annotations": {"readOnlyHint": True, "destructiveHint": False, "openWorldHint": False}},
    {"name": "draft", "title": "Préversion",
     "description": "Lance la préversion rapide (1080p, sans QC) en arrière-plan ; remplace la préversion précédente. "
                    "Suivre avec status (draft: true).",
     "inputSchema": {"type": "object", "additionalProperties": False, "required": ["scene", "confirm"],
                     "properties": {"scene": _SCENE, "confirm": _CONFIRM}},
     "annotations": {"readOnlyHint": False, "destructiveHint": True, "idempotentHint": True, "openWorldHint": False}},
    {"name": "all", "title": "Rendu final",
     "description": "Lance le rendu complet (plaques, images, déterminisme, maître, encodage, QC) en arrière-plan ; "
                    "remplace les livrables précédents. Suivre avec status.",
     "inputSchema": {"type": "object", "additionalProperties": False, "required": ["scene", "confirm"],
                     "properties": {"scene": _SCENE, "confirm": _CONFIRM}},
     "annotations": {"readOnlyHint": False, "destructiveHint": True, "idempotentHint": True, "openWorldHint": False}},
    {"name": "status", "title": "Avancement",
     "description": "Avancement d'un rendu (images par plan, maître, déterminisme, verdict QC, processus détachés).",
     "inputSchema": {"type": "object", "additionalProperties": False, "required": ["scene"],
                     "properties": {"scene": _SCENE, "draft": {"type": "boolean", "description": "préversion"}}},
     "annotations": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}},
    {"name": "beats", "title": "Tempo d'une musique",
     "description": "Tempo, premier temps fort, drop et dérive d'une musique (gelée si elle vient d'une IA). "
                    "write: true (avec scene et confirm: true) écrit le bloc audio dans la scène.",
     "inputSchema": {"type": "object", "additionalProperties": False, "required": ["audio"],
                     "properties": {"audio": {"type": "string", "description": "fichier audio (chemin relatif à la racine)"},
                                    "scene": _SCENE, "write": {"type": "boolean"}, "confirm": _CONFIRM}},
     "annotations": {"readOnlyHint": False, "destructiveHint": True, "openWorldHint": False}},
    {"name": "sheets", "title": "Planches contact",
     "description": "Planches contact d'une scène déjà rendue ; vision: true ajoute une relecture visuelle par Claude "
                    "(abonnement, avertissements seulement, jamais bloquante).",
     "inputSchema": {"type": "object", "additionalProperties": False, "required": ["scene"],
                     "properties": {"scene": _SCENE, "vision": {"type": "boolean"},
                                    "draft": {"type": "boolean", "description": "planches de la préversion"}}},
     "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}},
]
_BY_NAME = {t["name"]: t for t in TOOLS}


class ToolFailure(Exception):
    """Échec d'exécution d'un outil : renvoyé comme résultat isError=true (pas une erreur de protocole).
    `data` : sortie structurée jointe (ex. erreurs du compilateur d'une scène refusée)."""

    def __init__(self, message: str, data: dict | None = None):
        super().__init__(message)
        self.data = data or {}


# Requête traitée par ce fil (annulation) et sous-processus de chaque requête en cours.
_CTX = threading.local()
_LOCK = threading.Lock()
_PROCS: dict = {}
_CANCELLED: set = set()


def _mograph(*args: str, timeout: float = 1800.0) -> subprocess.CompletedProcess:
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    rid = getattr(_CTX, "rid", None)
    proc = subprocess.Popen([sys.executable, str(config.ROOT / "mograph.py"), *args], cwd=config.ROOT, env=env,
                            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            start_new_session=not config.IS_WINDOWS)
    with _LOCK:
        _PROCS.setdefault(rid, []).append(proc)
        cancelled = rid is not None and rid in _CANCELLED
    if cancelled:
        kill_tree(proc)
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        kill_tree(proc)
        proc.communicate()
        raise ToolFailure(f"mograph {args[0]} n'a pas terminé en {timeout:g} s.") from None
    finally:
        with _LOCK:
            if proc in _PROCS.get(rid, []):
                _PROCS[rid].remove(proc)
    return subprocess.CompletedProcess(proc.args, proc.returncode, out.decode("utf-8", "replace"),
                                       err.decode("utf-8", "replace"))


def cancel(rid) -> None:
    """notifications/cancelled : arrête les sous-processus de la requête ; sa réponse ne sera pas envoyée."""
    with _LOCK:
        _CANCELLED.add(rid)
        procs = list(_PROCS.get(rid, []))
    for proc in procs:
        kill_tree(proc)


def _json_out(cp: subprocess.CompletedProcess, what: str) -> dict:
    try:
        return json.loads(cp.stdout)
    except ValueError:
        raise ToolFailure(f"{what} : sortie inattendue (code {cp.returncode}) : "
                          f"{(cp.stderr or cp.stdout).strip()[-600:]}") from None


def _scene_arg(args: dict) -> str:
    s = args.get("scene")
    if not isinstance(s, str) or not s.strip():
        raise ToolFailure("paramètre « scene » manquant : chemin d'un .json ou id d'une scène de scenes/.")
    return s


def _need_confirm(args: dict, what: str) -> None:
    if args.get("confirm") is not True:
        raise ToolFailure(f"{what} remplace des fichiers : relancez l'outil avec « confirm »: true après accord de "
                          "l'utilisateur.")


def _port_open(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.3)
        return s.connect_ex(("127.0.0.1", port)) == 0


def tool_validate(args: dict) -> dict:
    argv = ["validate", _scene_arg(args), "--json"] + (["--layout"] if args.get("layout") else [])
    cp = _mograph(*argv)
    out = _json_out(cp, "validate")
    if cp.returncode != 0:
        what = "scène refusée" if not out.get("ok") else "mise en page NON CONFORME (le QC final échouera)"
        raise ToolFailure(f"{what} : corrigez la scène avec les messages joints, puis relancez validate.", out)
    return out


def tool_preview(args: dict) -> dict:
    scene, port = _scene_arg(args), int(args.get("port") or 8765)
    if _port_open(port):
        raise ToolFailure(f"le port {port} est déjà occupé (aperçu déjà lancé ?) : ouvrez http://127.0.0.1:{port}/ "
                          "ou choisissez un autre « port ».")
    log_dir = config.BUILD_DIR / "_mcp"
    log_dir.mkdir(parents=True, exist_ok=True)
    log = log_dir / f"preview_{port}.log"
    flags = 0x00000008 | 0x00000200 if config.IS_WINDOWS else 0  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    with open(log, "w", encoding="utf-8") as fh:
        proc = subprocess.Popen([sys.executable, str(config.ROOT / "mograph.py"), "preview", scene, "--no-open",
                                 "--port", str(port)], cwd=config.ROOT, stdout=fh, stderr=subprocess.STDOUT,
                                stdin=subprocess.DEVNULL, creationflags=flags, start_new_session=not config.IS_WINDOWS,
                                env=dict(os.environ, PYTHONIOENCODING="utf-8"))
    for _ in range(100):
        if _port_open(port):
            return {"url": f"http://127.0.0.1:{port}/", "pid": proc.pid, "log": str(log)}
        if proc.poll() is not None:
            break
        time.sleep(0.1)
    raise ToolFailure(f"l'aperçu n'a pas démarré : {log.read_text(encoding='utf-8', errors='replace')[-600:]}")


def _detached(cmd: list[str], scene: str) -> dict:
    draft = "--draft" in cmd
    st = _json_out(_mograph("status", scene, "--json", *(["--draft"] if draft else []), timeout=120), "status")
    running = [d for d in st.get("detached", []) if d.get("running")]
    if running:
        raise ToolFailure(f"un rendu de {st.get('scene_id')} est déjà en cours (PID {running[0]['pid']}, "
                          f"{running[0]['command']}) : attendez sa fin (outil status) avant d'en relancer un.")
    cp = _mograph(*cmd, "--detach", timeout=120)
    if cp.returncode != 0:
        raise ToolFailure((cp.stderr or cp.stdout).strip()[-800:] or f"{cmd[0]} : échec du lancement")
    return {"started": True, "scene": scene, "output": cp.stdout.strip()[-800:],
            "next": "suivez l'avancement avec l'outil status" + (" (draft: true)" if draft else "")}


def _validated(scene: str) -> None:
    # Scène refusée : erreurs du compilateur tout de suite, plutôt qu'un rendu détaché qui échoue aussitôt.
    cp = _mograph("validate", scene, "--json", timeout=300)
    out = _json_out(cp, "validate")
    if cp.returncode != 0:
        raise ToolFailure("scène refusée : rendu NON lancé ; corrigez la scène avec les messages joints.", out)


def tool_draft(args: dict) -> dict:
    scene = _scene_arg(args)
    _need_confirm(args, "la préversion")
    _validated(scene)
    return _detached(["all", scene, "--draft"], scene)


def tool_all(args: dict) -> dict:
    scene = _scene_arg(args)
    _need_confirm(args, "le rendu complet")
    _validated(scene)
    return _detached(["all", scene], scene)


def tool_status(args: dict) -> dict:
    argv = ["status", _scene_arg(args), "--json"] + (["--draft"] if args.get("draft") else [])
    return _json_out(_mograph(*argv, timeout=120), "status")


def tool_beats(args: dict) -> dict:
    audio = args.get("audio")
    if not isinstance(audio, str) or not audio.strip():
        raise ToolFailure("paramètre « audio » manquant : chemin du fichier audio (relatif à la racine).")
    argv = ["beats", audio, "--json"]
    if args.get("write"):
        _need_confirm(args, "l'écriture du bloc audio dans la scène")
        argv += ["--scene", _scene_arg(args), "--write"]
    cp = _mograph(*argv)
    if cp.returncode != 0:
        raise ToolFailure((cp.stderr or cp.stdout).strip()[-800:])
    out = _json_out(cp, "beats")
    if cp.stderr.strip():
        out["messages"] = cp.stderr.strip().splitlines()
    return out


def tool_sheets(args: dict) -> dict:
    argv = ["sheets", _scene_arg(args), "--json"] + (["--vision"] if args.get("vision") else [])         + (["--draft"] if args.get("draft") else [])
    cp = _mograph(*argv)
    out = _json_out(cp, "sheets")
    if cp.returncode != 0:
        raise ToolFailure(f"planches contact impossibles : {out.get('error')}", out)
    return out


HANDLERS = {"validate": tool_validate, "preview": tool_preview, "draft": tool_draft, "all": tool_all,
            "status": tool_status, "beats": tool_beats, "sheets": tool_sheets}


def _result(data: dict, error: bool = False) -> dict:
    return {"content": [{"type": "text", "text": json.dumps(data, ensure_ascii=False)}], "structuredContent": data,
            "isError": error}


def _valid_id(x) -> bool:
    return isinstance(x, str) or (isinstance(x, int) and not isinstance(x, bool))


def handle(msg: dict) -> dict | None:
    """Traite UN message JSON-RPC ; renvoie la réponse (None pour une notification)."""
    if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0" or not isinstance(msg.get("method"), str) \
            or ("id" in msg and not _valid_id(msg["id"])):
        rid = msg.get("id") if isinstance(msg, dict) and _valid_id(msg.get("id")) else None
        return {"jsonrpc": "2.0", "id": rid,
                "error": {"code": -32600, "message": "Requête JSON-RPC 2.0 invalide (id : chaîne ou entier ; lots non "
                                                     "pris en charge)."}}
    mid, method, params = msg.get("id"), msg["method"], msg.get("params")
    if "id" not in msg:
        if method == "notifications/cancelled" and isinstance(params, dict) and _valid_id(params.get("requestId")):
            cancel(params["requestId"])
        return None  # notification (notifications/initialized, notifications/cancelled…) : aucune réponse

    def ok(result):
        return {"jsonrpc": "2.0", "id": mid, "result": result}

    def err(code, message, data=None):
        e = {"code": code, "message": message}
        if data is not None:
            e["data"] = data
        return {"jsonrpc": "2.0", "id": mid, "error": e}

    if params is None:
        params = {}
    if not isinstance(params, dict):
        return err(-32602, "« params » doit être un objet.")
    if method == "initialize":
        asked = params.get("protocolVersion")
        version = asked if asked in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0]
        return ok({"protocolVersion": version, "capabilities": {"tools": {"listChanged": False}},
                   "serverInfo": SERVER_INFO, "instructions": INSTRUCTIONS})
    if method == "ping":
        return ok({})
    if method == "tools/list":
        return ok({"tools": TOOLS})
    if method == "tools/call":
        name, args = params.get("name"), params.get("arguments") or {}
        if not isinstance(name, str) or name not in HANDLERS:
            return err(-32602, f"Outil inconnu : {name}", {"tools": sorted(HANDLERS)})
        if not isinstance(args, dict):
            return err(-32602, "« arguments » doit être un objet.")
        allowed = set(_BY_NAME[name]["inputSchema"]["properties"])
        extra = sorted(set(args) - allowed)
        if extra:
            return ok(_result({"error": f"paramètre(s) inconnu(s) {extra} ; attendus : {sorted(allowed)}"}, True))
        try:
            return ok(_result(HANDLERS[name](args)))
        except ToolFailure as exc:
            return ok(_result({"error": str(exc), **exc.data}, True))
        except Exception as exc:  # noqa: BLE001 — un outil ne doit jamais arrêter le serveur
            return ok(_result({"error": f"{type(exc).__name__} : {exc}"}, True))
    return err(-32601, f"Méthode inconnue : {method}")


def serve(stdin=None, stdout=None) -> None:
    """Boucle stdio : un message JSON par ligne (UTF-8), réponses sur stdout, journal sur stderr. Chaque
    tools/call s'exécute dans son fil ; les autres messages sont traités tout de suite."""
    inp = stdin or sys.stdin.buffer
    out = stdout or sys.stdout.buffer
    wlock = threading.Lock()
    workers = []

    def send(resp):
        if resp is not None:
            with wlock:
                out.write((json.dumps(resp, ensure_ascii=False) + "\n").encode("utf-8"))
                out.flush()

    def safe(msg):
        try:
            return handle(msg)
        except Exception as exc:  # noqa: BLE001 — le serveur ne s'arrête jamais sur un message
            mid = msg.get("id") if isinstance(msg, dict) else None
            return {"jsonrpc": "2.0", "id": mid if _valid_id(mid) else None,
                    "error": {"code": -32603, "message": f"Erreur interne : {type(exc).__name__} : {exc}"}}

    def work(msg):
        _CTX.rid = msg["id"]
        resp = safe(msg)
        with _LOCK:
            cancelled = msg["id"] in _CANCELLED
            _CANCELLED.discard(msg["id"])
            _PROCS.pop(msg["id"], None)
        if not cancelled:
            send(resp)

    for raw in inp:
        line = raw.decode("utf-8", errors="replace").strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            send({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "JSON illisible."}})
            continue
        if isinstance(msg, dict) and msg.get("jsonrpc") == "2.0" and msg.get("method") == "tools/call" \
                and _valid_id(msg.get("id")):
            th = threading.Thread(target=work, args=(msg,), daemon=True)
            th.start()
            workers.append(th)
        else:
            send(safe(msg))
    for th in workers:
        th.join()
