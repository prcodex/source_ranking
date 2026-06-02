# 07 — Institution Boost

The entity boost in [`06-entity-boost.md`](06-entity-boost.md) handles named entities via direct-handle retrieval. Institution boost is a **specialization** for entities that meet two criteria:

1. The entity has an official publishing handle whose output is the canonical source of its views (a central bank, a major desk, a household-name commentator with an official account).
2. The entity is **also frequently discussed by others** in the corpus — quoted, paraphrased, or referenced. A central bank's statements get covered by ten different outlets; a meme account's views don't.

For these entities, splitting boost into two lanes — **direct authorship** and **mentions of the entity** — produces a richer retrieval than direct-only.

## The two lanes

**Lane 1 — DIRECT (FROM the institution)**

Same mechanism as the entity boost: filter on `source_id IN handles` and pull the most-relevant rows. Each row is tagged `track = "institution_direct"`. These rows represent the institution's primary voice — what it actually said, in its own words.

**Lane 2 — CITED (MENTIONS the institution)**

Filter on `content_text CONTAINS institution_name OR content_text CONTAINS any_alias`. Pull the most-relevant rows (sorted by semantic similarity to the query, not freshness alone — because mentions vary widely in relevance). Each row is tagged `track = "institution_cited"`. These rows represent secondary coverage — analysts, journalists, commentators discussing the institution's view.

The synthesizer can use the `track` tag to weight or attribute differently: *"The BCB said X (institution_direct), and the market interpreted this as Y (institution_cited)."* Without the tags, both rows look identical, and a synthesizer might attribute the analyst's interpretation back to the BCB.

## When to use institution boost vs entity boost

Use **entity boost** (direct only) when:

- The entity is an individual or boutique whose mentions are rare and uninformative.
- The entity's published output is the only signal that matters; what others say about them is noise.
- Production examples: individual analysts (Cembalest, Bremmer when in commentary mode), small research shops.

Use **institution boost** (direct + cited) when:

- The entity's statements are routinely re-published, paraphrased, or interpreted by others.
- You want both the official view AND the market reaction.
- Production examples: central banks (BCB, Fed, ECB), major investment banks (Goldman, JPM), policy makers (Treasury, IMF).

The dial that controls which mode applies is `boost_mode` in the entity registry (see [`06-entity-boost.md`](06-entity-boost.md)):

```yaml
- slug: bcb
  boost_mode: direct_plus_mentions
  ...

- slug: bremmer
  boost_mode: direct
  ...
```

## The "well-represented" short-circuit

When the regular semantic retrieval already returned a healthy number of documents from or about the institution, the institution boost is wasted effort — those rows are already in the pool. The reference implementation includes a guard:

```python
def needs_boost(institution_slug, base_retrieval):
    direct_count = sum(1 for d in base_retrieval if d.source_slug == institution_slug)
    mention_count = sum(1 for d in base_retrieval
                        if any(alias in d.content_text.lower()
                               for alias in registry[institution_slug].aliases))
    # If we already have 5+ direct or 10+ mentions, skip boost
    return direct_count < 5 and mention_count < 10
```

This is the `[InstitutionBoost] X well-represented: N mentions + M direct — no boost needed` log line in production. The guard saves a vector-DB query plus a content-search query per applicable institution per query — meaningful at sub-100ms-budget scale, especially when 2–3 institutions are mentioned in a query and most are already well-represented.

## Recency cutoff

Institution boost's CITED lane has a tendency to surface old material — a high-impact quote from a central banker two years ago that gets re-cited frequently in the corpus. For most queries, this is unwanted; the user wants current views.

The reference implementation supports a `recency_cutoff_hours` parameter that filters the CITED lane to documents within the last N hours. Default: 168 (one week) for current-events queries, no cutoff for analytical queries. The parameter is set from `ClassifierOutput.query_type`:

- `breaking` → 24 hours
- `current` → 168 hours (one week)
- `analytical` → no cutoff
- `historical` → no cutoff

The DIRECT lane is **not** filtered by recency — even an older official statement is part of the institution's canonical voice and should be retrievable.

## Worked flow

Query: *"What did the BCB say about Selic yesterday?"*

1. Classifier output: `entities=["bcb"]`, `topics=["selic", "monetary_policy"]`, `query_type=breaking`, `si=0.6`, `boost_keywords=["bcb", "selic", "copom", "banco central"]`.
2. Base retrieval returns 200 rows. Among them, 2 are from `source_id IN [bcb, bancocentral]` (direct), and 7 contain "bcb" in content (cited). The well-represented guard says: direct < 5, mention < 10 → boost runs.
3. Institution boost (DIRECT): filter `source_id IN [bcb, bancocentral]`, sort by published_at desc, limit 5. Returns 5 BCB tweets/statements from the last 48 hours, all tagged `track=institution_direct`.
4. Institution boost (CITED): filter `content_text CONTAINS ("bcb" OR "banco central" OR "bancocentral")` AND `published_at > now() - 24h`, sort by semantic similarity to query, limit 5. Returns 5 high-signal analyst commentary rows from the last day, all tagged `track=institution_cited`.
5. Merge: 200 base + 5 direct + 5 cited = 210 rows (after dedup, ~207). Re-score by the dynamic formula.
6. Top-10 sent to synthesizer. Synthesizer sees both `institution_direct` rows (the BCB's statements) and `institution_cited` rows (analyst takes), and writes a response attributing each correctly.

## Tagging downstream

The two `track` tags appear on `RetrievedDoc.metadata` and propagate to the synthesizer's context. A useful synthesizer prompt addition:

```markdown
Documents tagged track=institution_direct represent the institution's own
voice — quote them directly when attributing a view.

Documents tagged track=institution_cited represent secondary discussion
of the institution — attribute the *opinion* to the citing source, not
to the institution. If the cited source paraphrases the institution,
quote the paraphrase but mark it as the cited source's interpretation.
```

This is a small prompt change that produces a measurable improvement in source-attribution quality. The evaluator (Actor 7) can include a rubric line for *"did the response correctly distinguish direct from cited sources?"* and the rubric becomes meaningful once the synthesizer is reliably making the distinction.

## Failure modes

**Mentions match too liberally.** A short alias like "fed" matches "federal", "fed up", "federated" in content. Fix: use word-boundary regex (`\bfed\b`) rather than substring matching, and prefer multi-word aliases ("federal reserve") over single-word ones when possible.

**Mentions match too restrictively.** A misspelled or translated mention doesn't match. The BCB is sometimes written as "Bacen" in Portuguese coverage; the alias map must include both. Fix: maintain alias coverage as part of monthly maintenance; the `mention_count` log line surfaces misses indirectly (a query about BCB that returns 0 mentions in a corpus that obviously discusses the BCB indicates an alias gap).

**The CITED lane drowns out the DIRECT lane in synthesis.** If you pull 5 direct + 5 cited rows but the cited rows score higher (because they semantically align better with the query phrasing), the top-3 sent to the synthesizer ends up being all cited. Fix: explicit per-track quotas in the booster — guarantee at least 2 direct rows in the final cut, regardless of score, when DIRECT rows exist. The reference implementation supports this via a `min_direct` parameter.

**Cited recency cutoff kills useful old material on analytical queries.** A historical query about the BCB's evolution should pull old direct rows, not last-week-only. The query-type-driven cutoff handles this, but only if the classifier correctly categorizes the query. If you observe historical queries getting under-served, audit the classifier's query_type distribution.

Read [`08-keyword-boost.md`](08-keyword-boost.md) for the third boost lane.
