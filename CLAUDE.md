# FB Content Engine — project-local instructions

When the user asks to generate content for a niche (phrasing like
"generate content for <niche>", "make N posts for <niche>", "run today's
batch for <niche>"), you should:

1. Parse the niche name and count N from the request. If the niche name
   is omitted, list the folders under `niches/` and ask which one.
   Default count is the niche's `cadence.default_batch_size` from
   `niches/<niche>/brand.yaml`.
2. Run: `python run_batch.py --niche <name> --count <N>`
3. Stream stdout to the user.
4. When the script completes, report: number of posts generated, output
   folder path, any failures, any kill-criteria warnings the script flagged.

Do NOT generate posts yourself. The python script is the source of truth.
Do NOT publish anything to Facebook. Scheduling is manual via Meta Business
Suite — that step is the compliance firewall.

## Project shape

Each niche under `niches/` is fully self-contained:

```
niches/<niche>/
  brand.yaml         # voice, palette, cadence, banned phrases, links
  competitors.yaml   # FB pages to scrape + ranking settings
  themes.yaml        # rotation themes (3-day no-repeat rule)
  prompts/system.md  # generation system prompt
  state.json         # persisted theme rotation (gitignored, auto-created)
  out/YYYY-MM-DD/    # batch outputs (gitignored)
```

To add a new niche (e.g. an education page, a politics page):
`mkdir niches/<new-niche>/` and fill those four config files. The
orchestrator is niche-agnostic.
