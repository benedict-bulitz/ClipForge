"""Multi-platform publishing: one account authority, per-account credentials,
provider capabilities, Instagram/TikTok publications and ClipForge-owned schedules.

Modules: ``accounts`` (the connection authority for every platform),
``capabilities`` (what each platform/account can do; the UI adapts to it),
``oauth`` (pending sign-ins, PKCE), ``tiktok`` / ``instagram`` (the HTTP
boundaries, faked in tests), ``connections`` (per-platform sign-in and access
tokens), ``publications`` (render-bound publication records and their
execution), ``scheduler`` (the persistent due-work loop) and ``read_model``
(the unified view over YouTube uploads and social publications).
"""
