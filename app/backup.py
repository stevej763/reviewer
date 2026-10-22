"""Encrypted backups: export the stored rows as they are, import them with the key that made them.

A backup holds the same ciphertext as the database, so it is no more readable than the database
itself. Importing decrypts each value with the backup's key and re-encrypts it with this app's
key, so a backup from the server can be loaded into the Mac app (or the other way round).

A backup also carries the source app's wrapped data key (see vault.py), so that app's startup
password or recovery key opens it. (Version 1 backups, from before startup passwords, needed the
raw key and are no longer accepted.)
"""

import json
from datetime import date, datetime, timezone

from .appconfig import KEY as CONFIG_KEY
from .crypto import Box
from .db import Store
from .jobs import day_entries
from .vault import WrongSecret, open_keyring

FORMAT = "brain-dump-backup"
VERSION = 2
KEY_CHECK = "brain-dump"

# Columns copied from a backup; anything else in the file is ignored.
COLUMNS = {
    "entries": ("id", "entry_date", "created_at", "updated_at", "raw_text", "summary", "provider", "model",
                "status", "error", "wanted_provider"),
    "day_summaries": ("entry_date", "updated_at", "status", "error", "summary", "fingerprint", "entry_count",
                      "provider", "model"),
    "reports": ("id", "created_at", "start_date", "end_date", "style", "title", "content", "provider", "model"),
    "settings": ("key", "value"),
}
ENCRYPTED = {"raw_text", "summary", "title", "content", "value"}


class BackupError(ValueError):
    pass


def export_backup(store: Store, keyring: dict) -> dict:
    tables = {}
    for table in COLUMNS:
        rows = []
        for row in store.raw_rows(table):
            rows.append({k: (v.decode("ascii") if isinstance(v, bytes) else v) for k, v in row.items()})
        tables[table] = rows
    return {
        "format": FORMAT,
        "version": VERSION,
        "exported_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "key_check": store.box.seal(KEY_CHECK).decode("ascii"),
        "keyring": keyring,
        **tables,
    }


def _parse(raw: bytes) -> dict:
    try:
        data = json.loads(raw)
    except (ValueError, UnicodeDecodeError) as exc:
        raise BackupError("That file isn't a Brain Dump backup.") from exc
    if not isinstance(data, dict) or data.get("format") != FORMAT:
        raise BackupError("That file isn't a Brain Dump backup.")
    if data.get("version") == 1:
        raise BackupError("This backup was made before startup passwords and can't be loaded any more.")
    if data.get("version") != VERSION or not isinstance(data.get("keyring"), dict):
        raise BackupError("This backup was made by a different version of Brain Dump.")
    return data


def _rows(data: dict, table: str) -> list[dict]:
    rows = data.get(table) or []
    if not isinstance(rows, list) or not all(isinstance(r, dict) for r in rows):
        raise BackupError("The backup file is damaged.")
    return [{c: r.get(c) for c in COLUMNS[table]} for r in rows]


def _summary(text: str | None) -> str | None:
    """A summary is stored as JSON text; keep it as that text, or drop it if it isn't an object."""
    if not text:
        return None
    value = json.loads(text)
    return text if isinstance(value, dict) else None


def _source_box(data: dict, key: str, own_key: str) -> Box:
    """The Box that opens this backup. `key` is the source app's startup password or recovery
    key, as typed; blank means the backup came from this app."""
    secret = own_key
    if key:
        try:
            secret = open_keyring(data["keyring"], key)
        except WrongSecret:
            raise BackupError("That password or recovery key doesn't unlock this backup.") from None
    box = Box(secret)
    try:
        if box.open(data["key_check"].encode()) == KEY_CHECK:
            return box
    except (KeyError, AttributeError, RuntimeError):
        pass
    if not key:
        raise BackupError("This backup came from another copy of Brain Dump. Enter that app's startup "
                          "password or recovery key.")
    raise BackupError("The backup file is damaged.")


def import_backup(store: Store, raw: bytes, key: str, own_key: str) -> dict:
    """Add what's in the backup that this app doesn't have yet. Nothing here is overwritten."""
    data = _parse(raw)
    source = _source_box(data, key, own_key)

    def reveal(value):
        return source.open(value.encode()) if value else None

    def reseal(value):
        return store.box.seal(value) if value is not None else None

    try:
        entries = _rows(data, "entries")
        days = _rows(data, "day_summaries")
        reports = _rows(data, "reports")
        settings = _rows(data, "settings")
        for row in entries + days:
            date.fromisoformat(row["entry_date"])
        for row in reports:
            date.fromisoformat(row["start_date"]), date.fromisoformat(row["end_date"])
        for row in entries:
            row["raw_text"] = reveal(row["raw_text"]) or ""
            row["summary"] = _summary(reveal(row["summary"]))
        for row in days:
            row["summary"] = _summary(reveal(row["summary"]))
        for row in reports:
            row["title"], row["content"] = reveal(row["title"]) or "", reveal(row["content"]) or ""
        backup_config = next((json.loads(reveal(r["value"])) for r in settings if r["key"] == CONFIG_KEY), {})
    except (RuntimeError, AttributeError, TypeError, ValueError) as exc:
        raise BackupError("The backup file is damaged.") from exc

    counts = {"entries": 0, "days": 0, "reports": 0, "skipped": 0}

    # Entries. Recordings that were never transcribed have no text (their audio isn't in a
    # backup), so they're left out.
    existing = store.list_entries()
    have = {(e["date"], e["created_at"], e["text"]) for e in existing}
    dates_before = {e["date"] for e in existing}
    source_ids: dict[str, list] = {}  # date -> [[id, updated_at], ...] of finished entries, as the source saw them
    new_entries = []
    for row in sorted(entries, key=lambda r: r["id"] if isinstance(r["id"], int) else 0):
        if row["status"] == "done" and row["raw_text"].strip():
            source_ids.setdefault(row["entry_date"], []).append([row["id"], row["updated_at"]])
        if not row["raw_text"].strip():
            counts["skipped"] += 1
            continue
        if (row["entry_date"], row["created_at"], row["raw_text"]) in have:
            continue
        if row["status"] in ("summarising", "transcribing", "recording"):
            row["status"], row["error"] = "failed", "Highlights hadn't finished when the backup was made."
        new_entries.append({**row, "id": None, "raw_text": reseal(row["raw_text"]),
                            "summary": reseal(row["summary"])})
    store.insert_rows("entries", [{k: v for k, v in r.items() if k != "id"} for r in new_entries])
    counts["entries"] = len(new_entries)

    # End-of-day summaries, for days that don't have one here. A summary that was up to date in
    # the backup stays up to date if those entries are the only ones for that day here.
    have_days = {d["date"] for d in store.list_days()}
    new_days = []
    for row in days:
        if row["entry_date"] in have_days or not row["summary"]:
            continue
        day = row["entry_date"]
        was_current = row["fingerprint"] == json.dumps(source_ids.get(day, []))
        if was_current and day not in dates_before:
            row["fingerprint"] = day_entries(store, day)[1]
        if row["status"] != "done":
            row["status"], row["error"] = "done", None
        new_days.append({**row, "summary": reseal(row["summary"])})
    store.insert_rows("day_summaries", new_days)
    counts["days"] = len(new_days)

    have_reports = {(r["created_at"], r["start"], r["end"], r["style"], r["title"]) for r in store.list_reports()}
    new_reports = []
    for row in reports:
        if (row["created_at"], row["start_date"], row["end_date"], row["style"], row["title"]) in have_reports:
            continue
        new_reports.append({**row, "title": reseal(row["title"]), "content": reseal(row["content"])})
    store.insert_rows("reports", [{k: v for k, v in r.items() if k != "id"} for r in new_reports])
    counts["reports"] = len(new_reports)

    # The caller decides what to do with "About you". The PIN and AI settings never come across.
    profile = backup_config.get("profile") if isinstance(backup_config, dict) else None
    return {**counts, "profile": profile if isinstance(profile, dict) else None}
