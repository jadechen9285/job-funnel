import pytest
from conftest import load_fixture, make_job, mock_client

from job_funnel.ashby_ingestor import AshbyIngestor
from job_funnel.greenhouse_ingestor import GreenhouseIngestor
from job_funnel.pipeline import apply_gate, default_gate, ingest_all

pytestmark = pytest.mark.anyio


async def test_ingest_all_combines_sources():
    jobs = await ingest_all([
        GreenhouseIngestor("acme", client=mock_client(load_fixture("greenhouse_jobs.json"))),
        AshbyIngestor("acme", client=mock_client(load_fixture("ashby_jobs.json"))),
    ])
    assert {j.source for j in jobs} == {"greenhouse_api", "ashby_api"}
    assert len(jobs) == 5


async def test_failing_source_does_not_abort_run():
    jobs = await ingest_all([
        GreenhouseIngestor("down", client=mock_client(status_code=503)),
        AshbyIngestor("acme", client=mock_client(load_fixture("ashby_jobs.json"))),
    ])
    assert len(jobs) == 3
    assert all(j.source == "ashby_api" for j in jobs)


def test_apply_gate_splits_and_records_reasons():
    keep = make_job(external_id="1", title="ML Engineer")
    sales = make_job(external_id="2", title="AI Sales Lead")
    swe = make_job(external_id="3", title="Backend Engineer")

    surviving, pruned = apply_gate(default_gate(), [keep, sales, swe])

    assert surviving == [keep]
    assert [(j.external_id, "excluded" in r or "taxonomy" in r) for j, r in pruned] == [
        ("2", True), ("3", True)
    ]
