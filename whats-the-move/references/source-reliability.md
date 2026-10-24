# Judging local activity information

Use this when filling `candidates[]`. The goal is that a student never shows up to a canceled event or a surprise charge.

1. **Prefer the primary source.** The organizer's own page (the club, department, venue or university events calendar entry) beats a listing site, social post or aggregator. Set `organizer_type` to `organizer` or `venue` only when the page really belongs to them. Ticketing pages run by the organizer count; a third-party roundup does not.
2. **A mention is not availability.** A page that lists an event does not prove it is on, open or has room. Mark `availability` as `verified` only when the primary source says so (open, tickets available, no capacity warning) or you saw the registration/ticket page working. Otherwise leave it `unverified` or `unknown`.
3. **Verify fields separately.** Time, price and availability (for venues: hours, price, availability) each get their own `verified` / `unverified` / `unknown`. If the price is "from $10" or varies, say `unverified`. The engine downgrades any field from a non-primary source anyway.
4. **Always record the evidence.** `source.url` and `source.checked_at` are required for every concrete recommendation. Use the time you read the page, not the event date. Options without them are dropped.
5. **Watch for changes.** Set `listing_status` to `canceled`, `postponed`, `rescheduled` or `changed` whenever the page says so, including banners and "updated" notes. Changed hours or price turn an option provisional rather than hiding the change.
6. **Do not fill gaps.** If you cannot find the duration, price or location, leave it null. The plan will say it needs checking. Do not copy a typical duration or price from similar events.
7. **Travel.** Add `travel.legs` between the places in the student's day when you can estimate walking or transit time from a map source. A missing leg is assumed and labeled, not treated as known.
8. **No match is fine.** If nothing verified fits, run with an empty or thin `candidates` list; the engine adds generic fallbacks (a walk, downtime, texting a friend) that it labels as suggestions, not events.
