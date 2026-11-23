"""One-shot backfill: every document without a workspace gets one.

Usage:
    python -m finengine.store.migrate_workspaces           # dry run
    python -m finengine.store.migrate_workspaces --apply   # write
"""
from __future__ import annotations

import argparse
import logging
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.engine import Engine

from finengine.config import settings
from finengine.store.dialogs import get_dialog_store
from finengine.store.documents import DOCUMENT
from finengine.store.engine import движок
from finengine.store.workspaces import WorkspaceStore

log = logging.getLogger(__name__)


def _folder_timestamp(folder: dict[str, Any]) -> int:
    return int(folder.get("created_at") or folder.get("updated_at") or 0)


def _collect_mappings(state: dict[str, Any]) -> dict[str, list[tuple[str, int]]]:
    """doc_id -> [(folder_id, timestamp), ...] from a dialog state."""
    result: dict[str, list[tuple[str, int]]] = defaultdict(list)
    folders = {f["id"]: f for f in (state.get("folders") or []) if isinstance(f, dict) and f.get("id")}

    for folder in folders.values():
        fid = folder["id"]
        ts = _folder_timestamp(folder)
        for doc_id in folder.get("doc_ids") or []:
            if isinstance(doc_id, str):
                result[doc_id].append((fid, ts))

    for project in (state.get("projects") or []):
        if not isinstance(project, dict):
            continue
        folder_id = project.get("folder_id")
        if not folder_id or folder_id not in folders:
            continue
        ts = _folder_timestamp(folders[folder_id])
        for doc_id in project.get("doc_ids") or []:
            if isinstance(doc_id, str):
                result[doc_id].append((folder_id, ts))

    return result


def _resolve_workspace(doc_id: str, owner_id: int, state: dict[str, Any]) -> str | None:
    """Pick a folder for a document, or None for default workspace."""
    mappings = _collect_mappings(state)
    pairs = mappings.get(doc_id) or []
    # Only folders of the same owner are valid; foreign docs land in default.
    if not pairs:
        return None
    pairs.sort(key=lambda x: x[1])
    chosen = pairs[0][0]
    if len(pairs) > 1:
        log.info("doc %s owner %s: conflict, choosing %s over %s",
                 doc_id, owner_id, chosen, [p[0] for p in pairs[1:]])
    return chosen


def _workspace_metadata(
    state: dict[str, Any], workspace_id: str
) -> tuple[str, datetime | None]:
    folder = next((
        item for item in (state.get("folders") or [])
        if isinstance(item, dict) and item.get("id") == workspace_id
    ), None)
    if folder is None:
        return "", None
    stamp = int(folder.get("updated_at") or folder.get("created_at") or 0)
    return (
        str(folder.get("name") or ""),
        datetime.fromtimestamp(stamp / 1000, tz=timezone.utc),
    )


def migrate(engine: Engine, apply: bool) -> dict[int, int]:
    """Dry run or apply. Returns owner_id -> count of updated rows."""
    workspace_store = WorkspaceStore(engine) if apply else None
    doc_store_connection = engine
    dialog_store = get_dialog_store()

    counts: dict[int, int] = {}

    with doc_store_connection.begin() as conn:
        owner_rows = conn.execute(select(DOCUMENT.c.owner_id).distinct()).all()
        owner_ids = [row[0] for row in owner_rows]

    for owner_id in owner_ids:
        state = dialog_store.load(owner_id)
        doc_ids_to_update: list[tuple[str, str]] = []

        with doc_store_connection.begin() as conn:
            rows = conn.execute(
                select(DOCUMENT.c.doc_id)
                .where(DOCUMENT.c.owner_id == owner_id, DOCUMENT.c.workspace_id.is_(None))
            ).all()

            for (doc_id,) in rows:
                workspace_id = _resolve_workspace(doc_id, owner_id, state)
                if workspace_id is None:
                    workspace_id = f"owner:{owner_id}"
                if workspace_store is not None:
                    name, updated_at = _workspace_metadata(state, workspace_id)
                    try:
                        workspace_store.upsert_workspace(
                            owner_id=owner_id,
                            workspace_id=workspace_id,
                            name=name,
                            updated_at=updated_at,
                        )
                    except ValueError:
                        log.warning(
                            "doc %s: workspace %s is foreign or invalid; using default",
                            doc_id, workspace_id,
                        )
                        workspace_id = f"owner:{owner_id}"
                        workspace_store.upsert_workspace(
                            owner_id=owner_id, workspace_id=workspace_id, name="")
                doc_ids_to_update.append((doc_id, workspace_id))

        if apply:
            with doc_store_connection.begin() as conn:
                for doc_id, workspace_id in doc_ids_to_update:
                    conn.execute(
                        update(DOCUMENT)
                        .where(
                            DOCUMENT.c.doc_id == doc_id,
                            DOCUMENT.c.workspace_id.is_(None),
                        )
                        .values(workspace_id=workspace_id)
                    )

        counts[owner_id] = len(doc_ids_to_update)
        log.info("owner %s: %s documents -> %s", owner_id, len(doc_ids_to_update),
                 "written" if apply else "would be written")

    return counts


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    parser = argparse.ArgumentParser(description="Backfill document.workspace_id")
    parser.add_argument("--apply", action="store_true", help="write changes")
    args = parser.parse_args()

    url = settings.documents_database_url
    if not url:
        raise SystemExit("FINENGINE_DOCUMENTS_DATABASE_URL is not set")

    engine = движок(url, pool_pre_ping=True, future=True, echo=False)
    counts = migrate(engine, apply=args.apply)

    total = sum(counts.values())
    print(f"{'Wrote' if args.apply else 'Would write'} {total} workspace assignments across {len(counts)} owners")


if __name__ == "__main__":
    main()
