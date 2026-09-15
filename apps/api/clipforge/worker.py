from celery import Celery

from .config import get_settings

settings = get_settings()
celery_app = Celery(
    "clipforge",
    broker=settings.redis_url,
    backend=settings.redis_url,
    include=["clipforge.tasks"],
)
celery_app.conf.update(
    task_track_started=True,
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    task_routes={"clipforge.tasks.*": {"queue": "clipforge"}},
)
