"""Nearest-neighbour memory over past routing decisions (TF-IDF, cosine).

Pure Python on purpose: a few thousand short documents fit comfortably
in a hook's time budget, and nothing needs to be installed or served.
"""
from __future__ import annotations

import json
import math
from collections import Counter
from dataclasses import dataclass


@dataclass
class Neighbour:
    similarity: float
    tier: str
    final_tier: str
    escalations: int
    overkill: int
    cost: float | None


def _vector(tokens, idf):
    tf = Counter(tokens)
    vec = {t: (1 + math.log(c)) * idf.get(t, 0.0) for t, c in tf.items()}
    norm = math.sqrt(sum(v * v for v in vec.values())) or 1.0
    return {t: v / norm for t, v in vec.items()}


def neighbours(query_tokens, project, rows, k=8, min_similarity=0.25) -> list[Neighbour]:
    """Most similar past decisions. ``rows`` come from ``store.history``."""
    if not query_tokens or not rows:
        return []
    docs = []
    for r in rows:
        toks = json.loads(r["tokens"])
        if r["project"]:
            toks = toks + ["__project:" + r["project"]]  # same project: a little closer
        docs.append(toks)

    n = len(docs)
    df = Counter(t for d in docs for t in set(d))
    idf = {t: math.log((n + 1) / (c + 1)) + 1 for t, c in df.items()}

    q = list(query_tokens) + (["__project:" + project] if project else [])
    qv = _vector(q, idf)

    content = set(query_tokens)
    scored = []
    for r, d in zip(rows, docs):
        if not content.intersection(d):
            continue  # sharing only the project is not similarity
        dv = _vector(d, idf)
        sim = sum(w * dv.get(t, 0.0) for t, w in qv.items())
        if sim >= min_similarity:
            scored.append(Neighbour(sim, r["tier"], r["final_tier"] or r["tier"],
                                    r["escalations"], r["overkill"], r["cost"]))
    scored.sort(key=lambda x: x.similarity, reverse=True)
    return scored[:k]
