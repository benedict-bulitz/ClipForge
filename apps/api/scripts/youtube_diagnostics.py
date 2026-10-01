"""Read-only YouTube Learning Loop diagnostic; run from apps/api with PYTHONPATH=.

Prints the connected channel, the project/revision <-> video mapping, the
latest stored metrics, the raw retention points (first few + largest drops)
and the retention -> scene mapping.  Nothing is written and no secret, token
or upload session URI is ever printed.

    PYTHONPATH=. python scripts/youtube_diagnostics.py [project_id]
    PYTHONPATH=. python scripts/youtube_diagnostics.py --video VIDEO_ID
    PYTHONPATH=. python scripts/youtube_diagnostics.py --learning
    PYTHONPATH=. python scripts/youtube_diagnostics.py --performance [last10|28d|90d|all]

``--live`` additionally reads fresh status/metrics from Google (read-only
calls: videos.list and Analytics reports) without persisting anything.
"""

import argparse
from datetime import UTC, datetime, timedelta
from itertools import pairwise

from sqlalchemy import select

from clipforge.config import get_settings
from clipforge.database import SessionLocal
from clipforge.models import Project, YouTubeUpload
from clipforge.security.secrets import SecretStore
from clipforge.youtube import analytics, connection, learning, library, performance
from clipforge.youtube import status as status_authority
from clipforge.youtube.provider import GoogleYouTubeProvider, YouTubeApiError
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
        _connection, token = connection.access_token(db, settings, store, provider, capability="read")
        items = provider.list_videos(token, [upload.youtube_video_id], STATUS_PARTS)
        if not items:
            print("  video not found on YouTube (deleted?)")
            return
        status = items[0].get("status") or {}
        stats = items[0].get("statistics") or {}
        print(f"  privacy={status.get('privacyStatus')} uploadStatus={status.get('uploadStatus')} publishAt={status.get('publishAt')} publishedAt={(items[0].get('snippet') or {}).get('publishedAt')}")
        print(f"  views={stats.get('viewCount')} likes={stats.get('likeCount')} comments={stats.get('commentCount')}")
        _connection, token = connection.access_token(db, settings, store, provider, capability="analytics")
        start = (upload.published_at or upload.created_at or datetime.now(UTC)) - timedelta(days=1)
        metrics, _raw = analytics.fetch_video_metrics(provider, token, upload.youtube_video_id, start.date(), datetime.now(UTC).date())
        for name, item in metrics.items():
            print(f"  {name:<26} {_fmt(item['value']) if item['availability'] == 'available' else item['availability']}")
        status_name, points, used, _raw = analytics.fetch_retention(provider, token, upload.youtube_video_id, start.date(), datetime.now(UTC).date())
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
            verdict = f"INCLUDED via {loaded[0].source}"
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
            print(f"    due-only refresh now would capture bucket: {analytics.due_bucket(history, age, now)}")
        print(f"    overview: {verdict}")
    result = performance.performance_overview(db, scope=scope, min_sample=settings.youtube_baseline_min_sample, now=now)
    print(f"\nGET /api/videos/performance?scope={result['scope']}")
    print(f"  channel={result['channel']} video_count={result['video_count']} eligible_total={result['eligible_total']} sources={result['sources']}")
    for name in result["primary"]:
        print(f"  {name}: {result['metrics'][name]}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("project_id", nargs="?")
    parser.add_argument("--video", help="YouTube video ID")
    parser.add_argument("--live", action="store_true", help="also read fresh data from Google (read-only)")
    parser.add_argument("--learning", action="store_true", help="print the cross-video learning table")
    parser.add_argument("--performance", nargs="?", const="last10", metavar="SCOPE", help="trace the Channel Performance overview")
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
