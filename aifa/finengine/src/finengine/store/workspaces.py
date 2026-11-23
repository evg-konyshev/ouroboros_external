"""Таблица workspace: серверная группа документов, соответствующая папке."""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import TypedDict

from sqlalchemy import Column, DateTime, Integer, MetaData, String, Table, case, false, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine import Engine
from sqlalchemy.sql import ColumnElement

from finengine.store.engine import движок

log = logging.getLogger(__name__)

_metadata = MetaData()

_WORKSPACE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

WORKSPACE = Table(
    "workspace", _metadata,
    Column("workspace_id", String(64), primary_key=True),
    Column("owner_id", Integer, nullable=False, index=True),
    Column("name", String(255), nullable=False, default=""),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)


class WorkspaceRow(TypedDict):
    workspace_id: str
    owner_id: int
    name: str
    created_at: datetime
    updated_at: datetime


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _validate_workspace_id(owner_id: int, workspace_id: str) -> None:
    if not workspace_id:
        raise ValueError("workspace_id не может быть пустым")
    if workspace_id.startswith("owner:"):
        if workspace_id != f"owner:{owner_id}":
            raise ValueError("зарезервированный префикс owner: принадлежит другому владельцу")
        return
    if len(workspace_id) > 64 or not _WORKSPACE_ID_RE.match(workspace_id):
        raise ValueError(f"недопустимый workspace_id: {workspace_id!r}")


class WorkspaceStore:
    """Хранилище workspace. Таблица создаётся при первом обращении."""

    def __init__(self, engine_or_url: str | Engine):
        if isinstance(engine_or_url, Engine):
            self._engine = engine_or_url
        else:
            self._engine = движок(engine_or_url, pool_pre_ping=True, future=True, echo=False)
        _metadata.create_all(self._engine)

    def upsert_workspace(
        self,
        owner_id: int,
        workspace_id: str,
        name: str = "",
        updated_at: datetime | None = None,
    ) -> None:
        """Создать или обновить свой workspace; чужой id не менять."""
        _validate_workspace_id(owner_id, workspace_id)
        now = _now()
        insert = pg_insert if self._engine.dialect.name == "postgresql" else sqlite_insert
        statement = insert(WORKSPACE).values(
            workspace_id=workspace_id,
            owner_id=owner_id,
            name=name,
            created_at=now,
            updated_at=updated_at or now,
        )
        should_update: ColumnElement[bool]
        if updated_at is not None:
            should_update = (
                (WORKSPACE.c.updated_at < statement.excluded.updated_at)
                | ((WORKSPACE.c.name == "") & (statement.excluded.name != ""))
            )
        elif name:
            should_update = WORKSPACE.c.name != name
        else:
            should_update = false()
        result = self._execute_upsert(statement, should_update, owner_id)
        if result is None:
            raise ValueError("workspace_id принадлежит другому владельцу")

    def _execute_upsert(self, statement, should_update, owner_id: int) -> str | None:
        with self._engine.begin() as conn:
            return conn.execute(
                statement.on_conflict_do_update(
                    index_elements=[WORKSPACE.c.workspace_id],
                    set_={
                        "name": case(
                            (should_update, statement.excluded.name),
                            else_=WORKSPACE.c.name,
                        ),
                        "updated_at": case(
                            (should_update, statement.excluded.updated_at),
                            else_=WORKSPACE.c.updated_at,
                        ),
                    },
                    where=WORKSPACE.c.owner_id == owner_id,
                ).returning(WORKSPACE.c.workspace_id)
            ).scalar_one_or_none()

    def get_workspace(self, workspace_id: str) -> WorkspaceRow | None:
        with self._engine.begin() as conn:
            row = conn.execute(
                select(WORKSPACE).where(WORKSPACE.c.workspace_id == workspace_id)
            ).first()
            if row is None:
                return None
            return WorkspaceRow(
                workspace_id=row.workspace_id,
                owner_id=row.owner_id,
                name=row.name,
                created_at=row.created_at,
                updated_at=row.updated_at,
            )
