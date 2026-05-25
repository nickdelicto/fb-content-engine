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


def kie_poll_task(task_id: str, timeout_sec: int = 300, interval_sec: int = 5) -> str:
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
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
        time.sleep(interval_sec)
    raise TimeoutError(f"Kie task {task_id} did not complete within {timeout_sec}s")


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

today = datetime.date.today().isoformat()
out_dir = niche_dir / "out" / today
(out_dir / "images").mkdir(parents=True, exist_ok=True)

# 3-day theme rotation state
state_path = niche_dir / "state.json"
state = json.loads(state_path.read_text()) if state_path.exists() else {"recent": []}
cutoff = (datetime.date.today() - datetime.timedelta(days=3)).isoformat()
recent_themes = sorted({e["theme"] for e in state["recent"] if e["date"] >= cutoff})

# --- Stage B: scrape competitors via Apify (with optional cache) ---
cache_dir = niche_dir / "cache"
cache_dir.mkdir(parents=True, exist_ok=True)
scrape_cache_path = cache_dir / "apify_raw.json"

if args.use_cached_scrape and scrape_cache_path.exists():
    cached = json.loads(scrape_cache_path.read_text())
    raw_posts = cached["posts"]
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

ranked = sorted(raw_posts, key=velocity, reverse=True)
top_posts = ranked[: competitors["scrape_settings"]["top_n_to_pass_to_prompt"]]
top_posts_slim = [slim_post(p) for p in top_posts]

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
    max_uses = brand.get("generation", {}).get("web_search_max_uses", 2)
    create_kwargs["tools"] = [
        {"type": "web_search_20260209", "name": "web_search", "max_uses": max_uses},
    ]

resp = client.messages.create(**create_kwargs)
# With tool use, resp.content may contain multiple blocks (text, tool_use, tool_result, text...).
# Take the LAST text block as the model's final answer.
text_blocks = [b for b in resp.content if getattr(b, "type", None) == "text"]
if not text_blocks:
    (out_dir / "raw_response.txt").write_text(json.dumps([b.model_dump() if hasattr(b, "model_dump") else str(b) for b in resp.content], indent=2))
    sys.exit("[generate] No text block in response. Saved raw content for inspection.")
raw_text = text_blocks[-1].text.strip()
# Always save raw response — cheap insurance for debugging schema drift / truncation
(out_dir / "raw_response.txt").write_text(raw_text)

# Extract just the JSON array, ignoring any markdown fencing OR post-array commentary
# the model adds (especially with tool use, where it often appends editorial notes).
first_bracket = raw_text.find("[")
last_bracket = raw_text.rfind("]")
if first_bracket == -1 or last_bracket == -1 or last_bracket < first_bracket:
    sys.exit(f"[generate] Could not find JSON array boundaries in response. Raw saved to {out_dir / 'raw_response.txt'}.")
json_text = raw_text[first_bracket : last_bracket + 1]

try:
    posts = json.loads(json_text)
except json.JSONDecodeError as e:
    sys.exit(f"[generate] JSON parse failed: {e}. Raw saved to {out_dir / 'raw_response.txt'}.")

# --- Stage D: images via Kie.ai (async task pattern) + package ---
img_cfg = brand["image_gen"]
print(f"[images] generating {len(posts)} via {img_cfg['model']}…", flush=True)
rows = []
warnings = []
for i, post in enumerate(posts):
    img_file = ""
    try:
        # Pass-through pattern: brand.image_gen.input is sent verbatim to Kie.
        # Keeps run_batch.py model-agnostic — only brand.yaml needs editing when swapping image models.
        task_id = kie_create_task(
            model=img_cfg["model"],
            input_payload={"prompt": post["image_prompt"], **img_cfg.get("input", {})},
        )
        img_url = kie_poll_task(task_id)
        img_path = out_dir / "images" / f"post_{i:02d}.png"
        fetch_and_save_image(img_url, img_path, strip_metadata=img_cfg.get("strip_metadata", True))
        img_file = img_path.name
    except (requests.RequestException, RuntimeError, TimeoutError, KeyError) as e:
        warnings.append(f"post_{i:02d}: image generation failed ({e})")

    rows.append({
        "post_id": f"post_{i:02d}",
        "theme": post["theme"],
        "hook_type": post["hook_type"],
        "image_file": img_file,
        "caption": post["caption"],
        "first_comment": post["first_comment"],
    })

# --- Write outputs ---
pd.DataFrame(rows).to_csv(out_dir / "batch.csv", index=False)
with open(out_dir / "batch.md", "w") as f:
    f.write(f"# Batch {today} — {args.niche}\n\n")
    for r in rows:
        f.write(f"## {r['post_id']} — {r['theme']} ({r['hook_type']})\n\n")
        if r["image_file"]:
            f.write(f"![image](images/{r['image_file']})\n\n")
        else:
            f.write("_(image failed — see warnings)_\n\n")
        f.write(f"**Caption:**\n\n{r['caption']}\n\n")
        f.write(f"**First comment:**\n\n{r['first_comment']}\n\n---\n\n")

# Persist theme rotation state (prune anything older than the 3-day window)
state["recent"] = [e for e in state["recent"] if e["date"] >= cutoff]
state["recent"].extend([{"date": today, "theme": r["theme"]} for r in rows])
state_path.write_text(json.dumps(state, indent=2))

print(f"\n[done] {len(rows)} posts → {out_dir}", flush=True)
if warnings:
    print(f"[warnings] {len(warnings)}:")
    for w in warnings:
        print(f"  - {w}")
