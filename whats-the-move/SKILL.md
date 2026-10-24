---
name: whats-the-move
description: Plans a realistic day around a student's unfinished Canvas work, classes, job, budget and real things to do near their campus, as a timed schedule with costs and sources. Use when a student asks "what's the move", what to do today or tonight, how to fit fun around homework, or wants a plan that balances deadlines, free time and a spending limit, even if they never say "plan".
---

# What's the Move?

Turn "I have stuff due but I also want to do something" into one concrete, timed day: study blocks for work that is actually unfinished, real activities near the student's campus, walking time, and a running total against their spending limit. The schedule is the product, not a list of ideas.

You gather the facts with Cubbie's own abilities. A bundled script (`scripts/compose_day.py`) does the scheduling and money math, because clock arithmetic, overlaps and budgets must come out identical every time. The script cannot reach Canvas, Calendar, the web or memory, so everything it needs goes in as a file.

## 1. Gather what the plan depends on

Ask only for what you cannot get yourself, one question at a time, with a default the student can accept with "yes".

- **Today and timezone.** Use the student's local date and timezone from what Cubbie knows about them (ask if unsure). Do not trust the sandbox clock; it may be in another timezone.
- **Window, budget, mood.** When they are free from and until, the most they will spend in total today, and what they feel like (chill, active, social, food). Use remembered preferences, and save new durable ones (dietary needs, usual budget) to memory.
- **Campus.** A university email or Canvas account shows the university, not which campus. Ask which campus or neighborhood they are at today (for example "ASU Tempe") unless memory already says so. The skill works for any university; Tempe is just the first test campus.
- **Canvas (if connected).** Read assignments and quizzes across all courses with due dates and whatever submission status Canvas reports, and note when the data was last synced. If Canvas is not connected, ask what is due soon and whether it is done, then say you are going on their word.
- **Calendar (if connected and the student agrees).** Read-only: read every calendar the student has authorized (personal, school, work) for the planning window, expanded into individual occurrences, with busy/free, tentative and declined status, and the sync time. Never create, change or delete events, send invitations, or ask for new permissions without the student's yes. Follow `references/google-calendar-integration.md` for how events map into the input. If Calendar is not connected or a read failed, ask for the times and places of classes and shifts and say Calendar could not be checked. Never guess a timetable from Canvas deadlines, and never treat an empty or unread calendar as proof the student is free: say "no known calendar conflict".
- **Effort.** Ask roughly how long each unfinished item will take. If they do not know, leave it unknown; the script then reserves a clearly labeled provisional study block. Never make up a time to finish.

## 2. Find real things to do

Use web search and page reading to find options both on campus and in the surrounding community: university events, venues, free or cheap entertainment, food, and ways to recharge. Prefer the organizer's or venue's own page over listing aggregators. For each option record, without guessing: title, source URL, date and time, price, location, how you checked availability, when you checked (`checked_at`), and walking or travel time from the places in the student's day. Record in `sources.web` whether you actually searched. Leave a field empty when you could not find it; the script treats missing essentials as "needs verification", and that honesty is what keeps the plan trustworthy. Read `references/source-reliability.md` before judging what counts as verified.

If nothing verified fits, say so. Do not fill the time: the plan then shows only the student's commitments and recommended study, and the engine lists a few optional ideas (a walk, downtime) separately, clearly marked as ideas rather than events. Never invent an event, place, price or deadline.

## 3. Compose

Write the facts to a JSON file following `references/day-composer-contract.md` (field names, units, statuses), then run from this skill's folder:

```
python3 scripts/compose_day.py --input day.json --out result.json --format text
```

Never use `--sample` data or the test fixtures for a real student; build the input only from the student's real sources (synthetic inputs carry `data_origin: synthetic_sample` and are announced as such). Always fill `sources` (calendar, canvas, web) with what you really did, including `calendars_authorized`, `calendars_read` and `coverage` for Calendar, because the engine reports anything not reported as a gap and will not call a plan confirmed on unchecked data. Keep each commitment's and task's `source` (`google_calendar`/`manual`, `canvas`/`manual`) and ids. Money is in whole cents and times are ISO 8601 (local times may omit the offset). The printed text is a short summary; the full result, with reason codes and sources, is saved in `result.json` for you to read when you need detail. Exit code 2 means the input was unusable: the message names the problem, so fix the file or ask the student for the missing fact.

Try it without Canvas, web access or credentials: `python3 scripts/compose_day.py --sample --format text` (synthetic data, touches nothing).

Read `status` first:
- `ok`: at least one plan with outside events is confirmed (which requires Calendar to have been read freshly and completely; commitments the student typed in are used but never make a plan confirmed).
- `schedule_only`: no outside events are in any plan: the student's commitments plus any recommended study, not a verified day. On a quiet day it may be just the commitments with the rest left open; do not suggest filler.
- `empty_day`: nothing is known (no commitments, unfinished work or events). Say what could not be checked and ask what they have on; never say they are free.
- `provisional_only`: every plan with outside events relies on something unconfirmed; say so plainly and offer the schedule-only plan (`reliable_fallback_variant`, usually `baseline`) as the dependable option.
- `no_feasible_plan`: read `explanation`; tell the student why (no free time, nothing due, nothing fits) and what would change it.
- `invalid_input`: see above.

Up to three plans come back: `academic_first`, `balanced`, `social_first`. Lead with `recommended_variant` (usually balanced) and mention the others in a line each. A plan can be missing because it would not be defensible, such as social time that leaves urgent work untouched; do not apologize for that.

## 4. Replies

Keep each text to a few short lines, plain text, no Markdown symbols, links on their own line. Lead with the plan, not the process. For the recommended plan cover, in this order:

1. The unfinished work that matters today (and anything already submitted that you left out).
2. The timeline with times, including walking and breaks.
3. Why those activities fit (mood, budget, nearby).
4. Total cost and what is left of the budget.
5. Where each activity's information came from, and what is confirmed versus what still needs checking (price, hours, availability), in everyday words, never reason codes or JSON.
6. One next step the student can say yes to.

If academic work is at risk (`academic_risk` or `risk_flags`), say it straight: for example "the quiz still has no study time today". Urgent work listed in `critical_conflicts` cannot be fully scheduled at all, so lead with that and ask what to move. Study times are your recommendation, so say "I'd study" rather than stating them as fixed facts; commitments are "on your calendar" only if they came from Calendar (check each block's `origin`). Name where each activity came from and whether it is verified. Mention `data_gaps` that matter in plain words ("I couldn't read your calendar"). Treat a plan as fully confirmed only when its `status` is `confirmed`: if `status_detail` mentions assumed travel or unknown costs, say "the event is confirmed but the walk time/cost isn't", and describe the remaining budget as "at most" when `remaining_budget_is_upper_bound` is true. If Canvas data may be stale (`warnings`), say when it last synced. If the plan is long, send a short summary and the full schedule as a document.

Example:

```text
Due today: Essay 2 draft (~90 min). Quiz tomorrow 10am (~45 min).

9:00 quiz review
10:30 class
12:30 free acoustic set at the lawn
1:30 essay, 90 min
5:00 work
8:45 night market ($8)

$8 of $30 spent. Event info is from the organizers' pages, checked this morning.
Want me to put this on your calendar?
```

## 5. After they choose, and replanning

Offer only what fits: a study prep sheet, adding the plan to Google Calendar, reminders or a nightly check-in, a drafted invitation to friends, or opening a sign-up page in the browser. Do each one only after the student says yes. Never send messages to anyone else, spend money, book, register, submit work, or change an account on your own; draft invitations and let the student send them; for payment, hand the browser to the student.

When something changes (they reject an option, an event sells out or is canceled, the budget or a deadline moves, they run late), put the change in a small JSON file and re-run with `--changes changes.json` (keys are listed in the contract), or edit the input and re-run. Always recompute instead of patching the old plan by hand, so costs and travel stay consistent. Then show only what changed.

## 6. When something is missing

- Canvas not connected: ask what is due and whether it is done, and ask them to connect Canvas for next time.
- Calendar not connected or a calendar failed to load: ask for classes and shifts, plan around the times they give, and say which calendar could not be read.
- Web search unavailable: say outside events could not be checked. Web search ran but found nothing verified: say so, and offer the schedule-only plan.
- Always name the missing source; never imply live data was checked when it was not.
- Script error: say you could not build the schedule, then ask for the one fact that is missing.

## Privacy

Use grades, messages and coursework only to decide what is unfinished; do not quote private coursework back or save it. Never ask for or store a Canvas, Google or university password. Keep test data synthetic; the bundled sample contains no real student records.
