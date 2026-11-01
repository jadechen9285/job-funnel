import pytest
from conftest import make_job

from job_funnel.heuristic_gate import (
    AntiKeywordRule,
    FilterRule,
    HeuristicGate,
    LocationRule,
    TitleTaxonomyRule,
)


@pytest.mark.parametrize(
    "title",
    ["Senior Data Scientist", "ML Engineer", "Machine Learning Engineer", "AI Researcher",
     "LLM Infra Engineer", "Quant Researcher", "Head of Data Science"],
)
def test_title_taxonomy_accepts_target_titles(title):
    assert TitleTaxonomyRule().evaluate(make_job(title=title)) == (True, None)


@pytest.mark.parametrize("title", ["Software Engineer", "Email Marketing Lead", "HTML Developer"])
def test_title_taxonomy_rejects_and_respects_word_boundaries(title):
    passed, reason = TitleTaxonomyRule().evaluate(make_job(title=title))
    assert passed is False
    assert title in reason


@pytest.mark.parametrize("title", ["Frontend Engineer, AI", "AI Sales Lead", "Technical Recruiter", "HR Partner"])
def test_anti_keyword_rejects(title):
    passed, reason = AntiKeywordRule().evaluate(make_job(title=title))
    assert passed is False
    assert "excluded" in reason


def test_anti_keyword_word_boundary():
    # "hr" must not match inside "Three" / "Christopher"
    assert AntiKeywordRule().evaluate(make_job(title="Three-Tier ML Engineer"))[0] is True


def test_location_rule():
    assert LocationRule(require_remote=False).evaluate(make_job(location="NYC"))[0] is True
    assert LocationRule(require_remote=True).evaluate(make_job(location="NYC"))[0] is False
    assert LocationRule(require_remote=True).evaluate(make_job(location="Remote"))[0] is True


class _Recorder(FilterRule):
    def __init__(self, result):
        self.result, self.calls = result, 0

    def evaluate(self, job):
        self.calls += 1
        return self.result


def test_gate_short_circuits_on_first_failure():
    first, second = _Recorder((False, "nope")), _Recorder((True, None))
    passed, reasons = HeuristicGate([first, second]).process(make_job())
    assert (passed, reasons) == (False, ["nope"])
    assert second.calls == 0


def test_gate_passes_when_all_rules_pass():
    assert HeuristicGate([_Recorder((True, None))] * 2).process(make_job()) == (True, [])


def test_base_rule_is_abstract():
    with pytest.raises(NotImplementedError):
        FilterRule().evaluate(make_job())
