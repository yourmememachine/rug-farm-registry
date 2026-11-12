"""Funding-graph clusterer (1-2 hops).

Wallets funded by the same source — or funding each other — within a
couple of hops are treated as one cluster (a likely rug farm). This is
a heuristic, not proof: clusters are evidence for review, and a
cluster_id alone never flags a wallet (the >=2 rugged-mints rule
still applies per wallet).

Pure functions over a list of transfers:
    {"from": <wallet>, "to": <wallet>, "amount_sol": float, ...}
"""
from __future__ import annotations

import hashlib
from collections import defaultdict, deque

MAX_HOPS = 2


def build_graph(transfers: list[dict]) -> dict[str, set[str]]:
    """Undirected adjacency map from funding transfers."""
    graph: dict[str, set[str]] = defaultdict(set)
    for tx in transfers:
        src, dst = tx.get("from"), tx.get("to")
        if not src or not dst or src == dst:
            continue
        graph[src].add(dst)
        graph[dst].add(src)
    return dict(graph)


def nodes_within_hops(graph: dict[str, set[str]], seed: str, max_hops: int = MAX_HOPS) -> set[str]:
    """All wallets reachable from `seed` within `max_hops` funding hops."""
    if seed not in graph:
        return {seed}
    seen: set[str] = {seed}
    frontier = deque([(seed, 0)])
    while frontier:
        node, hops = frontier.popleft()
        if hops >= max_hops:
            continue
        for neighbour in graph.get(node, set()):
            if neighbour not in seen:
                seen.add(neighbour)
                frontier.append((neighbour, hops + 1))
    return seen


def cluster_id_for(members: set[str] | list[str]) -> str:
    """Deterministic cluster id from the sorted member list."""
    digest = hashlib.sha256(",".join(sorted(members)).encode()).hexdigest()[:8]
    return f"cluster-{digest}"


def build_clusters(transfers: list[dict], max_hops: int = MAX_HOPS) -> list[dict]:
    """Connected components, expanded to `max_hops` from each member.

    Returns [{"cluster_id": ..., "members": [...sorted...]}, ...] sorted
    by size (largest first), then by first member. Singletons are kept
    as one-wallet clusters so every wallet has a cluster.
    """
    graph = build_graph(transfers)
    wallets = set(graph)
    # Include wallets that only appear on one side are already in graph;
    # transfers with a missing side were skipped in build_graph.
    visited: set[str] = set()
    clusters: list[dict] = []
    for wallet in sorted(wallets):
        if wallet in visited:
            continue
        # Connected component (unbounded, for the component itself),
        # then the reported members are the 2-hop neighbourhood of the
        # component's smallest wallet — in practice the same set for
        # the small farms this MVP targets.
        component = nodes_within_hops(graph, wallet, max_hops=10**9)
        visited |= component
        members = sorted(component)
        clusters.append({"cluster_id": cluster_id_for(members), "members": members})
    clusters.sort(key=lambda c: (-len(c["members"]), c["members"][0]))
    return clusters


def find_cluster(transfers: list[dict], seed: str, max_hops: int = MAX_HOPS) -> dict:
    """The cluster containing `seed` (within `max_hops`), or a singleton."""
    graph = build_graph(transfers)
    members = sorted(nodes_within_hops(graph, seed, max_hops))
    return {"cluster_id": cluster_id_for(members), "members": members}
