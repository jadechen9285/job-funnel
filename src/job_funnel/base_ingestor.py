from abc import ABC, abstractmethod
from typing import Any

import httpx

from job_funnel.schemas import JobPosting


class BaseIngestor(ABC):
    """Common fetch logic for public ATS job-board APIs.

    Subclasses define `source`, `api_url` and how to `_parse` the JSON payload
    into validated `JobPosting`s.
    """

    source: str

    def __init__(
        self,
        company_board_token: str,
        client: httpx.AsyncClient | None = None,
        company_name: str | None = None,
        verbose: bool = True,
    ):
        self.company_token = company_board_token
        self.company_name = company_name or company_board_token.upper()
        # Injectable client so tests can use httpx.MockTransport instead of the network.
        self._client = client
        self.verbose = verbose

    @property
    @abstractmethod
    def api_url(self) -> str: ...

    @abstractmethod
    def _parse(self, payload: Any) -> list[JobPosting]: ...

    def _log(self, message: str) -> None:
        if self.verbose:
            print(message)

    async def fetch_active_jobs(self) -> list[JobPosting]:
        self._log(f"[*] Fetching live jobs for '{self.company_token}' from {self.source}...")

        if self._client is not None:
            response = await self._client.get(self.api_url)
        else:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.get(self.api_url)
        response.raise_for_status()

        payload = response.json()
        validated_jobs = self._parse(payload)
        total = len(payload) if isinstance(payload, list) else len(payload.get("jobs", []))
        self._log(f"[+] Successfully validated and ingested {len(validated_jobs)}/{total} jobs.")
        return validated_jobs
