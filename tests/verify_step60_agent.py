"""Vérification de la phase 6 : couche agent (serveur MCP, skill, mograph brief, relecture visuelle).

Lancement depuis la racine :  .venv/Scripts/python.exe tests/verify_step60_agent.py
Aucun appel réel à Claude : un FAUX « claude » (script Python désigné par MOGRAPH_CLAUDE) rejoue des
réponses scriptées et journalise chaque appel (arguments, prompt, variables d'environnement vues).
Isolé : build/_tests/step60/ (build, out, scènes : MOGRAPH_SCENES_DIR).
Contrôles :
  A. claude_cli : arguments (stream-json, schéma, réglages/MCP/skills ignorés, aucun outil par défaut,
     plafond --max-budget-usd, prompt par l'entrée standard) ; variables ANTHROPIC_* / CLAUDE_CODE_USE_* et
     de session hôte RETIRÉES ; « auth status » : non connectée, clé API, assistant de clé, fournisseur
     cloud, apiKeySource -> refus SANS appel ; apiKeySource ≠ "none" à l'initialisation -> ARBRE tué (y
     compris un petit-enfant qui tient la sortie) ; erreur de la CLI ; délai dépassé (petit-enfant
     compris) ; lanceur .cmd refusé ou résolu vers claude.exe ; MOGRAPH_CLAUDE introuvable ;
  B. brief : valide du premier coup (1 appel, scène écrite et validée, déclaration d'IA) ; refus puis
     correction (2 appels, messages du compilateur renvoyés) ; toujours refusée -> EXACTEMENT 3 appels,
     rien d'écrit, proposition gardée ; sortie hors schéma comptée ; --max-iterations borné ; scène
     existante refusée AVANT tout appel ; --force sans --id refusé ; texte absent, couleur du fond, durée
     nulle, animation hors champ -> renvoyés à Claude ; couleurs par défaut contrastées ; schéma simplifié
     = mouvements et couleurs du preset ; conversion déterministe ; --json = un seul objet ;
  C. serveur MCP (stdio) : initialize (négociation de version), notification sans réponse, tools/list
     (7 outils, confirm exigé), validate (valide / refusée -> isError + messages), confirmations
     absentes -> isError sans rien lancer, erreurs de protocole (-32700, -32601, -32602), paramètre
     inconnu, params/nom non conformes (-32602, serveur vivant), id null et lot (-32600), version
     2025-03-26 non proposée, draft sur scène refusée (rien lancé), ping, status, --json toujours un objet
     (status/sheets/beats en échec), id de scène hors motif refusé ; stdout = uniquement du JSON-RPC ;
  D. préversion par MCP (draft confirm: true) jusqu'au bout, commande de suivi exacte, second lancement
     refusé pendant le rendu, verrou d'un rendu au premier plan, PID réattribué non pris pour un rendu,
     annulation d'un outil long (ping servi, sous-processus tué, aucune réponse),
     puis sheets draft + vision : relecture écrite (vision_qc.json/.md), jamais bloquante, y compris CLI
     non connectée ; QC et porte de déterminisme ne lisent pas la relecture ;
  E. skill et configuration : SKILL.md (frontmatter, dix règles), .mcp.json ; aucun SDK/clé API Anthropic
     dans le code.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TEST = ROOT / "build" / "_tests" / "step60"
SCENES = TEST / "scenes"
FAKE = TEST / "fake_claude.py"
FAKE_LOG = TEST / "fake_calls.jsonl"
FAKE_SCRIPT = TEST / "fake_script.json"
FAKE_MARK = TEST / "fake_request_made"
os.environ.update({
    "MOGRAPH_BUILD_DIR": str(TEST / "build"), "MOGRAPH_OUT_DIR": str(TEST / "out"),
    "MOGRAPH_SCENES_DIR": str(SCENES), "MOGRAPH_CLAUDE": str(FAKE), "PYTHONIOENCODING": "utf-8",
    "FAKE_LOG": str(FAKE_LOG), "FAKE_SCRIPT": str(FAKE_SCRIPT), "FAKE_MARK": str(FAKE_MARK)})
for k in ("FAKE_LOGGED_IN", "FAKE_KEY_SOURCE", "MOGRAPH_CLAUDE_MODEL", "MOGRAPH_CLAUDE_TIMEOUT",
          "MOGRAPH_CLAUDE_MAX_BUDGET_USD"):
    os.environ.pop(k, None)
sys.path.insert(0, str(ROOT))

from pipeline import brief, claude_cli, config  # noqa: E402  (après l'environnement)

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

PY = sys.executable
FAILS: list[str] = []

FAKE_SRC = r'''
import json, os, sys, time
argv = sys.argv[1:]
if argv[:1] == ["--version"]:
    print("9.9.9 (Claude Code)"); sys.exit(0)
if argv[:2] == ["auth", "status"]:
    st = {"loggedIn": os.environ.get("FAKE_LOGGED_IN", "1") == "1",
          "authMethod": os.environ.get("FAKE_AUTH_METHOD", "claude.ai"),
          "apiProvider": os.environ.get("FAKE_PROVIDER", "firstParty")}
    if os.environ.get("FAKE_STATUS_KEY"):
        st["apiKeySource"] = os.environ["FAKE_STATUS_KEY"]
    print(json.dumps(st)); sys.exit(0)
prompt = sys.stdin.buffer.read().decode("utf-8")
log = os.environ["FAKE_LOG"]
with open(log, "a", encoding="utf-8") as fh:
    fh.write(json.dumps({"argv": argv, "prompt": prompt, "cwd": os.getcwd(),
                         "seen_env": sorted(k for k in os.environ if k.startswith(("ANTHROPIC_", "CLAUDE_CODE_USE_"))
                                            or k in ("CLAUDE_CODE_MESSAGING_TOKEN", "CLAUDECODE"))}) + "\n")
if os.environ.get("FAKE_GRANDCHILD"):
    import subprocess
    # Petit-enfant qui hérite de la sortie standard et écrit la « requête » 4 s plus tard.
    subprocess.Popen([sys.executable, "-c", "import os,time; time.sleep(4); open(os.environ['FAKE_MARK'] + '.gc', 'w').close()"])
src = os.environ.get("FAKE_KEY_SOURCE", "none")
print(json.dumps({"type": "system", "subtype": "init", "apiKeySource": src, "apiProvider": "firstParty",
                  "model": "fake-model", "tools": []}), flush=True)
if src != "none":
    time.sleep(4)
    open(os.environ["FAKE_MARK"], "w").close()
with open(log, encoding="utf-8") as fh:
    n = sum(1 for _ in fh)
script = json.load(open(os.environ["FAKE_SCRIPT"], encoding="utf-8"))
resp = script[min(n, len(script)) - 1]
if "hang" in resp:
    time.sleep(resp["hang"])
if "error" in resp:
    print(json.dumps({"type": "result", "subtype": "success", "is_error": True, "result": resp["error"],
                      "terminal_reason": "api_error"}), flush=True)
    sys.exit(1)
print(json.dumps({"type": "assistant", "message": {"content": []}}), flush=True)
print(json.dumps({"type": "result", "subtype": "success", "is_error": False, "structured_output": resp["out"],
                  "modelUsage": {"fake-model": {}}, "duration_ms": 3, "total_cost_usd": 0.01}), flush=True)
'''


def check(label: str, ok: bool, detail: str = "") -> bool:
    print(("PASS " if ok else "FAIL ") + label + (f" -- {detail}" if detail else ""), flush=True)
    if not ok:
        FAILS.append(label)
    return ok


def mograph(*args: str, env: dict | None = None, timeout: float = 600) -> subprocess.CompletedProcess:
    return subprocess.run([PY, str(ROOT / "mograph.py"), *args], cwd=ROOT, env=dict(os.environ, **(env or {})),
                          capture_output=True, stdin=subprocess.DEVNULL, text=True, encoding="utf-8",
                          errors="replace", timeout=timeout)


def tail(cp, n=2) -> str:
    return " | ".join((cp.stdout + cp.stderr).strip().splitlines()[-n:])


def script(*responses) -> None:
    FAKE_SCRIPT.write_text(json.dumps(list(responses), ensure_ascii=False), encoding="utf-8")
    FAKE_LOG.write_text("", encoding="utf-8")
    if FAKE_MARK.exists():
        FAKE_MARK.unlink()


def calls() -> list[dict]:
    return [json.loads(x) for x in FAKE_LOG.read_text(encoding="utf-8").splitlines() if x.strip()]


def good_brief(sid: str = "essai_agent", move: str = "chars_up") -> dict:
    return {"id": sid, "title": "Le café ouvre", "brief": "Annonce d'ouverture d'un café de quartier", "shots": [
        {"id": "titre", "intent": "Annoncer l'ouverture", "duration": 2.4, "background": "bg", "layers": [
            {"id": "titre", "type": "text", "text": "Le café ouvre", "style": "display", "size": 120, "color": "fg",
             "x": "safe:0%", "y": "safe:40%", "anchor": "left", "move": move, "at": 0.2},
            {"id": "trait", "type": "shape", "shape": "line", "w": 360, "color": "primary", "x": "safe:0%",
             "y": "safe:58%", "anchor": "left", "move": "draw", "at": 0.8}]},
        {"id": "date", "intent": "Donner la date", "duration": 2.0, "background": "cream", "transition": "wipe",
         "layers": [
             {"id": "jour", "type": "text", "text": "Lundi 9 h", "style": "body", "size": 64, "color": "fg",
              "x": "safe:50%", "y": "safe:50%", "move": "rise", "at": 0.1},
             {"id": "rond", "type": "shape", "shape": "ellipse", "w": 300, "h": 300, "color": "secondary",
              "x": "80%", "y": "50%", "move": "pop", "at": 0.3}]}]}


# ---------------------------------------------------------------------------------------------
def test_cli() -> None:
    schema = {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"]}
    check("find_claude : MOGRAPH_CLAUDE désigne le faux exécutable", claude_cli.find_claude() == FAKE.resolve())

    script({"out": {"ok": True}})
    saved = {k: os.environ.get(k) for k in ("ANTHROPIC_API_KEY", "CLAUDE_CODE_MESSAGING_TOKEN", "CLAUDECODE",
                                            "CLAUDE_CODE_USE_BEDROCK")}
    os.environ.update({"ANTHROPIC_API_KEY": "valeur-factice-de-test", "CLAUDE_CODE_MESSAGING_TOKEN": "x",
                       "CLAUDECODE": "1", "CLAUDE_CODE_USE_BEDROCK": "1"})
    try:
        obj, meta = claude_cli.run("Bonjour é→", schema, system="sys court")
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    c = calls()
    check("run : objet structuré et méta (modèle, source d'authentification « none »)",
          obj == {"ok": True} and meta["model"] == "fake-model" and meta["api_key_source"] == "none", str(meta))
    a = c[0]["argv"] if c else []
    check("run : un seul appel, prompt transmis par l'entrée standard (UTF-8)", len(c) == 1 and c[0]["prompt"] == "Bonjour é→"
          and "Bonjour é→" not in a)
    check("run : variables facturables et de session hôte RETIRÉES (clé API, Bedrock, jeton de messagerie)",
          c and c[0]["seen_env"] == [], str(c[0]["seen_env"] if c else None))

    def after(flag):
        return a[a.index(flag) + 1] if flag in a else None
    check("run : -p, stream-json, schéma JSON, session non conservée",
          "-p" in a and after("--output-format") == "stream-json" and "--verbose" in a
          and json.loads(after("--json-schema") or "null") == schema and "--no-session-persistence" in a)
    check("run : réglages ignorés, MCP et skills désactivés, aucun outil, plafond de coût liste",
          after("--setting-sources") == "" and "--strict-mcp-config" in a and "--disable-slash-commands" in a
          and after("--tools") == "" and "--allowedTools" not in a and float(after("--max-budget-usd") or 0) > 0
          and after("--system-prompt") == "sys court" and after("--model") == config.CLAUDE_MODEL and "--bare" not in a)
    check("run : dossier de travail temporaire (pas la racine du projet), supprimé ensuite",
          c and Path(c[0]["cwd"]).resolve() != ROOT and not Path(c[0]["cwd"]).exists())

    script({"out": {"ok": True}})
    os.environ["FAKE_LOGGED_IN"] = "0"
    try:
        claude_cli.run("x", schema, system="s")
        refused = False
    except claude_cli.ClaudeError as exc:
        refused = "claude auth login" in str(exc)
    finally:
        os.environ.pop("FAKE_LOGGED_IN")
    check("CLI non connectée : refus actionnable (claude auth login), AUCUN appel -p", refused and calls() == [])

    script({"out": {"ok": True}})
    os.environ["FAKE_KEY_SOURCE"] = "ANTHROPIC_API_KEY"
    t0 = time.perf_counter()
    try:
        claude_cli.run("x", schema, system="s")
        msg = ""
    except claude_cli.ClaudeError as exc:
        msg = str(exc)
    finally:
        os.environ.pop("FAKE_KEY_SOURCE")
    dt = time.perf_counter() - t0
    time.sleep(4.5)
    check("apiKeySource ≠ none (clé API) : appel REFUSÉ, processus tué avant la requête",
          "REFUSÉ" in msg and dt < 3.5 and not FAKE_MARK.exists(), f"{dt:.1f} s ; {msg[:80]}")

    script({"error": "quota épuisé"})
    try:
        claude_cli.run("x", schema, system="s")
        msg = ""
    except claude_cli.ClaudeError as exc:
        msg = str(exc)
    check("erreur de la CLI : ClaudeError avec le message", "quota épuisé" in msg, msg[:100])

    script({"hang": 30, "out": {"ok": True}})
    t0 = time.perf_counter()
    try:
        claude_cli.run("x", schema, system="s", timeout=2)
        msg = ""
    except claude_cli.ClaudeError as exc:
        msg = str(exc)
    check("délai dépassé : processus tué, message actionnable", "n'a pas répondu en 2 s" in msg
          and time.perf_counter() - t0 < 10, msg[:80])

    for label, env in (("clé API (authMethod api_key)", {"FAKE_AUTH_METHOD": "api_key"}),
                       ("assistant de clé (api_key_helper)", {"FAKE_AUTH_METHOD": "api_key_helper"}),
                       ("fournisseur cloud (bedrock)", {"FAKE_PROVIDER": "bedrock"}),
                       ("apiKeySource déclarée par auth status", {"FAKE_STATUS_KEY": "/login managed key"})):
        script({"out": {"ok": True}})
        os.environ.update(env)
        try:
            claude_cli.run("x", schema, system="s")
            msg = ""
        except claude_cli.ClaudeError as exc:
            msg = str(exc)
        finally:
            for k in env:
                os.environ.pop(k)
        check(f"auth status : {label} -> REFUS sans aucun appel -p", "REFUSÉ" in msg and calls() == [], msg[:90])

    script({"out": {"ok": True}})
    os.environ.update({"ANTHROPIC_BASE_URL": "https://passerelle.invalid", "CLAUDE_CODE_USE_MANTLE": "1",
                       "CLAUDE_CODE_USE_ANTHROPIC_AWS": "1"})
    try:
        claude_cli.run("x", schema, system="s")
    finally:
        for k in ("ANTHROPIC_BASE_URL", "CLAUDE_CODE_USE_MANTLE", "CLAUDE_CODE_USE_ANTHROPIC_AWS"):
            os.environ.pop(k)
    check("variables ANTHROPIC_* et CLAUDE_CODE_USE_* retirées par préfixe (passerelle, Mantle, AWS)",
          calls() and calls()[0]["seen_env"] == [], str(calls()[0]["seen_env"] if calls() else None))

    gc = Path(str(FAKE_MARK) + ".gc")
    for case, env, kw in (("apiKeySource ≠ none", {"FAKE_KEY_SOURCE": "ANTHROPIC_API_KEY"}, {}),
                          ("délai dépassé", {}, {"timeout": 2})):
        script({"hang": 30, "out": {"ok": True}} if case == "délai dépassé" else {"out": {"ok": True}})
        if gc.exists():
            gc.unlink()
        os.environ.update(dict(env, FAKE_GRANDCHILD="1"))
        t0 = time.perf_counter()
        try:
            claude_cli.run("x", schema, system="s", **kw)
            msg = ""
        except claude_cli.ClaudeError as exc:
            msg = str(exc)
        finally:
            for k in list(env) + ["FAKE_GRANDCHILD"]:
                os.environ.pop(k)
        dt = time.perf_counter() - t0
        time.sleep(5)
        check(f"{case} avec un petit-enfant qui tient la sortie : arbre tué, retour borné, aucune « requête »",
              msg and dt < 3.9 and not gc.exists(), f"{dt:.1f} s ; {msg[:60]}")

    cmd = TEST / "lanceur" / "claude.cmd"
    cmd.parent.mkdir(parents=True, exist_ok=True)
    cmd.write_text("@echo off\r\n", encoding="utf-8")
    os.environ["MOGRAPH_CLAUDE"], old = str(cmd), os.environ["MOGRAPH_CLAUDE"]
    try:
        claude_cli.find_claude()
        msg = ""
    except claude_cli.ClaudeError as exc:
        msg = str(exc)
    finally:
        os.environ["MOGRAPH_CLAUDE"] = old
    native = cmd.parent / "node_modules" / "@anthropic-ai" / "claude-code" / "bin" / "claude.exe"
    native.parent.mkdir(parents=True, exist_ok=True)
    native.write_bytes(b"")
    check("lanceur .cmd : jamais exécuté (MOGRAPH_CLAUDE refusé, sinon résolu vers le claude.exe qu'il appelle)",
          "lanceur" in msg and claude_cli._native(cmd) == native and claude_cli._native(TEST / "x.bat") is None, msg[:80])

    old = os.environ["MOGRAPH_CLAUDE"]
    os.environ["MOGRAPH_CLAUDE"] = str(TEST / "absent.exe")
    try:
        claude_cli.find_claude()
        ok = False
    except claude_cli.ClaudeError as exc:
        ok = "MOGRAPH_CLAUDE" in str(exc)
    finally:
        os.environ["MOGRAPH_CLAUDE"] = old
    check("MOGRAPH_CLAUDE introuvable : refus explicite", ok)


# ---------------------------------------------------------------------------------------------
def test_brief() -> Path:
    for p in brief.PRESETS_2D:
        preset = json.loads((config.PRESETS_DIR / f"{p}.json").read_text(encoding="utf-8"))
        s = brief.brief_schema(p)
        layer = s["properties"]["shots"]["items"]["properties"]["layers"]["items"]["properties"]
        if not check(f"schéma simplifié {p} : mouvements et couleurs du preset, rien d'autre",
                     layer["move"]["enum"] == sorted(preset["motion"]["moves"])
                     and layer["color"]["enum"] == sorted(preset["palette"])
                     and s["properties"]["shots"]["maxItems"] <= 6):
            break
    try:
        brief.brief_schema("clay_3d")
        ok = False
    except brief.BriefError:
        ok = True
    check("brief : preset 3D refusé (2D seulement)", ok)
    a = brief.to_scene(good_brief(), preset_id="flat_riso", aspect="16:9", model="m", date="2026-10-31")
    b = brief.to_scene(good_brief(), preset_id="flat_riso", aspect="16:9", model="m", date="2026-10-31")
    check("conversion brief -> scène déterministe (même entrée, même JSON)", json.dumps(a) == json.dumps(b))

    # B1. valide du premier coup
    script({"out": good_brief()})
    cp = mograph("brief", "Annonce de l'ouverture d'un café", "--json")
    target = SCENES / "essai_agent.json"
    try:
        out = json.loads(cp.stdout)
    except ValueError:
        out = {}
    check("brief valide du premier coup : 1 appel, --json = un seul objet, scène écrite",
          cp.returncode == 0 and len(calls()) == 1 and out.get("iterations") == 1 and target.is_file(), tail(cp, 3))
    v = mograph("validate", str(target))
    check("la scène produite passe « validate »", v.returncode == 0, tail(v))
    sc = json.loads(target.read_text(encoding="utf-8")) if target.is_file() else {}
    dis = (sc.get("provenance") or {}).get("ai_disclosure") or {}
    check("scène produite : déclaration d'IA (rôle scene, outil claude -p, modèle), durées en images entières",
          dis.get("ai_used") is True and "scene" in dis.get("roles", []) and dis["tools"][0]["model"] == "fake-model"
          and all(abs(s["duration"] * 25 - round(s["duration"] * 25)) < 1e-9 for s in sc.get("shots", [])))
    rec = TEST / "build" / "_brief" / "essai_agent.brief.json"
    r = json.loads(rec.read_text(encoding="utf-8")) if rec.is_file() else {}
    check("bilan non versionné build/_brief/<id>.brief.json (idée, essais, modèle)",
          r.get("iterations") == 1 and r.get("idea", "").startswith("Annonce") and r.get("model") == "fake-model")
    a1 = calls()[0]["argv"] if calls() else []
    sysp = a1[a1.index("--system-prompt") + 1] if "--system-prompt" in a1 else ""
    check("brief : prompt système court, sans outil (aucune lecture de fichier par Claude)",
          len(sysp) < 2000 and a1[a1.index("--tools") + 1] == "", f"{len(sysp)} caractères")

    # B2. refus puis correction
    script({"out": good_brief("essai_corrige", move="teleportation")}, {"out": good_brief("essai_corrige")})
    cp = mograph("brief", "idée", "--id", "essai_corrige")
    c = calls()
    check("brief refusé puis corrigé : 2 appels, scène écrite",
          cp.returncode == 0 and len(c) == 2 and (SCENES / "essai_corrige.json").is_file(), tail(cp, 3))
    check("2e appel : proposition refusée + messages du compilateur renvoyés à Claude",
          len(c) == 2 and "REFUSÉE" in c[1]["prompt"] and "teleportation" in c[1]["prompt"]
          and "Idée d'origine : idée" in c[1]["prompt"])

    # B3. toujours refusée : borne de 3 appels
    bad = {"out": good_brief("essai_refus", move="teleportation")}
    script(bad, bad, bad, bad, bad)
    cp = mograph("brief", "idée", "--id", "essai_refus")
    check("toujours refusée : EXACTEMENT 3 appels, code 1, aucune scène écrite",
          cp.returncode == 1 and len(calls()) == 3 and not (SCENES / "essai_refus.json").exists(), tail(cp, 2))
    check("proposition refusée conservée (build/_brief/<id>_refusee.json, avec les erreurs)",
          (TEST / "build" / "_brief" / "essai_refus_refusee.json").is_file())
    script(bad, bad)
    cp = mograph("brief", "idée", "--id", "essai_refus", "--max-iterations", "1")
    check("--max-iterations 1 : 1 seul appel", cp.returncode == 1 and len(calls()) == 1)
    cp = mograph("brief", "idée", "--max-iterations", "4")
    check("--max-iterations 4 refusé par la CLI (plafond 3)", cp.returncode == 2)
    try:
        brief.generate("idée", max_iterations=5, runner=lambda *a, **k: ({}, {}))
        ok = False
    except brief.BriefError:
        ok = True
    check("generate(max_iterations=5) refusé (plafond non contournable)", ok)

    # B4. sortie hors schéma : comptée, puis corrigée
    broken = good_brief("essai_casse")
    del broken["shots"]
    script({"out": broken}, {"out": good_brief("essai_casse")})
    cp = mograph("brief", "idée", "--id", "essai_casse")
    c = calls()
    check("sortie hors schéma : essai compté, message renvoyé, corrigée au 2e",
          cp.returncode == 0 and len(c) == 2 and "non conforme au schéma" in c[1]["prompt"], tail(cp, 2))

    # B5. scène existante : refus AVANT tout appel ; --force la remplace
    script({"out": good_brief()})
    cp = mograph("brief", "idée", "--id", "essai_agent")
    check("scène existante sans --force : refus avant tout appel (0 appel)", cp.returncode == 1 and calls() == [],
          tail(cp, 1))
    cp = mograph("brief", "idée", "--id", "essai_agent", "--force")
    check("--force : scène remplacée (1 appel)", cp.returncode == 0 and len(calls()) == 1, tail(cp, 1))
    cp = mograph("brief", "idée", "--id", "Mauvais Id")
    check("--id invalide refusé avant tout appel", cp.returncode == 1 and len(calls()) == 1)

    # B5b. --force sans --id
    script({"out": good_brief("essai_force")})
    cp = mograph("brief", "idée", "--force")
    check("--force sans --id refusé avant tout appel", cp.returncode == 1 and calls() == [], tail(cp, 1))

    # B5c. défauts acceptés par le compilateur mais refusés par brief, puis corrigés
    for label, mutate, needle in (
            ("texte absent (l'id serait affiché)", lambda b: b["shots"][0]["layers"][0].pop("text"), "« text » manquant"),
            ("couleur du calque = fond du plan", lambda b: b["shots"][0]["layers"][0].update(color="bg"), "identique au fond"),
            ("durée de plan nulle", lambda b: b["shots"][1].update(duration=0), "trop court"),
            ("animation qui finit après son plan", lambda b: b["shots"][0]["layers"][0].update(at=2.3), "hors champ")):
        bad = good_brief("essai_defaut")
        mutate(bad)
        script({"out": bad}, {"out": good_brief("essai_defaut")})
        cp = mograph("brief", "idée", "--id", "essai_defaut", "--force")
        c = calls()
        check(f"brief : {label} -> renvoyé à Claude, corrigé au 2e essai",
              cp.returncode == 0 and len(c) == 2 and needle in c[1]["prompt"], tail(cp, 2))
    b = good_brief()
    b["shots"][0]["background"] = "fg"
    del b["shots"][0]["layers"][0]["color"]
    b["shots"][1]["background"] = "primary"
    del b["shots"][1]["layers"][1]["color"]
    sc = brief.to_scene(b, preset_id="flat_riso", aspect="16:9", model="m", date="2026-10-31")
    check("couleurs par défaut contrastées : texte bg sur fond fg, forme secondary sur fond primary",
          sc["shots"][0]["layers"][0]["color"] == "bg" and sc["shots"][1]["layers"][1]["fill"] == "secondary")

    # B6. CLI non connectée
    script({"out": good_brief("essai_nc")})
    cp = mograph("brief", "idée", "--id", "essai_nc", env={"FAKE_LOGGED_IN": "0"})
    check("brief, CLI non connectée : code 1, message « claude auth login », 0 appel",
          cp.returncode == 1 and "claude auth login" in cp.stderr and calls() == [], tail(cp, 1))
    return target


# ---------------------------------------------------------------------------------------------
class Mcp:
    def __init__(self):
        self.p = subprocess.Popen([PY, str(ROOT / "mograph.py"), "mcp"], cwd=ROOT, env=dict(os.environ),
                                  stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.n = 0
        self.raw: list[str] = []

    def send(self, obj, expect=True):
        line = obj if isinstance(obj, str) else json.dumps(obj, ensure_ascii=False)
        self.p.stdin.write((line + "\n").encode("utf-8"))
        self.p.stdin.flush()
        if not expect:
            return None
        out = self.p.stdout.readline().decode("utf-8")
        self.raw.append(out)
        return json.loads(out)

    def call(self, name, args=None):
        self.n += 1
        return self.send({"jsonrpc": "2.0", "id": self.n, "method": "tools/call",
                          "params": {"name": name, "arguments": args or {}}})

    def close(self):
        self.p.stdin.close()
        rest = self.p.stdout.read().decode("utf-8")
        self.p.wait(timeout=30)
        return rest


def test_mcp(scene_path: Path) -> None:
    m = Mcp()
    try:
        r = m.send({"jsonrpc": "2.0", "id": 0, "method": "initialize",
                    "params": {"protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}})
        res = r.get("result", {})
        check("initialize : version 2025-11-25, capacité tools, serverInfo mograph, instructions",
              res.get("protocolVersion") == "2025-11-25" and "tools" in res.get("capabilities", {})
              and res.get("serverInfo", {}).get("name") == "mograph" and res.get("instructions"))
        m.send({"jsonrpc": "2.0", "method": "notifications/initialized"}, expect=False)
        r = m.send({"jsonrpc": "2.0", "id": "p", "method": "ping"})
        check("notification sans réponse ; ping -> {} (réponse suivante = celle du ping)",
              r.get("id") == "p" and r.get("result") == {})
        r = m.send({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        tools = {t["name"]: t for t in r.get("result", {}).get("tools", [])}
        check("tools/list : validate, preview, draft, all, status, beats, sheets",
              set(tools) == {"validate", "preview", "draft", "all", "status", "beats", "sheets"})
        check("draft et all : « confirm » obligatoire dans le schéma ; annotations destructives",
              all("confirm" in tools[n]["inputSchema"]["required"] and tools[n]["annotations"]["destructiveHint"]
                  for n in ("draft", "all")) and tools["validate"]["annotations"]["readOnlyHint"])

        r = m.call("validate", {"scene": str(scene_path)})
        sc = r["result"]
        check("validate (valide) : isError false, structuredContent.ok, texte JSON identique",
              sc["isError"] is False and sc["structuredContent"]["ok"] is True
              and json.loads(sc["content"][0]["text"]) == sc["structuredContent"])
        bad = SCENES / "essai_invalide.json"
        d = json.loads(scene_path.read_text(encoding="utf-8"))
        d["id"], d["shots"][0]["layers"][0]["anim"][0]["move"] = "essai_invalide", "teleportation"
        bad.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
        r = m.call("validate", {"scene": "essai_invalide"})["result"]
        check("validate (refusée) : isError true + erreurs du compilateur actionnables",
              r["isError"] is True and any("teleportation" in e for e in r["structuredContent"].get("errors", [])))
        for name, args in (("draft", {"scene": "essai_agent"}), ("all", {"scene": "essai_agent", "confirm": False}),
                           ("beats", {"audio": "assets/audio/x.wav", "scene": "essai_agent", "write": True})):
            r = m.call(name, args)["result"]
            check(f"{name} sans confirm: true : isError, message explicite", r["isError"] is True
                  and "confirm" in r["structuredContent"]["error"])
        check("aucun rendu lancé sans confirmation", not (TEST / "build" / "essai_agent_draft").exists()
              and not (TEST / "build" / "essai_agent" / "logs").exists())
        r = m.call("validate", {"scene": "essai_agent", "oups": 1})["result"]
        check("paramètre inconnu : isError avec la liste attendue", r["isError"] and "oups" in r["structuredContent"]["error"])
        r = m.call("inconnu")
        check("outil inconnu : erreur JSON-RPC -32602", r.get("error", {}).get("code") == -32602)
        r = m.send({"jsonrpc": "2.0", "id": 99, "method": "resources/list"})
        check("méthode inconnue : -32601", r.get("error", {}).get("code") == -32601)
        r = m.send("{pas du json")
        check("JSON illisible : -32700, le serveur continue", r.get("error", {}).get("code") == -32700)
        r = m.call("status", {"scene": "essai_agent"})["result"]
        check("status : structuré (plans, maître, processus détachés)", r["isError"] is False
              and r["structuredContent"]["scene_id"] == "essai_agent" and "shots" in r["structuredContent"])
        r = m.call("sheets", {"scene": "essai_agent"})["result"]
        check("sheets avant rendu : isError (pas de séquence maître), message", r["isError"] is True
              and r["structuredContent"].get("error"))
        r = m.send({"jsonrpc": "2.0", "id": 100, "method": "initialize",
                    "params": {"protocolVersion": "1999-01-01", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}})
        check("version inconnue demandée : le serveur propose la sienne (2025-11-25)",
              r["result"]["protocolVersion"] == "2025-11-25")
        r = m.send({"jsonrpc": "2.0", "id": 101, "method": "initialize",
                    "params": {"protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}})
        check("2025-03-26 (lots JSON-RPC obligatoires) non proposée", r["result"]["protocolVersion"] == "2025-11-25")
        r1 = m.send({"jsonrpc": "2.0", "id": 102, "method": "tools/call", "params": {"name": ["status"]}})
        r2 = m.send({"jsonrpc": "2.0", "id": 103, "method": "initialize", "params": [1]})
        r3 = m.send({"jsonrpc": "2.0", "id": 104, "method": "ping"})
        check("nom d'outil ou params non conformes : -32602, le serveur reste en vie",
              r1.get("error", {}).get("code") == -32602 and r2.get("error", {}).get("code") == -32602
              and r3.get("result") == {})
        r1 = m.send({"jsonrpc": "2.0", "id": None, "method": "ping"})
        r2 = m.send([{"jsonrpc": "2.0", "id": 105, "method": "ping"}])
        check("id null et lot JSON-RPC : -32600", r1.get("error", {}).get("code") == -32600
              and r2.get("error", {}).get("code") == -32600)
        r = m.call("draft", {"scene": "essai_invalide", "confirm": True})["result"]
        check("draft confirm: true sur une scène refusée : isError + erreurs, rien n'est lancé",
              r["isError"] is True and r["structuredContent"].get("errors")
              and not (TEST / "build" / "essai_invalide_draft").exists())
    finally:
        rest = m.close()
    lines = [x for x in m.raw + rest.splitlines() if x.strip()]
    ok = True
    for x in lines:
        try:
            ok &= json.loads(x).get("jsonrpc") == "2.0"
        except ValueError:
            ok = False
    check("stdout du serveur : uniquement des messages JSON-RPC", ok and len(lines) >= 15, f"{len(lines)} lignes")


def test_json_and_ids() -> None:
    for args in (("status", "nope", "--json"), ("sheets", "nope", "--json"), ("beats", "absent.wav", "--json")):
        cp = mograph(*args)
        try:
            out = json.loads(cp.stdout)
        except ValueError:
            out = {}
        check(f"{args[0]} --json en échec : UN objet JSON {{ok: false, error}} sur stdout, code 1",
              cp.returncode == 1 and out.get("ok") is False and out.get("error"), cp.stdout[:120])
    esc = SCENES / "evasion.json"
    esc.write_text(json.dumps({"id": "../evasion", "version": "1.0"}), encoding="utf-8")
    cp = mograph("all", str(esc), "--draft", "--detach")
    check("id de scène hors motif (« ../evasion ») : refusé, aucun dossier hors de build/",
          cp.returncode == 1 and not (TEST / "evasion_draft").exists() and not (TEST / "evasion").exists(), tail(cp, 1))


def test_draft_vision() -> None:
    m = Mcp()
    try:
        m.send({"jsonrpc": "2.0", "id": 0, "method": "initialize",
                "params": {"protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}})
        m.send({"jsonrpc": "2.0", "method": "notifications/initialized"}, expect=False)
        r = m.call("draft", {"scene": "essai_agent", "confirm": True})["result"]
        check("draft confirm: true : rendu détaché lancé", r["isError"] is False and r["structuredContent"]["started"],
              str(r["structuredContent"])[:200])
        check("détachement d'une préversion : commande de suivi exacte (status <scène> --draft)",
              "status essai_agent --draft" in r["structuredContent"].get("output", ""))
        r2 = m.call("draft", {"scene": "essai_agent", "confirm": True})["result"]
        check("second draft pendant le rendu : refusé (déjà en cours)", r2["isError"] is True
              and "déjà en cours" in r2["structuredContent"]["error"])
        st = {}
        t0 = time.perf_counter()
        while time.perf_counter() - t0 < 600:
            st = m.call("status", {"scene": "essai_agent", "draft": True})["result"]["structuredContent"]
            if st.get("detached") and not any(d["running"] for d in st["detached"]):
                break
            time.sleep(3)
        out = TEST / "out" / "essai_agent_draft"
        mp4 = list(out.glob("*.mp4"))
        check("préversion terminée : processus fini, maître complet, MP4 et planche contact",
              st.get("master") and st["master"]["done"] == st["master"]["total"] and mp4
              and (out / "planche_contact.png").is_file(),
              f"{time.perf_counter() - t0:.0f} s ; {(st.get('detached') or [{}])[0].get('tail', [])[-1:]}")

        import mograph as cli
        logs = TEST / "build" / "essai_agent" / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        lock = logs / "en_cours.pid"
        lock.write_text(f"{os.getpid()}\n\n{cli._proc_start(os.getpid())}\n", encoding="utf-8")
        r = m.call("all", {"scene": "essai_agent", "confirm": True})["result"]
        check("rendu au premier plan en cours (en_cours.pid) : all refusé", r["isError"] is True
              and "déjà en cours" in r["structuredContent"]["error"])
        cp = mograph("all", "essai_agent")
        check("CLI : second rendu de la même scène refusé au démarrage", cp.returncode == 1
              and "déjà en cours" in cp.stderr, tail(cp, 1))
        lock.write_text(f"{os.getpid()}\n\n123\n", encoding="utf-8")
        st = m.call("status", {"scene": "essai_agent"})["result"]["structuredContent"]
        check("PID réattribué (jeton de démarrage différent) : pas pris pour un rendu en cours",
              st["detached"] and not any(d["running"] for d in st["detached"]))
        lock.unlink()

        script({"hang": 60, "out": {"summary": "x", "issues": []}})
        m.send({"jsonrpc": "2.0", "id": "lent", "method": "tools/call",
                "params": {"name": "sheets", "arguments": {"scene": "essai_agent", "draft": True, "vision": True}}},
               expect=False)
        t1 = time.perf_counter()
        while not calls() and time.perf_counter() - t1 < 30:
            time.sleep(0.3)
        m.send({"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": "lent"}}, expect=False)
        r = m.send({"jsonrpc": "2.0", "id": "p2", "method": "ping"})
        dt = time.perf_counter() - t1
        check("annulation d'un outil long : ping servi aussitôt (réponse suivante = ping)",
              r.get("id") == "p2" and dt < 30, f"{dt:.1f} s")

        issues = [{"severity": "attention", "where": "image 12", "problem": "texte proche du bord",
                   "suggestion": "y: safe:45%"}]
        script({"out": {"summary": "Lisible dans l'ensemble.", "issues": issues}})
        r = m.call("sheets", {"scene": "essai_agent", "draft": True, "vision": True})["result"]
        rep = (r["structuredContent"] or {}).get("vision") or {}
        c = calls()
        a = c[0]["argv"] if c else []
        check("sheets + vision : relecture jointe, non bloquante, outil Read seul, dossier de la planche autorisé",
              r["isError"] is False and rep.get("blocking") is False and rep.get("sheets", [{}])[0].get("issues") == issues
              and a[a.index("--tools") + 1] == "Read" and Path(a[a.index("--add-dir") + 1]).resolve() == out.resolve(),
              str(r["structuredContent"])[:200])
        check("vision_qc.json et vision_qc.md écrits (avertissements indicatifs)",
              (out / "vision_qc.json").is_file() and "texte proche du bord" in (out / "vision_qc.md").read_text(encoding="utf-8"))
        script({"out": {"summary": "x", "issues": []}})
        cp = mograph("sheets", "essai_agent", "--draft", "--vision", env={"FAKE_LOGGED_IN": "0"})
        rep = json.loads((out / "vision_qc.json").read_text(encoding="utf-8"))
        check("vision, CLI non connectée : code 0 (jamais bloquant), avertissement consigné, 0 appel",
              cp.returncode == 0 and rep["warnings"] and calls() == [], tail(cp, 1))
    finally:
        rest = m.close()
    check("requête annulée : aucune réponse envoyée", '"id": "lent"' not in rest and all(
        '"id": "lent"' not in x for x in m.raw), rest[:120])
    # Seul « sheets --vision » importe vision_qc : ni le QC, ni la porte de déterminisme, ni run_all.
    users = sorted(f.name for f in list((ROOT / "pipeline").glob("*.py")) + [ROOT / "mograph.py"]
                   if re.search(r"vision_qc", f.read_text(encoding="utf-8")) and f.name != "vision_qc.py")
    cli = (ROOT / "mograph.py").read_text(encoding="utf-8")
    sheets_fn = cli[cli.index("def cmd_sheets"):cli.index("def cmd_preview")]
    check("QC automatique et porte de déterminisme ne lisent pas la relecture visuelle (vision_qc importé par "
          "sheets --vision seul)", users == ["mograph.py"] and cli.count("vision_qc") == sheets_fn.count("vision_qc")
          and not (TEST / "out" / "essai_agent_draft" / "qc_report.json").exists(), str(users))


def test_static() -> None:
    sk = (ROOT / ".claude" / "skills" / "mograph" / "SKILL.md").read_text(encoding="utf-8")
    fm = re.match(r"^---\n(.*?)\n---\n", sk, re.S)
    meta = dict(re.findall(r"^([a-z_-]+):\s*(.+)$", fm.group(1), re.M)) if fm else {}
    rules = re.findall(r"^(\d+)\. ", sk.split("## Les dix règles", 1)[-1].split("\n## ", 1)[0], re.M)
    check("skill : frontmatter (name mograph, description), dix règles du guide, outils MCP",
          meta.get("name") == "mograph" and len(meta.get("description", "")) > 50 and rules == [str(i) for i in range(1, 11)]
          and "GUIDE_SCENES.md" in sk and "confirm: true" in sk)
    mcp = json.loads((ROOT / ".mcp.json").read_text(encoding="utf-8"))
    s = mcp.get("mcpServers", {}).get("mograph", {})
    check(".mcp.json : serveur stdio « mograph » = mograph.py mcp", s.get("type") == "stdio" and s.get("args") == ["mograph.py", "mcp"])
    bad = []
    for f in list((ROOT / "pipeline").glob("*.py")) + [ROOT / "mograph.py"]:
        txt = f.read_text(encoding="utf-8")
        if re.search(r"^\s*(import anthropic|from anthropic)", txt, re.M) or "api.anthropic.com" in txt or "x-api-key" in txt:
            bad.append(f.name)
    check("aucun SDK ni point d'accès API Anthropic dans le code (seule la CLI claude -p est appelée)", not bad, str(bad))


def main() -> int:
    assert TEST.parent.name == "_tests"
    if TEST.exists():
        shutil.rmtree(TEST)
    SCENES.mkdir(parents=True)
    FAKE.write_text(FAKE_SRC, encoding="utf-8")
    if not check("environnement isolé : scènes, build et out de test", config.SCENES_DIR == SCENES.resolve()
                 and config.BUILD_DIR == (TEST / "build").resolve()):
        return 1
    t0 = time.perf_counter()
    try:
        test_static()
        test_cli()
        target = test_brief()
        test_mcp(target)
        test_json_and_ids()
        test_draft_vision()
    except Exception as exc:  # noqa: BLE001 — rapporté comme échec, jamais masqué
        import traceback
        traceback.print_exc()
        check("les contrôles s'exécutent sans exception", False, f"{type(exc).__name__} : {exc}"[:200])
    print(f"\nDurée : {time.perf_counter() - t0:.0f} s ; aucun appel réel à Claude (faux exécutable)")
    if FAILS:
        print(f"ÉCHEC : {len(FAILS)} contrôle(s) : {FAILS}")
        return 1
    print("SUCCÈS : couche agent conforme (phase 6).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
