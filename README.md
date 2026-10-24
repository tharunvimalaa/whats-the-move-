# What's the Move?

A Cubbie Hackathon **Track 1** Skill (Milestone 1). It helps a college student plan a realistic day around unfinished academic work, classes, a job, free time, a spending limit and real things to do near campus, and returns a timed schedule rather than a list of ideas. First test campus: Arizona State University, Tempe. Nothing in the engine is campus-specific.

## Architecture

```
whats-the-move/               <- the Skill folder (what Cubbie installs)
  SKILL.md                    instructions for Cubbie: gather, research, compose, reply, replan
  scripts/compose_day.py      deterministic Day Composer (Python standard library only)
  assets/sample_input.json    synthetic day for --sample mode
  references/                 JSON contract, reason codes, source-reliability policy
tests/                        unittest suite + synthetic JSON fixtures
```

Cubbie does everything that needs accounts or the internet: Canvas, Google Calendar, memory and web research. It writes what it found to one JSON file and runs the composer, which cannot access any account or network. The composer:

1. Validates the window, planning date, timezone, commitments and budget (all arithmetic is on absolute minutes, so cross-midnight and daylight-saving days work).
2. Sorts academic tasks: submitted/graded/excused are excluded, unknown status becomes a question, the rest are critical, soon or deferred by due time. Unknown effort becomes a labeled provisional block, never an invented estimate.
3. Assesses every candidate as `confirmed`, `provisional` or `rejected` with reason codes: missing source or `checked_at`, cancellation, stale checks, unverified price/availability, aggregator-only sources, conflicts with commitments, impossible travel, closed hours, cost.
4. Searches subsets of options for three variants (`academic_first`, `balanced`, `social_first`), placing blocks with travel, buffers and study breaks, checking cumulative spend (including fares), and ranking deterministically. Variants that duplicate another or are indefensible (social time that abandons urgent work) are dropped with a reason.
5. Returns plans with evidence, risks, questions and next actions that need the student's approval. A schedule-only baseline (commitments plus recommended study, no outside events and no suggestions) is added whenever it differs from the other plans. Every timeline block carries its origin (Calendar, manual, Canvas task, verified or unverified event, system recommendation, user-provided or assumed travel). Plans are `confirmed`, `provisional` or `schedule_only`; sources are reported as current, stale, unavailable, not reported or searched-without-results, and missing data is listed in `data_gaps`. Generic ideas stay out of timelines unless the student opts in and appear as `optional_suggestions`. Urgent academic work that cannot be fully scheduled is reported as a critical conflict, never shown as covered. Zero-fabrication policy: the engine never invents events, places, prices, deadlines, commitments, URLs or verification; study times, estimated lengths and assumed travel are always labeled recommendations or assumptions. It can be re-run with a changes file after a budget, deadline, availability or preference change.

See `whats-the-move/references/day-composer-contract.md` for the contract, `reason-codes.md` for every code and `google-calendar-integration.md` for the planned read-only Calendar integration (not built).

## Try it (no Canvas, web access or credentials)

```
python3 whats-the-move/scripts/compose_day.py --sample --format text
python3 whats-the-move/scripts/compose_day.py --sample --changes tests/fixtures/changes_budget_500.json --format text
python3 -m unittest discover -s tests -v
```

Requires Python 3.9 or newer with timezone data (`zoneinfo`; macOS and Linux include it). No packages to install.

## Installation (later, by the team)

Not installed or published yet. When approved: put the repository on GitHub, then text Cubbie the link to the `whats-the-move` folder. The creator kit's `validate_cubbie_skill.py` checks the folder first.

## Limitations

- The composer plans one day and one window. Multi-day study plans and recurring check-ins are for Cubbie to offer, not the engine.
- No live Calendar, Canvas or web access exists in this repository; Calendar integration is documented only. `confirmed` needs a fresh, complete read of every authorized calendar (freshness limit 12 hours, configurable), so until Cubbie supplies that, plans with outside events stay `provisional`.
- It only knows what Cubbie passes in. Quality depends on Cubbie finding sources, `checked_at` times and travel estimates; missing travel is assumed (20 min by default) and labeled, missing durations make an option unplaceable.
- Search is exhaustive over at most 14 top-scored candidates (options beyond that are listed as `considered: false` with a warning; academic work is placed independently of this limit) and 3 activities per plan; with many more options, lower-ranked ones are not considered.
- Plans are greedy and earliest-fit inside that search, so a plan is feasible and defensible but not mathematically optimal. Ranking weights are heuristics.
- Activity scoring is simple (interest tags, mood, area, source quality, cost). It does not learn preferences.
- Commitments entirely outside the window are ignored, so a class right after the window is not protected by travel time unless the window ends earlier.
- Naive local times inside a repeated daylight-saving hour are ambiguous; send an explicit offset on such days.
- Only synthetic data is bundled and tested. No live Canvas, Calendar or web flow has been exercised, and the skill has not been run on Cubbie.
