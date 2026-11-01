import asyncio
from job_funnel.greenhouse_ingestor import GreenhouseIngestor
from job_funnel.heuristic_gate import (
    HeuristicGate, 
    TitleTaxonomyRule, 
    LocationRule,
    AntiKeywordRule
)

async def main():
    print("="*50)
    print("JOB-FUNNEL PIPELINE: STAGE 1 (INGEST & GATE)")
    print("="*50)

    # 1. Initialize dependencies
    # Testing with 'discord' as a reliable Greenhouse endpoint
    ingestor = GreenhouseIngestor(company_board_token="discord") 
    
    # Configure strict heuristic rules
    gate = HeuristicGate(rules=[
        AntiKeywordRule(),       # Quickest string match to drop obvious trash
        TitleTaxonomyRule(),     # Core target matching
        LocationRule(require_remote=False) # Set to True if you only want remote roles
    ])

    # 2. Extract Data
    raw_jobs = await ingestor.fetch_active_jobs()
    if not raw_jobs:
        print("[-] No jobs returned from ingestor. Exiting.")
        return

    # 3. Filter Data
    surviving_jobs = []
    print("\n[*] Applying Heuristic Gate to ingested jobs...")
    
    for job in raw_jobs:
        passed, reasons = gate.process(job)
        if passed:
            surviving_jobs.append(job)
            # Silencing the successes to keep terminal clean, just logging failures below
        else:
            print(f"  [X] Pruned [{job.external_id}]: {job.title} -> {reasons[0]}")

    print("\n" + "="*50)
    print(f"PIPELINE SUMMARY")
    print("="*50)
    print(f"Total Ingested:  {len(raw_jobs)}")
    print(f"Total Surviving: {len(surviving_jobs)}")
    print(f"Prune Rate:      {((len(raw_jobs) - len(surviving_jobs)) / len(raw_jobs)) * 100:.1f}%\n")
    
    if surviving_jobs:
        print("SURVIVOR SAMPLE (Ready for LLM):")
        sample = surviving_jobs[0]
        print(f" - {sample.title} ({sample.location}) | ID: {sample.external_id}")

if __name__ == "__main__":
    # Ensure you are running this from the project root directory using python -m
    asyncio.run(main())