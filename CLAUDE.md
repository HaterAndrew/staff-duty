# CLAUDE.md — Staff Duty Roster Solver

ASCC HQ staff duty roster optimization tool. Flask web app + CLI backed by integer linear programming solver.

## Commands

| Command | Description |
|---------|-------------|
| `python -m staff_duty.main --help` | CLI entry point |
| `python -m staff_duty.app` | Run Flask web server |
| `fly deploy` | Deploy to Fly.io |

## Architecture

```
staff_duty/
├── app.py             # Flask web interface (port 8080)
├── main.py            # Click CLI
├── solver.py          # ILP constraint solver (PuLP)
├── export.py          # Excel / HTML export
├── calendar_utils.py  # Holiday and quarter logic
├── config.py          # Dataclass configuration
├── sample_config.json # Example configuration
└── fly.toml           # Fly.io deployment config
```

## Gotchas

- Self-contained: no imports from rest of repo
- Fly.io: `usareur-staff-duty.fly.dev`, Frankfurt region, port 8080
- Deploy: `cd staff_duty && fly deploy` or `fly deploy -c staff_duty/fly.toml`
