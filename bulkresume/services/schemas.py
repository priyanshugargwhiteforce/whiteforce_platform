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


class ResumeExtraction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = ""
    email: str = ""
    phone: str = ""
    linkedin_url: str = ""
    other_urls: List[str] = Field(default_factory=list)
    education: List[Education] = Field(default_factory=list)
    experience: List[Experience] = Field(default_factory=list)
    skills: List[str] = Field(default_factory=list)
    certifications: List[str] = Field(default_factory=list)
    internships: List[Experience] = Field(default_factory=list)
    profile_summary: str = ""