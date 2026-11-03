"""Board registry: which (ATS, token) job boards we know about and whether they're alive.

Greenhouse / Ashby / Lever only expose per-company endpoints, so discovering *which*
companies to ingest is its own problem. Discoverers (see `discovery.py`) mine text for
ATS links, `extract_board_refs` turns links into `(ats, token)` pairs, and the registry
persists them to a hand-editable CSV that the pipeline sweeps.
"""

import asyncio
import csv
import html
import re
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import ClassVar, Literal

import httpx
from pydantic import BaseModel, Field

from job_funnel.ashby_ingestor import AshbyIngestor
from job_funnel.base_ingestor import BaseIngestor
from job_funnel.greenhouse_ingestor import GreenhouseIngestor
from job_funnel.lever_ingestor import LeverIngestor

Ats = Literal["greenhouse", "ashby", "lever"]
BoardRef = tuple[str, str]  # (ats, token)

DEFAULT_REGISTRY_PATH = Path("data/boards.csv")

INGESTORS: dict[str, type[BaseIngestor]] = {
    "greenhouse": GreenhouseIngestor,
    "ashby": AshbyIngestor,
    "lever": LeverIngestor,
}

# Each pattern captures the board token from any URL shape that ATS uses.
ATS_PATTERNS: dict[str, re.Pattern] = {
    "greenhouse": re.compile(
        r"(?:job-boards|boards)(?:\.eu)?\.greenhouse\.io/"
        r"(?:embed/job_board(?:/js)?\?for=)?([A-Za-z0-9_-]+)"
        r"|boards-api(?:\.eu)?\.greenhouse\.io/v1/boards/([A-Za-z0-9_-]+)",
        re.IGNORECASE,
    ),
    "ashby": re.compile(
        r"jobs\.ashbyhq\.com/([A-Za-z0-9_.%-]+)"
        r"|api\.ashbyhq\.com/posting-api/job-board/([A-Za-z0-9_.%-]+)",
        re.IGNORECASE,
    ),
    "lever": re.compile(
        r"jobs(?:\.eu)?\.lever\.co/([A-Za-z0-9_-]+)"
        r"|api(?:\.eu)?\.lever\.co/v0/postings/([A-Za-z0-9_-]+)",
        re.IGNORECASE,
    ),
}
# Path segments that look like tokens but aren't boards.
_NOT_TOKENS = {"embed", "api", "v1", "v0", "jobs", "posting-api", "job-board", "job_board"}


def extract_board_refs(text: str) -> set[BoardRef]:
    """Finds every ATS board referenced in `text` (raw or HTML-escaped)."""
    text = html.unescape(text or "")
    refs: set[BoardRef] = set()
    for ats, pattern in ATS_PATTERNS.items():
        for match in pattern.finditer(text):
            token = next(g for g in match.groups() if g).strip(".-_%").lower()
            if token and token not in _NOT_TOKENS:
                refs.add((ats, token))
    return refs


class Board(BaseModel):
    ats: Ats
    token: str
    company_name: str | None = None
    discovered_via: str = "manual"
    first_seen: datetime = Field(default_factory=lambda: datetime.now(UTC))
    last_checked: datetime | None = None
    status: Literal["unverified", "active", "dead"] = "unverified"
    n_jobs: int | None = None
    n_gated_in: int | None = None

    @property
    def key(self) -> BoardRef:
        return (self.ats, self.token)

    def is_stale(self, max_age: timedelta) -> bool:
        return self.last_checked is None or datetime.now(UTC) - self.last_checked > max_age


def ingestor_for(board: Board, client: httpx.AsyncClient | None = None, verbose: bool = False) -> BaseIngestor:
    return INGESTORS[board.ats](
        board.token, client=client, company_name=board.company_name, verbose=verbose
    )


class BoardRegistry:
    """In-memory set of boards keyed on (ats, token), persisted as CSV."""

    FIELDS: ClassVar[list[str]] = list(Board.model_fields)

    def __init__(self, boards: Iterable[Board] = ()):
        self.boards: dict[BoardRef, Board] = {b.key: b for b in boards}

    def __len__(self) -> int:
        return len(self.boards)

    @classmethod
    def load(cls, path: Path = DEFAULT_REGISTRY_PATH) -> "BoardRegistry":
        if not path.exists():
            return cls()
        with path.open(newline="") as f:
            rows = [{k: v for k, v in row.items() if v != ""} for row in csv.DictReader(f)]
        return cls(Board(**row) for row in rows)

    def save(self, path: Path = DEFAULT_REGISTRY_PATH) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=self.FIELDS)
            writer.writeheader()
            for board in sorted(self.boards.values(), key=lambda b: b.key):
                row = board.model_dump(mode="json")
                writer.writerow({k: "" if v is None else v for k, v in row.items()})

    def merge(self, refs: Iterable[BoardRef], via: str) -> list[Board]:
        """Adds unseen boards as `unverified`; existing rows are left untouched. Returns new boards."""
        added = []
        for ats, token in refs:
            if (ats, token) not in self.boards:
                board = Board(ats=ats, token=token, discovered_via=via)
                self.boards[board.key] = board
                added.append(board)
        return added

    def active(self) -> list[Board]:
        return [b for b in self.boards.values() if b.status == "active"]


async def validate_board(board: Board, client: httpx.AsyncClient) -> Board:
    """One cheap request per board: 404 -> dead, else active with job count (and name for Greenhouse)."""
    now = datetime.now(UTC)
    try:
        if board.ats == "greenhouse":
            meta = await client.get(f"https://boards-api.greenhouse.io/v1/boards/{board.token}")
            if meta.status_code == 404:
                return board.model_copy(update={"status": "dead", "last_checked": now, "n_jobs": 0})
            meta.raise_for_status()
            jobs = await client.get(f"https://boards-api.greenhouse.io/v1/boards/{board.token}/jobs")
            jobs.raise_for_status()
            name = meta.json().get("name") or board.company_name
            n_jobs = len(jobs.json().get("jobs", []))
        else:
            response = await client.get(ingestor_for(board).api_url)
            if response.status_code == 404:
                return board.model_copy(update={"status": "dead", "last_checked": now, "n_jobs": 0})
            response.raise_for_status()
            payload = response.json()
            n_jobs = len(payload) if isinstance(payload, list) else len(payload.get("jobs", []))
            name = board.company_name
    except (httpx.HTTPError, ValueError) as e:
        # Transient failure: keep previous status, retry on the next run.
        print(f"[-] Could not validate {board.ats}/{board.token}: {e!r}")
        return board
    return board.model_copy(
        update={
            "status": "active",
            "last_checked": now,
            "n_jobs": n_jobs,
            "company_name": name or board.token.upper(),
        }
    )


async def validate_boards(
    boards: Iterable[Board], client: httpx.AsyncClient, max_concurrency: int = 10
) -> list[Board]:
    semaphore = asyncio.Semaphore(max_concurrency)

    async def _one(board: Board) -> Board:
        async with semaphore:
            return await validate_board(board, client)

    return await asyncio.gather(*(_one(b) for b in boards))
