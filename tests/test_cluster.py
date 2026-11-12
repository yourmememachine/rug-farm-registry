from src.cluster import build_clusters, cluster_id_for, find_cluster, nodes_within_hops, build_graph

TRANSFERS = [
    {"from": "WalletA1111111111111111111111111111111", "to": "WalletB1111111111111111111111111111111", "amount_sol": 5.0},
    {"from": "WalletB1111111111111111111111111111111", "to": "WalletC1111111111111111111111111111111", "amount_sol": 4.0},
    {"from": "WalletD1111111111111111111111111111111", "to": "WalletE1111111111111111111111111111111", "amount_sol": 1.0},
]

A = "WalletA1111111111111111111111111111111"
B = "WalletB1111111111111111111111111111111"
C = "WalletC1111111111111111111111111111111"
D = "WalletD1111111111111111111111111111111"


def test_one_hop():
    graph = build_graph(TRANSFERS)
    assert nodes_within_hops(graph, A, max_hops=1) == {A, B}


def test_two_hops_reaches_c():
    graph = build_graph(TRANSFERS)
    assert nodes_within_hops(graph, A, max_hops=2) == {A, B, C}
    assert C not in nodes_within_hops(graph, A, max_hops=1)


def test_build_clusters_groups_funding_linked_wallets():
    clusters = build_clusters(TRANSFERS)
    sizes = sorted(len(c["members"]) for c in clusters)
    assert sizes == [2, 3]
    big = next(c for c in clusters if len(c["members"]) == 3)
    assert big["members"] == sorted([A, B, C])


def test_find_cluster_singleton_when_unknown():
    result = find_cluster(TRANSFERS, "WalletZ1111111111111111111111111111111")
    assert result["members"] == ["WalletZ1111111111111111111111111111111"]


def test_cluster_id_deterministic():
    assert cluster_id_for({A, B}) == cluster_id_for([B, A])
    assert cluster_id_for({A, B}) != cluster_id_for({A, C})
