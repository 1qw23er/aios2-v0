"""Tests for scripts/wb_draft_to_aios.py (Gap #1: WorkBuddy -> AIOS draft).

Verifies the thin adapter that lands a WorkBuddy A-E draft into AIOS as a real
``Artifact(type=CONTENT_DRAFT)`` with a server-derived ``agent:workbuddy``
producer, frozen checksum, and parsed outline/conversion anchors.

The script depends only on zero-migration modules (#108-A adds no schema), so
the harness builds tables via ``SQLModel.metadata.create_all`` -- equivalent to
the applied migrations for these tables. A file-backed sqlite URL is used so the
script's own ``make_session()`` (a separate connection/engine) shares the data.

No paid model call: the script only creates; review/approval are out of scope.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
from sqlmodel import Session, SQLModel

# Load the script as a module (it uses absolute aios.* imports). Located in
# ../scripts relative to this tests/ file.
_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "wb_draft_to_aios.py"
_spec = importlib.util.spec_from_file_location("wb_draft_to_aios", _SCRIPT)
wb_draft_to_aios = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(wb_draft_to_aios)

from aios.db import get_engine  # noqa: E402
from aios.models import (  # noqa: E402
    Artifact,
    ArtifactReviewStatus,
    ArtifactType,
    Project,
    ProjectStatus,
)

PROJECT_ID = "proj_test_wb_draft"


@pytest.fixture()
def db_url(tmp_path: Path, monkeypatch) -> str:
    """File-backed sqlite so make_session() (separate connection) shares data.

    Uses monkeypatch.setenv so AIOS_DATABASE_URL is auto-restored after the test,
    preventing a stale DB url from leaking into later tests (REQUEST_CHANGES G).
    """
    url = f"sqlite:///{tmp_path / 'test_wb_draft.db'}"
    monkeypatch.setenv("AIOS_DATABASE_URL", url)
    engine = get_engine(url)
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        s.add(
            Project(
                id=PROJECT_ID,
                name="test",
                objective="test objective",
                status=ProjectStatus.PROPOSED,
            )
        )
        s.commit()
    return url


def _draft_body() -> str:
    return """# 初稿示例

### A. 选题
- 标题候选：电商卖家如何用 AI 做内容
- 目标用户：小电商团队

### B. 事实素材
- 已验证：AI觅提供 AI 作图

### C. 文章结构
- 开头钩子：你每天都在手动做图吗
- CTA：点击注册 AI觅，用魔法打败魔法

### D. 初稿
正文……

### E. 配图需求
- 手绘风对比图
"""


def test_create_draft_lands_artifact(db_url):
    result = wb_draft_to_aios.create_draft(
        project_id=PROJECT_ID,
        topic="电商卖家如何用 AI 做内容",
        body=_draft_body(),
        idempotency_key="test-key-1",
    )
    assert result["type"] == str(ArtifactType.CONTENT_DRAFT)
    assert result["revision_count"] == "0"
    assert result["review_status"] == str(ArtifactReviewStatus.UNVERIFIED)
    assert result["producer"] == "agent:workbuddy"
    assert result["artifact_id"]
    assert result["checksum"].startswith("sha256:")

    engine = get_engine(db_url)
    with Session(engine) as s:
        art = s.get(Artifact, result["artifact_id"])
        assert art is not None
        meta = art.metadata_json or {}
        assert meta["topic"] == "电商卖家如何用 AI 做内容"
        assert any("开头钩子" in o for o in (meta.get("outline") or []))
        assert any(a.get("kind") == "cta" for a in (meta.get("conversion_anchors") or []))


def test_parse_ae_sections_extracts_outline_and_cta():
    sections = wb_draft_to_aios._parse_ae_sections(_draft_body())
    assert "C" in sections and "D" in sections
    outline = wb_draft_to_aios._build_outline(sections)
    assert outline and any("开头钩子" in o for o in outline)
    anchors = wb_draft_to_aios._build_conversion_anchors(sections)
    assert anchors and anchors[0]["kind"] == "cta"


def test_create_draft_no_ae_markers_still_lands(db_url):
    result = wb_draft_to_aios.create_draft(
        project_id=PROJECT_ID,
        topic="纯文本草稿",
        body="就是一段没有分节标记的普通正文。",
        idempotency_key="test-key-2",
    )
    assert result["artifact_id"]
    assert result["review_status"] == str(ArtifactReviewStatus.UNVERIFIED)
