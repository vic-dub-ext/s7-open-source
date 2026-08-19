"""S7 - Normalization. In: record drafts (S6 output). Out: the same rows,
updated in place with normalized gene/variant/trait identifiers, every
lookup cached in ontology_cache.

- Genes: symbol -> Ensembl stable ID (providers/ontology.py's xrefs/symbol
  call already resolves common HGNC aliases). Ambiguous or unresolved ->
  left null, never guessed.
- Variants: parsed into chrom/pos/ref/alt (rs12345, 1:12345:A:G,
  chr1:12345_A/G, 1-12345-A-G). rsIDs are resolved to GRCh38 coordinates
  directly via Ensembl; explicit coordinates are lifted 37->38 only when the
  paper's methods text names GRCh37/hg19 -- unstated build is never lifted.
  Every A/T or C/G variant is flagged strand_ambiguous.
- Traits: OLS4 search, auto-accepted only on an exact/near-exact string
  match to the top candidate; anything softer than that goes to a single-
  model LLM call with the top 10 candidates and an explicit "none of these"
  escape.

Distinct raw values are resolved once per run (not once per row) and each
resolution is looked up in / written back to ontology_cache, which is
shared across runs and papers -- a symbol, variant string, or trait name
seen before never re-hits the network.
"""

from __future__ import annotations

import asyncio
import re
from typing import Any

import httpx
import structlog
from sqlalchemy import Connection

from s7.config import Settings, get_settings
from s7.models.ontology import TraitDisambiguation
from s7.models.stage import StageResult, StageStatus
from s7.prompts import render_prompt
from s7.providers.llm import LLMError, complete_json
from s7.providers.ontology import OntologyClient, OntologyError, parse_variant_string
from s7.store import events
from s7.store.context import get_methods_bundle_for_run
from s7.store.db import get_engine
from s7.store.llm_calls import delete_llm_calls_for_run, insert_llm_call
from s7.store.ontology import get_cached, store_cached
from s7.store.records import list_record_identity_fields, update_normalized_fields
from s7.store.runs import get_run, update_run_status

STAGE = "s7_normalize"

logger = structlog.get_logger()

# Distinct gene/variant/trait values are resolved concurrently, not one
# network round trip at a time -- matches s3_classify.py's MAX_CONCURRENCY
# pattern. Traits get a lower cap since an unmatched one falls through to
# an LLM call, not just a fast HTTP request.
GENE_CONCURRENCY = 10
VARIANT_CONCURRENCY = 10
TRAIT_CONCURRENCY = 5

STRAND_AMBIGUOUS_PAIRS = ({"A", "T"}, {"C", "G"})

_BUILD_37_RE = re.compile(r"GRCh37|hg19|NCBI36|hg18|build\s*37", re.IGNORECASE)
_BUILD_38_RE = re.compile(r"GRCh38|hg38|build\s*38", re.IGNORECASE)

# Trait strings routinely carry a trailing UK Biobank field-code, e.g.
# "Eosinophil count (30150)", or a leading ICD10 code, e.g. "ICD10 I71:
# Aortic aneurysm and dissection". Confirmed by hand against the live OLS4
# API: "Eosinophil count (30150)" returns zero search candidates while
# "Eosinophil count" returns ten good ones -- the code fragment isn't noise
# OLS4 tolerates, it kills the match entirely. This only cleans the *search
# query*; trait_raw on the stored record is never touched (*_raw fields are
# sacred, per models/record.py).
_TRAILING_FIELD_CODE_RE = re.compile(r"\s*\(\d+\)\s*$")
_LEADING_ICD_CODE_RE = re.compile(r"^ICD1?0?\s*[A-Z]?\d+(?:\.\d+)?\s*:\s*", re.IGNORECASE)


def _clean_search_term(raw: str) -> str:
    cleaned = _LEADING_ICD_CODE_RE.sub("", raw)
    cleaned = _TRAILING_FIELD_CODE_RE.sub("", cleaned)
    cleaned = cleaned.replace("_", " ")
    return re.sub(r"\s+", " ", cleaned).strip()


def _detect_genome_build(methods_text: str) -> str | None:
    """Lightweight heuristic, not NLP: an explicit, unambiguous mention of
    one build and not the other. Build 37 is lifted to 38 only where the
    paper states build 37; if the build is unstated, nothing is lifted.
    Both-or-neither is treated the same as unstated -- safer to skip a
    liftover than to apply one on a guess.
    """
    has_37 = bool(_BUILD_37_RE.search(methods_text))
    has_38 = bool(_BUILD_38_RE.search(methods_text))
    if has_37 and not has_38:
        return "37"
    if has_38 and not has_37:
        return "38"
    return None


def _is_strand_ambiguous(ref: str | None, alt: str | None) -> bool:
    if not ref or not alt or len(ref) != 1 or len(alt) != 1:
        return False
    return {ref.upper(), alt.upper()} in STRAND_AMBIGUOUS_PAIRS


def _is_near_exact_match(raw: str, candidate: dict[str, Any]) -> bool:
    raw_norm = raw.strip().lower()
    if candidate["label"].strip().lower() == raw_norm:
        return True
    return any(syn.strip().lower() == raw_norm for syn in candidate.get("exact_synonyms", []))


def _render_candidates_block(candidates: list[dict[str, Any]]) -> str:
    lines = []
    for c in candidates:
        desc = f" -- {c['description']}" if c.get("description") else ""
        lines.append(f"- {c['obo_id']} ({c['ontology'].upper()}): {c['label']}{desc}")
    return "\n".join(lines)


async def _resolve_gene(
    conn: Connection, client: OntologyClient, symbol: str, counts: dict[str, int]
) -> str | None:
    cached = get_cached(conn, kind="gene_symbol", raw_value=symbol)
    if cached is not None:
        ensembl_id = cached.get("ensembl_gene_id") if isinstance(cached, dict) else None
        # A cache hit is still a real resolution (or a real prior miss) --
        # counting only fresh lookups made a --force re-run against an
        # already-warm ontology_cache report "0 resolved" even though every
        # record's fields were correctly (re)written from the cache.
        counts["genes_resolved" if ensembl_id else "genes_unresolved"] += 1
        return ensembl_id
    try:
        ensembl_id = await client.resolve_gene_symbol(symbol)
    except OntologyError as exc:
        # A transient API error is not the same fact as "this symbol has no
        # Ensembl gene ID" -- caching the latter for the former would poison
        # every future run (any paper re-testing the same symbol, forever,
        # since ontology_cache is never invalidated) with a false negative.
        # Found live: an OLS4 blip during a real run cached "Schizophrenia"
        # as unresolved, even though the API worked fine moments later.
        # Log it, count it unresolved for THIS run, but leave the cache
        # empty so the next attempt tries again instead of trusting a
        # non-answer forever.
        logger.warning("gene_symbol_lookup_failed", symbol=symbol, error=str(exc))
        counts["genes_unresolved"] += 1
        return None
    store_cached(
        conn, kind="gene_symbol", raw_value=symbol, resolved={"ensembl_gene_id": ensembl_id}
    )
    if ensembl_id is None:
        counts["genes_unresolved"] += 1
    else:
        counts["genes_resolved"] += 1
    return ensembl_id


async def _resolve_variant(
    conn: Connection,
    client: OntologyClient,
    raw: str,
    *,
    genome_build: str | None,
    counts: dict[str, int],
) -> dict[str, Any]:
    cached = get_cached(conn, kind="variant", raw_value=raw)
    if cached is not None:
        cached_result = dict(cached)
        # Inferred from the cached payload's shape (see the branches below):
        # empty -> unparseable, has pos_b38 -> resolved, otherwise -> an
        # rsid lookup that didn't resolve. variants_lifted isn't
        # reconstructable this way (nothing marks *how* pos_b38 was
        # derived), so it stays under-counted on a cache hit -- a finer-
        # grained sub-stat, not the headline resolved/unresolved count.
        if not cached_result:
            counts["variants_unparseable"] += 1
        elif "pos_b38" in cached_result:
            counts["variants_resolved"] += 1
        else:
            counts["variants_unresolved"] += 1
        return cached_result

    result: dict[str, Any] = {}
    # False on a transient API error (see _resolve_gene's comment on the same
    # issue) -- for liftover specifically, caching the fallback-to-unlifted
    # position as "pos_b38" would be worse than a miscount: it silently
    # mislabels a GRCh37 coordinate as GRCh38 and persists that forever.
    cacheable = True
    parsed = parse_variant_string(raw)
    if parsed is None:
        counts["variants_unparseable"] += 1
    elif "rsid" in parsed:
        result["rsid"] = parsed["rsid"]
        try:
            coords = await client.resolve_rsid(parsed["rsid"])
        except OntologyError as exc:
            logger.warning("rsid_lookup_failed", rsid=parsed["rsid"], error=str(exc))
            coords = None
            cacheable = False
        if coords is not None:
            result.update(coords)
            counts["variants_resolved"] += 1
        else:
            counts["variants_unresolved"] += 1
    else:
        chrom, pos, ref, alt = parsed["chrom"], parsed["pos"], parsed["ref"], parsed["alt"]
        result["chrom"], result["ref"], result["alt"] = chrom, ref, alt
        if genome_build == "37":
            try:
                lifted = await client.liftover_37_to_38(chrom, pos)
            except OntologyError as exc:
                logger.warning("liftover_failed", chrom=chrom, pos=pos, error=str(exc))
                lifted = None
                cacheable = False
            result["pos_b38"] = lifted if lifted is not None else pos
            counts["variants_lifted"] += 1 if lifted is not None else 0
        else:
            result["pos_b38"] = pos  # GRCh38, or build unstated -- never lifted
        counts["variants_resolved"] += 1

    if cacheable:
        store_cached(conn, kind="variant", raw_value=raw, resolved=result)
    return result


async def _resolve_trait(
    conn: Connection,
    client: OntologyClient,
    settings: Settings,
    run_id: str,
    raw: str,
    counts: dict[str, int],
) -> str | None:
    cached = get_cached(conn, kind="trait", raw_value=raw)
    if cached is not None:
        cached_efo_id = cached.get("efo_id") if isinstance(cached, dict) else None
        # Cache doesn't retain *how* it was matched (exact vs. LLM
        # disambiguation), so a resolved cache hit is booked as
        # traits_matched_exact -- the run-summary message sums
        # matched_exact + matched_llm anyway, so the combined "traits
        # resolved" figure stays accurate either way.
        counts["traits_matched_exact" if cached_efo_id else "traits_needs_review"] += 1
        return cached_efo_id

    cacheable = True
    try:
        candidates = await client.search_trait_candidates(_clean_search_term(raw))
    except OntologyError as exc:
        logger.warning("trait_search_failed", trait=raw, error=str(exc))
        candidates = []
        cacheable = False

    efo_id: str | None = None
    if candidates and _is_near_exact_match(raw, candidates[0]):
        efo_id = candidates[0]["obo_id"]
        counts["traits_matched_exact"] += 1
    elif candidates:
        system, user = render_prompt(
            "s7_trait_disambiguation",
            raw_trait=raw,
            candidates_block=_render_candidates_block(candidates),
        )
        record = None
        try:
            parsed_result, record = await complete_json(
                settings=settings,
                model=settings.anthropic_spec,
                system=system,
                user=user,
                response_model=TraitDisambiguation,
                stage=STAGE,
                entity_id=raw,
            )
            candidate_ids = {c["obo_id"] for c in candidates}
            if parsed_result.matched_obo_id in candidate_ids:
                efo_id = parsed_result.matched_obo_id
                counts["traits_matched_llm"] += 1
            else:
                counts["traits_needs_review"] += 1
        except LLMError as exc:
            record = exc.record
            counts["traits_needs_review"] += 1
        insert_llm_call(
            conn,
            run_id=run_id,
            stage=STAGE,
            entity_id=record.entity_id,
            provider=record.model_spec.provider,
            model=record.model_spec.model,
            prompt_hash=record.prompt_hash,
            prompt=record.prompt,
            response=record.response,
            input_tokens=record.input_tokens,
            output_tokens=record.output_tokens,
            cost_usd=record.cost_usd,
            latency_ms=record.latency_ms,
            ok=record.ok,
        )
    else:
        counts["traits_needs_review"] += 1

    if cacheable:
        store_cached(conn, kind="trait", raw_value=raw, resolved={"efo_id": efo_id})
    return efo_id


async def run(run_id: str, *, force: bool = False) -> StageResult:
    settings = get_settings()
    settings.ensure_dirs()
    engine = get_engine()

    with engine.begin() as conn:
        run_row = get_run(conn, run_id)
        if run_row is None:
            raise ValueError(f"no such run: {run_id}")

        if not force and events.has_stage_finished(conn, run_id, STAGE):
            return StageResult(stage=STAGE, status="skipped", counts={})
        if force:
            delete_llm_calls_for_run(conn, run_id, stage=STAGE)

        events.emit(
            conn, run_id=run_id, stage=STAGE, event_type="stage_started", message="S7 started"
        )
        update_run_status(conn, run_id, status="running", stage_reached=STAGE)

        bundle = get_methods_bundle_for_run(conn, run_id)
        genome_build = _detect_genome_build(str(bundle["content"])) if bundle else None

        identities = list_record_identity_fields(conn, run_id)

        distinct_genes = {r["gene_symbol_raw"] for r in identities if r["gene_symbol_raw"]}
        distinct_variants = {r["variant_raw"] for r in identities if r["variant_raw"]}
        distinct_traits = {r["trait_raw"] for r in identities if r["trait_raw"]}

    counts: dict[str, int] = {
        "genes_resolved": 0,
        "genes_unresolved": 0,
        "variants_resolved": 0,
        "variants_unresolved": 0,
        "variants_unparseable": 0,
        "variants_lifted": 0,
        "traits_matched_exact": 0,
        "traits_matched_llm": 0,
        "traits_needs_review": 0,
        "records_updated": 0,
    }

    gene_map: dict[str, str | None] = {}
    variant_map: dict[str, dict[str, Any]] = {}
    trait_map: dict[str, str | None] = {}

    async def bounded_gene(sem: asyncio.Semaphore, symbol: str) -> None:
        async with sem:
            with engine.begin() as conn:
                gene_map[symbol] = await _resolve_gene(conn, client, symbol, counts)

    async def bounded_variant(sem: asyncio.Semaphore, variant: str) -> None:
        async with sem:
            with engine.begin() as conn:
                variant_map[variant] = await _resolve_variant(
                    conn, client, variant, genome_build=genome_build, counts=counts
                )

    async def bounded_trait(sem: asyncio.Semaphore, trait: str) -> None:
        async with sem:
            with engine.begin() as conn:
                trait_map[trait] = await _resolve_trait(
                    conn, client, settings, run_id, trait, counts
                )

    async with httpx.AsyncClient(timeout=30.0) as http_client:
        client = OntologyClient(httpx_client=http_client)

        gene_sem = asyncio.Semaphore(GENE_CONCURRENCY)
        await asyncio.gather(*(bounded_gene(gene_sem, s) for s in distinct_genes))

        variant_sem = asyncio.Semaphore(VARIANT_CONCURRENCY)
        await asyncio.gather(*(bounded_variant(variant_sem, v) for v in distinct_variants))

        trait_sem = asyncio.Semaphore(TRAIT_CONCURRENCY)
        await asyncio.gather(*(bounded_trait(trait_sem, t) for t in distinct_traits))

    updates: list[dict[str, Any]] = []
    for r in identities:
        variant_fields = variant_map.get(r["variant_raw"] or "", {})
        ref, alt = variant_fields.get("ref"), variant_fields.get("alt")
        updates.append(
            {
                "record_id": r["record_id"],
                "ensembl_gene_id": gene_map.get(r["gene_symbol_raw"] or ""),
                "chrom": variant_fields.get("chrom"),
                "pos_b38": variant_fields.get("pos_b38"),
                "ref": ref,
                "alt": alt,
                "rsid": variant_fields.get("rsid"),
                "efo_id": trait_map.get(r["trait_raw"] or ""),
                "strand_ambiguous": _is_strand_ambiguous(ref, alt),
            }
        )
        counts["records_updated"] += 1

    status: StageStatus = "done"
    traits_matched = counts["traits_matched_exact"] + counts["traits_matched_llm"]
    with engine.begin() as conn:
        update_normalized_fields(conn, updates)
        update_run_status(conn, run_id, status=status, stage_reached=STAGE)
        events.emit(
            conn,
            run_id=run_id,
            stage=STAGE,
            event_type="stage_finished",
            message=f"S7 normalization finished: {counts['records_updated']} records updated "
            f"({counts['genes_resolved']}/{len(distinct_genes)} genes, "
            f"{counts['variants_resolved']}/{len(distinct_variants)} variants, "
            f"{traits_matched}/{len(distinct_traits)} traits resolved), "
            f"genome_build={genome_build or 'unstated'}",
            payload=counts,
        )

    return StageResult(stage=STAGE, status=status, counts=counts)
