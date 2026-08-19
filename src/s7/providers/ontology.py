"""Ensembl REST (genes/variants) + EBI OLS4 (traits). Every lookup is cached
by the calling stage (s7_normalize.py) in ontology_cache, keyed on the raw
string -- these calls dominate wall-clock time otherwise.

Both are free, unauthenticated public APIs, so this module owns only rate
limiting and response parsing -- no credit/cost tracking like
providers/extend.py. Nothing outside this module talks to Ensembl or OLS4
directly, matching that module's boundary.
"""

from __future__ import annotations

import re
from typing import Any

import httpx

from s7.providers.extend import TokenBucket

ENSEMBL_BASE_URL = "https://rest.ensembl.org"
OLS4_BASE_URL = "https://www.ebi.ac.uk/ols4/api"
DEFAULT_RATE_LIMIT_PER_SEC = 10.0  # polite default for both free APIs

# Handles the common variant formats: rs12345, 1:12345:A:G, chr1:12345_A/G,
# 1-12345-A-G. One pattern covers the three chrom:pos:ref:alt spellings
# regardless of separator; rsIDs are matched separately.
_VARIANT_RE = re.compile(
    r"^(?:chr)?(?P<chrom>[0-9XYMxym]{1,2})[:_-](?P<pos>\d+)"
    r"[:_-](?P<ref>[ACGTacgt]+)[/:_-](?P<alt>[ACGTacgt]+)$"
)
_RSID_RE = re.compile(r"^rs\d+$", re.IGNORECASE)


class OntologyError(Exception):
    """A lookup that failed for reasons other than a normal not-found
    (network error, 5xx, malformed response) -- distinct from a clean
    "no match" (None), which is not an error.
    """


def parse_variant_string(raw: str) -> dict[str, Any] | None:
    """Pure parsing, no network. `rs12345` -> {"rsid": "rs12345"}; the
    chrom:pos:ref:alt family -> {"chrom", "pos", "ref", "alt"}. None for
    anything else -- the caller decides what an unparseable string means.
    """
    raw = raw.strip()
    if _RSID_RE.match(raw):
        return {"rsid": raw.lower()}
    match = _VARIANT_RE.match(raw)
    if not match:
        return None
    return {
        "chrom": match.group("chrom").upper(),
        "pos": int(match.group("pos")),
        "ref": match.group("ref").upper(),
        "alt": match.group("alt").upper(),
    }


class OntologyClient:
    """One instance per stage run -- caller owns and closes the
    httpx.AsyncClient, matching ExtendClient's pattern.
    """

    def __init__(
        self, *, httpx_client: httpx.AsyncClient, rate_per_sec: float = DEFAULT_RATE_LIMIT_PER_SEC
    ) -> None:
        self._http = httpx_client
        self._bucket = TokenBucket(rate_per_sec)

    async def _get(
        self, url: str, *, params: dict[str, Any] | None = None
    ) -> httpx.Response | None:
        await self._bucket.acquire()
        try:
            response = await self._http.get(url, params=params)
        except httpx.HTTPError as exc:
            raise OntologyError(f"request to {url} failed: {exc}") from exc
        if response.status_code == 404:
            return None
        if response.status_code >= 400:
            raise OntologyError(f"{url} returned {response.status_code}: {response.text[:200]}")
        return response

    async def resolve_gene_symbol(self, symbol: str) -> str | None:
        """Ensembl's xrefs/symbol endpoint already resolves common HGNC
        aliases (spot-checked: "GLUT1" -> ENSG00000117394, the same ID as
        its current symbol "SLC2A1"), so it covers most of what a separate
        HGNC alias-resolution call would add.
        Returns None on zero or more-than-one distinct ENSG match --
        ambiguity is reported, never guessed.
        """
        response = await self._get(
            f"{ENSEMBL_BASE_URL}/xrefs/symbol/homo_sapiens/{symbol}",
            params={"content-type": "application/json"},
        )
        if response is None:
            return None
        matches = response.json()
        gene_ids = {
            m["id"]
            for m in matches
            if m.get("type") == "gene" and str(m.get("id", "")).startswith("ENSG")
        }
        return next(iter(gene_ids)) if len(gene_ids) == 1 else None

    async def resolve_rsid(self, rsid: str) -> dict[str, Any] | None:
        """rsID -> {chrom, pos_b38, ref, alt} via Ensembl's variation
        endpoint, which reports GRCh38 coordinates directly -- no liftover
        needed for rsID-resolved variants.
        """
        response = await self._get(
            f"{ENSEMBL_BASE_URL}/variation/human/{rsid}",
            params={"content-type": "application/json"},
        )
        if response is None:
            return None
        mappings = response.json().get("mappings") or []
        chromosomal = [m for m in mappings if m.get("coord_system") == "chromosome"]
        if len(chromosomal) != 1:
            return None  # unmapped or multi-mapped -- don't guess which
        m = chromosomal[0]
        alleles = str(m.get("allele_string", "")).split("/")
        if len(alleles) < 2:
            return None
        return {
            "chrom": str(m["seq_region_name"]),
            "pos_b38": int(m["start"]),
            "ref": alleles[0],
            "alt": alleles[1],
        }

    async def liftover_37_to_38(self, chrom: str, pos: int) -> int | None:
        """GRCh37 -> GRCh38 for one base position. Note the region syntax is
        `start..end` (double dot) -- `start-end` silently hangs the request
        rather than erroring, confirmed against the live API.
        """
        response = await self._get(
            f"{ENSEMBL_BASE_URL}/map/human/GRCh37/{chrom}:{pos}..{pos}/GRCh38",
            params={"content-type": "application/json"},
        )
        if response is None:
            return None
        mappings = response.json().get("mappings") or []
        if len(mappings) != 1:
            return None
        return int(mappings[0]["mapped"]["start"])

    async def search_trait_candidates(self, term: str, *, rows: int = 10) -> list[dict[str, Any]]:
        response = await self._get(
            f"{OLS4_BASE_URL}/search",
            params={"q": term, "ontology": "efo,mondo", "rows": rows},
        )
        if response is None:
            return []
        docs = response.json().get("response", {}).get("docs", [])
        return [
            {
                "obo_id": d.get("obo_id", ""),
                "label": d.get("label", ""),
                "ontology": d.get("ontology_prefix", ""),
                "description": next(iter(d.get("description") or []), ""),
                "exact_synonyms": d.get("exact_synonyms") or [],
            }
            for d in docs
            if d.get("obo_id") and d.get("label")
        ]
