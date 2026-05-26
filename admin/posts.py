"""Reads generated post batches from niches/*/out/<date>/ and joins with
post-status tracking from admin.db."""
import datetime
import pathlib
import zoneinfo
from dataclasses import dataclass
from typing import Optional

import pandas as pd

# Dashboard "today" rolls over at midnight ET, not midnight UTC. VPS is on UTC,
# so without this the dashboard would flip to tomorrow at 8pm ET — bad UX for an
# operator still posting that day's content in the evening.
ET = zoneinfo.ZoneInfo("America/New_York")


@dataclass
class Post:
    niche: str
    target_date: str   # e.g., "2026-05-26"
    post_id: str       # e.g., "post_00"
    theme: str
    hook_type: str
    image_file: str    # relative filename, e.g., "post_00.png"
    image_url: str     # URL the dashboard serves the image at
    caption: str
    first_comment: str
    posted_at: Optional[str] = None  # ISO timestamp if marked as posted
    posted_by: Optional[str] = None
    recommended_time: Optional[str] = None  # e.g., "8:00 AM"


# Recommended posting times per day-of-week, matching the owner's posting strategy.
# Mon-Fri optimized for working-hours engagement; Sat morning; Sun afternoon/evening
# for reflective/personal-development content.
# Each day has 5 slots = the main daily quota. Posts beyond 5 (from stacked batches)
# show no recommendation — operator picks the timing.
# Keys: 0=Mon, 1=Tue, 2=Wed, 3=Thu, 4=Fri, 5=Sat, 6=Sun (Python date.weekday())
# ALL TIMES ARE EASTERN TIME (ET). Operator schedules in Meta Business Suite
# using these as ET reference points.
SCHEDULE_BY_DAY = {
    0: ["9:00 AM ET",  "10:00 AM ET", "3:00 PM ET",  "4:30 PM ET", "6:00 PM ET"],   # Monday:    9-10 AM + 3-6 PM
    1: ["10:00 AM ET", "3:00 PM ET",  "4:00 PM ET",  "5:00 PM ET", "6:00 PM ET"],   # Tuesday:   10 AM + 3-6 PM
    2: ["10:00 AM ET", "11:00 AM ET", "2:00 PM ET",  "4:00 PM ET", "6:00 PM ET"],   # Wednesday: 10-11 AM + 2-6 PM
    3: ["9:00 AM ET",  "3:00 PM ET",  "4:00 PM ET",  "5:00 PM ET", "6:00 PM ET"],   # Thursday:  9 AM + 3-6 PM
    4: ["9:00 AM ET",  "3:00 PM ET",  "4:00 PM ET",  "5:00 PM ET", "6:00 PM ET"],   # Friday:    9 AM + 3-6 PM
    5: ["9:00 AM ET",  "10:00 AM ET", "11:30 AM ET", "12:00 PM ET","1:00 PM ET"],   # Saturday:  9 AM - 1 PM
    6: ["3:00 PM ET",  "4:30 PM ET",  "6:00 PM ET",  "7:30 PM ET", "9:00 PM ET"],   # Sunday:    3-9 PM (reflective/personal dev)
}


def list_niches(root: pathlib.Path) -> list[str]:
    """Return list of niche names (folders under niches/) sorted alphabetically."""
    niches_dir = root / "niches"
    if not niches_dir.exists():
        return []
    return sorted([p.name for p in niches_dir.iterdir() if p.is_dir()])


def load_posts(root: pathlib.Path, niche: str, target_date: str, status_lookup: dict) -> list[Post]:
    """Read batch.csv for the given niche+date, join with status records, return Post list."""
    out_dir = root / "niches" / niche / "out" / target_date
    csv_path = out_dir / "batch.csv"
    if not csv_path.exists():
        return []
    df = pd.read_csv(csv_path)
    # Day-of-week schedule lookup — same for every post on this date
    try:
        day_of_week = datetime.date.fromisoformat(target_date).weekday()
        times_for_day = SCHEDULE_BY_DAY.get(day_of_week, [])
    except (ValueError, TypeError):
        times_for_day = []
    posts = []
    for i, row in df.iterrows():
        post_id = row["post_id"]
        img_file = str(row.get("image_file") or "").strip()
        # Fallback: if CSV missing image_file but file exists on disk, use the conventional name.
        # (Happens when the original image gen timed out and was recovered manually later.)
        if not img_file or img_file == "nan":
            candidate = out_dir / "images" / f"{post_id}.png"
            if candidate.exists():
                img_file = f"{post_id}.png"
            else:
                img_file = ""
        rec_time = times_for_day[i] if i < len(times_for_day) else None
        status = status_lookup.get((niche, target_date, post_id), {})
        posts.append(Post(
            niche=niche,
            target_date=target_date,
            post_id=post_id,
            theme=row["theme"],
            hook_type=row["hook_type"],
            image_file=img_file,
            image_url=f"/image/{niche}/{target_date}/{img_file}" if img_file else "",
            caption=row["caption"],
            first_comment=row["first_comment"],
            posted_at=status.get("posted_at"),
            posted_by=status.get("posted_by"),
            recommended_time=rec_time,
        ))
    return posts


def today_iso() -> str:
    return datetime.datetime.now(ET).date().isoformat()


def tomorrow_iso() -> str:
    return (datetime.datetime.now(ET).date() + datetime.timedelta(days=1)).isoformat()
