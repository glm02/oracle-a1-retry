#!/usr/bin/env python3
"""
Tableau de bord local (127.0.0.1 uniquement) du script de retry Oracle A1.

- Lit le journal du script et le regroupe par tentative (au lieu d'afficher
  les lignes brutes), pour voir d'un coup d'oeil où on en est.
- Vérifie toutes les 10 s si le script tourne vraiment, pour ne pas afficher
  « en attente » alors que plus rien ne tente.
- Permet de relancer le script depuis la page s'il est arrêté, sans doublon.

Aucune dépendance : bibliothèque standard Python uniquement.
Usage : python dashboard/server.py   puis http://127.0.0.1:8787

Variables d'environnement (toutes optionnelles) :
  OCI_RETRY_LOG   chemin du journal (défaut : ../retry_instance.log)
  OCI_RETRY_PORT  port d'écoute (défaut : 8787)
  OCI_RETRY_BASH  exécutable bash pour le bouton « Relancer »
  CONFIG_FILE     fichier de config (défaut : ../config.env)

Style : jetons de couleur de kargulstudio/sales-crm (MIT).
"""
import json
import os
import re
import shutil
import subprocess
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

IS_WINDOWS = os.name == "nt"
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOG_PATH = os.environ.get("OCI_RETRY_LOG", os.path.join(BASE_DIR, "retry_instance.log"))
SCRIPT_PATH = os.path.join(BASE_DIR, "retry_instance.sh")
CONFIG_PATH = os.environ.get("CONFIG_FILE", os.path.join(BASE_DIR, "config.env"))
_GIT_BASH = r"C:\Program Files\Git\bin\bash.exe"
BASH = os.environ.get("OCI_RETRY_BASH") or (
    _GIT_BASH if IS_WINDOWS and os.path.exists(_GIT_BASH) else shutil.which("bash") or "bash"
)
HOST, PORT = "127.0.0.1", int(os.environ.get("OCI_RETRY_PORT", "8787"))
ORIGIN = f"http://{HOST}:{PORT}"

LINE_RE = re.compile(r"^\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\] (.*)$")
MSG_RE = re.compile(r'"message":\s*"([^"]*)"')

# État du processus, mis à jour par un fil en arrière-plan (la requête
# PowerShell prend ~1 s, on ne veut pas la payer à chaque rafraîchissement).
_proc = {"running": None}


def _check_script_running():
    try:
        if IS_WINDOWS:
            cmd = (
                "(Get-CimInstance Win32_Process -Filter \"Name='bash.exe'\" | "
                "Where-Object { $_.CommandLine -match 'retry_instance' } | Measure-Object).Count"
            )
            out = subprocess.run(
                ["powershell", "-NoProfile", "-Command", cmd],
                capture_output=True, text=True, timeout=15,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            return int((out.stdout or "0").strip() or 0) > 0
        out = subprocess.run(["pgrep", "-f", "retry_instance.sh"], capture_output=True, timeout=5)
        return out.returncode == 0
    except Exception:
        return None  # inconnu : on devinera d'après le journal


def _watch_process():
    while True:
        _proc["running"] = _check_script_running()
        time.sleep(10)


def _ts_ms(s):
    return int(datetime.strptime(s, "%Y-%m-%d %H:%M:%S").timestamp() * 1000)


def read_config():
    """Cible affichée (nom, OCPU, RAM, disque, région) : config.env d'abord,
    sinon les valeurs écrites en dur dans le script."""
    txt = ""
    for path in (CONFIG_PATH, SCRIPT_PATH):
        try:
            with open(path, encoding="utf-8") as f:
                txt += f.read() + "\n"
        except OSError:
            pass
    cfg = {}
    for key in ("DISPLAY_NAME", "OCPUS", "MEMORY_GB", "BOOT_GB", "AVAILABILITY_DOMAIN", "AD"):
        # Le premier fichier lu (config.env) l'emporte ; on ignore les ${VAR:=...}.
        m = re.search(rf'^\s*{key}="?([^"\n$#]+?)"?\s*(?:#.*)?$', txt, re.M)
        if not m:  # valeur par défaut du script : : "${OCPUS:=4}"
            m = re.search(r"\$\{" + key + r":=([^}]+)\}", txt)
        if m and m.group(1).strip():
            cfg[key.lower()] = m.group(1).strip()
    ad = cfg.pop("availability_domain", "") or cfg.pop("ad", "")
    cfg.pop("ad", None)
    m = re.search(r":([A-Z]+-[A-Z]+-\d+)-AD", ad)
    cfg["region"] = m.group(1).lower() if m else ""
    return cfg


def parse_log():
    try:
        with open(LOG_PATH, encoding="utf-8", errors="replace") as f:
            lines = f.read().splitlines()
    except FileNotFoundError:
        lines = []

    attempts, runs, ip, final, error_msg = [], 0, None, None, None
    telegram_sent = False
    cur, expect_err = None, False

    for line in lines:
        m = LINE_RE.match(line)
        if not m:
            # Lignes de détail JSON d'une erreur : on garde juste le message.
            if expect_err:
                mm = MSG_RE.search(line)
                if mm:
                    error_msg, expect_err = mm.group(1), False
            continue
        t, msg = _ts_ms(m.group(1)), m.group(2)

        if "Démarrage du script" in msg:
            runs += 1
            if final == "error":
                final = None  # une relance efface l'arrêt précédent
            if cur and cur["result"] == "pending":
                cur["result"] = "interrupted"
        elif msg.startswith("Tentative #"):
            if cur and cur["result"] == "pending":
                cur["result"] = "interrupted"
            cur = {"ts": t, "result": "pending", "done": None, "pause": None}
            attempts.append(cur)
        elif cur and "Pas de capacité" in msg:
            cur.update(result="capacity", done=t)
        elif cur and "Limitation de débit" in msg:
            cur.update(result="ratelimit", done=t)
        elif cur and "Erreur inattendue" in msg:
            cur.update(result="error", done=t)
            final, expect_err, error_msg = "error", True, None
        elif cur and "SUCCÈS" in msg:
            cur.update(result="success", done=t)
            final = "success"
        elif "IP PUBLIQUE" in msg:
            ip = msg.split(":", 1)[1].strip()
        elif "Alerte Telegram envoyée" in msg:
            telegram_sent = True
        elif cur and msg.startswith("Pause de"):
            mm = re.search(r"(\d+)s", msg)
            if mm:
                cur["pause"] = int(mm.group(1))

    now = int(time.time() * 1000)
    last = attempts[-1] if attempts else None
    next_try = None
    if last and last["done"] and last["pause"]:
        next_try = last["done"] + last["pause"] * 1000

    running = _proc["running"]
    if running is None:
        # Processus inconnu : on devine d'après la fraîcheur du journal.
        if last and last["result"] == "pending":
            running = now - last["ts"] < 7 * 60 * 1000
        elif next_try:
            running = now < next_try + 2 * 60 * 1000
        else:
            running = False

    if final == "success":
        state = "success"
    elif final == "error" and not running:
        state = "error"
    elif not running:
        state = "stopped" if attempts else "empty"
    elif last and last["result"] == "pending":
        state = "trying"
    elif last and last["result"] == "ratelimit":
        state = "ratelimit"
    else:
        state = "waiting"

    counts = {k: 0 for k in ("capacity", "ratelimit", "error", "success", "pending", "interrupted")}
    for a in attempts:
        counts[a["result"]] += 1

    out = []
    for i, a in enumerate(attempts):
        out.append({
            "n": i + 1,
            "ts": a["ts"],
            "result": a["result"],
            "duration": round((a["done"] - a["ts"]) / 1000) if a["done"] else None,
            "pause": a["pause"],
        })

    return {
        "state": state,
        "running": running,
        "now": now,
        "next_try": next_try if state in ("waiting", "ratelimit") else None,
        "pause": last["pause"] if last else None,
        "last_ts": last["ts"] if last else None,
        "first_ts": attempts[0]["ts"] if attempts else None,
        "total": len(attempts),
        "runs": runs,
        "counts": counts,
        "ip": ip,
        "telegram_sent": telegram_sent,
        "error_msg": error_msg,
        "config": read_config(),
        "attempts": out[-300:][::-1],
    }


def start_script():
    # Détaché du serveur : fermer le tableau de bord n'arrête pas les tentatives.
    opts = dict(stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True)
    if IS_WINDOWS:
        opts["creationflags"] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        opts["start_new_session"] = True
    subprocess.Popen([BASH, SCRIPT_PATH], cwd=BASE_DIR, **opts)
    _proc["running"] = True


PAGE = r"""<!DOCTYPE html>
<html lang="fr">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Oracle A1 · suivi</title>
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 16 16'%3E%3Ccircle cx='8' cy='8' r='6' fill='%234124fb'/%3E%3C/svg%3E">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Geist:wght@400;500;600&family=Geist+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>
  /* Jetons de kargulstudio/sales-crm (MIT). */
  :root {
    color-scheme: dark;
    --radius: 0.5rem;
    --background: #161616;
    --foreground: #f9fbff;
    --card: #1b1d20;
    --secondary: #1e1e1e;
    --muted: #2a2a2a;
    --muted-foreground: #7f7f7f;
    --border: #232323;
    --line-strong: #393939;
    --ring: #676767;
    --subtle: #676767;
    --soft: #a4a4a4;
    --primary: #4124fb;
    --success: #22c55e;
    --warning: #fbbf24;
    --danger: #f97373;
    --track: #3a3a3a;
    --ease: cubic-bezier(0.25, 1, 0.5, 1);
    --tag-blue-bg: #1d2b3e;    --tag-blue-border: #23354c;    --tag-blue-text: #bfdbfe;
    --tag-moss-bg: #23451d;    --tag-moss-border: #2e5029;    --tag-moss-text: #b1ebc5;
    --tag-red-bg: #3e1d1e;     --tag-red-border: #4c2324;     --tag-red-text: #febfc6;
    --tag-orange-bg: #3e291d;  --tag-orange-border: #764d35;  --tag-orange-text: #eeb390;
    --tag-amber-bg: #31221b;   --tag-amber-border: #6c4830;   --tag-amber-text: #fed7aa;
    --tag-neutral-bg: #2a2a2a; --tag-neutral-border: #363636; --tag-neutral-text: #cfcfcf;
  }
  * { box-sizing: border-box; }
  html, body { margin: 0; }
  body {
    background: var(--background); color: var(--foreground);
    font: 400 14px/1.15 "Geist", -apple-system, "Segoe UI", sans-serif;
    -webkit-font-smoothing: antialiased;
  }
  .mono { font-family: "Geist Mono", ui-monospace, monospace; font-variant-numeric: tabular-nums; }
  .muted { color: var(--muted-foreground); }
  [hidden] { display: none !important; }

  /* En-tête */
  .topbar {
    position: sticky; top: 0; z-index: 2;
    display: flex; align-items: center; justify-content: space-between; gap: 12px;
    height: 52px; padding: 0 24px;
    background: rgba(22,22,22,.9); backdrop-filter: blur(8px);
    border-bottom: 1px solid var(--border);
  }
  .crumbs { display: flex; align-items: center; gap: 8px; min-width: 0; white-space: nowrap; overflow: hidden; }
  .crumbs .sep { color: var(--line-strong); }
  .crumbs .here { font-weight: 500; overflow: hidden; text-overflow: ellipsis; }
  .actions { display: flex; align-items: center; gap: 12px; flex-shrink: 0; }
  .live { display: flex; align-items: center; gap: 6px; font-size: 12px; color: var(--muted-foreground); }
  .dot { width: 6px; height: 6px; border-radius: 50%; background: var(--subtle); }
  .dot.ok { background: var(--success); }
  .dot.ko { background: var(--danger); }

  .content { max-width: 76.25rem; margin: 0 auto; padding: 24px; display: grid; grid-template-columns: minmax(0, 1fr); gap: 16px; }
  .content > * { min-width: 0; }

  /* Boutons et tags */
  .btn {
    display: inline-flex; align-items: center; gap: 6px; height: 30px; padding: 0 12px;
    border-radius: 9999px; border: 1px solid transparent; cursor: pointer;
    font: 500 13px "Geist", sans-serif; color: var(--foreground); white-space: nowrap;
    transition: background-color .15s var(--ease), border-color .15s var(--ease);
  }
  .btn:focus-visible { outline: 2px solid var(--ring); outline-offset: 2px; }
  .btn:disabled { opacity: .5; cursor: default; }
  .btn-primary { background: var(--primary); box-shadow: inset 0 1px 0 rgba(255,255,255,.18), inset 0 -1px 0 rgba(0,0,0,.25); }
  .btn-primary:hover:not(:disabled) { background: #5236ff; }
  .btn-secondary { background: var(--secondary); border-color: var(--line-strong); }
  .btn-secondary:hover { background: var(--muted); }
  .btn-sm { height: 26px; padding: 0 10px; font-size: 12px; }

  .tag {
    display: inline-flex; align-items: center; height: 22px; padding: 0 8px;
    border-radius: 9999px; border: 1px solid; font-size: 12px; line-height: 1; white-space: nowrap;
  }
  .t-blue    { border-color: var(--tag-blue-border);    background: var(--tag-blue-bg);    color: var(--tag-blue-text); }
  .t-moss    { border-color: var(--tag-moss-border);    background: var(--tag-moss-bg);    color: var(--tag-moss-text); }
  .t-red     { border-color: var(--tag-red-border);     background: var(--tag-red-bg);     color: var(--tag-red-text); }
  .t-orange  { border-color: var(--tag-orange-border);  background: var(--tag-orange-bg);  color: var(--tag-orange-text); }
  .t-amber   { border-color: var(--tag-amber-border);   background: var(--tag-amber-bg);   color: var(--tag-amber-text); }
  .t-neutral { border-color: var(--tag-neutral-border); background: var(--tag-neutral-bg); color: var(--tag-neutral-text); }

  .card { background: var(--card); border: 1px solid var(--border); border-radius: var(--radius); }
  .card-head {
    display: flex; align-items: center; justify-content: space-between; gap: 12px; flex-wrap: wrap;
    padding: 12px 16px; border-bottom: 1px solid var(--border);
  }
  h2 { font-size: 14px; font-weight: 600; margin: 0; }

  /* Bloc principal */
  .hero { display: grid; grid-template-columns: minmax(0, 1fr) 300px; }
  .hero-main { padding: 20px; display: flex; flex-direction: column; gap: 10px; min-width: 0; }
  .hero-main h1 { font-size: 16px; font-weight: 600; margin: 4px 0 0; }
  .hero-main p { margin: 0; color: var(--muted-foreground); }
  .cd { margin-top: 14px; }
  .cd-label { font-size: 12px; color: var(--muted-foreground); text-transform: uppercase; letter-spacing: 1px; }
  .cd-value { font-size: 40px; font-weight: 500; letter-spacing: -0.5px; margin: 6px 0 10px; line-height: 1; }
  .track { height: 4px; border-radius: 9999px; background: var(--track); overflow: hidden; }
  .fill { height: 100%; width: 0; background: var(--primary); border-radius: 9999px; transition: width 1s linear; }
  .cd-foot { display: flex; justify-content: space-between; margin-top: 8px; font-size: 12px; color: var(--muted-foreground); }

  .success { margin-top: 10px; display: grid; gap: 8px; }
  .copy-row {
    display: flex; align-items: center; justify-content: space-between; gap: 12px;
    padding: 10px 12px; border: 1px solid var(--border); border-radius: var(--radius); background: var(--background);
  }
  .copy-row .mono { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .ip-big { font-size: 24px; color: var(--tag-moss-text); }

  .specs { margin: 0; padding: 8px 20px; border-left: 1px solid var(--border); }
  .spec { display: flex; justify-content: space-between; gap: 12px; padding: 9px 0; border-bottom: 1px solid var(--border); }
  .spec:last-child { border-bottom: none; }
  .spec dt { color: var(--muted-foreground); }
  .spec dd { margin: 0; text-align: right; }

  /* Bande des dernières tentatives */
  .legend { display: flex; gap: 14px; flex-wrap: wrap; font-size: 12px; color: var(--muted-foreground); }
  .legend span { display: inline-flex; align-items: center; gap: 6px; }
  .legend i { width: 8px; height: 8px; border-radius: 2px; display: inline-block; }
  .strip { display: flex; gap: 3px; align-items: stretch; height: 32px; padding: 14px 16px; box-sizing: content-box; }
  .strip .b { flex: 1 1 0; max-width: 12px; border-radius: 2px; }
  .strip .empty-strip { color: var(--muted-foreground); font-size: 13px; align-self: center; }
  .r-capacity { background: #8a6420; }
  .r-ratelimit { background: #c2703d; }
  .r-error { background: var(--danger); }
  .r-success { background: var(--success); }
  .r-pending { background: var(--primary); }
  .r-interrupted { background: var(--line-strong); }

  /* Tableau */
  .segments { display: flex; gap: 4px; flex-wrap: wrap; }
  .seg {
    height: 26px; padding: 0 10px; border-radius: 9999px; border: 1px solid transparent;
    background: transparent; color: var(--muted-foreground); cursor: pointer;
    font: 500 12px "Geist", sans-serif; display: inline-flex; align-items: center; gap: 6px;
    transition: background-color .15s var(--ease), color .15s var(--ease);
  }
  .seg:hover { color: var(--foreground); }
  .seg[aria-pressed="true"] { background: var(--muted); border-color: var(--line-strong); color: var(--foreground); }
  .seg:focus-visible { outline: 2px solid var(--ring); outline-offset: 2px; }
  .seg .c { color: var(--subtle); font-family: "Geist Mono", monospace; }

  .table-wrap { max-height: 520px; overflow: auto; overscroll-behavior: contain; }
  table { width: 100%; border-collapse: collapse; }
  thead th {
    position: sticky; top: 0; background: var(--card);
    text-align: left; font-size: 12px; font-weight: 500; color: var(--muted-foreground);
    padding: 9px 16px; border-bottom: 1px solid var(--border);
  }
  tbody td { padding: 9px 16px; border-bottom: 1px solid var(--border); white-space: nowrap; }
  tbody tr:hover td { background: #1f2124; }
  .num { text-align: right; }
  .col-n { color: var(--subtle); width: 64px; }
  .tfoot { padding: 10px 16px; font-size: 12px; color: var(--muted-foreground); border-top: 1px solid var(--border); }
  .empty-row td { text-align: center; color: var(--muted-foreground); padding: 28px 16px; }

  @media (max-width: 760px) {
    .topbar { padding: 0 16px; }
    .content { padding: 16px; gap: 12px; }
    .hero { grid-template-columns: 1fr; }
    .specs { border-left: none; border-top: 1px solid var(--border); padding: 4px 16px; }
    .hero-main { padding: 16px; }
    .cd-value { font-size: 34px; }
    .col-dur, .col-pause { display: none; }
    tbody td, thead th { padding-left: 12px; padding-right: 12px; }
    .live-text { display: none; }
    .crumbs .opt { display: none; }
  }
</style>
</head>
<body>
<header class="topbar">
  <nav class="crumbs" aria-label="Fil d'Ariane">
    <span class="muted opt">Oracle Cloud</span><span class="sep opt">/</span>
    <span class="muted" id="c-region">—</span><span class="sep">/</span>
    <span class="here" id="c-name">—</span>
  </nav>
  <div class="actions">
    <span class="live"><span class="dot" id="live-dot"></span><span class="live-text" id="live-text">Connexion…</span></span>
    <button class="btn btn-primary" id="btn-start" hidden>Relancer le script</button>
  </div>
</header>

<main class="content">
  <section class="card hero">
    <div class="hero-main">
      <div><span class="tag t-neutral" id="state-tag">…</span></div>
      <h1 id="state-title">Chargement</h1>
      <p id="state-sub"></p>

      <div class="cd" id="cd" hidden>
        <div class="cd-label" id="cd-label">Prochaine tentative dans</div>
        <div class="cd-value mono" id="cd-value">--:--</div>
        <div class="track"><div class="fill" id="cd-fill"></div></div>
        <div class="cd-foot"><span id="cd-left"></span><span id="cd-right"></span></div>
      </div>

      <div class="success" id="success" hidden>
        <div class="copy-row">
          <span class="mono ip-big" id="ip-value"></span>
          <button class="btn btn-secondary btn-sm" data-copy="ip">Copier l'IP</button>
        </div>
        <div class="copy-row">
          <span class="mono" id="ssh-cmd"></span>
          <button class="btn btn-secondary btn-sm" data-copy="ssh">Copier</button>
        </div>
      </div>
    </div>
    <dl class="specs" id="specs"></dl>
  </section>

  <section class="card">
    <div class="card-head">
      <h2>Dernières tentatives</h2>
      <div class="legend">
        <span><i class="r-capacity"></i>Capacité saturée</span>
        <span><i class="r-ratelimit"></i>Limite 429</span>
        <span><i class="r-error"></i>Erreur</span>
        <span><i class="r-success"></i>Réussi</span>
      </div>
    </div>
    <div class="strip" id="strip"></div>
  </section>

  <section class="card">
    <div class="card-head">
      <h2>Historique</h2>
      <div class="segments" id="segments" role="group" aria-label="Filtrer par résultat"></div>
    </div>
    <div class="table-wrap">
      <table>
        <thead><tr>
          <th class="col-n">#</th><th>Heure</th><th>Résultat</th>
          <th class="num col-dur">Réponse</th><th class="num col-pause">Pause</th>
        </tr></thead>
        <tbody id="rows"></tbody>
      </table>
    </div>
    <div class="tfoot" id="tfoot"></div>
  </section>
</main>

<script>
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s == null ? '' : s).replace(/[&<>"']/g, (c) => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));

const RESULT = {
  capacity:    { label: 'Capacité saturée', tone: 'amber' },
  ratelimit:   { label: 'Limite 429',       tone: 'orange' },
  error:       { label: 'Erreur',           tone: 'red' },
  success:     { label: 'Réussi',           tone: 'moss' },
  pending:     { label: 'En cours',         tone: 'blue' },
  interrupted: { label: 'Interrompue',      tone: 'neutral' },
};
const STATE = {
  waiting:   { tag: 'En attente', tone: 'amber',   title: 'Pas de machine ARM libre pour l’instant', sub: 'Le script réessaie tout seul. Dès qu’une place se libère, la VM est créée et réservée directement.' },
  ratelimit: { tag: 'Ralenti',    tone: 'orange',  title: 'Oracle demande de ralentir (429)', sub: 'Pause plus longue avant la prochaine tentative, pour ne pas se faire bloquer.' },
  trying:    { tag: 'En cours',   tone: 'blue',    title: 'Tentative de création en cours', sub: 'Réponse d’Oracle attendue dans quelques secondes.' },
  success:   { tag: 'Réservée',   tone: 'moss',    title: 'Ta VM est créée et allumée', sub: 'Elle est à toi, rien à confirmer.' },
  error:     { tag: 'Arrêté',     tone: 'red',     title: 'Le script s’est arrêté sur une erreur', sub: '' },
  stopped:   { tag: 'Arrêté',     tone: 'neutral', title: 'Le script ne tourne pas', sub: 'Plus aucune tentative. Relance-le pour continuer.' },
  empty:     { tag: 'Prêt',       tone: 'neutral', title: 'Aucune tentative pour l’instant', sub: 'Lance le script pour commencer.' },
};
const FILTERS = [
  ['all', 'Tout'], ['capacity', 'Capacité'], ['ratelimit', '429'], ['error', 'Erreurs'], ['success', 'Réussi'],
];

let data = null, offset = 0, lastOk = 0, filter = 'all';

const now = () => Date.now() + offset;
const pad = (n) => String(n).padStart(2, '0');
function fmtClock(ms) {
  const d = new Date(ms);
  return pad(d.getHours()) + ':' + pad(d.getMinutes()) + ':' + pad(d.getSeconds());
}
function fmtStamp(ms) {
  const d = new Date(ms), t = new Date(now());
  const same = d.toDateString() === t.toDateString();
  return (same ? '' : pad(d.getDate()) + '/' + pad(d.getMonth() + 1) + ' ') + fmtClock(ms);
}
function fmtDur(s) {
  if (s == null) return '—';
  s = Math.max(0, Math.round(s));
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), x = s % 60;
  if (h) return h + ' h ' + pad(m);
  if (m) return m + ' min ' + pad(x);
  return x + ' s';
}
function fmtCd(ms) {
  const s = Math.max(0, Math.ceil(ms / 1000));
  return pad(Math.floor(s / 60)) + ':' + pad(s % 60);
}

function renderSpecs(d) {
  const c = d.config || {};
  // Troisième valeur : true si c'est un chiffre technique (police à chasse fixe).
  const rows = [
    ['Machine', 'A1.Flex · ARM', false],
    ['Processeur', c.ocpus ? c.ocpus + ' OCPU' : '—', true],
    ['Mémoire', c.memory_gb ? c.memory_gb + ' Go' : '—', true],
    ['Disque', c.boot_gb ? c.boot_gb + ' Go' : '—', true],
    ['Coût', 'Gratuit · Always Free', false],
    ['Tentatives', String(d.total), true],
    // Après le succès, la durée est figée sur la tentative gagnante.
    [d.state === 'success' ? 'Trouvée en' : 'En recherche depuis',
     d.first_ts ? fmtDur(((d.state === 'success' ? d.last_ts : now()) - d.first_ts) / 1000) : '—', true],
  ];
  $('specs').innerHTML = rows.map(([k, v, mono]) =>
    '<div class="spec"><dt>' + esc(k) + '</dt><dd' + (mono ? ' class="mono"' : '') + '>' + esc(v) + '</dd></div>'
  ).join('');
  $('c-region').textContent = c.region || 'région';
  $('c-name').textContent = c.display_name || 'instance';
}

function renderHero(d) {
  const s = STATE[d.state] || STATE.empty;
  const tag = $('state-tag');
  tag.textContent = s.tag;
  tag.className = 'tag t-' + s.tone;
  $('state-title').textContent = s.title;
  let sub = s.sub;
  if (d.state === 'error') sub = d.error_msg || 'Voir le journal retry_instance.log.';
  if (d.state === 'success' && d.telegram_sent) sub += ' Une alerte est partie sur Telegram.';
  $('state-sub').textContent = sub;
  $('btn-start').hidden = !(d.state === 'stopped' || d.state === 'error');

  $('success').hidden = d.state !== 'success';
  if (d.state === 'success' && d.ip) {
    $('ip-value').textContent = d.ip;
    $('ssh-cmd').textContent = 'ssh ubuntu@' + d.ip;
  }
  $('cd').hidden = !(d.state === 'waiting' || d.state === 'ratelimit' || d.state === 'trying');
}

function tick() {
  if (!data) return;
  const n = now();
  if (data.state === 'trying') {
    $('cd-label').textContent = 'Tentative lancée il y a';
    $('cd-value').textContent = fmtCd(n - data.last_ts);
    $('cd-fill').style.width = '100%';
    $('cd-left').textContent = 'Lancée à ' + fmtClock(data.last_ts);
    $('cd-right').textContent = '';
  } else if (data.next_try) {
    const left = data.next_try - n, total = (data.pause || 1) * 1000;
    $('cd-label').textContent = 'Prochaine tentative dans';
    $('cd-value').textContent = left > 0 ? fmtCd(left) : '00:00';
    $('cd-fill').style.width = Math.min(100, Math.max(0, 100 * (1 - left / total))) + '%';
    $('cd-left').textContent = 'Dernière à ' + fmtClock(data.last_ts);
    $('cd-right').textContent = left > 0 ? 'vers ' + fmtClock(data.next_try) : 'imminente';
  }
  const age = Math.round((Date.now() - lastOk) / 1000);
  const ok = lastOk && age < 15;
  $('live-dot').className = 'dot ' + (ok ? 'ok' : 'ko');
  $('live-text').textContent = ok ? (data.running ? 'Script actif' : 'Script arrêté') + ' · à jour' : 'Tableau de bord déconnecté';
}

function renderStrip(d) {
  const list = d.attempts.slice(0, 80).reverse();
  if (!list.length) {
    $('strip').innerHTML = '<span class="empty-strip">Aucune tentative.</span>';
    return;
  }
  $('strip').innerHTML = list.map((a) =>
    '<span class="b r-' + a.result + '" title="#' + a.n + ' · ' + esc(fmtStamp(a.ts)) + ' · ' + esc(RESULT[a.result].label) + '"></span>'
  ).join('');
}

function renderSegments(d) {
  const counts = Object.assign({ all: d.total }, d.counts);
  $('segments').innerHTML = FILTERS.filter(([k]) => k === 'all' || counts[k] > 0).map(([k, label]) =>
    '<button class="seg" data-f="' + k + '" aria-pressed="' + (filter === k) + '">' + label +
    ' <span class="c">' + counts[k] + '</span></button>'
  ).join('');
}

function renderRows(d) {
  const list = d.attempts.filter((a) => filter === 'all' || a.result === filter);
  $('rows').innerHTML = list.length ? list.map((a) => {
    const r = RESULT[a.result];
    return '<tr><td class="mono col-n">' + a.n + '</td>' +
      '<td class="mono">' + esc(fmtStamp(a.ts)) + '</td>' +
      '<td><span class="tag t-' + r.tone + '">' + esc(r.label) + '</span></td>' +
      '<td class="mono num col-dur">' + (a.duration == null ? '—' : a.duration + ' s') + '</td>' +
      '<td class="mono num col-pause">' + (a.pause ? fmtDur(a.pause) : '—') + '</td></tr>';
  }).join('') : '<tr class="empty-row"><td colspan="5">Rien à afficher pour ce filtre.</td></tr>';
  const shown = list.length, total = filter === 'all' ? d.total : (d.counts[filter] || 0);
  $('tfoot').textContent = shown < total
    ? shown + ' affichées sur ' + total + ' · ' + d.runs + ' lancement(s) du script'
    : total + ' tentative(s) · ' + d.runs + ' lancement(s) du script';
}

function render() {
  if (!data) return;
  renderHero(data);
  renderSpecs(data);
  renderStrip(data);
  renderSegments(data);
  renderRows(data);
  tick();
}

async function refresh() {
  try {
    const r = await fetch('/api/status', { cache: 'no-store' });
    data = await r.json();
    offset = data.now - Date.now();
    lastOk = Date.now();
    render();
  } catch (e) {
    tick();
  }
}

$('segments').addEventListener('click', (e) => {
  const b = e.target.closest('.seg');
  if (!b) return;
  filter = b.dataset.f;
  renderSegments(data);
  renderRows(data);
});

document.addEventListener('click', async (e) => {
  const b = e.target.closest('[data-copy]');
  if (!b || !data || !data.ip) return;
  const text = b.dataset.copy === 'ip' ? data.ip : 'ssh ubuntu@' + data.ip;
  try {
    await navigator.clipboard.writeText(text);
    const old = b.textContent;
    b.textContent = 'Copié';
    setTimeout(() => { b.textContent = old; }, 1500);
  } catch (err) { /* presse-papiers refusé : on ne fait rien */ }
});

$('btn-start').addEventListener('click', async () => {
  const b = $('btn-start');
  b.disabled = true;
  b.textContent = 'Lancement…';
  try { await fetch('/api/start', { method: 'POST' }); } catch (e) {}
  setTimeout(() => { b.disabled = false; b.textContent = 'Relancer le script'; refresh(); }, 2500);
});

refresh();
setInterval(refresh, 5000);
setInterval(tick, 1000);
</script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def _send(self, code, body=b"", ctype="application/json; charset=utf-8"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            self._send(200, PAGE.encode("utf-8"), "text/html; charset=utf-8")
        elif self.path == "/api/status":
            self._send(200, json.dumps(parse_log(), ensure_ascii=False).encode("utf-8"))
        else:
            self._send(404)

    def do_POST(self):
        if self.path != "/api/start":
            return self._send(404)
        # Refuse les requêtes venues d'un autre site (le serveur écoute en local,
        # mais une page web ouverte ailleurs pourrait tenter de l'appeler).
        if self.headers.get("Origin") not in (ORIGIN, None):
            return self._send(403)
        if _check_script_running():
            return self._send(409, b'{"error":"deja en cours"}')
        start_script()
        self._send(200, b'{"ok":true}')


if __name__ == "__main__":
    threading.Thread(target=_watch_process, daemon=True).start()
    print(f"Tableau de bord sur {ORIGIN}")
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
