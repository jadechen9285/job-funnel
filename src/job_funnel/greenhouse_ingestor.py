from typing import Any

from pydantic import ValidationError

from job_funnel.base_ingestor import BaseIngestor
from job_funnel.schemas import JobPosting
from job_funnel.text import clean_html


class GreenhouseIngestor(BaseIngestor):
    source = "greenhouse_api"

    @property
    def api_url(self) -> str:
        return f"https://boards-api.greenhouse.io/v1/boards/{self.company_token}/jobs?content=true"

    def _parse(self, payload: dict[str, Any]) -> list[JobPosting]:
        validated_jobs = []
        for job in payload.get("jobs", []):
            try:
                posting = JobPosting(
                    source=self.source,
                    external_id=str(job.get("id")),
                    title=job.get("title", ""),
                    company=self.company_token.upper(),
                    description=clean_html(job.get("content", "")),
                    location=(job.get("location") or {}).get("name", "Remote / Unspecified"),
                    url=job.get("absolute_url"),
                )
                validated_jobs.append(posting)
            except ValidationError:
                print(f"[-] Dropped job {job.get('id')} due to missing fields.")
        return validated_jobs
