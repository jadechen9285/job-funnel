from pydantic import BaseModel, HttpUrl, Field
from datetime import datetime, timezone

class JobPosting(BaseModel):
    source: str = "greenhouse_api"
    external_id: str
    title: str
    company: str
    description: str
    location: str
    url: HttpUrl
    discovered_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    
    @property
    def is_remote(self) -> bool:
        """Infers remote status from the location string."""
        return "remote" in self.location.lower()