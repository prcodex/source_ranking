# 06 — Entity Boost

The dynamic-scoring formula in [`05-dynamic-scoring.md`](05-dynamic-scoring.md) reweights an existing retrieval. It does not change *which documents enter the retrieval pool* in the first place. When the user asks about *Gavekal*, and the top-200 vector-similarity retrieval doesn't include any Gavekal documents (because the corpus has thousands of "China" documents that are individually more semantically aligned with the query than any specific Gavekal note), no amount of reweighting will save you.

The **entity boost lane** fixes this. When the classifier identifies a named entity in the query, the booster makes a **second, targeted retrieval** that fetches additional documents directly from that entity's known handles, regardless of their semantic similarity. These rows are then merged into the original retrieval and rescored by the dynamic formula.

## The entity registry

A YAML file mapping canonical entity names to:

- A list of `handles` (the `source_id`s under which the entity publishes in the corpus)
- A list of `aliases` (alternative spellings users employ in queries)
- Optional tier metadata (e.g., `tier.macro`, `tier.fiscal`, `tier.politics`) for tier-aware re-ranking when an entity is relevant across multiple domains

Example entries:

```yaml
# config/entity_registry.yaml
entities:
  - slug: gavekal
    canonical_name: Gavekal Research
    aliases: [gavekal_research, gavekal_dragonomics, louiscavekal, gavekal_economics]
    handles: [gavekal_research, gavekal_dragonomics]
    tier:
      macro: 1
      china: 1
    boost_mode: direct
    max_boost_rows: 5
    importance_threshold: 0.5

  - slug: itau_macro
    canonical_name: Itaú Macro
    aliases: [itau_economia, itau_bba, itaubba, itaubarbara]
    handles: [itau_macro, itau_economia, itau_bba]
    tier:
      macro: 1
      brazil: 1
      fiscal: 1
    boost_mode: direct_plus_mentions
    max_boost_rows: 5
    importance_threshold: 0.5

  - slug: bremmer
    canonical_name: Ian Bremmer
    aliases: [ianbremmer, eurasiagroup, ian_bremmer]
    handles: [ianbremmer, eurasiagroup]
    tier:
      geopolitics: 1
    boost_mode: direct
    max_boost_rows: 3
    importance_threshold: 0.4
```

`boost_mode` controls whether the entity gets only direct-authorship boosts (`direct`) or both direct + mentions-of-the-entity boosts (`direct_plus_mentions`). The latter is appropriate for institutions whose views are often relayed via second-hand reporting (covered in detail in [`07-institution-boost.md`](07-institution-boost.md)).

`max_boost_rows` is the default cap on how many additional rows to pull. `importance_threshold` is the `si` value above which the cap is raised — production uses `max_boost_rows × 3` when `si >= importance_threshold`, so a source-importance signal of 0.6 pulls 15 rows for Gavekal instead of 5.

## The lookup flow

When `ClassifierOutput.entities` contains `["gavekal"]`:

1. Look up `gavekal` in the registry by slug, then by alias if no slug match.
2. If found, read `handles`, `boost_mode`, `max_boost_rows`, `importance_threshold`.
3. Compute the effective row cap: `max_boost_rows × 3` if `si >= importance_threshold`, else `max_boost_rows`.
4. Issue a vector-DB query filtering on `source_id IN handles`, ordered by freshness, with limit = effective row cap.
5. Tag each returned row with `boost_source = "entity_direct"`, `boost_slug = "gavekal"`.
6. Merge into the original retrieval, deduplicating by `doc_id`.
7. Re-score using the dynamic formula (semantic similarity is recomputed for the new rows; source_score and freshness are computed normally).

The result is that the synthesizer receives a retrieval that **always contains the named entity's recent documents**, even when those documents would have been outside the top-200 cutoff under pure semantic.

## Why a separate registry instead of derived-from-priority

In principle, the priority registry from [`03-priority-registry.md`](03-priority-registry.md) already lists handles by priority. Why not just say "if the query names an entity, pull all `high`-priority documents from any handle that matches an alias"?

Three reasons:

1. **Aliasing is entity-specific.** "Goldman" maps to `goldman_sachs_research`, `goldman_macro`, `goldman_em` — different handles. The priority registry would need a parallel alias table, which is what the entity registry is.
2. **Tier metadata is entity-specific.** Gavekal is `tier.china = 1`; Itaú is `tier.brazil = 1`. The priority registry's three-level priority can't encode this. When you eventually want to do tier-aware reranking (e.g., for a China query, prefer entities tagged `tier.china = 1` over generic high-priority sources), the entity registry has the structure for it; the priority registry doesn't.
3. **`boost_mode` and `max_boost_rows` are entity-specific.** Institutional sources benefit from mentions boost; individual analysts don't. Major desks merit larger row caps; minor ones don't. The entity registry holds these per-entity dials; the priority registry has no place for them.

The two registries are complementary. The priority registry covers **every source in the corpus** at coarse granularity. The entity registry covers **the named entities users mention in queries** at fine granularity. There's overlap — high-priority sources are usually entries in both — but the function is distinct.

## Tier-aware entity boost

The `tier` map on each entity enables a more discriminating boost when multiple entities match a query alias. Suppose a user asks *"Itaú on China"*. The naive matcher finds `itau_macro` (Itaú Macro desk) via alias `itau`. But what if there's also an `itau_fx_strategy` entity that publishes more on China-FX themes specifically? The booster can compare `tier.china` across the candidates:

- `itau_macro` — `tier.macro=1, tier.brazil=1, tier.fiscal=1, tier.china=99`
- `itau_fx_strategy` — `tier.macro=2, tier.fx=1, tier.china=1`

When the query topic includes China, the booster sorts candidates by `tier.china` ascending and picks the lowest (highest priority for that topic). `itau_fx_strategy` wins for the China-specific query; `itau_macro` would win for a generic *"Itaú on Brazilian fiscal"* query.

This is the **highest-tier wins** logic that the production code uses (`_fb_matches.sort(key=lambda e: e.tier.get(topic, 99))`). It's optional — you can skip tier maps entirely and the booster will work fine with the first-matching-alias logic — but adopting tier maps is the natural next step once your entity registry has more than ~20 entries.

## Cost

The entity boost adds **one additional vector-DB query per query that names an entity** (so roughly 30–50% of queries in a typical macro/markets corpus). Each query is a filter-on-source_id-with-limit, which is fast on a properly indexed table — sub-100ms on LanceDB with a `source_id` index, no embedding required.

The extra rows feed into the synthesizer, which does cost more Sonnet tokens. If the unboosted retrieval returns 30 rows for the synthesizer and the boost adds 15, the synthesizer processes 1.5× more context. On Sonnet pricing, this is the dominant cost — roughly $0.003 → $0.005 per query. For a typical workload, the marginal cost is acceptable; if it isn't, the entity-boost row cap is the dial to lower first.

## Failure modes

**Alias collisions.** Two entities share an alias (e.g., "bremmer" matches both Ian Bremmer the individual and Bremmer Capital the fund). The first-match-wins logic produces silent wrong-entity boosts. Fix: audit the alias map for duplicates at startup, and prefer specific-string aliases over short ones (`ianbremmer` rather than `bremmer`).

**Stale handles.** An entity rotates Twitter handles or rebrands; the registry doesn't update; queries about that entity return zero boosted rows. Fix: include a check in monthly maintenance that for each entity in the registry, the most recent document fetched via boost is less than ~60 days old. Stale entries flag for review.

**Row cap saturation.** A heavy producer (e.g., a daily-publishing research desk) saturates the boost row cap with the most recent N notes, crowding out older notes that may be more relevant to the specific query. Fix: re-rank the boosted rows by score (semantic + freshness only — source is already implicit) before truncating to `max_boost_rows`, rather than truncating by raw freshness. The reference implementation does this.

**Topic-tier mismatch.** The tier-aware logic picks the wrong entity because the classifier extracted a topic that the registry doesn't have a tier for. Fix: extend the tier map. The registry tolerates new tier keys at any time — entries without the key default to tier 99 (lowest priority).

Read [`07-institution-boost.md`](07-institution-boost.md) for the institution-specific variant.
