import asyncio
import httpx
import re
import html
from typing import List
from pydantic import ValidationError

# Import the shared schema
from job_funnel.schemas import JobPosting

class GreenhouseIngestor:
    def __init__(self, company_board_token: str):
        self.company_token = company_board_token
        self.api_url = f"https://boards-api.greenhouse.io/v1/boards/{self.company_token}/jobs?content=true"

    def _clean_html(self, raw_html: str) -> str:
        if not raw_html:
            return "No description provided."
        text = re.sub(r'<[^>]+>', ' ', raw_html)
        text = html.unescape(text)
        return " ".join(text.split())

    async def fetch_active_jobs(self) -> List[JobPosting]:
        print(f"[*] Fetching live jobs for '{self.company_token}' from Greenhouse API...")
        
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(self.api_url)
            response.raise_for_status()
            
            payload = response.json()
            jobs_data = payload.get("jobs", [])
            
            validated_jobs = []
            for job in jobs_data:
                try:
                    posting = JobPosting(
                        external_id=str(job.get("id")),
                        title=job.get("title", ""),
                        company=self.company_token.upper(),
                        description=self._clean_html(job.get("content", "")),
                        location=job.get("location", {}).get("name", "Remote / Unspecified"),
                        url=job.get("absolute_url")
                    )
                    validated_jobs.append(posting)
                except ValidationError as e:
                    print(f"[-] Dropped job {job.get('id')} due to missing fields.")
                    
            print(f"[+] Successfully validated and ingested {len(validated_jobs)}/{len(jobs_data)} jobs.")
            return validated_jobs