import httpx
import pytest

from job_funnel.base_ingestor import request_with_retry

pytestmark = pytest.mark.anyio


def sequence_client(responses):
    """Serves the given status codes / exceptions in order."""
    calls = iter(responses)

    def handler(request):
        item = next(calls)
        if isinstance(item, Exception):
            raise item
        return httpx.Response(item, json={})

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_retries_5xx_then_succeeds():
    response = await request_with_retry(sequence_client([503, 504, 200]), "GET", "https://x.io")
    assert response.status_code == 200


async def test_retries_transport_error_then_succeeds():
    client = sequence_client([httpx.ConnectError("boom"), 200])
    assert (await request_with_retry(client, "GET", "https://x.io")).status_code == 200


async def test_returns_last_response_when_retries_exhausted():
    response = await request_with_retry(sequence_client([503, 503, 503]), "GET", "https://x.io", retries=2)
    assert response.status_code == 503


async def test_raises_transport_error_when_retries_exhausted():
    client = sequence_client([httpx.ConnectError("a"), httpx.ConnectError("b")])
    with pytest.raises(httpx.ConnectError):
        await request_with_retry(client, "GET", "https://x.io", retries=1)


async def test_404_is_not_retried():
    response = await request_with_retry(sequence_client([404]), "GET", "https://x.io")
    assert response.status_code == 404
