"""Regression: an Extend SDK ApiError (e.g. a 400 for a file type Extend
doesn't support) must come out of ExtendClient as the project's own
ExtendError, not escape unwrapped -- callers up the stack (s2_parse.py's
_parse_one, s3_classify.py's equivalent) already know how to treat an
ExtendError as one failed artifact rather than crashing the whole batch,
but only if it's actually an ExtendError. Found via a real paper whose
supplement included a .tsv file, which Extend's upload endpoint rejects.
"""

from __future__ import annotations

import pytest
from extend_ai.core.api_error import ApiError

from s7.config import get_settings
from s7.providers.extend import ExtendClient, ExtendError


@pytest.fixture
def client(monkeypatch) -> ExtendClient:
    monkeypatch.setenv("EXTEND_API_KEY", "test-key")
    return ExtendClient(get_settings(), stage="test")


def _bad_request() -> ApiError:
    return ApiError(status_code=400, body={"code": "INVALID_REQUEST", "message": "nope"})


async def test_upload_file_wraps_api_error(client, monkeypatch) -> None:
    async def fail(*args, **kwargs):
        raise _bad_request()

    monkeypatch.setattr(client._sdk.files, "upload", fail)
    with pytest.raises(ExtendError, match="rejected by Extend"):
        await client.upload_file(
            file_name="data.tsv", data=b"x", mime_type="text/tab-separated-values"
        )


async def test_create_parse_run_wraps_api_error(client, monkeypatch) -> None:
    async def fail(*args, **kwargs):
        raise _bad_request()

    monkeypatch.setattr(client._sdk.parse_runs, "create", fail)
    with pytest.raises(ExtendError, match="could not create parse run"):
        await client.create_parse_run(file_id="f1", config={})


async def test_create_classify_run_wraps_api_error(client, monkeypatch) -> None:
    async def fail(*args, **kwargs):
        raise _bad_request()

    monkeypatch.setattr(client._sdk.classify_runs, "create", fail)
    with pytest.raises(ExtendError, match="could not create classify run"):
        await client.create_classify_run(file_id="f1", config={"classifications": []})
