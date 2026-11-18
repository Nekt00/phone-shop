from django.utils import timezone
from django.db import transaction, close_old_connections
from datetime import timedelta
import logging
import threading
import time

from .models import TelegramChatCleanupTask
from .telegram_service import delete_telegram_messages_from_chat


def _due_tasks():
    return TelegramChatCleanupTask.objects.filter(
        processed_at__isnull=True,
        due_at__lte=timezone.now(),
        failed_attempts__lt=5,
    ).order_by('due_at')


def process_due_cleanup_tasks(limit=20):
    task_ids = list(_due_tasks().values_list('pk', flat=True)[:limit])

    processed = 0
    failed = 0

    for task_id in task_ids:
        with transaction.atomic():
            task = TelegramChatCleanupTask.objects.select_for_update().get(pk=task_id)
            if task.processed_at or task.due_at > timezone.now() or task.failed_attempts >= 5:
                continue
            try:
                delete_telegram_messages_from_chat(task.chat_id, task.message_ids)
                task.processed_at = timezone.now()
                task.save(update_fields=['processed_at'])
                processed += 1
            except Exception:
                task.failed_attempts += 1
                task.due_at = timezone.now() + timedelta(seconds=30 * 2 ** task.failed_attempts)
                task.save(update_fields=['failed_attempts', 'due_at'])
                logging.getLogger(__name__).exception('Telegram cleanup failed for task %s, attempt %s', task.pk, task.failed_attempts)
                failed += 1

    return processed, failed


_worker_lock = threading.Lock()
_last_started = 0.0


def request_cleanup():
    """Best-effort free-hosting cleanup; durable tasks survive process restarts."""
    global _last_started
    if time.monotonic() - _last_started < 30 or not _worker_lock.acquire(blocking=False):
        return
    _last_started = time.monotonic()

    def run():
        try:
            close_old_connections()
            process_due_cleanup_tasks(limit=2)
        except Exception:
            logging.getLogger(__name__).exception('Background Telegram cleanup failed')
        finally:
            close_old_connections()
            _worker_lock.release()

    try:
        threading.Thread(target=run, name='telegram-cleanup', daemon=True).start()
    except Exception:
        _worker_lock.release()
        logging.getLogger(__name__).exception('Could not start Telegram cleanup')
