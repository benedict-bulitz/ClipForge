"""TikTok FILE_UPLOAD chunk plan - regression for "The chunk size is invalid".

Rules (TikTok Media Transfer Guide): chunks are 5-64 MB, the final chunk may
carry the trailing bytes up to 128 MB, total_chunk_count = floor(video_size /
chunk_size) within 1..1000, a video under 5 MB is uploaded whole and a video
over 64 MB in several chunks.  "MB" is not defined, so every plan must be
valid under the decimal AND the binary reading.
"""
from __future__ import annotations

import pytest
from publishing_support import FakeTikTok, apis, connect_tiktok, publishing_settings, social_project

from clipforge.publishing import connections, publications, tiktok
from clipforge.publishing.errors import PublishingApiError
from clipforge.publishing.publications import PublicationRequest
from clipforge.security.secrets import SecretStore
from clipforge.youtube import uploads

MB, MIB = 1000 * 1000, 1024 * 1024


def assert_valid_for_tiktok(size: int, chunk: int, total: int) -> None:
    """TikTok's rules, checked against both meanings of "MB"."""
    ranges = tiktok.chunk_ranges(size, chunk, total)
    assert 1 <= total <= 1000
    assert total == size // chunk, "total_chunk_count must be floor(video_size / chunk_size)"
    assert chunk <= size, "chunk_size may never exceed video_size"
    assert ranges[0][0] == 0 and ranges[-1][1] == size - 1
    assert all(ranges[i][1] + 1 == ranges[i + 1][0] for i in range(len(ranges) - 1)), "contiguous"
    lengths = [last - first + 1 for first, last in ranges]
    for unit in (MB, MIB):
        if size < 5 * unit:
            assert (chunk, total) == (size, 1), "under 5 MB: uploaded whole"
            continue
        if size > 64 * min(MB, MIB):
            assert total > 1, "over 64 MB: several chunks"
        assert chunk <= 64 * unit, "chunk_size <= 64 MB"
        if total > 1:
            assert chunk >= 5 * unit, "chunk_size >= 5 MB"
            assert all(length == chunk for length in lengths[:-1])
            assert chunk <= lengths[-1] <= 128 * unit, "final chunk: chunk_size + remainder <= 128 MB"
        else:
            assert lengths == [size] and size <= 64 * unit


@pytest.mark.parametrize(("label", "size", "expected"), [
    ("under 5 MB", 3 * MB, (3 * MB, 1)),
    ("just under 5 MiB", 5 * MIB - 1, (5 * MIB - 1, 1)),
    ("exactly 5 MiB", 5 * MIB, (5 * MIB, 1)),
    ("between 5 and 10 MB (the live failure)", 7 * MIB, (7 * MIB, 1)),
    ("just under 10 MB", 9_999_999, (9_999_999, 1)),
    ("exactly 10 MB", 10 * MB, (10 * MB, 1)),
    ("exactly 10 MiB", 10 * MIB, (10 * MIB, 1)),
    ("between 10 and 64 MB", 23 * MIB, (23 * MIB, 1)),
    ("TikTok's example size", 50_000_123, (50_000_123, 1)),
    ("exactly 64 MB", 64 * MB, (64 * MB, 1)),
    ("just over 64 MB", 64 * MB + 1, (10 * MB, 6)),
    ("64 MiB (over 64 MB decimal)", 64 * MIB, (10 * MB, 6)),
    ("over 64 MB", 100 * MB, (10 * MB, 10)),
    ("over 64 MB with a remainder", 100 * MB + 123, (10 * MB, 10)),
    ("1 GB", 1000 * MB, (10 * MB, 100)),
])
def test_chunk_plan(label, size, expected):
    assert tiktok.chunk_plan(size) == expected, label
    assert_valid_for_tiktok(size, *expected)


def test_one_chunk_uploads_never_declare_a_chunk_larger_than_the_video():
    for size in range(1, 64 * MB + 1, 997_331):  # dense sweep up to 64 MB
        chunk, total = tiktok.chunk_plan(size)
        assert (chunk, total) == (size, 1)
    # the exact sizes the old plan got wrong (chunk_size 10 MiB > video_size)
    for size in (5 * MIB, 6 * MIB, 8 * MIB + 17, 10 * MB, 10 * MIB - 1):
        chunk, _total = tiktok.chunk_plan(size)
        assert chunk == size <= 10 * MIB


def test_every_size_yields_a_valid_plan():
    sizes = [1, 1000, 5 * MB - 1, 5 * MB, 5 * MIB, 63 * MIB, 64 * MB, 64 * MB + 1, 64 * MIB, 65 * MIB, 127 * MB, 128 * MB + 5, 999 * MB + 7, 4 * 1000 * MB, 4 * 1024 * MIB]
    sizes += list(range(64 * MB + 1, 400 * MB, 7_777_777))
    for size in sizes:
        assert_valid_for_tiktok(size, *tiktok.chunk_plan(size))


def test_final_chunk_carries_the_remainder():
    size = 100 * MB + 123
    chunk, total = tiktok.chunk_plan(size)
    ranges = tiktok.chunk_ranges(size, chunk, total)
    assert len(ranges) == total == 10
    assert ranges[:-1] == [(index * chunk, (index + 1) * chunk - 1) for index in range(9)]
    assert ranges[-1] == (9 * chunk, size - 1) and size - 9 * chunk == chunk + 123


def test_huge_files_stay_within_1000_chunks_and_invalid_sizes_are_refused():
    size = 20 * 1000 * MB  # 20 GB: 10 MB chunks would be 2000 chunks
    chunk, total = tiktok.chunk_plan(size)
    assert total <= 1000 and chunk <= 64 * MB
    assert_valid_for_tiktok(size, chunk, total)
    with pytest.raises(PublishingApiError) as error:
        tiktok.chunk_plan(0)
    assert error.value.code == "invalid_media" and not error.value.retryable
    with pytest.raises(PublishingApiError):
        tiktok.chunk_plan(200 * 1000 * MB)  # would need chunks > 64 MB


@pytest.fixture(autouse=True)
def _reset():
    connections.reset_cache()
    uploads._SHA_CACHE.clear()
    yield
    connections.reset_cache()


@pytest.mark.parametrize(("limits", "content_size", "expected_chunks"), [
    (None, 7 * 1024, 1),  # the real limits: a small file is one whole chunk
    ({"MAX_CHUNK": 10_000, "MIN_CHUNK": 1_000, "DEFAULT_CHUNK": 4_000, "MAX_FINAL_CHUNK": 20_000}, 24_003, 6),  # scaled-down >64 MB case
])
def test_direct_post_declares_and_sends_exactly_the_planned_chunks(db, tmp_path, monkeypatch, limits, content_size, expected_chunks):
    for name, value in (limits or {}).items():
        monkeypatch.setattr(tiktok, name, value)
    settings, store, fake = publishing_settings(tmp_path), SecretStore(), FakeTikTok()
    fake.add_user("open-a", "alpha")
    account = connect_tiktok(db, settings, store, fake, "open-a")
    content = bytes(range(256)) * (content_size // 256) + b"x" * (content_size % 256)
    project = social_project(db, settings, content)
    request = PublicationRequest(
        account_id=account.id, base_revision=project.current_revision,
        tiktok={"caption": "x", "privacy_level": "SELF_ONLY", "is_aigc": False, "music_usage_confirmed": True},
    )
    row, _run_now = publications.request_publication(db, project, settings, store, apis(fake), request)
    publications.run(db, row.id, settings, store, apis(fake))
    db.refresh(row)
    assert row.state == "processing", row.last_error_message
    source_info = next(detail for name, detail in fake.calls if name == "init")["source_info"]
    assert source_info["video_size"] == content_size and source_info["total_chunk_count"] == expected_chunks
    assert source_info["chunk_size"] <= content_size
    assert source_info["total_chunk_count"] == content_size // source_info["chunk_size"]
    sent = [(chunk["first"], chunk["last"]) for chunk in fake.chunks]
    assert len(sent) == expected_chunks and sent[0][0] == 0 and sent[-1][1] == content_size - 1
    assert bytes(fake.posts[row.remote_container_id]["data"]) == content
