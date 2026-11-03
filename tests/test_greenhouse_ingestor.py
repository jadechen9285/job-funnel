import httpx
import pytest
from conftest import RecordingTransport, load_fixture, mock_client

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


async def test_title_first_fetches_content_only_for_matches():
    listing = {"jobs": [
        {"id": 1, "title": "Senior Data Scientist", "absolute_url": "https://x.io/1", "location": {"name": "NYC"}},
        {"id": 2, "title": "Account Executive", "absolute_url": "https://x.io/2", "location": {"name": "NYC"}},
        {"id": 3, "title": "ML Engineer", "absolute_url": "https://x.io/3", "location": {"name": "Remote"}},
    ]}

    def handler(request):
        url = str(request.url)
        if url.endswith("/boards/acme/jobs"):
            return httpx.Response(200, json=listing)
        job_id = int(url.rsplit("/", 1)[1])
        if job_id == 3:
            return httpx.Response(500, json={})  # detail failure -> listing without description
        job = next(j for j in listing["jobs"] if j["id"] == job_id)
        return httpx.Response(200, json={**job, "content": "&lt;p&gt;Hi&lt;/p&gt;"})

    transport = RecordingTransport(handler)
    ingestor = GreenhouseIngestor(
        "acme", client=httpx.AsyncClient(transport=transport), title_filter=lambda t: "Account" not in t
    )
    jobs = {j.external_id: j for j in await ingestor.fetch_active_jobs()}

    assert set(jobs) == {"1", "3"}
    assert jobs["1"].description == "Hi"
    assert jobs["3"].description == "No description provided."
    urls = [str(r.url) for r in transport.requests]
    assert not any(u.endswith("/jobs/2") for u in urls)
    assert not any("content=true" in u for u in urls)
