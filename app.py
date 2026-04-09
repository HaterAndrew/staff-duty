"""
Staff Duty Roster — Web Application.

Run:
  python -m staff_duty.app
  # then open  http://localhost:5001

Dependencies (add to requirements.txt):
  flask>=3.0
"""

from __future__ import annotations

import html as _html
import io
import os
import tempfile
from datetime import datetime
from pathlib import Path

from flask import Flask, Response, request

from .calendar_utils import build_holiday_set, get_quarter_days
from .config import Directorate, RosterConfig
from .export import _USAREUR_SVG, write_excel, write_html
from .solver import solve, solve_joint

app = Flask(__name__)

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
            f'          <td><button type="button" class="rm-btn" onclick="removeRow(this)" title="Remove row">&#10005;</button></td>\n'
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
  <form id="roster-form" action="/generate" method="POST">

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
        <button type="button" class="add-btn" onclick="addRow()">+ ADD DIRECTORATE</button>
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
      <button type="submit" class="submit-btn" id="submit-btn">GENERATE ROSTER</button>
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
      `<td><button type="button" class="rm-btn" onclick="removeRow(this)" title="Remove">&#10005;</button></td>`;

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

  // ── Loading state on submit ─────────────────────────────────────────────────
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


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/")
def index() -> Response:
    return Response(_form_page(), mimetype="text/html")


@app.route("/generate", methods=["POST"])
def generate() -> Response:
    sdnco_sol, runner_sol, all_days, holiday_dates, err = _run_solver(request.form)
    if err:
        return Response(err, status=400, mimetype="text/html")

    fd, tmp = tempfile.mkstemp(suffix=".html")
    os.close(fd)
    tmp_path = Path(tmp)
    try:
        write_html([sdnco_sol, runner_sol], all_days, holiday_dates, tmp_path)
        page = tmp_path.read_text(encoding="utf-8")
    finally:
        os.unlink(tmp)

    page = _inject_nav(page, list(request.form.items(multi=True)))
    return Response(page, mimetype="text/html")


@app.route("/export/excel", methods=["POST"])
def export_excel() -> Response:
    """Re-run the solver with the same parameters and stream an .xlsx download."""
    sdnco_sol, runner_sol, all_days, holiday_dates, err = _run_solver(request.form)
    if err:
        return Response(err, status=400, mimetype="text/html")

    fd, tmp = tempfile.mkstemp(suffix=".xlsx")
    os.close(fd)
    tmp_path = Path(tmp)
    try:
        write_excel([sdnco_sol, runner_sol], all_days, holiday_dates, tmp_path)
        data = tmp_path.read_bytes()
    finally:
        os.unlink(tmp)

    q_start = all_days[0]
    q_end   = all_days[-1]
    filename = f"staff_duty_{q_start.strftime('%Y%m%d')}_{q_end.strftime('%Y%m%d')}.xlsx"

    return Response(
        data,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ── Shared solver logic ────────────────────────────────────────────────────────

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

    sdnco_sol, runner_sol = solve_joint(sdnco_cfg, runner_cfg, all_days, holiday_dates)

    return sdnco_sol, runner_sol, all_days, holiday_dates, None


# ── Nav bar injection ─────────────────────────────────────────────────────────

def _inject_nav(page: str, form_items: list[tuple[str, str]]) -> str:
    """
    Prepend a sticky nav bar to the generated dashboard.
    Includes:
      ← Configure New Roster    [Download Excel ↓]
    The Download button re-posts the same form data to /export/excel.
    """
    # Build hidden inputs from original form params (HTML-escaped)
    hidden_inputs = "\n".join(
        f'<input type="hidden" name="{_html.escape(k)}" value="{_html.escape(v)}">'
        for k, v in form_items
    )

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
  #excel-form button {{
    background: {_GOLD}; border: none; border-radius: 4px;
    color: {_NAVY}; cursor: pointer;
    font-size: 0.8rem; font-weight: 800; letter-spacing: 0.1em;
    padding: 0.4rem 1.1rem;
    transition: opacity .15s;
    white-space: nowrap;
  }}
  #excel-form button:hover {{ opacity: 0.85; }}
  #app-nav .nav-r {{ color: rgba(255,255,255,0.35); font-size: 0.7rem; letter-spacing: 0.1em; flex: 1; text-align: center; }}
</style>"""

    nav_bar = (
        '<div id="app-nav">'
        '<a class="nav-back" href="/">&#8592;&nbsp; Configure New Roster</a>'
        '<span class="nav-r">USAREUR-AF &bull; HHBn STAFF DUTY &bull; UNCLASSIFIED</span>'
        f'<form id="excel-form" action="/export/excel" method="POST">'
        f'{hidden_inputs}'
        '<button type="submit">&#11015;&nbsp; Download Excel</button>'
        '</form>'
        '</div>'
    )

    page = page.replace("</head>", nav_css + "\n</head>", 1)
    page = page.replace("<body>", "<body>\n" + nav_bar, 1)
    return page


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5001, debug=False)
