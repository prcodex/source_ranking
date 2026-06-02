# 05 — Dynamic Scoring

This layer is the heart of the system. It's a single formula, and it's worth spending five minutes understanding it intuitively before reading the rest.

## The formula

For each retrieved document `d`, compute:

```
score(d) = semantic(d) * w_sem(si)
        + source_score(d) * w_src(si)
        + freshness(d) * w_fresh
```

where:

```
w_sem(si)   = 0.70 - si * 0.55
w_src(si)   = 0.15 + si * 0.55
w_fresh     = 0.15
```

and:

- `semantic(d)` ∈ [0, 1] — cosine similarity between query embedding and document embedding (the existing retriever output, normalized).
- `source_score(d)` ∈ [0, 1] — the source priority multiplier mapped onto the unit interval: `high → 1.0`, `normal → 0.5`, `low → 0.0`.
- `freshness(d)` ∈ [0, 1] — exponential decay of document age, `exp(-age_days * decay_rate)` with a `decay_rate` of roughly 0.02–0.05 per day depending on how recency-sensitive your domain is.
- `si` ∈ [0.0, 0.8] — `source_importance` from the classifier (see [`04-query-understanding.md`](04-query-understanding.md)).

The weights `w_sem(si)` and `w_src(si)` always sum to 0.85, leaving 0.15 for freshness. The two are linked: as `si` rises, weight flows from semantic to source at the same rate.

## Why this functional form

The formula is a **linear interpolation between two regimes**, parameterized by a single signal from the query.

When `si = 0.0` (pure content query):
```
w_sem = 0.70, w_src = 0.15, w_fresh = 0.15
```
Semantic dominates, source is a tiebreaker, freshness adds recency bias.

When `si = 0.8` (single-source query):
```
w_sem = 0.26, w_src = 0.59, w_fresh = 0.15
```
Source dominates, semantic ranks documents within the source's footprint, freshness still matters.

The behaviour is continuous, not branched. There's no `if si > threshold else …` step that creates discontinuities in ranking when `si` drifts across a boundary. A query that the classifier flags at `si = 0.55` and another at `si = 0.65` produce nearly identical rankings — the formula is smooth.

## Worked example — content query, `si = 0.0`

Query: *"What's happening with Brazilian inflation?"*

Documents retrieved:

| Doc | Source priority | semantic | freshness | source_score | w_sem | w_src | w_fresh | **score** |
|---|---|---|---|---|---|---|---|---|
| A — Itaú Macro note on IPCA | high | 0.74 | 0.95 | 1.0 | 0.70 | 0.15 | 0.15 | **0.812** |
| B — Folha headline summary | normal | 0.81 | 0.99 | 0.5 | 0.70 | 0.15 | 0.15 | **0.789** |
| C — Random tweet using same phrasing | low | 0.83 | 1.00 | 0.0 | 0.70 | 0.15 | 0.15 | **0.731** |
| D — Goldman EM macro note (older) | high | 0.68 | 0.30 | 1.0 | 0.70 | 0.15 | 0.15 | **0.672** |

Ranking: A > B > C > D.

The Itaú note wins on a balance of strong semantic, high priority, and good freshness. The Folha summary loses on priority despite winning on semantic; the random tweet loses harder on priority despite winning on semantic and freshness. The Goldman note loses on freshness despite high priority.

This is what plain semantic search would have ordered as C > B > A > D — exactly the failure case from [`01-problem.md`](01-problem.md).

## Worked example — source-named query, `si = 0.6`

Query: *"What does Goldman say about EM allocation?"*

Same documents:

| Doc | semantic | source_score | freshness | w_sem | w_src | w_fresh | **score** |
|---|---|---|---|---|---|---|---|
| A — Itaú Macro note | 0.40 | 1.0 | 0.95 | 0.37 | 0.48 | 0.15 | **0.770** |
| B — Folha summary | 0.55 | 0.5 | 0.99 | 0.37 | 0.48 | 0.15 | **0.591** |
| C — Random tweet | 0.62 | 0.0 | 1.00 | 0.37 | 0.48 | 0.15 | **0.379** |
| D — Goldman EM macro note | 0.70 | 1.0 | 0.30 | 0.37 | 0.48 | 0.15 | **0.784** |

Ranking: D > A > B > C.

Now the Goldman note wins despite weaker freshness — the source weight has nearly tripled, and the freshness penalty is no longer enough to overcome it. The Itaú note (also high priority) is a strong second.

Note that semantic similarity for D went UP (0.68 → 0.70) compared to the previous example, because the query mentions Goldman explicitly. This is **before** keyword boost or entity boost — those layers will push D's score even higher. The dynamic-scoring layer alone is already enough to surface the correct answer.

## Worked example — single-source query, `si = 0.8`

Query: *"Only Gavekal research on China"*

Documents (different set):

| Doc | semantic | source_score | freshness | w_sem | w_src | w_fresh | **score** |
|---|---|---|---|---|---|---|---|
| E — Gavekal Daily on China consumption | 0.62 | 1.0 | 0.90 | 0.26 | 0.59 | 0.15 | **0.886** |
| F — Bridgewater note on China (high pri) | 0.70 | 1.0 | 0.80 | 0.26 | 0.59 | 0.15 | **0.892** |
| G — News article quoting Gavekal | 0.75 | 0.5 | 1.00 | 0.26 | 0.59 | 0.15 | **0.638** |

Ranking: F > E > G.

**This is wrong.** The user said "only Gavekal" but the formula promoted Bridgewater because both are `high` priority and Bridgewater has stronger semantic alignment to "China." This is the limit of what the dynamic-scoring layer alone can do — source priority is **categorical**, not **identity-specific**.

The fix is the **entity boost lane** ([`06-entity-boost.md`](06-entity-boost.md)), which knows that "Gavekal" specifically means *fetch additional documents from `gavekal_research`* and pulls those rows directly. After the boost lane runs, E gains 5 additional Gavekal documents with the explicit `track=entity_direct` tag, and F gets correctly ranked below them despite its strong score.

## Why freshness gets a fixed weight

Freshness is held at 0.15 regardless of `si`. Other reasonable choices:

- Make freshness a function of `query_type` (longer for analytical queries, shorter for breaking-news queries). This is a worthwhile second-order improvement and the reference code in [`reference/scoring.py`](../reference/scoring.py) exposes the hook (`w_fresh(query_type)`) for it.
- Make freshness a function of `si` (more important when `si` is low, less important when `si` is high — under the theory that a single-source query is willing to tolerate older material from that source). This is plausible but produces complex ranking behaviour and was not adopted in production.

The flat 0.15 is the simplest defensible default. Worry about freshness sophistication later.

## Why `0.55` as the slope on `si`

The choice of `0.55` as the multiplier on `si` (so weight shifts from `0.70 → 0.26` and `0.15 → 0.59` across the range) was tuned empirically. The criterion was:

- At `si = 0.0`: behave indistinguishably from a plain semantic-+-priority blend.
- At `si = 0.8`: ensure that source priority can overcome a ~0.20 semantic similarity deficit. (The Goldman example above: D wins by 0.014 despite losing on freshness by 0.65 and tying on priority — semantic deficit of ~0.15 was overcome.)

You can re-tune by varying the slope and observing whether ranked outputs match human judgment on a held-out set of queries. The recommended procedure is:

1. Sample 30 queries spanning `si = 0.0` to `si = 0.8`.
2. For each query, hand-rank the top-10 retrieved documents to produce a target ordering.
3. Compute Kendall's tau between formula ranking and hand ranking, varying the slope in {0.40, 0.50, 0.55, 0.60, 0.70}.
4. Pick the slope that maximizes mean tau. Production landed on 0.55; your corpus may land elsewhere.

This re-tune costs ~3 hours of human time per quarter. It's worthwhile when launching, optional thereafter.

## Failure modes

**Source priorities not normalized.** If `source_score` is the raw multiplier (`1.5` for high) rather than a `[0, 1]` value, the formula's weights become meaningless. The reference code maps multipliers to `[0, 1]` before applying weights. Don't skip this step.

**Documents with missing freshness.** If `published_at` is null, the reference code falls back to `freshness = 0.5` (neutral). Some corpora have a meaningful share of undated documents (e.g., research notes timestamped only by ingestion). Setting freshness to 0 in those cases unfairly penalizes them; setting freshness to 1 unfairly promotes them. 0.5 is the right default.

**Semantic similarity stuck near zero.** If your embedding model is poorly calibrated for your corpus (a frequent issue with multilingual models on Portuguese-language content), semantic similarities cluster in the 0.05–0.25 range for everything. The formula still works but loses discriminative power on the semantic lane. Fix at the embedder, not at the scorer.

Read [`06-entity-boost.md`](06-entity-boost.md) for the next layer.
