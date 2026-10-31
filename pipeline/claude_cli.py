"""Appels à Claude par la CLI « claude -p » (abonnement de l'utilisateur), jamais par l'API facturée.

Feuille de route (phase 6) : « Pour les appels Claude depuis le code : utiliser
claude -p --output-format json --json-schema <schéma> (abonnement), jamais le SDK API avec clé
facturée ». Garde-fous (CONTRACT §11.7) :
  - les variables qui feraient passer la CLI sur une facturation à l'usage (clé API, jeton, Bedrock,
    Vertex, Foundry) sont RETIRÉES de l'environnement de l'appel, ainsi que les variables qui relient
    une session Claude Code hôte (jeton de messagerie, identifiants de session) ;
  - réglages du projet et de l'utilisateur ignorés (--setting-sources ""), donc aucun apiKeyHelper ;
    serveurs MCP, skills et commandes désactivés ; outils désactivés sauf ceux demandés ; prompt
    système court ; dossier de travail temporaire ; session non conservée ;
  - avant l'appel : « claude auth status » doit indiquer une connexion (sinon message actionnable) ;
  - pendant l'appel : sortie « stream-json » ; le message d'initialisation doit déclarer
    apiKeySource = "none" (authentification par l'abonnement claude.ai) et apiProvider = "firstParty".
    Sinon le processus est TUÉ avant toute requête et l'appel refusé.
Sortie : l'objet validé par la CLI (champ structured_output du message « result »).
"""

from __future__ import annotations

import glob
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

from pipeline import config

# Variables qui feraient passer la CLI sur une facturation à l'usage : toujours retirées.
BILLED_ENV = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX",
              "CLAUDE_CODE_USE_FOUNDRY", "AWS_BEARER_TOKEN_BEDROCK", "ANTHROPIC_BEDROCK_BASE_URL",
              "ANTHROPIC_VERTEX_PROJECT_ID", "ANTHROPIC_FOUNDRY_API_KEY")
# Variables d'une session Claude Code hôte (mograph lancé depuis Claude Code) : jamais transmises.
HOST_SESSION_ENV = ("CLAUDECODE", "CLAUDE_CODE_SESSION_ID", "CLAUDE_CODE_HOST_SESSION_ID", "CLAUDE_CODE_MESSAGING_SOCKET",
                    "CLAUDE_CODE_MESSAGING_TOKEN", "CLAUDE_CODE_SDK_HAS_HOST_AUTH_REFRESH", "CLAUDE_CODE_CHILD_SESSION",
                    "CLAUDE_CODE_SESSION_ATTENDED", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_PID")
# Seule source d'authentification acceptée : l'abonnement (aucune clé API).
ACCEPTED_KEY_SOURCE = "none"
ACCEPTED_PROVIDER = "firstParty"


class ClaudeError(RuntimeError):
    """Appel impossible ou réponse inexploitable (message en français, actionnable)."""


def _version_key(path: str) -> tuple:
    parts = Path(path).parts
    ver = next((p for p in reversed(parts) if p[:1].isdigit() and "." in p), "0")
    return tuple(int(x) if x.isdigit() else 0 for x in ver.split("."))


def _child_env() -> dict:
    drop = set(BILLED_ENV) | set(HOST_SESSION_ENV)
    return {k: v for k, v in os.environ.items() if k not in drop}


def _argv0(exe: Path) -> list[str]:
    # Un script Python (faux « claude » des tests) se lance avec l'interpréteur courant.
    return [sys.executable, str(exe)] if Path(exe).suffix.lower() == ".py" else [str(exe)]


def _candidates() -> list[str]:
    out = []
    found = shutil.which("claude")
    if found:
        out.append(found)
    # CLI livrée avec l'application de bureau Claude : l'hôte publie son chemin quand mograph est lancé
    # depuis Claude Code ; sinon, emplacements connus (le plus récent d'abord).
    host = os.environ.get("CLAUDE_CODE_EXECPATH")
    if host:
        out.append(host)
    for base, pattern in ((os.environ.get("LOCALAPPDATA"), ("Claude-Data", "*", "claude-code", "*", "*", "claude.exe")),
                          (os.environ.get("APPDATA"), ("Claude", "claude-code", "*", "*", "claude.exe"))):
        if base:
            out += sorted(glob.glob(str(Path(base).joinpath(*pattern))), key=_version_key, reverse=True)
    seen, uniq = set(), []
    for c in out:
        k = os.path.normcase(os.path.abspath(c))
        if k not in seen:
            seen.add(k)
            uniq.append(c)
    return uniq


def find_claude() -> Path:
    """Exécutable « claude » : MOGRAPH_CLAUDE, sinon le premier candidat qui répond à --version (PATH,
    CLI de l'application de bureau)."""
    env = os.environ.get("MOGRAPH_CLAUDE")
    if env:
        p = Path(env)
        if p.is_file():
            return p.resolve()
        raise ClaudeError(f"MOGRAPH_CLAUDE désigne {p}, introuvable : corrigez la variable.")
    for c in _candidates():
        try:
            r = subprocess.run([*_argv0(Path(c)), "--version"], capture_output=True, text=True, timeout=30,
                               encoding="utf-8", errors="replace", stdin=subprocess.DEVNULL, env=_child_env())
        except (OSError, subprocess.SubprocessError):
            continue
        if r.returncode == 0 and "Claude Code" in r.stdout:
            return Path(c)
    raise ClaudeError("CLI « claude » introuvable ou inutilisable : installez Claude Code (ou l'application de bureau), "
                      "connectez-vous avec votre abonnement (« claude auth login »), ou indiquez l'exécutable dans "
                      "MOGRAPH_CLAUDE.")


def auth_status(exe: Path | None = None) -> dict:
    """« claude auth status » (lecture seule) ; lève ClaudeError si la CLI n'est pas connectée."""
    exe = exe or find_claude()
    try:
        r = subprocess.run([*_argv0(exe), "auth", "status"], capture_output=True, text=True, timeout=60,
                           encoding="utf-8", errors="replace", stdin=subprocess.DEVNULL, env=_child_env())
        st = json.loads(r.stdout)
    except (OSError, subprocess.SubprocessError, ValueError):
        raise ClaudeError(f"« {exe} auth status » illisible : mettez Claude Code à jour.") from None
    if not st.get("loggedIn"):
        raise ClaudeError("la CLI « claude » n'est pas connectée : lancez une fois « claude auth login » (compte "
                          "claude.ai de votre abonnement), puis relancez la commande. Aucun appel n'a été fait.")
    return st


def run(prompt: str, schema: dict, *, system: str, tools: list[str] | None = None, add_dirs: list[Path] | None = None,
        model: str | None = None, timeout: float | None = None, cwd: Path | None = None) -> tuple[dict, dict]:
    """Un appel « claude -p » à sortie structurée. Renvoie (objet validé, méta {model, duration_ms,
    cost_list_usd, api_key_source}).

    tools : outils intégrés autorisés (ex. ["Read"] pour lire une image) ; aucun par défaut.
    cost_list_usd est le coût « liste » indicatif affiché par la CLI ; sur un abonnement, rien n'est facturé
    à l'usage (consommation du quota de l'abonnement). Le prompt passe par l'entrée standard.
    """
    exe = find_claude()
    auth_status(exe)
    timeout = float(timeout or config.CLAUDE_TIMEOUT_S)
    model = model or config.CLAUDE_MODEL
    argv = [*_argv0(exe), "-p", "--output-format", "stream-json", "--verbose",
            "--json-schema", json.dumps(schema, ensure_ascii=False, separators=(",", ":")),
            "--no-session-persistence", "--setting-sources", "", "--strict-mcp-config", "--disable-slash-commands",
            "--system-prompt", system, "--tools", ",".join(tools) if tools else "",
            "--max-budget-usd", f"{config.CLAUDE_MAX_BUDGET_USD:g}"]
    if tools:
        argv += ["--allowedTools", ",".join(tools)]
    for d in add_dirs or []:
        argv += ["--add-dir", str(d)]
    if model:
        argv += ["--model", model]
    work = Path(cwd) if cwd else Path(tempfile.mkdtemp(prefix="mograph_claude_"))
    err_fh = tempfile.TemporaryFile()
    killed = {"why": None}
    try:
        try:
            proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=err_fh, cwd=work,
                                    env=_child_env())
        except OSError as exc:
            raise ClaudeError(f"Lancement de {exe} impossible ({exc}).") from None

        def _kill(why: str) -> None:
            if killed["why"] is None:
                killed["why"] = why
            try:
                proc.kill()
            except OSError:
                pass

        timer = threading.Timer(timeout, _kill, args=(f"Claude n'a pas répondu en {timeout:g} s : réessayez (ou "
                                                      "augmentez MOGRAPH_CLAUDE_TIMEOUT).",))
        timer.daemon = True
        timer.start()
        init, result = None, None
        try:
            try:
                proc.stdin.write(prompt.encode("utf-8"))
                proc.stdin.close()
            except OSError:
                pass  # la CLI s'est arrêtée tout de suite : le diagnostic vient de sa sortie
            for raw in proc.stdout:
                try:
                    msg = json.loads(raw.decode("utf-8", errors="replace"))
                except ValueError:
                    continue
                if not isinstance(msg, dict):
                    continue
                if msg.get("type") == "system" and msg.get("subtype") == "init" and init is None:
                    init = msg
                    src = msg.get("apiKeySource")
                    provider = msg.get("apiProvider", ACCEPTED_PROVIDER)
                    if src != ACCEPTED_KEY_SOURCE or provider != ACCEPTED_PROVIDER:
                        _kill(f"la CLI s'authentifierait par « {src} » ({provider}) et non par l'abonnement : appel "
                              "REFUSÉ et interrompu avant toute requête (facturation à l'usage interdite). Retirez la "
                              "clé API de la configuration de Claude Code (« claude auth status »), puis "
                              "« claude auth login » avec le compte claude.ai.")
                        break
                elif msg.get("type") == "result":
                    result = msg
            proc.wait()
        finally:
            timer.cancel()
            if proc.poll() is None:
                proc.kill()
                proc.wait()
        if killed["why"]:
            raise ClaudeError(killed["why"])
        if init is None and result is not None:
            raise ClaudeError("la CLI n'a pas déclaré sa source d'authentification (message d'initialisation absent) : "
                              "appel non vérifiable, mettez Claude Code à jour.")
        if result is None:
            err_fh.seek(0)
            tail = err_fh.read().decode("utf-8", errors="replace").strip()[-400:]
            raise ClaudeError(f"Réponse de « claude -p » illisible (code {proc.returncode}) : {tail or 'sortie vide'}. "
                              "Vérifiez la connexion à votre abonnement (« claude auth status »).")
    finally:
        err_fh.close()
        if not cwd:
            shutil.rmtree(work, ignore_errors=True)
    if result.get("is_error") or result.get("subtype") != "success" or not isinstance(result.get("structured_output"), dict):
        raise ClaudeError(f"Claude n'a pas produit de sortie structurée valide ({result.get('subtype')}, "
                          f"{result.get('terminal_reason')}) : {str(result.get('result') or '')[:300]}")
    models = list((result.get("modelUsage") or {}).keys())
    return result["structured_output"], {"model": models[0] if models else (init.get("model") or model or "inconnu"),
                                         "duration_ms": result.get("duration_ms"),
                                         "cost_list_usd": result.get("total_cost_usd"),
                                         "api_key_source": init.get("apiKeySource")}
