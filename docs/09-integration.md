# 09 — Integration with `m3xabr-core`

This document is the concrete wiring guide: what files to change in `m3xabr-core`, in what order, with what tests.

## Order of implementation

Implement the layers in dependency order. Each layer is independently useful, so you can pause at any point and have a working system that's strictly better than the layer-below baseline.

1. **L1 — Priority registry + scoring module** (~2 hours)
2. **L2 — Classifier extension** (~1 hour, mostly prompt iteration)
3. **L3 — Wire scoring into retriever** (~1 hour)
4. **L4-kw — Keyword boost in retriever** (~30 minutes)
5. **L4-ent — Entity boost actor** (~2 hours)
6. **L4-inst — Institution boost mode in entity registry** (~1 hour)

Total: ~7–8 hours of focused work for a complete implementation against a corpus that already has a populated entity registry. Building the entity registry itself from scratch on a new corpus is another 4–8 hours of curation, frontloaded.

## Step-by-step file changes

### Step 1 — Add the priority registry

**New file**: `m3xabr_core/registries/source_priority.yaml`

Use the format described in [`03-priority-registry.md`](03-priority-registry.md). Start with 10–20 entries for the corpus's most-cited sources. Untagged sources default to `normal`.

**New file**: `m3xabr_core/scoring.py`

Reference implementation in [`reference/scoring.py`](../reference/scoring.py). Key class:

```python
class SourceScorer:
    def __init__(self, priority_registry_path: Path): ...

    def source_score(self, source_id: str) -> float:
        """Returns 0.0, 0.5, or 1.0 based on priority registry."""

    def blend(self, semantic: float, source_id: str, freshness: float,
              source_importance: float) -> float:
        """The dynamic-blend formula from docs/05."""
```

**Test**: load the registry, call `source_score` on a known high-priority and low-priority source, verify 1.0 and 0.0 respectively. Call `blend` with `si=0` and `si=0.8` and verify the weights collapse to the expected values.

### Step 2 — Extend the classifier

**Modify**: `m3xabr_core/schemas.py`

Add two fields to `ClassifierOutput`:

```python
source_importance: float = Field(default=0.0, ge=0.0, le=0.8)
boost_keywords: list[str] = Field(default_factory=list)
```

**Modify**: `config/classifier_prompt.md`

Append the field specification and the 8–10 calibration examples from [`reference/classifier_addendum.py`](../reference/classifier_addendum.py). The exact wording is less important than including examples that span the `si` range and exercise the alias-aware boost-keyword expansion.

**Modify**: `m3xabr_core/actors/classifier.py`

Add the `_add_implicit_boosts` helper that reads `entities` and the entity registry, and appends known aliases to `boost_keywords`. This is purely post-processing; no LLM call.

**Test**: run the classifier on the 8 calibration examples (in [`reference/classifier_addendum.py`](../reference/classifier_addendum.py)) and verify the output matches the expected `source_importance` and `boost_keywords`. Tolerate ±0.1 on the float — the Haiku call isn't deterministic to that resolution. Iterate the prompt if any example is off by more than 0.2.

### Step 3 — Wire scoring into the retriever

**Modify**: `m3xabr_core/actors/retriever.py`

Inject the `SourceScorer` into the constructor:

```python
def __init__(self, embedder, vector_db, scorer: SourceScorer, ...):
    self._scorer = scorer
```

Replace the existing `_rescore` method with a version that uses `scorer.blend`:

```python
def _rescore(self, docs, classifier_output):
    si = classifier_output.source_importance
    now = datetime.now(timezone.utc)
    for doc in docs:
        vec_sim = max(0.0, 1.0 - doc.score)  # cosine distance → similarity
        freshness = self._freshness(doc.published_at, now)
        doc.score = self._scorer.blend(vec_sim, doc.source_id, freshness, si)
    return sorted(docs, key=lambda d: d.score, reverse=True)
```

**Test**: run a known query against a corpus that includes a high-priority and a normal-priority source on the same topic. Verify that with `si=0.0` they rank in semantic order, and with `si=0.8` the high-priority one rises.

### Step 4 — Add keyword boost in the retriever

**Modify**: `m3xabr_core/actors/retriever.py`

Add a keyword-boost step after `_rescore`:

```python
def _apply_keyword_boost(self, docs, boost_keywords):
    if not boost_keywords:
        return docs
    for doc in docs:
        hits = self._count_keyword_hits(doc.content_text, boost_keywords)
        doc.score += min(0.15, 0.05 * hits)
    return sorted(docs, key=lambda d: d.score, reverse=True)
```

And lower the minimum-similarity threshold conditionally:

```python
effective_min_sim = 0.20 if classifier_output.boost_keywords else self._config["min_sim"]
docs = [d for d in docs if d.score >= effective_min_sim]
```

Apply the filter **after** the keyword boost, so boosted documents have a fair chance to clear the threshold.

**Test**: run a query with keywords that match documents at semantic 0.30. Without keyword boost, those documents are filtered. With keyword boost, they appear in the top-10.

### Step 5 — Add the booster actor

**New file**: `m3xabr_core/actors/booster.py`

Reference implementation in [`reference/booster.py`](../reference/booster.py). The actor's interface:

```python
class Booster:
    def __init__(self, vector_db, entity_registry_path: Path): ...

    def boost(self, base_retrieval: list[RetrievedDoc],
              classifier_output: ClassifierOutput) -> list[RetrievedDoc]:
        """Pull additional rows for named entities and institutions,
        deduplicate against base, return the merged list."""
```

**New file**: `m3xabr_core/registries/entity_registry.yaml`

Build with the schema from [`06-entity-boost.md`](06-entity-boost.md). Start with the 5–10 most-mentioned entities in your corpus. Extend monthly.

**Modify**: `m3xabr_core/cli.py` (or wherever actor wiring happens)

After the retriever and before the synthesizer:

```python
base_docs = retriever.retrieve(query, classifier_output)
boosted_docs = booster.boost(base_docs, classifier_output)
# pass boosted_docs to synthesizer
```

**Test**: query naming a specific entity. Without the booster, the named entity's documents may or may not be in top-10. With the booster, they reliably are. Check the `boost_source` metadata on the top-10 — should see `entity_direct` tags for the named entity's rows.

### Step 6 — Enable institution mode

This is a configuration change, not a code change. In `entity_registry.yaml`, set `boost_mode: direct_plus_mentions` on the entities that warrant it (central banks, major desks, household-name commentators).

The booster code already handles both modes (see [`reference/booster.py`](../reference/booster.py)). Switching an entity from `direct` to `direct_plus_mentions` is a one-line YAML change.

**Test**: query about an institution. Verify the synthesizer receives both `institution_direct` and `institution_cited` tagged rows. Extend the synthesizer's prompt with the attribution distinction (from [`07-institution-boost.md`](07-institution-boost.md)).

## Tuning loop

After all layers are in place, run a structured tuning pass:

1. **Sample 30 queries** from real user logs, spanning low/medium/high source-importance.
2. **For each, run the full pipeline and capture top-10 retrieval**.
3. **Hand-rank** each top-10 against your judgment of relevance.
4. **Compute Kendall's tau** or NDCG@10 against your hand-ranking.
5. **Identify the worst-performing 5 queries** and diagnose: is it a missing entity registry entry? A stale priority tag? A miscalibrated `si`? A bad alias?
6. **Make targeted fixes** (registry / prompt iteration / scoring constant adjustment).
7. **Re-run** and measure. Stop when the bottom-quartile queries are acceptable.

This loop takes a half-day per quarter in steady state. After two or three quarters, the system reaches a plateau where further tuning produces diminishing returns and you focus on registry maintenance instead.

## Observability

Add log lines for each layer to make production behaviour debuggable. Recommended:

```python
print(f"[Classifier] si={si:.2f} entities={entities} boost_keywords={keywords[:5]}")
print(f"[Scoring]    base_weights sem={w_sem:.2f} src={w_src:.2f} fresh={w_fresh:.2f}")
print(f"[KeywordBoost] boosts_applied={boost_count} threshold={effective_min_sim:.2f}")
print(f"[EntityBoost] resolved {entity_slug} → handles={handles[:3]} mode={mode} limit={cap}")
print(f"[InstitutionBoost] {slug}: direct={direct_n} cited={cited_n} skip_well_repd={skipped}")
```

These five log lines per query reproduce the production observability of M3xA. They're invaluable when diagnosing "why did the system surface X instead of Y" — the answer is almost always visible in one of these lines.

## Rollback

The whole stack is reversible:

- Remove the import of `Booster` from `cli.py`; remove the `actors/booster.py` file. The pipeline reverts to retriever-only.
- Set all `source_importance` to 0.0 in the classifier output (by removing the prompt addition). The dynamic-scoring formula collapses to baseline.
- Delete `scoring.py` and revert `retriever.py._rescore` to the original. The pipeline reverts to plain semantic + freshness.
- Delete the registries. Untagged sources are `normal`; missing entities mean no boost.

Each step is independent. There's no "everything-or-nothing" coupling between the layers.

## What this doesn't address

Production M3xA has additional retrieval extensions that are out of scope for this repo:

- **Hybrid search** (BM25 over the same corpus, blended with vector). The keyword boost is a partial substitute; full hybrid is a project of similar size to this whole stack.
- **Cross-encoder rerank**. A second-pass scorer that takes the top-30 from this pipeline and reranks with a cross-encoder model. Valuable when the corpus has very high noise; expensive enough that it lives behind a query-type gate in production.
- **Time-aware retrieval**. The freshness term in the dynamic formula is a coarse approximation; production uses per-source decay rates (fast for news, slow for research) and per-query overrides (`time_hours` from the classifier). The reference code exposes the hook but doesn't implement the full behaviour.

Each of these is a separate project that can layer on top of the source-ranking stack without conflict.

You're done. Read [`examples/worked_query_trace.md`](../examples/worked_query_trace.md) for a final end-to-end trace.
