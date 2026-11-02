from pydantic import BaseModel

from app.schemas.common import SkillRef


class OccupationOut(BaseModel):
    code: str
    isco_code: str
    title: str
    title_en: str | None = None
    avg_exposure: float | None = None
    exposure_gradient: int | None = None
    match: str | None = None


class OccupationDetail(OccupationOut):
    description: str | None = None
    aliases: list[str] = []
    gradient_label: str
    task_count: int
    task_groups: dict[str, int]


class OccupationTaskOut(BaseModel):
    id: int
    description: str
    description_en: str | None = None
    ilo_score: float
    group: str
    group_label: str


class OccupationTasksOut(BaseModel):
    occupation: OccupationOut
    tasks: list[OccupationTaskOut]


class SkillOut(BaseModel):
    id: int
    label: str
    label_en: str | None = None
    category: str
    esco_uri: str | None = None


class NextRoleOut(BaseModel):
    occupation: OccupationOut
    similarity: float
    shared_skill_count: int
    skills_to_learn: list[SkillRef]
