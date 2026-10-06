"""Delete stored campaign context revisions that repeat their predecessor's content.

Before deduplication in CampaignMemory.sync, every journal event stored a full
context snapshot even when nothing in it changed. This keeps the first revision
of each distinct content run, the current revision, and any revision whose id is
cited by another record, event or workspace file; it removes the rest from SQLite
and their projections under campaigns/<id>/manager/revisions/.

    uv run --no-sync python scripts/prune_context_revisions.py runs/workspace/workspace          # dry run
    uv run --no-sync python scripts/prune_context_revisions.py runs/workspace/workspace --apply  # delete + VACUUM
"""
from __future__ import annotations

import argparse
import fcntl
import json
from pathlib import Path
import re
import sqlite3
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from optimization_framework.campaigns.memory import content_key  # noqa: E402

IDENTITY = re.compile(rb"context_[0-9a-f]{16}(?![0-9a-f])")


def cited_identities(db, directory):
    """Revision ids mentioned anywhere outside the revisions themselves."""
    cited = set()
    tables = [row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")]
    for table in tables:
        columns = [row[1] for row in db.execute(f"PRAGMA table_info({table})")]
        if not columns:
            continue
        query = f"SELECT {', '.join(columns)} FROM {table}"
        if table == "records":
            query += " WHERE kind<>'context_revision'"
        elif table.startswith("manager_search"):
            continue
        for row in db.execute(query):
            for value in row:
                if isinstance(value, str) and "context_" in value:
                    cited.update(match.decode() for match in IDENTITY.findall(value.encode()))
    for path in directory.rglob("*"):
        if path.is_file() and "revisions" not in path.parts and path.suffix in {".json", ".jsonl", ".md", ".txt", ".log"}:
            try:
                cited.update(match.decode() for match in IDENTITY.findall(path.read_bytes()))
            except OSError:
                pass
    return cited


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("workspace", type=Path, help="Directory containing workspace.sqlite3")
    parser.add_argument("--apply", action="store_true", help="Delete; without this only report")
    parser.add_argument("--no-vacuum", action="store_true", help="Skip VACUUM after deleting")
    args = parser.parse_args()
    directory = args.workspace.resolve()
    lease = (directory / "service.lock").open("a+")
    try:
        fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        sys.exit("The workspace service is running; stop it first.")

    db = sqlite3.connect(directory / "workspace.sqlite3")
    current = {json.loads(data)["context_id"] for (data,) in
               db.execute("SELECT data FROM records WHERE kind='manager_state'")}
    cited = cited_identities(db, directory) | current
    removed, kept_numbers = [], set()
    for (campaign_id,) in db.execute("SELECT DISTINCT campaign_id FROM records WHERE kind='context_revision'").fetchall():
        previous = None
        rows = db.execute("SELECT id, data FROM records WHERE kind='context_revision' AND campaign_id=? ORDER BY rowid",
                          (campaign_id,))
        for identity, data in rows:
            revision = json.loads(data)
            key = content_key(revision) if "structured" in revision else identity
            if key != previous or identity in cited:
                kept_numbers.add((campaign_id, revision["revision"]))
            else:
                removed.append((identity, campaign_id, revision["revision"]))
            previous = key
    print(f"revisions: keep {len(kept_numbers)}, remove {len(removed)} ({len(cited)} ids cited elsewhere)")
    if not args.apply:
        print("Dry run; pass --apply to delete.")
        return

    with db:
        db.executemany("DELETE FROM records WHERE id=? AND kind='context_revision'", [(i,) for i, _, _ in removed])
    files = 0
    for _, campaign_id, number in removed:
        if (campaign_id, number) in kept_numbers:
            continue
        for suffix in (".md", ".json"):
            path = directory / "campaigns" / campaign_id / "manager" / "revisions" / f"{number:08d}{suffix}"
            if path.exists():
                path.unlink()
                files += 1
    print(f"deleted {len(removed)} rows and {files} projection files")
    if not args.no_vacuum:
        print("VACUUM (rewrites the database; may take several minutes)...", flush=True)
        db.execute("VACUUM")
    db.close()


if __name__ == "__main__":
    main()
