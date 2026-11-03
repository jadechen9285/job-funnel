from datetime import UTC, datetime, timedelta

import pytest
from conftest import load_fixture, routed_client

from job_funnel.ashby_ingestor import AshbyIngestor
from job_funnel.boards import (
    Board,
    BoardRegistry,
    extract_board_refs,
    ingestor_for,
    validate_board,
    validate_boards,
)
from job_funnel.greenhouse_ingestor import GreenhouseIngestor
from job_funnel.lever_ingestor import LeverIngestor


@pytest.mark.parametrize(
    "text, expected",
    [
        ("https://boards.greenhouse.io/discord/jobs/123", {("greenhouse", "discord")}),
        ("https://job-boards.greenhouse.io/Reddit", {("greenhouse", "reddit")}),
        ("https://job-boards.eu.greenhouse.io/eurocorp/jobs/1", {("greenhouse", "eurocorp")}),
        ("https://boards.greenhouse.io/embed/job_board?for=acme&b=x", {("greenhouse", "acme")}),
        ("https://boards-api.greenhouse.io/v1/boards/acme/jobs", {("greenhouse", "acme")}),
        ("https://jobs.ashbyhq.com/duck-duck-go/abc-123", {("ashby", "duck-duck-go")}),
        ("https://api.ashbyhq.com/posting-api/job-board/openai", {("ashby", "openai")}),
        ("https://jobs.lever.co/curai/uuid/apply", {("lever", "curai")}),
        ("https://jobs.eu.lever.co/pennylane", {("lever", "pennylane")}),
        ("https:&#x2F;&#x2F;jobs.ashbyhq.com&#x2F;reducto", {("ashby", "reducto")}),  # HN escaping
        ("apply at jobs.ashbyhq.com/acme.", {("ashby", "acme")}),  # trailing punctuation
        ("https://boards.greenhouse.io/embed/", set()),  # not a board
        ("email jobs@acme.com or see acme.com/careers", set()),
        ("", set()),
    ],
)
def test_extract_board_refs(text, expected):
    assert extract_board_refs(text) == expected


def test_extract_multiple_boards_from_one_post():
    text = "see https://jobs.lever.co/a and https://jobs.ashbyhq.com/b and jobs.lever.co/a again"
    assert extract_board_refs(text) == {("lever", "a"), ("ashby", "b")}


def test_registry_merge_dedups_and_keeps_existing():
    existing = Board(ats="ashby", token="acme", status="active", n_jobs=5, discovered_via="manual")
    registry = BoardRegistry([existing])
    added = registry.merge({("ashby", "acme"), ("lever", "new")}, via="hn")

    assert [b.key for b in added] == [("lever", "new")]
    assert registry.boards[("ashby", "acme")] == existing  # untouched
    assert registry.boards[("lever", "new")].status == "unverified"
    assert registry.boards[("lever", "new")].discovered_via == "hn"


def test_registry_csv_round_trip(tmp_path):
    path = tmp_path / "boards.csv"
    boards = [
        Board(ats="greenhouse", token="discord", company_name="Discord", status="active",
              last_checked=datetime(2026, 11, 1, tzinfo=UTC), n_jobs=41, n_gated_in=7),
        Board(ats="lever", token="x"),  # optional fields empty
    ]
    BoardRegistry(boards).save(path)
    loaded = BoardRegistry.load(path)
    assert loaded.boards == {b.key: b for b in boards}


def test_registry_load_missing_file_is_empty(tmp_path):
    assert len(BoardRegistry.load(tmp_path / "nope.csv")) == 0


def test_active_and_staleness():
    fresh = Board(ats="ashby", token="a", status="active", last_checked=datetime.now(UTC))
    old = Board(ats="ashby", token="b", status="active",
                last_checked=datetime.now(UTC) - timedelta(days=30))
    dead = Board(ats="ashby", token="c", status="dead")
    registry = BoardRegistry([fresh, old, dead])
    assert {b.token for b in registry.active()} == {"a", "b"}
    assert not fresh.is_stale(timedelta(days=7))
    assert old.is_stale(timedelta(days=7))
    assert dead.is_stale(timedelta(days=7))  # never checked


@pytest.mark.parametrize(
    "ats, cls", [("greenhouse", GreenhouseIngestor), ("ashby", AshbyIngestor), ("lever", LeverIngestor)]
)
def test_ingestor_for(ats, cls):
    ingestor = ingestor_for(Board(ats=ats, token="acme", company_name="Acme Inc"))
    assert isinstance(ingestor, cls)
    assert ingestor.company_token == "acme"
    assert ingestor.company_name == "Acme Inc"
    assert ingestor.verbose is False


@pytest.mark.anyio
async def test_validate_greenhouse_active_uses_board_name():
    client = routed_client({
        "/v1/boards/acme/jobs": (200, {"jobs": [{"id": 1}, {"id": 2}]}),
        "/v1/boards/acme": (200, {"name": "Acme Inc"}),
    })
    board = await validate_board(Board(ats="greenhouse", token="acme"), client)
    assert (board.status, board.company_name, board.n_jobs) == ("active", "Acme Inc", 2)
    assert board.last_checked is not None


@pytest.mark.anyio
async def test_validate_ashby_and_lever_counts():
    client = routed_client({
        "ashbyhq.com": (200, load_fixture("ashby_jobs.json")),
        "lever.co": (200, load_fixture("lever_jobs.json")),
    })
    ashby = await validate_board(Board(ats="ashby", token="acme"), client)
    lever = await validate_board(Board(ats="lever", token="acme"), client)
    assert (ashby.status, ashby.n_jobs, ashby.company_name) == ("active", 4, "ACME")
    assert (lever.status, lever.n_jobs) == ("active", 3)


@pytest.mark.anyio
@pytest.mark.parametrize("ats", ["greenhouse", "ashby", "lever"])
async def test_validate_404_marks_dead(ats):
    board = await validate_board(Board(ats=ats, token="ghost"), routed_client({}))
    assert (board.status, board.n_jobs) == ("dead", 0)


@pytest.mark.anyio
async def test_validate_transient_error_keeps_previous_state():
    original = Board(ats="ashby", token="flaky", status="active", n_jobs=3)
    board = await validate_board(original, routed_client({"ashbyhq.com": (503, {})}))
    assert board == original


@pytest.mark.anyio
async def test_validate_boards_batch():
    client = routed_client({"lever.co/v0/postings/live": (200, [])})
    results = await validate_boards(
        [Board(ats="lever", token="live"), Board(ats="lever", token="gone")], client, max_concurrency=1
    )
    assert [(b.token, b.status) for b in results] == [("live", "active"), ("gone", "dead")]
