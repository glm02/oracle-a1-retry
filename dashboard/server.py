#!/usr/bin/env python3
"""
Petit serveur local (127.0.0.1 uniquement, par défaut) qui lit le journal du
script de retry et expose un état simple en JSON, + sert une page HTML qui se
rafraîchit toute seule pour suivre les tentatives en direct.

Usage :
  python server.py [chemin_vers_retry_instance.log] [port]

Variables d'environnement (optionnelles) :
  OCI_RETRY_LOG  chemin du fichier journal (défaut: ../retry_instance.log)
  OCI_RETRY_PORT port d'écoute (défaut: 8787)
"""
import json
import os
import re
import sys
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer

DEFAULT_LOG = os.path.join(os.path.dirname(__file__), "..", "retry_instance.log")
LOG_PATH = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("OCI_RETRY_LOG", DEFAULT_LOG)
PORT = int(sys.argv[2]) if len(sys.argv) > 2 else int(os.environ.get("OCI_RETRY_PORT", "8787"))

LINE_RE = re.compile(r"^\[(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\] (?P<msg>.*)$")


def parse_log():
    last_sleep_until = None
    status = "inconnu"
    success_ip = None
    events = []

    try:
        with open(LOG_PATH, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except FileNotFoundError:
        return {"status": "en attente", "events": [], "next_try": None, "ip": None,
                "server_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}

    for raw in lines:
        raw = raw.rstrip("\n")
        m = LINE_RE.match(raw)
        if not m:
            continue  # ligne de détail JSON d'une erreur : ignorée dans la vue condensée
        ts, msg = m.group("ts"), m.group("msg")

        kind = "info"
        if "Tentative #" in msg:
            kind = "tentative"
        elif "SUCCÈS" in msg:
            kind = "succes"
            status = "réussi"
        elif "Pas de capacité" in msg:
            kind = "capacite"
            status = "en attente (capacité saturée)"
        elif "Limitation de débit" in msg:
            kind = "limite"
            status = "en pause (limite de débit)"
        elif "Erreur inattendue" in msg:
            kind = "erreur"
            status = "arrêté sur erreur"
        elif "Arrêt du script" in msg:
            kind = "arret"
        elif "IP PUBLIQUE" in msg:
            success_ip = msg.split(":", 1)[-1].strip()
        elif "Pause de" in msg:
            mm = re.search(r"Pause de (\d+)s", msg)
            if mm:
                dt = datetime.strptime(ts, "%Y-%m-%d %H:%M:%S")
                last_sleep_until = dt + timedelta(seconds=int(mm.group(1)))
                if status not in ("réussi", "arrêté sur erreur"):
                    status = "en pause"

        events.append({"ts": ts, "msg": msg, "kind": kind})

    if status == "inconnu" and events:
        status = "en cours"

    next_try = last_sleep_until.strftime("%Y-%m-%d %H:%M:%S") if last_sleep_until else None

    return {
        "status": status,
        "events": events[-40:][::-1],
        "next_try": next_try,
        "ip": success_ip,
        "server_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


PAGE = """<!DOCTYPE html>
<html lang="fr">
<head>
<meta charset="utf-8">
<title>Oracle A1 Flex — suivi des tentatives</title>
<style>
  body { font-family: -apple-system, Segoe UI, sans-serif; background:#0f1115; color:#e6e6e6; margin:0; padding:24px; }
  h1 { font-size:20px; margin-bottom:4px; }
  .sub { color:#8a8f98; font-size:13px; margin-bottom:20px; }
  .status { display:inline-block; padding:6px 14px; border-radius:20px; font-weight:600; font-size:14px; margin-bottom:20px; }
  .ok { background:#10351d; color:#5ee37a; }
  .wait { background:#3a2f0f; color:#f2c744; }
  .err { background:#3a1414; color:#ff6b6b; }
  .unknown { background:#222; color:#999; }
  table { width:100%; border-collapse:collapse; font-size:13px; }
  td { padding:6px 10px; border-bottom:1px solid #22262e; vertical-align:top; }
  td.ts { color:#8a8f98; white-space:nowrap; font-family:monospace; }
  .tag { display:inline-block; padding:2px 8px; border-radius:10px; font-size:11px; margin-right:6px; }
  .tentative { background:#1f2733; color:#8fb4ff; }
  .capacite { background:#3a2f0f; color:#f2c744; }
  .limite { background:#3a1f0f; color:#ff9f43; }
  .erreur { background:#3a1414; color:#ff6b6b; }
  .succes { background:#10351d; color:#5ee37a; }
  .arret { background:#2a2a2a; color:#aaa; }
  .info { background:#1a1a1a; color:#999; }
  .ip-box { background:#10351d; color:#5ee37a; padding:14px; border-radius:8px; font-size:16px; margin-bottom:20px; font-family:monospace; }
</style>
</head>
<body>
  <h1>Oracle Cloud — création instance A1.Flex</h1>
  <div class="sub">Rafraîchi toutes les 5 secondes · <span id="servertime"></span></div>
  <div id="statusBox" class="status unknown">chargement...</div>
  <div id="ipBox"></div>
  <table id="events"><tbody></tbody></table>

<script>
async function refresh() {
  try {
    const r = await fetch('/api/status');
    const d = await r.json();
    document.getElementById('servertime').textContent = 'maj serveur: ' + d.server_time;

    const sb = document.getElementById('statusBox');
    sb.textContent = d.status;
    sb.className = 'status ' + (
      d.status === 'réussi' ? 'ok' :
      d.status === 'arrêté sur erreur' ? 'err' :
      d.status.startsWith('en') ? 'wait' : 'unknown'
    );

    const ipBox = document.getElementById('ipBox');
    if (d.ip) {
      ipBox.innerHTML = '<div class="ip-box">Instance prête — IP publique : ' + d.ip + '</div>';
    } else if (d.next_try) {
      ipBox.innerHTML = '<div class="sub">Prochaine tentative prévue vers ' + d.next_try + '</div>';
    } else {
      ipBox.innerHTML = '';
    }

    const tbody = document.querySelector('#events tbody');
    tbody.innerHTML = '';
    for (const e of d.events) {
      const tr = document.createElement('tr');
      tr.innerHTML = '<td class="ts">' + e.ts + '</td><td><span class="tag ' + e.kind + '">' + e.kind + '</span>' + e.msg + '</td>';
      tbody.appendChild(tr);
    }
  } catch (e) {
    document.getElementById('statusBox').textContent = 'serveur injoignable';
  }
}
refresh();
setInterval(refresh, 5000);
</script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            body = PAGE.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/api/status":
            data = json.dumps(parse_log(), ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        else:
            self.send_response(404)
            self.end_headers()


if __name__ == "__main__":
    HOST = os.environ.get("OCI_RETRY_HOST", "127.0.0.1")
    print(f"Journal suivi : {os.path.abspath(LOG_PATH)}")
    print(f"Tableau de bord sur http://{HOST}:{PORT}")
    HTTPServer((HOST, PORT), Handler).serve_forever()
