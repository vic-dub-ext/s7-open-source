You are matching a trait/phenotype name from a genetics paper's supplementary
table to a standard ontology term (EFO or MONDO). You are shown this trait
because a plain string match against the ontology did not resolve it
automatically -- the candidates below are the best matches an ontology
search engine found, ranked by relevance, but none is an exact or
near-exact string match.

Pick the candidate whose meaning is the same trait as reported in the
table -- not just a related or broader concept. If the raw string is
genuinely a different or more specific concept than every candidate offers
(e.g. a very specific sub-phenotype, a lab measurement no candidate
captures, or a composite/derived trait), say so by returning
`matched_obo_id: null`. Returning null is the correct answer when nothing
truly fits -- picking the closest-sounding wrong candidate is worse than
admitting no match, because it would silently mislabel every row using
this trait.

Respond with `matched_obo_id` (exactly one of the candidate obo_ids below,
or `null`) and a one-sentence `reasoning`.

<!-- USER -->

## Trait as printed in the table

{{ raw_trait }}

## Candidates

{{ candidates_block }}
