"""Appels à Claude par la CLI « claude -p » (abonnement de l'utilisateur), jamais par l'API facturée.

Feuille de route (phase 6) : « Pour les appels Claude depuis le code : utiliser
claude -p --output-format json --json-schema <schéma> (abonnement), jamais le SDK API avec clé
facturée ». Garde-fous (CONTRACT §11.7) :
  - exécutable NATIF seulement : un lanceur .cmd/.bat (npm) passerait par cmd.exe, qui altère les
    arguments et survit à l'arrêt ; le binaire qu'il lance est retrouvé, sinon le lanceur est refusé ;
  - environnement de l'appel : TOUTES les variables ANTHROPIC_* et CLAUDE_CODE_USE_* (clé API, jeton,
    passerelle, Bedrock, Vertex, Foundry, AWS, Google Cloud…) retirées, ainsi que les variables qui
    relient une session Claude Code hôte (jeton de messagerie, identifiants de session) ;
  - AVANT tout lancement de « claude -p » : « claude auth status » (même environnement) doit indiquer
    loggedIn, authMethod « claude.ai » ou « oauth_token » (abonnement), apiProvider « firstParty » et
    aucune apiKeySource ; sinon REFUS sans appel (barrière principale) ;
  - réglages du projet et de l'utilisateur ignorés (--setting-sources ""), serveurs MCP, skills et
    commandes désactivés, outils désactivés sauf ceux demandés, prompt système court, dossier de
    travail temporaire, session non conservée, plafond --max-budget-usd ;
  - pendant l'appel (seconde barrière) : sortie « stream-json » ; le message d'initialisation doit
    déclarer apiKeySource = "none", sinon l'ARBRE de processus est tué aussitôt et l'appel refusé ;
    délai dépassé -> arbre tué.
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

# Préfixes de variables qui changent l'authentification ou le fournisseur (facturation à l'usage
# possible) : toujours retirées de l'environnement de l'appel.
BILLED_ENV_PREFIXES = ("ANTHROPIC_", "CLAUDE_CODE_USE_")
BILLED_ENV = ("AWS_BEARER_TOKEN_BEDROCK",)
# Variables d'une session Claude Code hôte (mograph lancé depuis Claude Code) : jamais transmises.
HOST_SESSION_ENV = ("CLAUDECODE", "CLAUDE_CODE_SESSION_ID", "CLAUDE_CODE_HOST_SESSION_ID", "CLAUDE_CODE_MESSAGING_SOCKET",
                    "CLAUDE_CODE_MESSAGING_TOKEN", "CLAUDE_CODE_SDK_HAS_HOST_AUTH_REFRESH", "CLAUDE_CODE_CHILD_SESSION",
                    "CLAUDE_CODE_SESSION_ATTENDED", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_PID")
# Authentifications acceptées (« claude auth status ») : abonnement claude.ai seulement.
ACCEPTED_AUTH_METHODS = ("claude.ai", "oauth_token")
ACCEPTED_PROVIDER = "firstParty"
ACCEPTED_KEY_SOURCE = "none"
SHELL_LAUNCHERS = (".cmd", ".bat", ".ps1")


class ClaudeError(RuntimeError):
    """Appel impossible ou réponse inexploitable (message en français, actionnable)."""


def _version_key(path: str) -> tuple:
    parts = Path(path).parts
    ver = next((p for p in reversed(parts) if p[:1].isdigit() and "." in p), "0")
    return tuple(int(x) if x.isdigit() else 0 for x in ver.split("."))


def _child_env() -> dict:
    drop = set(BILLED_ENV) | set(HOST_SESSION_ENV)
    return {k: v for k, v in os.environ.items() if k not in drop and not k.upper().startswith(BILLED_ENV_PREFIXES)}


def _argv0(exe: Path) -> list[str]:
    # Un script Python (faux « claude » des tests) se lance avec l'interpréteur courant.
    return [sys.executable, str(exe)] if Path(exe).suffix.lower() == ".py" else [str(exe)]


def _native(path: str | os.PathLike) -> Path | None:
    """Exécutable lançable sans interpréteur de commandes : un lanceur npm « claude.cmd » est remplacé par
    le binaire qu'il appelle (node_modules/@anthropic-ai/claude-code/bin/claude.exe) ; None si introuvable."""
    p = Path(path)
    if p.suffix.lower() not in SHELL_LAUNCHERS:
        return p
    exe = p.parent / "node_modules" / "@anthropic-ai" / "claude-code" / "bin" / "claude.exe"
    return exe if exe.is_file() else None


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
        n = _native(c)
        if n is None:
            continue
        k = os.path.normcase(os.path.abspath(n))
        if k not in seen:
            seen.add(k)
            uniq.append(str(n))
    return uniq


def kill_tree(proc: subprocess.Popen) -> None:
    """Arrête le processus ET ses descendants (Windows : taskkill /T ; ailleurs : groupe de processus)."""
    if proc.poll() is not None:
        return
    try:
        if config.IS_WINDOWS:
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True, timeout=30,
                           stdin=subprocess.DEVNULL)
        else:
            import signal

            os.killpg(proc.pid, signal.SIGKILL)
    except (OSError, subprocess.SubprocessError):
        pass
    try:
        proc.kill()
    except OSError:
        pass


def _run_small(argv: list[str], timeout: float) -> subprocess.CompletedProcess:
    """Commande courte (--version, auth status) : arbre tué au délai, sortie bornée."""
    proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            env=_child_env(), start_new_session=not config.IS_WINDOWS)
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        kill_tree(proc)
        raise
    return subprocess.CompletedProcess(argv, proc.returncode, out.decode("utf-8", "replace"),
                                       err.decode("utf-8", "replace"))


def find_claude() -> Path:
    """Exécutable « claude » : MOGRAPH_CLAUDE, sinon le premier candidat natif qui répond à --version (PATH,
    lanceur npm résolu, CLI de l'application de bureau)."""
    env = os.environ.get("MOGRAPH_CLAUDE")
    if env:
        p = Path(env)
        if not p.is_file():
            raise ClaudeError(f"MOGRAPH_CLAUDE désigne {p}, introuvable : corrigez la variable.")
        n = _native(p)
        if n is None:
            raise ClaudeError(f"MOGRAPH_CLAUDE désigne un lanceur {p.suffix} ({p}) : indiquez le binaire claude.exe "
                              "lui-même (un lanceur passe par cmd.exe, qui altère les arguments et survit à l'arrêt).")
        return n.resolve()
    for c in _candidates():
        try:
            r = _run_small([*_argv0(Path(c)), "--version"], timeout=30)
        except (OSError, subprocess.SubprocessError):
            continue
        if r.returncode == 0 and "Claude Code" in r.stdout:
            return Path(c)
    raise ClaudeError("CLI « claude » introuvable ou inutilisable : installez Claude Code (ou l'application de bureau), "
                      "connectez-vous avec votre abonnement (« claude auth login »), ou indiquez l'exécutable dans "
                      "MOGRAPH_CLAUDE.")


def auth_status(exe: Path | None = None) -> dict:
    """« claude auth status » (lecture seule, environnement de l'appel) ; lève ClaudeError si la CLI n'est pas
    connectée par l'abonnement claude.ai (clé API, assistant de clé, fournisseur tiers ou cloud : REFUS)."""
    exe = exe or find_claude()
    try:
        r = _run_small([*_argv0(exe), "auth", "status"], timeout=60)
        st = json.loads(r.stdout)
        if not isinstance(st, dict):
            raise ValueError
    except (OSError, subprocess.SubprocessError, ValueError):
        raise ClaudeError(f"« {exe} auth status » illisible : mettez Claude Code à jour. Aucun appel n'a été fait.") from None
    if not st.get("loggedIn"):
        raise ClaudeError("la CLI « claude » n'est pas connectée : lancez une fois « claude auth login » (compte "
                          "claude.ai de votre abonnement), puis relancez la commande. Aucun appel n'a été fait.")
    method, provider, key = st.get("authMethod"), st.get("apiProvider"), st.get("apiKeySource")
    if method not in ACCEPTED_AUTH_METHODS or provider != ACCEPTED_PROVIDER or key:
        raise ClaudeError(f"la CLI « claude » est authentifiée par « {method} » (fournisseur {provider}"
                          + (f", clé {key}" if key else "") + ") et non par l'abonnement claude.ai : appel REFUSÉ, aucun "
                          "appel n'a été fait (facturation à l'usage interdite). Retirez la clé API ou l'assistant de "
                          "clé de la configuration de Claude Code, puis « claude auth login » avec le compte claude.ai.")
    return st


def run(prompt: str, schema: dict, *, system: str, tools: list[str] | None = None, add_dirs: list[Path] | None = None,
        model: str | None = None, timeout: float | None = None, cwd: Path | None = None) -> tuple[dict, dict]:
    """Un appel « claude -p » à sortie structurée. Renvoie (objet validé, méta {model, duration_ms,
    cost_list_usd, api_key_source, auth_method}).

    tools : outils intégrés autorisés (ex. ["Read"] pour lire une image) ; aucun par défaut.
    cost_list_usd est le coût « liste » indicatif affiché par la CLI ; sur un abonnement, rien n'est facturé
    à l'usage (consommation du quota de l'abonnement). Le prompt passe par l'entrée standard.
    """
    exe = find_claude()
    status = auth_status(exe)
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
                                    env=_child_env(), start_new_session=not config.IS_WINDOWS)
        except OSError as exc:
            raise ClaudeError(f"Lancement de {exe} impossible ({exc}).") from None

        def _kill(why: str) -> None:
            if killed["why"] is None:
                killed["why"] = why
            kill_tree(proc)

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
                    if src != ACCEPTED_KEY_SOURCE:
                        _kill(f"la CLI s'authentifie par « {src} » et non par l'abonnement : appel REFUSÉ et "
                              "interrompu (facturation à l'usage interdite). Retirez la clé API de la configuration de "
                              "Claude Code (« claude auth status »), puis « claude auth login » avec le compte claude.ai.")
                        break
                elif msg.get("type") == "result":
                    result = msg
            proc.wait()
        finally:
            timer.cancel()
            kill_tree(proc)
            if proc.poll() is None:
                proc.wait()
        if killed["why"]:
            raise ClaudeError(killed["why"])
        if init is None and result is not None:
            raise ClaudeError("la CLI n'a pas déclaré sa source d'authentification (message d'initialisation absent) : "
                              "résultat écarté, mettez Claude Code à jour.")
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
                                         "api_key_source": init.get("apiKeySource"),
                                         "auth_method": status.get("authMethod")}
