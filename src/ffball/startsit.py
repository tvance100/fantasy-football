"""Build the optimal starting lineup for a league's roster slots."""

from __future__ import annotations

from dataclasses import dataclass, field

from .projections import PlayerProjection

# slot label (as normalised in espn._roster_slot_counts) -> eligibleSlots token
_SLOT_TOKEN = {"FLEX": "RB/WR/TE", "SUPERFLEX": "OP"}

_DEDICATED = {"QB", "RB", "WR", "TE", "K", "D/ST",
              "DT", "DE", "LB", "DL", "CB", "S", "DB", "P", "HC"}


def _eligible(p: PlayerProjection, slot: str) -> bool:
    token = _SLOT_TOKEN.get(slot, slot)
    if token in p.eligible_slots:
        return True
    # fall back to bare position match
    return p.position == slot


@dataclass
class Lineup:
    week: int
    starters: list[tuple[str, PlayerProjection]] = field(default_factory=list)
    bench: list[PlayerProjection] = field(default_factory=list)
    projected_total: float = 0.0
    moves: list[str] = field(default_factory=list)          # START x / SIT y
    watch: list[str] = field(default_factory=list)          # injury / bye / weather
    droppable: list[str] = field(default_factory=list)


def optimize(projections: list[PlayerProjection], slot_counts: dict[str, int], week: int) -> Lineup:
    available = sorted(projections, key=lambda p: p.our_points, reverse=True)
    used: set[int] = set()
    starters: list[tuple[str, PlayerProjection]] = []

    # expand slot_counts into individual slot instances, dedicated first,
    # then compound/flex ordered by how few players can fill them.
    dedicated = [s for s in slot_counts if s in _DEDICATED]
    flex = [s for s in slot_counts if s not in _DEDICATED]

    def candidates(slot: str) -> int:
        return sum(1 for p in available if _eligible(p, slot))

    slot_instances: list[str] = []
    for s in dedicated:
        slot_instances += [s] * slot_counts[s]
    for s in sorted(flex, key=candidates):
        slot_instances += [s] * slot_counts[s]

    for slot in slot_instances:
        pick = next(
            (p for p in available if id(p) not in used and _eligible(p, slot) and not p.on_bye),
            None,
        )
        if pick is None:  # allow bye-week players only if nothing else fits
            pick = next((p for p in available if id(p) not in used and _eligible(p, slot)), None)
        if pick is not None:
            used.add(id(pick))
            starters.append((slot, pick))

    bench = [p for p in projections if id(p) not in used]
    bench.sort(key=lambda p: p.our_points, reverse=True)
    total = round(sum(p.our_points for _, p in starters), 2)

    lineup = Lineup(week=week, starters=starters, bench=bench, projected_total=total)
    _fill_moves(lineup)
    _fill_watch(lineup)
    _fill_droppable(lineup, slot_counts)
    return lineup


def _fill_moves(lineup: Lineup) -> None:
    started_now = {p.name for _, p in lineup.starters}
    benched_by_us = [
        b for b in lineup.bench if b.current_slot not in ("BE", "IR", "", None)
    ]
    named_in_start: set[str] = set()
    for slot, p in lineup.starters:
        if p.current_slot in ("BE", "IR", "", None):
            replaced = next(
                (b for b in benched_by_us
                 if b.name not in named_in_start and _shares_slot(b, p)),
                None,
            )
            if replaced:
                named_in_start.add(replaced.name)
                lineup.moves.append(
                    f"START {p.name} ({p.our_points}) over {replaced.name} "
                    f"({replaced.our_points}) at {slot}"
                )
            else:
                lineup.moves.append(f"START {p.name} ({p.our_points}) at {slot}")
    for b in benched_by_us:
        if b.name not in started_now and b.name not in named_in_start:
            lineup.moves.append(f"SIT {b.name} ({b.our_points}) — currently in {b.current_slot}")


def _shares_slot(a: PlayerProjection, b: PlayerProjection) -> bool:
    return bool(set(a.eligible_slots) & set(b.eligible_slots) - {"BE", "IR"})


def _fill_watch(lineup: Lineup) -> None:
    for slot, p in lineup.starters:
        for f in p.flags:
            if any(k in f for k in ("OUT", "DOUBTFUL", "QUESTIONABLE", "BYE", "weather", "tough matchup")):
                lineup.watch.append(f"{p.name} ({slot}): {f}")


def _fill_droppable(lineup: Lineup, slot_counts: dict[str, int]) -> None:
    from collections import Counter

    pos_need = Counter()
    for s, n in slot_counts.items():
        base = _SLOT_TOKEN.get(s, s)
        pos_need[base] += n
    have = Counter(p.position for p in lineup.bench + [pp for _, pp in lineup.starters])

    for b in sorted(lineup.bench, key=lambda p: p.our_points)[:3]:
        surplus = have[b.position] - pos_need.get(b.position, 0) - 1
        if b.our_points < 6 and surplus >= 1 and not b.on_bye:
            lineup.droppable.append(
                f"{b.name} ({b.position}, proj {b.our_points}) — {b.position} depth allows it"
            )
