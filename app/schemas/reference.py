import uuid
from pydantic import BaseModel, Field

from app.schemas.common import SkillRef


class OccupationOut(BaseModel):
    code: str
    isco_code: str
    title: str
    title_en: str | None = None
    avg_exposure: float | None = None
    exposure_gradient: int | None = None
    gradient_cat: str | None = None
    match: str | None = None


class OccupationDetail(OccupationOut):
    description: str | None = None
    aliases: list[str] = []
    gradient_label: str
    task_count: int
    task_groups: dict[str, int]


class OccupationTaskOut(BaseModel):
    id: str = Field(description="Id tugas ILO untuk template_tasks, mis. '4311-1'")
    description: str
    description_en: str | None = None
    ilo_score: float
    kind: str | None = None
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
    direction: str = Field(description="naik | pindah (data tim) atau mirip (kemiripan skill)")
    occupation: OccupationOut
    similarity: float | None
    skills_to_learn: list[SkillRef]


class SkillPromptTemplate(BaseModel):
    title: str
    when_to_use: str
    prompt: str


class SkillMicroDrill(BaseModel):
    scenario: str
    question: str
    evaluation_criteria: list[str]
    hint: str


class RelatedMissionSummary(BaseModel):
    id: uuid.UUID
    slug: str
    title: str
    est_minutes: int


class SkillCoachOut(BaseModel):
    skill: SkillRef
    category: str
    engine: str
    overview: str
    key_steps: list[str]
    prompt_templates: list[SkillPromptTemplate]
    micro_drill: SkillMicroDrill
    related_missions: list[RelatedMissionSummary] = []


class SkillDrillCheckIn(BaseModel):
    scenario: str = Field(min_length=5, max_length=3000)
    question: str = Field(min_length=5, max_length=1000)
    answer: str = Field(min_length=10, max_length=4000)


class SkillDrillCheckOut(BaseModel):
    skill_id: int
    score: int = Field(ge=0, le=100)
    ready_for_mission: bool
    feedback: str
    strengths: list[str]
    improvements: list[str]
    model_answer: str
    engine: str
