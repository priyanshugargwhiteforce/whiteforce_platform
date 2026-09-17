from typing import List

from pydantic import BaseModel, ConfigDict, Field


class Education(BaseModel):
    model_config = ConfigDict(extra="forbid")

    degree: str = ""
    institution: str = ""
    year: str = ""


class Experience(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = ""
    company: str = ""
    duration: str = ""
    description: str = ""


class Training(BaseModel):
    """
    Was previously List[str] on ResumeExtraction below, but the prompt
    (EXTRACTION_PROMPT in llm_extractor.py) has always asked the model for
    "name, provider/institution, and year if available" -- a structured
    shape, same as Education/Experience. The schema just never matched
    that instruction, so any resume where the model correctly followed the
    prompt and returned training as objects (not bare strings) failed
    Pydantic validation entirely, discarding the WHOLE extraction (name,
    skills, education, everything) and falling back to bare regex --
    correct fallback behavior, but an unnecessary quality loss triggered
    by one unrelated field.
    """
    model_config = ConfigDict(extra="forbid")

    name: str = ""
    provider: str = ""
    year: str = ""


class ResumeExtraction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = ""
    email: str = ""
    phone: str = ""
    gender: str = ""
    date_of_birth: str = ""
    marital_status: str = ""
    father_name: str = ""
    mother_name: str = ""
    known_languages: List[str] = Field(default_factory=list)
    candidate_address: str = ""
    pincode_postal_code: str = ""
    hobbies: List[str] = Field(default_factory=list)
    training: List[Training] = Field(default_factory=list)
    linkedin_url: str = ""
    other_urls: List[str] = Field(default_factory=list)
    education: List[Education] = Field(default_factory=list)
    experience: List[Experience] = Field(default_factory=list)
    skills: List[str] = Field(default_factory=list)
    certifications: List[str] = Field(default_factory=list)
    internships: List[Experience] = Field(default_factory=list)
    profile_summary: str = ""


# ── JD <-> Resume matching ───────────────────────────────────────────────────

class JobDescriptionExtraction(BaseModel):
    """Structured shape a JD (file / pasted text / loosely-shaped JSON) gets
    normalized into before it's ever compared against a candidate."""
    model_config = ConfigDict(extra="forbid")

    job_title: str = ""
    must_have_skills: List[str] = Field(default_factory=list)
    good_to_have_skills: List[str] = Field(default_factory=list)
    min_experience_years: str = ""
    max_experience_years: str = ""
    qualifications: List[str] = Field(default_factory=list)
    responsibilities: List[str] = Field(default_factory=list)
    location: str = ""
    employment_type: str = ""
    other_requirements: List[str] = Field(default_factory=list)


class FieldMatch(BaseModel):
    """One row of the field-by-field JD vs candidate comparison, e.g.
    field='experience', jd_requirement='3+ years backend', candidate_value='4 years backend (Acme Corp)'."""
    model_config = ConfigDict(extra="forbid")

    field: str = ""
    jd_requirement: str = ""
    candidate_value: str = ""
    matched: bool = False
    match_percent: float = 0.0
    note: str = ""


class ResumeJDMatchResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    overall_match_percent: float = 0.0
    matched_skills: List[str] = Field(default_factory=list)
    missing_skills: List[str] = Field(default_factory=list)
    matched_qualifications: List[str] = Field(default_factory=list)
    missing_qualifications: List[str] = Field(default_factory=list)
    experience_match: str = ""
    field_breakdown: List[FieldMatch] = Field(default_factory=list)
    strengths_summary: str = ""
    gaps_summary: str = ""
    recommendation: str = ""