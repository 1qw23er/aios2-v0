"""Tests for the persistent secret store (#103).

Covers the contract in ``docs/issue-103-secret-store-plan.md``:
  * no plaintext / one-way HMAC tags only (G1)
  * issue -> resolve -> revoke round-trip
  * ``row_mac`` row-level binding rejects a transplanted tag (G1 / §4.2)
  * KEK missing -> factory + store fail closed (503, G3 / G6)
  * malformed input + KEK missing still 503 (readiness before format, §6)
  * operational (DB) failure -> 503, never a silent ``None`` (G3)
  * dual-mode compensation ``revoke`` commits independently of a caller rollback
  * bootstrap atomic issue (commit -> resolvable; rollback -> not consumable)
  * deterministic concurrency revoke gate (post-commit revoke visible)
  * migration: table + index present; fail-closed downgrade on any row; lossless
    downgrade when empty.
"""

from __future__ import annotations

import hashlib
import hmac
from pathlib import Path

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

import aios.secrets_store as secrets_mod
from aios.api.security import authenticate_agent
from aios.db import run_migrations
from aios.models import Agent, AgentSecret
from aios.secrets_store import (
    AgentSecretStore,
    EncryptedDbAgentSecretStore,
    SecretStoreMisconfigured,
    SecretStoreUnavailable,
    _load_kek,
    get_secret_store,
    reset_secret_store,
    validate_secret_store_config,
)

KEK = bytes.fromhex("00" * 32)
KEK_HEX = "00" * 32


def _db_url(tmp_path: Path, name: str) -> str:
    return f"sqlite:///{(tmp_path / name).as_posix()}"


def _make_store(tmp_path: Path, name: str = "store.db") -> EncryptedDbAgentSecretStore:
    """Build a migrated temp DB + an encrypted store bound to it."""
    url = _db_url(tmp_path, name)
    run_migrations(url)
    engine = create_engine(url)
    return EncryptedDbAgentSecretStore(KEK, session_factory=lambda: Session(engine))


def _seed_agent(engine, agent_id: str) -> None:
    with Session(engine) as s:
        s.add(Agent(id=agent_id, name="n", role="r", adapter_type="api"))
        s.commit()


# ---------------------------------------------------------------------------
# In-memory default backend (no KEK, always ready)
# ---------------------------------------------------------------------------


def test_inmemory_issue_resolve_revoke() -> None:
    store = AgentSecretStore()
    tok = store.issue("agt_x")
    assert store.resolve(tok) == "agt_x"
    store.revoke("agt_x")
    assert store.resolve(tok) is None


def test_inmemory_malformed_token_returns_none_not_503() -> None:
    store = AgentSecretStore()
    # In-memory backend is always ready, so a malformed token is a 401 (None),
    # never a 503.
    assert store.resolve("not-a-token") is None
    assert store.resolve(None) is None


# ---------------------------------------------------------------------------
# Encrypted backend: no plaintext / one-way tags (G1)
# ---------------------------------------------------------------------------


def test_no_plaintext_and_tags_are_one_way(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    engine = store._session_factory().get_bind()
    _seed_agent(engine, "agt_plain")
    token = store.issue("agt_plain")

    with store._session_factory() as s:
        row = s.execute(
            select(AgentSecret).where(AgentSecret.agent_id == "agt_plain")
        ).scalar_one()

    # No plaintext anywhere in the stored columns.
    assert token.encode("utf-8") not in row.token_tag
    assert token.encode("utf-8") not in (row.row_mac or b"")
    # token_tag is a 32-byte SHA-256 HMAC, distinct from row_mac.
    assert len(row.token_tag) == 32
    assert len(row.row_mac) == 32
    assert row.token_tag != row.row_mac
    # One-way: the tag cannot be reversed to recover the token.
    assert row.token_tag != token.encode("utf-8")
    # revoke_at starts NULL for the active token.
    assert row.revoked_at is None


def test_plaintext_not_in_database_file(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    engine = store._session_factory().get_bind()
    _seed_agent(engine, "agt_file")
    token = store.issue("agt_file")
    # Read the raw on-disk bytes and assert the bearer never touched storage.
    db_path = Path(engine.url.database)
    raw = db_path.read_bytes()
    assert token.encode("utf-8") not in raw


# ---------------------------------------------------------------------------
# Encrypted backend: issue -> resolve -> revoke
# ---------------------------------------------------------------------------


def test_encrypted_issue_resolve_revoke(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    engine = store._session_factory().get_bind()
    _seed_agent(engine, "agt_round")
    tok = store.issue("agt_round")
    assert store.resolve(tok) == "agt_round"
    store.revoke("agt_round")
    assert store.resolve(tok) is None


def test_encrypted_unknown_token_returns_none(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    engine = store._session_factory().get_bind()
    _seed_agent(engine, "agt_unknown")
    store.issue("agt_unknown")
    # A well-formed-but-unknown token resolves to None (401 path).
    assert store.resolve("aios_ag_" + "z" * 43) is None


# ---------------------------------------------------------------------------
# Row-level binding: transplanted tag is rejected (G1 / §4.2)
# ---------------------------------------------------------------------------


def test_row_mac_transplant_rejected_with_503(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    engine = store._session_factory().get_bind()
    _seed_agent(engine, "agt_src")
    _seed_agent(engine, "agt_dst")
    token_src = store.issue("agt_src")

    with Session(engine) as s:
        # Give the destination its own legitimate secret row first.
        s.add(
            AgentSecret(
                agent_id="agt_dst",
                token_tag=_hmac(b"dst"),
                row_mac=_hmac(b"agt_dst" + _hmac(b"dst")),
            )
        )
        s.commit()

    # Attacker deletes the source row and moves its (token_tag, row_mac) onto
    # the destination row -- exactly the "tag transplanted to another agent"
    # attack the row_mac binding is meant to stop.
    with Session(engine) as s:
        src = s.get(AgentSecret, "agt_src")
        tag, mac = src.token_tag, src.row_mac
        s.delete(src)
        s.commit()
        dst = s.get(AgentSecret, "agt_dst")
        dst.token_tag = tag
        dst.row_mac = mac
        s.commit()

    with pytest.raises(SecretStoreUnavailable):
        store.resolve(token_src)


def _hmac(msg: bytes) -> bytes:
    return hmac.new(KEK, msg, hashlib.sha256).digest()


# ---------------------------------------------------------------------------
# Fail-closed: KEK missing (G3 / G6)
# ---------------------------------------------------------------------------


def test_encrypted_store_rejects_with_missing_kek() -> None:
    store = EncryptedDbAgentSecretStore(b"")  # empty KEK -> not ready
    with pytest.raises(SecretStoreUnavailable):
        store.resolve("aios_ag_" + "x" * 43)
    with pytest.raises(SecretStoreUnavailable):
        store.issue("agt_any")


def test_factory_fail_closed_when_backend_requires_missing_kek(
    monkeypatch,
) -> None:
    import aios.secrets_store as secrets_mod

    # Force the factory to re-evaluate the backend from the environment.
    monkeypatch.setattr(secrets_mod, "_STORE", None)
    monkeypatch.setenv("AIOS_SECRET_STORE_BACKEND", "encrypted_db")
    monkeypatch.delenv("AIOS_SECRET_MASTER_KEY", raising=False)
    with pytest.raises(SecretStoreUnavailable):
        get_secret_store()
    # Restore the default memory backend for the rest of the suite.
    reset_secret_store()


def test_factory_rejects_unknown_backend_fail_closed(monkeypatch) -> None:
    # P1-B (G6): an unsupported explicit AIOS_SECRET_STORE_BACKEND value must
    # FAIL CLOSED, never silently fall back to the in-memory backend (which
    # would silently lose credential persistence across restart).
    import aios.secrets_store as secrets_mod

    monkeypatch.setattr(secrets_mod, "_STORE", None)
    monkeypatch.setenv("AIOS_SECRET_STORE_BACKEND", "encrypted-db")  # typo
    with pytest.raises(SecretStoreUnavailable):
        get_secret_store()
    # Explicit 'memory' is still permitted (the documented default).
    monkeypatch.setenv("AIOS_SECRET_STORE_BACKEND", "memory")
    monkeypatch.setattr(secrets_mod, "_STORE", None)
    store = get_secret_store()
    assert isinstance(store, AgentSecretStore)
    reset_secret_store()


def test_kek_missing_malformed_still_503(tmp_path: Path, monkeypatch) -> None:
    # Readiness check precedes token-format short-circuit (§6): a malformed
    # token with an unavailable store must still surface 503.
    store = EncryptedDbAgentSecretStore(b"")
    with pytest.raises(SecretStoreUnavailable):
        store.resolve("totally-malformed")


def test_load_kek_from_hex_and_rejects_garbage(monkeypatch) -> None:
    # The real loader accepts hex and treats garbage / absence as None.
    monkeypatch.setenv("AIOS_SECRET_MASTER_KEY", "00" * 32)
    assert _load_kek() == KEK
    monkeypatch.setenv("AIOS_SECRET_MASTER_KEY", "not-hex!!")
    assert _load_kek() is None
    monkeypatch.delenv("AIOS_SECRET_MASTER_KEY", raising=False)
    assert _load_kek() is None


def test_load_kek_enforces_32_byte_length(monkeypatch) -> None:
    # Frozen plan §4.1: the KEK MUST be exactly 32 bytes; a key of the wrong
    # size is rejected (fail-closed), never silently coerced.
    import base64 as _b64

    # Wrong-size hex keys are rejected.
    monkeypatch.setenv("AIOS_SECRET_MASTER_KEY", "00")  # 1 byte
    assert _load_kek() is None
    monkeypatch.setenv("AIOS_SECRET_MASTER_KEY", "00" * 16)  # 16 bytes
    assert _load_kek() is None
    # A base64-encoded 32-byte key is accepted.
    monkeypatch.setenv("AIOS_SECRET_MASTER_KEY", _b64.urlsafe_b64encode(KEK).decode())
    assert _load_kek() == KEK
    monkeypatch.delenv("AIOS_SECRET_MASTER_KEY", raising=False)
    assert _load_kek() is None


# ---------------------------------------------------------------------------
# Operational failure -> 503, never silent None (G3)
# ---------------------------------------------------------------------------


class _BrokenSession:
    def execute(self, *args, **kwargs):  # pragma: no cover - injected failure
        raise SQLAlchemyError("simulated backend outage")

    def close(self) -> None:  # pragma: no cover - injected failure
        pass


def test_backend_outage_raises_503(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    store._session_factory = lambda: _BrokenSession()
    with pytest.raises(SecretStoreUnavailable):
        store.resolve("aios_ag_" + "x" * 43)


def test_malformed_token_with_db_down_returns_503(tmp_path: Path) -> None:
    # Readiness (incl. backend) precedes the format short-circuit (§6 / G3):
    # a malformed token must STILL yield 503 when the backend is unreachable,
    # never a format-derived 401. This is the attack that distinguishes
    # "store down" from "bad token format".
    store = _make_store(tmp_path)
    store._session_factory = lambda: _BrokenSession()
    with pytest.raises(SecretStoreUnavailable):
        store.resolve("totally-malformed")


def test_resolve_503_when_secret_table_missing(tmp_path: Path) -> None:
    # P1-A (G3): the readiness probe must touch the secret TABLE itself, not a
    # bare 'SELECT 1'. If the DB is up but agent_secret is absent (migration
    # not applied), resolve must 503 for EVERY input -- malformed AND well-
    # formed -- so no format-dependent 401/503 split leaks.
    url = _db_url(tmp_path, "unmigrated.db")
    engine = create_engine(url)
    # No run_migrations(): agent_secret does not exist on this database.
    store = EncryptedDbAgentSecretStore(KEK, session_factory=lambda: Session(engine))
    with pytest.raises(SecretStoreUnavailable):
        store.resolve("totally-malformed")  # would otherwise be 401
    with pytest.raises(SecretStoreUnavailable):
        store.resolve("aios_ag_" + "x" * 43)  # would otherwise be 401/unknown


def test_session_factory_failure_maps_to_503(tmp_path: Path) -> None:
    # G3: a session that cannot be opened (engine/URL misconfigured) must map to
    # 503 on BOTH issue and resolve, not escape as an unclassified 500.
    store = _make_store(tmp_path)

    def _boom_factory() -> Session:  # pragma: no cover - injected failure
        raise SQLAlchemyError("engine init failed")

    store._session_factory = _boom_factory
    with pytest.raises(SecretStoreUnavailable):
        store.resolve("aios_ag_" + "x" * 43)
    with pytest.raises(SecretStoreUnavailable):
        store.issue("agt_x")


def test_compensation_revoke_survives_caller_rollback(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    engine = store._session_factory().get_bind()
    _seed_agent(engine, "agt_comp")

    # issue with the DEFAULT session=None -> store opens its OWN session and
    # commits the tag independently of any caller transaction.
    tok = store.issue("agt_comp")
    assert store.resolve(tok) == "agt_comp"

    # Compensation: revoke() with default session=None commits independently.
    store.revoke("agt_comp")

    # A (unrelated) caller transaction rolls back -- the already-committed,
    # now-revoked tag must NOT come back.
    with Session(engine) as caller:
        caller.rollback()

    assert store.resolve(tok) is None


# ---------------------------------------------------------------------------
# Bootstrap atomic issue: commit -> resolvable; rollback -> not consumable
# ---------------------------------------------------------------------------


def test_bootstrap_issue_committed_then_resolvable(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    engine = store._session_factory().get_bind()
    with Session(engine) as caller:
        caller.add(Agent(id="agt_boot", name="n", role="r", adapter_type="api"))
        tok = store.issue("agt_boot", session=caller)
        caller.commit()
    # Post-commit, a fresh request can resolve the token.
    assert store.resolve(tok) == "agt_boot"


def test_bootstrap_issue_rolled_back_token_not_consumable(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    engine = store._session_factory().get_bind()
    with Session(engine) as caller:
        caller.add(Agent(id="agt_boot2", name="n", role="r", adapter_type="api"))
        tok = store.issue("agt_boot2", session=caller)
        # Simulate a later failure: roll back the whole bootstrap transaction.
        caller.rollback()
    # Token was never durably issued -> cannot be consumed (strict single-use
    # replay semantics preserved).
    assert store.resolve(tok) is None


# ---------------------------------------------------------------------------
# Deterministic concurrency revoke gate (G7 / §4.7)
# ---------------------------------------------------------------------------


def test_revoke_visible_to_new_resolve_after_commit(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    engine = store._session_factory().get_bind()
    _seed_agent(engine, "agt_gate")
    tok = store.issue("agt_gate")
    assert store.resolve(tok) == "agt_gate"

    # A second, independent session revokes and commits.
    with Session(engine) as revoker:
        revoker.execute(
            text(
                "UPDATE agent_secret SET revoked_at = "
                "(SELECT created_at FROM agent_secret WHERE agent_id='agt_gate') "
                "WHERE agent_id='agt_gate'"
            )
        )
        revoker.commit()

    # A brand-new resolve (as if from another replica) must see the revocation.
    with Session(engine):
        assert store.resolve(tok) is None


# ---------------------------------------------------------------------------
# Migration: table + index, fail-closed downgrade, lossless empty downgrade
# ---------------------------------------------------------------------------


def _alembic_cfg(url: str) -> object:
    from alembic.config import Config

    root = Path(__file__).resolve().parents[1]
    cfg = Config(root / "alembic.ini")
    cfg.set_main_option("script_location", str(root / "alembic"))
    cfg.set_main_option("sqlalchemy.url", url)
    return cfg


def test_migration_creates_table_and_unique_index(tmp_path: Path) -> None:
    url = _db_url(tmp_path, "mig.db")
    run_migrations(url)
    engine = create_engine(url)
    with Session(engine) as s:
        from sqlalchemy import inspect

        insp = inspect(s.get_bind())
        assert "agent_secret" in insp.get_table_names()
        cols = {c["name"] for c in insp.get_columns("agent_secret")}
        assert {"agent_id", "token_tag", "row_mac", "created_at", "revoked_at"} <= cols
        idx = insp.get_indexes("agent_secret")
        unique = [i for i in idx if i.get("unique")]
        assert len(unique) == 1
        assert unique[0]["column_names"] == ["token_tag"]
        assert unique[0]["name"] == "uq_agent_secret_token_tag"


def test_migration_downgrade_fail_closed_with_row(tmp_path: Path) -> None:
    from alembic import command

    url = _db_url(tmp_path, "down.db")
    run_migrations(url)
    engine = create_engine(url)
    with Session(engine) as s:
        s.add(Agent(id="agt_d", name="n", role="r", adapter_type="api"))
        s.commit()
        s.add(
            AgentSecret(
                agent_id="agt_d",
                token_tag=b"x" * 32,
                row_mac=b"y" * 32,
            )
        )
        s.commit()

    cfg = _alembic_cfg(url)
    with pytest.raises(RuntimeError):
        command.downgrade(cfg, "20260729_0001")


def test_migration_downgrade_ok_when_empty(tmp_path: Path) -> None:
    from alembic import command

    url = _db_url(tmp_path, "down_empty.db")
    run_migrations(url)
    cfg = _alembic_cfg(url)
    # Empty agent_secret -> lossless downgrade succeeds.
    command.downgrade(cfg, "20260729_0001")
    engine = create_engine(url)
    with Session(engine) as s:
        from sqlalchemy import inspect

        insp = inspect(s.get_bind())
        assert "agent_secret" not in insp.get_table_names()


# ---------------------------------------------------------------------------
# G3 readiness ordering at the HTTP boundary (authenticate_agent)
# ---------------------------------------------------------------------------


def test_authenticate_agent_missing_bearer_503_when_store_unavailable(
    monkeypatch,
) -> None:
    # G3: the readiness check MUST precede the missing-credential 401 branch.
    # With the encrypted_db backend configured but the KEK absent, a request
    # carrying NO bearer credential must still surface 503 (store unavailable),
    # never a token-dependent 401. Codex review P1 on security.py:196-199.
    monkeypatch.setattr(secrets_mod, "_STORE", None)
    monkeypatch.setenv("AIOS_SECRET_STORE_BACKEND", "encrypted_db")
    monkeypatch.delenv("AIOS_SECRET_MASTER_KEY", raising=False)
    try:
        with pytest.raises(HTTPException) as exc:
            authenticate_agent(credentials=None)
        assert exc.value.status_code == 503
    finally:
        reset_secret_store()


def test_authenticate_agent_missing_bearer_401_when_store_ready(
    monkeypatch,
) -> None:
    # Contrast: the default in-memory backend is always ready, so a missing
    # bearer is a 401 (unknown credential), not a 503. Confirms the readiness
    # branch does not mask the legitimate missing-credential path.
    monkeypatch.setattr(secrets_mod, "_STORE", None)
    monkeypatch.delenv("AIOS_SECRET_STORE_BACKEND", raising=False)
    monkeypatch.delenv("AIOS_SECRET_MASTER_KEY", raising=False)
    try:
        with pytest.raises(HTTPException) as exc:
            authenticate_agent(credentials=None)
        assert exc.value.status_code == 401
    finally:
        reset_secret_store()


# --- Startup fail-closed configuration validation (#103 follow-up) ---


def test_validate_config_memory_default_ok(monkeypatch) -> None:
    monkeypatch.delenv("AIOS_SECRET_STORE_BACKEND", raising=False)
    monkeypatch.delenv("AIOS_SECRET_MASTER_KEY", raising=False)
    # No exception for the default in-memory backend (no KEK required).
    validate_secret_store_config()


def test_validate_config_encrypted_db_valid_kek_ok(monkeypatch) -> None:
    monkeypatch.setenv("AIOS_SECRET_STORE_BACKEND", "encrypted_db")
    monkeypatch.setenv("AIOS_SECRET_MASTER_KEY", KEK_HEX)
    # A correctly provisioned encrypted_db backend passes validation.
    validate_secret_store_config()


def test_validate_config_encrypted_db_missing_kek_raises(monkeypatch) -> None:
    monkeypatch.setenv("AIOS_SECRET_STORE_BACKEND", "encrypted_db")
    monkeypatch.delenv("AIOS_SECRET_MASTER_KEY", raising=False)
    with pytest.raises(SecretStoreMisconfigured):
        validate_secret_store_config()


def test_validate_config_encrypted_db_invalid_kek_raises(monkeypatch) -> None:
    monkeypatch.setenv("AIOS_SECRET_STORE_BACKEND", "encrypted_db")
    monkeypatch.setenv("AIOS_SECRET_MASTER_KEY", "00" * 16)  # 16 bytes, too short
    with pytest.raises(SecretStoreMisconfigured):
        validate_secret_store_config()


def test_validate_config_unknown_backend_raises(monkeypatch) -> None:
    monkeypatch.setenv("AIOS_SECRET_STORE_BACKEND", "vault")
    with pytest.raises(SecretStoreMisconfigured):
        validate_secret_store_config()
