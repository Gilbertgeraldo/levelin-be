from fastapi import APIRouter

from app.api.v1 import (
    admin,
    attempts,
    audits,
    billing,
    courses,
    dev,
    jobs,
    me,
    missions,
    notifications,
    org,
    passport,
    plans,
    reference,
    team,
)

api_v1 = APIRouter(prefix="/v1")
for module in (
    me,
    reference,
    audits,
    plans,
    courses,
    missions,
    attempts,
    passport,
    jobs,
    org,
    team,
    notifications,
    billing,
    admin,
    dev,
):
    api_v1.include_router(module.router)
