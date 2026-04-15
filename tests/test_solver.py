"""Tests for the ILP roster solver."""


from staff_duty.calendar_utils import build_holiday_set, get_quarter_days
from staff_duty.config import Directorate


class TestSolverConstraints:
    """Verify solver output satisfies core constraints."""

    def test_full_coverage(self, solved_roster):
        """Every day has exactly one assignment per role."""
        sdnco_sol, runner_sol, all_days, _ = solved_roster
        assert set(sdnco_sol.assignment.keys()) == set(all_days)
        assert set(runner_sol.assignment.keys()) == set(all_days)

    def test_cooldown(self, solved_roster):
        """No directorate assigned consecutive days for either role."""
        sdnco_sol, runner_sol, all_days, _ = solved_roster
        for sol in [sdnco_sol, runner_sol]:
            for i in range(len(all_days) - 1):
                assert sol.assignment[all_days[i]] != sol.assignment[all_days[i + 1]], (
                    f"{sol.role}: {sol.assignment[all_days[i]]} assigned consecutive days "
                    f"{all_days[i]} and {all_days[i + 1]}"
                )

    def test_quota_bounds(self, solved_roster):
        """Each directorate's total days within floor/ceil of proportional quota."""
        import math

        sdnco_sol, runner_sol, all_days, _ = solved_roster
        n_days = len(all_days)

        for sol, cfg_dirs in [
            (sdnco_sol, [Directorate("G1", 6), Directorate("G2", 4), Directorate("G3", 3)]),
            (runner_sol, [Directorate("G1", 6), Directorate("G2", 4), Directorate("G3", 3)]),
        ]:
            total_eligible = sum(d.eligible for d in cfg_dirs)
            for d in cfg_dirs:
                expected_q = d.eligible / total_eligible * n_days
                lo = math.floor(expected_q)
                hi = math.ceil(expected_q)
                actual = sum(1 for v in sol.assignment.values() if v == d.name)
                assert lo <= actual <= hi, (
                    f"{sol.role}/{d.name}: expected [{lo}, {hi}], got {actual}"
                )

    def test_gini_reasonable(self, solved_roster):
        """Gini coefficient < 0.25 for both roles."""
        sdnco_sol, runner_sol, _, _ = solved_roster
        assert sdnco_sol.total_day_gini < 0.25, (
            f"SDNCO Gini {sdnco_sol.total_day_gini:.3f} >= 0.25"
        )
        assert runner_sol.total_day_gini < 0.25, (
            f"Runner Gini {runner_sol.total_day_gini:.3f} >= 0.25"
        )

    def test_no_same_day_overlap(self, solved_roster):
        """SDNCO and SD_Runner assignments differ on each day."""
        sdnco_sol, runner_sol, all_days, _ = solved_roster
        for day in all_days:
            # Overlap is soft-constrained (penalized), so just check it's mostly avoided
            pass
        # Count overlaps -- should be very few for a 14-day range with 3 dirs
        overlaps = sum(
            1 for day in all_days
            if sdnco_sol.assignment[day] == runner_sol.assignment[day]
        )
        assert overlaps == 0, f"Found {overlaps} same-day overlaps"


class TestGreedyFallback:
    """Verify the greedy fallback produces a valid roster when ILP fails."""

    def test_greedy_fallback_works(self, sample_config):
        """Call greedy fallback directly, verify it produces a valid roster."""
        sdnco_cfg, runner_cfg = sample_config
        holiday_dates = build_holiday_set(sdnco_cfg.start_date, sdnco_cfg.end_date)
        all_days = get_quarter_days(sdnco_cfg.start_date, sdnco_cfg.end_date)

        from staff_duty.solver import _greedy_joint_fallback

        sdnco_sol, runner_sol = _greedy_joint_fallback(
            sdnco_cfg, runner_cfg, all_days, holiday_dates
        )

        # Verify full coverage
        assert set(sdnco_sol.assignment.keys()) == set(all_days)
        assert set(runner_sol.assignment.keys()) == set(all_days)

        # Verify fallback status
        assert sdnco_sol.solver_status == "Fallback"
        assert runner_sol.solver_status == "Fallback"

        # Verify all assigned directorates are valid
        valid_names = {d.name for d in sdnco_cfg.directorates}
        for day, dn in sdnco_sol.assignment.items():
            assert dn in valid_names, f"Invalid directorate {dn} on {day}"
