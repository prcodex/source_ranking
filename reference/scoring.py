"""Reference implementation of source-ranking dynamic scoring.

This module is intended to drop into ``m3xabr_core/scoring.py``. It depends only
on the standard library plus PyYAML, and is consumed by an extended
``actors/retriever.py`` (see ``docs/09-integration.md``).

The single public class is :class:`SourceScorer`. It loads a priority registry
at construction time and exposes two methods:

* :meth:`SourceScorer.source_score` — maps a ``source_id`` to a value in
  ``[0.0, 1.0]`` via the ``high → 1.0 / normal → 0.5 / low → 0.0`` rule.
* :meth:`SourceScorer.blend` — applies the dynamic-weight formula described
  in ``docs/05-dynamic-scoring.md``.

See ``docs/05-dynamic-scoring.md`` for the rationale behind the constants.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Mapping

import yaml


# ─── Constants — see docs/05-dynamic-scoring.md ──────────────────────────────
PRIORITY_MULT: Mapping[str, float] = {
    "high": 1.0,
    "normal": 0.5,
    "low": 0.0,
}
"""Map from priority tier to the ``source_score`` value in [0, 1]."""

DEFAULT_PRIORITY = "normal"

# Dynamic-blend weights. See docs/05 for derivation.
BASE_W_SEM = 0.70
BASE_W_SRC = 0.15
W_FRESH = 0.15
SI_SLOPE = 0.55
SI_MAX = 0.80


@dataclass(frozen=True)
class SourceEntry:
    """One row in the priority registry."""

    id: str
    priority: str = DEFAULT_PRIORITY
    aliases: tuple[str, ...] = ()
    notes: str = ""


@dataclass
class SourceScorer:
    """Loads the priority registry and scores documents.

    :param registry_path: path to ``source_priority.yaml``.
    """

    registry_path: Path
    _by_id: dict[str, SourceEntry] = field(default_factory=dict, init=False)
    _alias_to_id: dict[str, str] = field(default_factory=dict, init=False)

    def __post_init__(self) -> None:
        self._load()

    def _load(self) -> None:
        data = yaml.safe_load(Path(self.registry_path).read_text(encoding="utf-8"))
        for entry in data.get("sources", []):
            sid = entry["id"].lower()
            priority = entry.get("priority", DEFAULT_PRIORITY).lower()
            aliases = tuple(a.lower() for a in entry.get("aliases", []))
            self._by_id[sid] = SourceEntry(
                id=sid,
                priority=priority,
                aliases=aliases,
                notes=entry.get("notes", ""),
            )
            for a in aliases:
                self._alias_to_id[a] = sid

    # ── public API ──────────────────────────────────────────────────────────

    def lookup(self, source_id: str | None) -> SourceEntry | None:
        """Resolve ``source_id`` (or one of its aliases) to a registry entry.

        Returns ``None`` if neither the id nor any alias matches; callers
        should treat that as ``DEFAULT_PRIORITY``.
        """
        if not source_id:
            return None
        key = source_id.lower()
        if key in self._by_id:
            return self._by_id[key]
        if key in self._alias_to_id:
            return self._by_id[self._alias_to_id[key]]
        return None

    def source_score(self, source_id: str | None) -> float:
        """Map ``source_id`` to a score in ``[0.0, 1.0]``.

        Untagged sources behave as ``normal`` → 0.5.
        """
        entry = self.lookup(source_id)
        priority = entry.priority if entry else DEFAULT_PRIORITY
        return PRIORITY_MULT.get(priority, PRIORITY_MULT[DEFAULT_PRIORITY])

    @staticmethod
    def weights(source_importance: float) -> tuple[float, float, float]:
        """Compute (w_sem, w_src, w_fresh) for a given ``si``.

        ``si`` is clamped to ``[0.0, SI_MAX]``. See docs/05.
        """
        si = max(0.0, min(SI_MAX, float(source_importance)))
        w_sem = BASE_W_SEM - si * SI_SLOPE
        w_src = BASE_W_SRC + si * SI_SLOPE
        return w_sem, w_src, W_FRESH

    def blend(
        self,
        semantic: float,
        source_id: str | None,
        freshness: float,
        source_importance: float,
    ) -> float:
        """The dynamic-blend formula.

        :param semantic:     cosine similarity in [0, 1]
        :param source_id:    used to look up source_score
        :param freshness:    decayed-age in [0, 1]
        :param source_importance: from ClassifierOutput.si
        :returns: the blended score (typically in [0, 1])
        """
        w_sem, w_src, w_fresh = self.weights(source_importance)
        src = self.source_score(source_id)
        return w_sem * semantic + w_src * src + w_fresh * freshness


# ─── Keyword boost — small helper, used by the retriever ──────────────────────

import re

# Common stop words filtered from boost keywords as a safety net.
# The classifier prompt should already exclude these, but this is the
# belt-and-braces.
_STOPWORDS = frozenset({
    "what", "about", "the", "in", "on", "for", "with", "now", "today",
    "this", "that", "and", "or", "but", "as", "of", "to", "is", "are",
    "was", "were", "be", "been", "being", "by", "from", "at", "an", "a",
    "say", "said", "saying", "tell", "think", "thinks", "thinking",
})

_BOOST_PER_HIT = 0.05
_MAX_BOOST = 0.15


def normalize_keywords(keywords: Iterable[str]) -> list[str]:
    """Strip stop words and short tokens from a keyword list."""
    out = []
    for kw in keywords:
        k = kw.strip().lower()
        if len(k) < 2 or k in _STOPWORDS:
            continue
        out.append(k)
    return out


def keyword_boost(content: str, keywords: Iterable[str]) -> float:
    """Additive keyword boost. Word-boundary matching.

    Returns a value in ``[0.0, _MAX_BOOST]``.
    """
    cleaned = normalize_keywords(keywords)
    if not cleaned:
        return 0.0
    text = content or ""
    hits = 0
    for kw in cleaned:
        pattern = r"\b" + re.escape(kw) + r"\b"
        if re.search(pattern, text, re.IGNORECASE):
            hits += 1
    return min(_MAX_BOOST, _BOOST_PER_HIT * hits)


def effective_min_sim(default_min: float, boost_keywords: Iterable[str]) -> float:
    """Lower the min-similarity threshold when boost keywords are present.

    Production default: 0.50 → 0.20 when boost keywords are non-empty.
    """
    return 0.20 if list(boost_keywords) else default_min
