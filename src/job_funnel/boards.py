"""Board registry: which (ATS, token) job boards we know about and whether they're alive.

Greenhouse / Ashby / Lever / Workday only expose per-company endpoints, so discovering *which*
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
from job_funnel.base_ingestor import BaseIngestor, TitleFilter, request_with_retry
from job_funnel.greenhouse_ingestor import GreenhouseIngestor
from job_funnel.lever_ingestor import LeverIngestor
from job_funnel.workday_ingestor import WorkdayIngestor

Ats = Literal["greenhouse", "ashby", "lever", "workday"]
BoardRef = tuple[str, str]  # (ats, token)

DEFAULT_REGISTRY_PATH = Path("data/boards.csv")

INGESTORS: dict[str, type[BaseIngestor]] = {
    "greenhouse": GreenhouseIngestor,
    "ashby": AshbyIngestor,
    "lever": LeverIngestor,
    "workday": WorkdayIngestor,
}

# How long until a board is re-validated, by status.
REVALIDATE_AFTER: dict[str, timedelta] = {
    "unverified": timedelta(0),
    "active": timedelta(days=7),
    "dormant": timedelta(days=30),
    "dead": timedelta(days=90),
}
DORMANT_AFTER_ZERO_CHECKS = 3

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
# Workday career sites need three parts: tenant, data-center shard (wdN) and site name.
WORKDAY_PATTERN = re.compile(
    r"([a-z0-9-]+)\.(wd\d+)\.myworkdayjobs\.com/(?:[a-z]{2}(?:-[a-z]{2})?/)?([A-Za-z0-9_-]+)",
    re.IGNORECASE,
)
# Path segments that look like tokens but aren't boards.
_NOT_TOKENS = {"embed", "api", "v1", "v0", "jobs", "posting-api", "job-board", "job_board"}
_NOT_WORKDAY_SITES = {"wday", "job", "jobs", "login", "robots", "sitemap", "favicon", "search", "details"}
_UUID_LIKE = re.compile(r"^[0-9a-f]{8}-?[0-9a-f]{4}-?[0-9a-f]{4}-?[0-9a-f]{4}-?[0-9a-f]{12}$")


def is_plausible_token(token: str) -> bool:
    """Rejects URL fragments that can't be company boards (job UUIDs, encoded spaces, junk)."""
    return (
        0 < len(token) <= 60
        and "%" not in token
        and " " not in token
        and not _UUID_LIKE.match(token)
        and token not in _NOT_TOKENS
    )


def extract_board_refs(text: str) -> set[BoardRef]:
    """Finds every ATS board referenced in `text` (raw or HTML-escaped)."""
    text = html.unescape(text or "")
    refs: set[BoardRef] = set()
    for ats, pattern in ATS_PATTERNS.items():
        for match in pattern.finditer(text):
            token = next(g for g in match.groups() if g).strip(".-_%").lower()
            if is_plausible_token(token):
                refs.add((ats, token))
    for tenant, wd, site in WORKDAY_PATTERN.findall(text):
        tenant, site = tenant.lower(), site.strip(".-_").lower()
        if site and site not in _NOT_WORKDAY_SITES and is_plausible_token(site):
            refs.add(("workday", f"{tenant}|{wd.lower()}|{site}"))
    return refs


class Board(BaseModel):
    ats: Ats
    token: str
    company_name: str | None = None
    discovered_via: str = "manual"
    first_seen: datetime = Field(default_factory=lambda: datetime.now(UTC))
    last_checked: datetime | None = None
    # dormant = alive but empty on several checks in a row; skipped by the pipeline
    status: Literal["unverified", "active", "dormant", "dead"] = "unverified"
    n_jobs: int | None = None
    n_gated_in: int | None = None
    zero_streak: int = 0

    @property
    def key(self) -> BoardRef:
        return (self.ats, self.token)

    def is_stale(self, max_age: timedelta | None = None) -> bool:
        """Due for re-validation; default max age depends on status (see REVALIDATE_AFTER)."""
        max_age = REVALIDATE_AFTER[self.status] if max_age is None else max_age
        return self.last_checked is None or datetime.now(UTC) - self.last_checked > max_age


def ingestor_for(
    board: Board,
    client: httpx.AsyncClient | None = None,
    verbose: bool = False,
    title_filter: TitleFilter | None = None,
) -> BaseIngestor:
    return INGESTORS[board.ats](
        board.token,
        client=client,
        company_name=board.company_name,
        verbose=verbose,
        title_filter=title_filter,
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


DEAD_STATUSES = {404, 410, 422}  # Workday answers 422 for a wrong shard/site


def _checked(board: Board, n_jobs: int, company_name: str | None) -> Board:
    """Marks a reachable board active, or dormant after repeated empty checks."""
    zero_streak = board.zero_streak + 1 if n_jobs == 0 else 0
    status = "dormant" if zero_streak >= DORMANT_AFTER_ZERO_CHECKS else "active"
    return board.model_copy(
        update={
            "status": status,
            "last_checked": datetime.now(UTC),
            "n_jobs": n_jobs,
            "zero_streak": zero_streak,
            "company_name": company_name or board.company_name or board.token.upper(),
        }
    )


def _dead(board: Board) -> Board:
    return board.model_copy(update={"status": "dead", "last_checked": datetime.now(UTC), "n_jobs": 0})


async def validate_board(board: Board, client: httpx.AsyncClient) -> Board:
    """One or two cheap requests per board: 404-ish -> dead, else active/dormant with a job count."""
    try:
        if board.ats == "greenhouse":
            response = await request_with_retry(client, "GET", f"https://boards-api.greenhouse.io/v1/boards/{board.token}/jobs")
            if response.status_code in DEAD_STATUSES:
                return _dead(board)
            response.raise_for_status()
            jobs = response.json().get("jobs", [])
            name = jobs[0].get("company_name") if jobs else None
            return _checked(board, len(jobs), name)
        if board.ats == "workday":
            ingestor = WorkdayIngestor(board.token)
            response = await request_with_retry(
                client, "POST", ingestor.api_url,
                json={"appliedFacets": {}, "limit": 1, "offset": 0, "searchText": ""},
            )
            if response.status_code in DEAD_STATUSES:
                return _dead(board)
            response.raise_for_status()
            return _checked(board, response.json().get("total") or 0, ingestor.company_name)
        response = await request_with_retry(client, "GET", ingestor_for(board).api_url)
        if response.status_code in DEAD_STATUSES:
            return _dead(board)
        response.raise_for_status()
        payload = response.json()
        n_jobs = len(payload) if isinstance(payload, list) else len(payload.get("jobs", []))
        return _checked(board, n_jobs, None)
    except (httpx.HTTPError, ValueError) as e:
        # Transient failure: keep previous status, retry on the next run.
        print(f"[-] Could not validate {board.ats}/{board.token}: {e!r}")
        return board


async def validate_boards(
    boards: Iterable[Board], client: httpx.AsyncClient, max_concurrency: int = 20
) -> list[Board]:
    semaphore = asyncio.Semaphore(max_concurrency)

    async def _one(board: Board) -> Board:
        async with semaphore:
            return await validate_board(board, client)

    return await asyncio.gather(*(_one(b) for b in boards))
