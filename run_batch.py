"""
FB Content Engine — batch orchestrator.

Usage:
    python run_batch.py --niche kenji-mori-retirement
    python run_batch.py --niche kenji-mori-retirement --count 10

Each niche is a self-contained folder under niches/ with its own
brand.yaml, competitors.yaml, themes.yaml, prompts/system.md, and
runtime state/output dirs.
"""
import argparse
import datetime
import json
import os
import pathlib
import sys
import time
from io import BytesIO

import pandas as pd
import requests
import yaml
from anthropic import Anthropic
from apify_client import ApifyClient
from dotenv import load_dotenv
from PIL import Image

KIE_BASE = "https://api.kie.ai/api/v1/jobs"

# Cost-tracking constants. Update when models or pricing change.
APIFY_FB_POSTS_USD_PER_1000 = 2.00
ANTHROPIC_WEB_SEARCH_USD = 0.01  # per search (= $10/1000)
ANTHROPIC_PRICING = {
    # USD per million tokens
    "claude-sonnet-4-6": {"input": 3.00, "output": 15.00, "cache_read": 0.30, "cache_write": 3.75},
    "claude-opus-4-7":   {"input": 5.00, "output": 25.00, "cache_read": 0.50, "cache_write": 6.25},
    "claude-haiku-4-5":  {"input": 1.00, "output": 5.00,  "cache_read": 0.10, "cache_write": 1.25},
}


COST_COLS = [
    "timestamp", "niche", "scrape_used_cache", "apify_posts", "apify_usd",
    "anthropic_model", "anthropic_input_tokens", "anthropic_output_tokens",
    "anthropic_cache_read_tokens", "anthropic_web_searches", "anthropic_usd",
    "kie_images", "kie_usd", "total_usd", "notes",
]


def log_cost(root: pathlib.Path, row: dict) -> None:
    """Append the row to BOTH cost_log.csv (simple tail-able log) AND admin.db
    cost_log table (queryable from SQL, also feeds the daily summary email and
    future dashboard cost view)."""
    # CSV — human-readable, easy to grep
    log_path = root / "cost_log.csv"
    write_header = not log_path.exists()
    with open(log_path, "a") as f:
        if write_header:
            f.write(",".join(COST_COLS) + "\n")
        f.write(",".join(str(row.get(c, "")) for c in COST_COLS) + "\n")

    # SQLite — queryable
    import sqlite3
    db_path = root / "admin" / "admin.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS cost_log (
            timestamp TEXT NOT NULL,
            niche TEXT NOT NULL,
            scrape_used_cache TEXT,
            apify_posts INTEGER,
            apify_usd REAL,
            anthropic_model TEXT,
            anthropic_input_tokens INTEGER,
            anthropic_output_tokens INTEGER,
            anthropic_cache_read_tokens INTEGER,
            anthropic_web_searches INTEGER,
            anthropic_usd REAL,
            kie_images INTEGER,
            kie_usd REAL,
            total_usd REAL,
            notes TEXT
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_cost_log_timestamp ON cost_log(timestamp)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_cost_log_niche ON cost_log(niche)")
    conn.execute(
        f"INSERT INTO cost_log ({', '.join(COST_COLS)}) VALUES ({', '.join('?' * len(COST_COLS))})",
        tuple(row.get(c, "") for c in COST_COLS),
    )
    conn.commit()
    conn.close()


def calc_anthropic_cost(model: str, resp) -> tuple[int, int, int, int, float]:
    """Returns (input_toks, output_toks, cache_read_toks, web_searches, total_usd).

    Note on web_searches: as of 2026, Anthropic's `usage.server_tool_use.web_search_requests`
    counter is unreliable (often returns 0 even when searches happened). We count
    `server_tool_use` blocks directly from response.content instead — those are 1:1
    with actual tool invocations. Web search results are already included in
    input_tokens (which IS accurate) so we only charge the $0.01-per-search fee
    on top of that.
    """
    rates = ANTHROPIC_PRICING.get(model, ANTHROPIC_PRICING["claude-sonnet-4-6"])
    usage = resp.usage
    input_t = getattr(usage, "input_tokens", 0) or 0
    output_t = getattr(usage, "output_tokens", 0) or 0
    cache_read = getattr(usage, "cache_read_input_tokens", 0) or 0
    cache_write = getattr(usage, "cache_creation_input_tokens", 0) or 0
    # Count actual server_tool_use blocks (search invocations) for accurate fee
    web_searches = sum(1 for b in resp.content if getattr(b, "type", None) == "server_tool_use")
    cost = (
        input_t * rates["input"] / 1_000_000
        + output_t * rates["output"] / 1_000_000
        + cache_read * rates["cache_read"] / 1_000_000
        + cache_write * rates["cache_write"] / 1_000_000
        + web_searches * ANTHROPIC_WEB_SEARCH_USD
    )
    return input_t, output_t, cache_read, web_searches, cost


def kie_create_task(model: str, input_payload: dict) -> str:
    resp = requests.post(
        f"{KIE_BASE}/createTask",
        headers={
            "Authorization": f"Bearer {os.environ['KIE_API_KEY']}",
            "Content-Type": "application/json",
        },
        json={"model": model, "input": input_payload},
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    if data.get("code") != 200:
        raise RuntimeError(f"Kie createTask failed: {data}")
    return data["data"]["taskId"]


def kie_poll_task(task_id: str, timeout_sec: int = 1800) -> str:
    """Poll Kie until task reports success or fail. Adaptive interval: 5s for first
    minute (most tasks finish here), 10s for next 4 min, 30s after. 30-min hard cap
    (GPT Image 2 can occasionally take 15-25 min on complex prompts during high Kie load).
    On timeout we don't assume failure — the task may still complete on Kie's end;
    error message tells you how to query it directly via the recovery URL.
    """
    deadline = time.time() + timeout_sec
    started = time.time()
    while time.time() < deadline:
        elapsed = time.time() - started
        if elapsed < 60:
            interval = 5
        elif elapsed < 300:
            interval = 10
        else:
            interval = 30
        resp = requests.get(
            f"{KIE_BASE}/recordInfo",
            params={"taskId": task_id},
            headers={"Authorization": f"Bearer {os.environ['KIE_API_KEY']}"},
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json().get("data", {}) or {}
        state = data.get("state")
        if state == "success":
            urls = data.get("resultUrls") or []
            if not urls and data.get("resultJson"):
                try:
                    urls = json.loads(data["resultJson"]).get("resultUrls", []) or []
                except json.JSONDecodeError:
                    pass
            if not urls:
                raise RuntimeError(f"Kie task succeeded but returned no URLs: {data}")
            return urls[0]
        if state == "fail":
            raise RuntimeError(f"Kie task failed: {data.get('failMsg') or data}")
        time.sleep(interval)
    raise TimeoutError(
        f"Kie task {task_id} did not complete within {timeout_sec}s of polling. "
        f"The task may still be running on Kie's end. Query directly: "
        f"GET https://api.kie.ai/api/v1/jobs/recordInfo?taskId={task_id}"
    )


def fetch_and_save_image(url: str, out_path: pathlib.Path, strip_metadata: bool) -> None:
    raw = requests.get(url, timeout=60).content
    if not strip_metadata:
        out_path.write_bytes(raw)
        return
    # Re-export through PIL: drops EXIF, ICC, and unknown PNG chunks (incl. C2PA
    # iTXt manifests). Avoids FB's metadata-based "Made with AI" labeling.
    img = Image.open(BytesIO(raw))
    if img.mode not in ("RGB", "RGBA"):
        img = img.convert("RGB")
    img.save(out_path, format="PNG", optimize=True)

ROOT = pathlib.Path(__file__).parent
load_dotenv(ROOT / ".env")


def _on_uncaught_exception(exc_type, exc_value, exc_tb):
    """Send a failure alert on any uncaught error, then re-raise so the original
    traceback still surfaces. Skips KeyboardInterrupt and clean exit(0)."""
    import traceback as _tb
    if exc_type is KeyboardInterrupt or (exc_type is SystemExit and (exc_value.code == 0 or exc_value.code is None)):
        sys.__excepthook__(exc_type, exc_value, exc_tb)
        return
    tb_str = "".join(_tb.format_exception(exc_type, exc_value, exc_tb))
    try:
        from notify import send_failure_alert
        niche_name = globals().get("args").niche if globals().get("args") else "unknown"
        # Truncate to avoid massive email bodies
        send_failure_alert(stage="batch", error_text=tb_str[-3000:], niche=niche_name)
    except Exception:
        pass  # never let the alert send fail mask the original error
    sys.__excepthook__(exc_type, exc_value, exc_tb)


sys.excepthook = _on_uncaught_exception

parser = argparse.ArgumentParser(description="Generate a batch of FB posts for a niche.")
parser.add_argument("--niche", required=True, help="Niche folder name under niches/")
parser.add_argument("--count", type=int, default=None,
                    help="Number of posts (defaults to brand.cadence.default_batch_size)")
parser.add_argument("--use-cached-scrape", action="store_true",
                    help="Reuse the last saved Apify scrape from niches/<niche>/cache/apify_raw.json instead of calling Apify. Saves ~$0.37/run when iterating on Anthropic/Kie logic. Falls through to live scrape if no cache exists.")
args = parser.parse_args()

niche_dir = ROOT / "niches" / args.niche
if not niche_dir.is_dir():
    sys.exit(f"Niche not found: {niche_dir}")

brand = yaml.safe_load((niche_dir / "brand.yaml").read_text())
competitors = yaml.safe_load((niche_dir / "competitors.yaml").read_text())
themes = yaml.safe_load((niche_dir / "themes.yaml").read_text())
system_prompt = (niche_dir / "prompts" / "system.md").read_text()

count = args.count or brand["cadence"]["default_batch_size"]

# Output is dated to the day the post is FOR (publish date), N days ahead of generation.
# Default lead time is 2 days so the operator always has a full "tomorrow" buffer
# in the dashboard even if a batch fails or needs a rerun. Override via brand.yaml
# `generation.lead_time_days` if a niche wants a different cadence.
generation_date = datetime.date.today()
lead_days = int(brand.get("generation", {}).get("lead_time_days", 2))
target_date_obj = generation_date + datetime.timedelta(days=lead_days)
target_date = target_date_obj.isoformat()
out_dir = niche_dir / "out" / target_date
(out_dir / "images").mkdir(parents=True, exist_ok=True)

# 3-day theme rotation state — rotate against the 3 days BEFORE the target publish date.
state_path = niche_dir / "state.json"
state = json.loads(state_path.read_text()) if state_path.exists() else {"recent": []}
cutoff = (target_date_obj - datetime.timedelta(days=3)).isoformat()
recent_themes = sorted({e["theme"] for e in state["recent"] if e["date"] >= cutoff})

# --- Stage B: scrape competitors via Apify (with optional cache) ---
cache_dir = niche_dir / "cache"
cache_dir.mkdir(parents=True, exist_ok=True)
scrape_cache_path = cache_dir / "apify_raw.json"

# Cost tracking counters (used at end to write cost_log.csv row)
scrape_used_cache = False
apify_posts_scraped = 0

if args.use_cached_scrape and scrape_cache_path.exists():
    cached = json.loads(scrape_cache_path.read_text())
    raw_posts = cached["posts"]
    scrape_used_cache = True
    print(f"[cache] using scrape from {cached['scraped_at']} ({len(raw_posts)} posts) — saved $0.37 Apify spend", flush=True)
else:
    if args.use_cached_scrape:
        print(f"[cache] no cache at {scrape_cache_path}; falling through to live scrape", flush=True)
    print(f"[scrape] {len(competitors['pages'])} competitors via Apify…", flush=True)
    apify = ApifyClient(os.environ["APIFY_TOKEN"])
    run = apify.actor("apify/facebook-posts-scraper").call(
        run_input={
            "startUrls": [{"url": f"https://facebook.com/{p['handle']}"} for p in competitors["pages"]],
            "resultsLimit": competitors["scrape_settings"]["per_page_post_count"],
        }
    )
    raw_posts = list(apify.dataset(run.default_dataset_id).iterate_items())
    apify_posts_scraped = len(raw_posts)
    print(f"[scrape] got {len(raw_posts)} posts", flush=True)
    # Save to cache so future runs can use --use-cached-scrape
    scrape_cache_path.write_text(json.dumps({
        "scraped_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "competitor_count": len(competitors["pages"]),
        "post_count": len(raw_posts),
        "posts": raw_posts,
    }, indent=2, default=str))

def velocity(p):
    hours = max(p.get("hours_since_post", 1), 1)
    return (p.get("likes", 0) + 2 * p.get("comments", 0) + 3 * p.get("shares", 0)) / hours


def slim_post(p: dict) -> dict:
    """Strip Apify post to just what the model needs. Drops massive 'media' field
    (24k+ chars of image/video metadata), reaction breakdowns, ad library refs, etc.
    Reduces per-post token count from ~6000 → ~125 — critical for staying under
    Anthropic's input-tokens-per-minute rate limit."""
    user = p.get("user") if isinstance(p.get("user"), dict) else {}
    return {
        "text": (p.get("text") or p.get("postText") or p.get("message") or "")[:1500],
        "page": p.get("pageName") or user.get("name") or "",
        "likes": p.get("likes", 0),
        "comments": p.get("comments", 0),
        "shares": p.get("shares", 0),
        "post_id": p.get("postId") or "",
    }

# Diversified ranking: top N PER competitor (not top N overall).
# Pre-2026-05-25 bug: ranking by velocity globally let the highest-audience page
# monopolize all source slots (Dave Ramsey took 15/15). Per-competitor ranking
# guarantees every page contributes its best posts to the source pool.
posts_by_page = {}
for p in raw_posts:
    user_obj = p.get("user") if isinstance(p.get("user"), dict) else {}
    page = p.get("pageName") or user_obj.get("name") or "unknown"
    posts_by_page.setdefault(page, []).append(p)

per_competitor = competitors["scrape_settings"].get("top_n_per_competitor", 5)
overall_cap = competitors["scrape_settings"].get("top_n_overall_cap", 25)
top_posts = []
for page, page_posts in posts_by_page.items():
    top_posts.extend(sorted(page_posts, key=velocity, reverse=True)[:per_competitor])
top_posts = sorted(top_posts, key=velocity, reverse=True)[:overall_cap]
top_posts_slim = [slim_post(p) for p in top_posts]
print(f"[rank] {len(posts_by_page)} competitors → top {per_competitor} each → {len(top_posts)} posts to model", flush=True)

# --- Stage C: generate via Anthropic ---
print(f"[generate] {count} posts via Anthropic…", flush=True)
client = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
user_msg = json.dumps({
    "N": count,
    "top_posts": top_posts_slim,
    "recent_themes": recent_themes,
    "themes_available": themes["themes"],
    "brand": brand,
})

create_kwargs = {
    "model": "claude-sonnet-4-6",
    "max_tokens": 8000,  # ~600 tokens per post × 5 posts = 3k; 8k gives 2x headroom. Right-sized for the single-caption schema. Bumping higher reserves capacity against Anthropic's 30k input-tokens/min Tier 1 rate limit.
    "system": system_prompt,
    "messages": [{"role": "user", "content": user_msg}],
}
# Web search is conditional on Anthropic tier. On Tier 1 (30k input tokens/min) the
# web_search tool's content payloads push us over the rate limit. Re-enable once on
# Tier 2 ($40 cumulative API spend) by setting brand.generation.web_search_enabled: true.
if brand.get("generation", {}).get("web_search_enabled"):
    # Scale max_uses dynamically by post count instead of using a flat ceiling.
    # Default: 2 searches per post. So a 1-post batch caps at 2, 5-post at 10.
    # Prevents the model from "going wild" with 7 searches on a single post.
    per_post = brand.get("generation", {}).get("web_search_per_post", 2)
    max_uses = max(2, count * per_post)
    create_kwargs["tools"] = [
        {"type": "web_search_20260209", "name": "web_search", "max_uses": max_uses},
    ]

resp = client.messages.create(**create_kwargs)
# INSURANCE: dump the FULL response (every block: text, tool_use, tool_result) to disk
# BEFORE we try to extract anything. If extraction fails for any reason, the actual
# generated content stays recoverable from this file. Costs nothing (we already paid
# Anthropic for the response); saves the batch when our extractor has bugs.
try:
    full_response_dump = {
        "id": resp.id,
        "model": resp.model,
        "stop_reason": resp.stop_reason,
        "usage": resp.usage.model_dump() if hasattr(resp.usage, "model_dump") else dict(resp.usage),
        "content": [b.model_dump() if hasattr(b, "model_dump") else str(b) for b in resp.content],
    }
    (out_dir / "anthropic_full_response.json").write_text(json.dumps(full_response_dump, indent=2, default=str))
except Exception as _dump_err:
    # Don't let dump failure derail the batch — but log it
    print(f"[warn] could not dump full response: {_dump_err}", flush=True)

# Cost tracking — capture token usage from this response.
ANTHROPIC_MODEL = create_kwargs["model"]
a_input, a_output, a_cache_read, a_websearch, anthropic_usd = calc_anthropic_cost(ANTHROPIC_MODEL, resp)
# With tool use (web_search), resp.content has multiple blocks interleaved:
# [text, server_tool_use, web_search_tool_result, text, server_tool_use, ..., text].
# The JSON array can live in ANY text block (often the first), not necessarily the
# last — the last is frequently editorial commentary the model adds after the
# search loop completes. Iterate through all text blocks and find the one whose
# contents parse as a JSON list of dicts (= our posts schema).
text_blocks = [b for b in resp.content if getattr(b, "type", None) == "text"]
if not text_blocks:
    (out_dir / "raw_response.txt").write_text(json.dumps([b.model_dump() if hasattr(b, "model_dump") else str(b) for b in resp.content], indent=2))
    sys.exit("[generate] No text block in response. Saved raw content for inspection.")

# Save full concatenation for debugging (so raw_response.txt isn't just the last block)
full_raw = "\n\n---BLOCK---\n\n".join(b.text for b in text_blocks)
(out_dir / "raw_response.txt").write_text(full_raw)

posts = None
json_text = None
for block in text_blocks:
    candidate_text = block.text.strip()
    # Strip leading ```json fence if present
    if candidate_text.startswith("```"):
        candidate_text = candidate_text.lstrip("`").lstrip()
        if candidate_text.lower().startswith("json"):
            candidate_text = candidate_text[4:].lstrip()
        if "```" in candidate_text:
            candidate_text = candidate_text[:candidate_text.index("```")].rstrip()
    first_bracket = candidate_text.find("[")
    last_bracket = candidate_text.rfind("]")
    if first_bracket == -1 or last_bracket == -1 or last_bracket < first_bracket:
        continue
    candidate = candidate_text[first_bracket: last_bracket + 1]
    try:
        loaded = json.loads(candidate)
        if isinstance(loaded, list) and loaded and isinstance(loaded[0], dict) and "caption" in loaded[0]:
            posts = loaded
            json_text = candidate
            break
    except json.JSONDecodeError:
        continue

if posts is None:
    sys.exit(f"[generate] No text block contained a parseable JSON array of posts. Raw saved to {out_dir / 'raw_response.txt'}.")


def strip_em_dashes(text: str) -> str:
    """Bulletproof em-dash removal. The model keeps slipping them in despite the
    prompt ban. Replace ' — ' (space-em-space, the most common form) with ', '.
    Then replace any remaining bare em dash with ', '. Also strips en dashes."""
    if not isinstance(text, str):
        return text
    # Em-dash (U+2014) and en-dash (U+2013)
    text = text.replace(" — ", ", ").replace("—", ",").replace(" – ", ", ").replace("–", ",")
    return text

# Apply em-dash strip to every caption + first_comment before downstream use.
# This guarantees the published output is em-dash-free regardless of model behavior.
for p in posts:
    for f in ("caption", "first_comment"):
        if f in p:
            p[f] = strip_em_dashes(p[f])

# --- Stage D: images via Kie.ai (async task pattern) + package ---
# APPEND MODE: if batch.csv already exists for this target_date (e.g., a previous
# manual run + tonight's cron), CONTINUE the post numbering instead of overwriting.
# Dashboard reads all rows in batch.csv and shows them — so multiple runs on the
# same date stack into one visible list. Operator can publish all of them.
img_cfg = brand["image_gen"]
KIE_COST_PER_IMAGE = float(img_cfg.get("cost_per_image", 0.03))  # default to GPT Image 2 1K rate

existing_csv = out_dir / "batch.csv"
existing_count = 0
if existing_csv.exists():
    try:
        existing_df = pd.read_csv(existing_csv)
        existing_count = len(existing_df)
        print(f"[append] batch.csv exists with {existing_count} posts — new posts will start at post_{existing_count:02d}", flush=True)
    except Exception:
        existing_count = 0  # fall back to write-fresh if read fails

print(f"[images] generating {len(posts)} via {img_cfg['model']}…", flush=True)
rows = []
warnings = []
kie_images_generated = 0
for i, post in enumerate(posts):
    post_num = existing_count + i  # continue numbering from existing
    post_id = f"post_{post_num:02d}"
    img_file = ""
    try:
        task_id = kie_create_task(
            model=img_cfg["model"],
            input_payload={"prompt": post["image_prompt"], **img_cfg.get("input", {})},
        )
        img_url = kie_poll_task(task_id)
        img_path = out_dir / "images" / f"{post_id}.png"
        fetch_and_save_image(img_url, img_path, strip_metadata=img_cfg.get("strip_metadata", True))
        img_file = img_path.name
        kie_images_generated += 1
    except (requests.RequestException, RuntimeError, TimeoutError, KeyError) as e:
        warnings.append(f"{post_id}: image generation failed ({e})")

    rows.append({
        "post_id": post_id,
        "theme": post["theme"],
        "hook_type": post["hook_type"],
        "image_file": img_file,
        "caption": post["caption"],
        "first_comment": post["first_comment"],
    })

# --- Write outputs (APPEND if existing) ---
csv_mode = "a" if existing_count > 0 else "w"
write_header = existing_count == 0
pd.DataFrame(rows).to_csv(existing_csv, mode=csv_mode, header=write_header, index=False)

md_mode = "a" if (out_dir / "batch.md").exists() and existing_count > 0 else "w"
with open(out_dir / "batch.md", md_mode) as f:
    if md_mode == "w":
        f.write(f"# Posts for {target_date} — {args.niche}\n\n")
    f.write(f"## Batch added {generation_date.isoformat()} ({len(rows)} post{'s' if len(rows) != 1 else ''})\n\n")
    for r in rows:
        f.write(f"### {r['post_id']} — {r['theme']} ({r['hook_type']})\n\n")
        if r["image_file"]:
            f.write(f"![image](images/{r['image_file']})\n\n")
        else:
            f.write("_(image failed — see warnings)_\n\n")
        f.write(f"**Caption:**\n\n{r['caption']}\n\n")
        f.write(f"**First comment:**\n\n{r['first_comment']}\n\n---\n\n")

# Persist theme rotation state (prune anything older than the 3-day window)
state["recent"] = [e for e in state["recent"] if e["date"] >= cutoff]
state["recent"].extend([{"date": target_date, "theme": r["theme"]} for r in rows])
state_path.write_text(json.dumps(state, indent=2))

# --- Cost tracking: write one row to cost_log.csv ---
apify_usd = (apify_posts_scraped / 1000.0) * APIFY_FB_POSTS_USD_PER_1000
kie_usd = kie_images_generated * KIE_COST_PER_IMAGE
total_usd = apify_usd + anthropic_usd + kie_usd
log_cost(ROOT, {
    "timestamp": datetime.datetime.now().isoformat(timespec="seconds"),
    "niche": args.niche,
    "scrape_used_cache": "yes" if scrape_used_cache else "no",
    "apify_posts": apify_posts_scraped,
    "apify_usd": f"{apify_usd:.4f}",
    "anthropic_model": ANTHROPIC_MODEL,
    "anthropic_input_tokens": a_input,
    "anthropic_output_tokens": a_output,
    "anthropic_cache_read_tokens": a_cache_read,
    "anthropic_web_searches": a_websearch,
    "anthropic_usd": f"{anthropic_usd:.4f}",
    "kie_images": kie_images_generated,
    "kie_usd": f"{kie_usd:.4f}",
    "total_usd": f"{total_usd:.4f}",
    "notes": f"warnings={len(warnings)}" if warnings else "",
})

print(f"\n[done] {len(rows)} posts scheduled for {target_date} → {out_dir}", flush=True)
print(f"[cost] this batch: ${total_usd:.4f}  (apify ${apify_usd:.4f} | anthropic ${anthropic_usd:.4f} | kie ${kie_usd:.4f})", flush=True)
if warnings:
    print(f"[warnings] {len(warnings)}:")
    for w in warnings:
        print(f"  - {w}")
