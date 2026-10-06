"""Read-only YouTube Learning Loop diagnostic; run from apps/api with PYTHONPATH=.

Prints the connected channel, the project/revision <-> video mapping, the
latest stored metrics, the raw retention points (first few + largest drops)
and the retention -> scene mapping.  Nothing is written and no secret, token
or upload session URI is ever printed.

    PYTHONPATH=. python scripts/youtube_diagnostics.py [project_id]
    PYTHONPATH=. python scripts/youtube_diagnostics.py --video VIDEO_ID
    PYTHONPATH=. python scripts/youtube_diagnostics.py --learning
    PYTHONPATH=. python scripts/youtube_diagnostics.py --performance [last10|28d|90d|all]
    PYTHONPATH=. python scripts/youtube_diagnostics.py --analytics-live VIDEO_ID [VIDEO_ID ...]

``--live`` additionally reads fresh status/metrics from Google (read-only
calls: videos.list and Analytics reports) without persisting anything.

``--analytics-live`` traces the YouTube Analytics ingestion of one video: the
stored captures (with YouTube's raw answers), the exact production queries
sent again live, and a few minimal comparison queries.  Read-only GET
requests only; the database session refuses every write.
"""

import argparse
from datetime import UTC, datetime, timedelta
from itertools import pairwise
from zoneinfo import ZoneInfo

from sqlalchemy import select

from clipforge.config import get_settings
from clipforge.database import SessionLocal
from clipforge.models import Project, YouTubeUpload
from clipforge.security.secrets import SecretStore
from clipforge.youtube import analytics, connection, learning, library, performance
from clipforge.youtube import status as status_authority
from clipforge.youtube.provider import (
    ANALYTICS_API,
    GoogleYouTubeProvider,
    YouTubeApiError,
    has_capability,
)
from clipforge.youtube.uploads import STATUS_PARTS


def _pct(value) -> str:
    return "n/a" if value is None else f"{value:.0%}"


def _fmt(value) -> str:
    if value is None:
        return "n/a"
    return f"{value:,.2f}".rstrip("0").rstrip(".") if isinstance(value, float) else str(value)


def _print_upload(db, upload: YouTubeUpload, settings, *, live: bool) -> None:
    print("\nYouTube:")
    print(f"  upload_id:        {upload.id}")
    print(f"  project/revision: {upload.project_id} / v{upload.project_revision} (render v{upload.render_revision})")
    print(f"  render sha256:    {upload.render_sha256[:16]}…")
    print(f"  channel_id:       {upload.channel_id}")
    print(f"  video_id:         {upload.youtube_video_id or '-'}")
    print(f"  state:            {upload.state} (uploadStatus={upload.upload_status or '-'}, processing={upload.processing_status or '-'})")
    print(f"  privacy@upload:   {upload.privacy_status} (see REMOTE for the current state)")
    print(f"  requested at:     {upload.publish_at or '-'} (schedule={upload.schedule_status})")
    print(f"  published_at:     {upload.published_at or '-'} ({upload.published_source or '-'})")
    print(f"  content type:     {upload.content_type or 'not confirmed by YouTube'}")
    print(f"  source:           {upload.source_kind or '-'} (uploaded directly from ClipForge storage)")
    print(f"  audience:         sent={upload.made_for_kids} youtube={upload.made_for_kids_confirmed}")
    print(f"  synthetic media:  {upload.contains_synthetic_media}")
    print(f"  thumbnail:        {upload.thumbnail_upload_status} ({upload.thumbnail_source or '-'} {upload.thumbnail_asset or ''}) {upload.thumbnail_failure_reason or ''}")
    print(f"  schedule:         {upload.schedule_local_time or '-'} {upload.schedule_timezone or ''} -> {upload.publish_at or '-'}")
    if upload.deleted_on_youtube:
        print("  !! deleted on YouTube")
    current = status_authority.current_status(upload)
    print("\nREQUESTED (history, never current truth):")
    print(f"  privacy:          {upload.requested_visibility}")
    print(f"  publishAt:        {upload.publish_at or '-'}")
    print(f"  local time/zone:  {upload.schedule_local_time or '-'} {upload.schedule_timezone or ''}")
    print("\nREMOTE (YouTube, current authority):")
    print(f"  current state:    {current['label']}{' (stale: ' + str(current['stale_reason']) + ')' if current['stale'] else ''}")
    print(f"  privacyStatus:    {upload.remote_privacy_status or '-'}")
    print(f"  uploadStatus:     {upload.upload_status or '-'}")
    print(f"  publishAt:        {upload.remote_publish_at or '-'}")
    print(f"  publishedAt:      {upload.remote_published_at or '-'} (snippet; publication time only while public)")
    print(f"  first seen public:{' ' + str(upload.first_observed_public_at) if upload.first_observed_public_at else ' -'}")
    print(f"  last checked:     {upload.remote_status_checked_at or 'never'}")
    if upload.remote_status_error_code:
        print(f"  last refresh err: {upload.remote_status_error_code}: {upload.remote_status_error}")
    print("\nLIVE STATS (videos.list):")
    print(f"  views={_fmt(upload.remote_view_count)} likes={_fmt(upload.remote_like_count)} comments={_fmt(upload.remote_comment_count)}")
    if upload.last_error_code:
        print(f"  last error:       {upload.last_error_code}: {upload.last_error_message}")
    if upload.analytics_error_code:
        print(f"  analytics error:  {upload.analytics_error_code}: {upload.analytics_error_message}")
    if live and upload.youtube_video_id:
        _live(db, upload, settings)
    report = learning.performance_report(db, upload, min_sample=settings.youtube_baseline_min_sample)
    print("\nANALYTICS (processed, YouTube Analytics API):")
    print(f"  state:            {report.get('analytics_state')}")
    print(f"  last fetch:       {upload.last_analytics_sync_at or '-'} (last attempt {upload.last_analytics_attempt_at or '-'})")
    print(f"  retention:        {'yes' if (report.get('retention') or {}).get('point_count') else 'no'}")
    print(f"\nPerformance status: {report['status']}")
    snapshots = report.get("snapshots") or []
    print(f"Snapshots stored: {len(snapshots)}")
    for item in snapshots[-6:]:
        print(f"  {item['fetched_at']}  age={_fmt(item['published_age_hours'])}h bucket={item['age_bucket']:<6} status={item['status']:<11} views={_fmt(item['views'])} engaged={_fmt(item['engagedViews'])} avg%={_fmt(item['averageViewPercentage'])}")
    latest = report.get("latest_snapshot")
    if latest:
        print("\nMetrics (latest snapshot, raw API values):")
        for name, item in sorted((latest.get("metrics") or {}).items()):
            detail = _fmt(item["value"]) if item["availability"] == "available" else f"{item['availability']} ({item['reason']})"
            print(f"  {name:<26} {detail}   [{item['source']}]")
    retention = report.get("retention") or {}
    print(f"\nRetention: {retention.get('point_count', 0)} raw points, metrics={retention.get('metrics') or []}")
    if retention.get("snapshot_id"):
        from clipforge.models import YouTubeAnalyticsSnapshot

        snapshot = db.get(YouTubeAnalyticsSnapshot, retention["snapshot_id"])
        points = sorted(snapshot.retention_points, key=lambda item: item.elapsed_video_ratio)
        for point in points[:5]:
            print(f"  ratio={point.elapsed_video_ratio:.2f} t={point.video_second:.2f}s watch={_fmt(point.audience_watch_ratio)} rel={_fmt(point.relative_retention_performance)} stopped={_fmt(point.stopped_watching)}")
        drops = sorted(
            (((b.audience_watch_ratio or 0) - (a.audience_watch_ratio or 0), a, b) for a, b in pairwise(points)),
            key=lambda item: item[0],
        )[:3]
        print("  largest drops between adjacent buckets:")
        for delta, a, b in drops:
            print(f"    {a.video_second:.2f}s -> {b.video_second:.2f}s: {delta:+.3f}")
    opening = report.get("opening_retention") or {}
    if opening.get("points"):
        print("\nOpening retention (audienceWatchRatio at YouTube buckets; not 'Stayed to watch'):")
        for item in opening["points"]:
            print(f"  ~{item['target_second']:.0f}s: {_pct(item.get('audience_watch_ratio'))} (bucket at {item.get('bucket_second', '-')}s)")
        hook = opening.get("hook") or {}
        print(f"  hook: strategy={hook.get('strategy')} verbal={hook.get('verbal_hook')!r} on-screen={hook.get('on_screen_hook')!r}")
    scenes = report.get("scene_retention") or []
    if scenes:
        print("\nScene mapping:")
        for row in scenes:
            if row["status"] != "ok":
                print(f"  Scene {row['index']} {row['start']:.2f}-{row['end']:.2f}s: {row['status']}")
                continue
            flag = "  <- notable drop" if row.get("notable_drop") else ""
            print(f"  Scene {row['index']} · {row.get('story_role') or '-'} {row['start']:.2f}-{row['end']:.2f}s: {_pct(row['retention_entering'])} -> {_pct(row['retention_leaving'])} Δ {row['retention_delta']:+.1%}{flag}")
    if report.get("classification"):
        print(f"\nClassification: {report['classification']['label']} (baseline n={report['baseline']['sample_size']}, min {report['baseline']['min_sample']})")


def _live(db, upload: YouTubeUpload, settings) -> None:
    provider, store = GoogleYouTubeProvider(), SecretStore()
    print("\nLive (read-only, not saved):")
    try:
        _connection, token = connection.access_token(db, settings, store, provider, capability="read", channel_id=upload.channel_id)
        items = provider.list_videos(token, [upload.youtube_video_id], STATUS_PARTS)
        if not items:
            print("  video not found on YouTube (deleted?)")
            return
        status = items[0].get("status") or {}
        stats = items[0].get("statistics") or {}
        print(f"  privacy={status.get('privacyStatus')} uploadStatus={status.get('uploadStatus')} publishAt={status.get('publishAt')} publishedAt={(items[0].get('snippet') or {}).get('publishedAt')}")
        print(f"  views={stats.get('viewCount')} likes={stats.get('likeCount')} comments={stats.get('commentCount')}")
        _connection, token = connection.access_token(db, settings, store, provider, capability="analytics", channel_id=upload.channel_id)
        start, end = analytics.report_window(upload.published_at or upload.created_at or datetime.now(UTC), datetime.now(UTC))
        metrics, _raw = analytics.fetch_video_metrics(provider, token, upload.youtube_video_id, start, end)
        for name, item in metrics.items():
            print(f"  {name:<26} {_fmt(item['value']) if item['availability'] == 'available' else item['availability']}")
        status_name, points, used, _raw = analytics.fetch_retention(provider, token, upload.youtube_video_id, start, end)
        print(f"  retention: {status_name}, {len(points)} points, metrics={list(used)}")
    except YouTubeApiError as exc:
        print(f"  {exc.code}: {exc.message}")
    finally:
        db.rollback()  # never persist anything from a diagnostic


def _performance(db, record, scope: str, settings) -> None:
    """Channel Performance trace: every library video of the channel, where its
    numbers come from, and whether the overview counts it (store only)."""
    now = datetime.now(UTC)
    channel_id = record.channel_id if record else None
    uploads_ = db.scalars(select(YouTubeUpload).where(library.library_condition())).all()
    summaries = library._analytics_summaries(db, None, performance.PERFORMANCE_METRICS)
    durations = performance._durations(db, (item.fingerprint_id for item in uploads_ if item.fingerprint_id))
    print(f"\nLibrary videos: {len(uploads_)} (connected channel {channel_id})")
    for upload in sorted(uploads_, key=library._sort_date, reverse=True):
        summary = summaries.get(upload.id)
        live = status_authority.live_stats(upload)
        history = analytics._snapshots(db, upload.id)
        published = upload.published_at
        age = (now - status_authority._utc(published)).total_seconds() / 3600 if published else None
        loaded = performance._row(upload, summary, durations.get(upload.fingerprint_id or "")) if published else None
        if upload.channel_id != channel_id and channel_id:
            verdict = "EXCLUDED: other channel"
        elif published is None:
            verdict = "EXCLUDED: not published (no published_at)"
        elif loaded is None:
            verdict = "EXCLUDED: no analytics snapshot with data and no videos.list statistics"
        else:
            row = loaded[0]
            sources = {name: row.source_of(name) for name in ("views", "likes", "engagedViews", "averageViewDuration", "averageViewPercentage")}
            verdict = f"INCLUDED; value sources: {sources}"
        state = library._analytics_state(upload, summary, now)
        print(f"\n  {upload.youtube_video_id} upload={upload.id} channel={upload.channel_id} '{(upload.title or '')[:50]}'")
        print(f"    state={library.library_state(upload, now)} published_at={published} ({upload.published_source}) analytics={state}")
        print(f"    library views/likes source: videos.list live_stats={'none' if live is None else {k: live[k] for k in ('views', 'likes', 'comments', 'checked_at')}}")
        print(f"    analytics error={upload.analytics_error_code} last_attempt={upload.last_analytics_attempt_at} last_sync={upload.last_analytics_sync_at}")
        print(f"    fingerprint={upload.fingerprint_id} rendered_duration={durations.get(upload.fingerprint_id or '')}")
        for snap in history:
            values = {item.name: item.value for item in snap.metrics if item.availability == "available"}
            print(f"    snapshot {snap.fetched_at} source={snap.source} bucket={snap.age_bucket} age={_fmt(snap.published_age_hours)}h status={snap.status} retention={snap.retention_status} channel={snap.channel_id}")
            print(f"      available: {values or 'none'}")
        if not history:
            print("    snapshots: none")
        if age is not None:
            print(f"    due-only refresh now would capture bucket: {analytics.due_bucket(history, age, now, published)}")
        print(f"    overview: {verdict}")
    result = performance.performance_overview(db, scope=scope, min_sample=settings.youtube_baseline_min_sample, now=now)
    print(f"\nGET /api/videos/performance?scope={result['scope']}")
    print(f"  channel={result['channel']} video_count={result['video_count']} eligible_total={result['eligible_total']} sources={result['sources']}")
    for name in result["primary"]:
        print(f"  {name}: {result['metrics'][name]}")


PACIFIC = ZoneInfo("America/Los_Angeles")
ROW_PREVIEW = 8


class _RecordingProvider:
    """The real provider, recording each Analytics request and its outcome."""

    def __init__(self, provider) -> None:
        self.provider = provider
        self.calls: list[tuple[dict, YouTubeApiError | None, dict | None]] = []

    def analytics_report(self, token: str, params: dict) -> dict:
        try:
            response = self.provider.analytics_report(token, params)
        except YouTubeApiError as exc:
            self.calls.append((dict(params), exc, None))
            raise
        self.calls.append((dict(params), None, response))
        return response


def _read_only(db) -> None:
    """A diagnostic never writes: a commit, or a flush with pending changes, raises."""
    flush = db.flush

    def refuse(*_args, **_kwargs):
        raise RuntimeError("youtube_diagnostics is read-only")

    def guarded_flush(*args, **kwargs):
        if db.new or db.dirty or db.deleted:
            refuse()
        return flush(*args, **kwargs)  # autoflush runs before every query; nothing to write is fine

    db.flush = guarded_flush
    db.commit = refuse


def _print_response(params: dict, error: YouTubeApiError | None, response: dict | None, indent: str = "  ") -> None:
    print(f"{indent}GET {ANALYTICS_API}")
    for key in ("ids", "startDate", "endDate", "metrics", "dimensions", "filters", "sort", "maxResults"):
        if key in params:
            print(f"{indent}  {key}={params[key]}")
    if error is not None:
        print(f"{indent}HTTP {error.status_code or '?'} -> {error.code} (reason={error.reason or '-'}): {error.message}")
        return
    headers = [str(item.get("name")) for item in (response or {}).get("columnHeaders") or [] if isinstance(item, dict)]
    rows = (response or {}).get("rows") or []
    print(f"{indent}HTTP 200  column headers={headers}  rows={len(rows)}")
    for row in rows[:ROW_PREVIEW]:
        print(f"{indent}  {row}")
    if len(rows) > ROW_PREVIEW:
        print(f"{indent}  ... {len(rows) - ROW_PREVIEW} more")


def _ask(provider, token: str, label: str, params: dict) -> tuple[YouTubeApiError | None, dict | None]:
    print(f"\n[{label}]")
    try:
        response = provider.analytics_report(token, params)
    except YouTubeApiError as exc:
        _print_response(params, exc, None)
        return exc, None
    _print_response(params, None, response)
    return None, response


def _rows(response: dict | None) -> int:
    return len((response or {}).get("rows") or [])


def _stored_evidence(db, upload: YouTubeUpload) -> None:
    print("\nSTORED CAPTURES (what YouTube answered at the time, from raw_responses):")
    snapshots = analytics._snapshots(db, upload.id)
    if not snapshots:
        print("  none")
    for snap in snapshots:
        print(f"  {snap.fetched_at}  age={_fmt(snap.published_age_hours)}h bucket={snap.age_bucket} source={snap.source} status={snap.status} retention={snap.retention_status} range={snap.date_range or {}}")
        for key, value in sorted((snap.raw_responses or {}).items()):
            if not isinstance(value, dict):
                continue
            if "error" in value:
                print(f"      {key}: ERROR {value['error']}")
                continue
            headers = [str(item.get("name")) for item in value.get("columnHeaders") or [] if isinstance(item, dict)]
            print(f"      {key}: headers={headers} rows={len(value.get('rows') or [])}")


def _analytics_live(db, settings, video_id: str) -> None:
    """One video's Analytics ingestion, end to end, read-only."""
    upload = db.scalar(select(YouTubeUpload).where(YouTubeUpload.youtube_video_id == video_id))
    print(f"\n{'=' * 78}\nVIDEO: {video_id}")
    if upload is None:
        print("  no ClipForge upload maps to this video ID")
        return
    record = connection.peek_channel(db, upload.channel_id)
    now = datetime.now(UTC)
    published = upload.published_at
    print(f"  upload={upload.id} channel={upload.channel_id} (connected: {record.channel_id if record else '-'}, match={bool(record and record.channel_id == upload.channel_id)})")
    print(f"  title='{upload.title}' content_type={upload.content_type or '-'} privacy={upload.remote_privacy_status} deleted={upload.deleted_on_youtube}")
    print(f"  live stats (videos.list): views={upload.remote_view_count} likes={upload.remote_like_count} checked={upload.remote_status_checked_at}")
    print(f"  analytics error stored: {upload.analytics_error_code or '-'} {upload.analytics_error_message or ''}")
    _stored_evidence(db, upload)
    if published is None:
        print("\nPUBLISHED_AT: none (not public yet) - production never queries Analytics for it")
        return
    published_utc = published.replace(tzinfo=UTC) if published.tzinfo is None else published.astimezone(UTC)
    start, end = analytics.report_window(published_utc, now)
    today_pt = now.astimezone(PACIFIC).date()
    published_pt = published_utc.astimezone(PACIFIC).date()
    print("\nPUBLISHED_AT:")
    print(f"  UTC {published_utc.isoformat()}  |  Pacific {published_utc.astimezone(PACIFIC).isoformat()} ({upload.published_source})")
    print(f"  age now: {(now - published_utc).total_seconds() / 3600:.1f} h")
    print("\nDATE AUDIT:")
    print(f"  production window startDate={start} endDate={end} (DATEs; start<=end: {start <= end})")
    print(f"  publication day: UTC {published_utc.date()} / Pacific {published_pt} -> inside window: {start <= published_pt <= end and start <= published_utc.date() <= end}")
    print(f"  today: UTC {now.date()} / Pacific {today_pt} -> endDate after today in Pacific: {end > today_pt}")

    provider = _RecordingProvider(GoogleYouTubeProvider())
    store = SecretStore()
    client = connection.oauth_client(settings)
    print("\nAUTH:")
    print(f"  stored scopes: {', '.join(scope.rsplit('/', 1)[-1] for scope in (record.granted_scopes if record else None) or []) or '-'}")
    print(f"  analytics capability (stored scopes): {has_capability((record.granted_scopes if record else None) or [], 'analytics')}")
    if client is None:
        print("  OAuth client not configured")
        return
    try:
        refresh_token = connection.stored_refresh_token(store, record)
        if not refresh_token:
            print("  no refresh token stored - reconnect YouTube")
            return
        grant = provider.provider.refresh_access_token(client, refresh_token)  # token only; nothing is saved
        token = grant.access_token
        if grant.scopes:
            print(f"  scopes Google reports for the token: {', '.join(scope.rsplit('/', 1)[-1] for scope in grant.scopes)}")
        identity = provider.provider.get_my_channel(token)
        print(f"  channel==MINE resolves to: {identity.channel_id} ({identity.title}) -> same as the video's channel: {identity.channel_id == upload.channel_id}")
    except YouTubeApiError as exc:
        print(f"  token/channel lookup failed: {exc.code} (HTTP {exc.status_code}): {exc.message}")
        return

    print("\nA. EXACT PRODUCTION QUERIES (the functions refresh_analytics calls, same dates):")
    try:
        metrics, _raw = analytics.fetch_video_metrics(provider, token, video_id, start, end)
        content_type, _raw_type = analytics.fetch_content_type(provider, token, video_id, start, end)
        retention_status, points, used, _raw_retention = analytics.fetch_retention(provider, token, video_id, start, end)
    except YouTubeApiError as exc:
        metrics = None
        print(f"  production path raised {exc.code} (HTTP {exc.status_code}, reason={exc.reason}): {exc.message}")
        print("  -> refresh_analytics stores this as analytics_error_code (library: failed/auth_error), not as processing")
    for params, error, response in provider.calls:
        _print_response(params, error, response, indent="    ")
        print()
    if metrics is not None:
        print("NORMALIZED RESULT (what ClipForge would store; nothing is stored):")
        for name, item in metrics.items():
            shown = _fmt(item["value"]) if item["availability"] == "available" else f"{item['availability']} ({item['reason']})"
            print(f"  {name:<26} {shown}")
        print(f"  content type: {content_type or '-'}   retention: {retention_status} ({len(points)} points, metrics={list(used)})")
        print(f"  snapshot status: {analytics.snapshot_status(metrics, retention_status)}")
    production = next((response for params, _error, response in provider.calls if params.get("dimensions") == "video" and "," in params.get("metrics", "")), None)
    production_error = next((error for params, error, _response in provider.calls if params.get("dimensions") == "video" and "," in params.get("metrics", "")), None)

    filters = f"video=={video_id}"
    base = {"ids": "channel==MINE", "startDate": start.isoformat(), "endDate": end.isoformat(), "filters": filters}
    print("\nB. COMPARISON QUERIES (read-only):")
    _e, minimal = _ask(provider.provider, token, "B1 smallest query: views, no dimensions, same dates", {**base, "metrics": "views"})
    _e, all_no_dim = _ask(provider.provider, token, "B2 all production metrics, no dimensions", {**base, "metrics": ",".join(analytics.VIDEO_METRICS)})
    wide_start = min(start, published_pt) - timedelta(days=1)
    _e, by_day = _ask(provider.provider, token, "B3 views by day, Pacific publication day - 1 .. today", {**base, "startDate": wide_start.isoformat(), "endDate": max(end, today_pt).isoformat(), "metrics": "views", "dimensions": "day"})
    _e, explicit = _ask(provider.provider, token, "B4 explicit channel ID instead of MINE", {**base, "ids": f"channel=={upload.channel_id}", "metrics": "views"})
    _e, channel_days = _ask(provider.provider, token, "B5 whole channel, views by day, last 14 days (no video filter)", {
        "ids": "channel==MINE", "startDate": (today_pt - timedelta(days=14)).isoformat(), "endDate": today_pt.isoformat(), "metrics": "views", "dimensions": "day",
    })
    if production_error is not None and production_error.code == "bad_request":
        without = tuple(name for name in analytics.VIDEO_METRICS if name != "engagedViews")
        _ask(provider.provider, token, "B6 production query without engagedViews", {**base, "metrics": ",".join(without), "dimensions": "video"})
        _ask(provider.provider, token, "B7 engagedViews alone", {**base, "metrics": "engagedViews", "dimensions": "video"})

    print("\nVERDICT (from the answers above):")
    if production_error is not None:
        print(f"  production metrics query FAILED: {production_error.code} (HTTP {production_error.status_code}) - an error, not 'no data yet'")
    elif _rows(production):
        print("  production query returns data NOW -> earlier captures were taken before YouTube had processed the video (see their age above);")
        print("  the next due refresh stores it.")
    elif _rows(minimal) or _rows(all_no_dim):
        print("  production query (dimensions=video) is EMPTY but the same filter without dimensions HAS rows -> query shape bug")
    elif _rows(by_day):
        print("  daily rows exist for this video but not for the production range -> date range bug (compare dates above)")
    elif _rows(channel_days):
        print("  the channel has Analytics rows, this video has none yet -> YouTube has not processed this video's analytics (delay) or the video is not in this channel")
    else:
        print("  NO Analytics rows for the whole channel (MINE) in 14 days -> token/channel mapping (Brand account?) or channel-wide data delay")
    if explicit is not None and _rows(explicit) != _rows(minimal):
        print("  channel==MINE and the explicit channel ID answer differently -> channel mapping problem")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("project_id", nargs="?")
    parser.add_argument("--video", help="YouTube video ID")
    parser.add_argument("--live", action="store_true", help="also read fresh data from Google (read-only)")
    parser.add_argument("--learning", action="store_true", help="print the cross-video learning table")
    parser.add_argument("--performance", nargs="?", const="last10", metavar="SCOPE", help="trace the Channel Performance overview")
    parser.add_argument("--analytics-live", nargs="+", metavar="VIDEO_ID", help="trace the YouTube Analytics ingestion live (read-only)")
    args = parser.parse_args()
    settings = get_settings()
    with SessionLocal() as db:
        record = connection.get_connection(db)
        print("Connection:")
        if record is None:
            print("  not connected")
        else:
            print(f"  channel: {record.channel_title} ({record.channel_id}) status={record.status}")
            print(f"  scopes:  {', '.join(scope.rsplit('/', 1)[-1] for scope in record.granted_scopes or [])}")
        if args.analytics_live:
            _read_only(db)
            for video_id in args.analytics_live:
                _analytics_live(db, settings, video_id)
            db.rollback()
            return
        if args.performance:
            _performance(db, record, args.performance, settings)
            db.rollback()
            return
        if args.learning:
            if record is None:
                parser.error("Connect YouTube first")
            table = learning.learning_table(db, record.channel_id)
            print(f"\nLearning table: {table['video_count']} videos, {table['scene_count']} scenes ({table['caveat']})")
            for question in table["questions"]:
                print(f"  - {question['question']} [{question['status']}]")
                for statement in question["statements"]:
                    print(f"      {statement}")
            return
        if args.video:
            upload = db.scalar(select(YouTubeUpload).where(YouTubeUpload.youtube_video_id == args.video))
            if upload is None:
                parser.error("No ClipForge upload maps to that video ID")
            uploads_ = [upload]
        else:
            query = select(Project).order_by(Project.created_at.desc())
            if args.project_id:
                query = select(Project).where(Project.id == args.project_id)
            project = db.scalars(query).first()
            if project is None:
                parser.error("Project not found")
            print(f"\nProject:\n  {project.id} '{project.title}' current revision v{project.current_revision}")
            uploads_ = db.scalars(select(YouTubeUpload).where(YouTubeUpload.project_id == project.id).order_by(YouTubeUpload.created_at)).all()
            if not uploads_:
                print("\nYouTube:\n  not uploaded")
        for upload in uploads_:
            _print_upload(db, upload, settings, live=args.live)
        db.rollback()


if __name__ == "__main__":
    main()
