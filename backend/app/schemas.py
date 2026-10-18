"""Structured contracts passed between the coordinator and agents (not raw chat transcripts)."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

KINDS = ("research", "analysis", "implementation", "document", "synthesis")
Kind = Literal["research", "analysis", "implementation", "document", "synthesis"]


class PlanSubtask(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    kind: Kind
    objective: str = Field(min_length=1, max_length=2000)
    acceptance_criteria: list[str] = Field(default_factory=list, max_length=10)
    depends_on: list[int] = Field(default_factory=list)  # 0-based indexes of earlier subtasks
    time_sensitive: bool = False  # research only: needs fresh data


class Plan(BaseModel):
    summary: str = ""
    assumptions: list[str] = Field(default_factory=list)
    missing_information: list[str] = Field(default_factory=list)
    blocking_questions: list[str] = Field(default_factory=list)  # only if the task truly cannot proceed without an answer
    subtasks: list[PlanSubtask] = Field(min_length=1)

    @field_validator("subtasks")
    @classmethod
    def _deps_valid(cls, v: list[PlanSubtask]) -> list[PlanSubtask]:
        for i, st in enumerate(v):
            for d in st.depends_on:
                if d < 0 or d >= i:
                    raise ValueError(f"subtask {i} depends on {d}; dependencies must refer to earlier subtasks")
        return v


class Finding(BaseModel):
    claim: str
    support: Literal["source", "interpretation"] = "source"  # stated by a source vs. the agent's own reading
    source_urls: list[str] = Field(default_factory=list)
    confidence: Literal["high", "medium", "low"] = "medium"


class SourceRef(BaseModel):
    title: str = ""
    url: str
    publisher: str = ""
    published: str | None = None
    excerpt: str = ""


class ResearchReport(BaseModel):
    question: str = ""
    key_findings: list[Finding] = Field(default_factory=list)
    sources: list[SourceRef] = Field(default_factory=list)
    confidence_and_limitations: str = ""
    conflicting_evidence: list[str] = Field(default_factory=list)
    claims_requiring_verification: list[str] = Field(default_factory=list)
    unresolved_questions: list[str] = Field(default_factory=list)
    time_sensitive: bool = False


class AnalysisOutput(BaseModel):
    summary: str
    content: str  # markdown
    risks: list[str] = Field(default_factory=list)
    unsupported_assumptions: list[str] = Field(default_factory=list)
    recommendation: str = ""


class ProposedFile(BaseModel):
    path: str
    action: Literal["create", "modify"] = "create"
    content: str


class ImplementationOutput(BaseModel):
    summary: str
    files: list[ProposedFile] = Field(default_factory=list)
    notes: str = ""


class SynthesisOutput(BaseModel):
    title: str = ""
    content: str  # markdown deliverable
    open_issues: list[str] = Field(default_factory=list)


class ReviewIssue(BaseModel):
    severity: Literal["blocker", "major", "minor"] = "major"
    location: str = ""
    description: str
    evidence: str = ""


class ReviewOutput(BaseModel):
    verdict: Literal["pass", "fail"]
    summary: str = ""
    checks_performed: list[str] = Field(default_factory=list)
    issues: list[ReviewIssue] = Field(default_factory=list)

    @model_validator(mode="after")
    def _evidence_required(self) -> "ReviewOutput":
        # Reviewers must not simply agree or object: failures need evidence, passes need the checks that were run.
        if self.verdict == "fail" and not any(i.evidence.strip() for i in self.issues):
            raise ValueError("a failing review must list at least one concrete issue with evidence")
        if self.verdict == "pass" and not self.checks_performed:
            raise ValueError("a passing review must list the checks that were performed")
        return self


class ClaimVerdict(BaseModel):
    claim: str
    status: Literal["verified", "unverified", "conflicting", "outdated"]
    note: str = ""


class EvidenceEvaluation(BaseModel):
    """OpenAI's evaluation of research evidence (step 4 of the research workflow)."""
    summary: str
    content: str
    claim_verdicts: list[ClaimVerdict] = Field(default_factory=list)
    uncertainties: list[str] = Field(default_factory=list)
