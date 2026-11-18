import logging

from django.db import transaction

from .models import TelegramPublication, TelegramPublicationMutex
from .telegram_service import delete_telegram_messages, send_telegram_messages


logger = logging.getLogger(__name__)


def publish_pricelist(text, user):
    """Return the new publication; requires PostgreSQL and no enclosing transaction."""
    from .telegram_bot import get_channel_entry_keyboard

    send_error = None
    # Reject enclosing transactions so partial IDs commit before send errors escape.
    with transaction.atomic(durable=True):
        TelegramPublicationMutex.objects.select_for_update().get(pk=1)
        publication = TelegramPublication.objects.create(
            kind=TelegramPublication.KIND_PRICE,
            text=text,
            sent_by=user,
            is_active=False,
        )

        def on_sent(message):
            publication.message_ids.append(message['message_id'])
            publication.save(update_fields=['message_ids'])

        try:
            send_telegram_messages(
                text, reply_markup=get_channel_entry_keyboard(), on_sent=on_sent,
            )
        except Exception as exc:
            send_error = exc
        else:
            publication.is_active = True
            publication.save(update_fields=['is_active'])
            previous = TelegramPublication.objects.filter(
                kind=TelegramPublication.KIND_PRICE,
            ).exclude(pk=publication.pk)
            previous.update(is_active=False)
            for old in previous.exclude(message_ids=[]):
                try:
                    delete_telegram_messages(old.message_ids)
                except Exception:
                    logger.exception('Could not remove previous Telegram publication %s', old.pk)
                else:
                    old.message_ids = []
                    old.save(update_fields=['message_ids'])

    if send_error is not None:
        raise send_error
    return publication
