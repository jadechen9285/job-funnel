import asyncio
from abc import ABC, abstractmethod
from collections.abc import Callable
from typing import Any

import httpx

from job_funnel.schemas import JobPosting

RETRY_STATUSES = {429, 500, 502, 503, 504}
DEFAULT_BACKOFF = 1.0  # seconds; tests set this to 0
TitleFilter = Callable[[str], bool]


async def request_with_retry(
    client: httpx.AsyncClient | None,
    method: str,
    url: str,
    *,
    retries: int = 2,
    backoff: float | None = None,
    timeout: float = 30.0,
    **kwargs: Any,
) -> httpx.Response:
    """Sends a request, retrying 429/5xx and transport errors with exponential backoff.

    Returns the last response (callers decide what a 404 means); re-raises the last
    transport error if every attempt failed. `client=None` uses a throwaway client.
    """
    backoff = DEFAULT_BACKOFF if backoff is None else backoff
    for attempt in range(retries + 1):
        try:
            if client is None:
                async with httpx.AsyncClient(timeout=timeout) as tmp:
                    response = await tmp.request(method, url, **kwargs)
            else:
                response = await client.request(method, url, **kwargs)
            if response.status_code not in RETRY_STATUSES or attempt == retries:
                return response
        except httpx.TransportError:
            if attempt == retries:
                raise
        await asyncio.sleep(backoff * 2**attempt)
    raise AssertionError("unreachable")


class BaseIngestor(ABC):
    """Common fetch logic for public ATS job-board APIs.

    Subclasses define `source`, `api_url` and how to `_parse` the JSON payload
    into validated `JobPosting`s. Sources that need several requests per board
    (e.g. Workday search + detail) override `fetch_active_jobs` and use `_request`.
    """

    source: str
    retries: int = 2
    retry_backoff: float | None = None  # None -> DEFAULT_BACKOFF

    def __init__(
        self,
        company_board_token: str,
        client: httpx.AsyncClient | None = None,
        company_name: str | None = None,
        verbose: bool = True,
        title_filter: TitleFilter | None = None,
    ):
        self.company_token = company_board_token
        self.company_name = company_name or company_board_token.upper()
        # Injectable client so tests can use httpx.MockTransport instead of the network,
        # and so the pipeline can share one connection pool across thousands of boards.
        self._client = client
        self.verbose = verbose
        # Optional cheap title check: sources that can list titles before fetching
        # descriptions use it to skip detail requests for obviously irrelevant jobs.
        self.title_filter = title_filter

    @property
    @abstractmethod
    def api_url(self) -> str: ...

    @abstractmethod
    def _parse(self, payload: Any) -> list[JobPosting]: ...

    def _log(self, message: str) -> None:
        if self.verbose:
            print(message)

    def _wanted(self, title: str) -> bool:
        return self.title_filter is None or self.title_filter(title)

    async def _request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        return await request_with_retry(
            self._client, method, url, retries=self.retries, backoff=self.retry_backoff, **kwargs
        )

    async def fetch_active_jobs(self) -> list[JobPosting]:
        self._log(f"[*] Fetching live jobs for '{self.company_token}' from {self.source}...")

        response = await self._request("GET", self.api_url)
        response.raise_for_status()

        payload = response.json()
        validated_jobs = self._parse(payload)
        total = len(payload) if isinstance(payload, list) else len(payload.get("jobs", []))
        self._log(f"[+] Successfully validated and ingested {len(validated_jobs)}/{total} jobs.")
        return validated_jobs
