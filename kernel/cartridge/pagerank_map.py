"""Deterministic file-level PageRank over the symbol-reference graph.

Edge u -> v exists with weight = number of symbols defined in v that are referenced in u.
Node order, iteration, and tie-breaking are fully deterministic (sorted node names).
"""

from __future__ import annotations

from collections.abc import Sequence

from kernel.cartridge.ast_extractor import FileSymbols


def build_graph(files: Sequence[FileSymbols]) -> dict[str, dict[str, float]]:
    defined_by: dict[str, list[str]] = {}
    for f in files:
        for sym in f.exported:
            defined_by.setdefault(sym.name, []).append(f.path)

    graph: dict[str, dict[str, float]] = {f.path: {} for f in files}
    for f in files:
        for name in f.referenced:
            for target in defined_by.get(name, []):
                if target != f.path:
                    graph[f.path][target] = graph[f.path].get(target, 0.0) + 1.0
    for src in graph:
        graph[src] = {k: graph[src][k] for k in sorted(graph[src])}
    return graph


def pagerank(
    graph: dict[str, dict[str, float]], alpha: float = 0.85, iters: int = 100, tol: float = 1e-9
) -> dict[str, float]:
    nodes = sorted(graph)
    n = len(nodes)
    if n == 0:
        return {}
    rank = {node: 1.0 / n for node in nodes}
    out_weight = {node: sum(graph[node].values()) for node in nodes}
    for _ in range(iters):
        new = {node: (1.0 - alpha) / n for node in nodes}
        dangling = sum(rank[node] for node in nodes if out_weight[node] == 0.0)
        for u in nodes:
            if out_weight[u] == 0.0:
                continue
            for v in sorted(graph[u]):
                new[v] += alpha * rank[u] * (graph[u][v] / out_weight[u])
        if dangling:
            share = alpha * dangling / n
            for node in nodes:
                new[node] += share
        diff = sum(abs(new[node] - rank[node]) for node in nodes)
        rank = new
        if diff < tol:
            break
    return rank


if __name__ == "__main__":
    from kernel.cartridge.ast_extractor import FileSymbols, Symbol

    a = FileSymbols("a.py", (Symbol("f", "function", "def f()", 1),), ("g",))
    b = FileSymbols("b.py", (Symbol("g", "function", "def g()", 1),), ("f",))
    graph = build_graph([a, b])
    assert graph["a.py"] == {"b.py": 1.0} and graph["b.py"] == {"a.py": 1.0}
    r = pagerank(graph)
    assert abs(r["a.py"] - r["b.py"]) < 1e-9
    print("pagerank_map.py self-check: OK")
