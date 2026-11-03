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


async def test_ingest_all_respects_concurrency_limit():
    import asyncio

    from job_funnel.base_ingestor import BaseIngestor

    state = {"running": 0, "peak": 0}

    class SlowIngestor(BaseIngestor):
        source = "slow"
        api_url = "unused"

        def _parse(self, payload):
            return []

        async def fetch_active_jobs(self):
            state["running"] += 1
            state["peak"] = max(state["peak"], state["running"])
            await asyncio.sleep(0.01)
            state["running"] -= 1
            return []

    await ingest_all([SlowIngestor(str(i)) for i in range(8)], max_concurrency=3)
    assert state["peak"] == 3


def test_build_ingestors_from_registry_and_fallback():
    from job_funnel.boards import Board, BoardRegistry
    from job_funnel.pipeline import build_ingestors

    registry = BoardRegistry([
        Board(ats="lever", token="a", status="active"),
        Board(ats="ashby", token="b", status="dead"),
    ])
    assert [(i.source, i.company_token) for i in build_ingestors(registry)] == [("lever_api", "a")]
    assert {i.company_token for i in build_ingestors(BoardRegistry())} == {"discord", "openai"}


def test_record_board_stats():
    from job_funnel.boards import Board, BoardRegistry
    from job_funnel.pipeline import record_board_stats

    registry = BoardRegistry([Board(ats="ashby", token="acme", status="active")])
    jobs = [make_job(source="ashby_api", board_token="acme", external_id=str(i)) for i in range(3)]
    record_board_stats(registry, jobs, jobs[:1])
    board = registry.boards[("ashby", "acme")]
    assert (board.n_jobs, board.n_gated_in) == (3, 1)
    assert board.last_checked is not None
