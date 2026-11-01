from datetime import UTC, datetime

from pydantic import BaseModel, Field, HttpUrl


class JobPosting(BaseModel):
    source: str  # set by each ingestor, e.g. "greenhouse_api", "ashby_api"
    external_id: str
    title: str
    company: str
    description: str
    location: str
    url: HttpUrl
    remote: bool | None = None  # structured remote flag when the source provides one
    discovered_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @property
    def is_remote(self) -> bool:
        """Uses the source's remote flag if known, otherwise infers from the location string."""
        if self.remote is not None:
            return self.remote
        return "remote" in self.location.lower()
