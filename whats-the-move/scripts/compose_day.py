#!/usr/bin/env python3
"""What's the Move? Day Composer.

Deterministic, standard-library-only planner. It reads one JSON document that
Cubbie assembled (Canvas tasks, calendar commitments, researched activities,
budget, preferences) and returns up to three feasible day plans plus the reasons
anything was excluded or left provisional. It never touches the network, the
clock, or any account: every fact it uses is in the input.

Usage:
  python3 scripts/compose_day.py --sample --format text
  python3 scripts/compose_day.py --input day.json [--changes changes.json]
                                 [--out result.json] [--format json|text]

Exit codes: 0 = a result was produced (check "status"), 2 = unusable input/usage.
The JSON contract and reason codes are documented in
references/day-composer-contract.md and references/reason-codes.md.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import itertools
import json
import sys
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

SCHEMA_VERSION = 1
VARIANTS = ("academic_first", "balanced", "social_first")

DEFAULT_SETTINGS = {
    "round_to_minutes": 5,             # flexible blocks start on this grid
    "safety_buffer_minutes": 10,       # around fixed commitments/events when the place changes
    "transition_minutes": 5,           # minimum slack whenever the place changes
    "study_break_minutes": 10,         # between two study blocks
    "min_study_block_minutes": 25,
    "max_study_block_minutes": 90,
    "max_academic_minutes": 360,       # study cap for one day
    "provisional_study_minutes": 60,   # labelled allocation when remaining effort is unknown
    "critical_hours": 24,              # due within this (or overdue/missing) = critical
    "soon_hours": 72,                  # due within this = scheduled today, otherwise deferred
    "protected_study_minutes": 45,     # social_first still protects this much per critical task
    "max_listing_age_hours": 24,       # older checked_at => provisional
    "canvas_stale_hours": 12,
    "calendar_stale_hours": 12,
    "include_generic_suggestions": False,  # opt-in: suggestions go on the timeline; they are always offered separately
    "max_optional_suggestions": 3,
    "unknown_travel_assumption_minutes": 20,  # null => unknown travel makes an option unplaceable
    "max_candidates_considered": 14,
    "max_activities": {"academic_first": 1, "balanced": 2, "social_first": 3},
    "meal_windows": [["11:30", "14:00"], ["17:30", "20:30"]],
}

# Task status aliases -> normalized state.
STATUS_ALIASES = {
    "unsubmitted": "unsubmitted", "not_submitted": "unsubmitted", "pending": "unsubmitted",
    "submitted": "submitted", "turned_in": "submitted", "late": "submitted",
    "graded": "graded", "excused": "excused", "missing": "missing",
    "upcoming": "upcoming", "unknown": "unknown",
}
COMMITMENT_META = ("source_event_id", "calendar_id", "calendar_name", "url", "synced_at", "linked_task_id",
                   "recurring_event_id", "all_day")
COMMITMENT_ORIGIN = {"google_calendar": "calendar_commitment", "manual": "manual_commitment"}
GENERIC_FALLBACKS = [
    {"id": "fallback_walk", "title": "Take a walk outside", "duration_minutes": 30, "tags": ["outdoors", "chill"]},
    {"id": "fallback_downtime", "title": "Unstructured downtime", "duration_minutes": 30, "tags": ["chill", "recharge"]},
    {"id": "fallback_friend_checkin", "title": "Text a friend to make a plan (draft only)", "duration_minutes": 15, "tags": ["social"]},
]


# ----------------------------------------------------------------------------
# Small helpers
# ----------------------------------------------------------------------------

def is_int(v):
    return isinstance(v, int) and not isinstance(v, bool)


def ceil_to(t, step):
    return -(-t // step) * step


def issue(code, message, path=None, **extra):
    d = {"code": code, "message": message}
    if path:
        d["path"] = path
    d.update(extra)
    return d


@dataclass
class Block:
    id: str
    kind: str                 # commitment | academic | event | food | activity | place | fallback
    title: str
    start: int = 0            # minutes since the Unix epoch (UTC)
    end: int = 0
    loc: str | None = None
    loc_free: bool = False    # can happen wherever the student already is
    fixed: bool = False
    buffer: int = 0
    cost: int | None = 0      # None = unknown
    status: str = "fixed"     # fixed | confirmed | provisional | suggested
    ref: str | None = None
    latest_end: int | None = None
    hours: list = field(default_factory=list)
    provisional_effort: bool = False
    meta: dict = field(default_factory=dict)


# ----------------------------------------------------------------------------
# Problem: parsed input plus the feasibility rules
# ----------------------------------------------------------------------------

class Problem:
    def __init__(self, data):
        self.data = data
        self.errors, self.warnings, self.questions = [], [], []
        self.settings = self._settings(data.get("settings"))
        self.tz = None
        self.ws = self.we = self.as_of = None
        self.commitments, self.matrix = [], {}
        self.tasks, self.excluded_tasks, self.deferred_tasks, self.status_check_tasks = [], [], [], []
        self.assessments, self.pool = [], []
        self.rejected_combos = {}
        self.ignored_commitments, self.data_gaps, self.source_status, self.sources = [], [], {}, {}
        self.certainty = {"availability": "unknown", "academic_data": "unknown"}
        self.task_by_id = {}
        self._parse()

    # -- parsing -------------------------------------------------------------

    def _settings(self, override):
        s = copy.deepcopy(DEFAULT_SETTINGS)
        for k, v in (override or {}).items():
            if k == "max_activities" and isinstance(v, dict):
                s[k].update(v)
            else:
                s[k] = v
        return s

    def dt(self, value, path, required=True, quiet=False):
        """Parse an ISO-8601 string to epoch minutes; naive values are student-local.

        Returns None (and records an input error unless quiet) when unreadable."""
        if value is None and not required:
            return None
        try:
            d = datetime.fromisoformat(value[:-1] + "+00:00" if str(value).endswith("Z") else value)
        except (TypeError, ValueError):
            if not quiet:
                self.errors.append(issue("INVALID_DATETIME", f"Cannot read {value!r} as an ISO date-time", path))
            return None
        if d.tzinfo is None:
            d = d.replace(tzinfo=self.tz)
        return int(d.timestamp() // 60)

    def fmt(self, m):
        return datetime.fromtimestamp(m * 60, self.tz).isoformat(timespec="minutes")

    def _parse(self):
        d, s = self.data, self.settings
        student = d.get("student") or {}
        tzname = student.get("timezone")
        try:
            self.tz = ZoneInfo(tzname)
        except (ZoneInfoNotFoundError, TypeError, ValueError):
            self.errors.append(issue("INVALID_TIMEZONE", f"Unknown student timezone {tzname!r}", "student.timezone"))
            return
        self.start_loc = student.get("start_location_id")
        if not self.start_loc:
            self.warnings.append(issue("START_LOCATION_UNKNOWN",
                                       "No starting location given, so no travel is assumed before the first block"))

        try:
            self.planning_date = datetime.fromisoformat(str(d.get("planning_date"))).date()
        except ValueError:
            self.errors.append(issue("INVALID_PLANNING_DATE", "planning_date must be YYYY-MM-DD", "planning_date"))
            return
        w = d.get("window") or {}
        self.ws, self.we = self.dt(w.get("start"), "window.start"), self.dt(w.get("end"), "window.end")
        if self.ws is None or self.we is None:
            return
        if self.we <= self.ws:
            self.errors.append(issue("INVALID_WINDOW", "window.end must be after window.start", "window"))
            return
        if datetime.fromtimestamp(self.ws * 60, self.tz).date() != self.planning_date:
            self.errors.append(issue("WINDOW_DATE_MISMATCH",
                                     "window.start is not on planning_date in the student's timezone", "window.start"))
            return
        self.as_of = self.dt(d.get("as_of"), "as_of", required=False) if d.get("as_of") else self.ws
        if self.as_of is None:
            return
        if not d.get("as_of"):
            self.warnings.append(issue("AS_OF_DEFAULTED", "No as_of time was given, so the window start is treated as 'now'"))
        self.sources = d.get("sources") or {}

        self.budget = d.get("budget_cents")
        if not is_int(self.budget) or self.budget < 0:
            self.errors.append(issue("INVALID_BUDGET", "budget_cents must be an integer >= 0", "budget_cents"))
            return

        prefs = d.get("preferences") or {}
        self.prefs = prefs
        fb = d.get("student_feedback") or {}
        self.rejected_ids, self.unavailable_ids = set(fb.get("rejected_candidate_ids", [])), set(fb.get("unavailable_candidate_ids", []))

        self._parse_matrix(d.get("travel") or {})
        self._parse_commitments(d.get("fixed_commitments"))
        self._parse_meal_windows()
        self._check_sources(d.get("sources") or {})
        if self.errors:
            return
        self._assess_tasks(d.get("academic_tasks") or [])
        self.task_by_id = {t["id"]: t for t in self.tasks}
        self._assess_candidates(d.get("candidates") or [])
        self._build_data_gaps()

    def _parse_matrix(self, travel):
        for i, e in enumerate(travel.get("legs", [])):
            m = e.get("minutes")
            if not (e.get("from") and e.get("to") and is_int(m) and m >= 0):
                self.errors.append(issue("INVALID_TRAVEL_LEG", "Each travel leg needs from, to and integer minutes >= 0", f"travel.legs[{i}]"))
                continue
            self.matrix[(e["from"], e["to"])] = e
            if not e.get("one_way"):
                self.matrix.setdefault((e["to"], e["from"]), e)

    def _parse_commitments(self, items):
        seen = set()
        for i, c in enumerate(items or []):
            path = f"fixed_commitments[{i}]"
            cid = str(c.get("id") or f"commitment_{i + 1}")
            if cid in seen:
                self.errors.append(issue("DUPLICATE_ID", f"Duplicate commitment id {cid}", path))
                continue
            seen.add(cid)
            a, b = self.dt(c.get("start"), path + ".start"), self.dt(c.get("end"), path + ".end")
            if a is None or b is None:
                continue
            if b <= a:
                self.errors.append(issue("INVALID_TIME_RANGE", f"{cid}: end must be after start", path))
                continue
            busy = str(c.get("busy_status", "busy")).lower()
            if busy in ("free", "transparent"):
                self.ignored_commitments.append({"id": cid, "title": str(c.get("title") or cid), "reason": "COMMITMENT_MARKED_FREE"})
                continue
            if b <= self.ws or a >= self.we:
                self.warnings.append(issue("COMMITMENT_OUTSIDE_WINDOW", f"{cid} does not overlap the planning window and was ignored", path))
                continue
            self.commitments.append(Block(
                id=cid, kind="commitment", title=str(c.get("title") or cid), start=a, end=b,
                loc=c.get("location_id"), loc_free=bool(c.get("location_free")), fixed=True,
                buffer=c.get("safety_buffer_minutes", self.settings["safety_buffer_minutes"]),
                cost=0, status="fixed", ref=c.get("source"),
                meta=dict({k: c[k] for k in COMMITMENT_META if c.get(k) is not None}, **({"tentative": True} if busy == "tentative" or c.get("tentative") else {}))))
        self.commitments.sort(key=lambda b: (b.start, b.end, b.id))
        self.commitment_overlaps = []
        for x, y in itertools.combinations(self.commitments, 2):
            if y.start < x.end:
                self.commitment_overlaps.append({"a": x.id, "b": y.id})
                self.warnings.append(issue("COMMITMENT_OVERLAP", f"'{x.title}' and '{y.title}' overlap; please confirm which is right", ids=[x.id, y.id]))
        if not self.commitments:
            self.questions.append(issue("QUESTION_COMMITMENTS", "No classes or shifts were provided. Do you have anything fixed today?"))

    def _parse_meal_windows(self):
        self.meal_ranges = []
        day0 = datetime.fromtimestamp(self.ws * 60, self.tz).date()
        day1 = datetime.fromtimestamp(self.we * 60, self.tz).date()
        for n in range((day1 - day0).days + 1):
            day = day0.fromordinal(day0.toordinal() + n)
            for a, b in self.settings["meal_windows"]:
                lo = datetime.fromisoformat(f"{day}T{a}").replace(tzinfo=self.tz)
                hi = datetime.fromisoformat(f"{day}T{b}").replace(tzinfo=self.tz)
                self.meal_ranges.append((int(lo.timestamp() // 60), int(hi.timestamp() // 60)))

    def _check_sources(self, sources):
        cal, canvas = sources.get("calendar") or {}, sources.get("canvas") or {}
        if cal.get("connected") is False:
            self.warnings.append(issue("CALENDAR_UNAVAILABLE", "Google Calendar is not connected; using commitments the student supplied"))
        if canvas.get("connected") is False:
            self.warnings.append(issue("CANVAS_NOT_CONNECTED", "Canvas is not connected; academic work is limited to what the student told us"))
        elif canvas.get("synced_at"):
            t = self.dt(canvas["synced_at"], "sources.canvas.synced_at")
            if t is not None and self.as_of - t > self.settings["canvas_stale_hours"] * 60:
                self.warnings.append(issue("CANVAS_DATA_STALE", f"Canvas data was last synced {(self.as_of - t) // 60}h before planning and may be out of date"))
        elif canvas.get("connected"):
            self.warnings.append(issue("CANVAS_SYNC_TIME_UNKNOWN", "Canvas is connected but no sync time was reported"))

    def _source_state(self, src, stale_hours, path, need_coverage=False):
        """current / stale / partial / coverage_unknown / connected_sync_unknown / unavailable / not_reported.

        With need_coverage (Calendar), "current" also requires the source to say every authorized
        calendar was read successfully (coverage "complete", and calendars_read covering calendars_authorized)."""
        if not isinstance(src, dict) or src.get("connected") is None:
            return "not_reported"
        if src["connected"] is False or src.get("error") or src.get("status") == "failed":
            return "unavailable"
        if src.get("calendars_failed") or src.get("coverage") == "partial":
            return "partial"
        if need_coverage and src.get("calendars_authorized") and not set(src["calendars_authorized"]) <= set(src.get("calendars_read", [])):
            return "partial"
        t = self.dt(src.get("synced_at"), path, quiet=True) if src.get("synced_at") else None
        if t is None:
            return "connected_sync_unknown"
        if self.as_of - t > stale_hours * 60:
            return "stale"
        if need_coverage and src.get("coverage") != "complete":
            return "coverage_unknown"
        return "current"

    def _build_data_gaps(self):
        """State which live sources were current, stale, missing or searched without result."""
        s, src = self.settings, self.sources
        cal = self._source_state(src.get("calendar"), s["calendar_stale_hours"], "sources.calendar.synced_at", need_coverage=True)
        canvas = self._source_state(src.get("canvas"), s["canvas_stale_hours"], "sources.canvas.synced_at")
        web_raw = src.get("web")
        real = [a for a in self.assessments if not a["raw"].get("fallback")]
        if not isinstance(web_raw, dict) or web_raw.get("searched") is None:
            web = "not_reported"
        elif web_raw["searched"] is False or web_raw.get("error"):
            web = "unavailable"
        elif any(a["state"] == "confirmed" for a in real):
            web = "searched_verified_results"
        elif any(a["state"] == "provisional" for a in real):
            web = "searched_unverified_results_only"
        else:
            web = "searched_no_verified_results"
        self.source_status = {"calendar": cal, "canvas": canvas, "web": web}

        manual = [c for c in self.commitments if c.ref == "manual"]
        reported = bool(self.commitments) and len(manual) == len(self.commitments)
        self.certainty["availability"] = (
            "verified_current" if cal == "current" else
            "student_reported" if cal in ("unavailable", "not_reported") and reported else
            {"stale": "stale", "partial": "partial", "coverage_unknown": "coverage_unknown",
             "connected_sync_unknown": "sync_time_unknown"}.get(cal, "unknown"))
        all_tasks = self.tasks + self.excluded_tasks + self.deferred_tasks + self.status_check_tasks
        self.certainty["academic_data"] = (
            "current" if canvas == "current" else
            "student_reported" if canvas in ("unavailable", "not_reported") and all_tasks and all(t["source"] == "manual" for t in all_tasks) else
            {"stale": "stale", "partial": "partial", "connected_sync_unknown": "sync_time_unknown"}.get(canvas, "unknown"))

        def gap(source, state, message, affects, severity):
            self.data_gaps.append({"source": source, "state": state, "message": message, "affects": affects, "severity": severity})

        tail = (" Using commitments the student reported." if reported else
                " No commitments are known, which does not mean the day is free.")
        phrase = " Free time here means no known calendar conflict, not guaranteed free."
        if cal == "unavailable":
            gap("calendar", cal, "Google Calendar could not be read (not connected or retrieval failed)." + tail + phrase, ["availability"], "missing")
        elif cal == "not_reported":
            gap("calendar", cal, "Calendar status was not reported, so it is unknown whether busy times were checked." + tail + phrase, ["availability"], "missing")
        elif cal == "partial":
            failed = ", ".join(map(str, (src.get("calendar") or {}).get("calendars_failed", []))) or "some"
            gap("calendar", cal, f"Not every calendar could be read ({failed}); busy times may be missing." + phrase, ["availability"], "missing")
        elif cal == "stale":
            gap("calendar", cal, "Google Calendar was last synced a while before planning and may be out of date." + phrase, ["availability"], "stale")
        elif cal == "coverage_unknown":
            gap("calendar", cal, "Google Calendar was read, but it was not confirmed that every authorized calendar was read completely; busy times may be missing." + phrase, ["availability"], "unknown")
        elif cal == "connected_sync_unknown":
            gap("calendar", cal, "Google Calendar is connected but no sync time was reported." + phrase, ["availability"], "unknown")
        elif not self.commitments:
            gap("calendar", cal, "Calendar returned no events in the planning window: no known calendar conflict." , ["availability"], "info")
        if canvas in ("unavailable", "not_reported"):
            gap("canvas", canvas, "Canvas could not be read or its status was not reported; unfinished work is limited to what the student told us.", ["academic"], "missing")
        elif canvas == "partial":
            gap("canvas", canvas, "Not every course could be read from Canvas; some assignments may be missing.", ["academic"], "missing")
        elif canvas == "stale":
            gap("canvas", canvas, "Canvas was last synced a while before planning, so submission statuses may be out of date.", ["academic"], "stale")
        elif canvas == "connected_sync_unknown":
            gap("canvas", canvas, "Canvas is connected but no sync time was reported.", ["academic"], "unknown")
        web_msg = {
            "not_reported": ("Web research status was not reported; no outside events were verified.", "missing"),
            "unavailable": ("Web research was unavailable, so no outside events were checked.", "missing"),
            "searched_no_verified_results": ("Web research ran but found no verified events that fit.", "info"),
            "searched_unverified_results_only": ("Web research found options, but none could be fully verified.", "info"),
        }
        if web in web_msg:
            gap("web", web, web_msg[web][0], ["events"], web_msg[web][1])
        if manual:
            gap("commitments", "student_reported", "Some commitments were entered by the student rather than read from Calendar.", ["availability"], "info")

    # -- academic ---------------------------------------------------------------

    def _assess_tasks(self, items):
        s = self.settings
        seen = set()
        for i, t in enumerate(items):
            path = f"academic_tasks[{i}]"
            tid = str(t.get("id") or f"task_{i + 1}")
            if tid in seen:
                self.errors.append(issue("DUPLICATE_ID", f"Duplicate task id {tid}", path))
                continue
            seen.add(tid)
            row = {"id": tid, "title": str(t.get("title") or tid), "course": t.get("course"),
                   "source": t.get("source"), "url": t.get("url"), "source_id": t.get("source_id"),
                   "synced_at": t.get("synced_at"), "reason_codes": []}
            status = STATUS_ALIASES.get(str(t.get("submission_status", "unknown")).lower(), "unknown")
            row["status"] = status
            due = self.dt(t["due"], path + ".due") if t.get("due") else None
            row["due"] = self.fmt(due) if due is not None else None
            if status in ("submitted", "graded", "excused"):
                row["reason_codes"].append({"submitted": "ACADEMIC_SUBMITTED", "graded": "ACADEMIC_GRADED", "excused": "ACADEMIC_EXCUSED"}[status])
                self.excluded_tasks.append(row)
                continue
            if status == "unknown" or (status == "upcoming" and due is not None and due <= self.as_of):
                code = "ACADEMIC_STATUS_UNKNOWN" if status == "unknown" else "ACADEMIC_STATUS_INCONSISTENT"
                row["reason_codes"].append(code)
                row["due_soon"] = due is not None and due - self.as_of <= s["soon_hours"] * 60
                self.status_check_tasks.append(row)
                self.questions.append(issue("QUESTION_TASK_STATUS", f"Have you already submitted '{row['title']}'? I can't tell from the data I have.", task=tid))
                continue
            if due is None:
                row["reason_codes"].append("ACADEMIC_DUE_UNKNOWN")
                self.deferred_tasks.append(row)
                self.questions.append(issue("QUESTION_TASK_DUE", f"When is '{row['title']}' due?", task=tid))
                continue
            hours = (due - self.as_of) / 60
            overdue = status == "missing" or due <= self.as_of
            if overdue:
                row["tier"] = "critical"
                row["reason_codes"].append("ACADEMIC_OVERDUE")
            elif hours <= s["critical_hours"] or str(t.get("urgency", "")).lower() == "high":
                row["tier"] = "critical"
                row["reason_codes"].append("ACADEMIC_CRITICAL")
            elif hours <= s["soon_hours"]:
                row["tier"] = "soon"
            else:
                row["reason_codes"].append("ACADEMIC_NOT_DUE_SOON")
                self.deferred_tasks.append(row)
                continue
            rem = t.get("remaining_minutes")
            if rem is None:
                row.update(need=s["provisional_study_minutes"], effort_source="provisional_allocation", provisional=True)
                row["reason_codes"].append("ACADEMIC_EFFORT_UNKNOWN")
                self.questions.append(issue("QUESTION_EFFORT", f"About how long do you think '{row['title']}' will take?", task=tid))
            elif is_int(rem) and rem >= 0:
                row.update(need=rem, effort_source=t.get("effort_source", "student"), provisional=False)
                if rem == 0:
                    row["reason_codes"].append("ACADEMIC_READY_TO_SUBMIT")
                    self.questions.append(issue("QUESTION_READY_TO_SUBMIT", f"'{row['title']}' looks finished but isn't submitted. Want to submit it?", task=tid))
            else:
                self.errors.append(issue("INVALID_EFFORT", "remaining_minutes must be an integer >= 0 or null", path))
                continue
            row["due_epoch"] = due
            row["latest_end"] = due if self.ws < due else None
            self.tasks.append(row)
        rank = {"critical": 0, "soon": 1}
        self.tasks.sort(key=lambda r: (rank[r["tier"]], r["due_epoch"], r["id"]))

    # -- candidates -----------------------------------------------------------------

    def _assess_candidates(self, items):
        seen = set()
        for i, raw in enumerate(items):
            cid = str(raw.get("id") or f"candidate_{i + 1}")
            if cid in seen:
                self.errors.append(issue("DUPLICATE_ID", f"Duplicate candidate id {cid}", f"candidates[{i}]"))
                continue
            seen.add(cid)
            self.assessments.append(self._assess(cid, raw, f"candidates[{i}]"))

    def _assess(self, cid, raw, path):
        s = self.settings
        a = {"id": cid, "title": str(raw.get("title") or cid), "kind": raw.get("kind", "activity"),
             "state": None, "reason_codes": [], "notes": [], "verified_fields": [], "unverified_fields": [],
             "placeable": True, "score": 0, "block": None, "raw": raw}
        rej, prov = [], []
        is_fb = bool(raw.get("fallback"))
        src = raw.get("source") or {}
        if cid in self.rejected_ids:
            rej.append("USER_REJECTED")
        if cid in self.unavailable_ids:
            rej.append("REPORTED_UNAVAILABLE")
        avoided = set(raw.get("tags", [])) & set(self.prefs.get("avoid_tags", []))
        if avoided:
            rej.append("PREFERENCE_AVOIDED")

        checked = None
        if not is_fb:
            if not src.get("url"):
                rej.append("MISSING_SOURCE_URL")
            if not src.get("checked_at"):
                rej.append("MISSING_CHECKED_AT")
            else:
                checked = self.dt(src["checked_at"], path, quiet=True)
                if checked is None:
                    rej.append("MISSING_CHECKED_AT")
        if checked is not None:
            if self.as_of - checked > s["max_listing_age_hours"] * 60:
                prov.append("STALE_CHECK")
            elif checked - self.as_of > 5:
                prov.append("CHECKED_AT_IN_FUTURE")

        listing = str(raw.get("listing_status", "active")).lower()
        if listing in ("canceled", "cancelled"):
            rej.append("EVENT_CANCELED")
        elif listing == "postponed":
            rej.append("EVENT_POSTPONED")
        elif listing in ("changed", "rescheduled"):
            prov.append("CHANGES_REPORTED")
        avail = str(raw.get("availability") or "unknown").lower()
        if avail in ("sold_out", "full", "closed"):
            rej.append("SOLD_OUT")
        elif avail == "registration_required":
            prov.append("REGISTRATION_REQUIRED")

        # Source reliability: only the organizer or venue can verify a field.
        primary = src.get("organizer_type") in ("organizer", "venue")
        ver = raw.get("verification") or {}

        def verified(name):
            return primary and ver.get(name) == "verified"

        if not is_fb and not primary:
            prov.append("ONLY_AGGREGATOR_SOURCE")
            a["notes"].append("Only a third-party listing was found; confirm with the organizer or venue.")

        fixed = raw.get("start") is not None
        start = end = None
        blk = Block(id=cid, kind=a["kind"], title=a["title"], fixed=fixed, loc=raw.get("location_id"),
                    loc_free=bool(raw.get("location_free")), cost=raw.get("cost_cents"))
        if fixed:
            start = self.dt(raw["start"], path, quiet=True)
            if raw.get("end") is not None:
                end = self.dt(raw["end"], path, quiet=True)
            elif is_int(raw.get("duration_minutes")) and raw["duration_minutes"] > 0 and start is not None:
                end = start + raw["duration_minutes"]
            if start is None or (raw.get("end") is not None and end is None):
                rej.append("INVALID_TIME_RANGE")
            elif end is None:
                prov.append("END_TIME_UNKNOWN")
                a["placeable"] = False
            elif end <= start:
                rej.append("INVALID_TIME_RANGE")
            elif end <= self.as_of:
                rej.append("EVENT_ALREADY_ENDED")
            else:
                blk.start, blk.end = start, end
                blk.buffer = raw.get("safety_buffer_minutes", s["safety_buffer_minutes"])
            if not is_fb and not verified("time"):
                prov.append("TIME_UNVERIFIED")
                a["unverified_fields"].append("time")
            elif not is_fb:
                a["verified_fields"].append("time")
        else:
            dur = raw.get("duration_minutes")
            if not (is_int(dur) and dur > 0):
                prov.append("DURATION_UNKNOWN")
                a["placeable"] = False
            blk.buffer = raw.get("safety_buffer_minutes", 0)
            if not is_fb:
                hours = raw.get("hours")
                if hours is None:
                    if not raw.get("always_available"):
                        prov.append("HOURS_UNKNOWN")
                        a["unverified_fields"].append("hours")
                else:
                    for h in hours:
                        o, c = self.dt(h.get("open"), path, quiet=True), self.dt(h.get("close"), path, quiet=True)
                        if o is not None and c is not None and c > o:
                            blk.hours.append((o, c))
                    if not blk.hours:
                        prov.append("HOURS_UNKNOWN")
                        a["unverified_fields"].append("hours")
                    elif not verified("hours"):
                        prov.append("HOURS_UNVERIFIED")
                        a["unverified_fields"].append("hours")
                    else:
                        a["verified_fields"].append("hours")

        cost = raw.get("cost_cents")
        if cost is None:
            prov.append("COST_UNKNOWN")
            blk.cost = None
            a["unverified_fields"].append("price")
        elif not is_int(cost) or cost < 0:
            rej.append("INVALID_COST")
        elif cost > self.budget:
            rej.append("COST_EXCEEDS_BUDGET")
        elif not is_fb:
            if verified("price"):
                a["verified_fields"].append("price")
            else:
                prov.append("PRICE_UNVERIFIED")
                a["unverified_fields"].append("price")
        if not is_fb:
            if avail == "unknown":
                prov.append("AVAILABILITY_UNKNOWN")
                a["unverified_fields"].append("availability")
            elif not verified("availability"):
                if avail != "registration_required":
                    prov.append("AVAILABILITY_UNVERIFIED")
                a["unverified_fields"].append("availability")
            else:
                a["verified_fields"].append("availability")
            if avail == "limited":
                a["notes"].append("Limited availability reported.")
        if not (blk.loc or blk.loc_free):
            prov.append("LOCATION_UNKNOWN")
            a["unverified_fields"].append("location")

        a["block"] = blk
        if not rej and a["placeable"]:
            rej += self._solo_check(a, raw)
        if rej == ["UNKNOWN_TRAVEL"]:
            rej = []
            prov.append("TRAVEL_UNKNOWN")
            a["placeable"] = False
        if rej:
            a["state"], a["reason_codes"] = "rejected", list(dict.fromkeys(rej))
            return a
        a["reason_codes"] = list(dict.fromkeys(prov))
        a["state"] = "provisional" if prov else ("fallback" if is_fb else "confirmed")
        blk.status = "provisional" if prov else ("suggested" if is_fb else "confirmed")
        a["score"] = self._score(a, raw, src, primary)
        if prov and not is_fb:
            self.questions.append(issue("QUESTION_VERIFY_OPTION", f"'{a['title']}' needs verification ({', '.join(sorted(set(a['unverified_fields']))) or 'details'}) before it is booked or relied on.", candidate=cid))
        return a

    def _solo_check(self, a, raw):
        """Reject options that cannot work even in an otherwise empty day."""
        blk = a["block"]
        if self.settings["unknown_travel_assumption_minutes"] is None and blk.loc:
            known_places = [self.start_loc] + [c.loc for c in self.commitments if c.loc]
            if any(self.travel(x, blk.loc)[0] is None for x in known_places):
                return ["UNKNOWN_TRAVEL"]
        if blk.fixed:
            problems = self.evaluate(self.commitments + [blk])["problems"]
            codes = []
            for p in problems:
                if blk.id in p[1:]:
                    codes.append({"OVERLAP": "ACTIVITY_CONFLICTS_COMMITMENT"}.get(p[0], p[0]))
            return sorted(set(codes), key=codes.index)
        placed = self.try_place(self.commitments, blk, [raw["duration_minutes"]])
        if placed:
            return []
        if blk.hours and not any(c - o >= raw["duration_minutes"] and c > self.ws and o < self.we for o, c in blk.hours):
            return ["ACTIVITY_CLOSED_DURING_WINDOW"]
        return ["NO_FEASIBLE_SLOT"]

    def _score(self, a, raw, src, primary):
        sc = 10
        tags = set(raw.get("tags", []))
        sc += 15 * len(tags & set(self.prefs.get("interests", [])))
        if self.prefs.get("mood") and self.prefs["mood"] in raw.get("moods", []):
            sc += 10
        if self.prefs.get("area_preference") and raw.get("area") == self.prefs["area_preference"]:
            sc += 5
        if a["kind"] == "event":
            sc += 5
        if primary:
            sc += 3
        if a["state"] == "confirmed":
            sc += 10
        if a["state"] == "fallback":
            sc -= 5
        sc -= (a["block"].cost or 0) // 500
        return max(sc, 1)

    # -- feasibility rules ------------------------------------------------------------

    def travel(self, x, y):
        """(minutes|None, cost_cents, kind, leg) between two effective locations."""
        if x is None or y is None or x == y:
            return 0, 0, "none", None
        leg = self.matrix.get((x, y))
        if leg:
            return leg["minutes"], leg.get("cost_cents", 0) or 0, "known", leg
        assumed = self.settings["unknown_travel_assumption_minutes"]
        return assumed, 0, ("assumed" if assumed is not None else "unknown"), None

    def effective_locations(self, seq):
        cur, out = self.start_loc, []
        for b in seq:
            eff = b.loc or (cur if b.loc_free else "?" + b.id)
            out.append(eff)
            if eff is not None:
                cur = eff
        return out

    def evaluate(self, blocks):
        """Check a set of blocks. Returns problems (code, id[, id]) and the transitions between blocks."""
        seq = sorted(blocks, key=lambda b: (b.start, b.end, b.id))
        problems, transitions = [], []
        for b in seq:
            if b.kind != "commitment" and (b.start < self.ws or b.end > self.we):
                problems.append(("OUTSIDE_WINDOW", b.id))
            if b.latest_end is not None and b.end > b.latest_end:
                problems.append(("AFTER_DEADLINE", b.id))
            if b.hours and not any(o <= b.start and b.end <= c for o, c in b.hours):
                problems.append(("OUTSIDE_OPEN_HOURS", b.id))
        for i, x in enumerate(seq):
            for y in seq[i + 1:]:
                if y.start < x.end and not (x.kind == y.kind == "commitment"):
                    problems.append(("OVERLAP", x.id, y.id))
        eff = self.effective_locations(seq)
        pred = None  # index of the earlier block that ends last
        for j, y in enumerate(seq):
            if pred is not None:
                x = seq[pred]
                if not (x.kind == y.kind == "commitment") and y.start >= x.end:
                    mins, cost, kind, leg = self.travel(eff[pred], eff[j])
                    if mins is None:
                        problems.append(("UNKNOWN_TRAVEL", x.id, y.id))
                    else:
                        buf = 0
                        if eff[pred] != eff[j]:
                            buf = max(x.buffer, y.buffer, self.settings["transition_minutes"])
                        if x.kind == y.kind == "academic":
                            buf = max(buf, self.settings["study_break_minutes"])
                        gap = y.start - x.end
                        if gap < mins:
                            problems.append(("TRAVEL_EXCEEDS_AVAILABLE_TIME", x.id, y.id))
                        elif gap < mins + buf:
                            problems.append(("TRANSITION_BUFFER_TOO_TIGHT", x.id, y.id))
                        transitions.append({"from": x, "to": y, "from_loc": eff[pred], "to_loc": eff[j],
                                            "minutes": mins, "cost": cost, "kind": kind, "leg": leg, "buffer": buf})
            if pred is None or y.end > seq[pred].end:
                pred = j
        return {"problems": problems, "transitions": transitions, "sequence": seq}

    def candidate_starts(self, blocks, proto):
        seq = sorted(blocks, key=lambda b: (b.start, b.end, b.id))
        eff = self.effective_locations(seq)
        starts = {self.ws}
        for b, e in zip(seq, eff):
            new_eff = proto.loc or (e if proto.loc_free else "?" + proto.id)
            mins, _, _, _ = self.travel(e, new_eff)
            buf = 0
            if e != new_eff:
                buf = max(b.buffer, proto.buffer, self.settings["transition_minutes"])
            if b.kind == proto.kind == "academic":
                buf = max(buf, self.settings["study_break_minutes"])
            starts.add(b.end + (mins or 0) + buf)
            starts.add(b.end)
        for o, _ in proto.hours:
            starts.add(o)
        step = self.settings["round_to_minutes"]
        return sorted(ceil_to(t, step) for t in starts if t >= self.ws)

    def try_place(self, blocks, proto, durations, prefer_ranges=()):
        """Earliest valid placement of a flexible block; tries each duration in order."""
        starts = self.candidate_starts(blocks, proto)
        if prefer_ranges:
            starts.sort(key=lambda t: (0 if any(a <= t < b for a, b in prefer_ranges) else 1, t))
        for d in durations:
            for t in starts:
                if t + d > self.we:
                    continue
                cand = replace(proto, start=t, end=t + d)
                if not self.evaluate(blocks + [cand])["problems"]:
                    return cand
        return None


# ----------------------------------------------------------------------------
# Composer: builds and ranks day layouts
# ----------------------------------------------------------------------------

@dataclass
class Layout:
    variant: str
    combo: tuple
    blocks: list
    transitions: list
    placed: dict
    cov: dict
    cost_total: int
    unknown_cost: list
    cumulative: list
    key: tuple = ()


SOCIAL_SCORE_REF = 180  # activity score total that counts as "fully social" (about three strong picks)
ACADEMIC_WEIGHT = {"critical": 4, "soon": 2}
VARIANT_WEIGHTS = {"academic_first": (9, 1), "balanced": (5, 5), "social_first": (1, 9)}
VARIANT_STEPS = {
    "academic_first": [("acad", "full"), ("flex", None)],
    "balanced": [("acad", "critical"), ("flex", None), ("acad", "full")],
    "social_first": [("flex", None), ("acad", "protected"), ("acad", "full")],
}


class Composer:
    def __init__(self, prob):
        self.p = prob
        self.s = prob.settings
        self.pool = self._build_pool()
        self.by_id = {a["id"]: a for a in prob.assessments}

    def generics(self):
        """Built-in generic ideas (not events). Offered separately; on the timeline only on opt-in."""
        if self._generics is None:
            self._generics = []
            for i, g in enumerate(GENERIC_FALLBACKS):
                raw = dict(g, kind="fallback", fallback=True, cost_cents=0, location_free=True, generic=True)
                a = self.p._assess(g["id"], raw, f"generic_fallbacks[{i}]")
                a["generic"] = True
                a["notes"].append("Generic suggestion, not a verified event.")
                a["considered"] = False
                self.p.assessments.append(a)
                if a["state"] == "fallback":
                    self._generics.append(a)
        return self._generics

    def _build_pool(self):
        p = self.p
        self._generics = None
        for a in p.assessments:
            a["considered"] = False
        ok = [a for a in p.assessments if a["state"] in ("confirmed", "provisional", "fallback") and a["placeable"]]
        usable = [a for a in ok if a["state"] != "fallback"]
        self.suggestions = sorted([a for a in ok if a["state"] == "fallback"] + self.generics(), key=lambda a: (-a["score"], a["id"]))
        for a in self.suggestions:
            a["suggestion"] = True
        if self.s["include_generic_suggestions"]:
            usable = sorted(usable + self.suggestions, key=lambda a: (-a["score"], a["id"]))
        usable.sort(key=lambda a: (-a["score"], a["id"]))
        cap = self.s["max_candidates_considered"]
        for a in usable[:cap]:
            a["considered"] = True
        skipped = usable[cap:]
        for a in skipped:
            a["notes"].append("Not considered: ranked below the candidate limit.")
        if skipped:
            p.warnings.append(issue("CANDIDATE_CAP_APPLIED",
                                    f"{len(skipped)} lower-ranked option(s) were not considered (limit {cap}). Academic work is planned first and is not affected.",
                                    ids=[a["id"] for a in skipped]))
        return usable[:cap]

    # -- search -----------------------------------------------------------------------

    def subsets(self, cap, pool=None):
        p = self.p
        pool = self.pool if pool is None else pool
        for n in range(cap + 1):
            for combo in itertools.combinations(pool, n):
                if sum(a["kind"] == "food" for a in combo) > 1:
                    continue
                groups = [a["raw"]["group"] for a in combo if a["raw"].get("group")]
                if len(groups) != len(set(groups)):
                    continue
                known = sum(a["block"].cost or 0 for a in combo)
                if known > p.budget:
                    ids = tuple(sorted(a["id"] for a in combo))
                    if not any(set(k) < set(ids) for k in p.rejected_combos):
                        p.rejected_combos[ids] = {"candidates": list(ids), "reason": "COMBINED_COST_EXCEEDS_BUDGET",
                                                  "total_cents": known, "budget_cents": p.budget}
                    continue
                yield combo

    def layout(self, variant, combo):
        p, s = self.p, self.s
        anchors = p.commitments + [a["block"] for a in combo if a["block"].fixed]
        if p.evaluate(anchors)["problems"]:
            return None
        blocks = list(anchors)
        placed = {t["id"]: 0 for t in p.tasks}
        chunks = {t["id"]: 0 for t in p.tasks}
        step = s["round_to_minutes"]

        def academic(mode):
            for t in p.tasks:
                if mode != "full" and t["tier"] != "critical":
                    continue
                target = t["need"] if mode != "protected" else min(t["need"], s["protected_study_minutes"])
                while placed[t["id"]] < target and sum(placed.values()) < s["max_academic_minutes"]:
                    want = min(s["max_study_block_minutes"], target - placed[t["id"]],
                               s["max_academic_minutes"] - sum(placed.values()))
                    chunks[t["id"]] += 1
                    proto = Block(id=f"study:{t['id']}:{chunks[t['id']]}", kind="academic", title=t["title"],
                                  loc_free=True, status="planned", ref=t["id"], latest_end=t["latest_end"],
                                  provisional_effort=t["provisional"])
                    got = p.try_place(blocks, proto, [want])
                    if got is None:
                        low = min(s["min_study_block_minutes"], want)
                        got = p.try_place(blocks, proto, list(range(want - step, low - 1, -step)))
                    if got is None:
                        break
                    blocks.append(got)
                    placed[t["id"]] += got.end - got.start

        flex = sorted((a for a in combo if not a["block"].fixed), key=lambda a: (-a["score"], a["id"]))
        for what, mode in VARIANT_STEPS[variant]:
            if what == "acad":
                academic(mode)
                continue
            for a in flex:
                got = p.try_place(blocks, a["block"], [a["raw"]["duration_minutes"]],
                                  p.meal_ranges if a["kind"] == "food" else ())
                if got is None:
                    return None
                blocks.append(got)

        ev = p.evaluate(blocks)
        if ev["problems"]:
            return None
        spend, cumulative, unknown = [], [], []
        for b in ev["sequence"]:
            if b.kind == "commitment":
                continue
            if b.cost is None:
                unknown.append(b.id)
            elif b.cost:
                spend.append((b.start, 1, b.cost, b.id))
        for tr in ev["transitions"]:
            if tr["cost"]:
                spend.append((tr["to"].start, 0, tr["cost"], f"travel:{tr['to'].id}"))
        total = 0
        for at, _, c, ref in sorted(spend):
            total += c
            cumulative.append({"at": p.fmt(at), "ref_id": ref, "cumulative_cents": total})
        if total > p.budget:
            return None
        cov = {t["id"]: (min(1000, placed[t["id"]] * 1000 // t["need"]) if t["need"] else 1000) for t in p.tasks}
        return Layout(variant, combo, ev["sequence"], ev["transitions"], placed, cov, total, unknown, cumulative)

    def defensible(self, lay, base):
        p, s = self.p, self.s
        crit = [t for t in p.tasks if t["tier"] == "critical"]
        if lay.variant in ("academic_first", "balanced", "baseline"):
            return sum(lay.cov[t["id"]] for t in crit) >= sum(base.cov[t["id"]] for t in crit)
        if lay.variant == "social_first":
            for t in crit:
                prot = min(1000, min(t["need"], s["protected_study_minutes"]) * 1000 // t["need"]) if t["need"] else 1000
                if lay.cov[t["id"]] < min(base.cov[t["id"]], prot):
                    return False
        return True

    def rank_key(self, lay):
        p = self.p
        wa, ws_ = VARIANT_WEIGHTS[lay.variant]
        weights = {t["id"]: ACADEMIC_WEIGHT[t["tier"]] for t in p.tasks}
        a_norm = sum(weights[i] * c for i, c in lay.cov.items()) // sum(weights.values()) if weights else 0
        s_norm = min(1000, 1000 * sum(a["score"] for a in lay.combo) // SOCIAL_SCORE_REF)
        n_prov = sum(a["state"] == "provisional" for a in lay.combo)
        combined = wa * a_norm + ws_ * s_norm - 30 * n_prov
        return (-combined, n_prov, lay.cost_total, tuple(sorted(a["id"] for a in lay.combo)))

    def run(self):
        base = self.layout("academic_first", ())
        self.baseline = base
        best = {}
        for v in VARIANTS:
            champion = None
            for combo in self.subsets(self.s["max_activities"][v]):
                lay = self.layout(v, combo)
                if lay is None or not self.defensible(lay, base):
                    continue
                lay.key = self.rank_key(lay)
                if champion is None or lay.key < champion.key:
                    champion = lay
            best[v] = champion
        plans, dropped = [], []
        for v in VARIANTS:
            lay = best[v]
            if lay is None:
                dropped.append({"variant": v, "reason": "VARIANT_NOT_DEFENSIBLE"})
                continue
            # A day with nothing to add is still a valid commitments-only schedule. Only unplaceable
            # unfinished work, or no commitments at all, leaves nothing to show.
            if not any(b.kind != "commitment" for b in lay.blocks) and (
                    not self.p.commitments or any(t["need"] for t in self.p.tasks)):
                dropped.append({"variant": v, "reason": "VARIANT_EMPTY"})
                continue
            twin = next((pl for pl in plans if self._same(pl["_layout"], lay)), None)
            if twin:
                twin["also_matches"].append(v)
                dropped.append({"variant": v, "reason": "VARIANT_DUPLICATE_OF", "duplicate_of": twin["variant"]})
                continue
            plans.append(self.render_plan(lay))
        baseline = self.baseline_plan(plans, base) if plans else None
        if baseline:
            plans.append(baseline)
        return plans, dropped

    def baseline_plan(self, plans, base):
        """Commitments plus recommended study only: no external events, no suggestions.

        Offered whenever it differs from the ordinary plans; an identical plan is not repeated."""
        lay = self.layout("balanced", ())
        if lay is None or not any(b.kind != "commitment" for b in lay.blocks):
            return None
        lay.key = self.rank_key(lay)
        lay.variant = "baseline"
        if any(self._same(pl["_layout"], lay) for pl in plans):
            return None
        plan = self.render_plan(lay)
        plan["baseline"] = True
        plan["label"] = "Baseline: your commitments and recommended study time only; no outside events"
        return plan

    @staticmethod
    def _same(x, y):
        if {a["id"] for a in x.combo} != {a["id"] for a in y.combo}:
            return False
        return abs(sum(x.placed.values()) - sum(y.placed.values())) < 30

    # -- output --------------------------------------------------------------------------

    def block_out(self, b):
        p = self.p
        out = {"type": b.kind, "title": b.title, "start": p.fmt(b.start), "end": p.fmt(b.end),
               "minutes": b.end - b.start, "location_id": b.loc, "status": b.status}
        a = self.by_id.get(b.id)
        if b.kind == "academic":
            t = p.task_by_id[b.ref]
            task = {"id": t["id"], "title": t["title"], "due": t["due"],
                    "origin": {"canvas": "canvas_assignment", "manual": "student_entered_task"}.get(t["source"], "unspecified_task"),
                    "source_id": t.get("source_id"), "url": t.get("url"), "synced_at": t.get("synced_at")}
            out.update(title="Suggested study: " + b.title, task_id=b.ref, origin="system_study_recommendation", recommendation=True)
            out["provenance"] = {"origin": "system_study_recommendation", "recommendation": True, "verified": False,
                                 "note": "Study time chosen by the planner; not a Calendar or Canvas fact.",
                                 "task": {k: v for k, v in task.items() if v is not None}}
            if b.provisional_effort:
                out["status"] = "provisional"
                out["notes"] = ["Provisional block: remaining effort is unknown, so this is time to start, not a promise to finish."]
        elif b.kind == "commitment":
            origin = COMMITMENT_ORIGIN.get(b.ref, "unspecified_commitment")
            out.update(ref_id=b.id, source=b.ref, origin=origin, recommendation=False)
            out["provenance"] = dict({"origin": origin, "source": b.ref, "recommendation": False}, **b.meta)
            if b.meta.get("tentative"):
                out["notes"] = ["Marked tentative on the calendar; treated as busy."]
        elif a["state"] == "fallback":
            out.update(ref_id=b.id, origin="system_suggestion", recommendation=True, duration_is_estimate=True,
                       notes=["Optional idea, not a verified event. The length is an estimate."])
            if (b.cost or 0) > 0:
                out.update(cost_cents=b.cost, cost_is_estimate=True)
            out["provenance"] = {"origin": "system_suggestion", "recommendation": True, "verified": False}
        else:
            origin = "verified_external_event" if a["state"] == "confirmed" else "unverified_external_event"
            src = a["raw"].get("source") or {}
            out.update(ref_id=b.id, cost_cents=b.cost, origin=origin, recommendation=False)
            if b.cost is None:
                out["cost_known"] = False
                out["notes"] = ["Cost unknown; not included in the spend total."]
            prov = {"origin": origin, "recommendation": False, "verified": a["state"] == "confirmed",
                    "source_url": src.get("url"), "source_name": src.get("name"), "source_id": src.get("id"),
                    "organizer_type": src.get("organizer_type"), "checked_at": src.get("checked_at"),
                    "verified_fields": sorted(set(a["verified_fields"])), "unverified_fields": sorted(set(a["unverified_fields"]))}
            out["provenance"] = {k: v for k, v in prov.items() if v is not None}
        out.setdefault("origin", out["provenance"]["origin"])
        return out

    def optional_suggestions(self, lay):
        """Ideas that would fit in this plan's free time. They are not scheduled and not events."""
        got, chosen = [], {a["id"] for a in lay.combo}
        for a in self.suggestions:
            if a["id"] in chosen or len(got) >= self.s["max_optional_suggestions"]:
                continue
            dur = a["raw"]["duration_minutes"]
            if self.p.try_place(lay.blocks, a["block"], [dur]) is None:
                continue
            item = {"id": a["id"], "title": a["title"], "origin": "system_suggestion", "verified": False,
                    "estimated_minutes": dur, "duration_is_estimate": True, "tags": a["raw"].get("tags", []),
                    "note": "Optional idea. Not a scheduled activity or a verified event."}
            if (a["raw"].get("cost_cents") or 0) > 0:
                item.update(estimated_cost_cents=a["raw"]["cost_cents"], cost_is_estimate=True)
            got.append(item)
        return got

    def render_plan(self, lay):
        p = self.p
        outs = {b.id: self.block_out(b) for b in lay.blocks}
        timeline = list(outs.values())
        assumed = False
        assumed_legs = assumed_minutes = 0
        for tr in lay.transitions:
            end = tr["to"].start - tr["buffer"]
            if tr["minutes"] > 0:
                leg = tr["leg"] or {}
                mode = leg.get("mode", "travel")
                known = tr["kind"] == "known"
                t = {"type": "travel", "title": f"{mode.capitalize()} to {tr['to'].title}",
                     "start": p.fmt(end - tr["minutes"]), "end": p.fmt(end), "minutes": tr["minutes"],
                     "from_location_id": tr["from_loc"], "to_location_id": tr["to_loc"], "cost_cents": tr["cost"],
                     "assumed": not known, "status": "confirmed" if known else "provisional",
                     "origin": "user_provided_travel" if known else "assumed_travel_estimate"}
                if known:
                    t["provenance"] = {k: v for k, v in {"origin": t["origin"], "assumed": False, "mode": leg.get("mode"),
                                                          "source": leg.get("source"), "checked_at": leg.get("checked_at")}.items() if v is not None}
                else:
                    assumed = True
                    assumed_legs += 1
                    assumed_minutes += tr["minutes"]
                    dest = outs[tr["to"].id]
                    dest["arrival_confirmed"] = False
                    dest.setdefault("notes", []).append("Getting here relies on an assumed travel time.")
                    t["notes"] = ["Travel time is an assumption; no estimate was provided for this trip."]
                    t["provenance"] = {"origin": t["origin"], "assumed": True, "recommendation": True,
                                       "basis": "unknown_travel_assumption_minutes setting"}
                timeline.append(t)
            if tr["buffer"] > 0:
                timeline.append({"type": "buffer", "title": "Break" if tr["from"].kind == tr["to"].kind == "academic" else "Buffer",
                                 "start": p.fmt(end), "end": p.fmt(tr["to"].start), "minutes": tr["buffer"], "status": "planned",
                                 "origin": "system_buffer", "provenance": {"origin": "system_buffer", "recommendation": True}})
        timeline.sort(key=lambda x: (x["start"], x["end"]))

        externals = [a for a in lay.combo if a["state"] in ("confirmed", "provisional")]
        suggested = [a for a in lay.combo if a["state"] == "fallback"]
        reasons = sorted({c for a in externals for c in a["reason_codes"] if a["state"] == "provisional"})
        if assumed:
            reasons.append("TRAVEL_ASSUMED")
        if lay.unknown_cost:
            reasons.append("COST_INCOMPLETE")
        availability = p.certainty["availability"]
        if availability != "verified_current":
            reasons.append("AVAILABILITY_NOT_VERIFIED")
        status = "schedule_only" if not externals else ("provisional" if reasons else "confirmed")
        unconfirmed = [a["title"] for a in externals if a["state"] == "provisional"]
        unknown_titles = [b.title for b in lay.blocks if b.id in lay.unknown_cost]
        certainty = {
            "events": "some_provisional" if unconfirmed else ("all_confirmed" if externals else "none"),
            "costs": "incomplete" if lay.unknown_cost else "complete",
            "travel": "assumed" if assumed else "known",
            "academic_effort": "provisional" if any(t["provisional"] and lay.placed[t["id"]] for t in p.tasks) else "known",
            "availability": availability,
            "academic_data": p.certainty["academic_data"],
        }
        gaps = []
        if unconfirmed:
            gaps.append(f"{len(unconfirmed)} event/option(s) still need verification")
        if assumed:
            gaps.append(f"{assumed_legs} travel time(s) are assumed")
        if lay.unknown_cost:
            gaps.append(f"{len(lay.unknown_cost)} cost(s) are unknown")
        if "AVAILABILITY_NOT_VERIFIED" in reasons:
            gaps.append({"student_reported": "availability rests on commitments the student reported; Calendar was not read",
                         "stale": "Calendar data is stale", "partial": "not every authorized calendar was read",
                         "coverage_unknown": "Calendar coverage was not confirmed complete",
                         "sync_time_unknown": "the Calendar sync time is unknown"}.get(availability, "Calendar availability could not be checked"))
        missing = [f"{g['source']} ({g['state'].replace('_', ' ')})" for g in p.data_gaps if g["severity"] in ("missing", "stale", "unknown")]
        if status == "schedule_only":
            status_detail = ("Schedule only: your commitments" + (" plus recommended study time" if sum(lay.placed.values()) else "")
                             + ". No outside events are included, and nothing here is independently verified.")
        elif gaps:
            status_detail = "Not fully confirmed: " + "; ".join(gaps) + "."
        else:
            status_detail = "Verified events, current calendar, known travel times and complete costs."
        if missing:
            status_detail += " Not checked or incomplete: " + ", ".join(missing) + "."

        coverage, risk_rank = [], {"none": 0, "medium": 1, "high": 2}
        worst = "none"
        for t in p.tasks:
            got, need = lay.placed[t["id"]], t["need"]
            codes = list(t["reason_codes"])
            if need == 0:
                cv, risk = "ready_to_submit", "none"
            elif t["provisional"]:
                cv = "provisional_progress" if got else "uncovered"
                risk = ("high" if t["tier"] == "critical" else "medium") if not got else ("medium" if t["tier"] == "critical" else "none")
            elif got >= need:
                cv, risk = "covered", "none"
            elif got:
                cv, risk = "partial", "high" if t["tier"] == "critical" else "medium"
                codes.append("ACADEMIC_PARTIAL")
            else:
                cv, risk = "uncovered", "high" if t["tier"] == "critical" else "medium"
            if cv == "uncovered":
                codes.append("ACADEMIC_UNCOVERED")
            worst = max(worst, risk, key=risk_rank.get)
            coverage.append({"task_id": t["id"], "title": t["title"], "due": t["due"], "tier": t["tier"],
                             "needed_minutes": None if t["provisional"] else need,
                             "allocated_minutes": got, "effort_source": t["effort_source"],
                             "coverage": cv, "risk": risk, "reason_codes": list(dict.fromkeys(codes))})

        evidence = []
        for a in suggested:
            evidence.append({"id": a["id"], "title": a["title"], "source": "system suggestion",
                             "note": "Not a verified event; no source is claimed."})
        for a in externals:
            src = a["raw"].get("source") or {}
            evidence.append({"id": a["id"], "title": a["title"], "source_url": src.get("url"), "source_name": src.get("name"),
                             "organizer_type": src.get("organizer_type"), "checked_at": src.get("checked_at"),
                             "status": a["state"], "verified_fields": sorted(set(a["verified_fields"])),
                             "unverified_fields": sorted(set(a["unverified_fields"])), "reason_codes": a["reason_codes"]})

        questions = [q for q in p.questions
                     if q["code"] not in ("QUESTION_VERIFY_OPTION",) or q.get("candidate") in {a["id"] for a in externals}]
        actions = []
        for a in externals:
            if a["state"] == "provisional":
                actions.append({"action": "verify_details", "target": a["id"], "requires_approval": False,
                                "detail": f"Check {', '.join(sorted(set(a['unverified_fields']))) or 'listing'} with the organizer or venue."})
            if "REGISTRATION_REQUIRED" in a["reason_codes"]:
                actions.append({"action": "register_or_book", "target": a["id"], "requires_approval": True,
                                "detail": "Student must approve any sign-up; the student handles payment."})
            if "social" in a["raw"].get("tags", []):
                actions.append({"action": "draft_invitation", "target": a["id"], "requires_approval": True,
                                "detail": "Draft only; the student sends it."})
        if any(t["need"] for t in p.tasks):
            actions.append({"action": "prepare_study_materials", "requires_approval": False})
        actions.append({"action": "add_to_google_calendar", "requires_approval": True})
        actions.append({"action": "set_reminders", "requires_approval": True})

        known = lay.cost_total
        spans = [(b.start, b.end) for b in lay.blocks]
        for tr in lay.transitions:
            edge = tr["to"].start - tr["buffer"]
            spans += [(edge - tr["minutes"], tr["to"].start)] if tr["minutes"] or tr["buffer"] else []
        unscheduled = (p.we - p.ws) - _union_minutes(spans, p.ws, p.we)
        names = [a["title"] for a in externals]
        study = sum(lay.placed.values())
        summary = (f"{study} min of recommended study" if study else "Your commitments only; nothing else scheduled") + (" + " + ", ".join(names) if names else "")
        if suggested:
            summary += f" (+{len(suggested)} optional suggestion(s))"
        return {
            "variant": lay.variant, "also_matches": [], "status": status, "summary": summary,
            "status_detail": status_detail, "certainty": certainty, "data_gaps": p.data_gaps,
            "optional_suggestions": self.optional_suggestions(lay),
            "assumed_travel": {"legs": assumed_legs, "minutes": assumed_minutes},
            "timeline": timeline, "unscheduled_minutes": unscheduled,
            "totals": {"budget_cents": p.budget, "known_cost_cents": known, "known_planned_spend_cents": known,
                       "remaining_budget_cents": p.budget - known, "unknown_cost_items": lay.unknown_cost,
                       "unknown_cost_titles": unknown_titles, "cost_complete": not lay.unknown_cost,
                       "remaining_budget_is_upper_bound": bool(lay.unknown_cost), "cumulative": lay.cumulative},
            "critical_conflicts": [c["task_id"] for c in coverage if c["tier"] == "critical" and c["coverage"] in ("uncovered", "partial")],
            "academic_coverage": coverage, "academic_risk": worst,
            "evidence": evidence, "provisional_reasons": reasons,
            "unresolved_questions": questions, "next_actions": actions,
            "rank_key": list(lay.key[:1]), "_layout": lay,
        }


# ----------------------------------------------------------------------------
# Entry points
# ----------------------------------------------------------------------------

def fingerprint(data):
    return hashlib.sha256(json.dumps(data, sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:16]


def apply_changes(data, changes):
    """Return a copy of `data` with a student's change applied, ready to replan.

    Supported keys: budget_cents, window, as_of, preferences (merged), settings (merged),
    reject_candidates, unavailable_candidates, candidate_updates {id: fields},
    task_updates {id: fields}, add_commitments, remove_commitments, add_candidates,
    add_tasks."""
    d = copy.deepcopy(data)
    for k in ("budget_cents", "window", "as_of"):
        if k in changes:
            d[k] = changes[k]
    for k in ("preferences", "settings"):
        if k in changes:
            d.setdefault(k, {}).update(changes[k])
    fb = d.setdefault("student_feedback", {})
    for src, dst in (("reject_candidates", "rejected_candidate_ids"), ("unavailable_candidates", "unavailable_candidate_ids")):
        fb[dst] = sorted(set(fb.get(dst, [])) | set(changes.get(src, [])))
    for key, field_ in (("candidate_updates", "candidates"), ("task_updates", "academic_tasks")):
        for ident, fields in changes.get(key, {}).items():
            for item in d.get(field_, []):
                if item.get("id") == ident:
                    item.update(fields)
    d["fixed_commitments"] = [c for c in d.get("fixed_commitments", []) if c.get("id") not in set(changes.get("remove_commitments", []))]
    d["fixed_commitments"] += changes.get("add_commitments", [])
    d.setdefault("candidates", []).extend(changes.get("add_candidates", []))
    d.setdefault("academic_tasks", []).extend(changes.get("add_tasks", []))
    return d


def compose(data, changes=None):
    base_fp = fingerprint(data)
    if changes:
        data = apply_changes(data, changes)
    result = {"schema_version": SCHEMA_VERSION, "input_fingerprint": fingerprint(data)}
    if changes:
        result["replan"] = {"previous_fingerprint": base_fp, "changed_keys": sorted(changes)}
    p = Problem(data)
    if p.errors:
        result["data_origin"] = data.get("data_origin")
        result.update(status="invalid_input", errors=p.errors, warnings=p.warnings, plans=[],
                      explanation="The input could not be used: " + "; ".join(e["message"] for e in p.errors[:3]))
        return result

    result["data_origin"] = data.get("data_origin")
    if str(data.get("data_origin", "")).startswith("synthetic"):
        p.warnings.insert(0, issue("SYNTHETIC_DATA", "This input is marked synthetic sample data. It is not a real student's day."))
    comp = Composer(p)
    plans, dropped = comp.run()
    base = comp.baseline

    free = (p.we - p.ws) - _busy_minutes(p)
    academic_risk = []
    for t in p.tasks:
        cov = base.placed[t["id"]]
        if t["need"] and (cov < t["need"] or t["provisional"]):
            code = "ACADEMIC_UNCOVERED" if cov == 0 else ("ACADEMIC_EFFORT_UNKNOWN" if t["provisional"] else "ACADEMIC_PARTIAL")
            academic_risk.append({"task_id": t["id"], "code": code, "tier": t["tier"],
                                  "best_case_minutes": cov, "needed_minutes": None if t["provisional"] else t["need"]})
    critical_conflicts = []
    cap_left = p.settings["max_academic_minutes"] - sum(base.placed.values())
    for t in p.tasks:
        got = base.placed[t["id"]]
        if t["tier"] == "critical" and t["need"] and got < t["need"]:
            cap_hit = cap_left < min(p.settings["min_study_block_minutes"], t["need"] - got)
            cause = "DAILY_STUDY_CAP" if cap_hit else ("NO_TIME_BEFORE_DEADLINE" if t["latest_end"] is not None and t["latest_end"] <= p.we else "NOT_ENOUGH_FREE_TIME")
            critical_conflicts.append({"task_id": t["id"], "title": t["title"], "due": t["due"],
                                       "code": "CRITICAL_TASK_UNSCHEDULABLE" if got == 0 else "CRITICAL_TASK_PARTIAL",
                                       "cause": cause, "best_case_minutes": got, "needed_minutes": t["need"],
                                       "effort_source": t["effort_source"]})
            p.warnings.append(issue("CRITICAL_ACADEMIC_CONFLICT",
                                    f"'{t['title']}' is urgent but only {got} of {t['need']} minutes can be scheduled ({cause}).", task=t["id"]))
    for t in p.status_check_tasks:
        if t.get("due_soon"):
            academic_risk.append({"task_id": t["id"], "code": "ACADEMIC_STATUS_UNKNOWN", "tier": "unknown"})

    def public(t):
        return {k: v for k, v in t.items() if k not in ("due_epoch", "latest_end")}

    for pl in plans:
        pl.pop("_layout")
    event_plans = [pl for pl in plans if not pl.get("baseline")]
    rec = None
    for pl in event_plans:
        if pl["variant"] == "balanced" or "balanced" in pl["also_matches"]:
            rec = pl["variant"]
    rec = rec or (event_plans[0]["variant"] if event_plans else None)

    reasons = []
    if not plans:
        if not p.commitments and not any(t["need"] for t in p.tasks):
            reasons.append("EMPTY_DAY")
        else:
            if free < p.settings["min_study_block_minutes"]:
                reasons.append("NO_FREE_TIME")
            if p.tasks and not any(base.placed.values()):
                reasons.append("ACADEMIC_UNCOVERED")
            reasons = reasons or ["NO_FEASIBLE_COMBINATION"]
    status = ("empty_day" if reasons == ["EMPTY_DAY"] else "no_feasible_plan" if not plans else "ok" if any(pl["status"] == "confirmed" for pl in plans)
              else "provisional_only" if any(pl["status"] == "provisional" for pl in plans) else "schedule_only")

    result.update({
        "status": status,
        "planning": {"date": str(p.planning_date), "timezone": str(p.tz), "window_start": p.fmt(p.ws),
                     "window_end": p.fmt(p.we), "as_of": p.fmt(p.as_of), "free_minutes": free,
                     "free_time_meaning": "no known calendar conflict (not guaranteed free)",
                     "availability_certainty": p.certainty["availability"],
                     "campus": (p.data.get("student") or {}).get("campus")},
        "errors": [], "warnings": p.warnings, "questions": p.questions,
        "source_status": p.source_status, "data_gaps": p.data_gaps, "ignored_commitments": p.ignored_commitments,
        "academic": {"unfinished": [public(t) for t in p.tasks], "excluded": [public(t) for t in p.excluded_tasks],
                     "deferred": [public(t) for t in p.deferred_tasks], "needs_status_check": [public(t) for t in p.status_check_tasks],
                     "risk_flags": academic_risk, "critical_conflicts": critical_conflicts},
        "options": [{"id": a["id"], "title": a["title"], "kind": a["kind"], "state": a["state"],
                     "reason_codes": a["reason_codes"], "notes": a["notes"], "placeable": a["placeable"],
                     "considered": a.get("considered", False), "suggestion": bool(a.get("suggestion")),
                     "verified_fields": sorted(set(a["verified_fields"])), "unverified_fields": sorted(set(a["unverified_fields"]))}
                    for a in p.assessments],
        "plans": plans, "dropped_variants": dropped, "recommended_variant": rec,
        "reliable_fallback_variant": next((pl["variant"] for pl in plans if pl["status"] == "schedule_only"), None),
        "no_plan_reasons": reasons,
        "diagnostics": {"rejected_combinations": sorted(p.rejected_combos.values(), key=lambda r: r["candidates"])[:10],
                        "commitment_overlaps": p.commitment_overlaps},
    })
    result["explanation"] = _explain(result)
    return result


def _busy_minutes(p):
    return _union_minutes([(c.start, c.end) for c in p.commitments], p.ws, p.we)


def _union_minutes(spans, lo, hi):
    spans = sorted((max(a, lo), min(b, hi)) for a, b in spans if b > lo and a < hi)
    total, cur_end = 0, None
    for a, b in spans:
        if cur_end is None or a > cur_end:
            total += b - a
            cur_end = b
        elif b > cur_end:
            total += b - cur_end
            cur_end = b
    return total


def _explain(r):
    if r["plans"]:
        n = len(r["plans"])
        base = f"{n} plan{'s' if n != 1 else ''} found; recommended: {r['recommended_variant']}."
        if r["academic"]["critical_conflicts"]:
            base += " Urgent academic work cannot be fully scheduled today: " + "; ".join(
                f"{c['title']} ({c['best_case_minutes']} of {c['needed_minutes']} min)" for c in r["academic"]["critical_conflicts"]) + "."
        elif r["academic"]["risk_flags"]:
            base += " Some academic work is not fully covered today."
        if r["status"] == "provisional_only":
            base += " Every plan with outside events relies on something unconfirmed."
        if r["status"] == "schedule_only":
            base += " No outside events are included: this is your commitments plus recommended study time."
            if not any(b["type"] == "academic" for pl in r["plans"] for b in pl["timeline"]):
                base += " Nothing is due soon, so the rest of the day is left unscheduled."
        elif r["reliable_fallback_variant"]:
            base += " A schedule-only plan (no outside events) is also included."
        missing = [g["source"] for g in r["data_gaps"] if g["severity"] in ("missing", "stale", "unknown")]
        if missing:
            base += " Not checked or incomplete: " + ", ".join(dict.fromkeys(missing)) + "."
        return base
    why = {
        "NO_FREE_TIME": "Your fixed commitments leave no usable free time in the window.",
        "EMPTY_DAY": "No commitments, unfinished work or outside events are known for this window. That does not mean the day is free; see the data gaps for what could not be checked.",
        "ACADEMIC_UNCOVERED": "Unfinished work is due but it could not be placed in the free time available.",
        "NO_FEASIBLE_COMBINATION": "No combination of options fits the time, travel and budget limits.",
    }
    text = " ".join(why[c] for c in r["no_plan_reasons"])
    if r["academic"]["critical_conflicts"]:
        text += " Urgent work at risk: " + "; ".join(c["title"] for c in r["academic"]["critical_conflicts"]) + "."
    return text


# ----------------------------------------------------------------------------
# Text rendering and CLI
# ----------------------------------------------------------------------------

def money(c):
    return f"${c // 100}.{c % 100:02d}"


def clock(iso, date):
    t = iso[11:16]
    return t if iso[:10] == date else f"{t} (+{(datetime.fromisoformat(iso[:10]).date() - datetime.fromisoformat(date).date()).days}d)"


ORIGIN_TAG = {
    "calendar_commitment": "calendar", "manual_commitment": "student-entered", "unspecified_commitment": "source not stated",
    "system_study_recommendation": "recommended", "verified_external_event": "verified event",
    "unverified_external_event": "UNVERIFIED event", "system_suggestion": "idea, not an event",
}
SOURCE_GAP_SKIP = ("CALENDAR_UNAVAILABLE", "CANVAS_NOT_CONNECTED", "CANVAS_DATA_STALE", "CANVAS_SYNC_TIME_UNKNOWN")


def render_text(r):
    if r["status"] == "invalid_input":
        return "Cannot plan: " + r["explanation"]
    out = []
    date = r["planning"]["date"]
    for w in r["warnings"]:
        if w["code"] not in SOURCE_GAP_SKIP:
            out.append(f"! {w['message']}")
    out.append(f"Free time = {r['planning']['free_time_meaning']}")
    for g in r["data_gaps"]:
        out.append(f"* {g['source']} - {g['state'].replace('_', ' ')}: {g['message']}")
    if r["status"] == "provisional_only":
        out.append("! Every plan with outside events relies on something unconfirmed.")
    if r["academic"]["excluded"]:
        out.append("Already done (not scheduled): " + ", ".join(t["title"] for t in r["academic"]["excluded"]))
    if r["status"] == "empty_day":
        out.append("Nothing to schedule: " + r["explanation"])
    elif not r["plans"]:
        out.append("No full plan: " + r["explanation"])
    for pl in r["plans"]:
        tag = " (recommended)" if pl["variant"] == r["recommended_variant"] else ""
        out += ["", f"== {pl['variant']}{tag} - {pl['status']} ==", pl["summary"], pl["status_detail"]]
        for cid in pl["critical_conflicts"]:
            out.append(f"!! Urgent work not fully scheduled: {next(c['title'] for c in pl['academic_coverage'] if c['task_id'] == cid)}")
        for b in pl["timeline"]:
            if b["type"] == "buffer":
                continue
            cost = f" [{money(b['cost_cents'])}]" if b.get("cost_cents") else (" [cost unknown]" if b.get("cost_known") is False else "")
            flag = " ~assumed" if b.get("assumed") else (" ?provisional" if b["status"] == "provisional" else "")
            label = ORIGIN_TAG.get(b["origin"])
            label = f" ({label})" if label else ""
            out.append(f"{clock(b['start'], date)}-{clock(b['end'], date)}  {b['title']}{cost}{flag}{label}")
        out.append(f"Unscheduled time left open: {pl['unscheduled_minutes']} min")
        t = pl["totals"]
        if t["cost_complete"]:
            out.append(f"Spend {money(t['known_cost_cents'])} of {money(t['budget_cents'])}, left {money(t['remaining_budget_cents'])}")
        else:
            out.append(f"Known spend {money(t['known_cost_cents'])} of {money(t['budget_cents'])}; at most {money(t['remaining_budget_cents'])} left, "
                       f"not counting unknown cost: {', '.join(t['unknown_cost_titles'])}")
        for c in pl["academic_coverage"]:
            out.append(f"Academic: {c['title']} - {c['coverage'].replace('_', ' ')} (risk {c['risk']})")
        for e in pl["evidence"]:
            if e.get("source_url"):
                out.append(f"Source: {e['title']} - {e['source_url']} (checked {e['checked_at']}; {e['status']})")
        if pl["optional_suggestions"]:
            out.append("Optional ideas (not scheduled, not events): " + "; ".join(
                f"{x['title']} (~{x['estimated_minutes']} min, estimate)" for x in pl["optional_suggestions"]))
        for q in pl["unresolved_questions"]:
            out.append(f"? {q['message']}")
    return "\n".join(out)


def load_json(path):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError) as e:
        sys.exit(f"Cannot read JSON from {path}: {e}")


def main(argv=None):
    ap = argparse.ArgumentParser(description="What's the Move? deterministic day composer")
    ap.add_argument("--input", help="day input JSON")
    ap.add_argument("--sample", action="store_true", help="use the bundled synthetic sample (no credentials, no network)")
    ap.add_argument("--changes", help="JSON of student changes to apply before replanning")
    ap.add_argument("--format", choices=("json", "text"), default="json")
    ap.add_argument("--out", help="also write the full JSON result to this path")
    args = ap.parse_args(argv)
    if args.sample == bool(args.input):
        ap.error("give exactly one of --input or --sample")
    path = Path(__file__).resolve().parent.parent / "assets" / "sample_input.json" if args.sample else args.input
    changes = load_json(args.changes) if args.changes else None
    result = compose(load_json(path), changes)
    if args.out:
        Path(args.out).write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    if args.sample and args.format == "text":
        print("SYNTHETIC SAMPLE DATA - not a real student's day\n")
    print(render_text(result) if args.format == "text" else json.dumps(result, indent=2, sort_keys=True))
    return 2 if result["status"] == "invalid_input" else 0


if __name__ == "__main__":
    sys.exit(main())
