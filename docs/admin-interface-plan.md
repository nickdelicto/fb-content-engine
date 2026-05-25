# Admin Interface Plan (parked, not yet built)

Discussion summary from 2026-05-24 session. To revisit after the immediate content-quality issues are resolved.

## Goal

A simple web dashboard (or path on existing setformoney.com) where a hired operator sees today's 5 posts, copies what they need into Meta Business Suite, and marks each as posted. Auto-cleanup of old folders to prevent disk bloat.

## User's vision (verbatim concept)

> Once a post is marked as Done or checked off it literally crosses it over, so it's [a] post management platform to show only we need 5 done and their recommended times to do it. Once posted, [operator] checks it off and it appears struck off in a very intuitive way. The image is also there in its original format ready to grab. Then once crossed off and 30 days later we have a script that completely clears or deletes this [old] image to avoid killing our memory.

## Comparison vs simpler alternatives

| Method | Operator UX | Setup effort | Status tracking | Auto-cleanup |
|---|---|---|---|---|
| **Drive sync (rclone)** | Open Drive folder | ~30 min | ✗ | Manual |
| **Subdomain admin UI** | Visit `fb.setformoney.com/admin` | ~4-6 hours | ✓ | Built-in cron |
| **Path on setformoney.com (`/fb-admin`)** | Visit existing site | ~4-6 hours | ✓ | Built-in cron |
| **Email/Slack digest** | Inbox notification | Medium | Limited | Standard |

## Recommended approach

**Separate tiny Express app on a subdomain** (e.g., `fb.setformoney.com/admin`):

- Keeps fb-content-engine fully self-contained — doesn't touch home-budget-app codebase
- Reuses the Hetzner VPS we already pay for
- Lives on a subdomain via one Cloudflare DNS record + nginx vhost
- ~300 lines of code total
- Backend: reads from `niches/*/out/<date>/` folders directly, SQLite for "done" status tracking
- Frontend: simple HTML + minimal JS for the strikethrough behavior
- Auth: basic auth (single shared password) — appropriate for a single operator

## What the operator sees (sketch)

```
Today — 2026-05-25 — 5 posts to schedule

┌─────────────────────────────────────────────┐
│ □ Post 1 (recommended: 8:00 AM)             │
│   [image preview]                            │
│   Caption: [click to copy]                   │
│   First comment: [click to copy]             │
└─────────────────────────────────────────────┘
┌─────────────────────────────────────────────┐
│ □ Post 2 (recommended: 11:30 AM)            │
│   ...                                        │
└─────────────────────────────────────────────┘
...

(struck-through items move to bottom or grey out)
```

## Behavior details

- Each post has a checkbox. Click → strikethrough + grey-out + sink to bottom
- Status persists in SQLite (so refresh keeps state)
- "Recommended times" — simple ladder: 8am, 11:30am, 3pm, 6pm, 8:30pm (configurable per niche)
- Image displayed inline + click-to-download original PNG
- "Copy caption" / "Copy first comment" buttons

## 30-day cleanup

- Cron job runs daily: `find niches/*/out/ -type d -mtime +30 -exec rm -rf {} \;`
- Or via Python script that ALSO clears the SQLite status records for cleaned posts
- Cleanup applies to:
  - `out/<date>/images/*.png`
  - `out/<date>/batch.csv`
  - `out/<date>/batch.md`
  - `out/<date>/raw_response.txt`
- Cache file (`cache/apify_raw.json`) is NOT auto-cleaned — gets overwritten on each fresh scrape

## Build estimate

~4-6 hours for v1 covering:
- Subdomain + nginx vhost setup (30 min)
- Express skeleton (45 min)
- HTML/CSS for the dashboard (1-2 hrs)
- SQLite status tracking + done/strikethrough behavior (1 hr)
- Auto-cleanup cron (30 min)
- Basic auth + deploy testing (1 hr)

## Stack proposal

- Express + EJS templates (matches setformoney.com's stack)
- better-sqlite3 for status persistence
- No frontend framework — vanilla JS for the strikethrough/copy interactions
- Tailwind CDN for quick styling

## Cadence + auto-generation (related decisions)

- **Generation:** daily cron at 6am, 5 posts/day, using `--use-cached-scrape`
- **Scrape refresh:** weekly Sunday 11pm — fresh Apify scrape, repopulates cache
- **Cost:** Apify ~$0.72/week (one fresh scrape with 12 competitors) + Anthropic+Kie daily generation ~$0.30/day = ~$10/mo total
- **Failure alerts:** email to owner via Brevo SMTP (same as setformoney.com magic link auth) when a batch exits non-zero

## Operator rules (v1)

- Strictly "publish what's there." NO regeneration authority.
- Operator flags problems to owner via text/email — no in-app feedback loop in v1
- Operator handbook: 1-page doc covering schedule cadence, copy-paste flow, what to do if image looks off ("notify owner, skip post"), no responsibility for content judgment

## Open questions to revisit

1. **Recommended posting times** — should they be globally fixed, or per-niche configurable, or learned from FB's "best time to post" data?
2. **Should the admin show competitor source posts alongside our generated ones?** Could help operator catch quality issues. Adds clutter.
3. **Backup plan if cron fails?** (e.g., Sunday scrape crashes — does operator see "no posts today" or do they fall back to previous week's overflow?)
4. **Mobile-friendly UI?** Operator might want to schedule from phone.
