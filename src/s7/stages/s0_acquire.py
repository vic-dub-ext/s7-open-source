"""S0 - Acquire. In: a DOI (and, once resolved, a PMCID). Out: Artifact rows
for the article full text and every supplementary file.

Primary source is Europe PMC (no key needed): resolve the PMCID, fetch the
full-text XML, fetch the supplementary-files zip. If Europe PMC has no
supplementary files for the paper, fall back to scraping the Nature-family
landing page for static-content links. Both paths run with no API keys at
all.
"""

from __future__ import annotations

import re
import zipfile
from io import BytesIO
from urllib.parse import urljoin

import httpx
from sqlalchemy import Connection

from s7.config import Settings, get_settings
from s7.models.stage import StageResult, StageStatus
from s7.storage import sha256_of, store_bytes
from s7.store import events
from s7.store.artifacts import delete_all_for_run, insert_artifact, list_top_level
from s7.store.db import get_engine, now_iso
from s7.store.runs import get_run, set_pmcid, update_run_status

STAGE = "s0_acquire"

EUROPE_PMC_BASE = "https://www.ebi.ac.uk/europepmc/webservices/rest"
UNPAYWALL_BASE = "https://api.unpaywall.org/v2"
MAX_BYTES = 200 * 1024 * 1024  # files larger than this are skipped
USER_AGENT = (
    "s7/0.1.0 (+https://github.com; genetics-supplement-extraction; "
    "mailto:research@example.org)"
)

# Springer Nature's own marker for genuine supplementary material files, as
# opposed to inline Fig<N>_ESM / Tab<N>_ESM / Article_Eq<N> renders that ride
# along in the same zip for HTML embedding.
MOESM_PATTERN = re.compile(r"MOESM\d+_ESM", re.IGNORECASE)
SUPPLEMENT_EXTENSIONS = {".xlsx", ".xls", ".csv", ".tsv", ".pdf", ".docx", ".zip"}
NON_FIGURE_EXTENSIONS = SUPPLEMENT_EXTENSIONS - {".zip"}

EMPTY_SHA256 = sha256_of(b"")


def _ext(name: str) -> str:
    i = name.rfind(".")
    return name[i:].lower() if i != -1 else ""


def _record_unreachable(
    conn: Connection,
    *,
    run_id: str,
    kind: str,
    file_name: str,
    download_url: str | None,
    mime_type: str,
    skip_reason: str,
    skip_detail: str,
    byte_size: int = 0,
) -> str:
    return insert_artifact(
        conn,
        run_id=run_id,
        kind=kind,
        file_name=file_name,
        mime_type=mime_type,
        byte_size=byte_size,
        sha256=EMPTY_SHA256,
        storage_path="",
        retrieved_at=now_iso(),
        download_url=download_url,
        skip_reason=skip_reason,
        skip_detail=skip_detail,
    )


async def _resolve_pmcid(client: httpx.AsyncClient, doi: str) -> str | None:
    resp = await client.get(
        f"{EUROPE_PMC_BASE}/search", params={"query": f"DOI:{doi}", "format": "json"}
    )
    resp.raise_for_status()
    data = resp.json()
    results = data.get("resultList", {}).get("result", [])
    if not results:
        return None
    pmcid = results[0].get("pmcid")
    return str(pmcid) if pmcid else None


async def _fetch_article(
    client: httpx.AsyncClient,
    conn: Connection,
    settings: Settings,
    *,
    run_id: str,
    doi: str,
    pmcid: str | None,
) -> bool:
    """Try Europe PMC full text, then Unpaywall's best OA PDF. Returns True on success."""
    if pmcid:
        url = f"{EUROPE_PMC_BASE}/{pmcid}/fullTextXML"
        try:
            resp = await client.get(url)
        except httpx.HTTPError as exc:
            events.emit(
                conn,
                run_id=run_id,
                stage=STAGE,
                event_type="error",
                level="warn",
                message=f"fullTextXML request failed: {exc}",
            )
        else:
            if resp.status_code == 200 and resp.content:
                digest, path = store_bytes(settings.downloads_dir, resp.content)
                insert_artifact(
                    conn,
                    run_id=run_id,
                    kind="article",
                    file_name=f"{pmcid}_fulltext.xml",
                    mime_type="application/xml",
                    byte_size=len(resp.content),
                    sha256=digest,
                    storage_path=str(path),
                    retrieved_at=now_iso(),
                    download_url=url,
                )
                events.emit(
                    conn,
                    run_id=run_id,
                    stage=STAGE,
                    event_type="artifact_found",
                    message=f"fetched full text for {pmcid}",
                    payload={"bytes": len(resp.content)},
                )
                return True

    if settings.unpaywall_email:
        try:
            resp = await client.get(
                f"{UNPAYWALL_BASE}/{doi}", params={"email": settings.unpaywall_email}
            )
            resp.raise_for_status()
            pdf_url = (resp.json().get("best_oa_location") or {}).get("url_for_pdf")
        except (httpx.HTTPError, ValueError):
            pdf_url = None
        if pdf_url:
            try:
                pdf_resp = await client.get(pdf_url)
                pdf_resp.raise_for_status()
            except httpx.HTTPError as exc:
                _record_unreachable(
                    conn,
                    run_id=run_id,
                    kind="article",
                    file_name="article.pdf",
                    download_url=pdf_url,
                    mime_type="application/pdf",
                    skip_reason="download_failed",
                    skip_detail=str(exc),
                )
            else:
                digest, path = store_bytes(settings.downloads_dir, pdf_resp.content)
                insert_artifact(
                    conn,
                    run_id=run_id,
                    kind="article",
                    file_name="article.pdf",
                    mime_type="application/pdf",
                    byte_size=len(pdf_resp.content),
                    sha256=digest,
                    storage_path=str(path),
                    retrieved_at=now_iso(),
                    download_url=pdf_url,
                )
                return True

    _record_unreachable(
        conn,
        run_id=run_id,
        kind="article",
        file_name=f"{doi}.article",
        download_url=None,
        mime_type="application/octet-stream",
        skip_reason="access_denied",
        skip_detail="No PMC full text and no Unpaywall OA PDF location found.",
    )
    return False


def _is_macos_zip_junk(name: str) -> bool:
    """True for the metadata macOS's own `zip`/Finder silently embeds in
    every archive it creates: a parallel `__MACOSX/` tree of AppleDouble
    resource-fork stubs, one per real entry, named `._<original-name>`.
    These carry the *same extension* as the real file next to them (e.g.
    `__MACOSX/supplement/._supplementary_dataset_14_trait.xlsx`), so an
    extension-based filter alone lets them through as if they were genuine
    supplementary content -- they're a few hundred bytes of resource-fork
    data, not a real workbook, and crash openpyxl's zip parser in S1 when
    treated as one. Found via a real paper's supplement zip, not a
    hypothetical.
    """
    basename = name.rsplit("/", 1)[-1]
    return name.startswith("__MACOSX/") or basename.startswith("._")


def _expand_zip(data: bytes, *, depth: int, max_depth: int = 2) -> list[tuple[str, bytes, int]]:
    """Flatten a zip's entries, recursing into nested zips up to max_depth.
    Each entry carries the depth it was found at, since MOESM naming (Springer
    Nature's supplementary-material marker) only applies at the top level --
    once inside a nested zip, everything in it is already-confirmed content.
    """
    out: list[tuple[str, bytes, int]] = []
    with zipfile.ZipFile(BytesIO(data)) as zf:
        for info in zf.infolist():
            if info.is_dir() or _is_macos_zip_junk(info.filename):
                continue
            entry_bytes = zf.read(info.filename)
            if _ext(info.filename) == ".zip" and depth < max_depth:
                out.extend(_expand_zip(entry_bytes, depth=depth + 1, max_depth=max_depth))
            else:
                out.append((info.filename, entry_bytes, depth))
    return out


def _select_supplementary_entries(
    entries: list[tuple[str, bytes, int]],
) -> list[tuple[str, bytes]]:
    """Keep genuine supplementary material, drop inline figure/equation renders
    that ride along in the same Springer Nature zip.

    The MOESM filter only applies to top-level entries -- a Fig<N>_ESM.gif at
    depth 0 is noise, but once we're inside a MOESM*_ESM.zip, its contents
    (e.g. "Supplementary Table 1.xlsx") won't carry MOESM naming themselves
    and must not be filtered out by it.
    """
    top_level = [(name, data) for name, data, depth in entries if depth == 0]
    nested = [(name, data) for name, data, depth in entries if depth > 0]

    moesm = [(name, data) for name, data in top_level if MOESM_PATTERN.search(name)]
    top_kept = moesm or [(n, d) for n, d in top_level if _ext(n) in NON_FIGURE_EXTENSIONS]
    nested_kept = [(n, d) for n, d in nested if _ext(n) in NON_FIGURE_EXTENSIONS]
    return top_kept + nested_kept


async def _store_supplement(
    client: httpx.AsyncClient,
    conn: Connection,
    settings: Settings,
    *,
    run_id: str,
    file_name: str,
    data: bytes,
    download_url: str | None,
) -> None:
    if len(data) > MAX_BYTES:
        _record_unreachable(
            conn,
            run_id=run_id,
            kind="supplement",
            file_name=file_name,
            download_url=download_url,
            mime_type="application/octet-stream",
            skip_reason="too_large",
            skip_detail=f"{len(data)} bytes exceeds the {MAX_BYTES} byte cap",
            byte_size=len(data),
        )
        return
    digest, path = store_bytes(settings.downloads_dir, data)
    insert_artifact(
        conn,
        run_id=run_id,
        kind="supplement",
        file_name=file_name,
        mime_type=_mime_for(file_name),
        byte_size=len(data),
        sha256=digest,
        storage_path=str(path),
        retrieved_at=now_iso(),
        download_url=download_url,
    )
    events.emit(
        conn,
        run_id=run_id,
        stage=STAGE,
        event_type="artifact_found",
        message=f"supplement {file_name}",
        payload={"bytes": len(data)},
    )


def _mime_for(file_name: str) -> str:
    ext = _ext(file_name)
    return {
        ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        ".xls": "application/vnd.ms-excel",
        ".csv": "text/csv",
        ".tsv": "text/tab-separated-values",
        ".pdf": "application/pdf",
        ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        ".zip": "application/zip",
    }.get(ext, "application/octet-stream")


async def _fetch_supplementary_via_epmc(
    client: httpx.AsyncClient, conn: Connection, settings: Settings, *, run_id: str, pmcid: str
) -> bool:
    """Returns True if Europe PMC answered the question at all (200 with a zip,
    or a definitive 404 meaning it has none) -- either way, no fallback needed.
    """
    url = f"{EUROPE_PMC_BASE}/{pmcid}/supplementaryFiles"
    try:
        resp = await client.get(url)
    except httpx.HTTPError as exc:
        events.emit(
            conn,
            run_id=run_id,
            stage=STAGE,
            event_type="error",
            level="warn",
            message=f"supplementaryFiles request failed: {exc}",
        )
        return False

    if resp.status_code == 404:
        return True  # definitive: Europe PMC confirms there are none
    if resp.status_code != 200 or not resp.content:
        return False

    entries = _expand_zip(resp.content, depth=0)
    kept = _select_supplementary_entries(entries)
    events.emit(
        conn,
        run_id=run_id,
        stage=STAGE,
        event_type="artifact_found",
        message=f"supplementary zip: kept {len(kept)} of {len(entries)} entries "
        f"({len(entries) - len(kept)} inline figure/equation renders dropped)",
    )
    for name, data in kept:
        await _store_supplement(
            client, conn, settings, run_id=run_id, file_name=name, data=data, download_url=url
        )
    return True


async def _fetch_supplementary_via_nature(
    client: httpx.AsyncClient, conn: Connection, settings: Settings, *, run_id: str, doi: str
) -> None:
    if not doi.startswith("10.1038/"):
        events.emit(
            conn,
            run_id=run_id,
            stage=STAGE,
            event_type="artifact_skipped",
            level="warn",
            message="no supplementary files via Europe PMC, and DOI is not Nature-family "
            "so the landing-page fallback does not apply",
        )
        return

    article_id = doi.split("/")[-1]
    landing_url = f"https://www.nature.com/articles/{article_id}"
    try:
        resp = await client.get(landing_url)
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        events.emit(
            conn,
            run_id=run_id,
            stage=STAGE,
            event_type="error",
            level="warn",
            message=f"Nature landing page fetch failed: {exc}",
        )
        return

    hrefs = re.findall(
        r'href="([^"]+?\.(?:xlsx|xls|csv|tsv|pdf|docx|zip))"', resp.text, re.IGNORECASE
    )
    urls = sorted({urljoin(landing_url, h) for h in hrefs})
    events.emit(
        conn,
        run_id=run_id,
        stage=STAGE,
        event_type="artifact_found",
        message=f"Nature landing page: found {len(urls)} candidate supplementary links",
    )
    for file_url in urls:
        file_name = file_url.rsplit("/", 1)[-1]
        try:
            file_resp = await client.get(file_url)
            file_resp.raise_for_status()
        except httpx.HTTPError as exc:
            _record_unreachable(
                conn,
                run_id=run_id,
                kind="supplement",
                file_name=file_name,
                download_url=file_url,
                mime_type=_mime_for(file_name),
                skip_reason="download_failed",
                skip_detail=str(exc),
            )
            continue
        if file_resp.status_code in (401, 403):
            _record_unreachable(
                conn,
                run_id=run_id,
                kind="supplement",
                file_name=file_name,
                download_url=file_url,
                mime_type=_mime_for(file_name),
                skip_reason="access_denied",
                skip_detail=f"HTTP {file_resp.status_code}",
            )
            continue
        await _store_supplement(
            client,
            conn,
            settings,
            run_id=run_id,
            file_name=file_name,
            data=file_resp.content,
            download_url=file_url,
        )


async def run(
    run_id: str, *, force: bool = False, transport: httpx.AsyncBaseTransport | None = None
) -> StageResult:
    """`transport` is exposed only so tests can inject an httpx.MockTransport
    instead of hitting the network; production callers never pass it.
    """
    settings = get_settings()
    settings.ensure_dirs()
    engine = get_engine()

    with engine.begin() as conn:
        run_row = get_run(conn, run_id)
        if run_row is None:
            raise ValueError(f"no such run: {run_id}")

        existing = list_top_level(conn, run_id)
        if not force and existing:
            return StageResult(stage=STAGE, status="skipped", counts={"found": len(existing)})
        if force and existing:
            delete_all_for_run(conn, run_id)

        events.emit(
            conn, run_id=run_id, stage=STAGE, event_type="stage_started", message="S0 started"
        )
        update_run_status(conn, run_id, status="running", stage_reached=STAGE)

    doi = run_row["doi"]
    pmcid: str | None = run_row["pmcid"]

    async with httpx.AsyncClient(
        follow_redirects=True,
        timeout=60.0,
        headers={"User-Agent": USER_AGENT},
        transport=transport,
    ) as client:
        if not pmcid:
            pmcid = await _resolve_pmcid(client, doi)
            if pmcid:
                with engine.begin() as conn:
                    set_pmcid(conn, run_id, pmcid)

        with engine.begin() as conn:
            article_ok = await _fetch_article(
                client, conn, settings, run_id=run_id, doi=doi, pmcid=pmcid
            )

        supplements_handled = False
        if pmcid:
            with engine.begin() as conn:
                supplements_handled = await _fetch_supplementary_via_epmc(
                    client, conn, settings, run_id=run_id, pmcid=pmcid
                )
        if not supplements_handled:
            with engine.begin() as conn:
                await _fetch_supplementary_via_nature(
                    client, conn, settings, run_id=run_id, doi=doi
                )

    with engine.begin() as conn:
        top_level = list_top_level(conn, run_id)
        found = len(top_level)
        skipped = sum(1 for a in top_level if a["skip_reason"])
        status: StageStatus = (
            "done"
            if article_ok and skipped == 0
            else ("partial" if found > skipped else "failed")
        )
        update_run_status(conn, run_id, status=status, stage_reached=STAGE)
        events.emit(
            conn,
            run_id=run_id,
            stage=STAGE,
            event_type="stage_finished",
            message=f"S0 acquire finished: {found} artifacts found, {skipped} skipped",
            payload={"found": found, "skipped": skipped},
        )

    return StageResult(stage=STAGE, status=status, counts={"found": found, "skipped": skipped})
