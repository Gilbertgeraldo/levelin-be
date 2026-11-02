import uuid

from pydantic import BaseModel

from app.schemas.common import SkillRef


class CourseOut(BaseModel):
    id: uuid.UUID
    title: str
    provider: str
    url: str
    description: str | None
    price: int
    is_free: bool
    duration_hours: float | None
    level: str | None
    language: str
    rating: float | None
    is_partner: bool
    skills: list[SkillRef]
    gap_skills_covered: list[SkillRef]
    gap_covered_count: int


class CourseClickOut(BaseModel):
    redirect_url: str
    is_partner: bool
