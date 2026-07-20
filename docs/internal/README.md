# Internal docs — NOT for open-source distribution

The material in this directory is **internal sales / competitive collateral**. It is
intentionally kept out of the published product surface:

- Do not link to it from the public `README.md` or user-facing docs.
- Do not include it in release artifacts or marketing pages.
- It is not part of the Python package (`pyproject.toml` packages only `src*`).

## Accuracy rules for competitive material

These docs were scrubbed of unverified/fabricated claims (funding, valuations, ARR, an
acquisition, inaccurate competitor test counts, and "nobody does X" assertions). Keep it that
way:

1. **No financial or M&A figures without a primary, dated public source** (the company's own
   announcement, an SEC filing, or a named press release). If you need a number for a deal,
   source it at that time and cite it inline — do not maintain a standing table of approximate
   figures.
2. **Don't claim competitors "have nothing."** For broad categories (PII redaction, injection
   detection), managed and dedicated tools exist and are often more robust. Differentiate on
   *integration* and *self-hosting*, not exclusivity.
3. **Represent LiteLLM accurately** — it enforces key/team budgets and rate limits and has an
   extensive test suite. Compare on the hierarchy + integrated pipeline, not on false absences.
