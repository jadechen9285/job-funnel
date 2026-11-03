from typing import Any

from pydantic import ValidationError

from job_funnel.base_ingestor import BaseIngestor
from job_funnel.schemas import JobPosting
from job_funnel.text import clean_html


class AshbyIngestor(BaseIngestor):
    source = "ashby_api"

    @property
    def api_url(self) -> str:
        return f"https://api.ashbyhq.com/posting-api/job-board/{self.company_token}"

    @staticmethod
    def _remote_flag(job: dict[str, Any]) -> bool | None:
        """Ashby exposes structured remote info; None means 'unknown, infer from location'.

        `workplaceType` wins over `isRemote`: Ashby boards often set isRemote=True on Hybrid roles.
        """
        workplace = job.get("workplaceType")
        if workplace == "Remote":
            return True
        if workplace in ("Hybrid", "OnSite"):
            return False
        return job.get("isRemote")

    @staticmethod
    def _location(job: dict[str, Any]) -> str:
        primary = job.get("location") or "Remote / Unspecified"
        secondary = [
            loc.get("location") for loc in job.get("secondaryLocations") or [] if loc.get("location")
        ]
        return "; ".join([primary, *secondary])

    def _parse(self, payload: dict[str, Any]) -> list[JobPosting]:
        validated_jobs = []
        for job in payload.get("jobs", []):
            if job.get("isListed") is False:
                continue
            try:
                posting = JobPosting(
                    source=self.source,
                    external_id=str(job.get("id")),
                    title=job.get("title", ""),
                    company=self.company_name,
                    board_token=self.company_token,
                    description=job.get("descriptionPlain") or clean_html(job.get("descriptionHtml")),
                    location=self._location(job),
                    url=job.get("jobUrl"),
                    remote=self._remote_flag(job),
                )
                validated_jobs.append(posting)
            except ValidationError:
                self._log(f"[-] Dropped job {job.get('id')} due to missing fields.")
        return validated_jobs
