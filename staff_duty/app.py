"""
Staff Duty Roster — Web Application.

Run:
  python -m staff_duty.app
  # then open  http://localhost:5001
"""

from __future__ import annotations

import hashlib
import html as _html
import ipaddress
import json
import logging
import os
import tempfile
import time
from collections import OrderedDict
from datetime import datetime
from pathlib import Path
from urllib.parse import urlencode

from flask import Flask, Response, jsonify, request

from odt.logging import flask_request_hooks, setup_logging

from .calendar_utils import build_holiday_set, get_quarter_days
from .config import Directorate, RosterConfig
from .export import _USAREUR_SVG, write_excel, write_html
from .solver import DirectorateStats, RosterSolution, solve_joint

app = Flask(__name__)
_secret = os.environ.get("SECRET_KEY")
if not _secret:
    raise RuntimeError("SECRET_KEY environment variable must be set")
app.secret_key = _secret

# Session cookie hardening. Secure=True by default; set STAFF_DUTY_COOKIE_INSECURE=true
# for local HTTP dev (e.g., running under `flask run` without TLS).
app.config.update(
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.environ.get("STAFF_DUTY_COOKIE_INSECURE", "").lower() != "true",
    SESSION_COOKIE_HTTPONLY=True,
)

setup_logging("staff_duty", log_dir=os.environ.get("LOG_DIR"))
logger = logging.getLogger("staff_duty")
flask_request_hooks(app)

# ── CORS ──────────────────────────────────────────────────────────────────────
_ALLOWED_ORIGINS = {
    "https://haterandrew.github.io",
    "https://usareur-af-odt.github.io",
    "http://localhost:5001",
    "http://127.0.0.1:5001",
}

@app.after_request
def add_cors(response):
    origin = request.headers.get("Origin", "")
    if origin in _ALLOWED_ORIGINS:
        response.headers["Access-Control-Allow-Origin"] = origin
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS, DELETE"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type"
    return response

# ── Rate limiting ─────────────────────────────────────────────────────────────
# NOTE: The rate-limit store is per-process memory. With gunicorn running 2
# workers (see Dockerfile/fly.toml), a given client may hit each worker
# independently, so the effective cap is ~2x the per-process limit. That is
# acceptable because the IP allowlist is the primary access control; this
# limiter is a secondary DoS guard for accidental over-use from a trusted IP.
# The OrderedDict + _RATE_STORE_MAX cap prevents unbounded memory growth if
# a burst of unique client IPs hits the service.
_RATE_STORE_MAX = 10_000
_rate_store: OrderedDict[str, list[float]] = OrderedDict()

def _client_key() -> str:
    # Behind the Fly edge, request.remote_addr is the proxy's IP, so every real
    # client would share a single bucket. Fly edge sets Fly-Client-IP and strips
    # client-supplied copies of that header, so it is trustworthy as the real
    # client identifier. Locally (no Fly edge), fall back to remote_addr.
    # Do NOT honour X-Forwarded-For (user-controllable upstream of Fly).
    raw = (request.headers.get("Fly-Client-IP") or request.remote_addr or "").strip()
    if not raw:
        # Unknown client: bucket under a shared key so we still apply the limit
        # (fail-safe: would-be abuser gets throttled with anyone else unknown).
        return "unknown"
    try:
        # Validate as IP to prevent weird header values from creating many buckets.
        ipaddress.ip_address(raw)
    except ValueError:
        return "unknown"
    return raw


def _check_rate_limit(limit: int = 5, window: int = 60) -> bool:
    ip = _client_key()
    now = time.time()
    hits = [t for t in _rate_store.get(ip, []) if t > now - window]
    # Re-insert at the tail to mark this key as most-recently-used (LRU).
    if ip in _rate_store:
        _rate_store.move_to_end(ip)
    if len(hits) >= limit:
        _rate_store[ip] = hits
        return True
    hits.append(now)
    _rate_store[ip] = hits
    # Evict oldest entries when the store grows past the cap. Bounded eviction
    # loop prevents a flood of unique IPs from exhausting memory.
    while len(_rate_store) > _RATE_STORE_MAX:
        _rate_store.popitem(last=False)
    return False

# ── Colours (must match dashboard) ────────────────────────────────────────────
_NAVY   = "#0C2340"
_GOLD   = "#C8971A"
_DARK   = "#0a1628"
_CARD   = "#111d30"
_BORDER = "#1e3050"
_TEXT   = "#d0dae8"

# ── Default directorate set (Q3 2026 sample) ──────────────────────────────────
_DEFAULT_DIRS: list[tuple[str, int, int]] = [
    ("G1",    10,  8),
    ("G2",     5,  4),
    ("G3",     5,  4),
    ("G4",     6,  5),
    ("G6",     8,  6),
    ("G8",     4,  3),
    ("ACOS",   3,  2),
]


# ── Form page ─────────────────────────────────────────────────────────────────

def _default_rows_html() -> str:
    rows = []
    for name, sdnco, runner in _DEFAULT_DIRS:
        rows.append(
            f'        <tr class="dir-row">\n'
            f'          <td><input type="text"   name="dir_name"    value="{name}"   required class="inp" placeholder="G9"></td>\n'
            f'          <td><input type="number" name="sdnco_count" value="{sdnco}"  min="1" required class="inp num" oninput="mirrorRunner(this)"></td>\n'
            f'          <td class="runner-col"><input type="number" name="runner_count" value="{runner}" min="1" class="inp num runner-inp"></td>\n'
            f'          <td><a href="#" class="rm-btn" onclick="removeRow(this);return false" title="Remove row">&#10005;</a></td>\n'
            f'        </tr>'
        )
    return "\n".join(rows)


def _form_page() -> str:
    rows = _default_rows_html()
    n_default = len(_DEFAULT_DIRS)

    pre_svg = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>USAREUR-AF &middot; Staff Duty Roster Generator</title>
  <style>
    *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}

    body {{
      background: {_DARK};
      color: {_TEXT};
      font-family: 'Segoe UI', Arial, sans-serif;
      font-size: 0.95rem;
      min-height: 100vh;
    }}

    /* ── Header ── */
    header {{
      background: linear-gradient(135deg, {_NAVY} 0%, #16304d 100%);
      border-bottom: 3px solid {_GOLD};
      padding: 1.25rem 2rem;
      display: flex;
      align-items: center;
      gap: 1.5rem;
    }}
    header svg {{ flex-shrink: 0; }}
    .hdr-text  {{ display: flex; flex-direction: column; gap: 0.2rem; }}
    .hdr-unit  {{ color: {_GOLD}; font-size: 0.72rem; font-weight: 700; letter-spacing: .16em; }}
    .hdr-title {{ color: #fff; font-size: 1.45rem; font-weight: 700; letter-spacing: .04em; }}
    .hdr-sub   {{ color: rgba(200,151,26,.65); font-size: 0.75rem; letter-spacing: .1em; }}

    /* ── Main layout ── */
    main {{ max-width: 820px; margin: 2.5rem auto; padding: 0 1.5rem 4rem; }}

    /* ── Cards ── */
    .card {{
      background: {_CARD};
      border: 1px solid {_BORDER};
      border-radius: 6px;
      margin-bottom: 1.5rem;
      overflow: hidden;
    }}
    .card-hdr {{
      background: {_NAVY};
      border-bottom: 2px solid {_GOLD};
      padding: 0.55rem 1.2rem;
      display: flex;
      align-items: center;
      gap: 0.7rem;
    }}
    .badge {{
      background: {_GOLD};
      color: {_NAVY};
      font-size: 0.68rem;
      font-weight: 800;
      letter-spacing: .12em;
      padding: .15rem .5rem;
      border-radius: 3px;
    }}
    .card-title {{ color: #fff; font-size: 0.88rem; font-weight: 600; letter-spacing: .06em; }}
    .card-body  {{ padding: 1.2rem; }}

    /* ── Inputs ── */
    .inp {{
      background: #0d1d33;
      border: 1px solid {_BORDER};
      border-radius: 4px;
      color: {_TEXT};
      padding: .42rem .6rem;
      font-size: 0.88rem;
      font-family: inherit;
      transition: border-color .15s;
      width: 100%;
    }}
    .inp:focus  {{ outline: none; border-color: {_GOLD}; }}
    .inp.num    {{ width: 90px; text-align: center; }}
    input[type="date"].inp {{ width: 165px; color-scheme: dark; }}

    /* ── Date row ── */
    .date-row   {{ display: flex; gap: 2rem; flex-wrap: wrap; }}
    .date-group {{ display: flex; flex-direction: column; gap: 0.4rem; }}
    .field-lbl  {{ font-size: 0.73rem; color: {_GOLD}; font-weight: 700; letter-spacing: .1em; }}

    /* ── Toggle ── */
    .toggle-row {{ display: flex; align-items: center; gap: .6rem; margin-bottom: .85rem; }}
    .toggle-row label {{ font-size: .85rem; cursor: pointer; user-select: none; }}
    input[type="checkbox"] {{ width: 15px; height: 15px; accent-color: {_GOLD}; cursor: pointer; flex-shrink: 0; }}

    /* ── Directorates table ── */
    .dir-table {{ width: 100%; border-collapse: collapse; margin-top: .5rem; }}
    .dir-table th {{
      text-align: left;
      font-size: .72rem;
      font-weight: 700;
      letter-spacing: .1em;
      color: {_GOLD};
      padding: .4rem .5rem .55rem;
      border-bottom: 1px solid {_BORDER};
    }}
    .dir-table td {{ padding: .3rem .5rem; vertical-align: middle; }}
    .dir-table tr:not(:last-child) td {{ border-bottom: 1px solid rgba(30,48,80,.5); }}

    .rm-btn {{
      background: transparent;
      border: 1px solid rgba(255,80,80,.35);
      color: rgba(255,110,110,.65);
      border-radius: 4px;
      cursor: pointer;
      padding: .25rem .55rem;
      font-size: .8rem;
      text-decoration: none;
      display: inline-block;
      transition: all .15s;
    }}
    .rm-btn:hover {{ background: rgba(255,60,60,.15); color: #ff6060; border-color: #ff5050; }}

    .add-btn {{
      background: transparent;
      border: 1px dashed {_GOLD};
      color: {_GOLD};
      border-radius: 4px;
      cursor: pointer;
      padding: .4rem 1rem;
      font-size: .8rem;
      font-weight: 700;
      letter-spacing: .08em;
      margin-top: .75rem;
      text-decoration: none;
      display: inline-block;
      transition: all .15s;
    }}
    .add-btn:hover {{ background: rgba(200,151,26,.1); }}

    /* ── Options ── */
    .option-row {{ display: flex; align-items: center; gap: .6rem; margin-bottom: .5rem; }}
    .option-row label {{ font-size: .85rem; cursor: pointer; user-select: none; }}
    .option-note {{
      font-size: .75rem;
      color: rgba(200,180,120,.5);
      margin-left: 1.6rem;
      margin-top: -.2rem;
      margin-bottom: .75rem;
    }}
    .extra-hol {{ display: flex; flex-direction: column; gap: .4rem; margin-top: .4rem; }}
    #extra_holidays {{ max-width: 380px; }}

    /* ── Submit ── */
    .submit-wrap {{ text-align: center; margin-top: 2rem; }}
    .submit-btn {{
      background: linear-gradient(135deg, {_GOLD}, #a07515);
      border: none;
      border-radius: 5px;
      color: {_NAVY};
      cursor: pointer;
      font-size: .95rem;
      font-weight: 800;
      letter-spacing: .12em;
      padding: .85rem 3.5rem;
      text-transform: uppercase;
      box-shadow: 0 3px 12px rgba(200,151,26,.3);
      transition: all .2s;
    }}
    .submit-btn:hover    {{ transform: translateY(-1px); box-shadow: 0 6px 20px rgba(200,151,26,.45); }}
    .submit-btn:active   {{ transform: translateY(0); }}
    .submit-btn:disabled {{ opacity: .5; cursor: not-allowed; transform: none; }}

    /* ── Loading overlay ── */
    #loading {{
      display: none;
      position: fixed; inset: 0;
      background: rgba(10,22,40,.88);
      z-index: 999;
      align-items: center;
      justify-content: center;
      flex-direction: column;
      gap: 1rem;
    }}
    #loading.active {{ display: flex; }}
    .spinner {{
      width: 50px; height: 50px;
      border: 4px solid rgba(200,151,26,.2);
      border-top-color: {_GOLD};
      border-radius: 50%;
      animation: spin .85s linear infinite;
    }}
    @keyframes spin {{ to {{ transform: rotate(360deg); }} }}
    .loading-text {{ color: {_GOLD}; font-size: .88rem; font-weight: 700; letter-spacing: .12em; }}

    footer {{
      text-align: center;
      color: rgba(150,160,180,.35);
      font-size: .7rem;
      letter-spacing: .1em;
      padding: 2rem;
      border-top: 1px solid {_BORDER};
      margin-top: 2rem;
    }}
  </style>
</head>
<body>

<div id="loading">
  <div class="spinner"></div>
  <div class="loading-text">SOLVING ROSTER &mdash; PLEASE WAIT&hellip;</div>
</div>

<header>
"""

    post_svg = f"""
  <div class="hdr-text">
    <div class="hdr-unit">UNITED STATES ARMY EUROPE AND AFRICA</div>
    <div class="hdr-title">STAFF DUTY ROSTER GENERATOR</div>
    <div class="hdr-sub">HHBn &nbsp;&middot;&nbsp; PROPORTIONAL FAIRNESS SOLVER &nbsp;&middot;&nbsp; ILP</div>
  </div>
</header>

<main>
  <form id="roster-form" action="/generate" method="GET">

    <!-- QUARTER ---------------------------------------------------------------->
    <div class="card">
      <div class="card-hdr">
        <span class="badge">QUARTER</span>
        <span class="card-title">Date Range</span>
      </div>
      <div class="card-body">
        <div class="date-row">
          <div class="date-group">
            <label class="field-lbl" for="start">START DATE</label>
            <input type="date" id="start" name="start" value="2026-07-01" required class="inp">
          </div>
          <div class="date-group">
            <label class="field-lbl" for="end">END DATE</label>
            <input type="date" id="end" name="end" value="2026-09-30" required class="inp">
          </div>
        </div>
      </div>
    </div>

    <!-- DIRECTORATES ----------------------------------------------------------->
    <div class="card">
      <div class="card-hdr">
        <span class="badge">DIRECTORATES</span>
        <span class="card-title">Eligible Personnel by Role</span>
      </div>
      <div class="card-body">
        <div class="toggle-row">
          <input type="checkbox" id="same_counts" name="same_counts"
                 onchange="toggleSameCounts(this)">
          <label for="same_counts">Use same eligible counts for SDNCO and SD Runner</label>
        </div>
        <table class="dir-table">
          <thead>
            <tr>
              <th style="width:36%">DIRECTORATE</th>
              <th style="width:22%">SDNCO ELIGIBLE</th>
              <th class="runner-col" style="width:22%">SD RUNNER ELIGIBLE</th>
              <th style="width:8%"></th>
            </tr>
          </thead>
          <tbody id="dir-tbody">
{rows}
          </tbody>
        </table>
        <a href="#" class="add-btn" onclick="addRow();return false">+ ADD DIRECTORATE</a>
      </div>
    </div>

    <!-- OPTIONS ---------------------------------------------------------------->
    <div class="card">
      <div class="card-hdr">
        <span class="badge">OPTIONS</span>
        <span class="card-title">Calendar &amp; Holiday Settings</span>
      </div>
      <div class="card-body">
        <div style="background:rgba(200,151,26,.12);border:1px solid rgba(200,151,26,.35);border-radius:4px;padding:.7rem 1rem;margin-bottom:1rem;font-size:.82rem;line-height:1.5;">
          <strong style="color:{_GOLD};">&#9888; MANUAL HOLIDAY INPUT REQUIRED</strong><br>
          US federal holidays and USAREUR-AF bridge days (4-day weekends) are included automatically.
          You <strong>must</strong> manually enter any command-directed training holidays and DONSAs below.
          Refer to the current <strong>AEA Pam 350-1</strong> or your unit training calendar for accurate dates.
        </div>
        <div class="extra-hol">
          <label class="field-lbl" for="extra_holidays">TRAINING HOLIDAYS &amp; DONSAs</label>
          <input type="text" id="extra_holidays" name="extra_holidays" class="inp"
                 placeholder="YYYY-MM-DD, YYYY-MM-DD, &hellip;">
        </div>
      </div>
    </div>

    <div class="submit-wrap">
      <input type="submit" class="submit-btn" id="submit-btn" value="GENERATE ROSTER">
    </div>

  </form>
</main>

<footer>
  HEADQUARTERS, UNITED STATES ARMY EUROPE AND AFRICA &nbsp;&bull;&nbsp;
  HHBn STAFF DUTY ROSTER &nbsp;&bull;&nbsp; UNCLASSIFIED
</footer>

<script>
  // ── Toggle same-counts ──────────────────────────────────────────────────────
  function toggleSameCounts(cb) {{
    document.querySelectorAll('.runner-col').forEach(el => {{
      el.style.display = cb.checked ? 'none' : '';
    }});
    document.querySelectorAll('.runner-inp').forEach(inp => {{
      inp.required = !cb.checked;
    }});
  }}

  // ── Mirror SDNCO value into Runner field (optional convenience) ─────────────
  function mirrorRunner(sdncoInput) {{
    // Only mirror when same_counts is active — otherwise leave independent
    if (!document.getElementById('same_counts').checked) return;
    const row = sdncoInput.closest('tr');
    const ri  = row.querySelector('.runner-inp');
    if (ri) ri.value = sdncoInput.value;
  }}

  // ── Add directorate row ─────────────────────────────────────────────────────
  let _rowIdx = {n_default};
  function addRow() {{
    const tbody     = document.getElementById('dir-tbody');
    const same      = document.getElementById('same_counts').checked;
    const runnerVis = same ? 'none' : '';

    const tr = document.createElement('tr');
    tr.className = 'dir-row';
    tr.innerHTML =
      `<td><input type="text"   name="dir_name"     required class="inp" placeholder="G9"></td>` +
      `<td><input type="number" name="sdnco_count"  value="5" min="1" required class="inp num" oninput="mirrorRunner(this)"></td>` +
      `<td class="runner-col" style="display:${{runnerVis}}">` +
        `<input type="number" name="runner_count" value="4" min="1" ${{same ? '' : 'required'}} class="inp num runner-inp">` +
      `</td>` +
      `<td><a href="#" class="rm-btn" onclick="removeRow(this);return false" title="Remove">&#10005;</a></td>`;

    tbody.appendChild(tr);
    tr.querySelector('input[name="dir_name"]').focus();
    _rowIdx++;
  }}

  // ── Remove directorate row ──────────────────────────────────────────────────
  function removeRow(btn) {{
    const allRows = document.querySelectorAll('#dir-tbody .dir-row');
    if (allRows.length <= 2) {{
      alert('A minimum of 2 directorates is required.');
      return;
    }}
    btn.closest('tr').remove();
  }}

  // ── Validation on submit ────────────────────────────────────────────────────
  document.getElementById('roster-form').addEventListener('submit', function (e) {{
    // Validate: no blank names
    let ok = true;
    document.querySelectorAll('input[name="dir_name"]').forEach(n => {{
      if (!n.value.trim()) ok = false;
    }});
    if (!ok) {{ e.preventDefault(); alert('All directorate names must be filled in.'); return; }}

    // Validate: end >= start
    const s = new Date(document.getElementById('start').value);
    const en = new Date(document.getElementById('end').value);
    if (en <= s) {{ e.preventDefault(); alert('End date must be after start date.'); return; }}

    document.getElementById('loading').classList.add('active');
    document.getElementById('submit-btn').disabled = true;
  }});
</script>

</body>
</html>"""

    return pre_svg + _USAREUR_SVG + post_svg


# ── IP allowlist (Fly edge enforcement) ───────────────────────────────────────
# Fly.io's edge sets the `Fly-Client-IP` header and strips any client-supplied
# copies, so we can trust it. Locally (no Fly edge), fall back to remote_addr.
# The allowlist is driven by STAFF_DUTY_ALLOWED_IPS (comma-separated CIDRs).
# If unset/empty, the app fails closed with 503.

def _parse_allowed_cidrs(raw: str) -> list[ipaddress._BaseNetwork]:
    nets: list[ipaddress._BaseNetwork] = []
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        try:
            nets.append(ipaddress.ip_network(item, strict=False))
        except ValueError:
            logger.warning("invalid cidr in STAFF_DUTY_ALLOWED_IPS: %s", item)
    return nets


@app.before_request
def _ip_allowlist_gate():
    # Exempt health checks so Fly's platform probes continue to work.
    if request.path == "/health":
        return None

    raw = os.environ.get("STAFF_DUTY_ALLOWED_IPS", "").strip()
    if not raw:
        return Response(
            "Service Unavailable: allowlist not configured. "
            "Set STAFF_DUTY_ALLOWED_IPS.",
            status=503,
            mimetype="text/plain",
        )

    networks = _parse_allowed_cidrs(raw)
    if not networks:
        return Response(
            "Service Unavailable: allowlist not configured. "
            "Set STAFF_DUTY_ALLOWED_IPS.",
            status=503,
            mimetype="text/plain",
        )

    # Prefer Fly-Client-IP (trusted, set by Fly edge); fall back for local dev.
    client_ip_raw = request.headers.get("Fly-Client-IP") or request.remote_addr or ""
    client_ip_raw = client_ip_raw.strip()
    try:
        client_ip = ipaddress.ip_address(client_ip_raw)
    except ValueError:
        logger.warning("deny ip=%s path=%s", client_ip_raw, request.path)
        return Response("Forbidden", status=403, mimetype="text/plain")

    for net in networks:
        if client_ip in net:
            return None

    logger.warning("deny ip=%s path=%s", client_ip_raw, request.path)
    return Response("Forbidden", status=403, mimetype="text/plain")


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/health")
def health():
    # Minimal liveness probe. Do not disclose version/build/uptime to
    # unauthenticated callers — Fly's platform probe and external monitoring
    # only need the 200 status; richer introspection belongs behind the
    # IP allowlist on authenticated routes.
    return jsonify({"ok": True})


@app.route("/")
def index() -> Response:
    return Response(_form_page(), mimetype="text/html")


@app.route("/generate", methods=["GET", "POST"])
def generate() -> Response:
    if _check_rate_limit():
        return jsonify({"error": "Too many requests."}), 429
    params = request.args if request.method == "GET" else request.form
    sdnco_sol, runner_sol, all_days, holiday_dates, err = _run_solver(params)
    if err:
        return Response(err, status=400, mimetype="text/html")

    _save_roster_to_db(sdnco_sol, runner_sol, all_days, holiday_dates, params)

    # TemporaryDirectory guarantees cleanup even if write_html raises, so we
    # don't leak /tmp files on solver output failures.
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir) / "roster.html"
        write_html([sdnco_sol, runner_sol], all_days, holiday_dates, tmp_path)
        page = tmp_path.read_text(encoding="utf-8")

    page = _inject_nav(page, list(params.items(multi=True)))
    return Response(page, mimetype="text/html")


@app.route("/export/excel", methods=["GET", "POST"])
def export_excel() -> Response:
    """Re-run the solver with the same parameters and stream an .xlsx download."""
    params = request.args if request.method == "GET" else request.form
    sdnco_sol, runner_sol, all_days, holiday_dates, err = _run_solver(params)
    if err:
        return Response(err, status=400, mimetype="text/html")

    # TemporaryDirectory guarantees cleanup even if write_excel raises.
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir) / "roster.xlsx"
        write_excel([sdnco_sol, runner_sol], all_days, holiday_dates, tmp_path)
        data = tmp_path.read_bytes()

    q_start = all_days[0]
    q_end   = all_days[-1]
    filename = f"staff_duty_{q_start.strftime('%Y%m%d')}_{q_end.strftime('%Y%m%d')}.xlsx"

    return Response(
        data,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ── Config CRUD ────────────────────────────────────────────────────────────────

@app.route("/configs", methods=["GET"])
def list_configs_route():
    from .database import list_configs
    return jsonify(list_configs())

@app.route("/configs", methods=["POST"])
def save_config_route():
    if _check_rate_limit():
        return jsonify({"error": "Too many requests."}), 429
    data = request.get_json(silent=True) or {}
    name = data.get("name", "").strip()
    config = data.get("config")
    if not name or not config:
        return jsonify({"error": "name and config are required"}), 400
    from .database import save_config
    return jsonify({"id": save_config(name, config)}), 201

@app.route("/configs/<config_id>", methods=["GET"])
def get_config_route(config_id):
    from .database import get_config
    cfg = get_config(config_id)
    return jsonify(cfg) if cfg else (jsonify({"error": "Not found"}), 404)

@app.route("/configs/<config_id>", methods=["DELETE"])
def delete_config_route(config_id):
    from .database import delete_config
    return jsonify({"ok": True}) if delete_config(config_id) else (jsonify({"error": "Not found"}), 404)


# ── History ────────────────────────────────────────────────────────────────────

@app.route("/history")
def history_page():
    return Response("<html><body><h1>Roster History</h1><p>Coming soon.</p><a href='/'>Back</a></body></html>", mimetype="text/html")

@app.route("/api/history")
def list_rosters_route():
    # Clamp to [1, 10000] to prevent unbounded OFFSET; default to 1 on bad input
    # rather than leaking a 500 / stack trace.
    try:
        page = max(1, min(int(request.args.get("page", 1)), 10000))
    except (TypeError, ValueError):
        page = 1
    from .database import list_rosters
    rosters, total = list_rosters(page=page)
    return jsonify({"rosters": rosters, "total": total, "page": page})

@app.route("/history/<roster_id>")
def view_roster(roster_id):
    from .database import get_roster
    roster = get_roster(roster_id)
    if not roster:
        return Response("<h2>Not found</h2>", status=404, mimetype="text/html")
    roster_data = roster["roster"]
    sdnco_sol, runner_sol, all_days, holiday_dates = _reconstruct_from_stored(roster_data)
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir) / "roster.html"
        write_html([sdnco_sol, runner_sol], all_days, holiday_dates, tmp_path)
        page = tmp_path.read_text(encoding="utf-8")
    page = _inject_nav(page, [])
    return Response(page, mimetype="text/html")

@app.route("/history/<roster_id>/export/excel")
def export_roster_excel(roster_id):
    from .database import get_roster
    roster = get_roster(roster_id)
    if not roster:
        return jsonify({"error": "Not found"}), 404
    roster_data = roster["roster"]
    sdnco_sol, runner_sol, all_days, holiday_dates = _reconstruct_from_stored(roster_data)
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir) / "roster.xlsx"
        write_excel([sdnco_sol, runner_sol], all_days, holiday_dates, tmp_path)
        data = tmp_path.read_bytes()
    filename = f"staff_duty_{all_days[0].strftime('%Y%m%d')}_{all_days[-1].strftime('%Y%m%d')}.xlsx"
    return Response(data, mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": f'attachment; filename="{filename}"'})

@app.route("/history/<roster_id>", methods=["DELETE"])
def delete_roster_route(roster_id):
    from .database import delete_roster
    return jsonify({"ok": True}) if delete_roster(roster_id) else (jsonify({"error": "Not found"}), 404)


# ── Swap ──────────────────────────────────────────────────────────────────────

def _roster_etag(roster_data: dict) -> str:
    """Content-addressed ETag for a roster payload.

    Hashes the canonical JSON encoding of the stored roster so two clients
    viewing the same persisted state see the same tag. Any mutation (swap,
    re-solve) changes the tag, so a stale client write gets a 412/409 on
    mismatch instead of silently clobbering a concurrent update.
    """
    canonical = json.dumps(roster_data, sort_keys=True).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


@app.route("/roster/<roster_id>/swap", methods=["POST"])
def swap_duty(roster_id):
    from .database import get_db, get_roster
    roster = get_roster(roster_id)
    if not roster:
        return jsonify({"error": "Not found"}), 404
    data = request.get_json(silent=True) or {}
    day1, day2 = data.get("day1"), data.get("day2")
    role = data.get("role", "SDNCO")
    if not day1 or not day2:
        return jsonify({"error": "day1 and day2 required"}), 400
    roster_data = roster["roster"]

    # Optimistic concurrency control: the client must echo the ETag it last
    # saw (either via the If-Match header, standard HTTP style, or an
    # `if_match` field in the JSON body for clients that can't set headers).
    # If no tag is supplied, the request is rejected rather than silently
    # accepted — this closes the lost-update window entirely for callers
    # that want safety, while leaving a path for trusted automation to
    # explicitly opt out by fetching the current tag first.
    current_etag = _roster_etag(roster_data)
    client_etag = (
        request.headers.get("If-Match", "").strip().strip('"')
        or str(data.get("if_match", "")).strip()
    )
    if not client_etag:
        return (
            jsonify({
                "error": "If-Match required",
                "etag": current_etag,
            }),
            428,  # Precondition Required
        )
    if client_etag != current_etag:
        return (
            jsonify({
                "error": "Roster was modified by another request",
                "etag": current_etag,
            }),
            409,  # Conflict
        )

    role_key = "sdnco" if role == "SDNCO" else "runner"
    assignments = roster_data.get(role_key, {})
    if day1 not in assignments or day2 not in assignments:
        return jsonify({"error": "Days not in roster"}), 400
    assignments[day1], assignments[day2] = assignments[day2], assignments[day1]
    roster_data[role_key] = assignments
    swaps = roster.get("swaps") or []
    swaps.append({"day1": day1, "day2": day2, "role": role, "reason": data.get("reason", "")})
    conn = get_db()
    conn.execute("UPDATE rosters SET roster_json=?, swaps_json=? WHERE id=?",
                 (json.dumps(roster_data), json.dumps(swaps), roster_id))
    conn.commit()
    new_etag = _roster_etag(roster_data)
    return jsonify({"valid": True, "swaps": swaps, "etag": new_etag})


# ── Lock & re-solve ──────────────────────────────────────────────────────────

@app.route("/roster/<roster_id>/resolve", methods=["POST"])
def resolve_with_locks(roster_id):
    from .database import get_db, get_roster
    roster = get_roster(roster_id)
    if not roster:
        return jsonify({"error": "Not found"}), 404
    data = request.get_json(silent=True) or {}
    locked = data.get("locked", {})
    config_data = roster["config"]
    try:
        start = datetime.strptime(config_data["start"], "%Y-%m-%d").date()
        end = datetime.strptime(config_data["end"], "%Y-%m-%d").date()
    except (KeyError, ValueError):
        return jsonify({"error": "Bad config"}), 500
    sdnco_dirs = [Directorate(d["name"], d["eligible"]) for d in config_data.get("sdnco", [])]
    runner_dirs = [Directorate(d["name"], d["eligible"]) for d in config_data.get("sd_runner", [])]
    if not sdnco_dirs or not runner_dirs:
        return jsonify({"error": "Bad config"}), 500
    holiday_dates = build_holiday_set(start, end)
    all_days = get_quarter_days(start, end)
    sdnco_sol, runner_sol = solve_joint(
        RosterConfig("SDNCO", start, end, sdnco_dirs),
        RosterConfig("SD_Runner", start, end, runner_dirs), all_days, holiday_dates)
    locked_count = 0
    for lock_key, lock_dir in locked.items():
        parts = lock_key.rsplit("_", 1)
        if len(parts) != 2:
            continue
        try:
            lock_day = datetime.strptime(parts[0], "%Y-%m-%d").date()
        except ValueError:
            continue
        if parts[1] == "SDNCO" and lock_day in sdnco_sol.assignment:
            sdnco_sol.assignment[lock_day] = lock_dir
            locked_count += 1
        elif lock_day in runner_sol.assignment:
            runner_sol.assignment[lock_day] = lock_dir
            locked_count += 1
    new_data = _serialize_roster(sdnco_sol, runner_sol, all_days, holiday_dates)
    conn = get_db()
    conn.execute("UPDATE rosters SET roster_json=?, locked_json=? WHERE id=?",
                 (json.dumps(new_data), json.dumps(locked), roster_id))
    conn.commit()
    return jsonify({"locked_count": locked_count, "gini_sdnco": sdnco_sol.total_day_gini})


# ── Soldiers ──────────────────────────────────────────────────────────────────

@app.route("/roster/<roster_id>/soldiers", methods=["GET"])
def get_soldiers(roster_id):
    from .database import get_roster
    roster = get_roster(roster_id)
    if not roster:
        return jsonify({"error": "Not found"}), 404
    return jsonify(roster.get("soldier_assignments") or [])

@app.route("/roster/<roster_id>/soldiers", methods=["POST"])
def update_soldiers(roster_id):
    from .database import get_roster, update_roster_soldiers
    roster = get_roster(roster_id)
    if not roster:
        return jsonify({"error": "Not found"}), 404
    data = request.get_json(silent=True) or []
    update_roster_soldiers(roster_id, data)
    return jsonify({"ok": True, "count": len(data)})


# ── What-if ───────────────────────────────────────────────────────────────────

@app.route("/whatif", methods=["POST"])
def whatif():
    if _check_rate_limit():
        return jsonify({"error": "Too many requests."}), 429
    data = request.get_json(silent=True) or {}
    try:
        start = datetime.strptime(data["start"], "%Y-%m-%d").date()
        end = datetime.strptime(data["end"], "%Y-%m-%d").date()
    except (KeyError, ValueError):
        return jsonify({"error": "Invalid dates"}), 400
    sdnco_dirs = [Directorate(d["name"], d["eligible"]) for d in data.get("sdnco", [])]
    runner_dirs = [Directorate(d["name"], d["eligible"]) for d in data.get("sd_runner", [])]
    if len(sdnco_dirs) < 2:
        return jsonify({"error": "Need 2+ directorates"}), 400

    # DoS caps — use the larger of the two role lists to decide the directorate cap.
    max_dirs_for_cap = sdnco_dirs if len(sdnco_dirs) >= len(runner_dirs) else runner_dirs
    raw_extra = data.get("extra_holidays", "") or ""
    if isinstance(raw_extra, list):
        extra_list = [str(x).strip() for x in raw_extra if str(x).strip()]
    else:
        extra_list = [p for p in (p.strip() for p in str(raw_extra).split(",")) if p]
    cap_err = _validate_solver_inputs(max_dirs_for_cap, start, end, extra_list)
    if cap_err:
        return jsonify({"error": cap_err}), 400

    holiday_dates = build_holiday_set(start, end)
    all_days = get_quarter_days(start, end)
    t0 = time.time()
    sdnco_sol, runner_sol = solve_joint(
        RosterConfig("SDNCO", start, end, sdnco_dirs),
        RosterConfig("SD_Runner", start, end, runner_dirs), all_days, holiday_dates)
    return jsonify({
        "gini_sdnco": round(sdnco_sol.total_day_gini, 4),
        "gini_runner": round(runner_sol.total_day_gini, 4),
        "solver_status": sdnco_sol.solver_status,
        "duration_seconds": round(time.time() - t0, 2),
    })


# ── Guide ─────────────────────────────────────────────────────────────────────

@app.route("/guide")
def guide():
    return Response("<html><body><h1>User Guide</h1><p>See CLAUDE.md for docs.</p><a href='/'>Back</a></body></html>", mimetype="text/html")


# ── Shared solver logic ────────────────────────────────────────────────────────

# DoS caps applied at the Flask boundary — the ILP solver's runtime grows
# non-linearly in (directorates * days), so we reject inputs that could let a
# single request burn the gunicorn 150s budget or wedge a worker.
_MAX_DIRECTORATES = 20
_MAX_DAYS = 180
_MAX_EXTRA_HOLIDAYS = 60


def _validate_solver_inputs(
    directorates: list,
    start_date,
    end_date,
    extra_holidays_list: list,
) -> str | None:
    """Return an error message if any cap is breached, else None.

    Caps:
      - directorates: up to 20
      - date range:   up to 180 days (start -> end, inclusive of either bound)
      - extra_holidays: up to 60 entries
    """
    if len(directorates) > _MAX_DIRECTORATES:
        return (
            f"Too many directorates: {len(directorates)} "
            f"(max {_MAX_DIRECTORATES})."
        )
    try:
        span_days = (end_date - start_date).days
    except Exception:
        return "Invalid date range."
    if span_days > _MAX_DAYS:
        return (
            f"Date range too long: {span_days} days "
            f"(max {_MAX_DAYS})."
        )
    if len(extra_holidays_list) > _MAX_EXTRA_HOLIDAYS:
        return (
            f"Too many extra holidays: {len(extra_holidays_list)} "
            f"(max {_MAX_EXTRA_HOLIDAYS})."
        )
    return None


def _run_solver(form):
    """Parse form, run ILP solver, return (sdnco_sol, runner_sol, all_days, holidays, err_html).
    On error, the first four values are None and err_html is a string."""
    try:
        start = datetime.strptime(form["start"], "%Y-%m-%d").date()
        end   = datetime.strptime(form["end"],   "%Y-%m-%d").date()
    except (KeyError, ValueError) as exc:
        return None, None, None, None, f"<h2>Bad date input: {exc}</h2>"

    if end <= start:
        return None, None, None, None, "<h2>End date must be after start date.</h2>"

    names         = form.getlist("dir_name")
    sdnco_counts  = form.getlist("sdnco_count")
    runner_counts = form.getlist("runner_count")
    same_counts   = "same_counts" in form

    # Pre-solver DoS caps (input volume). Filter blanks to match the parse loop.
    non_blank_names = [n for n in names if n.strip()]
    raw_extra = form.get("extra_holidays", "") or ""
    extra_list = [p for p in (p.strip() for p in raw_extra.split(",")) if p]
    cap_err = _validate_solver_inputs(non_blank_names, start, end, extra_list)
    if cap_err:
        return None, None, None, None, f"<h2>{_html.escape(cap_err)}</h2>"

    sdnco_dirs:  list[Directorate] = []
    runner_dirs: list[Directorate] = []

    for i, name in enumerate(names):
        name = name.strip()
        if not name:
            continue
        try:
            sc = max(1, int(sdnco_counts[i]))
        except (IndexError, ValueError):
            sc = 1
        if same_counts:
            rc = sc
        else:
            try:
                rc = max(1, int(runner_counts[i]))
            except (IndexError, ValueError):
                rc = sc
        sdnco_dirs.append(Directorate(name=name, eligible=sc))
        runner_dirs.append(Directorate(name=name, eligible=rc))

    if len(sdnco_dirs) < 2:
        return None, None, None, None, "<h2>At least 2 directorates are required.</h2>"

    extra: set = set()
    for ds in form.get("extra_holidays", "").split(","):
        ds = ds.strip()
        if ds:
            try:
                extra.add(datetime.strptime(ds, "%Y-%m-%d").date())
            except ValueError:
                pass

    sdnco_cfg  = RosterConfig("SDNCO",     start, end, sdnco_dirs)
    runner_cfg = RosterConfig("SD_Runner", start, end, runner_dirs)

    holiday_dates = build_holiday_set(
        start, end, extra_holidays=extra or None,
    )
    all_days = get_quarter_days(start, end)

    t0 = time.time()
    try:
        sdnco_sol, runner_sol = solve_joint(sdnco_cfg, runner_cfg, all_days, holiday_dates)
    except Exception as exc:
        logger.error("Solver failed: %s", exc)
        return None, None, None, None, (
            "<h2>Solver error</h2><p>Try widening the date range or reducing directorates.</p>"
            f"<p>{_html.escape(str(exc))}</p>"
        )
    logger.info("Solver: status=%s gini_s=%.4f gini_r=%.4f dur=%.2fs",
                sdnco_sol.solver_status, sdnco_sol.total_day_gini,
                runner_sol.total_day_gini, time.time() - t0)

    return sdnco_sol, runner_sol, all_days, holiday_dates, None


# ── Serialization helpers ─────────────────────────────────────────────────────

def _serialize_roster(sdnco_sol, runner_sol, all_days, holiday_dates) -> dict:
    return {
        "sdnco": {d.isoformat(): dn for d, dn in sdnco_sol.assignment.items()},
        "runner": {d.isoformat(): dn for d, dn in runner_sol.assignment.items()},
        "stats": {
            "sdnco": [{"name": s.name, "eligible": s.eligible, "total_days": s.total_days,
                        "weekday_days": s.weekday_days, "weekend_days": s.weekend_days,
                        "holiday_days": s.holiday_days} for s in sdnco_sol.stats],
            "runner": [{"name": s.name, "eligible": s.eligible, "total_days": s.total_days,
                        "weekday_days": s.weekday_days, "weekend_days": s.weekend_days,
                        "holiday_days": s.holiday_days} for s in runner_sol.stats],
        },
        "gini": {"sdnco_total": sdnco_sol.total_day_gini, "sdnco_hard": sdnco_sol.hard_day_gini,
                 "runner_total": runner_sol.total_day_gini, "runner_hard": runner_sol.hard_day_gini},
        "solver_status": sdnco_sol.solver_status,
        "start": all_days[0].isoformat(), "end": all_days[-1].isoformat(),
        "holidays": [d.isoformat() for d in sorted(holiday_dates)],
    }


def _reconstruct_from_stored(roster_data):
    start = datetime.strptime(roster_data["start"], "%Y-%m-%d").date()
    end = datetime.strptime(roster_data["end"], "%Y-%m-%d").date()
    holiday_dates = {datetime.strptime(d, "%Y-%m-%d").date() for d in roster_data.get("holidays", [])}
    all_days = get_quarter_days(start, end)
    solutions = []
    for role_key, role_name in [("sdnco", "SDNCO"), ("runner", "SD_Runner")]:
        assignment = {datetime.strptime(d, "%Y-%m-%d").date(): dn for d, dn in roster_data.get(role_key, {}).items()}
        stats = [DirectorateStats(name=s["name"], eligible=s["eligible"], total_days=s["total_days"],
                                  weekday_days=s["weekday_days"], weekend_days=s["weekend_days"],
                                  holiday_days=s["holiday_days"])
                 for s in roster_data.get("stats", {}).get(role_key, [])]
        gini = roster_data.get("gini", {})
        solutions.append(RosterSolution(role=role_name, assignment=assignment, stats=stats,
                                        solver_status=roster_data.get("solver_status", "Stored"),
                                        total_day_gini=gini.get(f"{role_key}_total", 0.0),
                                        hard_day_gini=gini.get(f"{role_key}_hard", 0.0)))
    return solutions[0], solutions[1], all_days, holiday_dates


def _save_roster_to_db(sdnco_sol, runner_sol, all_days, holiday_dates, params) -> str | None:
    try:
        from .database import save_roster
        roster_data = _serialize_roster(sdnco_sol, runner_sol, all_days, holiday_dates)
        names = params.getlist("dir_name")
        sdnco_counts = params.getlist("sdnco_count")
        runner_counts = params.getlist("runner_count")
        same = "same_counts" in params
        sdnco, sd_runner = [], []
        for i, n in enumerate(names):
            n = n.strip()
            if not n:
                continue
            sc = max(1, int(sdnco_counts[i])) if i < len(sdnco_counts) else 1
            rc = sc if same else (max(1, int(runner_counts[i])) if i < len(runner_counts) else sc)
            sdnco.append({"name": n, "eligible": sc})
            sd_runner.append({"name": n, "eligible": rc})
        config_data = {"start": params.get("start", ""), "end": params.get("end", ""),
                       "sdnco": sdnco, "sd_runner": sd_runner,
                       "extra_holidays": params.get("extra_holidays", "")}
        q_start = all_days[0]
        month_to_q = {10:1,11:1,12:1,1:2,2:2,3:2,4:3,5:3,6:3,7:4,8:4,9:4}
        quarter = f"FY{q_start.year}-Q{month_to_q.get(q_start.month, 0)}"
        return save_roster(config_id=None, quarter=quarter, solver_status=sdnco_sol.solver_status,
                           gini_sdnco=sdnco_sol.total_day_gini, gini_runner=runner_sol.total_day_gini,
                           roster_json=roster_data, config_json=config_data)
    except Exception as exc:
        logger.warning("DB save failed: %s", exc)
        return None


# ── Nav bar injection ─────────────────────────────────────────────────────────

def _inject_nav(page: str, form_items: list[tuple[str, str]]) -> str:
    """
    Prepend a sticky nav bar to the generated dashboard.
    Includes:
      ← Configure New Roster    [Download Excel ↓]
    The Download button re-posts the same form data to /export/excel.
    """
    nav_css = f"""<style>
  #app-nav {{
    position: sticky; top: 0; z-index: 9000;
    background: {_NAVY}; border-bottom: 2px solid {_GOLD};
    padding: 0.5rem 1.5rem;
    display: flex; align-items: center; justify-content: space-between;
    font-family: 'Segoe UI', Arial, sans-serif; font-size: 0.82rem;
    gap: 1rem;
  }}
  #app-nav a.nav-back {{
    color: {_GOLD}; text-decoration: none; font-weight: 700; letter-spacing: 0.06em;
    white-space: nowrap;
  }}
  #app-nav a.nav-back:hover {{ text-decoration: underline; }}
  #app-nav .excel-link {{
    background: {_GOLD}; border: none; border-radius: 4px;
    color: {_NAVY}; text-decoration: none;
    font-size: 0.8rem; font-weight: 800; letter-spacing: 0.1em;
    padding: 0.4rem 1.1rem;
    transition: opacity .15s;
    white-space: nowrap;
    display: inline-block;
  }}
  #app-nav .excel-link:hover {{ opacity: 0.85; }}
  #app-nav .nav-r {{ color: rgba(255,255,255,0.35); font-size: 0.7rem; letter-spacing: 0.1em; flex: 1; text-align: center; }}
</style>"""

    # Build query string for GET-based Excel download link
    excel_qs = urlencode(form_items)

    nav_bar = (
        '<div id="app-nav">'
        '<a class="nav-back" href="/">&#8592;&nbsp; Configure New Roster</a>'
        '<span class="nav-r">USAREUR-AF &bull; HHBn STAFF DUTY &bull; UNCLASSIFIED</span>'
        f'<a class="excel-link" href="/export/excel?{_html.escape(excel_qs)}">&#11015;&nbsp; Download Excel</a>'
        '</div>'
    )

    page = page.replace("</head>", nav_css + "\n</head>", 1)
    page = page.replace("<body>", "<body>\n" + nav_bar, 1)
    return page


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5001, debug=False)
