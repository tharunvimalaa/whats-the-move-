# Day Composer JSON contract (schema_version 1)

`python3 scripts/compose_day.py --input day.json [--changes changes.json] [--out result.json] [--format json|text]`

Times are ISO 8601. A time without an offset is read as the student's local wall-clock time in `student.timezone`; a time with an offset or `Z` is used as given. All durations are whole minutes and all money is integer cents. The engine never reads the clock, network or accounts; `as_of` is "now".

## Input

| Field | Type | Notes |
| --- | --- | --- |
| `student.timezone` | IANA name | required, e.g. `America/Phoenix` |
| `student.campus` | string | echoed back |
| `student.start_location_id` | string | where the student is at window start; if missing, no travel is assumed before the first block |
| `planning_date` | `YYYY-MM-DD` | the student's actual local date; `window.start` must fall on it |
| `as_of` | time | defaults to `window.start`; used for "due within" and staleness |
| `window.start`, `window.end` | time | end may be after midnight; end must be after start |
| `budget_cents` | int >= 0 | total remaining for the day |
| `sources.calendar` / `sources.canvas` | `{connected, synced_at, calendars_authorized[], calendars_read[], calendars_failed[], coverage, error}` | what Cubbie actually checked; see Source states. Calendar is `current` only with a fresh `synced_at`, `coverage: "complete"`, and `calendars_read` covering `calendars_authorized` when that list is given |
| `data_origin` | string | `synthetic_sample` marks test data; it is echoed in the result with a warning. Real inputs omit it |
| `sources.web` | `{searched, searched_at, error}` | whether web research ran; omit if not reported |
| `fixed_commitments[]` | `{id, title, start, end, location_id, location_free, safety_buffer_minutes, source, busy_status, source_event_id, calendar_id, calendar_name, url, synced_at, linked_task_id, recurring_event_id, all_day}` | classes, shifts. `source` is `google_calendar` or `manual`; leave it out if unknown (shown as "source not stated"). `busy_status`: `busy` (default), `tentative` (still blocks, flagged) or `free` (ignored, listed in `ignored_commitments`). The id fields are passed through to provenance. Overlapping ones are reported, not fixed. See `google-calendar-integration.md` |
| `academic_tasks[]` | `{id, title, course, due, submission_status, remaining_minutes, effort_source, urgency, source, source_id, url, synced_at}` | `source` is `canvas` or `manual` (leave out if unknown). See below |
| `preferences` | `{mood, interests[], avoid_tags[], area_preference, want_both}` | interests match candidate `tags`; mood matches `moods`; `area_preference` is `on_campus` or `community` |
| `travel.legs[]` | `{from, to, minutes, mode, cost_cents, one_way}` | symmetric unless `one_way` |
| `candidates[]` | see below | events, food, activities, places, fallbacks |
| `student_feedback` | `{rejected_candidate_ids[], unavailable_candidate_ids[]}` | what the student said no to / what turned out unavailable |
| `settings` | overrides of the defaults in `DEFAULT_SETTINGS`; `include_generic_suggestions` (default false) puts generic ideas on the timeline, `max_optional_suggestions` (3), `calendar_stale_hours` (12) | buffers, block sizes, thresholds, `max_activities` per variant, `unknown_travel_assumption_minutes` (null = unknown travel makes an option unplaceable) |

### Academic tasks

`submission_status`: `unsubmitted`, `upcoming` (due in the future, no submission recorded), `missing` (overdue, nothing turned in), `submitted`, `graded`, `excused`, `unknown`. Aliases such as `not_submitted`, `turned_in`, `late` are accepted.

- `submitted`, `graded`, `excused`: excluded (`result.academic.excluded`), never scheduled.
- `unknown` (or `upcoming` with a past due date): not scheduled and not assumed unfinished; becomes a question for the student (`needs_status_check`).
- Unfinished work is **critical** (overdue, missing, `urgency: "high"`, or due within `critical_hours`=24 of `as_of`), **soon** (within `soon_hours`=72), otherwise **deferred**.
- `remaining_minutes`: integer from the student, or `null` when unknown. Unknown effort gets a block of `provisional_study_minutes` labeled `provisional_allocation`; it is never reported as enough to finish. `0` means "done but not submitted" (a question, no study block).
- Study never ends after the due time when the due time is inside the window.

### Candidates

Common: `id`, `kind` (`event` | `food` | `activity` | `place`), `title`, `cost_cents` (null = unknown), `location_id` or `location_free: true`, `tags[]`, `moods[]`, `area`, `group` (only one per group), `safety_buffer_minutes`, `fallback: true` (supplied suggestion that makes no external claim).

- **Fixed-time**: `start` plus `end` (or `duration_minutes`).
- **Flexible**: `duration_minutes`, optional `hours: [{open, close}]` (absolute times) or `always_available: true`.
- **Evidence**: `source: {url, name, organizer_type, checked_at}` with `organizer_type` of `organizer`, `venue`, `aggregator` or `unknown`. A URL and `checked_at` are required for any non-fallback option, otherwise it is rejected.
- **Verification**: `verification: {time|hours, price, availability}` each `verified` / `unverified` / `unknown`. Only an `organizer` or `venue` source can verify a field.
- **Status**: `listing_status` (`active`, `canceled`, `postponed`, `rescheduled`, `changed`) and `availability` (`open`, `limited`, `registration_required`, `sold_out`, `unknown`).

## Changes file (replanning)

Any of: `budget_cents`, `window`, `as_of`, `preferences` (merged), `settings` (merged), `reject_candidates[]`, `unavailable_candidates[]`, `candidate_updates {id: fields}`, `task_updates {id: fields}`, `add_commitments[]`, `remove_commitments[]`, `add_candidates[]`, `add_tasks[]`. The result then carries `replan.changed_keys` and a new `input_fingerprint`.

## Output

| Field | Meaning |
| --- | --- |
| `status` | `ok` (at least one plan with outside events is `confirmed`), `provisional_only` (outside-event plans exist but none is confirmed), `schedule_only` (no plan includes outside events; includes an uneventful day shown as commitments only), `empty_day` (no commitments, unfinished work or events are known: no plan, an honest explanation and `data_gaps`), `no_feasible_plan` (genuine infeasibility only: unfinished work that cannot be placed at all), `invalid_input` |
| `errors[]`, `warnings[]`, `questions[]` | `{code, message, ...}` |
| `planning` | resolved date, timezone, window, `as_of`, `free_minutes` |
| `academic` | `unfinished[]`, `excluded[]`, `deferred[]`, `needs_status_check[]`, `risk_flags[]` (best case with no social time), `critical_conflicts[]` (urgent tasks that cannot be fully scheduled even with no activities: `code`, `cause`, `best_case_minutes`, `needed_minutes`) |
| `options[]` | every candidate with `state` (`confirmed`, `provisional`, `fallback`, `rejected`), `reason_codes`, `verified_fields`, `unverified_fields`, `placeable`, `considered` (false when ranked below `max_candidates_considered`; a `CANDIDATE_CAP_APPLIED` warning lists them) |
| `plans[]` | up to three; see below |
| `dropped_variants[]` | variants not returned and why (`VARIANT_DUPLICATE_OF`, `VARIANT_NOT_DEFENSIBLE`, `VARIANT_EMPTY`) |
| `recommended_variant` | `balanced` when available; never the baseline |
| `reliable_fallback_variant` | variant of the first `schedule_only` plan (the baseline, or an ordinary plan that already has no outside events), else null |
| `source_status` | `calendar`, `canvas`, `web` states, see below |
| `data_gaps[]` | `{source, state, message, affects[], severity}` with severity `missing`, `stale`, `unknown` or `info`; every plan repeats it |
| `unscheduled_minutes` (per plan) | window time not used by any block, including travel and buffers |
| `ignored_commitments[]` | commitments marked free that were not scheduled around |
| `no_plan_reasons`, `explanation` | plain-language reason when `plans` is empty |
| `diagnostics.rejected_combinations[]` | sets that fit the schedule but not the budget |

A plan: `variant`, `also_matches[]` (variants that produced an equivalent plan), `status` (`confirmed`, `provisional` or `schedule_only`), `summary`, `timeline[]` (chronological `commitment`, `academic`, `event|food|activity|place|fallback`, `travel`, `buffer` blocks with `start`, `end`, `minutes`, `status`, `cost_cents`, `assumed`, **`origin`** and **`provenance`**), `optional_suggestions[]`, `data_gaps[]`, `totals` (`known_cost_cents` = `known_planned_spend_cents`, `remaining_budget_cents`, `unknown_cost_items`, `unknown_cost_titles`, `cost_complete`, `remaining_budget_is_upper_bound` (true when a cost is unknown, so the remaining budget is a ceiling), `cumulative[]` including travel fares), `status_detail` (one plain sentence on what is not yet confirmed), `certainty` (`events`, `costs`, `travel`, `academic_effort`, `availability`, `academic_data`; each kept separate), `assumed_travel` (`legs`, `minutes`), `critical_conflicts[]` (task ids with urgent work not fully covered in this plan), `academic_coverage[]` (`covered`, `partial`, `provisional_progress`, `uncovered`, `ready_to_submit`, with `risk`), `academic_risk`, `evidence[]`, `provisional_reasons[]`, `unresolved_questions[]`, `next_actions[]` (each says whether it `requires_approval`).

## Provenance

Every timeline block has `origin` (also in `provenance.origin`):

| `origin` | Meaning | Provenance carries |
| --- | --- | --- |
| `calendar_commitment` | read from Google Calendar | `source`, event/calendar ids, `url`, `synced_at` |
| `manual_commitment` | typed by the student | `source` |
| `unspecified_commitment` | source not stated | |
| `system_study_recommendation` | study time chosen by the planner; `recommendation: true`, `verified: false` | `task`: id, title, due, `origin` (`canvas_assignment`, `student_entered_task`, `unspecified_task`), `source_id`, `url`, `synced_at` |
| `verified_external_event` | all essentials verified by an organizer/venue source | `source_url`, `source_name`, `source_id`, `organizer_type`, `checked_at`, verified/unverified fields |
| `unverified_external_event` | found but not fully verified | same, `verified: false` |
| `system_suggestion` | generic idea, never an event; length is an estimate; no price | `recommendation: true` |
| `user_provided_travel` | travel time Cubbie supplied | `mode`, `source`, `checked_at` |
| `assumed_travel_estimate` | a planning assumption | `assumed: true` |
| `system_buffer` | rule-based buffer or break | |

Recommendations and assumptions are never presented as verified facts.

## Suggestions

Generic ideas (a walk, downtime, texting a friend) and any `fallback: true` candidate are **off the timeline by default**. Each plan lists the ones that would fit in `optional_suggestions[]` (`origin: system_suggestion`, `estimated_minutes` with `duration_is_estimate: true`, no price). Set `settings.include_generic_suggestions: true` to let them onto timelines; they still carry `origin: system_suggestion`, never count as events, and never make a plan `confirmed`.

## Plan status

- `schedule_only`: the plan has no outside events: commitments plus recommended study. It does not imply the input was verified.
- `confirmed`: at least one outside event, every outside event confirmed, no assumed travel, no unknown costs, and calendar availability `verified_current`: Calendar read fresh and completely, covering every calendar the student authorized for planning. Student-reported commitments are valid input to plan around but never establish that nothing else is scheduled.
- `provisional`: has outside events but something above is missing (`provisional_reasons` lists it, including `AVAILABILITY_NOT_VERIFIED`).

`certainty` keeps the kinds of uncertainty apart: `events`, `costs`, `travel`, `academic_effort` (does not change status), `availability` (`verified_current`, `student_reported`, `stale`, `partial`, `coverage_unknown`, `sync_time_unknown`, `unknown`; only `verified_current` allows `confirmed`) and `academic_data`.

## Source states

`calendar` and `canvas`: `current`, `stale`, `partial`, `coverage_unknown` (calendar only), `connected_sync_unknown`, `unavailable`, `not_reported`. `web`: `searched_verified_results`, `searched_unverified_results_only`, `searched_no_verified_results`, `unavailable`, `not_reported`. A missing or empty source is never read as "nothing exists": `planning.free_time_meaning` is always "no known calendar conflict (not guaranteed free)".

## Quiet days

An uneventful day is not a failure. If commitments exist but nothing is due soon and no outside event fits, the result is a valid commitments-only plan (`schedule_only`) with the rest of the day left open (`unscheduled_minutes`); nothing is invented to fill it. If no commitments, unfinished work or events are known at all, the status is `empty_day` with `no_plan_reasons: ["EMPTY_DAY"]` and the applicable `data_gaps`; an empty or unread calendar is never described as confirmed free time.

## Baseline plan

An extra plan with `baseline: true` and `variant: "baseline"` made only from commitments and recommended study. It uses no outside events and no suggestions, even when suggestions are on. It is added whenever it differs from the ordinary plans and is omitted when an ordinary plan is already identical (for example when there are no outside events at all). Its status is `schedule_only`.

## Honesty rules for plan status

A confirmed event inside a plan with assumed travel or unknown costs keeps its own `confirmed` status, but the plan is `provisional`, the destination block gets `arrival_confirmed: false`, and `status_detail` says why. Unknown costs are never counted as spend. Provisional study effort is reported in `certainty.academic_effort` and does not by itself make the schedule provisional.

## How the plans differ

All three share the same fixed commitments and rules; they differ in what is placed first and how options are weighed.

- `academic_first`: study for all in-scope work first, at most one activity. Never covers critical work worse than studying alone.
- `balanced`: critical work fully covered as well as it can be, then up to two activities, then remaining study. Never covers critical work worse than `academic_first`.
- `social_first`: up to three activities first, but every critical task keeps a protected study block (`protected_study_minutes`).

Ranking is deterministic: score, then fewer provisional items, then lower cost, then option ids alphabetically.
