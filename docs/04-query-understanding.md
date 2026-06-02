# 04 — Query Understanding (extending the classifier)

The classifier (m3xabr-core Actor 1) already runs a Haiku call to extract query metadata. This layer adds **two new fields** to its output: `source_importance` (a float between 0.0 and 0.8) and `boost_keywords` (a list of lowercase strings). Both come from the same Haiku call — no second LLM invocation.

The cost is a few additional tokens in the prompt and a few additional tokens in the JSON output. On Haiku at production volumes, the marginal cost is approximately **$0.00002 per query**.

## What `source_importance` means

`source_importance` (henceforth `si`) is the answer to the question: **"How much does the identity of the author matter for this query, relative to the content?"**

- `si = 0.0` — pure content query. The user wants the best answer regardless of who wrote it. Examples: *"Fed rate outlook"*, *"What's happening in Brazilian fiscal policy?"*, *"Why did the BRL strengthen yesterday?"*
- `si = 0.3 – 0.5` — content with a preferred slant. The user is open to multiple sources but has hinted at a preference. Examples: *"Sell-side view on EM inflows"*, *"Hedge fund positioning in oil"* (genre or category named, not a specific entity).
- `si = 0.6` — a single source or institution is explicitly named. The user wants that source's view. Examples: *"What does Goldman say about EM allocation?"*, *"What is the BCB saying about Selic?"*, *"Bremmer's take on Iran"*.
- `si = 0.7 – 0.8` — exclusive or near-exclusive single-source query. The user signals they only care about one source. Examples: *"Only Gavekal research on China"*, *"Show me everything Cembalest has written this month"*. Cap at 0.8 — never 1.0, because pure source-only queries are better handled by an explicit filter, not by scoring.

The 0.0 – 0.8 range is deliberate. The dynamic scoring formula in [`05-dynamic-scoring.md`](05-dynamic-scoring.md) multiplies `si` by 0.55 to derive a weight shift. With max `si = 0.8`, the maximum shift is 0.44 — meaning the semantic weight can drop from 0.70 to 0.26 and the source weight can rise from 0.15 to 0.59. That's a large but not extreme reweighting, leaving room for freshness and other factors. Allowing `si = 1.0` would zero out semantic, which empirically produces worse results because even single-source queries benefit from semantic ranking *within* the named source's documents.

## What `boost_keywords` means

A list of lowercase strings that the classifier identifies as **lexically important for retrieval**. The classifier's prompt instructs it to include:

- Named entities (people, institutions, instruments) — *"goldman"*, *"itau"*, *"bcb"*, *"selic"*, *"copom"*
- Distinctive content nouns from the query — *"fiscal"*, *"inflation"*, *"em"*, *"allocation"*
- Implicit synonyms when the classifier knows them (handled by `_add_implicit_boosts` in the reference code) — when the classifier extracts *"goldman"* it also adds *"gs"* and *"goldman sachs"*; *"itau"* adds *"itaú"* and *"itau macro"*.

These keywords are used by the keyword-boost lane (see [`08-keyword-boost.md`](08-keyword-boost.md)) to:

- Additively boost the score of any retrieved document whose content contains any of the keywords.
- Lower the minimum-similarity threshold from 0.50 to 0.20 for boosted documents, ensuring that a document with a strong keyword match isn't filtered out for mediocre semantic similarity.

## Schema extension

In `m3xabr_core/schemas.py`, the `ClassifierOutput` model gains two fields:

```python
class ClassifierOutput(BaseModel):
    # ... existing fields (query_type, time_window, entities, topics, language) ...

    # NEW
    source_importance: float = Field(default=0.0, ge=0.0, le=0.8)
    boost_keywords: list[str] = Field(default_factory=list)
```

Both fields are defaulted, so a classifier that doesn't emit them (or fails to validate) degrades gracefully — the system falls back to `si = 0.0` and no keyword boost, which is equivalent to plain semantic search.

## Prompt extension

The classifier prompt at `config/classifier_prompt.md` needs to instruct Haiku on how to fill the new fields. The minimum addition is a short specification plus a few examples that anchor the model's calibration. The reference content for the addition is in [`reference/classifier_addendum.py`](../reference/classifier_addendum.py) (as a string constant suitable for splicing into the markdown prompt), excerpted here:

```markdown
## Field: source_importance (float 0.0 – 0.8)

How much does the source identity matter for this query, relative to the content?

- 0.0 — pure content query, source doesn't matter
- 0.6 — one source or institution is named (Goldman, BCB, Itaú, Bremmer)
- 0.8 — query restricts to a single source ("only", "exclusively", "show me everything from")

## Field: boost_keywords (list of lowercase strings)

Keywords to additively boost in retrieval. Include:
- Names of any people, institutions, or instruments mentioned
- 1–3 distinctive content nouns from the query
- Lowercase, no punctuation

## Calibration examples

- "Fed rate outlook" → source_importance=0.0, boost_keywords=["fed", "rates"]
- "What does Goldman think about inflation?" → source_importance=0.6, boost_keywords=["goldman", "inflation"]
- "Iran war impact on oil" → source_importance=0.0, boost_keywords=["iran", "oil"]
- "What is Bremmer saying about Iran?" → source_importance=0.6, boost_keywords=["bremmer", "iran"]
- "What happened over last 12h on Iran war?" → source_importance=0.0, boost_keywords=["iran"]
- "Hormuz shipping and oil supply" → source_importance=0.0, boost_keywords=["hormuz", "oil", "shipping"]
- "Only Gavekal research on China" → source_importance=0.8, boost_keywords=["gavekal", "china"]
- "What does BCB say about Selic?" → source_importance=0.6, boost_keywords=["bcb", "selic", "copom"]
- "Brazilian fiscal outlook second half" → source_importance=0.0, boost_keywords=["fiscal", "brasil"]
```

The Haiku calibration examples are the load-bearing part of the prompt. The model's behaviour on borderline cases (genre-named queries like "sell-side view on...", "buyside positioning") is driven heavily by how those cases are bracketed by the examples. If you observe miscalibration on a class of query you care about, add an example for that class and the classifier will reliably re-anchor.

## Implicit boosts (zero-cost expansions)

When the classifier outputs entities or sources, the post-processing in `_add_implicit_boosts` enriches `boost_keywords` with known aliases without another LLM call. This handles cases the model didn't explicitly write out:

- Classifier emits `"goldman"` → post-processing adds `"gs"`, `"goldman sachs"`.
- Classifier emits `"itau"` → post-processing adds `"itaú"`, `"itau macro"`, `"itau bba"`.
- Classifier emits `"bcb"` → post-processing adds `"banco central"`, `"banco central do brasil"`.

The alias map lives in the entity registry (see [`06-entity-boost.md`](06-entity-boost.md)). The classifier never needs to learn this — it only emits the canonical entity name.

## Failure modes

**The classifier outputs a `source_importance` that doesn't match the calibration.** This is the most common issue at launch. Diagnosis: pull 50 queries from a day's logs, classify them by hand, compare. If the model is systematically too high or too low, adjust the examples in the prompt — adding two or three more examples on the underrepresented region of the scale shifts behaviour reliably. If it's noisy rather than biased, lower the prompt's temperature (the reference code uses `temperature=0.0`).

**`boost_keywords` gets too noisy.** The model includes filler words like "what", "about", "now". Add an explicit instruction to the prompt: *"Do not include common function words (what, about, the, in, on, for, with, now, today)."* The fix lands within one prompt iteration.

**Implicit alias map is incomplete.** A new source is added to the corpus, classifier extracts it correctly, but the alias map doesn't know about it. Result: keyword boost only fires on the canonical form, not the alternate spellings used in the corpus. Fix: add to the alias map. This is normal corpus maintenance; budget ~5 minutes per new source.

Move on to [`05-dynamic-scoring.md`](05-dynamic-scoring.md) for how `source_importance` and `boost_keywords` are consumed.
