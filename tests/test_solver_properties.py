"""Property-based tests for the roster solver using Hypothesis."""

from datetime import date, timedelta

from hypothesis import given, settings
from hypothesis import strategies as st

from staff_duty.calendar_utils import build_holiday_set, get_quarter_days
from staff_duty.config import Directorate, RosterConfig
from staff_duty.solver import solve_joint

# ── Strategies ──────────────────────────────────────────────────────────────

_dir_names = ["G1", "G2", "G3", "G4", "G6", "G8", "ACOS", "DCOS", "COS"]


@st.composite
def roster_configs(draw):
    """Generate a pair of (sdnco_cfg, runner_cfg) with random but valid parameters."""
    start_ordinal = draw(st.integers(
        min_value=date(2026, 1, 1).toordinal(),
        max_value=date(2026, 10, 1).toordinal(),
    ))
    start = date.fromordinal(start_ordinal)

    # Duration: 14-45 days (keep large enough for ILP feasibility)
    duration = draw(st.integers(min_value=14, max_value=45))
    end = start + timedelta(days=duration - 1)

    # 3-5 directorates (need 3+ for cool-down to be reliably feasible)
    n_dirs = draw(st.integers(min_value=3, max_value=5))
    names = _dir_names[:n_dirs]

    sdnco_dirs = []
    runner_dirs = []
    for name in names:
        s_eligible = draw(st.integers(min_value=2, max_value=15))
        r_eligible = draw(st.integers(min_value=2, max_value=15))
        sdnco_dirs.append(Directorate(name, s_eligible))
        runner_dirs.append(Directorate(name, r_eligible))

    sdnco_cfg = RosterConfig("SDNCO", start, end, sdnco_dirs)
    runner_cfg = RosterConfig("SD_Runner", start, end, runner_dirs)
    return sdnco_cfg, runner_cfg


# ── Property tests ──────────────────────────────────────────────────────────

class TestSolverProperties:
    """Property-based solver tests."""

    @given(configs=roster_configs())
    @settings(max_examples=50, deadline=60000)
    def test_full_coverage(self, configs):
        """Every day is assigned for any valid config."""
        sdnco_cfg, runner_cfg = configs
        holiday_dates = build_holiday_set(
            sdnco_cfg.start_date, sdnco_cfg.end_date,
            extra_holidays={sdnco_cfg.start_date},
        )
        all_days = get_quarter_days(sdnco_cfg.start_date, sdnco_cfg.end_date)

        sdnco_sol, runner_sol = solve_joint(
            sdnco_cfg, runner_cfg, all_days, holiday_dates
        )

        assert set(sdnco_sol.assignment.keys()) == set(all_days)
        assert set(runner_sol.assignment.keys()) == set(all_days)

    @given(configs=roster_configs())
    @settings(max_examples=50, deadline=60000)
    def test_cooldown(self, configs):
        """No consecutive assignments when ILP finds optimal solution."""
        sdnco_cfg, runner_cfg = configs
        holiday_dates = build_holiday_set(
            sdnco_cfg.start_date, sdnco_cfg.end_date,
            extra_holidays={sdnco_cfg.start_date},
        )
        all_days = get_quarter_days(sdnco_cfg.start_date, sdnco_cfg.end_date)

        sdnco_sol, runner_sol = solve_joint(
            sdnco_cfg, runner_cfg, all_days, holiday_dates
        )

        # Cool-down is strictly enforced by ILP but not guaranteed by greedy fallback
        for sol in [sdnco_sol, runner_sol]:
            if sol.solver_status == "Fallback":
                continue
            for i in range(len(all_days) - 1):
                assert sol.assignment[all_days[i]] != sol.assignment[all_days[i + 1]]

    @given(configs=roster_configs())
    @settings(max_examples=50, deadline=60000)
    def test_fairness_bound(self, configs):
        """Gini coefficient reasonable for optimal solutions."""
        sdnco_cfg, runner_cfg = configs
        holiday_dates = build_holiday_set(
            sdnco_cfg.start_date, sdnco_cfg.end_date,
            extra_holidays={sdnco_cfg.start_date},
        )
        all_days = get_quarter_days(sdnco_cfg.start_date, sdnco_cfg.end_date)

        sdnco_sol, runner_sol = solve_joint(
            sdnco_cfg, runner_cfg, all_days, holiday_dates
        )

        # Gini should be reasonable; short quarters with skewed eligible can still be > 0.3
        for sol in [sdnco_sol, runner_sol]:
            assert sol.total_day_gini < 0.5, f"{sol.role} Gini {sol.total_day_gini:.3f} >= 0.5"

    @given(configs=roster_configs())
    @settings(max_examples=50, deadline=60000)
    def test_no_same_day_overlap(self, configs):
        """SDNCO and Runner differ on each day (soft constraint)."""
        sdnco_cfg, runner_cfg = configs

        holiday_dates = build_holiday_set(
            sdnco_cfg.start_date, sdnco_cfg.end_date,
            extra_holidays={sdnco_cfg.start_date},
        )
        all_days = get_quarter_days(sdnco_cfg.start_date, sdnco_cfg.end_date)

        sdnco_sol, runner_sol = solve_joint(
            sdnco_cfg, runner_cfg, all_days, holiday_dates
        )

        # Overlap avoidance is only enforced by ILP, not greedy fallback
        if sdnco_sol.solver_status == "Fallback":
            return

        overlaps = sum(
            1 for day in all_days
            if sdnco_sol.assignment[day] == runner_sol.assignment[day]
        )
        # ILP soft-constrains overlap; allow up to 10%
        max_allowed = max(2, len(all_days) // 10)
        assert overlaps <= max_allowed, (
            f"{overlaps} overlaps out of {len(all_days)} days exceeds {max_allowed}"
        )
