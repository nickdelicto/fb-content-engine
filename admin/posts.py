"""Reads generated post batches from niches/*/out/<date>/ and joins with
post-status tracking from admin.db."""
import datetime
import pathlib
from dataclasses import dataclass
from typing import Optional

import pandas as pd


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


# Recommended posting times for a 5-post day. Operator can use these as
# rough guidance — they're spread across the day.
RECOMMENDED_TIMES_5 = ["8:00 AM", "11:30 AM", "3:00 PM", "6:00 PM", "8:30 PM"]


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
        rec_time = RECOMMENDED_TIMES_5[i] if i < len(RECOMMENDED_TIMES_5) else None
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
    return datetime.date.today().isoformat()


def tomorrow_iso() -> str:
    return (datetime.date.today() + datetime.timedelta(days=1)).isoformat()
