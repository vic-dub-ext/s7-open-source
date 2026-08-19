# s7

*The data is in the supplement.*

`s7` is an open-source pipeline that extracts human genetic association
results from the **supplementary files** of genetics papers and publishes
them as one clean, queryable dataset with full provenance, including row-level citations. 

The name comes from a play on typical exome papers' filenames:
`Supplementary_Table_S7.xlsx` is where the real
data actually lives.

This is a personal project by [Victor Dubeux](https://twitter.com/victordubeux)
([LinkedIn](https://www.linkedin.com/in/victordubeux/)), built using Claude,
GPT, and [Extend](https://extend.ai) as infrastructure.

## What the dataset looks like

Every result is one row in Parquet, partitioned by `source_doi`, with full
cell-level provenance and a confidence-gated review status:

```python
import pandas as pd

df = pd.read_parquet("data/parquet")  # Hive-partitioned by source_doi
df[df.gene_symbol_raw == "SLC2A1"][
    ["trait_raw", "effect_type", "effect_value", "p_value", "analysis_role", "cohort_name"]
]
```

See the full schema in [`docs/data_dictionary.md`](docs/data_dictionary.md). It is
generated directly from the Pydantic model that defines every published
field, so it can't drift from the actual schema.

## Real results

Run end-to-end against two real papers so far:

| Paper | Records | Auto-published | Needs review |
|---|---:|---:|---:|
| [Backman et al. 2021](https://doi.org/10.1038/s41586-021-04103-z) — UK Biobank exome sequencing, 454K participants | 2,406 | 1,288 | 1,118 |
| [Chen et al. 2024](https://doi.org/10.1038/s41467-024-45774-2) — WES of depression | 4,712 | 4,697 | 15 |

`needs_review` records aren't dropped but they're published separately in a
`quarantine/` partition, alongside every check that flagged them. See
`coverage_report.json` in the output directory for the full per-paper
breakdown of what was found, parsed, classified, and published.

## Quickstart

```bash
uv sync
cp .env.example .env        # fill in what you have -- S0/S1 need no keys at all
uv run s7 corpus list        # the five fixed test papers
uv run s7 stage s0_acquire --run <run-id>   # run one stage against one paper
uv run s7 ui                 # http://localhost:8420 -- watch a run stage by stage
```

`s7 ui` also lets you kick off a run against **any paper with a resolvable
DOI**, not just the fixed test corpus — paste a DOI, a doi.org link, or a
Nature-family article URL.

Each stage fails gracefully and names the exact key it's missing, the first
time it actually needs one, but you don't need every API key to get started.

## How it works

Ten stages, S0 through S10, each independently inspectable and re-runnable:

| | Stage | What it does |
|---|---|---|
| S0 | Acquire | Resolve a DOI to its full text + supplementary files (Europe PMC / Unpaywall / landing-page fallback — no key needed) |
| S1 | Explode | Split multi-sheet workbooks into one artifact per sheet |
| S2 | Parse | [Extend](https://extend.ai) parses each file into cell-level, coordinate-tagged content |
| S3 | Classify | Extend classifies each table against a 9-category taxonomy — inline config, no saved processor to provision |
| S4 | Context | Assemble the paper's methods text + mask/phenotype decoder tables into one bundle |
| S5 | Contract | Two independent LLMs (Claude + GPT) each read a table's header once and induce a schema contract — disagreements are surfaced, not averaged |
| S6 | Project | Deterministic code applies the contract to every row. No LLM touches row data. |
| S7 | Normalize | Resolve genes (Ensembl), variants (Ensembl), and traits (EBI OLS4) to stable IDs |
| S8 | Validate | Arithmetic consistency + grounding checks (pure code) plus a semantic sanity pass (LLM, only where interpretation is genuinely required) |
| S9 | Arbitrate | Combine every check into one confidence score and a routing decision |
| S10 | Publish | Write Parquet + a generated data dictionary + a coverage report |

**The core design commitment: interpret once, apply many.** An LLM reads a
table's header and emits a schema contract, which is a mapping from source column to
target field, with a transform and its own confidence. That contract is then
applied to every row by deterministic code. Row data is never sent to an LLM
for extraction; the LLM's job is understanding structure.

## Where we use LLM judges, and where we don't

- **Arithmetic consistency and grounding (S8) are pure code.** Recomputing a
  p-value from an effect size and standard error, or checking that a stored
  value still matches the exact source cell it came from.
- **LLMs are used only where interpretation is genuinely required**: reading
  a table's header to induce a schema (S5), and a final semantic sanity pass
  on whether a row's values make biological sense together (S8's V3 check
  "is this effect allele plausible given how the paper defines it?").
- **Every S5 and S8 judgment runs through two independent model families**
  (Claude and GPT), and disagreements are surfaced for review rather than
  averaged away or resolved by picking one arbitrarily.

## Known limitations

- **Strand ambiguity is not resolved.** For A/T and C/G variants, strand
  can't be determined from the document alone. These are flagged
  `strand_ambiguous: true`, not silently guessed.
- **Effect alleles the source doesn't state are left `null`, never
  inferred.** Getting this wrong silently flips a result's direction, which
  is exactly the kind of error this pipeline exists to prevent.
- **Coverage is partial, and itemized, not hidden.** `coverage_report.json`
  reports exactly what fraction of each paper's supplementary content was
  found, parsed, classified, and published — including what wasn't.
- **Trait mappings below the ontology match threshold are routed to human
  review, never guessed.**
- **The acceptance-test suite (`corpus/assertions/`) is not yet built out.**
  The fixed five-paper test corpus (`corpus/papers.yaml`) and its intended
  assertion format are defined; specific per-paper assertions are a known
  gap — see `corpus/assertions/README.md`.

## License

[MIT](LICENSE)
