from s7.providers.ontology import parse_variant_string


def test_parse_variant_string_rsid() -> None:
    assert parse_variant_string("rs12345") == {"rsid": "rs12345"}


def test_parse_variant_string_rsid_is_case_insensitive() -> None:
    assert parse_variant_string("RS12345") == {"rsid": "rs12345"}


def test_parse_variant_string_colon_separated() -> None:
    assert parse_variant_string("1:12345:A:G") == {
        "chrom": "1",
        "pos": 12345,
        "ref": "A",
        "alt": "G",
    }


def test_parse_variant_string_chr_prefix_with_underscore_and_slash() -> None:
    assert parse_variant_string("chr1:12345_A/G") == {
        "chrom": "1",
        "pos": 12345,
        "ref": "A",
        "alt": "G",
    }


def test_parse_variant_string_dash_separated() -> None:
    assert parse_variant_string("1-12345-A-G") == {
        "chrom": "1",
        "pos": 12345,
        "ref": "A",
        "alt": "G",
    }


def test_parse_variant_string_handles_x_y_chromosomes() -> None:
    assert parse_variant_string("X:12345:A:G")["chrom"] == "X"
    assert parse_variant_string("chrY:12345:A:G")["chrom"] == "Y"


def test_parse_variant_string_returns_none_for_unparseable() -> None:
    assert parse_variant_string("not a variant") is None
    assert parse_variant_string("") is None


def test_parse_variant_string_returns_none_for_multi_base_indel_style_junk() -> None:
    # Sanity check that garbage doesn't silently half-match.
    assert parse_variant_string("gene:BRCA1") is None
