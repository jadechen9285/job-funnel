import pytest
from conftest import load_fixture, routed_client

from job_funnel.boards import Board, BoardRegistry
from job_funnel.discovery import HNWhoIsHiringDiscoverer, run_discovery

pytestmark = pytest.mark.anyio


def hn_client(extra_routes=None):
    return routed_client({
        "search_by_date": (200, load_fixture("hn_search.json")),
        "items/300": (200, load_fixture("hn_item_300.json")),
        "items/200": (200, load_fixture("hn_item_200.json")),
        "items/100": (200, {"title": "old", "children": [{"text": "https://jobs.lever.co/too-old"}]}),
        **(extra_routes or {}),
    })


async def test_hn_discoverer_mines_top_level_posts_of_recent_threads():
    refs = await HNWhoIsHiringDiscoverer(months=2).discover(hn_client())
    assert refs == {
        ("ashby", "acme-ai"),  # appears in both threads, case-normalized
        ("greenhouse", "widgets"),
        ("lever", "gadgetco"),
    }
    # replies ("should-not-count"), "wants to be hired" threads, and the 3rd month are excluded


async def test_hn_discoverer_month_limit():
    refs = await HNWhoIsHiringDiscoverer(months=1).discover(hn_client())
    assert ("lever", "gadgetco") not in refs
    assert ("lever", "too-old") not in refs


async def test_run_discovery_merges_and_validates():
    registry = BoardRegistry([Board(ats="greenhouse", token="discord", discovered_via="manual")])
    client = hn_client({
        "posting-api/job-board/acme-ai": (200, {"jobs": [{}, {}]}),
        "v0/postings/gadgetco": (200, [{}]),
        "/v1/boards/discord/jobs": (200, {"jobs": []}),
        "/v1/boards/discord": (200, {"name": "Discord"}),
        # widgets -> 404 -> dead
    })
    summary = await run_discovery(registry, [HNWhoIsHiringDiscoverer(months=2)], client)

    assert summary["new"] == 3
    assert summary["validated"] == 4
    assert summary["active"] == 3 and summary["dead"] == 1
    assert registry.boards[("ashby", "acme-ai")].discovered_via == "hn_whoishiring"
    assert registry.boards[("ashby", "acme-ai")].n_jobs == 2
    assert registry.boards[("greenhouse", "discord")].company_name == "Discord"
    assert registry.boards[("greenhouse", "widgets")].status == "dead"


async def test_failing_discoverer_does_not_abort():
    registry = BoardRegistry()
    client = routed_client({"search_by_date": (500, {})})
    summary = await run_discovery(registry, [HNWhoIsHiringDiscoverer()], client)
    assert summary["new"] == 0 and summary["total"] == 0
