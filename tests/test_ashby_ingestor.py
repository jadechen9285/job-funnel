import httpx
import pytest
from conftest import load_fixture, mock_client

from job_funnel.ashby_ingestor import AshbyIngestor

pytestmark = pytest.mark.anyio


@pytest.fixture
async def jobs():
    ingestor = AshbyIngestor("acme", client=mock_client(load_fixture("ashby_jobs.json")))
    return {j.external_id: j for j in await ingestor.fetch_active_jobs()}


async def test_unlisted_jobs_skipped(jobs):
    assert set(jobs) == {"a-1", "a-2", "a-4"}


async def test_fields_mapped(jobs):
    job = jobs["a-1"]
    assert job.source == "ashby_api"
    assert job.company == "ACME"
    assert job.location == "San Francisco; New York City; Seattle"
    assert job.description == "Train and deploy models."
    assert str(job.url) == "https://jobs.ashbyhq.com/acme/a-1"


async def test_html_fallback_when_plain_empty(jobs):
    assert jobs["a-2"].description == "Research & publish."


async def test_remote_flag(jobs):
    assert jobs["a-1"].is_remote is False  # Hybrid wins over isRemote=True
    assert jobs["a-2"].is_remote is True   # workplaceType=Remote
    assert jobs["a-4"].remote is None      # unknown -> inferred from "Remote (US)"
    assert jobs["a-4"].is_remote is True


@pytest.mark.parametrize(
    "job, expected",
    [
        ({"workplaceType": "Remote"}, True),
        ({"workplaceType": "OnSite", "isRemote": True}, False),
        ({"isRemote": True}, True),
        ({"isRemote": False}, False),
        ({}, None),
    ],
)
def test_remote_flag_precedence(job, expected):
    assert AshbyIngestor._remote_flag(job) is expected


async def test_http_error_raises():
    with pytest.raises(httpx.HTTPStatusError):
        await AshbyIngestor("missing", client=mock_client(status_code=500)).fetch_active_jobs()
