import asyncio
from typing import Any

from pydantic import ValidationError

from job_funnel.base_ingestor import BaseIngestor
from job_funnel.schemas import JobPosting
from job_funnel.text import clean_html

# Workday tenants can have thousands of postings, so instead of listing everything we
# use the board's own (fuzzy) keyword search and let the heuristic gate clean up.
SEARCH_TERMS = [
    "data scientist",
    "data science",
    "machine learning",
    "applied scientist",
    "ML engineer",
    "AI engineer",
]


class WorkdayIngestor(BaseIngestor):
    """Workday career sites ("{tenant}.wd{N}.myworkdayjobs.com/{site}").

    The board token packs all three parts: "tenant|wdN|site".
    """

    source = "workday_api"
    page_size = 20
    max_results_per_term = 200
    detail_concurrency = 5

    def __init__(self, company_board_token: str, *args: Any, search_terms: list[str] | None = None, **kwargs: Any):
        super().__init__(company_board_token, *args, **kwargs)
        self.tenant, self.wd, self.site = company_board_token.split("|")
        if self.company_name == company_board_token.upper():
            self.company_name = self.tenant.upper()
        self.search_terms = search_terms or SEARCH_TERMS

    @property
    def base_url(self) -> str:
        return f"https://{self.tenant}.{self.wd}.myworkdayjobs.com/wday/cxs/{self.tenant}/{self.site}"

    @property
    def api_url(self) -> str:
        return f"{self.base_url}/jobs"

    @property
    def public_url(self) -> str:
        return f"https://{self.tenant}.{self.wd}.myworkdayjobs.com/{self.site}"

    async def _search(self, term: str) -> list[dict[str, Any]]:
        postings: list[dict[str, Any]] = []
        total = None
        while True:
            response = await self._request(
                "POST",
                self.api_url,
                json={"appliedFacets": {}, "limit": self.page_size, "offset": len(postings), "searchText": term},
            )
            response.raise_for_status()
            data = response.json()
            if total is None:  # Workday only reports `total` on the first page
                total = data.get("total") or 0
            page = data.get("jobPostings") or []
            postings.extend(page)
            if not page or len(postings) >= min(total, self.max_results_per_term):
                return postings

    async def _detail(self, external_path: str) -> dict[str, Any] | None:
        response = await self._request("GET", f"{self.base_url}{external_path}")
        if response.status_code != 200:
            self._log(f"[-] Workday detail {external_path} -> HTTP {response.status_code}")
            return None
        return response.json()

    async def fetch_active_jobs(self) -> list[JobPosting]:
        self._log(f"[*] Searching Workday site '{self.company_token}'...")
        listings: dict[str, dict[str, Any]] = {}
        for term in self.search_terms:
            for posting in await self._search(term):
                if posting.get("externalPath"):
                    listings.setdefault(posting["externalPath"], posting)

        wanted = [p for p in listings.values() if self._wanted(p.get("title", ""))]
        semaphore = asyncio.Semaphore(self.detail_concurrency)

        async def _one(posting: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any] | None]:
            async with semaphore:
                return posting, await self._detail(posting["externalPath"])

        pairs = await asyncio.gather(*(_one(p) for p in wanted))
        jobs = self._parse(pairs)
        self._log(f"[+] {len(listings)} search hits, {len(wanted)} title matches, {len(jobs)} ingested.")
        return jobs

    @staticmethod
    def _remote_flag(info: dict[str, Any]) -> bool | None:
        remote_type = (info.get("remoteType") or "").lower()
        if not remote_type:
            return None
        return "remote" in remote_type and "hybrid" not in remote_type

    def _parse(self, payload: list[tuple[dict[str, Any], dict[str, Any] | None]]) -> list[JobPosting]:
        jobs = []
        for listing, detail in payload:
            info = (detail or {}).get("jobPostingInfo") or {}
            try:
                jobs.append(
                    JobPosting(
                        source=self.source,
                        external_id=str(info.get("jobReqId") or info.get("id") or listing["externalPath"]),
                        title=info.get("title") or listing.get("title", ""),
                        company=self.company_name,
                        board_token=self.company_token,
                        description=clean_html(info.get("jobDescription")),
                        location=info.get("location") or listing.get("locationsText") or "Remote / Unspecified",
                        url=info.get("externalUrl") or f"{self.public_url}{listing['externalPath']}",
                        remote=self._remote_flag(info),
                    )
                )
            except (ValidationError, KeyError):
                self._log(f"[-] Dropped Workday job {listing.get('externalPath')} due to missing fields.")
        return jobs
