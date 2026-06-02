"""Reference implementation of the boost actor (entity + institution lanes).

Drops into ``m3xabr_core/actors/booster.py``. Consumes the entity registry
described in ``docs/06-entity-boost.md`` and runs between the retriever
(Actor 4) and the synthesizer (Actor 5) in the m3xabr-core pipeline.

The actor exposes one public method, :meth:`Booster.boost`, which takes the
base retrieval and the classifier output and returns an augmented, re-scored
list of documents with explicit ``boost_source`` and ``track`` metadata so
the synthesizer can attribute correctly.

This module assumes the project provides:

* ``m3xabr_core.backends.vector_db.VectorDBBackend`` — same as the retriever uses
* ``m3xabr_core.schemas.RetrievedDoc`` — same as the retriever returns; the
  module adds ``boost_source`` and ``track`` fields to the doc metadata if
  the schema supports them (the schema extension is described in
  ``docs/04-query-understanding.md``).
* ``m3xabr_core.scoring.SourceScorer`` — for re-scoring boosted rows
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

import yaml


# ─── Entity registry data classes ─────────────────────────────────────────────

@dataclass(frozen=True)
class EntityEntry:
    """One row in the entity registry."""

    slug: str
    canonical_name: str
    aliases: tuple[str, ...]
    handles: tuple[str, ...]
    tier: dict[str, int]
    boost_mode: str  # "direct" or "direct_plus_mentions"
    max_boost_rows: int
    importance_threshold: float


@dataclass
class EntityRegistry:
    """Loads and indexes the entity registry YAML."""

    path: Path
    _by_slug: dict[str, EntityEntry] = field(default_factory=dict, init=False)
    _by_alias: dict[str, str] = field(default_factory=dict, init=False)

    def __post_init__(self) -> None:
        data = yaml.safe_load(Path(self.path).read_text(encoding="utf-8"))
        for raw in data.get("entities", []):
            slug = raw["slug"].lower()
            entry = EntityEntry(
                slug=slug,
                canonical_name=raw.get("canonical_name", slug),
                aliases=tuple(a.lower() for a in raw.get("aliases", [])),
                handles=tuple(h.lower() for h in raw.get("handles", [])),
                tier=dict(raw.get("tier", {})),
                boost_mode=raw.get("boost_mode", "direct").lower(),
                max_boost_rows=int(raw.get("max_boost_rows", 5)),
                importance_threshold=float(raw.get("importance_threshold", 0.5)),
            )
            self._by_slug[slug] = entry
            self._by_alias[slug] = slug
            for a in entry.aliases:
                self._by_alias[a] = slug
            # also index by handle so that "what does itau_macro say"
            # (handle leaked into query) resolves
            for h in entry.handles:
                self._by_alias.setdefault(h, slug)

    def resolve(self, entity_or_alias: str) -> EntityEntry | None:
        """Look up an entity by slug, alias, or handle. Returns None if not found."""
        key = (entity_or_alias or "").strip().lower()
        if not key:
            return None
        slug = self._by_alias.get(key)
        if slug is None:
            return None
        return self._by_slug.get(slug)

    def resolve_for_topic(
        self, entity_or_alias: str, topic: str | None
    ) -> EntityEntry | None:
        """If multiple entities share an alias, pick the one with highest tier
        for the given topic (lower tier number = higher priority)."""
        key = (entity_or_alias or "").strip().lower()
        candidates = [e for e in self._by_slug.values()
                      if key == e.slug or key in e.aliases or key in e.handles]
        if not candidates:
            return None
        if topic is None or len(candidates) == 1:
            return candidates[0]
        candidates.sort(key=lambda e: e.tier.get(topic, 99))
        return candidates[0]


# ─── Booster actor ────────────────────────────────────────────────────────────

# Query-type → recency cutoff for the CITED lane (in hours).
# None = no cutoff. See docs/07-institution-boost.md.
_RECENCY_BY_QUERY_TYPE: dict[str, int | None] = {
    "breaking": 24,
    "current": 168,        # 1 week
    "analytical": None,
    "historical": None,
}

# Well-represented guard thresholds. If the base retrieval already has
# at least these many rows for the entity, skip the boost.
_WELL_REPRESENTED_DIRECT = 5
_WELL_REPRESENTED_MENTIONS = 10


@dataclass
class Booster:
    """Actor 4.5 — entity / institution / keyword-aware boost.

    :param vector_db: the same backend used by the retriever
    :param entity_registry: loaded :class:`EntityRegistry`
    :param scorer: :class:`SourceScorer` for re-scoring boosted rows
    """

    vector_db: object  # VectorDBBackend
    entity_registry: EntityRegistry
    scorer: object  # SourceScorer

    # ── public API ──────────────────────────────────────────────────────────

    def boost(self, base_retrieval, classifier_output) -> list:
        """Run all applicable boost lanes and return the merged retrieval.

        ``base_retrieval`` and ``classifier_output`` are the same types the
        retriever and classifier already use. Returns a list of
        ``RetrievedDoc`` with new ``boost_source`` and ``track`` metadata.
        """
        entities = list(classifier_output.entities or [])
        topics = list(classifier_output.topics or [])
        query_type = getattr(classifier_output, "query_type", "current")
        si = float(getattr(classifier_output, "source_importance", 0.0))

        # Dedup base by doc_id for merge
        seen = {d.doc_id: d for d in base_retrieval}

        for ent_name in entities:
            primary_topic = topics[0] if topics else None
            entry = self.entity_registry.resolve_for_topic(ent_name, primary_topic)
            if entry is None:
                continue

            # Well-represented guard
            if not self._needs_boost(entry, base_retrieval):
                continue

            # DIRECT lane (always runs when boost triggers)
            direct_cap = self._effective_cap(entry, si)
            for row in self._fetch_direct(entry, direct_cap):
                if row.doc_id in seen:
                    continue
                row.metadata = dict(row.metadata or {})
                row.metadata["boost_source"] = "entity_direct"
                row.metadata["boost_slug"] = entry.slug
                row.metadata["track"] = "institution_direct"
                seen[row.doc_id] = row

            # CITED lane (only for direct_plus_mentions entities)
            if entry.boost_mode == "direct_plus_mentions":
                cutoff = _RECENCY_BY_QUERY_TYPE.get(query_type)
                for row in self._fetch_cited(entry, direct_cap, cutoff):
                    if row.doc_id in seen:
                        continue
                    row.metadata = dict(row.metadata or {})
                    row.metadata["boost_source"] = "institution_cited"
                    row.metadata["boost_slug"] = entry.slug
                    row.metadata["track"] = "institution_cited"
                    seen[row.doc_id] = row

        # Re-score every doc with the dynamic formula (using stored
        # semantic/freshness components if present; falling back to
        # current score otherwise).
        merged = list(seen.values())
        for d in merged:
            sem = getattr(d, "semantic_similarity", None)
            fresh = getattr(d, "freshness_score", None)
            if sem is not None and fresh is not None:
                d.score = self.scorer.blend(
                    semantic=sem,
                    source_id=d.source_id,
                    freshness=fresh,
                    source_importance=si,
                )
            # else: leave score from base retrieval untouched

        merged.sort(key=lambda d: d.score, reverse=True)
        return merged

    # ── internals ──────────────────────────────────────────────────────────

    def _effective_cap(self, entry: EntityEntry, si: float) -> int:
        """Raise cap when source_importance crosses the entry's threshold."""
        if si >= entry.importance_threshold:
            return entry.max_boost_rows * 3
        return entry.max_boost_rows

    def _needs_boost(self, entry: EntityEntry, base_retrieval) -> bool:
        """Skip boost when the base retrieval already has enough material."""
        direct = sum(
            1 for d in base_retrieval
            if (d.source_id or "").lower() in entry.handles
        )
        # mentions = aliases appearing in content_text
        alias_patterns = [
            re.compile(r"\b" + re.escape(a) + r"\b", re.IGNORECASE)
            for a in entry.aliases
        ]
        mentions = 0
        for d in base_retrieval:
            text = (d.content_text or "")
            if any(p.search(text) for p in alias_patterns):
                mentions += 1
        skip = (direct >= _WELL_REPRESENTED_DIRECT
                or mentions >= _WELL_REPRESENTED_MENTIONS)
        return not skip

    def _fetch_direct(self, entry: EntityEntry, cap: int) -> list:
        """Pull most-recent rows authored by the entity."""
        return self.vector_db.search(
            filter_expr=f"source_id IN ({_in_list(entry.handles)})",
            order_by="published_at DESC",
            top_k=cap,
        )

    def _fetch_cited(
        self,
        entry: EntityEntry,
        cap: int,
        cutoff_hours: int | None,
    ) -> list:
        """Pull most-relevant rows mentioning the entity in content.

        Sorted by semantic similarity, not freshness — mentions vary widely
        in topical relevance and we want the most topical ones.
        """
        clauses = [f"content_text LIKE '%{a}%'" for a in entry.aliases]
        where = "(" + " OR ".join(clauses) + ")"
        if cutoff_hours is not None:
            cutoff_iso = (
                datetime.now(timezone.utc) - timedelta(hours=cutoff_hours)
            ).isoformat()
            where += f" AND published_at > '{cutoff_iso}'"
        return self.vector_db.search(
            filter_expr=where,
            top_k=cap,
            # The backend should sort by stored semantic_similarity if
            # available; otherwise vector search at query embedding.
        )


def _in_list(items: Iterable[str]) -> str:
    return ", ".join(f"'{i}'" for i in items)
