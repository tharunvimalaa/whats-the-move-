"""Deterministic tests for the What's the Move? Day Composer (synthetic data only).

Run from the repository root:  python3 -m unittest discover -s tests -v
"""
import copy
import json
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "whats-the-move" / "scripts" / "compose_day.py"
sys.path.insert(0, str(SCRIPT.parent))

import compose_day as cd  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures"
VERIFIED = {"time": "verified", "price": "verified", "availability": "verified", "hours": "verified"}


def load(name):
    return json.loads((FIXTURES / name).read_text())


def source(slug="x", kind="organizer", checked="2026-10-26T08:00:00-07:00"):
    return {"url": f"https://events.example.edu/{slug}", "name": slug, "organizer_type": kind, "checked_at": checked}


def event(cid, start, end, loc="mu_lawn", cost=0, **kw):
    d = {"id": cid, "kind": "event", "title": cid, "start": start, "end": end, "cost_cents": cost,
         "location_id": loc, "tags": ["social"], "listing_status": "active", "availability": "open",
         "source": source(cid), "verification": dict(VERIFIED)}
    d.update(kw)
    return d


def venue(cid, minutes, cost, loc, opens="2026-10-26T09:00:00", closes="2026-10-26T21:00:00", kind="activity", **kw):
    d = {"id": cid, "kind": kind, "title": cid, "duration_minutes": minutes, "cost_cents": cost, "location_id": loc,
         "availability": "open", "hours": [{"open": opens, "close": closes}], "source": source(cid, "venue"),
         "verification": dict(VERIFIED)}
    d.update(kw)
    return d


def base():
    return load("base_day.json")


def only(d, candidates):
    d = copy.deepcopy(d)
    d["candidates"] = candidates
    return d


def plan_of(r, variant):
    for p in r["plans"]:
        if p["variant"] == variant or variant in p["also_matches"]:
            return p
    raise AssertionError(f"no {variant} plan in {[p['variant'] for p in r['plans']]}")


def option(r, cid):
    return next(o for o in r["options"] if o["id"] == cid)


def ids_in(plan):
    return {b.get("ref_id") for b in plan["timeline"]} | {b.get("task_id") for b in plan["timeline"]}


class PlanAssertions(unittest.TestCase):
    def assert_plan_sound(self, r, plan):
        """Independent check: no overlaps, inside the window, spend within budget."""
        blocks = [b for b in plan["timeline"] if b["type"] != "buffer"]
        blocks.sort(key=lambda b: (b["start"], b["end"]))
        end_max = None
        for b in blocks:
            if b["type"] == "commitment":
                continue
            for o in blocks:
                if o is not b and o["start"] < b["end"] and b["start"] < o["end"]:
                    self.fail(f"overlap: {b['title']} vs {o['title']}")
            self.assertGreaterEqual(b["start"], r["planning"]["window_start"])
            self.assertLessEqual(b["end"], r["planning"]["window_end"])
        t = plan["totals"]
        self.assertLessEqual(t["known_cost_cents"], t["budget_cents"])
        for step in t["cumulative"]:
            self.assertLessEqual(step["cumulative_cents"], t["budget_cents"])


class TestScenarios(PlanAssertions):
    # 1 -----------------------------------------------------------------------------
    def test_01_academic_and_social_both_fit(self):
        r = cd.compose(base())
        self.assertEqual(r["status"], "ok")
        bal = plan_of(r, "balanced")
        self.assertEqual(r["recommended_variant"], "balanced")
        covered = {c["task_id"]: c["coverage"] for c in bal["academic_coverage"]}
        self.assertEqual(covered, {"t_essay": "covered", "t_quiz": "covered"})
        self.assertTrue([b for b in bal["timeline"] if b["type"] in ("event", "food", "activity", "place")])
        self.assertEqual(bal["status"], "confirmed")
        for p in r["plans"]:
            self.assert_plan_sound(r, p)
        ordinary = [p for p in r["plans"] if not p.get("baseline")]
        self.assertEqual({p["variant"] for p in ordinary} | {m for p in ordinary for m in p["also_matches"]} | {d["variant"] for d in r["dropped_variants"]}, set(cd.VARIANTS))
        self.assertEqual(r["plans"][-1]["variant"], "baseline")
        self.assertEqual(r["plans"][-1]["status"], "schedule_only")

    # 2 -----------------------------------------------------------------------------
    def tight_day(self, essay_status):
        d = only(base(), [event("ev_morning", "2026-10-26T09:30:00", "2026-10-26T10:30:00")])
        d["fixed_commitments"] = []
        d["window"]["end"] = "2026-10-26T11:00:00"
        for t in d["academic_tasks"]:
            if t["id"] == "t_essay":
                t["submission_status"] = essay_status
        return d

    def test_02_urgent_assignment_changes_priority(self):
        free = cd.compose(self.tight_day("submitted"))
        self.assertIn("ev_morning", ids_in(plan_of(free, "social_first")))
        urgent = cd.compose(self.tight_day("unsubmitted"))
        for p in urgent["plans"]:
            self.assertNotIn("ev_morning", ids_in(p), "the activity may not displace a critical assignment")
        first = plan_of(urgent, "academic_first")
        essay = next(c for c in first["academic_coverage"] if c["task_id"] == "t_essay")
        self.assertEqual(essay["coverage"], "covered")
        self.assertEqual(first["timeline"][0]["task_id"], "t_essay")
        # The less urgent quiz no longer fits and the risk is flagged rather than hidden.
        quiz = next(c for c in first["academic_coverage"] if c["task_id"] == "t_quiz")
        self.assertEqual(quiz["coverage"], "uncovered")
        self.assertIn("ACADEMIC_UNCOVERED", quiz["reason_codes"])
        self.assertTrue(any(f["task_id"] == "t_quiz" for f in urgent["academic"]["risk_flags"]))

    # 3 -----------------------------------------------------------------------------
    def test_03_finished_work_is_not_scheduled(self):
        d = base()
        d["academic_tasks"] += [
            {"id": "t_graded", "title": "Graded", "due": "2026-10-26T20:00:00", "submission_status": "graded", "remaining_minutes": 60},
            {"id": "t_excused", "title": "Excused", "due": "2026-10-26T20:00:00", "submission_status": "excused", "remaining_minutes": 60}]
        r = cd.compose(d)
        excluded = {t["id"]: t["reason_codes"] for t in r["academic"]["excluded"]}
        self.assertEqual(excluded["t_lab"], ["ACADEMIC_SUBMITTED"])
        self.assertEqual(excluded["t_graded"], ["ACADEMIC_GRADED"])
        self.assertEqual(excluded["t_excused"], ["ACADEMIC_EXCUSED"])
        for p in r["plans"]:
            self.assertFalse({"t_lab", "t_graded", "t_excused"} & {c["task_id"] for c in p["academic_coverage"]})
            self.assertFalse({"t_lab", "t_graded", "t_excused"} & ids_in(p))
        # Unknown status is asked about, not assumed unfinished; far-off work is deferred.
        self.assertEqual([t["id"] for t in r["academic"]["needs_status_check"]], ["t_reading"])
        self.assertTrue(any(q["code"] == "QUESTION_TASK_STATUS" for q in r["questions"]))
        self.assertEqual([t["id"] for t in r["academic"]["deferred"]], ["t_project"])

    # 4 -----------------------------------------------------------------------------
    def test_04_combined_spending_over_budget_is_rejected(self):
        d = only(base(), [venue("act_climb", 90, 1500, "climb"), venue("food_tacos", 45, 1100, "tacos", kind="food")])
        d["budget_cents"] = 2500
        r = cd.compose(d)
        self.assertEqual(option(r, "act_climb")["state"], "confirmed")  # each is affordable alone
        for p in r["plans"]:
            self.assertFalse({"act_climb", "food_tacos"} <= ids_in(p))
            self.assert_plan_sound(r, p)
        combo = [c for c in r["diagnostics"]["rejected_combinations"] if c["candidates"] == ["act_climb", "food_tacos"]]
        self.assertEqual(combo[0]["reason"], "COMBINED_COST_EXCEEDS_BUDGET")
        self.assertEqual(combo[0]["total_cents"], 2600)
        # One item that is unaffordable by itself is rejected outright.
        d["budget_cents"] = 1000
        r = cd.compose(d)
        self.assertEqual(option(r, "act_climb")["reason_codes"], ["COST_EXCEEDS_BUDGET"])
        self.assertEqual(option(r, "food_tacos")["reason_codes"], ["COST_EXCEEDS_BUDGET"])

    def test_04b_cumulative_spend_matches_remaining_budget(self):
        r = cd.compose(base())
        p = plan_of(r, "social_first")
        spent = sum(b.get("cost_cents") or 0 for b in p["timeline"])
        self.assertEqual(p["totals"]["known_cost_cents"], spent)
        self.assertEqual(p["totals"]["remaining_budget_cents"], 3000 - spent)
        self.assertEqual(p["totals"]["cumulative"][-1]["cumulative_cents"], spent)

    def test_04c_travel_fares_count_toward_budget(self):
        d = only(base(), [venue("act_climb", 90, 1500, "climb")])
        d["travel"]["legs"] = [dict(l, cost_cents=300) if "climb" in (l["from"], l["to"]) else l for l in d["travel"]["legs"]]
        d["budget_cents"] = 1700  # 1500 + a 300 fare would not fit
        r = cd.compose(d)
        for p in r["plans"]:
            self.assertNotIn("act_climb", ids_in(p))

    # 5 -----------------------------------------------------------------------------
    def test_05_activity_conflicts_with_class_or_shift(self):
        d = only(base(), [
            event("ev_in_class", "2026-10-26T11:00:00", "2026-10-26T12:00:00", loc="bldg_class"),
            event("ev_in_shift", "2026-10-26T18:00:00", "2026-10-26T19:00:00", loc="job_site"),
            venue("closed_while_free", 45, 0, "cafe", opens="2026-10-26T11:00:00", closes="2026-10-26T11:40:00")])
        r = cd.compose(d)
        self.assertEqual(option(r, "ev_in_class")["reason_codes"], ["ACTIVITY_CONFLICTS_COMMITMENT"])
        self.assertEqual(option(r, "ev_in_shift")["reason_codes"], ["ACTIVITY_CONFLICTS_COMMITMENT"])
        self.assertEqual(option(r, "closed_while_free")["state"], "rejected")
        for p in r["plans"]:
            self.assertFalse({"ev_in_class", "ev_in_shift"} & ids_in(p))

    def test_05b_overlapping_commitments_are_reported(self):
        d = base()
        d["fixed_commitments"].append({"id": "double", "title": "Club meeting", "start": "2026-10-26T11:00:00", "end": "2026-10-26T12:00:00", "location_id": "library"})
        r = cd.compose(d)
        self.assertIn("COMMITMENT_OVERLAP", [w["code"] for w in r["warnings"]])
        self.assertEqual(r["diagnostics"]["commitment_overlaps"], [{"a": "class_prog", "b": "double"}])

    # 6 -----------------------------------------------------------------------------
    def test_06_required_travel_exceeds_available_time(self):
        d = only(base(), [
            event("ev_too_far", "2026-10-26T11:50:00", "2026-10-26T12:30:00", loc="downtown"),   # 5 min gap, 22 min walk
            event("ev_too_tight", "2026-10-26T12:10:00", "2026-10-26T12:50:00", loc="downtown")])  # 25 min gap < 22 + 10
        r = cd.compose(d)
        self.assertEqual(option(r, "ev_too_far")["reason_codes"], ["TRAVEL_EXCEEDS_AVAILABLE_TIME"])
        self.assertEqual(option(r, "ev_too_tight")["reason_codes"], ["TRANSITION_BUFFER_TOO_TIGHT"])

    def test_06b_chained_events_need_travel_between_them(self):
        d = only(base(), [
            event("ev_a", "2026-10-26T12:30:00", "2026-10-26T13:00:00", loc="mu_lawn"),
            event("ev_b", "2026-10-26T13:05:00", "2026-10-26T13:45:00", loc="downtown")])
        r = cd.compose(d)
        for p in r["plans"]:
            self.assertFalse({"ev_a", "ev_b"} <= ids_in(p), "5 minutes is not enough to reach downtown")

    # 7 -----------------------------------------------------------------------------
    def test_07_outdated_or_canceled_listings(self):
        d = only(base(), [
            event("ev_canceled", "2026-10-26T13:00:00", "2026-10-26T14:00:00", listing_status="canceled"),
            event("ev_postponed", "2026-10-26T13:00:00", "2026-10-26T14:00:00", listing_status="postponed"),
            event("ev_past", "2026-10-25T13:00:00", "2026-10-25T14:00:00"),
            event("ev_stale", "2026-10-26T13:00:00", "2026-10-26T14:00:00", source=source("stale", checked="2026-10-23T08:00:00-07:00")),
            event("ev_changed", "2026-10-26T13:00:00", "2026-10-26T14:00:00", listing_status="rescheduled"),
            event("ev_soldout", "2026-10-26T13:00:00", "2026-10-26T14:00:00", availability="sold_out")])
        r = cd.compose(d)
        self.assertEqual(option(r, "ev_canceled")["reason_codes"], ["EVENT_CANCELED"])
        self.assertEqual(option(r, "ev_postponed")["reason_codes"], ["EVENT_POSTPONED"])
        self.assertEqual(option(r, "ev_past")["reason_codes"], ["EVENT_ALREADY_ENDED"])
        self.assertEqual(option(r, "ev_soldout")["reason_codes"], ["SOLD_OUT"])
        self.assertEqual((option(r, "ev_stale")["state"], option(r, "ev_stale")["reason_codes"]), ("provisional", ["STALE_CHECK"]))
        self.assertEqual((option(r, "ev_changed")["state"], option(r, "ev_changed")["reason_codes"]), ("provisional", ["CHANGES_REPORTED"]))
        for p in r["plans"]:
            self.assertFalse({"ev_canceled", "ev_postponed", "ev_past", "ev_soldout"} & ids_in(p))

    # 8 -----------------------------------------------------------------------------
    def test_08_unverified_price_or_availability_is_provisional(self):
        d = only(base(), [
            event("ev_price", "2026-10-26T12:30:00", "2026-10-26T13:30:00", cost=500, verification={"time": "verified", "price": "unverified", "availability": "verified"}),
            event("ev_avail", "2026-10-26T14:00:00", "2026-10-26T15:00:00", availability="unknown"),
            event("ev_agg", "2026-10-26T15:30:00", "2026-10-26T16:30:00", source=source("agg", kind="aggregator")),
            event("ev_nosrc", "2026-10-26T14:00:00", "2026-10-26T15:00:00", source={"name": "blog"})])
        r = cd.compose(d)
        self.assertEqual(option(r, "ev_price")["state"], "provisional")
        self.assertIn("PRICE_UNVERIFIED", option(r, "ev_price")["reason_codes"])
        self.assertIn("price", option(r, "ev_price")["unverified_fields"])
        self.assertIn("time", option(r, "ev_price")["verified_fields"])
        self.assertIn("AVAILABILITY_UNKNOWN", option(r, "ev_avail")["reason_codes"])
        # Claims of "verified" from an aggregator do not count as verification.
        self.assertIn("ONLY_AGGREGATOR_SOURCE", option(r, "ev_agg")["reason_codes"])
        self.assertEqual(option(r, "ev_agg")["verified_fields"], [])
        self.assertEqual(option(r, "ev_nosrc")["state"], "rejected")
        self.assertIn("MISSING_SOURCE_URL", option(r, "ev_nosrc")["reason_codes"])
        chosen = [p for p in r["plans"] if {"ev_price", "ev_avail", "ev_agg"} & ids_in(p)]
        self.assertTrue(chosen, "provisional options may still appear in a plan")
        for p in chosen:
            self.assertEqual(p["status"], "provisional")
            self.assertTrue(any(a["action"] == "verify_details" for a in p["next_actions"]))
            for e in p["evidence"]:
                self.assertTrue(e["source_url"] and e["checked_at"])
        self.assertEqual(r["status"], "provisional_only")  # every plan leans on an unverified option

    def test_08b_unknown_cost_and_duration_are_never_invented(self):
        d = only(base(), [
            venue("v_nocost", 45, None, "cafe", kind="place"),
            venue("v_nodur", 45, 0, "library", kind="place", duration_minutes=None)])
        r = cd.compose(d)
        self.assertIn("COST_UNKNOWN", option(r, "v_nocost")["reason_codes"])
        self.assertIn("DURATION_UNKNOWN", option(r, "v_nodur")["reason_codes"])
        self.assertFalse(option(r, "v_nodur")["placeable"])
        for p in r["plans"]:
            self.assertNotIn("v_nodur", ids_in(p))
            if "v_nocost" in ids_in(p):
                self.assertFalse(p["totals"]["cost_complete"])
                self.assertEqual(p["totals"]["unknown_cost_items"], ["v_nocost"])
                self.assertEqual(p["status"], "provisional")

    def test_08c_unknown_effort_is_a_labeled_allocation(self):
        d = base()
        for t in d["academic_tasks"]:
            if t["id"] == "t_essay":
                t["remaining_minutes"] = None
        r = cd.compose(d)
        cov = next(c for c in plan_of(r, "academic_first")["academic_coverage"] if c["task_id"] == "t_essay")
        self.assertIsNone(cov["needed_minutes"])
        self.assertEqual(cov["effort_source"], "provisional_allocation")
        self.assertEqual(cov["coverage"], "provisional_progress")
        self.assertIn("ACADEMIC_EFFORT_UNKNOWN", cov["reason_codes"])
        block = next(b for b in plan_of(r, "academic_first")["timeline"] if b.get("task_id") == "t_essay")
        self.assertEqual(block["status"], "provisional")
        self.assertTrue(any(q["code"] == "QUESTION_EFFORT" for q in r["questions"]))

    def test_08d_missing_assignment_is_critical_and_flagged(self):
        d = base()
        d["academic_tasks"].append({"id": "t_missing", "title": "Missed worksheet", "due": "2026-10-20T23:59:00",
                                    "submission_status": "missing", "remaining_minutes": 30})
        r = cd.compose(d)
        row = next(t for t in r["academic"]["unfinished"] if t["id"] == "t_missing")
        self.assertEqual(row["tier"], "critical")
        self.assertIn("ACADEMIC_OVERDUE", row["reason_codes"])
        self.assertEqual(r["academic"]["unfinished"][0]["id"], "t_missing")  # most overdue first

    # 9 -----------------------------------------------------------------------------
    def test_09_no_usable_external_events_gives_schedule_only_not_invented_activities(self):
        for cands in ([], [event("ev_canceled", "2026-10-26T13:00:00", "2026-10-26T14:00:00", listing_status="canceled")]):
            r = cd.compose(only(base(), cands))
            self.assertEqual(r["status"], "schedule_only")
            for p in r["plans"]:
                self.assertEqual(p["status"], "schedule_only")
                self.assertEqual(p["evidence"], [])
                self.assertFalse([b for b in p["timeline"] if b["type"] in ("fallback", "event", "food", "activity", "place")])
                ideas = p["optional_suggestions"]
                self.assertTrue(ideas)
                for i in ideas:
                    self.assertEqual((i["origin"], i["verified"], i["duration_is_estimate"]), ("system_suggestion", False, True))
                    self.assertNotIn("cost_cents", i)
                self.assert_plan_sound(r, p)

    def test_09c_opting_in_puts_suggestions_on_the_timeline_with_no_invented_price(self):
        d = only(base(), [])
        d["settings"] = {"include_generic_suggestions": True}
        r = cd.compose(d)
        blocks = [b for p in r["plans"] for b in p["timeline"] if b["type"] == "fallback"]
        self.assertTrue(blocks)
        for b in blocks:
            self.assertEqual((b["origin"], b["recommendation"], b["duration_is_estimate"]), ("system_suggestion", True, True))
            self.assertNotIn("cost_cents", b)
        for p in r["plans"]:
            if not p.get("baseline"):
                self.assertEqual(p["status"], "schedule_only")  # suggestions never make a plan "confirmed"
        base_plan = [p for p in r["plans"] if p.get("baseline")]
        for p in base_plan:
            self.assertFalse([b for b in p["timeline"] if b["type"] == "fallback"])

    def test_09b_no_free_time_gives_clear_explanation(self):
        d = base()
        d["fixed_commitments"] = [{"id": "all_day", "title": "Exam block", "start": "2026-10-26T08:00:00", "end": "2026-10-26T23:00:00", "location_id": "bldg_class"}]
        r = cd.compose(d)
        self.assertEqual(r["status"], "no_feasible_plan")
        self.assertEqual(r["plans"], [])
        self.assertIn("NO_FREE_TIME", r["no_plan_reasons"])
        self.assertIn("no usable free time", r["explanation"])
        self.assertTrue(r["academic"]["risk_flags"])  # unfinished work is still reported as at risk

    # 10 ----------------------------------------------------------------------------
    def test_10_budget_change_requires_replanning(self):
        before = cd.compose(base())
        snapshot = copy.deepcopy(base())
        after = cd.compose(base(), {"budget_cents": 500})
        self.assertEqual(base(), snapshot)
        self.assertNotEqual(before["input_fingerprint"], after["input_fingerprint"])
        self.assertEqual(after["replan"]["changed_keys"], ["budget_cents"])
        self.assertEqual(option(after, "food_tacos")["reason_codes"], ["COST_EXCEEDS_BUDGET"])
        for p in after["plans"]:
            self.assertLessEqual(p["totals"]["known_cost_cents"], 500)
            self.assert_plan_sound(after, p)
        self.assertGreater(max(p["totals"]["known_cost_cents"] for p in before["plans"]), 500)

    def test_10b_rejection_and_unavailability_replan(self):
        r = cd.compose(base(), {"reject_candidates": ["ev_concert"], "unavailable_candidates": ["ev_market"]})
        self.assertEqual(option(r, "ev_concert")["reason_codes"], ["USER_REJECTED"])
        self.assertEqual(option(r, "ev_market")["reason_codes"], ["REPORTED_UNAVAILABLE"])
        for p in r["plans"]:
            self.assertFalse({"ev_concert", "ev_market"} & ids_in(p))

    def test_10c_deadline_change_moves_study_before_new_deadline(self):
        r = cd.compose(base(), {"task_updates": {"t_essay": {"due": "2026-10-26T15:00:00"}}})
        p = plan_of(r, "academic_first")
        for b in p["timeline"]:
            if b.get("task_id") == "t_essay":
                self.assertLessEqual(b["end"], "2026-10-26T15:00-07:00")

    def test_10d_preference_change_avoids_tags(self):
        r = cd.compose(base(), {"preferences": {"avoid_tags": ["food"]}})
        self.assertEqual(option(r, "food_tacos")["reason_codes"], ["PREFERENCE_AVOIDED"])

    # 11 ----------------------------------------------------------------------------
    def test_11_manual_commitments_without_calendar(self):
        d = load("manual_commitments_no_calendar.json")
        r = cd.compose(d)
        self.assertIn("CALENDAR_UNAVAILABLE", [w["code"] for w in r["warnings"]])
        self.assertIn("CANVAS_NOT_CONNECTED", [w["code"] for w in r["warnings"]])
        p = plan_of(r, "balanced")
        commits = [b for b in p["timeline"] if b["type"] == "commitment"]
        self.assertEqual([c["source"] for c in commits], ["manual", "manual"])
        self.assertEqual([c["title"] for c in commits], ["Chemistry lecture (told by student)", "Tutoring job (told by student)"])
        self.assert_plan_sound(r, p)

    def test_11b_no_calendar_and_no_manual_times_does_not_invent_a_timetable(self):
        d = base()
        d["fixed_commitments"] = []
        d["sources"]["calendar"] = {"connected": False}
        r = cd.compose(d)
        self.assertTrue(any(q["code"] == "QUESTION_COMMITMENTS" for q in r["questions"]))
        for p in r["plans"]:
            self.assertFalse([b for b in p["timeline"] if b["type"] == "commitment"])

    def test_11c_stale_canvas_data_is_flagged(self):
        d = base()
        d["sources"]["canvas"]["synced_at"] = "2026-10-25T08:00:00-07:00"
        self.assertIn("CANVAS_DATA_STALE", [w["code"] for w in cd.compose(d)["warnings"]])

    # 12 ----------------------------------------------------------------------------
    def test_12_cross_midnight_window(self):
        r = cd.compose(load("cross_midnight.json"))
        self.assertEqual(r["status"], "ok")
        self.assertEqual(r["planning"]["window_end"], "2026-10-27T02:00-07:00")
        p = plan_of(r, "social_first")
        late = next(b for b in p["timeline"] if b.get("ref_id") == "ev_late_show")
        self.assertEqual((late["start"], late["end"], late["minutes"]), ("2026-10-26T23:30-07:00", "2026-10-27T00:30-07:00", 60))
        self.assertIn("(+1d)", cd.render_text(r))
        for pl in r["plans"]:
            self.assert_plan_sound(r, pl)

    def test_12b_dst_fall_back_duration_uses_real_elapsed_time(self):
        r = cd.compose(load("dst_fall_back.json"))
        ev = next(b for pl in r["plans"] for b in pl["timeline"] if b.get("ref_id") == "ev_repeat_hour")
        self.assertEqual(ev["start"], "2026-11-01T01:30-04:00")
        self.assertEqual(ev["end"], "2026-11-01T01:15-05:00")
        self.assertEqual(ev["minutes"], 45)

    def test_12c_timezone_handling(self):
        local = base()
        utc = copy.deepcopy(local)
        # The same instants written in UTC must give the same plan as local wall-clock times.
        for c in utc["fixed_commitments"]:
            for k in ("start", "end"):
                c[k] = _to_utc(c[k])
        utc["window"] = {k: _to_utc(v) for k, v in local["window"].items()}
        a, b = cd.compose(local), cd.compose(utc)
        self.assertEqual(a["plans"], b["plans"])
        bad = base()
        bad["planning_date"] = "2026-10-27"
        r = cd.compose(bad)
        self.assertEqual((r["status"], r["errors"][0]["code"]), ("invalid_input", "WINDOW_DATE_MISMATCH"))
        bad = base()
        bad["student"]["timezone"] = "Mars/Olympus"
        self.assertEqual(cd.compose(bad)["errors"][0]["code"], "INVALID_TIMEZONE")

    def test_12d_invalid_windows_and_ranges(self):
        d = base()
        d["window"]["end"] = d["window"]["start"]
        self.assertEqual(cd.compose(d)["errors"][0]["code"], "INVALID_WINDOW")
        d = base()
        d["fixed_commitments"][0]["end"] = d["fixed_commitments"][0]["start"]
        self.assertEqual(cd.compose(d)["errors"][0]["code"], "INVALID_TIME_RANGE")
        d = base()
        d["budget_cents"] = 12.5
        self.assertEqual(cd.compose(d)["errors"][0]["code"], "INVALID_BUDGET")

    def boundary(self, start, end, loc, **settings):
        d = only(base(), [event("ev", start, end, loc=loc, safety_buffer_minutes=0)])
        d["settings"] = settings
        d["fixed_commitments"][0]["safety_buffer_minutes"] = 0
        return option(cd.compose(d), "ev")

    def test_12e_exact_boundaries(self):
        zero = {"transition_minutes": 0}
        # Same place: starting the minute class ends is allowed, one minute earlier is not.
        self.assertEqual(self.boundary("2026-10-26T11:45:00", "2026-10-26T12:15:00", "bldg_class", **zero)["state"], "confirmed")
        self.assertEqual(self.boundary("2026-10-26T11:44:00", "2026-10-26T12:15:00", "bldg_class", **zero)["reason_codes"], ["ACTIVITY_CONFLICTS_COMMITMENT"])
        # Five-minute walk: exactly enough vs one minute short.
        self.assertEqual(self.boundary("2026-10-26T11:50:00", "2026-10-26T12:15:00", "mu_lawn", **zero)["state"], "confirmed")
        self.assertEqual(self.boundary("2026-10-26T11:49:00", "2026-10-26T12:15:00", "mu_lawn", **zero)["reason_codes"], ["TRAVEL_EXCEEDS_AVAILABLE_TIME"])
        # With the default 10-minute transition buffer: 5 + 10 minutes exactly vs one short.
        d = only(base(), [event("ev", "2026-10-26T12:00:00", "2026-10-26T12:30:00", loc="mu_lawn"),
                          event("ev2", "2026-10-26T11:59:00", "2026-10-26T12:30:00", loc="mu_lawn")])
        r = cd.compose(d)
        self.assertEqual(option(r, "ev")["state"], "confirmed")
        self.assertEqual(option(r, "ev2")["reason_codes"], ["TRANSITION_BUFFER_TOO_TIGHT"])

    def test_12f_window_edges_and_deadline_edge(self):
        d = only(base(), [event("ends_at_close", "2026-10-26T21:00:00", "2026-10-26T22:00:00", loc="job_site"),
                          event("ends_late", "2026-10-26T21:00:00", "2026-10-26T22:01:00", loc="job_site")])
        r = cd.compose(d)
        self.assertEqual(option(r, "ends_at_close")["state"], "confirmed")
        self.assertEqual(option(r, "ends_late")["reason_codes"], ["OUTSIDE_WINDOW"])
        # A study block may end exactly at the deadline, never after it.
        t = base()
        t["fixed_commitments"] = []
        t["window"] = {"start": "2026-10-26T09:00:00", "end": "2026-10-26T12:00:00"}
        t["candidates"] = []
        t["academic_tasks"] = [{"id": "t_x", "title": "X", "due": "2026-10-26T10:30:00", "submission_status": "unsubmitted", "remaining_minutes": 90}]
        r = cd.compose(t)
        block = next(b for b in plan_of(r, "academic_first")["timeline"] if b.get("task_id") == "t_x")
        self.assertEqual((block["start"], block["end"]), ("2026-10-26T09:00-07:00", "2026-10-26T10:30-07:00"))
        t["window"]["end"] = "2026-10-26T10:00:00"  # only 60 of the 90 minutes fit before the window closes
        r = cd.compose(t)
        cov = plan_of(r, "academic_first")["academic_coverage"][0]
        self.assertEqual((cov["coverage"], cov["allocated_minutes"], cov["risk"]), ("partial", 60, "high"))


class TestEngineProperties(PlanAssertions):
    def test_deterministic_and_pure(self):
        d = base()
        snapshot = copy.deepcopy(d)
        a = json.dumps(cd.compose(d), sort_keys=True)
        b = json.dumps(cd.compose(copy.deepcopy(d)), sort_keys=True)
        self.assertEqual(a, b)
        self.assertEqual(d, snapshot)

    def test_unknown_travel_is_assumed_and_labeled(self):
        d = only(base(), [venue("v_far", 45, 0, "mystery_spot", kind="place")])
        r = cd.compose(d)
        pls = [p for p in r["plans"] if "v_far" in ids_in(p)]
        self.assertTrue(pls)
        for p in pls:
            self.assertEqual(p["status"], "provisional")
            self.assertIn("TRAVEL_ASSUMED", p["provisional_reasons"])
            self.assertTrue(any(b.get("assumed") for b in p["timeline"] if b["type"] == "travel"))
        d["settings"] = {"unknown_travel_assumption_minutes": None}
        r = cd.compose(d)
        self.assertEqual((option(r, "v_far")["state"], option(r, "v_far")["placeable"]), ("provisional", False))
        self.assertIn("TRAVEL_UNKNOWN", option(r, "v_far")["reason_codes"])

    def test_every_external_recommendation_carries_source_and_checked_at(self):
        for p in cd.compose(base())["plans"]:
            for e in p["evidence"]:
                self.assertTrue(e["source_url"].startswith("https://"))
                self.assertTrue(e["checked_at"])

    def test_sample_asset_matches_fixture_and_runs(self):
        sample = json.loads((ROOT / "whats-the-move" / "assets" / "sample_input.json").read_text())
        self.assertEqual(cd.compose(sample)["plans"], cd.compose(base())["plans"])


class TestHardening(PlanAssertions):
    """Regression tests for the Milestone 1 hardening pass."""

    def provisional_only_day(self):
        return only(base(), [
            event("ev_price", "2026-10-26T12:30:00", "2026-10-26T13:30:00", cost=500,
                  verification={"time": "verified", "price": "unverified", "availability": "verified"}),
            event("ev_agg", "2026-10-26T15:30:00", "2026-10-26T16:30:00", source=source("agg", kind="aggregator"))])

    # -- 1. reliable fallback ---------------------------------------------------------
    def test_baseline_is_schedule_only_and_built_from_real_inputs(self):
        r = cd.compose(self.provisional_only_day())
        self.assertEqual(r["status"], "provisional_only")
        base_plan = r["plans"][-1]
        self.assertTrue(base_plan["baseline"])
        self.assertEqual(base_plan["status"], "schedule_only")
        self.assertEqual(r["reliable_fallback_variant"], "baseline")
        self.assertFalse({"ev_price", "ev_agg"} & ids_in(base_plan))
        self.assertEqual(base_plan["evidence"], [])
        self.assertEqual({b["ref_id"] for b in base_plan["timeline"] if b["type"] == "commitment"}, {"class_prog", "shift_cafe"})
        self.assertEqual({c["task_id"]: c["coverage"] for c in base_plan["academic_coverage"]}, {"t_essay": "covered", "t_quiz": "covered"})
        kinds = {b["type"] for b in base_plan["timeline"]}
        self.assertTrue(kinds <= {"commitment", "academic", "travel", "buffer"}, kinds)
        self.assert_plan_sound(r, base_plan)

    def test_baseline_is_offered_whenever_it_differs_and_never_duplicated(self):
        with_events = cd.compose(base())  # confirmed events exist, baseline still offered
        self.assertEqual(with_events["plans"][-1]["variant"], "baseline")
        self.assertEqual(with_events["status"], "ok")
        no_events = cd.compose(only(base(), []))  # ordinary plan already is schedule-only: no duplicate
        self.assertFalse(any(p.get("baseline") for p in no_events["plans"]))
        self.assertEqual(len(no_events["plans"]), 1)
        self.assertEqual(no_events["plans"][0]["also_matches"], ["balanced", "social_first"])
        ordinary = [p for p in with_events["plans"] if not p.get("baseline")]
        self.assertTrue(all(p["variant"] in cd.VARIANTS for p in ordinary))
        self.assertIn(with_events["recommended_variant"], cd.VARIANTS)

    def test_baseline_ignores_suggestions_even_when_opted_in(self):
        d = base()
        d["settings"] = {"include_generic_suggestions": True}
        for p in cd.compose(d)["plans"]:
            if p.get("baseline"):
                self.assertFalse([b for b in p["timeline"] if b["type"] == "fallback"])

    def test_baseline_is_deterministic(self):
        d = self.provisional_only_day()
        self.assertEqual(json.dumps(cd.compose(d), sort_keys=True), json.dumps(cd.compose(copy.deepcopy(d)), sort_keys=True))

    # -- 2. critical task protection -------------------------------------------------------
    def test_candidate_limit_is_reported_and_never_costs_critical_coverage(self):
        many = [venue(f"v{i:02d}", 30, 100, "cafe", kind="place", tags=["chill"]) for i in range(20)]
        r = cd.compose(only(base(), many))
        skipped = [o for o in r["options"] if not o["considered"] and not o["suggestion"]]
        self.assertEqual(len(skipped), 6)
        cap = next(w for w in r["warnings"] if w["code"] == "CANDIDATE_CAP_APPLIED")
        self.assertEqual(sorted(cap["ids"]), sorted(o["id"] for o in skipped))
        for p in r["plans"]:
            cov = {c["task_id"]: c["coverage"] for c in p["academic_coverage"]}
            self.assertEqual(cov, {"t_essay": "covered", "t_quiz": "covered"})
            self.assertEqual(p["critical_conflicts"], [])
        self.assertEqual(r["academic"]["critical_conflicts"], [])

    def test_activities_never_displace_critical_work_in_any_variant(self):
        r = cd.compose(self.tight())
        for p in r["plans"]:
            essay = next(c for c in p["academic_coverage"] if c["task_id"] == "t_essay")
            self.assertEqual(essay["coverage"], "covered", p["variant"])

    def tight(self):
        d = only(base(), [event("ev_morning", "2026-10-26T09:30:00", "2026-10-26T10:30:00")])
        d["fixed_commitments"] = []
        d["window"]["end"] = "2026-10-26T11:00:00"
        return d

    def test_unschedulable_critical_task_is_surfaced_not_hidden(self):
        d = base()
        d["window"] = {"start": "2026-10-26T09:00:00", "end": "2026-10-26T10:00:00"}
        d["fixed_commitments"] = []
        d["candidates"] = []
        d["academic_tasks"][0]["due"] = "2026-10-26T10:00:00"  # essay: 90 min needed, 60 available before the deadline
        r = cd.compose(d)
        [c] = r["academic"]["critical_conflicts"]
        self.assertEqual((c["task_id"], c["code"], c["cause"], c["best_case_minutes"], c["needed_minutes"]),
                         ("t_essay", "CRITICAL_TASK_PARTIAL", "NO_TIME_BEFORE_DEADLINE", 60, 90))
        self.assertIn("CRITICAL_ACADEMIC_CONFLICT", [w["code"] for w in r["warnings"]])
        self.assertIn("Essay 2 draft", r["explanation"])
        for p in r["plans"]:
            self.assertEqual(p["critical_conflicts"], ["t_essay"])
            essay = next(x for x in p["academic_coverage"] if x["task_id"] == "t_essay")
            self.assertEqual((essay["coverage"], essay["risk"]), ("partial", "high"))
        self.assertIn("!! Urgent work not fully scheduled: Essay 2 draft", cd.render_text(r))

    def test_critical_task_with_no_time_at_all(self):
        d = base()
        d["fixed_commitments"] = [{"id": "all_day", "title": "Exam block", "start": "2026-10-26T08:00:00", "end": "2026-10-26T23:00:00", "location_id": "bldg_class"}]
        r = cd.compose(d)
        self.assertEqual(r["status"], "no_feasible_plan")
        self.assertEqual({c["task_id"]: c["code"] for c in r["academic"]["critical_conflicts"]}, {"t_essay": "CRITICAL_TASK_UNSCHEDULABLE"})
        self.assertIn("Urgent work at risk: Essay 2 draft", r["explanation"])

    def test_daily_study_cap_conflict_is_named(self):
        d = only(base(), [])
        d["settings"] = {"max_academic_minutes": 60}
        d["academic_tasks"][1]["urgency"] = "high"  # quiz becomes critical too
        r = cd.compose(d)
        causes = {c["task_id"]: c["cause"] for c in r["academic"]["critical_conflicts"]}
        self.assertEqual(causes, {"t_essay": "DAILY_STUDY_CAP", "t_quiz": "DAILY_STUDY_CAP"})

    # -- 3. cost and travel honesty ---------------------------------------------------------
    def test_unknown_cost_is_not_counted_as_spend_or_as_confirmed(self):
        d = only(base(), [event("ev_tbd", "2026-10-26T12:30:00", "2026-10-26T13:30:00", cost=None)])
        r = cd.compose(d)
        pls = [p for p in r["plans"] if "ev_tbd" in ids_in(p)]
        self.assertTrue(pls)
        for p in pls:
            t = p["totals"]
            self.assertEqual((t["known_cost_cents"], t["known_planned_spend_cents"]), (0, 0))
            self.assertEqual(t["unknown_cost_titles"], ["ev_tbd"])
            self.assertTrue(t["remaining_budget_is_upper_bound"] and not t["cost_complete"])
            self.assertEqual(p["status"], "provisional")
            self.assertEqual(p["certainty"]["costs"], "incomplete")
            self.assertIn("1 cost(s) are unknown", p["status_detail"])
            blk = next(b for b in p["timeline"] if b.get("ref_id") == "ev_tbd")
            self.assertIs(blk["cost_known"], False)
        text = cd.render_text(r)
        self.assertIn("[cost unknown]", text)
        self.assertIn("at most $30.00 left", text)

    def test_assumed_travel_stays_visibly_provisional(self):
        d = only(base(), [venue("v_far", 45, 0, "mystery_spot", kind="place")])
        r = cd.compose(d)
        pls = [p for p in r["plans"] if "v_far" in ids_in(p)]
        self.assertTrue(pls)
        for p in pls:
            self.assertEqual(p["status"], "provisional")
            self.assertEqual(p["certainty"]["travel"], "assumed")
            self.assertGreaterEqual(p["assumed_travel"]["legs"], 1)
            legs = [b for b in p["timeline"] if b["type"] == "travel" and b["assumed"]]
            self.assertTrue(legs and all(b["status"] == "provisional" for b in legs))
            dest = next(b for b in p["timeline"] if b.get("ref_id") == "v_far")
            self.assertIs(dest["arrival_confirmed"], False)
            self.assertIn("assumed", p["status_detail"])
        self.assertIn("~assumed", cd.render_text(r))

    def test_confirmed_event_does_not_make_whole_plan_confirmed(self):
        # The event itself is fully verified; the unknown-location commitment makes travel assumed.
        d = only(base(), [event("ev_ok", "2026-10-26T12:30:00", "2026-10-26T13:30:00")])
        d["fixed_commitments"][0].pop("location_id")
        r = cd.compose(d)
        self.assertEqual(option(r, "ev_ok")["state"], "confirmed")
        for p in r["plans"]:
            if "ev_ok" in ids_in(p):
                self.assertEqual(p["status"], "provisional")
                self.assertEqual(p["certainty"]["events"], "all_confirmed")
                self.assertEqual(p["certainty"]["travel"], "assumed")

    def test_uncertainty_demo_fixture(self):
        r = cd.compose(load("uncertainty_demo.json"))
        self.assertEqual(r["status"], "provisional_only")
        self.assertEqual(r["reliable_fallback_variant"], "baseline")
        self.assertTrue(all(p["status"] == "provisional" for p in r["plans"] if not p.get("baseline")))
        self.assertEqual(r["plans"][-1]["status"], "schedule_only")

    def test_fully_confirmed_plan_says_so_and_provisional_effort_is_separate(self):
        p = plan_of(cd.compose(base()), "balanced")
        self.assertEqual(p["status"], "confirmed")
        self.assertEqual(p["status_detail"], "Verified events, current calendar, known travel times and complete costs.")
        self.assertEqual(p["certainty"], {"events": "all_confirmed", "costs": "complete", "travel": "known", "academic_effort": "known",
                                          "availability": "verified_current", "academic_data": "current"})
        d = base()
        d["academic_tasks"][0]["remaining_minutes"] = None
        self.assertEqual(plan_of(cd.compose(d), "academic_first")["certainty"]["academic_effort"], "provisional")


class TestFounderDecisions(PlanAssertions):
    """Provenance, plan status, missing-data reporting and the zero-fabrication policy."""

    ORIGINS = {"calendar_commitment", "manual_commitment", "unspecified_commitment", "system_study_recommendation",
               "verified_external_event", "unverified_external_event", "system_suggestion",
               "user_provided_travel", "assumed_travel_estimate", "system_buffer"}

    def all_blocks(self, r):
        return [(p, b) for p in r["plans"] for b in p["timeline"]]

    # -- provenance ---------------------------------------------------------------------
    def test_every_block_exposes_origin_and_provenance(self):
        d = only(base(), [event("ev_ok", "2026-10-26T12:30:00", "2026-10-26T13:30:00"),
                          event("ev_unv", "2026-10-26T14:00:00", "2026-10-26T15:00:00", availability="unknown"),
                          venue("v_far", 45, 0, "mystery_spot", kind="place")])
        d["settings"] = {"include_generic_suggestions": True}
        r = cd.compose(d)
        seen = set()
        for _, b in self.all_blocks(r):
            self.assertIn(b["origin"], self.ORIGINS, b)
            self.assertEqual(b["provenance"]["origin"], b["origin"])
            seen.add(b["origin"])
        self.assertTrue({"calendar_commitment", "system_study_recommendation", "verified_external_event",
                         "unverified_external_event", "user_provided_travel", "assumed_travel_estimate",
                         "system_buffer"} <= seen, seen)

    def test_study_blocks_are_recommendations_that_keep_the_task_reference(self):
        r = cd.compose(base())
        for _, b in self.all_blocks(r):
            if b["type"] == "academic":
                self.assertTrue(b["recommendation"])
                prov = b["provenance"]
                self.assertEqual((prov["origin"], prov["verified"]), ("system_study_recommendation", False))
                task = prov["task"]
                self.assertEqual(task["id"], b["task_id"])
                self.assertEqual(task["origin"], "canvas_assignment")
                self.assertTrue(task["source_id"].startswith("synthetic_canvas_"))
                self.assertTrue(task["url"].startswith("https://canvas.example.edu/"))
                self.assertIn("synced_at", task)
                self.assertTrue(b["title"].startswith("Suggested study"))

    def test_calendar_vs_manual_vs_student_entered_origins(self):
        r = cd.compose(base())
        commits = [b for b in plan_of(r, "balanced")["timeline"] if b["type"] == "commitment"]
        self.assertEqual({b["origin"] for b in commits}, {"calendar_commitment"})
        self.assertEqual(commits[0]["provenance"]["calendar_id"], "synthetic_primary")
        self.assertTrue(commits[0]["provenance"]["source_event_id"].startswith("synthetic_evt_"))
        r = cd.compose(load("manual_commitments_no_calendar.json"))
        p = plan_of(r, "balanced")
        self.assertEqual({b["origin"] for b in p["timeline"] if b["type"] == "commitment"}, {"manual_commitment"})
        study = next(b for b in p["timeline"] if b["type"] == "academic")
        self.assertEqual(study["provenance"]["task"]["origin"], "student_entered_task")
        d = base()
        d["fixed_commitments"][0].pop("source")
        blk = next(b for b in plan_of(cd.compose(d), "balanced")["timeline"] if b.get("ref_id") == "class_prog")
        self.assertEqual(blk["origin"], "unspecified_commitment")  # never claim Calendar when the source is not stated

    def test_external_event_provenance_keeps_source_fields(self):
        r = cd.compose(self.demo())
        for _, b in self.all_blocks(r):
            if b["origin"] in ("verified_external_event", "unverified_external_event"):
                prov = b["provenance"]
                self.assertTrue(prov["source_url"].startswith("https://") and prov["checked_at"])
                self.assertEqual(prov["verified"], b["origin"] == "verified_external_event")
                self.assertEqual(b["origin"] == "unverified_external_event", bool(prov["unverified_fields"]))

    def demo(self):
        return load("uncertainty_demo.json")

    def test_travel_provenance_separates_provided_from_assumed(self):
        d = only(base(), [venue("v_far", 45, 0, "mystery_spot", kind="place")])
        r = cd.compose(d)
        legs = [b for _, b in self.all_blocks(r) if b["type"] == "travel"]
        provided = [b for b in legs if not b["assumed"]]
        assumed = [b for b in legs if b["assumed"]]
        self.assertTrue(provided and assumed)
        self.assertTrue(all(b["origin"] == "user_provided_travel" and b["provenance"]["source"] == "synthetic" for b in provided))
        self.assertTrue(all(b["origin"] == "assumed_travel_estimate" and b["provenance"]["recommendation"] for b in assumed))

    # -- schedule_only and confirmed ---------------------------------------------------------
    def test_schedule_only_is_not_confirmed(self):
        r = cd.compose(load("schedule_only_example.json"))
        self.assertEqual(r["status"], "schedule_only")
        self.assertEqual(r["reliable_fallback_variant"], r["plans"][0]["variant"])
        for p in r["plans"]:
            self.assertEqual(p["status"], "schedule_only")
            self.assertEqual(p["certainty"]["events"], "none")
            self.assertIn("nothing here is independently verified", p["status_detail"])
            self.assertNotIn("confirmed", p["status_detail"].lower().replace("not fully confirmed", ""))
        self.assertIn("No outside events are included", r["explanation"])

    def test_confirmed_requires_current_and_complete_calendar_coverage(self):
        fresh = "2026-10-26T07:45:00-07:00"
        cases = [
            ("stale", {"connected": True, "synced_at": "2026-10-24T08:00:00-07:00", "coverage": "complete"}),
            ("sync_time_unknown", {"connected": True, "coverage": "complete"}),
            ("partial", {"connected": True, "synced_at": fresh, "coverage": "complete", "calendars_failed": ["work"]}),
            ("partial", {"connected": True, "synced_at": fresh, "coverage": "partial"}),
            ("partial", {"connected": True, "synced_at": fresh, "coverage": "complete",
                         "calendars_authorized": ["primary", "work"], "calendars_read": ["primary"]}),
            ("coverage_unknown", {"connected": True, "synced_at": fresh}),
            ("unknown", {"connected": False}),
            ("unknown", None),
        ]
        for avail, cal in cases:
            d = base()
            if cal is None:
                d["sources"].pop("calendar")
            else:
                d["sources"]["calendar"] = cal
            d["fixed_commitments"] = []  # nothing known: must not read as "free"
            r = cd.compose(d)
            plans = [p for p in r["plans"] if p["certainty"]["events"] != "none"]
            self.assertTrue(plans, avail)
            for p in plans:
                self.assertEqual((p["status"], p["certainty"]["availability"]), ("provisional", avail), cal)
                self.assertIn("AVAILABILITY_NOT_VERIFIED", p["provisional_reasons"])
            self.assertNotEqual(r["status"], "ok")
        # all authorized calendars read, fresh, complete: confirmed
        good = base()
        good["sources"]["calendar"].update(calendars_authorized=["primary", "work"], calendars_read=["work", "primary"])
        self.assertEqual(plan_of(cd.compose(good), "balanced")["status"], "confirmed")

    def test_student_reported_commitments_are_valid_input_but_never_confirm_a_plan(self):
        r = cd.compose(load("manual_commitments_no_calendar.json"))
        p = plan_of(r, "balanced")
        self.assertEqual(p["certainty"]["availability"], "student_reported")
        self.assertEqual(p["certainty"]["academic_data"], "student_reported")
        self.assertIn("student_reported", {g["state"] for g in r["data_gaps"]})
        # their commitments are still planned around
        self.assertEqual([b["title"] for b in p["timeline"] if b["type"] == "commitment"],
                         ["Chemistry lecture (told by student)", "Tutoring job (told by student)"])
        self.assertEqual(p["status"], "provisional")
        self.assertIn("rests on commitments the student reported", p["status_detail"])
        self.assertNotEqual(r["status"], "ok")
        # without outside events the same input stays schedule_only
        d = load("manual_commitments_no_calendar.json")
        d["candidates"] = []
        self.assertEqual(cd.compose(d)["status"], "schedule_only")

    def test_uncertainty_kinds_stay_separate(self):
        r = cd.compose(self.demo())
        p = plan_of(r, "social_first")
        self.assertEqual({k: p["certainty"][k] for k in ("events", "costs", "travel")}, {"events": "some_provisional", "costs": "incomplete", "travel": "assumed"})
        d = base()
        d["academic_tasks"][0]["remaining_minutes"] = None
        p = plan_of(cd.compose(d), "balanced")
        self.assertEqual((p["status"], p["certainty"]["academic_effort"]), ("confirmed", "provisional"))

    # -- missing data -----------------------------------------------------------------------
    def test_source_states_are_distinguished(self):
        r = cd.compose(base())
        self.assertEqual(r["source_status"], {"calendar": "current", "canvas": "current", "web": "searched_verified_results"})
        self.assertEqual([g for g in r["data_gaps"] if g["severity"] != "info"], [])

        stale = base()
        stale["sources"]["canvas"]["synced_at"] = "2026-10-25T08:00:00-07:00"
        stale["sources"]["calendar"]["synced_at"] = "2026-10-25T08:00:00-07:00"
        self.assertEqual(cd.compose(stale)["source_status"]["canvas"], "stale")
        self.assertEqual(cd.compose(stale)["source_status"]["calendar"], "stale")

        r = cd.compose(load("source_gaps_example.json"))
        self.assertEqual(r["source_status"], {"calendar": "not_reported", "canvas": "unavailable", "web": "unavailable"})
        by_source = {g["source"]: g for g in r["data_gaps"]}
        self.assertEqual(by_source["calendar"]["severity"], "missing")
        self.assertIn("Using commitments the student reported", by_source["calendar"]["message"])
        self.assertIn("no known calendar conflict", by_source["calendar"]["message"])
        self.assertEqual(by_source["web"]["affects"], ["events"])

        searched = only(base(), [event("ev_canceled", "2026-10-26T13:00:00", "2026-10-26T14:00:00", listing_status="canceled")])
        r = cd.compose(searched)
        self.assertEqual(r["source_status"]["web"], "searched_no_verified_results")
        self.assertEqual(next(g for g in r["data_gaps"] if g["source"] == "web")["severity"], "info")

        none_reported = base()
        none_reported.pop("sources")
        r = cd.compose(none_reported)
        self.assertEqual(r["source_status"], {"calendar": "not_reported", "canvas": "not_reported", "web": "not_reported"})

    def test_missing_calendar_is_never_read_as_a_free_day(self):
        d = base()
        d["fixed_commitments"] = []
        d["sources"]["calendar"] = {"connected": False}
        r = cd.compose(d)
        self.assertIn("does not mean the day is free", next(g for g in r["data_gaps"] if g["source"] == "calendar")["message"])
        self.assertEqual(r["planning"]["availability_certainty"], "unknown")
        self.assertIn("no known calendar conflict", r["planning"]["free_time_meaning"])
        for p in r["plans"]:
            self.assertNotEqual(p["status"], "confirmed")
            self.assertIn("calendar (unavailable)", p["status_detail"])
        text = cd.render_text(r)
        self.assertIn("Free time = no known calendar conflict", text)
        self.assertIn("calendar - unavailable", text)

    def test_empty_but_current_calendar_is_reported_as_no_known_conflict(self):
        d = base()
        d["fixed_commitments"] = []
        gap = next(g for g in cd.compose(d)["data_gaps"] if g["source"] == "calendar")
        self.assertEqual((gap["severity"], gap["state"]), ("info", "current"))
        self.assertIn("no known calendar conflict", gap["message"])

    def test_as_of_default_is_disclosed(self):
        d = base()
        d.pop("as_of")
        self.assertIn("AS_OF_DEFAULTED", [w["code"] for w in cd.compose(d)["warnings"]])

    # -- calendar metadata (read-only input contract) -----------------------------------------------
    def test_free_and_tentative_calendar_events(self):
        d = base()
        d["fixed_commitments"] += [
            {"id": "ooo_note", "title": "Study abroad fair (marked free)", "start": "2026-10-26T13:30:00", "end": "2026-10-26T14:30:00", "busy_status": "free", "source": "google_calendar"},
            {"id": "maybe_meet", "title": "Club meeting (tentative)", "start": "2026-10-26T15:00:00", "end": "2026-10-26T16:00:00", "busy_status": "tentative", "location_id": "library", "source": "google_calendar"}]
        r = cd.compose(d)
        self.assertEqual(r["ignored_commitments"], [{"id": "ooo_note", "title": "Study abroad fair (marked free)", "reason": "COMMITMENT_MARKED_FREE"}])
        for p in r["plans"]:
            titles = [b["title"] for b in p["timeline"]]
            self.assertNotIn("Study abroad fair (marked free)", titles)
            tent = next(b for b in p["timeline"] if b.get("ref_id") == "maybe_meet")
            self.assertTrue(tent["provenance"]["tentative"])
            self.assertTrue(tent["notes"])
            self.assert_plan_sound(r, p)

    # -- zero fabrication ---------------------------------------------------------------------------
    def test_nothing_is_added_that_the_inputs_do_not_contain(self):
        for name in ("base_day.json", "schedule_only_example.json", "source_gaps_example.json", "uncertainty_demo.json"):
            d = load(name)
            r = cd.compose(d)
            commitment_ids = {c["id"] for c in d["fixed_commitments"]}
            candidate_ids = {c["id"] for c in d["candidates"]}
            task_ids = {t["id"] for t in d["academic_tasks"]}
            for _, b in self.all_blocks(r):
                if b["type"] == "commitment":
                    self.assertIn(b["ref_id"], commitment_ids)
                elif b["type"] == "academic":
                    self.assertIn(b["task_id"], task_ids)
                elif b["type"] in ("travel", "buffer"):
                    self.assertIn(b["origin"], ("user_provided_travel", "assumed_travel_estimate", "system_buffer"))
                else:
                    self.assertIn(b["ref_id"], candidate_ids)  # no built-in idea reaches the timeline by default
                    self.assertNotEqual(b["origin"], "system_suggestion")
            for p in r["plans"]:
                for e in p["evidence"]:
                    self.assertIn(e["id"], candidate_ids)

    def quiet(self, **kw):
        d = only(base(), [])
        d["academic_tasks"] = []
        d.update(kw)
        return d

    def test_quiet_day_with_commitments_returns_a_commitments_only_schedule(self):
        r = cd.compose(self.quiet())
        self.assertEqual(r["status"], "schedule_only")
        self.assertNotIn("NO_FEASIBLE", json.dumps(r["no_plan_reasons"]))
        [p] = r["plans"]
        self.assertEqual(p["status"], "schedule_only")
        self.assertEqual({b["type"] for b in p["timeline"]}, {"commitment"})  # nothing invented, no filler
        self.assertEqual(p["unscheduled_minutes"], 13 * 60 - 75 - 180)
        self.assertEqual(p["evidence"], [])
        self.assertTrue(p["optional_suggestions"])  # offered separately, never scheduled
        self.assertIn("left unscheduled", r["explanation"])
        self.assertIn("Unscheduled time left open: 525 min", cd.render_text(r))
        self.assert_plan_sound(r, p)

    def test_quiet_day_commitments_cover_the_whole_window_is_still_valid(self):
        d = self.quiet()
        d["fixed_commitments"] = [{"id": "all_day", "title": "Exam block", "start": "2026-10-26T08:00:00", "end": "2026-10-26T23:00:00", "location_id": "bldg_class", "source": "manual"}]
        r = cd.compose(d)
        self.assertEqual((r["status"], r["plans"][0]["unscheduled_minutes"]), ("schedule_only", 0))

    def test_empty_day_is_honest_not_infeasible(self):
        d = self.quiet(fixed_commitments=[])
        for cal in ({"connected": False}, None):
            if cal is None:
                d["sources"].pop("calendar")
            else:
                d["sources"]["calendar"] = cal
            r = cd.compose(d)
            self.assertEqual((r["status"], r["plans"]), ("empty_day", []))
            self.assertEqual(r["no_plan_reasons"], ["EMPTY_DAY"])
            self.assertIn("does not mean the day is free", r["explanation"])
            self.assertIn("calendar", {g["source"] for g in r["data_gaps"]})
            self.assertIn("Nothing to schedule", cd.render_text(r))

    def test_empty_day_with_a_current_calendar_says_no_known_conflict_not_free(self):
        r = cd.compose(self.quiet(fixed_commitments=[]))
        self.assertEqual(r["status"], "empty_day")
        gap = next(g for g in r["data_gaps"] if g["source"] == "calendar")
        self.assertEqual(gap["severity"], "info")
        self.assertIn("no known calendar conflict", gap["message"])
        self.assertNotIn("confirmed", r["explanation"].lower())

    def test_no_feasible_plan_is_reserved_for_real_infeasibility(self):
        d = base()
        d["fixed_commitments"] = [{"id": "all_day", "title": "Exam block", "start": "2026-10-26T08:00:00", "end": "2026-10-26T23:00:00", "location_id": "bldg_class"}]
        self.assertEqual(cd.compose(d)["status"], "no_feasible_plan")  # urgent work cannot be placed
        d = self.quiet()
        d["candidates"] = [event("ev_canceled", "2026-10-26T13:00:00", "2026-10-26T14:00:00", listing_status="canceled")]
        self.assertEqual(cd.compose(d)["status"], "schedule_only")  # a rejected listing is an uneventful day

    # -- synthetic data must identify itself --------------------------------------------------------
    def test_synthetic_data_is_marked_in_results_and_output(self):
        r = cd.compose(base())
        self.assertEqual(r["data_origin"], "synthetic_sample")
        self.assertEqual(r["warnings"][0]["code"], "SYNTHETIC_DATA")
        self.assertIn("synthetic sample data", cd.render_text(r))
        real = base()
        real.pop("data_origin")
        r = cd.compose(real)
        self.assertIsNone(r["data_origin"])
        self.assertNotIn("SYNTHETIC_DATA", [w["code"] for w in r["warnings"]])


class TestCli(unittest.TestCase):
    def run_cli(self, *args):
        return subprocess.run([sys.executable, "-I", str(SCRIPT), *args], capture_output=True, text=True)

    def test_sample_text_and_json(self):
        p = self.run_cli("--sample", "--format", "text")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("balanced (recommended)", p.stdout)
        self.assertTrue(p.stdout.startswith("SYNTHETIC SAMPLE DATA"))
        p = self.run_cli("--sample")
        self.assertEqual(json.loads(p.stdout)["status"], "ok")

    def test_invalid_input_exit_code(self):
        bad = FIXTURES / "invalid_window.json"
        p = self.run_cli("--input", str(bad))
        self.assertEqual(p.returncode, 2)
        self.assertEqual(json.loads(p.stdout)["status"], "invalid_input")

    def test_changes_flag_and_usage_errors(self):
        p = self.run_cli("--sample", "--changes", str(FIXTURES / "changes_budget_500.json"))
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(json.loads(p.stdout)["replan"]["changed_keys"], ["budget_cents"])
        self.assertEqual(self.run_cli().returncode, 2)


def _to_utc(local_iso):
    from datetime import datetime, timezone, timedelta
    return (datetime.fromisoformat(local_iso) + timedelta(hours=7)).replace(tzinfo=timezone.utc).isoformat().replace("+00:00", "Z")


if __name__ == "__main__":
    unittest.main()
