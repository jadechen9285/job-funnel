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

    def __init__(self, company_board_token: str, client: httpx.AsyncClient | None = None):
        self.company_token = company_board_token
        # Injectable client so tests can use httpx.MockTransport instead of the network.
        self._client = client

    @property
    @abstractmethod
    def api_url(self) -> str: ...

    @abstractmethod
    def _parse(self, payload: dict[str, Any]) -> list[JobPosting]: ...

    async def fetch_active_jobs(self) -> list[JobPosting]:
        print(f"[*] Fetching live jobs for '{self.company_token}' from {self.source}...")

        if self._client is not None:
            response = await self._client.get(self.api_url)
        else:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.get(self.api_url)
        response.raise_for_status()

        payload = response.json()
        validated_jobs = self._parse(payload)
        total = len(payload.get("jobs", []))
        print(f"[+] Successfully validated and ingested {len(validated_jobs)}/{total} jobs.")
        return validated_jobs
