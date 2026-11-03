import json
from pathlib import Path

import httpx
import pytest

from job_funnel.schemas import JobPosting

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def anyio_backend():
    return "asyncio"


def load_fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


def mock_client(payload: dict | None = None, status_code: int = 200) -> httpx.AsyncClient:
    """An AsyncClient whose every request returns `payload` with `status_code`."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, json=payload or {})

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def make_job(**overrides) -> JobPosting:
    fields = {
        "source": "test",
        "external_id": "1",
        "title": "Data Scientist",
        "company": "ACME",
        "description": "desc",
        "location": "San Francisco",
        "url": "https://example.com/jobs/1",
    }
    fields.update(overrides)
    return JobPosting(**fields)


def routed_client(routes: dict[str, tuple[int, object]]) -> httpx.AsyncClient:
    """An AsyncClient that answers by the first route whose key is a substring of the URL; 404 otherwise."""

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        for fragment, (status_code, payload) in routes.items():
            if fragment in url:
                return httpx.Response(status_code, json=payload)
        return httpx.Response(404, json={})

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))
