import asyncio
from typing import Any

from pydantic import ValidationError

from job_funnel.base_ingestor import BaseIngestor
from job_funnel.schemas import JobPosting
from job_funnel.text import clean_html


class GreenhouseIngestor(BaseIngestor):
    source = "greenhouse_api"
    detail_concurrency = 5

    @property
    def board_url(self) -> str:
        return f"https://boards-api.greenhouse.io/v1/boards/{self.company_token}/jobs"

    @property
    def api_url(self) -> str:
        return f"{self.board_url}?content=true"

    async def fetch_active_jobs(self) -> list[JobPosting]:
        """With a `title_filter`, list titles only (small payload) and fetch content just for matches."""
        if self.title_filter is None:
            return await super().fetch_active_jobs()

        self._log(f"[*] Listing '{self.company_token}' on Greenhouse (title-first)...")
        response = await self._request("GET", self.board_url)
        response.raise_for_status()
        listings = response.json().get("jobs", [])
        wanted = [job for job in listings if self._wanted(job.get("title", ""))]

        semaphore = asyncio.Semaphore(self.detail_concurrency)

        async def _detail(job: dict[str, Any]) -> dict[str, Any]:
            async with semaphore:
                detail = await self._request("GET", f"{self.board_url}/{job.get('id')}")
            # Fall back to the listing (no description) if the detail call fails.
            return detail.json() if detail.status_code == 200 else job

        details = await asyncio.gather(*(_detail(job) for job in wanted))
        jobs = self._parse({"jobs": details})
        self._log(f"[+] {len(listings)} listed, {len(wanted)} title matches, {len(jobs)} ingested.")
        return jobs

    def _parse(self, payload: dict[str, Any]) -> list[JobPosting]:
        validated_jobs = []
        for job in payload.get("jobs", []):
            try:
                posting = JobPosting(
                    source=self.source,
                    external_id=str(job.get("id")),
                    title=job.get("title", ""),
                    company=self.company_name,
                    board_token=self.company_token,
                    description=clean_html(job.get("content", "")),
                    location=(job.get("location") or {}).get("name", "Remote / Unspecified"),
                    url=job.get("absolute_url"),
                )
                validated_jobs.append(posting)
            except ValidationError:
                self._log(f"[-] Dropped job {job.get('id')} due to missing fields.")
        return validated_jobs
