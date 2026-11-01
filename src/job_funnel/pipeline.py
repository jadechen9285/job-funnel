import asyncio
from collections import Counter
from collections.abc import Sequence

from job_funnel.ashby_ingestor import AshbyIngestor
from job_funnel.base_ingestor import BaseIngestor
from job_funnel.greenhouse_ingestor import GreenhouseIngestor
from job_funnel.heuristic_gate import (
    AntiKeywordRule,
    HeuristicGate,
    LocationRule,
    TitleTaxonomyRule,
)
from job_funnel.schemas import JobPosting


def default_gate() -> HeuristicGate:
    return HeuristicGate(rules=[
        AntiKeywordRule(),       # Quickest string match to drop obvious trash
        TitleTaxonomyRule(),     # Core target matching
        LocationRule(require_remote=False) # Set to True if you only want remote roles
    ])


async def ingest_all(ingestors: Sequence[BaseIngestor]) -> list[JobPosting]:
    """Fetches all sources concurrently; a failing source is logged and skipped."""
    results = await asyncio.gather(
        *(ingestor.fetch_active_jobs() for ingestor in ingestors), return_exceptions=True
    )
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


async def main():
    print("="*50)
    print("JOB-FUNNEL PIPELINE: STAGE 1 (INGEST & GATE)")
    print("="*50)

    # 1. Initialize dependencies
    ingestors: list[BaseIngestor] = [
        GreenhouseIngestor(company_board_token="discord"),
        AshbyIngestor(company_board_token="openai"),
    ]
    gate = default_gate()

    # 2. Extract Data
    raw_jobs = await ingest_all(ingestors)
    if not raw_jobs:
        print("[-] No jobs returned from ingestors. Exiting.")
        return

    # 3. Filter Data
    print("\n[*] Applying Heuristic Gate to ingested jobs...")
    surviving_jobs, pruned = apply_gate(gate, raw_jobs)
    for job, reason in pruned:
        print(f"  [X] Pruned [{job.source}:{job.external_id}]: {job.title} -> {reason}")

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

    if surviving_jobs:
        print("SURVIVOR SAMPLE (Ready for LLM):")
        sample = surviving_jobs[0]
        print(f" - {sample.title} ({sample.location}) | ID: {sample.external_id}")

if __name__ == "__main__":
    # Ensure you are running this from the project root directory using python -m
    asyncio.run(main())
