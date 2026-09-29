"""Smart Slot Planner diagnostic; run from apps/api with PYTHONPATH=.

Prints the channel's publishing cadence, the configured slots, what YouTube
reports as upcoming/published (the schedule cache), today's counts, the next
free slot, the cache freshness and the learning gate.  No secret, token or
upload session URI is ever printed.

    PYTHONPATH=. python scripts/youtube_schedule_diagnostics.py
    PYTHONPATH=. python scripts/youtube_schedule_diagnostics.py --refresh

Without ``--refresh`` Google is not called and the cached schedule is shown
with its age (the only write: expired slot reservations are marked released,
exactly as any planner read does).  ``--refresh`` first re-reads the channel's
uploads playlist + videos.list (read-only Google calls) into the schedule
cache - the same refresh the "Refresh schedule" button performs.
"""

import argparse
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from sqlalchemy import select

from clipforge.config import get_settings
from clipforge.database import SessionLocal
from clipforge.models import YouTubeScheduleEntry
from clipforge.security.secrets import SecretStore
from clipforge.youtube import connection, schedule_learning
from clipforge.youtube import schedule as schedule_authority
from clipforge.youtube import slots as planner
from clipforge.youtube.provider import GoogleYouTubeProvider, YouTubeApiError


def _local(value, zone: str) -> str:
    if value is None:
        return "-"
    value = value.replace(tzinfo=UTC) if value.tzinfo is None else value
    local = value.astimezone(ZoneInfo(zone))
    return f"{local:%a %Y-%m-%d %H:%M} {local.tzname()}"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--refresh", action="store_true", help="re-read the channel schedule from YouTube first (read-only Google calls)")
    parser.add_argument("--days", type=int, default=4, help="days shown in the overview (default 4)")
    args = parser.parse_args()
    settings = get_settings()
    now = datetime.now(UTC)
    with SessionLocal() as db:
        record = connection.active_connection(db)
        if record is None:
            parser.error("Connect YouTube first")
        existing = schedule_authority.get_schedule(db, record.channel_id)
        if existing is None and not args.refresh:
            print("No publishing schedule yet (it is created with 1 video/day when the upload sheet or Settings opens).")
            return
        if args.refresh:
            try:
                result = schedule_authority.sync_remote(db, settings, SecretStore(), GoogleYouTubeProvider(), record.channel_id, now=now)
                print(f"Refreshed: {result.pages} playlist page(s), {result.videos} video(s) read, {result.entries} on the calendar, complete={result.complete}")
            except YouTubeApiError as exc:
                print(f"Refresh FAILED: {exc.code}: {exc.message}")
        schedule = schedule_authority.ensure_schedule(db, record.channel_id)
        zone = schedule.timezone
        slots = schedule_authority.slot_map(db, schedule.channel_id)

        print("CHANNEL")
        print(f"  channel ID:      {schedule.channel_id} ({record.channel_title})")
        print(f"  timezone:        {zone} (now {_local(now, zone)})")
        print(f"  mode:            {schedule.mode}{' (starter schedule, not derived from data)' if schedule.mode == 'seed' else ''}")
        print(f"  videos/day:      {schedule.videos_per_day}")
        print(f"  smart schedule:  {'on' if schedule.enabled else 'off'}")
        print(f"  tolerance:       ±{schedule.occupancy_tolerance_minutes} min")
        print(f"  min lead time:   {schedule.min_lead_minutes} min")
        print(f"  horizon:         {schedule.horizon_days} days")

        print("\nCONFIGURED SLOTS")
        print(f"  every day:       {' · '.join(slots.get(None, ())) or '-'}")
        for weekday, values in sorted((key, value) for key, value in slots.items() if key is not None):
            print(f"  weekday {weekday}:       {' · '.join(values)}")

        print("\nREMOTE UPCOMING (YouTube; today onward)")
        today = now.astimezone(ZoneInfo(zone)).date()
        rows = db.scalars(select(YouTubeScheduleEntry).where(YouTubeScheduleEntry.channel_id == schedule.channel_id).order_by(YouTubeScheduleEntry.occupies_at)).all()
        shown = [row for row in rows if planner.local_date(row.occupies_at, zone) >= today]
        if not shown:
            print("  (none)")
        for row in shown:
            publish_at = row.publish_at.replace(tzinfo=UTC).strftime("%Y-%m-%dT%H:%M:%SZ") if row.publish_at else "-"
            print(f"  {_local(row.occupies_at, zone)}  {row.kind:<9} privacy={row.privacy_status:<8} publishAt={publish_at}  {row.video_id}")

        state = schedule_authority.smart_state(db, settings, SecretStore(), GoogleYouTubeProvider(), schedule.channel_id, refresh=False, days=max(1, args.days), now=now)
        first = state["days"][0] if state["days"] else None
        print("\nTODAY")
        if first:
            print(f"  published count: {first['published']}")
            print(f"  scheduled count: {first['scheduled']}")
            print(f"  reserved count:  {first['reserved']} (ClipForge uploads in flight)")
            print(f"  target:          {first['target']}{' (full)' if first['full'] else ''}")
        for day in state["days"]:
            print(f"  {day['date']}: " + "  ".join(f"{slot['local_time']} {slot['status']}" for slot in day["slots"]))

        print("\nNEXT FREE SLOT")
        recommendation = state["recommendation"] or state["cached_recommendation"]
        if recommendation:
            label = "" if state["recommendation"] else "  (from the cached schedule; YouTube not re-checked)"
            print(f"  {recommendation['local_date']} {recommendation['local_time']} {zone} ({recommendation['abbreviation']}, {recommendation['utc_offset']}) = {recommendation['publish_at']}{label}")
            print(f"  why: {recommendation['reason']}")
        elif state["horizon_full"]:
            print(f"  none: every slot in the next {schedule.horizon_days} days is taken")
        else:
            print("  unknown: the YouTube schedule could not be verified (run with --refresh)")

        fresh = state["freshness"]
        print("\nSCHEDULE CACHE")
        age = "" if fresh["age_seconds"] is None else f" ({fresh['age_seconds'] // 60} min ago)"
        print(f"  last refreshed:  {fresh['checked_at'] or 'never'}{age}")
        print(f"  state:           {'fresh' if fresh['fresh'] else 'stale'}{'' if fresh['complete'] or not fresh['checked_at'] else ' (last scan was cut off)'}")
        if fresh["error"]:
            print(f"  last error:      {fresh['error']['code']}: {fresh['error']['message']}")

        analysis = schedule_learning.analyze(db, schedule)
        print("\nLEARNING")
        print(f"  eligible videos: {analysis['eligible_count']} (needs {analysis['min_eligible']}; age bucket {analysis['reference_age_bucket']})")
        print(f"  learned recommendation available: {'yes' if analysis['available'] else 'no'}")
        if analysis["available"]:
            print(f"  current:   {' · '.join(analysis['current'])}")
            print(f"  suggested: {' · '.join(analysis['suggested'])} (based on {analysis['based_on']} Shorts; never applied automatically)")
        elif analysis.get("reason"):
            print(f"  reason: {analysis['reason']}")
        db.rollback()


if __name__ == "__main__":
    main()
