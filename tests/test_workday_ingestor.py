import json

import httpx
import pytest
from conftest import RecordingTransport

from job_funnel.pipeline import title_prefilter
from job_funnel.workday_ingestor import WorkdayIngestor

pytestmark = pytest.mark.anyio

TOKEN = "acme|wd5|careers"
BASE = "https://acme.wd5.myworkdayjobs.com/wday/cxs/acme/careers"

# search term -> full result list (served in pages)
RESULTS = {
    "data scientist": [
        {"title": "Senior Data Scientist", "externalPath": "/job/NY/Senior-DS_R1", "locationsText": "New York"},
        {"title": "Sales Director", "externalPath": "/job/NY/Sales_R2", "locationsText": "New York"},
        {"title": "Data Scientist II", "externalPath": "/job/SF/DS2_R3", "locationsText": "San Francisco"},
    ],
    "machine learning": [
        {"title": "Senior Data Scientist", "externalPath": "/job/NY/Senior-DS_R1", "locationsText": "New York"},
        {"title": "ML Engineer", "externalPath": "/job/Remote/MLE_R4", "locationsText": "Remote"},
    ],
}
DETAILS = {
    "/job/NY/Senior-DS_R1": {"jobReqId": "R1", "title": "Senior Data Scientist", "location": "New York, NY",
                             "jobDescription": "<p>Model &amp; ship.</p>",
                             "externalUrl": "https://acme.wd5.myworkdayjobs.com/careers/job/NY/Senior-DS_R1"},
    "/job/SF/DS2_R3": {"jobReqId": "R3", "title": "Data Scientist II", "jobDescription": "<p>Analyze.</p>",
                       "remoteType": "Hybrid"},
    "/job/Remote/MLE_R4": {"jobReqId": "R4", "title": "ML Engineer", "jobDescription": "<p>Train.</p>",
                           "remoteType": "Fully Remote"},
}


def workday_transport(page_size_seen=None):
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if request.method == "POST" and url == f"{BASE}/jobs":
            body = json.loads(request.content)
            results = RESULTS.get(body["searchText"], [])
            page = results[body["offset"]: body["offset"] + body["limit"]]
            # Workday only reports total on the first page
            return httpx.Response(200, json={"total": len(results) if body["offset"] == 0 else 0, "jobPostings": page})
        path = url.removeprefix(BASE)
        if path in DETAILS:
            return httpx.Response(200, json={"jobPostingInfo": DETAILS[path]})
        return httpx.Response(404, json={})

    return RecordingTransport(handler)


def ingestor(transport, **kwargs):
    client = httpx.AsyncClient(transport=transport)
    return WorkdayIngestor(TOKEN, client=client, search_terms=list(RESULTS), **kwargs)


def test_token_parts_and_urls():
    w = WorkdayIngestor(TOKEN)
    assert (w.tenant, w.wd, w.site) == ("acme", "wd5", "careers")
    assert w.api_url == f"{BASE}/jobs"
    assert w.company_name == "ACME"
    assert WorkdayIngestor(TOKEN, company_name="Acme Corp").company_name == "Acme Corp"


async def test_multi_term_search_dedups_and_fetches_details():
    transport = workday_transport()
    jobs = {j.external_id: j for j in await ingestor(transport).fetch_active_jobs()}

    # R2 has no detail page (404) -> falls back to its path as id
    assert set(jobs) == {"R1", "R3", "R4", "/job/NY/Sales_R2"}
    detail_gets = [r for r in transport.requests if r.method == "GET"]
    assert len(detail_gets) == 4  # 4 unique postings, the duplicate R1 fetched once


async def test_title_filter_skips_detail_requests():
    transport = workday_transport()
    jobs = await ingestor(transport, title_filter=title_prefilter).fetch_active_jobs()

    assert sorted(j.title for j in jobs) == ["Data Scientist II", "ML Engineer", "Senior Data Scientist"]
    fetched = {str(r.url).removeprefix(BASE) for r in transport.requests if r.method == "GET"}
    assert "/job/NY/Sales_R2" not in fetched


async def test_fields_mapped():
    jobs = {j.external_id: j for j in await ingestor(workday_transport(), title_filter=title_prefilter).fetch_active_jobs()}
    r1 = jobs["R1"]
    assert (r1.source, r1.company, r1.board_token) == ("workday_api", "ACME", TOKEN)
    assert r1.description == "Model & ship."
    assert r1.location == "New York, NY"
    assert str(r1.url) == "https://acme.wd5.myworkdayjobs.com/careers/job/NY/Senior-DS_R1"
    assert jobs["R3"].location == "San Francisco"  # falls back to listing
    assert str(jobs["R3"].url) == "https://acme.wd5.myworkdayjobs.com/careers/job/SF/DS2_R3"
    assert jobs["R3"].remote is False  # Hybrid
    assert jobs["R4"].remote is True


async def test_pagination_respects_page_size_and_cap():
    transport = workday_transport()
    w = ingestor(transport, title_filter=title_prefilter)
    w.page_size = 2
    await w.fetch_active_jobs()
    offsets = [json.loads(r.content)["offset"] for r in transport.requests if r.method == "POST"]
    assert offsets == [0, 2, 0]  # 3 results -> 2 pages; 2 results -> 1 page

    transport = workday_transport()
    w = ingestor(transport, title_filter=title_prefilter)
    w.page_size, w.max_results_per_term = 1, 1
    await w.fetch_active_jobs()
    assert len([r for r in transport.requests if r.method == "POST"]) == 2  # one page per term


async def test_search_error_raises():
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(422, json={})))
    with pytest.raises(httpx.HTTPStatusError):
        await WorkdayIngestor(TOKEN, client=client).fetch_active_jobs()
