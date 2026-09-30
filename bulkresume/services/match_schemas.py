"""
SCHEMA FOR THE GEMINI JD-MATCH ROUTE
--------------------------------------
Independent of schemas.py (which the bulk upload and single-parse endpoints
share), so changing what the bulk parser extracts -- e.g. dropping the job
`description` -- can't affect matching, and vice versa.

Matching needs the WHY behind a candidate's skills, so unlike the bulk schema
this one keeps the job/internship description and the project details. It
leaves out fields that never matter for a match (parents' names, marital
status, hobbies, PIN code...), which also keeps the prompt and output short.

Field names of the fields it shares with ResumeExtraction are identical, so
parse_score.compute_parse_score() and grounding.ground_extraction() work on
it unchanged.
"""
from typing import List, Optional

from pydantic import BaseModel, ConfigDict, Field


class MatchEducation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    degree: str = ""
    institution: str = ""
    year: str = ""
    percentage: Optional[str] = None   # exactly as written; None if not stated


class MatchJob(BaseModel):
    """One employment or internship entry."""
    model_config = ConfigDict(extra="forbid")

    title: str = ""
    company: str = ""
    duration: str = ""
    description: str = ""


class MatchProject(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = ""
    company: str = ""
    start_date: str = ""
    end_date: str = ""
    description: str = ""
    methodologies: List[str] = Field(default_factory=list)


class MatchTraining(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = ""
    provider: str = ""
    year: str = ""


class MatchResumeExtraction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = ""
    email: str = ""
    phone: str = ""
    linkedin_url: str = ""
    other_urls: List[str] = Field(default_factory=list)
    candidate_address: str = ""
    known_languages: List[str] = Field(default_factory=list)
    total_experience: str = ""
    education: List[MatchEducation] = Field(default_factory=list)
    experience: List[MatchJob] = Field(default_factory=list)
    internships: List[MatchJob] = Field(default_factory=list)
    projects: List[MatchProject] = Field(default_factory=list)
    skills: List[str] = Field(default_factory=list)
    certifications: List[str] = Field(default_factory=list)
    training: List[MatchTraining] = Field(default_factory=list)
    profile_summary: str = ""
