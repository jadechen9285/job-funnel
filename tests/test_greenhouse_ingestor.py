import httpx
import pytest
from conftest import load_fixture, mock_client

from job_funnel.greenhouse_ingestor import GreenhouseIngestor

pytestmark = pytest.mark.anyio


async def test_parses_valid_jobs_and_drops_invalid():
    ingestor = GreenhouseIngestor("acme", client=mock_client(load_fixture("greenhouse_jobs.json")))
    jobs = await ingestor.fetch_active_jobs()

    assert [j.external_id for j in jobs] == ["1001", "1002"]  # 1003 has no URL
    first = jobs[0]
    assert first.source == "greenhouse_api"
    assert first.company == "ACME"
    assert first.location == "Remote - US"
    assert first.is_remote is True
    assert first.description == "Build ML models & ship them."
    assert str(first.url) == "https://job-boards.greenhouse.io/acme/jobs/1001"


async def test_api_url_uses_board_token():
    assert "boards/acme/jobs?content=true" in GreenhouseIngestor("acme").api_url


async def test_missing_location_falls_back():
    payload = {"jobs": [{"id": 1, "title": "DS", "absolute_url": "https://x.io/1", "content": ""}]}
    jobs = await GreenhouseIngestor("acme", client=mock_client(payload)).fetch_active_jobs()
    assert jobs[0].location == "Remote / Unspecified"


async def test_http_error_raises():
    with pytest.raises(httpx.HTTPStatusError):
        await GreenhouseIngestor("missing", client=mock_client(status_code=404)).fetch_active_jobs()
