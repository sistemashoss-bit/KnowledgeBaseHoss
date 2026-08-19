"""Scheduled retention of chat messages.

Deletes messages older than the retention window, removes their attachment
files from object storage (which the old in-request job leaked), and lets the
DB cascade drop the `message_attachments` rows.

Runs as an in-process daily scheduler started in the app lifespan, gated by a
Redis daily lock so only one instance runs it when several are deployed.
"""
import asyncio
import logging
from datetime import datetime, timedelta

from app.config import settings
from app.database import SessionLocal
from app.models import AuditLog, Message, MessageAttachment, SearchLog
from app import storage, valkey_client as vk

logger = logging.getLogger(__name__)

_LOCK_KEY = "msg_cleanup_daily"


def purge_old_messages(days: int) -> tuple[int, int]:
    """Delete messages older than `days`. Returns (messages_deleted, files_deleted)."""
    db = SessionLocal()
    files_deleted = 0
    try:
        cutoff = datetime.utcnow() - timedelta(days=days)

        # Delete storage objects first — we need the keys before the rows vanish.
        keys = (
            db.query(MessageAttachment.file_key)
            .join(Message, MessageAttachment.message_id == Message.id)
            .filter(Message.created_at < cutoff)
            .all()
        )
        for (key,) in keys:
            try:
                storage.delete_chat_file(key)
                files_deleted += 1
            except Exception:
                logger.warning("cleanup: could not delete chat file %s", key, exc_info=True)

        # Explicit ordered delete (attachments → messages) so it works whether or
        # not the DB-level ON DELETE CASCADE migration has been applied.
        old_msg_ids = db.query(Message.id).filter(Message.created_at < cutoff)
        db.query(MessageAttachment).filter(
            MessageAttachment.message_id.in_(old_msg_ids)
        ).delete(synchronize_session=False)
        messages_deleted = (
            db.query(Message)
            .filter(Message.created_at < cutoff)
            .delete(synchronize_session=False)
        )
        db.commit()
        return messages_deleted, files_deleted
    finally:
        db.close()


def purge_old_logs(days: int) -> tuple[int, int]:
    """Delete audit/search logs older than `days`. Returns (audit_deleted, search_deleted)."""
    db = SessionLocal()
    try:
        cutoff = datetime.utcnow() - timedelta(days=days)
        audit_deleted = (
            db.query(AuditLog)
            .filter(AuditLog.created_at < cutoff)
            .delete(synchronize_session=False)
        )
        search_deleted = (
            db.query(SearchLog)
            .filter(SearchLog.created_at < cutoff)
            .delete(synchronize_session=False)
        )
        db.commit()
        return audit_deleted, search_deleted
    finally:
        db.close()


def run_cleanup() -> dict | None:
    """Run all daily purges once, honouring the cross-instance daily lock.

    Returns a summary dict, or None if another instance already claimed today's run.
    """
    # When Redis is up, the lock ensures a single instance runs. When Redis is
    # down, try_acquire_daily_lock returns False; fall through and run anyway.
    if vk.available() and not vk.try_acquire_daily_lock(_LOCK_KEY):
        return None
    messages, files = purge_old_messages(settings.message_retention_days)
    audit_logs, search_logs = purge_old_logs(settings.audit_retention_days)
    return {
        "messages": messages,
        "chat_files": files,
        "audit_logs": audit_logs,
        "search_logs": search_logs,
    }


def _seconds_until(hour: int) -> float:
    now = datetime.utcnow()
    nxt = now.replace(hour=hour, minute=0, second=0, microsecond=0)
    if nxt <= now:
        nxt += timedelta(days=1)
    return (nxt - now).total_seconds()


def run_recurring_generation() -> int | None:
    """Materialize recurring task templates due today, honouring a daily lock.

    Returns the number of tasks created, or None if another instance claimed
    today's run.
    """
    from app.tasks.recurring import generate_due_tasks

    if vk.available() and not vk.try_acquire_daily_lock("recurring_task_gen"):
        return None
    return generate_due_tasks()


async def scheduler_loop() -> None:
    """Fire the daily jobs once a day at settings.cleanup_hour_utc. Never raises out."""
    while True:
        try:
            await asyncio.sleep(_seconds_until(settings.cleanup_hour_utc))
            result = await asyncio.to_thread(run_cleanup)
            if result is not None:
                logger.info("daily cleanup: %s", result)
            generated = await asyncio.to_thread(run_recurring_generation)
            if generated is not None:
                logger.info("recurring tasks generated: %s", generated)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("daily scheduler job failed")
            # Avoid a hot loop if the clock math / DB keeps failing.
            await asyncio.sleep(3600)
