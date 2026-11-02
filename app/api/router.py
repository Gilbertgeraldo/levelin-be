from fastapi import APIRouter

from app.api.v1 import (
    admin,
    attempts,
    audits,
    billing,
    courses,
    jobs,
    me,
    missions,
    notifications,
    org,
    passport,
    plans,
    reference,
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
    notifications,
    billing,
    admin,
):
    api_v1.include_router(module.router)
