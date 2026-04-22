# CLAUDE.md: Staff Duty Roster Solver

ASCC HQ staff duty roster optimization tool. Flask web app + CLI backed by integer linear programming solver.

## Commands

| Command | Description |
|---------|-------------|
| `python -m staff_duty.app` | Run Flask web server (dev, port 5001) |
| `python -m staff_duty.main --help` | CLI entry point |
| `python -m pytest tests/` | Run test suite |
| `fly deploy` | Deploy to Fly.io |

## Architecture

```
staff_duty/
├── app.py             # Flask web interface (all routes + HTML builders)
├── database.py        # SQLite persistence (configs, rosters, swaps, soldiers)
├── solver.py          # ILP constraint solver (PuLP/CBC)
├── export.py          # Excel / HTML export
├── calendar_utils.py  # Holiday and quarter logic
├── config.py          # Dataclass configuration
├── main.py            # Click CLI
├── sample_config.json # Example configuration
├── fly.toml           # Fly.io deployment config
├── Dockerfile         # Production container (gunicorn)
├── tests/             # pytest suite + hypothesis property tests
└── .github/workflows/ # CI pipeline
```

## Routes

| Route | Method | Purpose |
|-------|--------|---------|
| `/` | GET | Form page |
| `/health` | GET | Health check (JSON) |
| `/generate` | GET/POST | Run solver, display results |
| `/export/excel` | GET/POST | Download Excel workbook |
| `/history` | GET | Roster history page |
| `/api/history` | GET | Roster history JSON API |
| `/history/<id>` | GET/DELETE | View or delete a roster |
| `/history/<id>/export/excel` | GET | Re-export historical roster |
| `/configs` | GET/POST | List or save configurations |
| `/configs/<id>` | GET/DELETE | Get or delete a config |
| `/roster/<id>/swap` | POST | Swap duty days |
| `/roster/<id>/resolve` | POST | Re-solve with locked assignments |
| `/roster/<id>/soldiers` | GET/POST | Soldier assignments |
| `/whatif` | POST | What-if comparison |
| `/guide` | GET | User guide |

## Deployment

- **GitHub Pages**: `https://usareur-af-odt.github.io/staff-duty`
- **Fly.io**: `usareur-staff-duty.fly.dev`, Frankfurt region, port 8080
- **Deploy**: `cd staff_duty && fly deploy` or `fly deploy -c staff_duty/fly.toml`
- **Persistent volume**: `/data/staff_duty.db` on Fly.io (volume: `staff_duty_data`)

## Gotchas

- Self-contained: no imports from rest of repo
- SQLite DB path: `/data/staff_duty.db` on Fly.io, `./staff_duty.db` locally. Override with `STAFF_DUTY_DB` env var.
- Gunicorn timeout: 150s to accommodate 120s solver runs
- CORS allowed: `haterandrew.github.io`, `usareur-af-odt.github.io`, localhost

## Required env (Phase 1 hardening)

- `SECRET_KEY`: **required** — Flask sessions; app raises at import if unset. Set in Fly secrets in prod.
- `STAFF_DUTY_ALLOWED_IPS`: **required** — comma-separated CIDR allowlist (e.g. `127.0.0.1/32,10.0.0.0/8`). Unset/empty returns 503 on every non-health request. Enforced against `Fly-Client-IP` header behind the Fly edge.
- `STAFF_DUTY_COOKIE_INSECURE=true`: **dev only** — disables `SESSION_COOKIE_SECURE` so local HTTP dev (`python -m staff_duty.app`) can set session cookies. Never set in prod.
