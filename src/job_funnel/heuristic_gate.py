import re
from typing import List, Tuple, Optional
from job_funnel.schemas import JobPosting

class FilterRule:
    """Base interface for all heuristic rules."""
    def evaluate(self, job: JobPosting) -> Tuple[bool, Optional[str]]:
        raise NotImplementedError

class TitleTaxonomyRule(FilterRule):
    def __init__(self):
        # Broad inclusion taxonomy
        self.pattern = re.compile(
            r'\b(data scientist|data science|machine learning|ml|ai|artificial intelligence|deep learning|llm|quant)\b', 
            re.IGNORECASE
        )
        
    def evaluate(self, job: JobPosting) -> Tuple[bool, Optional[str]]:
        if self.pattern.search(job.title):
            return True, None
        return False, f"Title '{job.title}' misses AI/Data taxonomy."

class LocationRule(FilterRule):
    def __init__(self, require_remote: bool):
        self.require_remote = require_remote

    def evaluate(self, job: JobPosting) -> Tuple[bool, Optional[str]]:
        if self.require_remote and not job.is_remote:
            return False, f"Location '{job.location}' does not indicate remote work."
        return True, None

class AntiKeywordRule(FilterRule):
    def __init__(self):
        # Exclude common false positives (e.g., Frontend dev on an "AI Team")
        self.anti_pattern = re.compile(
            r'\b(frontend|sales|marketing|account executive|recruiter|hr)\b', 
            re.IGNORECASE
        )

    def evaluate(self, job: JobPosting) -> Tuple[bool, Optional[str]]:
        if self.anti_pattern.search(job.title):
            return False, f"Title '{job.title}' contains excluded role keywords."
        return True, None

# ============================================================================
# THE HEURISTIC GATE (ORCHESTRATOR)
# ============================================================================
class HeuristicGate:
    def __init__(self, rules: List[FilterRule]):
        self.rules = rules

    def process(self, job: JobPosting) -> Tuple[bool, List[str]]:
        for rule in self.rules:
            passed, reason = rule.evaluate(job)
            if not passed:
                return False, [reason] # Short-circuit evaluation
        return True, []