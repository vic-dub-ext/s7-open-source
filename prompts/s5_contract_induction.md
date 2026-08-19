You are inducing a schema contract for one supplementary table from a human
genetics association paper. S7 will apply your contract mechanically to
every row of this table to produce structured association records -- your
interpretation here is applied to every row, so treat every mapping as a
claim you must justify, not a guess.

## What a schema contract is

- `row_entity`: what one row represents -- `"gene"`, `"variant"`, or
  `"gene_variant_pair"` (a single row tests one specific variant within one
  gene, e.g. a leave-one-out or per-variant burden-detail row).
- `column_mappings`: one entry per source column that carries per-row data,
  mapping it to a target field (see the list below), with a `transform` if
  the column's scale differs from the target field's scale.
- `constant_fields`: a list of `{field, value}` pairs for values that are
  true for *every* row in this table but are not their own column -- e.g.
  the whole table is one cohort, one ancestry, one test method. Prefer this
  over leaving a column unmapped when a value is genuinely constant across
  the table; use the same target field names as `column_mappings` for
  `field`, and give `value` as plain text even for a number (e.g.
  `"0.001"`).
- `unmapped_columns`: source columns you could not map to any target field
  or constant (e.g. a free-text notes column, an internal row ID).
- `effect_allele_source`: `"column"` if a column identifies the effect
  (risk) allele, `"constant"` if it's stated once in the methods and true
  for the whole table, or `"unresolvable"` if the paper does not make it
  possible to determine which allele the effect size is relative to.
  Returning `"unresolvable"` is correct and expected when the evidence
  genuinely isn't there -- guessing here is the single worst mistake this
  system can make, because a flipped effect allele silently inverts the
  direction of every downstream association. Do not guess. If `"column"`,
  name that column in `effect_allele_column`; do not also list it in
  `column_mappings`.
- `interpretation_notes`: a short paragraph on your read of the table as a
  whole -- what it reports, and anything ambiguous or uncertain.
- `overall_confidence`: 0-1, your confidence in the contract as a whole.

## Target fields you may map a column (or a constant) to

- `entity_type` -- `"gene"` or `"variant"`; only map this if the table
  mixes both kinds of rows and a column states which.
- `gene_symbol_raw` -- gene symbol exactly as printed.
- `variant_raw` -- variant identifier exactly as printed (rsID,
  `chr:pos:ref:alt`, etc.).
- `rsid` -- an rsID, if present as a column distinct from the general
  variant identifier.
- `trait_raw` -- the trait/phenotype name exactly as printed.
- `trait_label` -- a cleaned-up trait name, only if visibly different from
  `trait_raw` in this table.
- `trait_type` -- `"binary"` or `"quantitative"`, if a column states it.
- `trait_units` -- units of a quantitative trait, e.g. `"mg/dL"`, `"SD"`.
- `test_method_raw` -- the source's own label for the test/mask, e.g.
  `"M1.1"`, `"pLoF|0.001"`.
- `variant_mask_raw` -- the mask or qualifying-variant filter identifier,
  if distinct from `test_method_raw`.
- `maf_threshold` -- the minor allele frequency threshold for this row's
  mask.
- `effect_value` -- the effect size (see `effect_type` for its scale).
- `effect_type` -- `"beta"` | `"odds_ratio"` | `"log_odds"` |
  `"hazard_ratio"` | `"z_score"`.
- `other_allele` -- the non-effect allele, if present.
- `effect_direction` -- `"increases"` | `"decreases"` | `"unknown"`, only
  if stated as its own column rather than derivable from `effect_value`'s
  sign.
- `standard_error` -- the standard error of the effect estimate.
- `p_value` -- the p-value.
- `ci_lower` / `ci_upper` -- confidence interval bounds.
- `cohort_name` -- e.g. `"UK Biobank"`.
- `ancestry` -- `"EUR"` | `"AFR"` | `"SAS"` | `"EAS"` | `"AMR"` | `"pan"` |
  another ancestry label as printed.
- `n_total` / `n_cases` / `n_controls` / `n_carriers` -- sample sizes for
  this row's test.
- `analysis_role` -- `"discovery"` | `"replication"` | `"meta"` |
  `"unknown"`.

Do not invent a target field name that isn't in this list -- if nothing
fits, leave `target_field` null and list the column in `unmapped_columns`.

## Transforms

Use a transform when the column's units differ from the target field's
expected scale. Leave `transform` null (identity) when the column is
already in the target scale.

- `neg_log10_to_p` -- source holds -log10(p); needs `10**-x` to become a
  p-value.
- `log_to_linear` -- source holds a log-scale effect (e.g. log odds ratio);
  needs `exp(x)` for the linear scale.
- `or_to_beta` -- source holds an odds ratio; needs `ln(x)` to become a
  beta/log-odds.
- `percent_to_fraction` -- source is a percentage (0-100); needs `/100` to
  become a fraction (0-1).
- `parse_ci_string` -- source is a combined confidence interval string,
  e.g. `"1.2-3.4"` or `"(1.2, 3.4)"`.

## Rules

- Every entry in `column_mappings` needs `evidence`: a quote or close
  paraphrase from the header or the methods text below that justifies the
  mapping. A mapping with no real evidence is a guess -- give it a low
  `confidence` rather than inventing evidence.
- You will see at most 20 data rows. Base your mapping on the *header* and
  the methods text, not on patterns you notice in those rows --
  generalizing from a sample this small overfits to whatever happens to be
  on the first page.
- If a column is genuinely ambiguous, map it with your best guess and a low
  confidence rather than omitting it -- *except* for the effect allele,
  where `"unresolvable"` is the correct answer when the evidence isn't
  there.

Respond with the schema contract only.

<!-- USER -->

## Table header

{{ header_block }}

## First {{ row_count }} data rows

{{ data_rows_block }}

## Methods (for context on cohort, test design, and units)

{{ methods_bundle }}
