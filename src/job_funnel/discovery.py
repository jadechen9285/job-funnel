"""Stage -1: discover ATS boards of companies we don't know yet.

Usage:
    uv run python -m job_funnel.discovery --hn-months 12
"""

import argparse
import asyncio
from abc import ABC, abstractmethod
from collections import Counter
from datetime import timedelta
from pathlib import Path

import httpx

from job_funnel.boards import (
    DEFAULT_REGISTRY_PATH,
    BoardRef,
    BoardRegistry,
    extract_board_refs,
    validate_boards,
)


class Discoverer(ABC):
    """Produces (ats, token) refs from some public source. Add careers-page / Common Crawl / search-API ones here."""

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


async def run_discovery(
    registry: BoardRegistry,
    discoverers: list[Discoverer],
    client: httpx.AsyncClient,
    revalidate_after: timedelta = timedelta(days=7),
) -> dict[str, int]:
    """Discover -> merge into registry -> validate new and stale boards. Returns summary counts."""
    n_new = 0
    for discoverer in discoverers:
        try:
            refs = await discoverer.discover(client)
        except httpx.HTTPError as e:
            print(f"[-] Discoverer {discoverer.name} failed: {e!r}")
            continue
        n_new += len(registry.merge(refs, via=discoverer.name))

    to_check = [b for b in registry.boards.values() if b.is_stale(revalidate_after)]
    print(f"[*] Validating {len(to_check)} new/stale boards...")
    for board in await validate_boards(to_check, client):
        registry.boards[board.key] = board

    status = Counter(b.status for b in registry.boards.values())
    return {"new": n_new, "validated": len(to_check), "total": len(registry), **status}


async def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Discover ATS job boards and update the registry.")
    parser.add_argument("--hn-months", type=int, default=12, help="how many monthly HN threads to mine")
    parser.add_argument("--registry", type=Path, default=DEFAULT_REGISTRY_PATH)
    args = parser.parse_args(argv)

    registry = BoardRegistry.load(args.registry)
    async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
        summary = await run_discovery(registry, [HNWhoIsHiringDiscoverer(args.hn_months)], client)
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
