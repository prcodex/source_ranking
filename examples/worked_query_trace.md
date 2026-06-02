# Worked Query Trace — three queries through the full stack

This document walks three queries through every layer of the source-ranking pipeline, showing the actual values at each stage so you can verify your implementation matches the production behaviour described in the docs.

The three queries are chosen to exercise different `source_importance` regimes and different boost lanes:

1. **Query A** — pure content (`si = 0.0`), keyword boost only
2. **Query B** — institution-named (`si = 0.6`), institution + entity + keyword boosts
3. **Query C** — single-source restricted (`si = 0.8`), entity boost dominates

Assume the corpus uses the [`source_priority.yaml`](source_priority.yaml) and a matching `entity_registry.yaml` derived from it.

---

## Query A — *"Brazilian fiscal outlook for the second half"*

### Stage 1 — Classifier output

```json
{
  "query_type": "analytical",
  "language": "en",
  "entities": [],
  "topics": ["fiscal", "brasil"],
  "source_importance": 0.0,
  "boost_keywords": ["fiscal", "brasil", "outlook"]
}
```

### Stage 2 — Retriever (semantic + freshness only initially)

Top-5 returned by raw vector search against the Brazilian corpus:

| Rank | doc_id | source_id | semantic | published_at |
|---|---|---|---|---|
| 1 | doc_1042 | folha_mercado | 0.71 | 2 days ago |
| 2 | doc_1015 | randombr_curious | 0.78 | yesterday |
| 3 | doc_1098 | itau_macro | 0.62 | 6 days ago |
| 4 | doc_0998 | infomoney | 0.65 | 3 days ago |
| 5 | doc_0871 | xp_research | 0.59 | 12 days ago |

### Stage 3 — Dynamic scoring (`si = 0.0`)

Weights: `w_sem = 0.70, w_src = 0.15, w_fresh = 0.15`.

| Rank | doc_id | semantic | source_score | freshness | **score** |
|---|---|---|---|---|---|
| 1 | doc_1042 | 0.71 | 1.0 (high) | 0.96 | **0.788** |
| 2 | doc_1098 | 0.62 | 1.0 (high) | 0.89 | **0.717** |
| 3 | doc_0998 | 0.65 | 0.5 (normal) | 0.94 | **0.671** |
| 4 | doc_0871 | 0.59 | 1.0 (high) | 0.79 | **0.681** |
| 5 | doc_1015 | 0.78 | 0.0 (low) | 0.98 | **0.693** |

Ranking after Stage 3: doc_1042 > doc_1015 > doc_0871 > doc_1098 > doc_0998.

The low-priority retail commentator (doc_1015) lost ground but is still in 2nd. Itaú and XP rose past Infomoney despite weaker semantic.

### Stage 4 — Keyword boost

Boost keywords: `["fiscal", "brasil", "outlook"]`. All five documents contain at least "fiscal":

| Rank | doc_id | base | keyword hits | boost | **final** |
|---|---|---|---|---|---|
| 1 | doc_1042 | 0.788 | 2 (fiscal, brasil) | +0.10 | **0.888** |
| 2 | doc_1098 | 0.717 | 3 (fiscal, brasil, outlook) | +0.15 | **0.867** |
| 3 | doc_0871 | 0.681 | 2 | +0.10 | **0.781** |
| 4 | doc_0998 | 0.671 | 1 | +0.05 | **0.721** |
| 5 | doc_1015 | 0.693 | 1 | +0.05 | **0.743** |

Final ranking: doc_1042 > doc_1098 > doc_0871 > doc_1015 > doc_0998.

Itaú (doc_1098) jumped from 4th raw to 2nd final — the three-keyword match pushed it past the Folha headline. The retail commentator dropped further behind.

### Stage 5 — Booster

No entities in classifier output → entity lane skipped. No institution lane (no `direct_plus_mentions` entity matched). Final retrieval is the post-keyword-boost ranking.

---

## Query B — *"What did the BCB say about Selic this week?"*

### Stage 1 — Classifier output

```json
{
  "query_type": "current",
  "language": "en",
  "entities": ["bcb"],
  "topics": ["selic", "monetary_policy"],
  "source_importance": 0.6,
  "boost_keywords": ["bcb", "selic", "copom"]
}
```

After implicit boost expansion (entity registry aliases for `bcb`):

```
boost_keywords = ["bcb", "selic", "copom", "bancocentral",
                  "banco central", "bcbbrasil"]
```

### Stage 2 — Base retrieval

Top-5 from raw vector search:

| Rank | doc_id | source_id | semantic | published_at |
|---|---|---|---|---|
| 1 | doc_2210 | valor_economico | 0.74 | 1 day ago |
| 2 | doc_2235 | infomoney | 0.71 | 2 days ago |
| 3 | doc_2189 | folha_mercado | 0.68 | 3 days ago |
| 4 | doc_2301 | bcb | 0.65 | yesterday |
| 5 | doc_2245 | itau_macro | 0.63 | 4 days ago |

The BCB's own statement (doc_2301) ranks 4th in raw semantic — outranked by analyst commentary that uses query-phrasing more naturally.

### Stage 3 — Dynamic scoring (`si = 0.6`)

Weights: `w_sem = 0.37, w_src = 0.48, w_fresh = 0.15`.

| Rank | doc_id | semantic | source_score | freshness | **score** |
|---|---|---|---|---|---|
| 1 | doc_2210 | 0.74 | 1.0 (high) | 0.98 | **0.901** |
| 2 | doc_2301 | 0.65 | 1.0 (high) | 0.99 | **0.870** |
| 3 | doc_2189 | 0.68 | 1.0 (high) | 0.96 | **0.876** |
| 4 | doc_2245 | 0.63 | 1.0 (high) | 0.94 | **0.854** |
| 5 | doc_2235 | 0.71 | 0.5 (normal) | 0.97 | **0.647** |

Ranking: doc_2210 > doc_2189 > doc_2301 > doc_2245 > doc_2235.

BCB rose from 4th to 3rd just from the dynamic weights — the normal-priority Infomoney that was 2nd has dropped to 5th. Not yet enough to land BCB on top.

### Stage 4 — Keyword boost

| Rank | doc_id | base | hits | boost | **post-keyword** |
|---|---|---|---|---|---|
| 1 | doc_2210 | 0.901 | 2 (selic, bcb) | +0.10 | **1.001** |
| 2 | doc_2301 | 0.870 | 3 (bcb, selic, copom) | +0.15 | **1.020** |
| 3 | doc_2189 | 0.876 | 2 | +0.10 | **0.976** |
| 4 | doc_2245 | 0.854 | 2 | +0.10 | **0.954** |
| 5 | doc_2235 | 0.647 | 2 | +0.10 | **0.747** |

Post-keyword ranking: **doc_2301 (BCB) > doc_2210 > doc_2189 > doc_2245 > doc_2235**.

The BCB's own statement is now #1 — three keyword hits put it over the top.

### Stage 5 — Booster (institution lane)

`bcb` resolved in entity registry: `boost_mode = direct_plus_mentions`, `max_boost_rows = 5`, `importance_threshold = 0.5`. With `si = 0.6 ≥ 0.5`, effective cap = 15.

Well-represented check: base retrieval has 1 doc from BCB handles (doc_2301), 3 mentions. Both under thresholds — boost runs.

**DIRECT lane** fetches up to 15 most-recent rows from `source_id IN [bcb, bancocentral]`, sorted by published_at desc. Returns 7 rows (BCB doesn't tweet 15 times per week). Each tagged `track=institution_direct`.

**CITED lane**, with `query_type=current → cutoff=168h`, fetches up to 15 rows containing any BCB alias in content, sorted by semantic similarity. Returns 12 rows tagged `track=institution_cited`.

After dedup against base retrieval, +6 direct rows and +10 cited rows are added.

### Final retrieval to synthesizer

Top-10 sent to Sonnet:

| Rank | doc_id | source | semantic | final | track |
|---|---|---|---|---|---|
| 1 | doc_2301 | bcb | 0.65 | 1.020 | institution_direct |
| 2 | doc_5512 | bcb | 0.45 | 0.95* | institution_direct (boosted) |
| 3 | doc_2210 | valor_economico | 0.74 | 1.001 | (base) |
| 4 | doc_5601 | bcb | 0.42 | 0.91* | institution_direct (boosted) |
| 5 | doc_2189 | folha_mercado | 0.68 | 0.976 | (base) |
| 6 | doc_4422 | itau_macro | 0.51 | 0.85* | institution_cited (boosted) |
| 7 | doc_2245 | itau_macro | 0.63 | 0.954 | (base) |
| 8 | doc_4501 | xp_research | 0.49 | 0.82* | institution_cited (boosted) |
| 9 | doc_2235 | infomoney | 0.71 | 0.747 | (base) |
| 10 | doc_5489 | bcb | 0.40 | 0.88* | institution_direct (boosted) |

\* boosted rows recomputed with the dynamic formula on full re-score.

The synthesizer receives 4 BCB-direct rows and 2 institution-cited rows. The synthesizer prompt knows the distinction (see [`07-institution-boost.md`](../docs/07-institution-boost.md)) and writes the response attributing direct rows to the BCB and cited rows to the analysts who interpreted them.

---

## Query C — *"Only Gavekal research on China"*

### Stage 1 — Classifier output

```json
{
  "query_type": "analytical",
  "language": "en",
  "entities": ["gavekal"],
  "topics": ["china"],
  "source_importance": 0.8,
  "boost_keywords": ["gavekal", "china", "research"]
}
```

After implicit expansion:

```
boost_keywords = ["gavekal", "china", "research", "gavekal_research",
                  "gavekal_dragonomics", "louiscavekal"]
```

### Stage 2 — Base retrieval

Top-5 from raw vector search (assume corpus has many China-focused notes from multiple sources):

| Rank | doc_id | source_id | semantic | published_at |
|---|---|---|---|---|
| 1 | doc_3320 | bridgewater_daily | 0.79 | 5 days ago |
| 2 | doc_3398 | goldman_macro | 0.75 | 7 days ago |
| 3 | doc_3415 | gavekal_research | 0.65 | 3 days ago |
| 4 | doc_3290 | randombr_curious | 0.71 | 2 days ago |
| 5 | doc_3380 | gavekal_dragonomics | 0.62 | 6 days ago |

Gavekal docs are 3rd and 5th in raw semantic.

### Stage 3 — Dynamic scoring (`si = 0.8`)

Weights: `w_sem = 0.26, w_src = 0.59, w_fresh = 0.15`.

| Rank | doc_id | semantic | source_score | freshness | **score** |
|---|---|---|---|---|---|
| 1 | doc_3320 | 0.79 | 1.0 (assumed high) | 0.91 | **0.929** |
| 2 | doc_3398 | 0.75 | 1.0 (high) | 0.87 | **0.916** |
| 3 | doc_3415 | 0.65 | 1.0 (high) | 0.95 | **0.901** |
| 4 | doc_3380 | 0.62 | 1.0 (high) | 0.89 | **0.884** |
| 5 | doc_3290 | 0.71 | 0.0 (low) | 0.97 | **0.331** |

Ranking after Stage 3: doc_3320 > doc_3398 > doc_3415 > doc_3380 > doc_3290.

Even with `si = 0.8` weighting source heavily, the formula cannot distinguish Gavekal-the-named-entity from Bridgewater-and-Goldman-also-high-priority. They all share `source_score = 1.0`. Bridgewater wins on semantic.

**This is the failure case dynamic scoring alone cannot solve.** Reading [`05-dynamic-scoring.md`](../docs/05-dynamic-scoring.md) called this out explicitly.

### Stage 4 — Keyword boost

| Rank | doc_id | base | hits | boost | **post-keyword** |
|---|---|---|---|---|---|
| 1 | doc_3320 | 0.929 | 1 (china) | +0.05 | **0.979** |
| 2 | doc_3398 | 0.916 | 1 (china) | +0.05 | **0.966** |
| 3 | doc_3415 | 0.901 | 3 (gavekal, china, research) | +0.15 | **1.051** |
| 4 | doc_3380 | 0.884 | 3 | +0.15 | **1.034** |
| 5 | doc_3290 | 0.331 | 1 | +0.05 | **0.381** |

Post-keyword ranking: **doc_3415 > doc_3380 > doc_3320 > doc_3398 > doc_3290**.

Gavekal documents now win 1st and 2nd, by exploiting the keyword match. The keyword boost is what makes "only" queries work — the user mentioned "Gavekal" lexically; the document mentions "Gavekal" lexically; the boost rewards that lexical alignment in a way that pure semantic embeddings often miss.

### Stage 5 — Booster (entity lane)

`gavekal` resolved: `boost_mode = direct`, `max_boost_rows = 5`, `importance_threshold = 0.5`. With `si = 0.8`, effective cap = 15.

Well-represented check: base retrieval has 2 docs from gavekal handles, 0 mentions of just-mentions. Direct < 5 → boost runs.

**DIRECT lane**: filter `source_id IN [gavekal_research, gavekal_dragonomics]`, sort by published_at desc, limit 15. Returns 12 rows. After dedup with base retrieval, +10 rows added.

No CITED lane (boost_mode = direct).

### Final retrieval to synthesizer

Top-10:

| Rank | doc_id | source_id | track | final score |
|---|---|---|---|---|
| 1 | doc_3415 | gavekal_research | base + keyword | 1.051 |
| 2 | doc_3380 | gavekal_dragonomics | base + keyword | 1.034 |
| 3 | doc_6712 | gavekal_research | entity_direct (boosted) | 0.98* |
| 4 | doc_6688 | gavekal_dragonomics | entity_direct (boosted) | 0.97* |
| 5 | doc_3320 | bridgewater_daily | base | 0.979 |
| 6 | doc_6701 | gavekal_research | entity_direct (boosted) | 0.96* |
| 7 | doc_3398 | goldman_macro | base | 0.966 |
| 8 | doc_6679 | gavekal_research | entity_direct (boosted) | 0.95* |
| 9 | doc_6655 | gavekal_dragonomics | entity_direct (boosted) | 0.93* |
| 10 | doc_6612 | gavekal_research | entity_direct (boosted) | 0.91* |

Of the top-10, **8 are Gavekal**. The synthesizer can honour the "only Gavekal" instruction faithfully because the retrieval pool is mostly Gavekal.

If the user's instruction had been **stricter** ("ONLY Gavekal, nothing else"), the synthesizer's prompt would filter to `boost_source ∈ {entity_direct, base-with-gavekal-source}` before composing the answer. The booster provides the metadata to make that filter possible.

---

## Verification checklist

If your implementation matches the architecture, you should see:

✅ Query A returns Itaú or Folha in top-2; the retail commentator is below position 3.

✅ Query B returns the BCB's own statement at top-1 or top-2, with `track=institution_direct` metadata. Cited rows are present but ranked below direct rows.

✅ Query C returns ≥ 6 Gavekal rows in top-10. Non-Gavekal rows are in the bottom half.

If any of these fail, walk back through the layer outputs and find where the expected ranking diverges. Most issues land in one of three places:

1. **Classifier emitting wrong `si`** — prompt iteration on the affected query class.
2. **Entity registry missing an entity or handle** — add the entry.
3. **Boost row cap too low** — raise `max_boost_rows` for the entity, or lower the `importance_threshold`.
