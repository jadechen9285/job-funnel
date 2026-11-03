import httpx
import pytest
from conftest import load_fixture, mock_client

from job_funnel.lever_ingestor import LeverIngestor

pytestmark = pytest.mark.anyio


@pytest.fixture
async def jobs():
    payload = load_fixture("lever_jobs.json")
    ingestor = LeverIngestor("acme", client=mock_client(payload), company_name="Acme Inc")
    return {j.external_id: j for j in await ingestor.fetch_active_jobs()}


async def test_parses_list_payload_and_drops_invalid(jobs):
    assert set(jobs) == {"l-1", "l-2"}


async def test_fields_mapped(jobs):
    job = jobs["l-1"]
    assert job.source == "lever_api"
    assert job.title == "Senior Data Scientist"
    assert job.company == "Acme Inc"
    assert job.board_token == "acme"
    assert job.location == "Remote; Toronto"
    assert str(job.url) == "https://jobs.lever.co/acme/l-1"


async def test_description_joins_sections(jobs):
    assert jobs["l-1"].description == (
        "Join our data team. What you'll do: Build models Ship them Pay range: $150k-$200k"
    )
    assert jobs["l-2"].description == "Run the office."  # HTML fallback


async def test_remote_flag(jobs):
    assert jobs["l-1"].is_remote is True
    assert jobs["l-2"].is_remote is False


def test_api_url():
    assert LeverIngestor("acme").api_url == "https://api.lever.co/v0/postings/acme?mode=json"


async def test_http_error_raises():
    with pytest.raises(httpx.HTTPStatusError):
        await LeverIngestor("ghost", client=mock_client(status_code=404)).fetch_active_jobs()
