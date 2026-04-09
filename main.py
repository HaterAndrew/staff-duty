"""
Staff Duty Roster CLI.

Usage examples
──────────────
# Run with a JSON config file (recommended):
  python -m staff_duty.main --config q2_2026.json --output ./output/

# Quick inline run (SDNCO and SD_Runner share the same headcounts here):
  python -m staff_duty.main \
      --start 2026-04-01 --end 2026-06-30 \
      --dir G1:10 --dir G2:5 --dir G3:5 --dir G4:6 --dir G6:8 \
      --output ./output/

JSON config schema:
{
  "start": "2026-04-01",
  "end":   "2026-06-30",
  "sdnco": [
    {"name": "G1", "eligible": 10},
    {"name": "G2", "eligible": 5}
  ],
  "sd_runner": [
    {"name": "G1", "eligible": 8},
    {"name": "G2", "eligible": 4}
  ]
}

If only one set of directorates is provided via --dir, the same list is used
for both SDNCO and SD_Runner roles.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import date, datetime
from pathlib import Path
from typing import List, Optional

import click

from .calendar_utils import build_holiday_set, get_quarter_days
from .config import Directorate, RosterConfig
from .export import write_excel, write_html
from .solver import solve, solve_joint

logging.basicConfig(
    level=logging.INFO,
    format="%(levelname)s  %(message)s",
)
logger = logging.getLogger(__name__)


# ── CLI ───────────────────────────────────────────────────────────────────────

@click.command()
@click.option(
    "--config", "-c",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=None,
    help="Path to JSON config file. Overrides --start / --end / --dir flags.",
)
@click.option("--start",  "-s", default=None, help="Quarter start date (YYYY-MM-DD)")
@click.option("--end",    "-e", default=None, help="Quarter end date   (YYYY-MM-DD)")
@click.option(
    "--dir", "-d", "dir_args",
    multiple=True,
    metavar="NAME:COUNT",
    help="Directorate and eligible count. Repeat for each directorate. "
         "E.g.: --dir G1:10 --dir G2:5",
)
@click.option(
    "--output", "-o",
    type=click.Path(path_type=Path),
    default=Path("."),
    show_default=True,
    help="Output directory for Excel and HTML files.",
)
@click.option(
    "--holiday", "extra_holidays",
    multiple=True,
    metavar="YYYY-MM-DD",
    help="Additional command-directed holiday. Repeat to add multiple.",
)
def main(
    config: Optional[Path],
    start: Optional[str],
    end: Optional[str],
    dir_args: tuple,
    output: Path,
    extra_holidays: tuple,
) -> None:
    """Generate a quarterly staff duty roster with fairness analysis."""

    # ── Load configuration ────────────────────────────────────────────────────
    if config:
        sdnco_cfg, sd_runner_cfg = _load_json_config(config)
    else:
        if not start or not end:
            click.echo("ERROR: --start and --end are required when not using --config.", err=True)
            sys.exit(1)
        if not dir_args:
            click.echo("ERROR: at least one --dir argument is required.", err=True)
            sys.exit(1)
        dirs = _parse_dir_args(dir_args)
        q_start = _parse_date(start)
        q_end   = _parse_date(end)
        sdnco_cfg    = RosterConfig("SDNCO",    q_start, q_end, dirs)
        sd_runner_cfg = RosterConfig("SD_Runner", q_start, q_end, dirs)

    q_start = sdnco_cfg.start_date
    q_end   = sdnco_cfg.end_date

    # ── Build calendar ────────────────────────────────────────────────────────
    extra = {_parse_date(d) for d in extra_holidays} if extra_holidays else None
    holiday_dates = build_holiday_set(
        q_start, q_end,
        extra_holidays=extra,
    )
    all_days = get_quarter_days(q_start, q_end)

    logger.info(
        "Quarter: %s — %s  (%d days, %d holidays/weekends)",
        q_start, q_end, len(all_days),
        sum(1 for d in all_days if d in holiday_dates or d.weekday() >= 5),
    )

    # ── Solve ─────────────────────────────────────────────────────────────────
    click.echo(f"Solving SDNCO + SD_Runner rosters jointly ({sdnco_cfg.n_days} days)…")
    sdnco_sol, sd_runner_sol = solve_joint(sdnco_cfg, sd_runner_cfg, all_days, holiday_dates)

    solutions = [sdnco_sol, sd_runner_sol]

    # ── Export ────────────────────────────────────────────────────────────────
    output.mkdir(parents=True, exist_ok=True)
    quarter_tag = f"{q_start.strftime('%Y%m%d')}_{q_end.strftime('%Y%m%d')}"

    xlsx_path = output / f"staff_duty_{quarter_tag}.xlsx"
    html_path = output / f"staff_duty_{quarter_tag}.html"

    click.echo(f"Writing Excel → {xlsx_path}")
    write_excel(solutions, all_days, holiday_dates, xlsx_path)

    click.echo(f"Writing HTML  → {html_path}")
    write_html(solutions, all_days, holiday_dates, html_path)

    # ── Print summary to terminal ─────────────────────────────────────────────
    click.echo("\n── FAIRNESS SUMMARY ────────────────────────────────")
    for sol in solutions:
        click.echo(f"\n  {sol.role}  (solver: {sol.solver_status})")
        click.echo(f"  {'Directorate':<14} {'Eligible':>8} {'Days':>6} {'Hard':>6}")
        click.echo(f"  {'─'*14} {'─'*8} {'─'*6} {'─'*6}")
        for s in sol.stats:
            click.echo(
                f"  {s.name:<14} {s.eligible:>8} {s.total_days:>6} {s.hard_days:>6}"
            )
        click.echo(
            f"  Gini total={sol.total_day_gini:.4f}  hard={sol.hard_day_gini:.4f}"
        )

    click.echo("\nDone.")


# ── Helpers ───────────────────────────────────────────────────────────────────

def _parse_date(s: str) -> date:
    return datetime.strptime(s, "%Y-%m-%d").date()


def _parse_dir_args(dir_args: tuple) -> List[Directorate]:
    dirs = []
    for arg in dir_args:
        if ":" not in arg:
            raise click.BadParameter(f"Expected NAME:COUNT, got '{arg}'")
        name, count_str = arg.rsplit(":", 1)
        try:
            count = int(count_str)
        except ValueError:
            raise click.BadParameter(f"Count must be an integer, got '{count_str}'")
        dirs.append(Directorate(name=name.strip(), eligible=count))
    return dirs


def _load_json_config(path: Path):
    """Load and validate a JSON config file. Returns (sdnco_cfg, sd_runner_cfg)."""
    raw = json.loads(path.read_text(encoding="utf-8"))

    start = _parse_date(raw["start"])
    end   = _parse_date(raw["end"])

    def parse_dirs(entries: list) -> List[Directorate]:
        return [Directorate(name=e["name"], eligible=int(e["eligible"])) for e in entries]

    if "sdnco" in raw and "sd_runner" in raw:
        sdnco_dirs    = parse_dirs(raw["sdnco"])
        sd_runner_dirs = parse_dirs(raw["sd_runner"])
    elif "directorates" in raw:
        # Single list: same dirs for both roles
        sdnco_dirs = sd_runner_dirs = parse_dirs(raw["directorates"])
    else:
        raise ValueError("JSON config must contain 'sdnco'+'sd_runner' or 'directorates'.")

    return (
        RosterConfig("SDNCO",    start, end, sdnco_dirs),
        RosterConfig("SD_Runner", start, end, sd_runner_dirs),
    )


if __name__ == "__main__":
    main()
