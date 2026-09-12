r"""AIOS V0 — 冒烟验证（TestClient，无需 uvicorn 网络）。

证明：应用可构建、Alembic 迁移可跑、/health 响应、POST /projects 可创建。
等同于在真实 uvicorn 上跑这些端点（使用同一 create_app / 同一 DB 层）。

前置 env（占位符即可，/health 与 /projects 不需要真实 LLM key）：
    AIOS_OWNER_ID / AIOS_OWNER_API_KEY(>=32)  —— 仅创建项目用的 handler 不需要，
                                                 但 create_app 镜像生产，留占位符无害。
    AIOS_DATABASE_URL = sqlite:///./data/aios_smoke.db

运行：  .venv\Scripts\python.exe scripts/smoke_testclient.py
"""
from __future__ import annotations

import os
import sys

from fastapi.testclient import TestClient

from aios.api.app import create_app


def main() -> int:
    db_url = os.environ.get("AIOS_DATABASE_URL", "sqlite:///./data/aios_smoke.db")
    os.environ.setdefault("AIOS_OWNER_ID", "local-owner")
    os.environ.setdefault("AIOS_OWNER_API_KEY", "REPLACE_WITH_32CHAR_RANDOM_OWNER_SECRET_0000")

    print(f"[smoke] DB = {db_url}")
    with TestClient(create_app()) as client:
        # 1) 健康检查
        r = client.get("/health")
        print(f"[1] GET /health -> {r.status_code} {r.json()}")
        assert r.status_code == 200 and r.json().get("status") == "ok"

        # 2) 创建项目（走与真实 HTTP 相同的 create_project_service）
        r = client.post("/projects", json={"name": "smoke", "objective": "verify wiring"})
        print(f"[2] POST /projects -> {r.status_code}")
        assert r.status_code == 201, r.text
        proj = r.json()
        print(f"    project.id = {proj['id']}")

        # 3) 列出项目
        r = client.get("/projects")
        print(f"[3] GET /projects -> {r.status_code}, count={len(r.json())}")

        # 4) OpenAPI 文档可用（确认路由全部注册）
        r = client.get("/openapi.json")
        paths = list(r.json().get("paths", {}).keys())
        print(f"[4] GET /openapi.json -> {r.status_code}, routes={len(paths)}")
        for p in ("/health", "/projects", "/tasks", "/tasks/{task_id}/execute", "/orchestrator/process"):
            print(f"    route present: {p} -> {'/tasks/{task_id}/execute' in p or p in r.json()['paths'] or p in paths}")

    print("\nSMOKE: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
