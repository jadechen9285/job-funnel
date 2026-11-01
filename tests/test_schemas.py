import pytest
from conftest import make_job
from pydantic import ValidationError


def test_invalid_url_rejected():
    with pytest.raises(ValidationError):
        make_job(url="not-a-url")


def test_missing_url_rejected():
    with pytest.raises(ValidationError):
        make_job(url=None)


@pytest.mark.parametrize(
    "location, expected", [("Remote - US", True), ("REMOTE", True), ("New York, NY", False)]
)
def test_is_remote_inferred_from_location(location, expected):
    assert make_job(location=location).is_remote is expected


def test_explicit_remote_flag_overrides_location():
    assert make_job(location="Remote", remote=False).is_remote is False
    assert make_job(location="Anywhere", remote=True).is_remote is True


def test_discovered_at_is_timezone_aware():
    assert make_job().discovered_at.tzinfo is not None
