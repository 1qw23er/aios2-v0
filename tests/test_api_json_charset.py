"""JSON responses must advertise an explicit ``charset=utf-8``.

Starlette emits ``Content-Type: application/json`` with no charset parameter.
Some Windows clients -- notably PowerShell 5.1 ``Invoke-RestMethod`` -- then
fall back to Latin-1 and turn every CJK character into mojibake. The app
therefore stamps ``application/json; charset=utf-8`` on every JSON response so
PowerShell / curl / browsers all decode it correctly.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from aios.api.app import create_app


@pytest.fixture
def client(tmp_path: Path, monkeypatch) -> TestClient:
    database_path = tmp_path / "json_charset.db"
    monkeypatch.setenv("AIOS_DATABASE_URL", f"sqlite:///{database_path.as_posix()}")
    with TestClient(create_app()) as test_client:
        yield test_client


def test_health_json_declares_utf8_charset(client: TestClient) -> None:
    response = client.get("/health")

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/json; charset=utf-8"


def test_cjk_json_response_decodes_as_utf8(client: TestClient) -> None:
    response = client.post(
        "/projects",
        json={"name": "拼多多选品助手", "objective": "验证中文往返"},
    )

    assert response.status_code == 201
    # The declared charset must be explicit, or PowerShell 5.1 mangles CJK.
    assert response.headers["content-type"] == "application/json; charset=utf-8"
    # Raw bytes must be real UTF-8, not the Latin-1 rendering of UTF-8 bytes.
    assert "拼多多选品助手" in response.content.decode("utf-8")
    assert response.json()["name"] == "拼多多选品助手"
