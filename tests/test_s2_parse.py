from s7.stages.s2_parse import _table_from_block, _tables_and_fallback


def _cell(row, col, content, *, is_header=False, page=1, bbox=None):
    bbox = bbox or {"left": 0, "top": 0, "right": 10, "bottom": 10}
    return {
        "type": "table_cell",
        "content": content,
        "details": {
            "type": "table_cell_details",
            "rowIndex": row,
            "columnIndex": col,
            "isHeader": is_header,
        },
        "metadata": {"page": {"number": page}},
        "boundingBox": bbox,
    }


def test_table_from_block_splits_header_and_data_rows() -> None:
    block = {
        "type": "table",
        "content": "<table>...</table>",
        "details": {"type": "table_details", "rowCount": 3, "columnCount": 2},
        "children": [
            _cell(0, 0, "gene", is_header=True),
            _cell(0, 1, "p", is_header=True),
            _cell(1, 0, "BRCA1"),
            _cell(1, 1, "1e-8"),
            _cell(2, 0, "TP53"),
            _cell(2, 1, "2e-9"),
        ],
    }
    table = _table_from_block(block)
    assert table["header_rows"] == [["gene", "p"]]
    assert table["row_count"] == 3
    assert table["col_count"] == 2
    values = {(c["row_index"], c["col_index"]): c["value"] for c in table["cells"]}
    assert values[(1, 0)] == "BRCA1"
    assert values[(2, 1)] == "2e-9"
    # header cells must not leak into the data cell list
    assert (0, 0) not in values


def test_table_from_block_preserves_bbox_and_page() -> None:
    block = {
        "details": {"rowCount": 1, "columnCount": 1},
        "children": [_cell(0, 0, "x", page=3, bbox={"left": 1, "top": 2, "right": 3, "bottom": 4})],
    }
    cell = _table_from_block(block)["cells"][0]
    assert cell["page"] == 3
    assert cell["bbox_x0"] == 1
    assert cell["bbox_y0"] == 2
    assert cell["bbox_x1"] == 3
    assert cell["bbox_y1"] == 4


def test_table_from_block_handles_sparse_header_row() -> None:
    """A header row with a gap (e.g. a merged cell) must not crash the
    row-width computation -- missing columns become None, not KeyError.
    """
    block = {
        "details": {"rowCount": 2, "columnCount": 3},
        "children": [
            _cell(0, 0, "Gene", is_header=True),
            _cell(0, 2, "P value", is_header=True),  # column 1 skipped
        ],
    }
    table = _table_from_block(block)
    assert table["header_rows"] == [["Gene", None, "P value"]]


def test_tables_and_fallback_collects_multiple_tables() -> None:
    output = {
        "chunks": [
            {
                "blocks": [
                    {"type": "heading", "content": "# Title"},
                    {
                        "type": "table",
                        "content": "<table>1</table>",
                        "details": {"rowCount": 1, "columnCount": 1},
                        "children": [_cell(0, 0, "a")],
                    },
                    {
                        "type": "table",
                        "content": "<table>2</table>",
                        "details": {"rowCount": 1, "columnCount": 1},
                        "children": [_cell(0, 0, "b")],
                    },
                ]
            }
        ]
    }
    tables, fallback = _tables_and_fallback(output)
    assert len(tables) == 2
    assert fallback == "# Title"


def test_tables_and_fallback_with_no_tables_returns_concatenated_text() -> None:
    output = {
        "chunks": [
            {"blocks": [{"type": "heading", "content": "# Methods"}]},
            {"blocks": [{"type": "text", "content": "We sequenced exomes."}]},
        ]
    }
    tables, fallback = _tables_and_fallback(output)
    assert tables == []
    assert fallback == "# Methods\n\nWe sequenced exomes."
