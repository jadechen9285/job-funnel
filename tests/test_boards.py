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
from job_funnel.workday_ingestor import WorkdayIngestor


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
        ("https://nvidia.wd5.myworkdayjobs.com/en-US/NVIDIAExternalCareerSite/job/US-CA/X_JR1",
         {("workday", "nvidia|wd5|nvidiaexternalcareersite")}),
        ("https://capitalone.wd12.myworkdayjobs.com/Capital_One", {("workday", "capitalone|wd12|capital_one")}),
        ("https://3m.wd1.myworkdayjobs.com/es/3M_Careers", {("workday", "3m|wd1|3m_careers")}),
        ("https://acme.wd1.myworkdayjobs.com/wday/cxs/acme/site/jobs", set()),  # API path
        ("https://acme.wd1.myworkdayjobs.com/search", set()),
        ("https://jobs.ashbyhq.com/51f67855-3ba7-445a-99bb-97e9f5093e4b", set()),  # job UUID, not a board
        ("https://jobs.ashbyhq.com/a1%20garage%20door", set()),  # encoded spaces
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


def test_default_staleness_depends_on_status():
    ten_days_ago = datetime.now(UTC) - timedelta(days=10)
    assert Board(ats="ashby", token="a").is_stale()  # unverified: always
    assert Board(ats="ashby", token="a", status="active", last_checked=ten_days_ago).is_stale()
    assert not Board(ats="ashby", token="a", status="dormant", last_checked=ten_days_ago).is_stale()
    assert not Board(ats="ashby", token="a", status="dead", last_checked=ten_days_ago).is_stale()


@pytest.mark.parametrize(
    "ats, token, cls",
    [("greenhouse", "acme", GreenhouseIngestor), ("ashby", "acme", AshbyIngestor),
     ("lever", "acme", LeverIngestor), ("workday", "acme|wd1|site", WorkdayIngestor)],
)
def test_ingestor_for(ats, token, cls):
    ingestor = ingestor_for(Board(ats=ats, token=token, company_name="Acme Inc"), title_filter=str.isupper)
    assert isinstance(ingestor, cls)
    assert ingestor.company_token == token
    assert ingestor.company_name == "Acme Inc"
    assert ingestor.verbose is False
    assert ingestor.title_filter is str.isupper


@pytest.mark.anyio
async def test_validate_greenhouse_active_uses_listed_company_name():
    client = routed_client({
        "/v1/boards/acme/jobs": (200, {"jobs": [{"id": 1, "company_name": "Acme Inc"}, {"id": 2}]}),
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


@pytest.mark.anyio
async def test_validate_workday_uses_search_total():
    client = routed_client({"acme.wd5.myworkdayjobs.com/wday/cxs/acme/careers/jobs": (200, {"total": 57})})
    board = await validate_board(Board(ats="workday", token="acme|wd5|careers"), client)
    assert (board.status, board.n_jobs, board.company_name) == ("active", 57, "ACME")


@pytest.mark.anyio
@pytest.mark.parametrize("status_code", [404, 410, 422])
async def test_validate_dead_statuses(status_code):
    client = routed_client({"myworkdayjobs.com": (status_code, {})})
    board = await validate_board(Board(ats="workday", token="acme|wd1|nope"), client)
    assert board.status == "dead"


@pytest.mark.anyio
async def test_empty_board_goes_dormant_after_repeated_checks_and_recovers():
    empty = routed_client({"lever.co": (200, [])})
    board = Board(ats="lever", token="quiet")
    for expected in ["active", "active", "dormant"]:
        board = await validate_board(board, empty)
        assert board.status == expected
    assert board.zero_streak == 3

    hiring = routed_client({"lever.co": (200, [{"id": "x"}])})
    board = await validate_board(board, hiring)
    assert (board.status, board.zero_streak, board.n_jobs) == ("active", 0, 1)
