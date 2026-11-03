"""Stage -1: discover ATS boards of companies we don't know yet.

Usage:
    uv run python -m job_funnel.discovery --hn-months 12
    uv run python -m job_funnel.discovery --common-crawl --cc-crawls 3
"""

import argparse
import asyncio
import bisect
import gzip
import json
from abc import ABC, abstractmethod
from collections import Counter
from collections.abc import Callable
from datetime import timedelta
from pathlib import Path

import httpx

from job_funnel.base_ingestor import request_with_retry
from job_funnel.boards import (
    DEFAULT_REGISTRY_PATH,
    BoardRef,
    BoardRegistry,
    extract_board_refs,
    validate_boards,
)


class Discoverer(ABC):
    """Produces (ats, token) refs from some public source. Add careers-page / search-API ones here."""

    name: str

    @abstractmethod
    async def discover(self, client: httpx.AsyncClient) -> set[BoardRef]: ...


class HNWhoIsHiringDiscoverer(Discoverer):
    """Mines the monthly Hacker News 'Ask HN: Who is hiring?' threads via the Algolia API."""

    name = "hn_whoishiring"
    SEARCH_URL = "https://hn.algolia.com/api/v1/search_by_date"
    ITEM_URL = "https://hn.algolia.com/api/v1/items/{id}"

    def __init__(self, months: int = 12):
        self.months = months

    async def _thread_ids(self, client: httpx.AsyncClient) -> list[str]:
        # whoishiring posts 3 stories a month (hiring / wants to be hired / freelancer)
        response = await client.get(
            self.SEARCH_URL,
            params={"tags": "story,author_whoishiring", "hitsPerPage": self.months * 3 + 3},
        )
        response.raise_for_status()
        hits = [h for h in response.json().get("hits", []) if "who is hiring" in h.get("title", "").lower()]
        hits.sort(key=lambda h: int(h.get("created_at_i", 0)), reverse=True)
        return [h["objectID"] for h in hits[: self.months]]

    async def discover(self, client: httpx.AsyncClient) -> set[BoardRef]:
        refs: set[BoardRef] = set()
        for thread_id in await self._thread_ids(client):
            response = await client.get(self.ITEM_URL.format(id=thread_id))
            response.raise_for_status()
            thread = response.json()
            # Top-level comments are the job posts; replies are discussion.
            posts = [c.get("text") or "" for c in thread.get("children", [])]
            found = set().union(*(extract_board_refs(t) for t in posts)) if posts else set()
            print(f"[+] {thread.get('title')}: {len(posts)} posts -> {len(found)} boards")
            refs |= found
        return refs


class CommonCrawlDiscoverer(Discoverer):
    """Enumerates every ATS board URL that Common Crawl has captured.

    The public CDX *server* (index.commoncrawl.org) is overloaded and often 504s, so we
    read the same index straight from data.commoncrawl.org (S3-backed, supports HTTP
    Range): `cluster.idx` maps sorted SURT keys to ~3,000-line gzip blocks inside the
    `cdx-*.gz` shards, so we binary-search the ATS host prefixes and range-fetch only
    those blocks (a few dozen per crawl). Everything is cached under `cache_dir`, so
    reruns are instant and an interrupted run resumes.
    """

    name = "commoncrawl"
    COLLINFO_URL = "https://index.commoncrawl.org/collinfo.json"
    INDEX_URL = "https://data.commoncrawl.org/cc-index/collections/{crawl}/indexes/{file}"
    # SURT (reversed-host) prefixes of the ATS hosts we can ingest.
    SURT_PREFIXES = (
        "co,lever,jobs)/",
        "com,ashbyhq,jobs)/",
        "com,myworkdayjobs,",
        "io,greenhouse,boards)/",
        "io,greenhouse,job-boards)/",
    )

    def __init__(
        self,
        crawls: int | list[str] = 2,
        cache_dir: Path = Path("data/cache/commoncrawl"),
        concurrency: int = 4,
        retries: int = 5,
    ):
        self.crawls = crawls
        self.cache_dir = cache_dir
        self.concurrency = concurrency
        self.retries = retries

    async def _get(self, client: httpx.AsyncClient, url: str, **kwargs) -> httpx.Response:
        response = await request_with_retry(client, "GET", url, retries=self.retries, timeout=300.0, **kwargs)
        response.raise_for_status()
        return response

    async def _crawl_ids(self, client: httpx.AsyncClient) -> list[str]:
        if isinstance(self.crawls, list):
            return self.crawls
        collections = (await self._get(client, self.COLLINFO_URL)).json()
        return [c["id"] for c in collections[: self.crawls]]  # newest first

    async def _cluster_index(self, client: httpx.AsyncClient, crawl: str) -> list[tuple[str, str, int, int]]:
        """[(first SURT key, shard file, offset, length)] for every block of the crawl's index."""
        path = self.cache_dir / crawl / "cluster.idx"
        if not path.exists():
            print(f"[*] Downloading {crawl} cluster.idx (~100MB)...")
            path.parent.mkdir(parents=True, exist_ok=True)
            response = await self._get(client, self.INDEX_URL.format(crawl=crawl, file="cluster.idx"))
            tmp = path.with_suffix(".part")
            tmp.write_bytes(response.content)
            tmp.rename(path)
        blocks = []
        with path.open() as f:
            for line in f:
                key_ts, shard, offset, length = line.split("\t")[:4]
                blocks.append((key_ts.split(" ", 1)[0], shard, int(offset), int(length)))
        return blocks

    @classmethod
    def _blocks_for_prefixes(cls, blocks: list[tuple[str, str, int, int]]) -> list[tuple[str, int, int]]:
        """Blocks that may contain keys starting with any SURT prefix (incl. the block just before)."""
        keys = [b[0] for b in blocks]
        selected: dict[tuple[str, int], int] = {}
        for prefix in cls.SURT_PREFIXES:
            upper = prefix[:-1] + chr(ord(prefix[-1]) + 1)
            lo = max(bisect.bisect_left(keys, prefix) - 1, 0)
            hi = bisect.bisect_left(keys, upper)
            for _, shard, offset, length in blocks[lo:hi]:
                selected[(shard, offset)] = length
        return [(shard, offset, length) for (shard, offset), length in selected.items()]

    async def _block_urls(self, client: httpx.AsyncClient, crawl: str, shard: str, offset: int, length: int) -> list[str]:
        """ATS URLs inside one index block (cached as plain text, one URL per line)."""
        path = self.cache_dir / crawl / "blocks" / f"{shard}-{offset}.txt"
        if path.exists():
            return path.read_text().splitlines()
        response = await self._get(
            client,
            self.INDEX_URL.format(crawl=crawl, file=shard),
            headers={"Range": f"bytes={offset}-{offset + length - 1}"},
        )
        urls = []
        for line in gzip.decompress(response.content).decode("utf-8", "replace").splitlines():
            key, _, rest = line.partition(" ")
            if key.startswith(self.SURT_PREFIXES):
                try:
                    urls.append(json.loads(rest.split(" ", 1)[1])["url"])
                except (IndexError, ValueError, KeyError):
                    continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(urls))
        return urls

    async def discover(self, client: httpx.AsyncClient) -> set[BoardRef]:
        refs: set[BoardRef] = set()
        semaphore = asyncio.Semaphore(self.concurrency)
        for crawl in await self._crawl_ids(client):
            blocks = self._blocks_for_prefixes(await self._cluster_index(client, crawl))

            async def _one(block: tuple[str, int, int], crawl: str = crawl) -> list[str]:
                async with semaphore:
                    try:
                        return await self._block_urls(client, crawl, *block)
                    except httpx.HTTPError as e:
                        print(f"[-] {crawl} block {block[0]}@{block[1]} failed: {e!r}")
                        return []

            urls = [u for batch in await asyncio.gather(*(_one(b) for b in blocks)) for u in batch]
            found = set().union(*(extract_board_refs(u) for u in urls)) if urls else set()
            by_ats = Counter(ats for ats, _ in found)
            print(f"[+] {crawl}: {len(blocks)} index blocks, {len(urls)} ATS URLs -> {len(found)} boards {dict(by_ats)}")
            refs |= found
        return refs


async def run_discovery(
    registry: BoardRegistry,
    discoverers: list[Discoverer],
    client: httpx.AsyncClient,
    revalidate_after: timedelta | None = None,
    checkpoint: Callable[[], None] | None = None,
    chunk_size: int = 500,
) -> dict[str, int]:
    """Discover -> merge into registry -> validate new and stale boards. Returns summary counts.

    Validation runs in chunks; `checkpoint` (e.g. registry.save) is called after every chunk so
    a long run (10k+ boards) that gets interrupted keeps its progress.
    """
    n_new = 0
    for discoverer in discoverers:
        try:
            refs = await discoverer.discover(client)
        except httpx.HTTPError as e:
            print(f"[-] Discoverer {discoverer.name} failed: {e!r}")
            continue
        n_new += len(registry.merge(refs, via=discoverer.name))

    # None -> per-status schedule (new: now, active: 7d, dormant: 30d, dead: 90d)
    to_check = [b for b in registry.boards.values() if b.is_stale(revalidate_after)]
    print(f"[*] Validating {len(to_check)} new/stale boards...")
    for start in range(0, len(to_check), chunk_size):
        for board in await validate_boards(to_check[start : start + chunk_size], client):
            registry.boards[board.key] = board
        if checkpoint is not None:
            checkpoint()
        done = min(start + chunk_size, len(to_check))
        status = Counter(registry.boards[b.key].status for b in to_check[:done])
        print(f"[*] validated {done}/{len(to_check)} {dict(status)}", flush=True)

    status = Counter(b.status for b in registry.boards.values())
    return {"new": n_new, "validated": len(to_check), "total": len(registry), **status}


async def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Discover ATS job boards and update the registry.")
    parser.add_argument("--hn-months", type=int, default=12, help="monthly HN threads to mine (0 = skip)")
    parser.add_argument("--common-crawl", action="store_true", help="enumerate ATS boards from Common Crawl")
    parser.add_argument("--cc-crawls", type=int, default=2, help="how many recent crawls to read")
    parser.add_argument("--registry", type=Path, default=DEFAULT_REGISTRY_PATH)
    args = parser.parse_args(argv)

    discoverers: list[Discoverer] = []
    if args.hn_months:
        discoverers.append(HNWhoIsHiringDiscoverer(args.hn_months))
    if args.common_crawl:
        discoverers.append(CommonCrawlDiscoverer(crawls=args.cc_crawls))

    registry = BoardRegistry.load(args.registry)
    # Thousands of distinct hosts (one per Workday tenant): big pool, few idle keep-alives,
    # and a generous pool wait since concurrency is already bounded by validate_boards.
    limits = httpx.Limits(max_connections=100, max_keepalive_connections=10)
    timeout = httpx.Timeout(30.0, pool=120.0)
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True, limits=limits) as client:
        summary = await run_discovery(
            registry, discoverers, client, checkpoint=lambda: registry.save(args.registry)
        )
    registry.save(args.registry)

    print("\n" + "=" * 50)
    print("DISCOVERY SUMMARY")
    print("=" * 50)
    for key, value in summary.items():
        print(f"  {key:<10} {value}")
    by_ats = Counter(b.ats for b in registry.active())
    print(f"  active by ATS: {dict(by_ats)}")
    print(f"  registry saved to {args.registry}")


if __name__ == "__main__":
    asyncio.run(main())
