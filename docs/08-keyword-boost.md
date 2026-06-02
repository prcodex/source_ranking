# 08 — Keyword Boost

The entity and institution lanes pull *additional rows* into the retrieval pool. The keyword lane works differently: it **adjusts the score and the inclusion threshold of rows already in the pool**, based on whether their content contains query-relevant keywords.

There are two mechanisms:

1. **Additive score boost** — for each row whose content contains any `boost_keyword`, add a small positive value to the row's final score.
2. **Threshold relaxation** — when `boost_keywords` is non-empty, lower the minimum-similarity threshold from default (0.50) to a relaxed value (0.20) so that boosted rows survive even when their semantic similarity is mediocre.

These mechanisms are simpler than the entity/institution lanes but contribute meaningfully because they catch cases the other lanes miss.

## Why this matters

Embedding models are imperfect — particularly on technical or institutional vocabulary. A document that explicitly names "BCB" and "Selic" in its first paragraph should rank highly for a query about *"BCB Selic decision"*, but the embedding may not reflect this if:

- The document is long and the topical content is diluted across paragraphs about other things.
- The embedding model isn't specifically tuned for Portuguese financial terminology and treats "BCB" as a noisy acronym.
- The query is short and lexically distinctive; the document is long and lexically diffuse.

Plain semantic similarity in these cases can fall to 0.30–0.45 for documents that are obviously the right match. The minimum-similarity threshold of 0.50, useful for filtering noise out of plain semantic retrieval, becomes harmful: it filters out the correct answer.

The keyword lane is a cheap, robust complement. If the query says "BCB" and the document says "BCB", that's signal — even when the cosine similarity hasn't caught it.

## The additive boost

For each retrieved row `d`:

```
keyword_hit_count = count of boost_keywords that appear in d.content_text (case-insensitive)
boost = min(0.15, 0.05 * keyword_hit_count)
score(d) += boost
```

The cap of 0.15 prevents a document with five keyword matches from monopolizing the ranking. The 0.05 per-hit value is calibrated to be meaningful (the same order of magnitude as the difference between strong and mediocre semantic similarity) but not dominant.

The boost is **additive on top of the dynamic scoring formula**, after the formula has been computed. The order is:

```
1. Compute base_score = dynamic_formula(d, si, source_priority, freshness)
2. keyword_boost = compute_keyword_boost(d, boost_keywords)
3. final_score = base_score + keyword_boost
```

For a document with two keyword hits (`boost = 0.10`) and `base_score = 0.55`, the final score is `0.65`. A document with no keyword hits and `base_score = 0.62` ranks below it — the keyword match has rescued a slightly-lower-semantic document.

## Threshold relaxation

Most retrieval systems apply a minimum-similarity threshold to avoid surfacing documents that semantically have nothing to do with the query. A typical default is 0.50 (on the cosine-similarity-to-[0,1]-mapped scale). Below this, results are usually noise.

When `boost_keywords` is non-empty, the reference implementation lowers `effective_min_sim` to 0.20:

```python
effective_min_sim = 0.20 if query_understanding.get("boost_keywords") else self._default_min_sim
```

A document with semantic similarity 0.35 — normally filtered out — survives if it has keyword matches contributing 0.10 boost, reaching final_score = 0.45. Without the relaxation, that document never enters the rerank pool.

The 0.20 floor is a hard quality minimum. Below 0.20, the document is almost certainly retrieving on token-level noise (a stop word coincidence or a tangentially-mentioned name). The relaxation widens the pool without dropping it open.

## Implicit keyword expansion

The classifier emits an explicit `boost_keywords` list, but the post-processing step `_add_implicit_boosts` expands it using aliases from the entity registry. This handles cases where the query named an entity but the document mentions it under an alternative spelling:

- Query: *"What does Itaú say about Selic?"*
- Classifier `boost_keywords`: `["itau", "selic"]`
- After implicit expansion: `["itau", "selic", "itau macro", "itaú", "itau bba", "itau economia"]`

The document was indexed with `itau macro` (canonical handle, no accent) and contains "Itaú Macro views..." in the body. Without expansion, "itau" matches but the accented "itaú" does not. After expansion, both match.

The expansion is purely lookup-based — no LLM call. It runs on the classifier's output before the retriever sees it.

## Subset matching policy

A keyword like "fed" is a partial match for "federal" — should it boost?

The reference implementation uses **word-boundary matching** by default:

```python
import re

def has_keyword(content, keyword):
    pattern = r'\b' + re.escape(keyword) + r'\b'
    return bool(re.search(pattern, content, re.IGNORECASE))
```

This means "fed" matches "fed" and "Fed" but not "federal" or "federated." For most queries this is correct. For queries that genuinely want partial matches ("inflation" → "inflationary"), the right answer is a longer explicit keyword in the boost list (the classifier should emit both forms) rather than disabling word-boundary matching globally.

Word-boundary matching avoids the most common source of false-positive boosts — partial-string coincidences. It does require careful keyword construction, but the classifier handles that by emitting full forms (the prompt instructs *"include the form a user would actually write, lowercase"*).

## Cost

Negligible. Keyword matching is local regex on already-retrieved document content, no additional DB queries, no LLM calls. Adds ~5ms per query for a 200-row pool with 10 keywords.

## Composition with other layers

For a query that triggers all four layers — *"What does Itaú say about Brazilian fiscal policy?"* — the contributions stack:

1. **Dynamic scoring** (L3): with `si = 0.6`, source weight rises to 0.48. High-priority Itaú documents get a 1.0 source_score multiplier vs 0.5 for normal sources. Itaú documents land ~+0.05 above normal-priority documents with identical content.
2. **Entity boost** (L4-ent): registry resolves "itau" → handles `[itau_macro, itau_economia, itau_bba]`. Pulls 5 (or 15 if `si ≥ threshold`) additional rows. These rows enter the rerank pool tagged `boost_source=entity_direct`.
3. **Institution boost** (L4-inst): with `boost_mode=direct_plus_mentions`, an additional 5 rows mentioning "itau" or its aliases get pulled, tagged `track=institution_cited`. The well-represented guard checks first — if direct ≥ 5 already, skips DIRECT lane.
4. **Keyword boost** (L4-kw): every row in the merged pool that contains "itau", "itaú", "fiscal", or other keywords gets +0.05 to +0.15. Threshold relaxes to 0.20.

The cumulative effect: Itaú documents on Brazilian fiscal policy are virtually guaranteed to land in the top-10 of the final ranking, even if some of them have weaker semantic similarity than competitor sell-side notes that talk *about* Itaú's view.

## Failure modes

**Stop words leak into `boost_keywords`.** "What", "about", "the" become boost keywords and match everything, washing out the boost. Fix: the classifier prompt explicitly excludes stop words; additionally, the booster code has a hardcoded stop-word filter as a safety net.

**Numeric keywords match unrelated numbers.** A query about "BCB rate decision 2026" produces a boost keyword "2026" that matches any document containing the year, including unrelated articles from earlier in the year. Fix: don't include bare years or common numbers as boost keywords; the classifier prompt instructs to exclude them.

**Threshold relaxation pulls in irrelevant material.** Lowering the threshold to 0.20 occasionally surfaces a document at 0.22 semantic + 0.05 keyword boost = 0.27 that is genuinely off-topic. Fix: when this happens reliably for a class of queries, raise the floor (e.g., to 0.30) at the cost of occasionally missing a borderline-but-correct document. The default 0.20 trades a small false-positive rate for a meaningful recall gain.

**Implicit expansion overfires.** "Itau" expands to "itau bba", which matches a document about Itaú BBA (the investment banking arm) when the user wanted Itaú Macro (the research desk). The document is from Itaú but not the right Itaú voice. Fix: split entity registry entries when the entity has materially distinct sub-units; ensure each sub-unit's aliases don't collide.

Read [`09-integration.md`](09-integration.md) for the final wiring step.
