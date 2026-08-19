import zipfile
from io import BytesIO

from s7.stages.s0_acquire import _expand_zip, _ext, _select_supplementary_entries


def _zip_bytes(files: dict[str, bytes]) -> bytes:
    buf = BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in files.items():
            zf.writestr(name, data)
    return buf.getvalue()


def test_ext_lowercases_and_handles_missing_dot() -> None:
    assert _ext("Foo.XLSX") == ".xlsx"
    assert _ext("no_extension") == ""


def test_select_prefers_moesm_at_top_level_over_inline_figures() -> None:
    entries = [
        ("41586_Fig1_HTML.gif", b"x", 0),
        ("41586_MOESM1_ESM.pdf", b"x", 0),
        ("41586_MOESM4_ESM.xlsx", b"x", 0),
        ("41586_Tab1_ESM.jpg", b"x", 0),
    ]
    kept = _select_supplementary_entries(entries)
    names = {n for n, _ in kept}
    assert names == {"41586_MOESM1_ESM.pdf", "41586_MOESM4_ESM.xlsx"}


def test_select_falls_back_to_extension_whitelist_without_moesm() -> None:
    entries = [
        ("data.csv", b"x", 0),
        ("notes.xlsx", b"x", 0),
        ("random.gif", b"x", 0),
    ]
    kept = _select_supplementary_entries(entries)
    names = {n for n, _ in kept}
    assert names == {"data.csv", "notes.xlsx"}


def test_select_keeps_nested_zip_contents_even_without_moesm_naming() -> None:
    """Regression: wang2021's MOESM3_ESM.zip contains 'Supp table 1.xlsx' etc,
    none of which carry MOESM naming themselves. Once a top-level MOESM zip is
    expanded, its contents must not be filtered out by the MOESM check that
    correctly applies to the top level.
    """
    entries = [
        ("41586_MOESM1_ESM.pdf", b"x", 0),  # top-level, matches MOESM
        ("41586_Fig1_HTML.gif", b"x", 0),  # top-level, inline figure noise
        ("Supp table 1 - Studied phenotypes.xlsx", b"x", 1),  # from nested zip
        ("Supp table 2 - ExWAS Top hits.xlsx", b"x", 1),  # from nested zip
    ]
    kept = _select_supplementary_entries(entries)
    names = {n for n, _ in kept}
    assert names == {
        "41586_MOESM1_ESM.pdf",
        "Supp table 1 - Studied phenotypes.xlsx",
        "Supp table 2 - ExWAS Top hits.xlsx",
    }


def test_expand_zip_recurses_one_level_into_nested_zip() -> None:
    inner = _zip_bytes({"Supp table 1.xlsx": b"real data"})
    outer = _zip_bytes(
        {
            "41586_MOESM1_ESM.pdf": b"note",
            "41586_MOESM3_ESM.zip": inner,
        }
    )
    entries = _expand_zip(outer, depth=0)
    by_name = {name: (data, depth) for name, data, depth in entries}
    assert by_name["41586_MOESM1_ESM.pdf"] == (b"note", 0)
    assert by_name["Supp table 1.xlsx"] == (b"real data", 1)


def test_expand_zip_drops_macos_resource_fork_junk() -> None:
    """Regression: a real paper's supplement zip (built on macOS) contained
    '__MACOSX/supplement/._supplementary_dataset_14_trait.xlsx' -- a few
    hundred bytes of AppleDouble resource-fork data, not a real workbook,
    but named with the same .xlsx extension as the genuine file next to it.
    Passed through unfiltered, it crashed openpyxl in S1 as if it were a
    real (corrupt) workbook.
    """
    outer = _zip_bytes(
        {
            "supplement/supplementary_dataset_14_trait.xlsx": b"real xlsx bytes",
            "__MACOSX/supplement/._supplementary_dataset_14_trait.xlsx": b"\x00\x05\x16\x07",
            "__MACOSX/._supplement": b"\x00\x05\x16\x07",
        }
    )
    entries = _expand_zip(outer, depth=0)
    names = {name for name, _, _ in entries}
    assert names == {"supplement/supplementary_dataset_14_trait.xlsx"}


def test_expand_zip_stops_at_max_depth() -> None:
    innermost = _zip_bytes({"leaf.xlsx": b"leaf data"})
    middle = _zip_bytes({"nested.zip": innermost})
    outer = _zip_bytes({"nested.zip": middle})
    entries = _expand_zip(outer, depth=0, max_depth=1)
    # depth 0 -> expands "nested.zip" (middle) at depth 1, but depth 1 is not
    # < max_depth(1), so middle's own "nested.zip" entry is kept as-is, not expanded
    names = {name for name, _, _ in entries}
    assert "nested.zip" in names
    assert "leaf.xlsx" not in names
