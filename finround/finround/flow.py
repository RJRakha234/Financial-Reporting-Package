"""Minimum-cost integral circulation on a network with arc lower bounds.

Controlled rounding of a two-way table *is* a network problem: rows are
sources, columns are sinks, and every cell is an arc whose flow must end up
either the floor or the ceiling of that cell's true value. Node conservation
is what makes the totals foot.

Two classical facts make this the right tool:

* the constraint matrix of a network is totally unimodular, so whenever a
  *fractional* circulation exists — and the un-rounded table itself is one —
  an *integral* one exists too. A solution is therefore guaranteed, not hoped
  for;
* minimising a linear cost over that network gives the *best* such rounding,
  not merely a valid one.

Arc costs here are frequently negative (nudging a ``.9`` up is cheaper than
pushing it down), so negative arcs are saturated up front — the classic
transformation — leaving a residual network with non-negative costs that
successive shortest paths (Dijkstra with potentials) handles.

Costs may be ``int``, ``float`` or ``Fraction``; the solver only compares and
adds them, so exact rational arithmetic works throughout.
"""

from __future__ import annotations

import heapq

INF = float("inf")


class _Arc:
    __slots__ = ("to", "cap", "cost", "rev")

    def __init__(self, to: int, cap: int, cost, rev: "_Arc | None") -> None:
        self.to = to
        self.cap = cap
        self.cost = cost
        self.rev = rev


class Circulation:
    """A min-cost circulation problem.

    Every arc carries an integral flow within ``[lower, upper]`` (both may be
    negative) and every node conserves flow. ``solve()`` reports whether a
    feasible circulation exists; when it does, the one found is of minimum
    cost and ``flow(edge_id)`` reads it back.
    """

    def __init__(self, n_nodes: int) -> None:
        self.n = n_nodes
        self.cost = 0
        self._edges: list[tuple[int, int, int, int, object]] = []
        self._flow: list[int] | None = None

    def add_edge(self, u: int, v: int, lower: int, upper: int, cost=0) -> int:
        """Add an arc ``u -> v`` with ``lower <= flow <= upper``; returns its id."""
        if upper < lower:
            raise ValueError(f"upper bound {upper} below lower bound {lower}")
        if not (0 <= u < self.n and 0 <= v < self.n):
            raise ValueError("node index out of range")
        self._edges.append((u, v, lower, upper, cost))
        return len(self._edges) - 1

    def flow(self, edge_id: int) -> int:
        if self._flow is None:
            raise RuntimeError("solve() must succeed before reading flows")
        return self._flow[edge_id]

    def solve(self) -> bool:
        """Find a minimum-cost feasible circulation. False if none exists."""
        src, snk = self.n, self.n + 1
        graph: list[list[_Arc]] = [[] for _ in range(self.n + 2)]

        def link(u: int, v: int, cap: int, cost) -> _Arc:
            fwd = _Arc(v, cap, cost, None)
            bwd = _Arc(u, 0, -cost, fwd)
            fwd.rev = bwd
            graph[u].append(fwd)
            graph[v].append(bwd)
            return fwd

        # Subtracting each arc's lower bound leaves a node imbalance that a
        # super-source/-sink must absorb; a saturated negative arc is simply
        # one whose "lower bound" is taken to be its capacity.
        imbalance = [0] * (self.n + 2)
        refs: list[tuple[_Arc, int, int, int]] = []
        base_cost = 0
        for u, v, lo, hi, cost in self._edges:
            forced = hi if cost < 0 else lo
            base_cost += forced * cost
            imbalance[v] += forced
            imbalance[u] -= forced
            if cost < 0:
                arc = link(v, u, hi - lo, -cost)
                refs.append((arc, hi - lo, hi, -1))
            else:
                arc = link(u, v, hi - lo, cost)
                refs.append((arc, hi - lo, lo, 1))

        required = 0
        for node, delta in enumerate(imbalance):
            if delta > 0:
                link(src, node, delta, 0)
                required += delta
            elif delta < 0:
                link(node, snk, -delta, 0)

        moved, moved_cost = _min_cost_flow(graph, src, snk, required)
        if moved < required:
            return False

        self.cost = base_cost + moved_cost
        self._flow = [
            forced + sign * (cap0 - arc.cap) for arc, cap0, forced, sign in refs
        ]
        return True


def _min_cost_flow(graph: list[list[_Arc]], src: int, snk: int, required: int):
    """Successive shortest paths with potentials; all arc costs are >= 0."""
    n = len(graph)
    potential = [0] * n
    sent = 0
    total_cost = 0

    while sent < required:
        dist: list = [INF] * n
        dist[src] = 0
        came: list[_Arc | None] = [None] * n
        settled = [False] * n
        heap: list[tuple] = [(0, src)]
        while heap:
            d, node = heapq.heappop(heap)
            if settled[node]:
                continue
            settled[node] = True
            for arc in graph[node]:
                if arc.cap <= 0:
                    continue
                nxt = arc.to
                weight = arc.cost + potential[node] - potential[nxt]
                if not settled[nxt] and d + weight < dist[nxt]:
                    dist[nxt] = d + weight
                    came[nxt] = arc
                    heapq.heappush(heap, (dist[nxt], nxt))
        if dist[snk] == INF:
            break

        for node in range(n):
            if dist[node] != INF:
                potential[node] += dist[node]

        push = required - sent
        node = snk
        while node != src:
            arc = came[node]
            push = min(push, arc.cap)
            node = arc.rev.to
        node = snk
        while node != src:
            arc = came[node]
            arc.cap -= push
            arc.rev.cap += push
            total_cost += push * arc.cost
            node = arc.rev.to
        sent += push

    return sent, total_cost
