"""BM25 retriever with per-plan namespacing. Loads chunks from data/chunks.json.

Hybrid (BM25 + dense) is the documented upgrade path; MVP is BM25 only.
"""
from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path

from rank_bm25 import BM25Okapi

from backend.models import Citation


_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]+")


def tokenize(text: str) -> list[str]:
    return [t.lower() for t in _TOKEN.findall(text)]


class PlanIndex:
    """BM25 index for a single plan."""

    def __init__(self, plan: str, chunks: list[dict]):
        self.plan = plan
        self.chunks = chunks
        self._tokenized = [tokenize(c["text"]) for c in chunks]
        self.bm25 = BM25Okapi(self._tokenized) if self._tokenized else None

    # Source boost: prefer full EOC prose over SBC benefit tables and disclosure forms.
    # SBC/disclosure docs add coverage breadth but EOC language is what policy decisions cite.
    _SOURCE_BOOST = {"eoc": 1.3, "disclosure": 1.0, "sbc": 0.7}

    # Sections that are purely navigational and never contain citable policy language.
    # "Document Start" is a fallback label (heading not detected) — content is still valid.
    _SKIP_SECTIONS = {"table of contents"}

    def search(self, query: str, top_k: int = 5) -> list[tuple[dict, float]]:
        if not self.bm25:
            return []
        q_tokens = tokenize(query)
        scores = self.bm25.get_scores(q_tokens)
        boosted = [
            (chunk, score * self._SOURCE_BOOST.get(chunk.get("source", "eoc"), 1.0))
            for chunk, score in zip(self.chunks, scores)
            if chunk.get("section", "").lower() not in self._SKIP_SECTIONS
        ]
        ranked = sorted(boosted, key=lambda x: x[1], reverse=True)
        return ranked[:top_k]


class Retriever:
    def __init__(self, chunks_path: Path):
        raw = json.loads(Path(chunks_path).read_text())
        self._indices: dict[str, PlanIndex] = {
            plan: PlanIndex(plan, chunks) for plan, chunks in raw.items()
        }

    @property
    def plans(self) -> list[str]:
        return list(self._indices.keys())

    def search(self, plan: str, query: str, top_k: int = 5) -> list[Citation]:
        idx = self._indices.get(plan)
        if idx is None:
            return []
        hits = idx.search(query, top_k=top_k)
        return [
            Citation(
                plan=plan,
                section=chunk["section"],
                excerpt=chunk["text"][:600],
                score=float(score),
            )
            for chunk, score in hits
            if score > 0
        ]


@lru_cache(maxsize=1)
def get_retriever() -> Retriever:
    root = Path(__file__).resolve().parents[2]
    return Retriever(root / "data" / "chunks.json")
