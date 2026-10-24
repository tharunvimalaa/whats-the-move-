# Google Calendar integration contract (read-only, for Cubbie integration)

Status: **documented, not built.** Nothing in this repository connects to Google. The engine only reads the JSON Cubbie writes. This file defines what Cubbie must read and how it must be handed over, and how to test it without live credentials.

## Principles

- Calendar is a first-class source of availability, equal to Canvas. Know what the student already has before suggesting anything.
- **Read-only.** Never create, edit, move or delete events. Never send invitations, change attendees or reply to invites. Adding the chosen plan to Calendar is a separate action that happens only after the student says yes, and is out of scope for this phase.
- **Minimum permission.** Use Cubbie's existing Google connection and only the read access needed to list events from calendars the student authorizes (for example a read-only events scope). Do not request new permissions or connect another account without the student's explicit yes. Never ask for or store a Google password.
- **Honesty.** A calendar that returned nothing is "no known calendar conflict", not "free". A calendar that was not read is a gap, not an empty day.

## What Cubbie reads

1. The list of calendars the student has authorized: personal, school, work, shared. Read **all** of them, and record which were read and which failed. If the student has not said which to include, ask once and remember the answer.
2. Every event overlapping the planning window, **expanded into individual occurrences** (recurring events must arrive as their dated instances, with cancelled or moved instances applied), plus a margin of a few hours either side so travel to the first or from the last commitment is visible.
3. For each event: title, start, end, time zone, calendar id and name, event id, recurring-event id if any, busy/free (transparency), the student's response (accepted, tentative, needs action, declined), status (confirmed, tentative, cancelled), all-day flag, location text, and when it was fetched.

## Mapping to `fixed_commitments[]`

| Calendar fact | Day Composer field | Rule |
| --- | --- | --- |
| event id / calendar id / name | `source_event_id`, `calendar_id`, `calendar_name` | preserved in each block's `provenance` |
| title | `title` | |
| start, end | `start`, `end` | send ISO 8601 **with offset** (or naive local time in `student.timezone`); never convert dates by hand |
| fetched time | `synced_at` | per event or the same for the batch |
| event link | `url` | if available |
| Calendar origin | `source: "google_calendar"` | student-typed commitments use `source: "manual"`; unknown origin is left out and shown as "source not stated" |
| transparency = free / available | `busy_status: "free"` | engine ignores it for scheduling and lists it in `ignored_commitments` |
| tentative / maybe | `busy_status: "tentative"` | still blocks time (safe default), flagged `tentative` in the block |
| declined, cancelled | **omit** | Cubbie filters these before handing over |
| all-day events | see below | |
| place | `location_id` | only when Cubbie can match it to a known place; otherwise leave it out so travel is shown as assumed, not invented |
| related Canvas item | `linked_task_id` | see de-duplication |

### Edge cases Cubbie must handle before the engine runs

- **Recurring events:** expand to the occurrences in the window; honor exceptions, moved and cancelled instances. Do not pass the recurrence rule.
- **All-day events:** many are informational (holidays, "due today" reminders). Pass one only if it is marked busy; otherwise mark it `busy_status: "free"`. Multi-day busy events (trips, exam weeks) become commitments clipped to the window.
- **Time zones:** an event's own zone can differ from the student's. Send the absolute instant with its offset. For a student who is traveling, use the zone they are in today for `student.timezone`.
- **Daylight-saving days:** a day can be 23 or 25 hours long. Always send offsets so the engine measures real elapsed time. The engine's tests cover the repeated hour.
- **Overlaps:** the student's own calendar can double-book. The engine reports `COMMITMENT_OVERLAP` and plans around the union; do not silently drop one.
- **Cross-midnight events and windows:** supported; end after midnight is fine.
- **Declined invitations and cancelled events:** omit. Needs-action invitations: treat as tentative.
- **Working-location, birthday and holiday calendars:** exclude unless the student asks.
- **Canvas duplicates:** if a calendar event is the same thing as a Canvas item (for example an "Essay due" entry or an exam also in Canvas), keep one. Prefer the Calendar event as the commitment if it blocks time, and set `linked_task_id` to the Canvas task. If the event is only a deadline reminder, drop it; the Canvas task already carries the due time. Never schedule the same thing twice.
- **Event privacy:** use titles and times only to plan. Do not copy descriptions, attendees or private notes into the input, and do not quote them back.

## What Cubbie reports in `sources.calendar`

```json
{"connected": true, "synced_at": "2026-10-26T07:45:00-07:00",
 "calendars_authorized": ["primary", "work"], "calendars_read": ["primary", "work"],
 "calendars_failed": [], "coverage": "complete"}
```

The 12-hour freshness limit (`settings.calendar_stale_hours`) is an initial, configurable policy, not a universal rule. To be treated as confirmed, Calendar must have been read successfully and completely across every calendar the student authorized for planning. Student-typed commitments are used for scheduling but cannot prove the rest of the day is empty.

| Situation | Send | Result |
| --- | --- | --- |
| Read everything just now | `connected: true`, `synced_at`, `calendars_authorized`, `calendars_read`, `coverage: "complete"` | `current`; plans may be `confirmed` |
| Read, but completeness not stated | `connected: true`, `synced_at` only | `coverage_unknown`; plans cannot be `confirmed` |
| Read, but fetched long ago | `synced_at` older than `calendar_stale_hours` (12) | `stale`; plans cannot be `confirmed` |
| Some calendars failed | `calendars_failed: [ids]` | `partial`; plans cannot be `confirmed` |
| Not connected or request failed | `connected: false` (and `error`) | `unavailable`; availability is `student_reported` if the student gave times, otherwise `unknown`. Either way plans with outside events stay `provisional` |
| Cubbie did not check | omit `sources.calendar` | `not_reported`; same as unavailable |
| Connected, no sync time | `connected: true` only | `connected_sync_unknown` |

When the calendar is current and empty, say "no known calendar conflict", not "you are free".

## Controlled testing (no live credentials in this repo)

1. **Fixtures (automated, already in place):** `tests/fixtures/*.json` carry Calendar-shaped commitments (`source_event_id`, `calendar_id`, `synced_at`, `busy_status`, tentative, cross-midnight, DST) and every source state. They run with `python3 -m unittest discover -s tests`.
2. **Mapping tests (next phase):** add synthetic raw Calendar API-style events for each edge case above, run them through Cubbie's mapping step, and compare the produced `fixed_commitments[]` with expected JSON. Cover recurring expansion, exceptions, all-day, declined, free/busy, multi-calendar, two time zones, and both DST transitions.
3. **Dry run on a test Google account the team owns (read-only):** create a calendar with known events, authorize read access only, run the skill, and check that the timeline shows exactly those events with `calendar_commitment` origin and that nothing in Calendar changed.
4. **Failure drills:** revoke access, disconnect one of two calendars, and let a sync go stale. Confirm each shows as the right `data_gaps` entry and the reply tells the student what could not be checked.
5. **Privacy check:** confirm the saved input and result contain titles, times and ids only, and no descriptions, attendees or credentials.

## Out of scope for this phase

Live Google calls, OAuth, writing to Calendar, sending invitations, and any new permission request.
