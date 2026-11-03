import asyncio
from collections import Counter
from collections.abc import Sequence
from datetime import UTC, datetime

import httpx

from job_funnel.ashby_ingestor import AshbyIngestor
from job_funnel.base_ingestor import BaseIngestor
from job_funnel.boards import (
    DEFAULT_REGISTRY_PATH,
    INGESTORS,
    BoardRegistry,
    ingestor_for,
)
from job_funnel.greenhouse_ingestor import GreenhouseIngestor
from job_funnel.heuristic_gate import (
    AntiKeywordRule,
    HeuristicGate,
    LocationRule,
    TitleTaxonomyRule,
)
from job_funnel.schemas import JobPosting

_TITLE_RULE = TitleTaxonomyRule()
_ANTI_RULE = AntiKeywordRule()


def title_prefilter(title: str) -> bool:
    """Cheap title-only version of the gate, used by ingestors to skip fetching irrelevant descriptions."""
    return bool(_TITLE_RULE.pattern.search(title)) and not _ANTI_RULE.anti_pattern.search(title)


def default_gate() -> HeuristicGate:
    return HeuristicGate(rules=[
        AntiKeywordRule(),       # Quickest string match to drop obvious trash
        TitleTaxonomyRule(),     # Core target matching
        LocationRule(require_remote=False) # Set to True if you only want remote roles
    ])


async def ingest_all(
    ingestors: Sequence[BaseIngestor], max_concurrency: int = 20, progress_every: int = 250
) -> list[JobPosting]:
    """Fetches all sources concurrently (bounded); a failing source is logged and skipped."""
    semaphore = asyncio.Semaphore(max_concurrency)
    done = 0

    async def _fetch(ingestor: BaseIngestor) -> list[JobPosting]:
        nonlocal done
        async with semaphore:
            try:
                return await ingestor.fetch_active_jobs()
            finally:
                done += 1
                if progress_every and done % progress_every == 0:
                    print(f"[*] {done}/{len(ingestors)} boards fetched...")

    results = await asyncio.gather(*(_fetch(i) for i in ingestors), return_exceptions=True)
    raw_jobs: list[JobPosting] = []
    for ingestor, result in zip(ingestors, results):
        if isinstance(result, Exception):
            print(f"[-] Source {ingestor.source}/{ingestor.company_token} failed: {result!r}")
            continue
        raw_jobs.extend(result)
    return raw_jobs


def apply_gate(
    gate: HeuristicGate, jobs: Sequence[JobPosting]
) -> tuple[list[JobPosting], list[tuple[JobPosting, str]]]:
    """Splits jobs into survivors and (job, reason) rejections."""
    surviving_jobs, pruned = [], []
    for job in jobs:
        passed, reasons = gate.process(job)
        if passed:
            surviving_jobs.append(job)
        else:
            pruned.append((job, reasons[0]))
    return surviving_jobs, pruned


def build_ingestors(
    registry: BoardRegistry,
    client: httpx.AsyncClient | None = None,
    title_filter=title_prefilter,
) -> list[BaseIngestor]:
    """One ingestor per active registry board; falls back to two demo boards if the registry is empty."""
    active = registry.active()
    if not active:
        print(f"[!] No active boards in {DEFAULT_REGISTRY_PATH}; run `python -m job_funnel.discovery` first.")
        print("[!] Falling back to demo boards (greenhouse/discord, ashby/openai).")
        return [
            GreenhouseIngestor(company_board_token="discord", client=client),
            AshbyIngestor(company_board_token="openai", client=client),
        ]
    return [ingestor_for(board, client=client, title_filter=title_filter) for board in active]


def record_board_stats(
    registry: BoardRegistry, raw_jobs: Sequence[JobPosting], surviving_jobs: Sequence[JobPosting]
) -> None:
    """Writes per-board yield back to the registry so we can rank/prune boards over time."""
    ats_by_source = {cls.source: ats for ats, cls in INGESTORS.items()}
    ingested = Counter((ats_by_source[j.source], j.board_token) for j in raw_jobs)
    gated_in = Counter((ats_by_source[j.source], j.board_token) for j in surviving_jobs)
    now = datetime.now(UTC)
    for key, count in ingested.items():
        if key in registry.boards:
            registry.boards[key] = registry.boards[key].model_copy(
                update={"n_jobs": count, "n_gated_in": gated_in[key], "last_checked": now}
            )


async def main():
    print("="*50)
    print("JOB-FUNNEL PIPELINE: STAGE 1 (INGEST & GATE)")
    print("="*50)

    # 1. Initialize dependencies
    registry = BoardRegistry.load(DEFAULT_REGISTRY_PATH)
    gate = default_gate()

    # 2. Extract Data (one shared connection pool for all boards)
    limits = httpx.Limits(max_connections=100, max_keepalive_connections=10)
    timeout = httpx.Timeout(30.0, pool=120.0)
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True, limits=limits) as client:
        ingestors = build_ingestors(registry, client=client)
        print(f"[*] Fetching {len(ingestors)} boards...")
        raw_jobs = await ingest_all(ingestors)
    if not raw_jobs:
        print("[-] No jobs returned from ingestors. Exiting.")
        return

    # 3. Filter Data
    print("\n[*] Applying Heuristic Gate to ingested jobs...")
    surviving_jobs, pruned = apply_gate(gate, raw_jobs)
    if len(registry):
        record_board_stats(registry, raw_jobs, surviving_jobs)
        registry.save(DEFAULT_REGISTRY_PATH)

    print("\n" + "="*50)
    print("PIPELINE SUMMARY")
    print("="*50)
    ingested_by_source = Counter(job.source for job in raw_jobs)
    surviving_by_source = Counter(job.source for job in surviving_jobs)
    for source, count in ingested_by_source.items():
        print(f"  {source:<16} ingested={count:<5} surviving={surviving_by_source[source]}")
    print(f"Total Ingested:  {len(raw_jobs)}")
    print(f"Total Surviving: {len(surviving_jobs)}")
    print(f"Prune Rate:      {len(pruned) / len(raw_jobs) * 100:.1f}%\n")

    top_boards = Counter(job.company for job in surviving_jobs).most_common(10)
    print("TOP BOARDS BY SURVIVORS:", ", ".join(f"{c} ({n})" for c, n in top_boards))

    if surviving_jobs:
        print("\nSURVIVOR SAMPLE (Ready for LLM):")
        for sample in surviving_jobs[:10]:
            print(f" - [{sample.company}] {sample.title} ({sample.location})")

if __name__ == "__main__":
    # Ensure you are running this from the project root directory using python -m
    asyncio.run(main())
