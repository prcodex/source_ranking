"""Drop-in extensions for ``m3xabr_core/actors/classifier.py`` and
``config/classifier_prompt.md``.

This file is intentionally not a complete classifier — it is the *delta* you
apply to the existing m3xabr-core classifier to teach it the
``source_importance`` and ``boost_keywords`` fields.

Three concrete artifacts are exported:

1. :data:`SCHEMA_FIELDS` — the additions to ``ClassifierOutput``.
2. :data:`PROMPT_ADDENDUM` — the markdown block to append to
   ``config/classifier_prompt.md``.
3. :func:`add_implicit_boosts` — the post-processing helper that expands
   ``boost_keywords`` using the entity registry's alias map.
"""
from __future__ import annotations

from typing import Iterable


# ─── 1. Schema additions ──────────────────────────────────────────────────────
#
# Drop these onto your ``ClassifierOutput`` model. The defaults are chosen so
# the rest of the pipeline degrades gracefully when the classifier omits or
# fails to validate the new fields.
SCHEMA_FIELDS = '''
    # New for source-ranking — see source_ranking/docs/04
    source_importance: float = Field(default=0.0, ge=0.0, le=0.8)
    boost_keywords: list[str] = Field(default_factory=list)
'''


# ─── 2. Prompt addendum ───────────────────────────────────────────────────────
#
# Append to ``config/classifier_prompt.md``. The calibration examples are the
# load-bearing part; iterate by adding new examples for edge cases that
# observe miscalibration in your corpus.
PROMPT_ADDENDUM = """
## Field: `source_importance` (float 0.0 – 0.8)

How much does the **identity of the author** matter for this query, relative
to the content itself?

- **0.0** — pure content query. The user wants the best answer regardless of
  who wrote it. (e.g. "Fed rate outlook", "fiscal policy 2H")
- **0.3 – 0.5** — content with a preferred slant. Genre or category is named
  but no specific entity. (e.g. "sell-side view on EM", "hedge fund positioning")
- **0.6** — a single source, institution, or named analyst is explicitly
  mentioned. (e.g. "What does Goldman say...", "BCB on Selic", "Bremmer's take")
- **0.7 – 0.8** — query restricts to a single source. Words like "only",
  "just", "show me everything from". (e.g. "Only Gavekal on China")

Cap at 0.8. Never emit values above 0.8 — true source-exclusive queries are
better served by an explicit filter than by scoring.

## Field: `boost_keywords` (list of lowercase strings)

Keywords to additively boost in retrieval. Include:

- **Named entities** mentioned in the query (people, institutions, instruments)
- **1–3 distinctive content nouns** from the query (the substantive topic words)
- All lowercase, no punctuation

**Do not include** common function words: what, about, the, in, on, for,
with, now, today, this, that, and, or, but, as, of, to. Do not include bare
years or common numbers.

## Calibration examples

```
"Fed rate outlook"
→ source_importance=0.0, boost_keywords=["fed", "rates", "outlook"]

"What does Goldman think about inflation?"
→ source_importance=0.6, boost_keywords=["goldman", "inflation"]

"Iran war impact on oil"
→ source_importance=0.0, boost_keywords=["iran", "oil", "war"]

"What is Bremmer saying about Iran?"
→ source_importance=0.6, boost_keywords=["bremmer", "iran"]

"What happened over last 12h on Iran war?"
→ source_importance=0.0, boost_keywords=["iran", "war"]

"Hormuz shipping and oil supply"
→ source_importance=0.0, boost_keywords=["hormuz", "oil", "shipping"]

"Only Gavekal research on China"
→ source_importance=0.8, boost_keywords=["gavekal", "china", "research"]

"What did the BCB say about Selic?"
→ source_importance=0.6, boost_keywords=["bcb", "selic", "copom"]

"Brazilian fiscal outlook second half"
→ source_importance=0.0, boost_keywords=["fiscal", "brasil"]

"Show me everything Cembalest wrote this month"
→ source_importance=0.8, boost_keywords=["cembalest", "jp morgan"]
```
"""


# ─── 3. Implicit boost expansion ──────────────────────────────────────────────
#
# Expands the classifier's emitted boost_keywords using the alias map from the
# entity registry. Runs after the classifier returns, before the retriever sees
# the output. No LLM call.

def add_implicit_boosts(
    classifier_output,
    entity_registry,
) -> None:
    """Mutate ``classifier_output.boost_keywords`` in place, adding aliases.

    For each entity that appears in ``classifier_output.entities``:

    - Look up the entity in the registry (by slug, alias, or handle).
    - Add its canonical slug, its aliases, and its handles to boost_keywords.

    Idempotent. Deduplicated. Lowercased.
    """
    if not getattr(classifier_output, "entities", None):
        return
    existing = {k.lower() for k in (classifier_output.boost_keywords or [])}
    out = list(existing)
    for ent_name in classifier_output.entities:
        entry = entity_registry.resolve(ent_name)
        if entry is None:
            continue
        candidates = [entry.slug, *entry.aliases, *entry.handles]
        for c in candidates:
            k = c.strip().lower()
            if k and k not in existing:
                out.append(k)
                existing.add(k)
    classifier_output.boost_keywords = out


# ─── Validation: a tiny smoke test you can run after wiring this in ──────────

CALIBRATION_EXAMPLES: list[tuple[str, float, list[str]]] = [
    ("Fed rate outlook",                                0.0,  ["fed", "rates"]),
    ("What does Goldman think about inflation?",        0.6,  ["goldman", "inflation"]),
    ("Iran war impact on oil",                          0.0,  ["iran", "oil"]),
    ("What is Bremmer saying about Iran?",              0.6,  ["bremmer", "iran"]),
    ("Hormuz shipping and oil supply",                  0.0,  ["hormuz", "oil"]),
    ("Only Gavekal research on China",                  0.8,  ["gavekal", "china"]),
    ("What did the BCB say about Selic?",               0.6,  ["bcb", "selic"]),
    ("Brazilian fiscal outlook second half",            0.0,  ["fiscal", "brasil"]),
]
"""(query, expected_si, expected_keywords_subset).

Use this in a unit test: run the classifier on each query, assert the
emitted ``source_importance`` is within ±0.1 of the expected value, and
that every expected keyword is present in the emitted ``boost_keywords``.
"""


def validate_calibration(classifier, tolerance: float = 0.1) -> list[str]:
    """Run the calibration examples; return a list of failure messages."""
    failures: list[str] = []
    for query, exp_si, exp_kw in CALIBRATION_EXAMPLES:
        out = classifier.classify(query)
        if abs(out.source_importance - exp_si) > tolerance:
            failures.append(
                f"si mismatch on {query!r}: got {out.source_importance:.2f}, "
                f"expected {exp_si:.2f}"
            )
        emitted = {k.lower() for k in out.boost_keywords}
        missing = [k for k in exp_kw if k not in emitted]
        if missing:
            failures.append(
                f"missing keywords on {query!r}: {missing}"
            )
    return failures
