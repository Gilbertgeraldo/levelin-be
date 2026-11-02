"""Login demo hanya hidup bila dinyalakan eksplisit, hanya untuk akun demo; tujuan tautan undangan per peran."""

import pytest

from app.core.config import get_settings
from app.services.org import invite_redirect
from tests.conftest import HR_EMAIL

pytestmark = pytest.mark.anyio


async def test_demo_login_off_by_default(client):
    r = await client.post("/v1/dev/demo-login", json={"email": HR_EMAIL})
    assert r.status_code == 404


async def test_demo_login_issues_working_token_for_demo_accounts_only(client, monkeypatch):
    monkeypatch.setattr(get_settings(), "dev_login_enabled", True)

    r = await client.post("/v1/dev/demo-login", json={"email": HR_EMAIL})
    assert r.status_code == 200
    token = r.json()["access_token"]
    me = (await client.get("/v1/me", headers={"Authorization": f"Bearer {token}"})).json()
    assert me["email"] == HR_EMAIL and me["role"] == "hr_admin" and me["full_name"] == "Hana Pratiwi"

    r = await client.post("/v1/dev/demo-login", json={"email": "direktur@perusahaan-asli.co.id"})
    assert r.status_code == 403

    monkeypatch.setattr(get_settings(), "app_env", "production")
    r = await client.post("/v1/dev/demo-login", json={"email": HR_EMAIL})
    assert r.status_code == 404


def test_invite_redirect_sends_hr_and_managers_to_web(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "web_url", "https://hr.levelin.id/")
    monkeypatch.setattr(settings, "app_auth_redirect_url", "levelin://masuk")
    assert invite_redirect("hr_admin") == "https://hr.levelin.id/masuk/kata-sandi"
    assert invite_redirect("manager") == "https://hr.levelin.id/masuk/kata-sandi"
    assert invite_redirect("worker") == "levelin://masuk"
