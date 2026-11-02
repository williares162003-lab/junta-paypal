"""Fair turn swaps, computed with the Hungarian algorithm (written from scratch, no libraries).

Fairness rules:
  - Only members who have not received the pot yet take part, and only over the turns not yet paid out.
  - A member who did not ask for anything ("NEUTRAL") keeps their turn. Nobody is moved without asking.
  - EARLY members want the soonest possible turn, weighted by urgency (1-3).
  - LATER members prefer to receive at the end.
  - Every move costs a little, so among equally good plans the one that moves fewer people wins.
  - The result is only a proposal: each affected member must approve it before it is applied (see swaps.py).
"""
INF = 10**9
MOVE_COST = 1
EARLY_WEIGHT = 10   # per urgency level, per position of delay
LATER_WEIGHT = 5    # per position of being too early


def hungarian(cost):
    """Minimum-cost perfect assignment for a square matrix. Returns assignment[row] = column.

    Classic O(n^3) version with row/column potentials (Kuhn-Munkres).
    """
    n = len(cost)
    if n == 0:
        return []
    u, v = [0] * (n + 1), [0] * (n + 1)      # potentials for rows and columns
    match_col = [0] * (n + 1)                # match_col[j] = row assigned to column j (1-based, 0 = free)
    way = [0] * (n + 1)
    for i in range(1, n + 1):
        match_col[0] = i
        j0 = 0
        min_slack = [float("inf")] * (n + 1)
        used = [False] * (n + 1)
        while True:
            used[j0] = True
            i0, delta, j1 = match_col[j0], float("inf"), 0
            for j in range(1, n + 1):
                if not used[j]:
                    reduced = cost[i0 - 1][j - 1] - u[i0] - v[j]
                    if reduced < min_slack[j]:
                        min_slack[j], way[j] = reduced, j0
                    if min_slack[j] < delta:
                        delta, j1 = min_slack[j], j
            for j in range(n + 1):
                if used[j]:
                    u[match_col[j]] += delta
                    v[j] -= delta
                else:
                    min_slack[j] -= delta
            j0 = j1
            if match_col[j0] == 0:
                break
        while j0:                            # augment along the found path
            j1 = way[j0]
            match_col[j0] = match_col[j1]
            j0 = j1
    assignment = [0] * n
    for j in range(1, n + 1):
        assignment[match_col[j] - 1] = j - 1
    return assignment


def turn_cost(member, turn, rank, n_turns):
    """How unhappy this member is if they receive at `turn` (which is position `rank` among open turns)."""
    moved = turn != member["turn"]
    pref = member.get("preference", "NEUTRAL")
    if pref == "EARLY":
        return EARLY_WEIGHT * max(1, member.get("urgency", 1)) * rank + (MOVE_COST if moved else 0)
    if pref == "LATER":
        return LATER_WEIGHT * (n_turns - 1 - rank) + (MOVE_COST if moved else 0)
    return INF if moved else 0               # NEUTRAL: never moved


def propose(members):
    """members: members still waiting for the pot, each {id, name, turn, preference, urgency}.

    Returns {"changes": {member_id: (old_turn, new_turn)}, "cost_before", "cost_after"}.
    No changes means the current order is already the fairest.
    """
    turns = sorted(m["turn"] for m in members)
    if len(set(turns)) != len(turns):
        raise ValueError("Two members share a turn.")
    n = len(turns)
    cost = [[turn_cost(m, t, rank, n) for rank, t in enumerate(turns)] for m in members]
    assignment = hungarian(cost)
    cost_after = sum(cost[i][assignment[i]] for i in range(n))
    cost_before = sum(cost[i][turns.index(m["turn"])] for i, m in enumerate(members))
    if cost_after >= cost_before:
        return {"changes": {}, "cost_before": cost_before, "cost_after": cost_before}
    changes = {m["id"]: (m["turn"], turns[assignment[i]])
               for i, m in enumerate(members) if turns[assignment[i]] != m["turn"]}
    return {"changes": changes, "cost_before": cost_before, "cost_after": cost_after}
