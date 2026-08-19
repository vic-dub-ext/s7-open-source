from s7.corpus import load_papers


def test_load_papers_has_five_fixed_papers() -> None:
    papers = load_papers()
    assert set(papers) == {
        "backman2021",
        "wang2021",
        "vanhout2020",
        "chen2024depression",
        "chen2023cognitive",
    }


def test_each_paper_has_doi_and_tests() -> None:
    papers = load_papers()
    for key, paper in papers.items():
        assert paper["doi"], key
        assert paper["tests"], key
