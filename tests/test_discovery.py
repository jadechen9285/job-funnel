import gzip
import json

import httpx
import pytest
from conftest import RecordingTransport, load_fixture, routed_client

from job_funnel.boards import Board, BoardRegistry
from job_funnel.discovery import (
    CommonCrawlDiscoverer,
    HNWhoIsHiringDiscoverer,
    run_discovery,
)

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
        "/v1/boards/discord/jobs": (200, {"jobs": [{"id": 1, "company_name": "Discord"}]}),
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


# ---------------------------------------------------------------- Common Crawl
def cdx_line(surt: str, url: str) -> str:
    return f'{surt} 20261017000000 {json.dumps({"url": url, "status": "200"})}'


# Three index blocks in one shard; the ATS keys straddle block boundaries on purpose.
BLOCKS = [
    [cdx_line("co,example)/", "https://example.co/"),
     cdx_line("co,lever,jobs)/gadgetco", "https://jobs.lever.co/gadgetco")],
    [cdx_line("com,ashbyhq,jobs)/acme-ai/123", "https://jobs.ashbyhq.com/acme-ai/123"),
     cdx_line("com,ashbyhq,jobs)/beta/9", "https://jobs.ashbyhq.com/beta/9"),
     cdx_line("com,blah)/", "https://blah.com/")],
    [cdx_line("com,myworkdayjobs,nvidia,wd5)/en-us/ext/job/1",
              "https://nvidia.wd5.myworkdayjobs.com/en-US/Ext/job/1"),
     cdx_line("io,greenhouse,job-boards)/widgets/jobs/4", "https://job-boards.greenhouse.io/widgets/jobs/4"),
     cdx_line("org,zzz)/", "https://zzz.org/")],
]


def build_index():
    shard, cluster, offset = b"", [], 0
    for i, lines in enumerate(BLOCKS):
        member = gzip.compress(("\n".join(lines) + "\n").encode())
        first_key = lines[0].split(" ")[0]
        cluster.append(f"{first_key} 20261017000000\tcdx-00001.gz\t{offset}\t{len(member)}\t{i + 1}")
        shard += member
        offset += len(member)
    return shard, "\n".join(cluster) + "\n"


SHARD, CLUSTER = build_index()
EXPECTED = {("lever", "gadgetco"), ("ashby", "acme-ai"), ("ashby", "beta"),
            ("workday", "nvidia|wd5|ext"), ("greenhouse", "widgets")}


def cc_transport(fail_first: int = 0):
    state = {"failures": fail_first}

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.endswith("collinfo.json"):
            return httpx.Response(200, json=[{"id": "CC-MAIN-TEST"}, {"id": "CC-MAIN-OLD"}])
        if url.endswith("cluster.idx"):
            return httpx.Response(200, text=CLUSTER)
        if url.endswith("cdx-00001.gz"):
            if state["failures"]:
                state["failures"] -= 1
                return httpx.Response(504)
            start, end = map(int, request.headers["Range"].removeprefix("bytes=").split("-"))
            return httpx.Response(206, content=SHARD[start: end + 1])
        return httpx.Response(404)

    return RecordingTransport(handler)


async def test_common_crawl_discovers_all_ats_hosts(tmp_path):
    transport = cc_transport()
    discoverer = CommonCrawlDiscoverer(crawls=1, cache_dir=tmp_path)
    refs = await discoverer.discover(httpx.AsyncClient(transport=transport))
    assert refs == EXPECTED
    assert (tmp_path / "CC-MAIN-TEST" / "cluster.idx").exists()


async def test_common_crawl_cache_avoids_refetch(tmp_path):
    await CommonCrawlDiscoverer(crawls=["CC-MAIN-TEST"], cache_dir=tmp_path).discover(
        httpx.AsyncClient(transport=cc_transport())
    )
    offline = RecordingTransport(lambda r: httpx.Response(500))
    refs = await CommonCrawlDiscoverer(crawls=["CC-MAIN-TEST"], cache_dir=tmp_path).discover(
        httpx.AsyncClient(transport=offline)
    )
    assert refs == EXPECTED
    assert offline.requests == []


async def test_common_crawl_retries_504(tmp_path):
    transport = cc_transport(fail_first=2)
    refs = await CommonCrawlDiscoverer(crawls=["CC-MAIN-TEST"], cache_dir=tmp_path, concurrency=1).discover(
        httpx.AsyncClient(transport=transport)
    )
    assert refs == EXPECTED


async def test_common_crawl_failed_block_does_not_abort(tmp_path):
    transport = cc_transport(fail_first=100)
    refs = await CommonCrawlDiscoverer(crawls=["CC-MAIN-TEST"], cache_dir=tmp_path, retries=1).discover(
        httpx.AsyncClient(transport=transport)
    )
    assert refs == set()


def test_blocks_for_prefixes_includes_preceding_block():
    blocks = [(line.split("\t")[0].split(" ")[0], "s", i, 1) for i, line in enumerate(CLUSTER.splitlines())]
    selected = {offset for _, offset, _ in CommonCrawlDiscoverer._blocks_for_prefixes(blocks)}
    # lever keys live inside block 0 (which starts at "co,example"), so it must be included
    assert selected == {0, 1, 2}


async def test_run_discovery_checkpoints_each_chunk():
    registry = BoardRegistry([Board(ats="lever", token=f"b{i}") for i in range(5)])
    saves = []
    await run_discovery(registry, [], routed_client({"lever.co": (200, [])}),
                        checkpoint=lambda: saves.append(1), chunk_size=2)
    assert len(saves) == 3  # 5 boards in chunks of 2
    assert all(b.status == "active" for b in registry.boards.values())
