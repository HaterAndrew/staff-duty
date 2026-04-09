"""
Integer Linear Program (ILP) roster solver.

PROBLEM FORMULATION
───────────────────
Given:
  D = set of directorates, each with eligible headcount h_d
  T = set of days in the quarter
  W = subset of T that are weekend or holiday ("hard" days)
  K = T \\ W  (weekday non-holiday days)

Two roles are solved jointly: SDNCO (variables x) and SD_Runner (variables y).

Decision variables:
  x[d][t] ∈ {0, 1}   — 1 if directorate d is assigned SDNCO on day t
  y[d][t] ∈ {0, 1}   — 1 if directorate d is assigned SD_Runner on day t

Constraints (applied per role, denoted generically as z):
  (C1) Coverage:    ∑_d z[d][t] = 1              ∀ t ∈ T
  (C2) Total quota: floor(q_d) ≤ ∑_t z[d][t] ≤ ceil(q_d)   ∀ d
  (C3) Hard-day quota (soft, penalised)
  (C4) Cool-down: z[d][t] + z[d][t+1] ≤ 1       ∀ d, consecutive t   (strict)
  (C5) Monthly spread (soft, penalised) — even month-to-month distribution
  (C6) Same-day overlap avoidance (soft, penalised):
       x[d][t] + y[d][t] ≤ 1 + s[d][t]   where s is a slack penalised in objective.
       Discourages assigning the same directorate to both roles on the same day.

Objective:
  Minimise  ∑ hard-day deviation
          + α · ∑ monthly spread deviation
          + β · ∑ same-day overlap slack

If the ILP is infeasible, the solver falls back to a greedy assignment
that still respects the cool-down and overlap constraints.

OUTPUT
──────
Two RosterSolution objects (SDNCO, SD_Runner), plus summary statistics.
"""

from __future__ import annotations

import logging
import math
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from typing import Dict, List, Set, Tuple

import pulp

from .calendar_utils import HOLIDAY, WEEKEND, WEEKDAY, classify_day
from .config import RosterConfig

logger = logging.getLogger(__name__)


# ── Result dataclasses ────────────────────────────────────────────────────────

@dataclass
class DirectorateStats:
    name: str
    eligible: int
    total_days: int
    weekday_days: int
    weekend_days: int
    holiday_days: int

    @property
    def hard_days(self) -> int:
        """Weekend + holiday days (both require sacrifice of personal time)."""
        return self.weekend_days + self.holiday_days

    @property
    def hard_day_pct(self) -> float:
        if self.total_days == 0:
            return 0.0
        return self.hard_days / self.total_days


@dataclass
class RosterSolution:
    """Full solution for one role (SDNCO or SD_Runner)."""
    role: str
    assignment: Dict[date, str]          # date → directorate name
    stats: List[DirectorateStats]
    solver_status: str                   # "Optimal", "Feasible", "Fallback"

    # Fairness metrics (lower = more fair)
    total_day_gini: float = 0.0          # Gini of total days per directorate
    hard_day_gini: float  = 0.0          # Gini of hard days per directorate


# ── Penalty weights ──────────────────────────────────────────────────────────
MONTHLY_PENALTY = 1.5    # even month-to-month distribution
OVERLAP_PENALTY = 3.0    # discourage same directorate on both roles same day
SPACING_PENALTY = 0.6    # balance within-month halves to prevent clustering


# ── Joint solver (public API) ────────────────────────────────────────────────

def solve_joint(
    sdnco_cfg: RosterConfig,
    runner_cfg: RosterConfig,
    all_days: List[date],
    holiday_dates: Set[date],
) -> Tuple[RosterSolution, RosterSolution]:
    """
    Solve SDNCO and SD_Runner jointly so the same-day overlap constraint
    can be enforced across roles.  Returns (sdnco_solution, runner_solution).
    """
    T      = all_days
    n_days = len(T)
    t_indices = list(range(n_days))

    hard_days    = [d for d in T if classify_day(d, holiday_dates) in (WEEKEND, HOLIDAY)]
    hard_indices = {T.index(hd) for hd in hard_days}
    n_hard       = len(hard_days)

    # Group day indices by month
    month_indices: Dict[Tuple[int, int], List[int]] = defaultdict(list)
    for ti, day in enumerate(T):
        month_indices[(day.year, day.month)].append(ti)

    # Shared directorate names (both configs must use the same names)
    dir_names = [d.name for d in sdnco_cfg.directorates]

    prob = pulp.LpProblem("StaffDuty_Joint", pulp.LpMinimize)

    # ── Build variables and constraints for each role ────────────────────────
    role_vars = {}   # role_tag → x dict
    obj_terms = []

    for tag, cfg in [("S", sdnco_cfg), ("R", runner_cfg)]:
        dirs = cfg.directorates
        H    = cfg.total_eligible

        x = pulp.LpVariable.dicts(
            f"x{tag}",
            ((dn, ti) for dn in dir_names for ti in t_indices),
            cat="Binary",
        )
        role_vars[tag] = x

        # Quotas
        total_quota: Dict[str, Tuple[int, int]] = {}
        hard_target: Dict[str, float] = {}
        for d in dirs:
            q = d.eligible / H * n_days
            total_quota[d.name] = (math.floor(q), math.ceil(q))
            hard_target[d.name] = d.eligible / H * n_hard

        # Hard-day deviation vars
        over  = pulp.LpVariable.dicts(f"over{tag}",  dir_names, lowBound=0)
        under = pulp.LpVariable.dicts(f"under{tag}", dir_names, lowBound=0)

        # (C1) Coverage
        for ti in t_indices:
            prob += pulp.lpSum(x[(dn, ti)] for dn in dir_names) == 1

        # (C2) Total quota
        for d in dirs:
            lo, hi = total_quota[d.name]
            total_assigned = pulp.lpSum(x[(d.name, ti)] for ti in t_indices)
            prob += total_assigned >= lo
            prob += total_assigned <= hi

        # (C3) Hard-day deviation
        for d in dirs:
            hard_assigned = pulp.lpSum(x[(d.name, ti)] for ti in hard_indices)
            target = hard_target[d.name]
            prob += hard_assigned - target <= over[d.name]
            prob += target - hard_assigned <= under[d.name]

        obj_terms.append(pulp.lpSum(over[dn] + under[dn] for dn in dir_names))

        # (C4) Cool-down — strict, no back-to-back
        for d in dirs:
            for ti in range(n_days - 1):
                prob += x[(d.name, ti)] + x[(d.name, ti + 1)] <= 1

        # (C5) Monthly spread
        mo_over  = {}
        mo_under = {}
        for d in dirs:
            q_total = d.eligible / H * n_days
            for ym, indices in month_indices.items():
                month_share    = len(indices) / n_days
                monthly_target = q_total * month_share
                key = (d.name, ym)
                mo_over[key]  = pulp.LpVariable(
                    f"mo_over{tag}_{d.name}_{ym[0]}_{ym[1]}", lowBound=0)
                mo_under[key] = pulp.LpVariable(
                    f"mo_under{tag}_{d.name}_{ym[0]}_{ym[1]}", lowBound=0)
                month_assigned = pulp.lpSum(x[(d.name, ti)] for ti in indices)
                prob += month_assigned - monthly_target <= mo_over[key]
                prob += monthly_target - month_assigned <= mo_under[key]

        obj_terms.append(
            MONTHLY_PENALTY * pulp.lpSum(
                mo_over[k] + mo_under[k] for k in mo_over))

        # Within-month spacing: split each month into two halves and
        # penalise deviation from half the monthly target in each half.
        # This prevents front- or back-loading within a month without
        # creating O(n_days × n_dirs) extra variables.
        half_over  = {}
        half_under = {}
        for d in dirs:
            q_total = d.eligible / H * n_days
            for ym, indices in month_indices.items():
                mid = len(indices) // 2
                for half_idx, half_indices in enumerate([indices[:mid], indices[mid:]]):
                    half_target = q_total * len(half_indices) / n_days
                    key = (d.name, ym, half_idx)
                    half_over[key]  = pulp.LpVariable(
                        f"ho{tag}_{d.name}_{ym[0]}_{ym[1]}_{half_idx}", lowBound=0)
                    half_under[key] = pulp.LpVariable(
                        f"hu{tag}_{d.name}_{ym[0]}_{ym[1]}_{half_idx}", lowBound=0)
                    half_sum = pulp.lpSum(x[(d.name, ti)] for ti in half_indices)
                    prob += half_sum - half_target <= half_over[key]
                    prob += half_target - half_sum  <= half_under[key]

        obj_terms.append(
            SPACING_PENALTY * pulp.lpSum(
                half_over[k] + half_under[k] for k in half_over))

    # (C6) Same-day overlap avoidance (soft) ──────────────────────────────────
    xs = role_vars["S"]
    xr = role_vars["R"]
    overlap_slack = {}
    for dn in dir_names:
        for ti in t_indices:
            s_var = pulp.LpVariable(f"olap_{dn}_{ti}", cat="Binary")
            overlap_slack[(dn, ti)] = s_var
            prob += xs[(dn, ti)] + xr[(dn, ti)] <= 1 + s_var

    obj_terms.append(
        OVERLAP_PENALTY * pulp.lpSum(
            overlap_slack[k] for k in overlap_slack))

    # ── Objective ────────────────────────────────────────────────────────────
    prob += pulp.lpSum(obj_terms)

    # ── Solve ────────────────────────────────────────────────────────────────
    solver = pulp.PULP_CBC_CMD(msg=0, timeLimit=120)
    status_code = prob.solve(solver)
    status_str  = pulp.LpStatus[prob.status]
    logger.info("Joint ILP solver status: %s", status_str)

    if status_code not in (1,):
        logger.warning("Joint ILP status '%s' — falling back to greedy.", status_str)
        return _greedy_joint_fallback(
            sdnco_cfg, runner_cfg, T, holiday_dates)

    # ── Extract solutions ────────────────────────────────────────────────────
    results = []
    for tag, cfg in [("S", sdnco_cfg), ("R", runner_cfg)]:
        xv = role_vars[tag]
        assignment: Dict[date, str] = {}
        for ti, day in enumerate(T):
            for dn in dir_names:
                if pulp.value(xv[(dn, ti)]) > 0.5:
                    assignment[day] = dn
                    break
        results.append(_build_solution(cfg, assignment, holiday_dates, status_str))

    return results[0], results[1]


# ── Single-role solver (backward compat, used by CLI) ────────────────────────

def solve(
    config: RosterConfig,
    all_days: List[date],
    holiday_dates: Set[date],
) -> RosterSolution:
    """Solve a single role independently (no overlap constraint)."""
    return _solve_single(config, all_days, holiday_dates)


def _solve_single(
    config: RosterConfig,
    all_days: List[date],
    holiday_dates: Set[date],
) -> RosterSolution:
    dirs   = config.directorates
    H      = config.total_eligible
    T      = all_days
    n_days = len(T)

    hard_days    = [d for d in T if classify_day(d, holiday_dates) in (WEEKEND, HOLIDAY)]
    hard_indices = {T.index(hd) for hd in hard_days}
    n_hard       = len(hard_days)

    month_indices: Dict[Tuple[int, int], List[int]] = defaultdict(list)
    for ti, day in enumerate(T):
        month_indices[(day.year, day.month)].append(ti)

    total_quota: Dict[str, Tuple[int, int]] = {}
    hard_target: Dict[str, float] = {}
    for d in dirs:
        q = d.eligible / H * n_days
        total_quota[d.name] = (math.floor(q), math.ceil(q))
        hard_target[d.name] = d.eligible / H * n_hard

    prob = pulp.LpProblem(f"StaffDuty_{config.role}", pulp.LpMinimize)
    dir_names = [d.name for d in dirs]
    t_indices = list(range(n_days))

    x = pulp.LpVariable.dicts(
        "x", ((dn, ti) for dn in dir_names for ti in t_indices), cat="Binary")

    over  = pulp.LpVariable.dicts("over",  dir_names, lowBound=0)
    under = pulp.LpVariable.dicts("under", dir_names, lowBound=0)

    for ti in t_indices:
        prob += pulp.lpSum(x[(dn, ti)] for dn in dir_names) == 1
    for d in dirs:
        lo, hi = total_quota[d.name]
        total_assigned = pulp.lpSum(x[(d.name, ti)] for ti in t_indices)
        prob += total_assigned >= lo
        prob += total_assigned <= hi
    for d in dirs:
        hard_assigned = pulp.lpSum(x[(d.name, ti)] for ti in hard_indices)
        target = hard_target[d.name]
        prob += hard_assigned - target <= over[d.name]
        prob += target - hard_assigned <= under[d.name]
    for d in dirs:
        for ti in range(n_days - 1):
            prob += x[(d.name, ti)] + x[(d.name, ti + 1)] <= 1

    mo_over, mo_under = {}, {}
    for d in dirs:
        q_total = d.eligible / H * n_days
        for ym, indices in month_indices.items():
            monthly_target = q_total * len(indices) / n_days
            key = (d.name, ym)
            mo_over[key]  = pulp.LpVariable(f"mo_over_{d.name}_{ym[0]}_{ym[1]}", lowBound=0)
            mo_under[key] = pulp.LpVariable(f"mo_under_{d.name}_{ym[0]}_{ym[1]}", lowBound=0)
            month_assigned = pulp.lpSum(x[(d.name, ti)] for ti in indices)
            prob += month_assigned - monthly_target <= mo_over[key]
            prob += monthly_target - month_assigned <= mo_under[key]

    prob += (pulp.lpSum(over[dn] + under[dn] for dn in dir_names) +
             MONTHLY_PENALTY * pulp.lpSum(mo_over[k] + mo_under[k] for k in mo_over))

    solver = pulp.PULP_CBC_CMD(msg=0, timeLimit=60)
    status_code = prob.solve(solver)
    status_str  = pulp.LpStatus[prob.status]

    if status_code != 1:
        return _greedy_fallback(config, T, holiday_dates, hard_target, total_quota)

    assignment: Dict[date, str] = {}
    for ti, day in enumerate(T):
        for dn in dir_names:
            if pulp.value(x[(dn, ti)]) > 0.5:
                assignment[day] = dn
                break
    return _build_solution(config, assignment, holiday_dates, status_str)


# ── Greedy fallbacks ─────────────────────────────────────────────────────────

def _greedy_joint_fallback(
    sdnco_cfg: RosterConfig,
    runner_cfg: RosterConfig,
    all_days: List[date],
    holiday_dates: Set[date],
) -> Tuple[RosterSolution, RosterSolution]:
    """Greedy fallback that respects cool-down and avoids same-day overlap."""
    H_s = sdnco_cfg.total_eligible
    H_r = runner_cfg.total_eligible
    n_days = len(all_days)
    n_hard = sum(1 for d in all_days if classify_day(d, holiday_dates) in (WEEKEND, HOLIDAY))

    hard_target_s = {d.name: d.eligible / H_s * n_hard for d in sdnco_cfg.directorates}
    hard_target_r = {d.name: d.eligible / H_r * n_hard for d in runner_cfg.directorates}
    total_quota_s = {}
    total_quota_r = {}
    for d in sdnco_cfg.directorates:
        q = d.eligible / H_s * n_days
        total_quota_s[d.name] = (math.floor(q), math.ceil(q))
    for d in runner_cfg.directorates:
        q = d.eligible / H_r * n_days
        total_quota_r[d.name] = (math.floor(q), math.ceil(q))

    s_sol = _greedy_fallback(sdnco_cfg, all_days, holiday_dates, hard_target_s, total_quota_s)
    r_sol = _greedy_fallback(
        runner_cfg, all_days, holiday_dates, hard_target_r, total_quota_r,
        avoid_assignment=s_sol.assignment)
    return s_sol, r_sol


def _greedy_fallback(
    config: RosterConfig,
    all_days: List[date],
    holiday_dates: Set[date],
    hard_target: Dict[str, float],
    total_quota: Dict[str, Tuple[int, int]],
    avoid_assignment: Dict[date, str] | None = None,
) -> RosterSolution:
    """
    Greedy assignment when ILP fails.
    Respects cool-down and optionally avoids same-day overlap with another role.
    """
    dirs      = config.directorates
    dir_names = [d.name for d in dirs]

    remaining_total = {d.name: total_quota[d.name][1] for d in dirs}
    assigned_hard   = {d.name: 0 for d in dirs}
    assignment: Dict[date, str] = {}

    hard_days = [d for d in all_days if classify_day(d, holiday_dates) in (WEEKEND, HOLIDAY)]
    soft_days = [d for d in all_days if d not in set(hard_days)]
    ordered_days = _interleave(hard_days, soft_days)

    prev_assigned: str | None = None

    for day in ordered_days:
        is_hard = classify_day(day, holiday_dates) in (WEEKEND, HOLIDAY)
        eligible = [dn for dn in dir_names if remaining_total[dn] > 0]
        if not eligible:
            eligible = dir_names

        # (C4) Cool-down
        if prev_assigned and prev_assigned in eligible and len(eligible) > 1:
            eligible = [dn for dn in eligible if dn != prev_assigned]

        # (C6) Avoid same-day overlap with other role
        if avoid_assignment and day in avoid_assignment:
            other_dir = avoid_assignment[day]
            if other_dir in eligible and len(eligible) > 1:
                eligible = [dn for dn in eligible if dn != other_dir]

        if is_hard:
            chosen = max(eligible, key=lambda dn: hard_target.get(dn, 0) - assigned_hard[dn])
        else:
            chosen = max(eligible, key=lambda dn: remaining_total[dn])

        assignment[day] = chosen
        remaining_total[chosen] -= 1
        if is_hard:
            assigned_hard[chosen] += 1
        prev_assigned = chosen

    return _build_solution(config, assignment, holiday_dates, "Fallback")


def _interleave(a: list, b: list) -> list:
    """Interleave two lists as evenly as possible."""
    result, ai, bi = [], 0, 0
    while ai < len(a) or bi < len(b):
        ratio_a = ai / max(len(a), 1)
        ratio_b = bi / max(len(b), 1)
        if ai < len(a) and (bi >= len(b) or ratio_a <= ratio_b):
            result.append(a[ai]); ai += 1
        else:
            result.append(b[bi]); bi += 1
    return result


# ── Stats and fairness metrics ────────────────────────────────────────────────

def _build_solution(
    config: RosterConfig,
    assignment: Dict[date, str],
    holiday_dates: Set[date],
    status: str,
) -> RosterSolution:
    stats_list: List[DirectorateStats] = []

    for d in config.directorates:
        days_for_d = [day for day, dn in assignment.items() if dn == d.name]
        wd = sum(1 for day in days_for_d if classify_day(day, holiday_dates) == WEEKDAY)
        we = sum(1 for day in days_for_d if classify_day(day, holiday_dates) == WEEKEND)
        ho = sum(1 for day in days_for_d if classify_day(day, holiday_dates) == HOLIDAY)
        stats_list.append(
            DirectorateStats(
                name=d.name,
                eligible=d.eligible,
                total_days=len(days_for_d),
                weekday_days=wd,
                weekend_days=we,
                holiday_days=ho,
            )
        )

    total_vals = [s.total_days for s in stats_list]
    hard_vals  = [s.hard_days  for s in stats_list]

    return RosterSolution(
        role=config.role,
        assignment=assignment,
        stats=stats_list,
        solver_status=status,
        total_day_gini=_gini(total_vals),
        hard_day_gini=_gini(hard_vals),
    )


def _gini(values: list[int]) -> float:
    """
    Gini coefficient of a list of non-negative integers.
    0 = perfectly equal, 1 = maximally unequal.
    Used as a fairness metric on the dashboard.
    """
    if not values or sum(values) == 0:
        return 0.0
    n = len(values)
    s = sorted(values)
    cumsum = 0
    for i, v in enumerate(s):
        cumsum += (2 * (i + 1) - n - 1) * v
    return cumsum / (n * sum(values))
