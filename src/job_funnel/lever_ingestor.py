from typing import Any

from pydantic import ValidationError

from job_funnel.base_ingestor import BaseIngestor
from job_funnel.schemas import JobPosting
from job_funnel.text import clean_html


class LeverIngestor(BaseIngestor):
    source = "lever_api"

    @property
    def api_url(self) -> str:
        return f"https://api.lever.co/v0/postings/{self.company_token}?mode=json"

    @staticmethod
    def _remote_flag(job: dict[str, Any]) -> bool | None:
        workplace = (job.get("workplaceType") or "").lower()
        if workplace == "remote":
            return True
        if workplace in ("hybrid", "on-site", "onsite"):
            return False
        return None

    @staticmethod
    def _description(job: dict[str, Any]) -> str:
        """Lever splits the JD into an intro, titled `lists` (responsibilities...) and a closing."""
        parts = [job.get("descriptionPlain") or clean_html(job.get("description"))]
        for section in job.get("lists") or []:
            parts.append(f"{section.get('text', '')}: {clean_html(section.get('content'))}")
        if job.get("additionalPlain"):
            parts.append(job["additionalPlain"])
        return " ".join(" ".join(parts).split())

    @staticmethod
    def _location(job: dict[str, Any]) -> str:
        categories = job.get("categories") or {}
        locations = categories.get("allLocations") or [categories.get("location")]
        return "; ".join(loc for loc in locations if loc) or "Remote / Unspecified"

    def _parse(self, payload: list[dict[str, Any]]) -> list[JobPosting]:
        # Lever returns a bare JSON list, not {"jobs": [...]}
        validated_jobs = []
        for job in payload:
            try:
                posting = JobPosting(
                    source=self.source,
                    external_id=str(job.get("id")),
                    title=job.get("text", ""),
                    company=self.company_name,
                    board_token=self.company_token,
                    description=self._description(job),
                    location=self._location(job),
                    url=job.get("hostedUrl"),
                    remote=self._remote_flag(job),
                )
                validated_jobs.append(posting)
            except ValidationError:
                self._log(f"[-] Dropped job {job.get('id')} due to missing fields.")
        return validated_jobs
