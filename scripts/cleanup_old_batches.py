"""Delete output folders (niches/*/out/<YYYY-MM-DD>/) older than RETENTION_DAYS.
Also vacuums admin.db post_status rows for the deleted dates.

Run via cron (weekly is enough):
    python -m scripts.cleanup_old_batches
"""
import datetime
import pathlib
import shutil
import sqlite3
import sys

RETENTION_DAYS = 30
ROOT = pathlib.Path(__file__).resolve().parent.parent  # fb-content-engine/


def is_date_dir(name: str) -> bool:
    try:
        datetime.date.fromisoformat(name)
        return True
    except ValueError:
        return False


def main():
    cutoff = (datetime.date.today() - datetime.timedelta(days=RETENTION_DAYS)).isoformat()
    print(f"[cleanup] removing batch folders dated before {cutoff} (retention {RETENTION_DAYS} days)")

    niches_dir = ROOT / "niches"
    if not niches_dir.exists():
        print("[cleanup] no niches/ directory; nothing to do")
        return

    deleted_dirs = 0
    deleted_dates_per_niche = {}  # niche → list[date]

    for niche_dir in niches_dir.iterdir():
        if not niche_dir.is_dir():
            continue
        out_dir = niche_dir / "out"
        if not out_dir.exists():
            continue
        for date_dir in out_dir.iterdir():
            if not date_dir.is_dir() or not is_date_dir(date_dir.name):
                continue
            if date_dir.name < cutoff:
                print(f"  - {niche_dir.name}/out/{date_dir.name}/")
                shutil.rmtree(date_dir)
                deleted_dirs += 1
                deleted_dates_per_niche.setdefault(niche_dir.name, []).append(date_dir.name)

    # Vacuum SQLite status rows for deleted (niche, date) pairs
    db_path = ROOT / "admin" / "admin.db"
    db_rows_deleted = 0
    if db_path.exists() and deleted_dates_per_niche:
        conn = sqlite3.connect(db_path)
        for niche, dates in deleted_dates_per_niche.items():
            for d in dates:
                cur = conn.execute("DELETE FROM post_status WHERE niche = ? AND target_date = ?", (niche, d))
                db_rows_deleted += cur.rowcount
        conn.commit()
        conn.close()

    print(f"[cleanup] deleted {deleted_dirs} batch folder(s), {db_rows_deleted} status row(s)")


if __name__ == "__main__":
    main()
