from datetime import timedelta
import logging

from django.conf import settings
from django.utils import timezone
from django.db import transaction

from .models import (
    AggregatedProduct,
    TelegramChatCleanupTask,
    TelegramCustomer,
    TelegramCustomerMessage,
    TelegramCustomerRequest,
    TelegramPublication,
    TelegramIncomingUpdate,
)
from .telegram_service import (
    answer_telegram_callback,
    delete_telegram_messages_from_chat,
    send_operator_notification,
    send_telegram_message_to_chat,
    send_telegram_text_to_chat,
)


REQUEST_TYPE_LABELS = {
    TelegramCustomerRequest.TYPE_PRICE: 'уточнение цены',
    TelegramCustomerRequest.TYPE_AVAILABILITY: 'уточнение наличия',
    TelegramCustomerRequest.TYPE_ORDER: 'оформление заказа',
    TelegramCustomerRequest.TYPE_QUESTION: 'вопрос оператору',
}


CLOSE_REQUEST_TEXT = '✅ Заявка закрыта. Сообщения будут удалены при ближайшей очистке.'
CUSTOMER_CLOSED_REQUEST_NOTE = '[system] customer_closed_request'


def get_channel_entry_keyboard():
    bot_username = getattr(settings, 'TELEGRAM_BOT_USERNAME', '').strip().lstrip('@')
    if not bot_username:
        return None

    return {
        'inline_keyboard': [
            [
                {'text': '💬 Уточнить цену и наличие', 'url': f'https://t.me/{bot_username}?start=price'},
            ],
            [
                {'text': '🛒 Оформить заказ', 'url': f'https://t.me/{bot_username}?start=order'},
                {'text': '👨‍💼 Связаться с оператором', 'url': f'https://t.me/{bot_username}?start=question'},
            ],
        ]
    }


def get_bot_main_menu():
    return {
        'inline_keyboard': [
            [
                {'text': '📋 Смотреть прайс', 'callback_data': 'menu:price'},
            ],
            [
                {'text': '📦 Уточнить наличие', 'callback_data': 'request:availability'},
                {'text': '💵 Уточнить цену', 'callback_data': 'request:price'},
            ],
            [
                {'text': '🛒 Оформить заказ', 'callback_data': 'request:order'},
                {'text': '👨‍💼 Связаться с оператором', 'callback_data': 'request:question'},
            ],
            [
                {'text': '✅ Закрыть текущую заявку', 'callback_data': 'menu:close_request'},
            ],
        ]
    }


def get_welcome_text():
    return (
        '👋 Здравствуйте! Я помогу получить актуальный прайс, уточнить цену и наличие '
        'или передать сообщение оператору.\n\nВыберите действие:'
    )


def add_pending_chat_message_id(customer, message_id):
    if not message_id:
        return

    active = customer.requests.exclude(status__in=['done', 'cancelled']).order_by('created_at', 'id').first()
    if active:
        active.extra_message_ids = list(dict.fromkeys([*(active.extra_message_ids or []), message_id]))
        active.save(update_fields=['extra_message_ids'])
        return

    pending_ids = list(customer.pending_chat_message_ids or [])
    if message_id in pending_ids:
        return

    pending_ids.append(message_id)
    customer.pending_chat_message_ids = pending_ids
    customer.save(update_fields=['pending_chat_message_ids', 'updated_at', 'last_seen_at'])


def delete_customer_message(chat_id, message_id):
    if not chat_id or not message_id:
        return

    try:
        delete_telegram_messages_from_chat(chat_id, [message_id])
    except Exception:
        # We never want a failed cleanup attempt to break the customer flow.
        pass


def get_or_create_telegram_customer(user_data):
    defaults = {
        'username': user_data.get('username', '') or '',
        'first_name': user_data.get('first_name', '') or '',
        'last_name': user_data.get('last_name', '') or '',
        'language_code': user_data.get('language_code', '') or '',
    }
    customer, created = TelegramCustomer.objects.get_or_create(
        telegram_id=user_data['id'],
        defaults=defaults,
    )
    if not created:
        changed = False
        for field, value in defaults.items():
            if getattr(customer, field) != value:
                setattr(customer, field, value)
                changed = True
        if changed:
            customer.save(update_fields=['username', 'first_name', 'last_name', 'language_code', 'updated_at', 'last_seen_at'])
        else:
            customer.save(update_fields=['updated_at', 'last_seen_at'])
    return customer


def match_product_by_query(query):
    query = (query or '').strip()
    if not query:
        return None

    products = AggregatedProduct.objects.filter(model__icontains=query).order_by('brand', 'model')[:2]
    if len(products) == 1:
        return products[0]

    products = AggregatedProduct.objects.filter(
        brand__icontains=query
    ).order_by('brand', 'model')[:2]
    if len(products) == 1:
        return products[0]

    return None


def start_request_flow(customer, request_type):
    customer.state = TelegramCustomer.STATE_AWAITING_REQUEST_TEXT
    customer.pending_request_type = request_type
    customer.save(update_fields=['state', 'pending_request_type', 'updated_at', 'last_seen_at'])



def send_welcome(chat_id, customer=None):
    sent_message = send_telegram_message_to_chat(chat_id, get_welcome_text(), reply_markup=get_bot_main_menu())

    if customer and sent_message:
        add_pending_chat_message_id(customer, sent_message.get('message_id'))

    return sent_message


def send_latest_price_to_customer(customer):
    latest_publication = TelegramPublication.objects.filter(
        kind=TelegramPublication.KIND_PRICE,
        is_active=True,
    ).first()

    if not latest_publication or not latest_publication.text:
        send_telegram_message_to_chat(customer.telegram_id, '📭 Актуальный прайс пока не опубликован.')
        return

    send_telegram_text_to_chat(
        customer.telegram_id, latest_publication.text,
        on_sent=lambda message: add_pending_chat_message_id(customer, message['message_id']),
    )


def notify_operator_about_request(request_obj):
    operator_chat_id = getattr(settings, 'TELEGRAM_OPERATOR_CHAT_ID', '').strip()
    if not operator_chat_id or operator_chat_id == getattr(settings, 'TELEGRAM_CHAT_ID', '').strip():
        return

    customer = request_obj.customer
    last_message = request_obj.messages.order_by('-created_at').first()
    product_text = request_obj.product_query or 'Без уточнения модели'
    customer_name = customer.first_name or customer.username or str(customer.telegram_id)

    lines = [
        '🔔 Новое обращение из Telegram',
        f'Тип: {request_obj.get_request_type_display()}',
        f'Клиент: {customer_name}',
        f'Telegram ID: {customer.telegram_id}',
        f'Запрос: {product_text}',
    ]

    if request_obj.product:
        lines.append(f'Товар: {request_obj.product}')

    if last_message and last_message.text:
        lines.append(f'Сообщение: {last_message.text}')

    send_operator_notification('\n'.join(lines))


def notify_operator_about_request_closed(request_obj):
    operator_chat_id = getattr(settings, 'TELEGRAM_OPERATOR_CHAT_ID', '').strip()
    if not operator_chat_id or operator_chat_id == getattr(settings, 'TELEGRAM_CHAT_ID', '').strip():
        return

    customer = request_obj.customer
    customer_name = customer.first_name or customer.username or str(customer.telegram_id)
    product_text = request_obj.product_query or 'Без уточнения модели'

    lines = [
        '🔕 Клиент закрыл заявку',
        f'Заявка: #{request_obj.id}',
        f'Тип: {request_obj.get_request_type_display()}',
        f'Клиент: {customer_name}',
        f'Telegram ID: {customer.telegram_id}',
        f'Запрос: {product_text}',
    ]

    if request_obj.product:
        lines.append(f'Товар: {request_obj.product}')

    send_operator_notification('\n'.join(lines))


def schedule_request_chat_cleanup(request_obj, extra_message_ids=None, delay_seconds=60):
    message_ids = list(
        request_obj.messages.exclude(telegram_message_id__isnull=True)
        .values_list('telegram_message_id', flat=True)
    )
    message_ids.extend(request_obj.extra_message_ids or [])
    if extra_message_ids:
        message_ids.extend([message_id for message_id in extra_message_ids if message_id])

    unique_message_ids = []
    seen = set()
    for message_id in message_ids:
        if message_id in seen:
            continue
        seen.add(message_id)
        unique_message_ids.append(message_id)

    if not unique_message_ids:
        return None

    return TelegramChatCleanupTask.objects.create(
        request=request_obj,
        customer=request_obj.customer,
        chat_id=request_obj.customer.telegram_id,
        message_ids=unique_message_ids,
        due_at=timezone.now() + timedelta(seconds=delay_seconds),
    )


def _create_backfilled_message(request_obj, sender_type, text, created_at, operator=None):
    message = TelegramCustomerMessage.objects.create(
        request=request_obj,
        sender_type=sender_type,
        text=text,
        operator=operator,
    )
    TelegramCustomerMessage.objects.filter(id=message.id).update(created_at=created_at)
    return TelegramCustomerMessage.objects.get(id=message.id)


def ensure_request_history(request_obj):
    if request_obj.messages.exists():
        return

    if request_obj.client_message:
        _create_backfilled_message(
            request_obj=request_obj,
            sender_type=TelegramCustomerMessage.SENDER_CUSTOMER,
            text=request_obj.client_message,
            created_at=request_obj.created_at,
        )

    if request_obj.operator_reply:
        _create_backfilled_message(
            request_obj=request_obj,
            sender_type=TelegramCustomerMessage.SENDER_OPERATOR,
            text=request_obj.operator_reply,
            created_at=request_obj.replied_at or request_obj.updated_at,
            operator=request_obj.assigned_operator,
        )


def merge_customer_requests(primary_request, duplicate_requests):
    changed_fields = set()

    ensure_request_history(primary_request)

    for duplicate in duplicate_requests:
        ensure_request_history(duplicate)

        TelegramCustomerMessage.objects.filter(request=duplicate).update(request=primary_request)

        if not primary_request.product and duplicate.product:
            primary_request.product = duplicate.product
            changed_fields.add('product')
        if not primary_request.product_query and duplicate.product_query:
            primary_request.product_query = duplicate.product_query
            changed_fields.add('product_query')
        if not primary_request.assigned_operator and duplicate.assigned_operator:
            primary_request.assigned_operator = duplicate.assigned_operator
            changed_fields.add('assigned_operator')
        if not primary_request.operator_note and duplicate.operator_note:
            primary_request.operator_note = duplicate.operator_note
            changed_fields.add('operator_note')
        if duplicate.operator_reply:
            primary_request.operator_reply = duplicate.operator_reply
            changed_fields.add('operator_reply')
        if duplicate.replied_at and (not primary_request.replied_at or duplicate.replied_at > primary_request.replied_at):
            primary_request.replied_at = duplicate.replied_at
            changed_fields.add('replied_at')
        if duplicate.updated_at > primary_request.updated_at:
            primary_request.client_message = duplicate.client_message or primary_request.client_message
            changed_fields.add('client_message')

        duplicate.status = TelegramCustomerRequest.STATUS_CANCELLED
        duplicate.operator_note = (
            (duplicate.operator_note + '\n') if duplicate.operator_note else ''
        ) + f'Объединено в заявку #{primary_request.id}'
        duplicate.save(update_fields=['status', 'operator_note', 'updated_at'])

    if changed_fields:
        changed_fields.add('updated_at')
        primary_request.save(update_fields=sorted(changed_fields))

    return primary_request


def get_active_customer_request(customer):
    active_requests = list(customer.requests.exclude(
        status__in=[TelegramCustomerRequest.STATUS_DONE, TelegramCustomerRequest.STATUS_CANCELLED]
    ).order_by('created_at', 'id'))

    if not active_requests:
        return None

    primary_request = active_requests[0]
    duplicate_requests = active_requests[1:]

    if duplicate_requests:
        primary_request = merge_customer_requests(primary_request, duplicate_requests)
        primary_request.refresh_from_db()
    else:
        ensure_request_history(primary_request)

    return primary_request


@transaction.atomic
def close_request_and_notify_customer(request_obj, text=CLOSE_REQUEST_TEXT):
    request_obj = TelegramCustomerRequest.objects.select_for_update().get(pk=request_obj.pk)
    if request_obj.status in [TelegramCustomerRequest.STATUS_DONE, TelegramCustomerRequest.STATUS_CANCELLED]:
        return None
    if request_obj.status != TelegramCustomerRequest.STATUS_DONE:
        request_obj.status = TelegramCustomerRequest.STATUS_DONE
        request_obj.has_unread_customer_message = False
        request_obj.unread_messages_count = 0
        request_obj.save(update_fields=['status', 'has_unread_customer_message', 'unread_messages_count', 'updated_at'])

    sent_message = send_telegram_message_to_chat(request_obj.customer.telegram_id, text)
    schedule_request_chat_cleanup(request_obj, extra_message_ids=[sent_message.get('message_id')])
    TelegramCustomer.objects.filter(pk=request_obj.customer_id).update(state='', pending_request_type='')
    return sent_message


def close_request_by_customer(request_obj, text):
    add_message_to_request(
        request_obj,
        text=CUSTOMER_CLOSED_REQUEST_NOTE,
        sender_type=TelegramCustomerMessage.SENDER_OPERATOR,
        update_request_state=False,
    )
    transaction.on_commit(lambda: _notify_operator_safely(request_obj, closed=True))
    return close_request_and_notify_customer(request_obj, text=text)


def send_request_message(request_obj, text, reply_markup=None, operator=None):
    sent_message = send_telegram_message_to_chat(
        request_obj.customer.telegram_id,
        text,
        reply_markup=reply_markup,
    )
    add_message_to_request(
        request_obj,
        text=text,
        sender_type=TelegramCustomerMessage.SENDER_OPERATOR,
        telegram_message_id=sent_message.get('message_id'),
        operator=operator,
        update_request_state=bool(operator),
    )
    return sent_message


def add_message_to_request(request_obj, text, sender_type, telegram_message_id=None, operator=None, update_request_state=True):
    cleaned_text = (text or '').strip()
    if not cleaned_text:
        return None

    message = TelegramCustomerMessage.objects.create(
        request=request_obj,
        sender_type=sender_type,
        text=cleaned_text,
        operator=operator,
        telegram_message_id=telegram_message_id,
    )

    if not update_request_state:
        return message

    if sender_type == TelegramCustomerMessage.SENDER_CUSTOMER:
        request_obj.client_message = cleaned_text
        request_obj.has_unread_customer_message = True
        request_obj.unread_messages_count = (request_obj.unread_messages_count or 0) + 1
    else:
        request_obj.operator_reply = cleaned_text
        request_obj.has_unread_customer_message = False
        request_obj.unread_messages_count = 0

    request_obj.save(update_fields=['client_message', 'operator_reply', 'has_unread_customer_message', 'unread_messages_count', 'updated_at'])
    return message


def create_or_append_customer_request(customer, text, telegram_message_id=None):
    request_type = customer.pending_request_type or TelegramCustomerRequest.TYPE_QUESTION
    matched_product = match_product_by_query(text)
    request_obj = get_active_customer_request(customer)
    is_new_request = request_obj is None

    if request_obj is None:
        publication = TelegramPublication.objects.filter(
            kind=TelegramPublication.KIND_PRICE,
            is_active=True,
        ).first()
        pending_message_ids = list(customer.pending_chat_message_ids or [])

        request_obj = TelegramCustomerRequest.objects.create(
            customer=customer,
            request_type=request_type,
            publication=publication,
            product=matched_product,
            product_query=(text or '').strip(),
            client_message=(text or '').strip(),
            telegram_message_id=telegram_message_id,
            status=TelegramCustomerRequest.STATUS_NEW,
            has_unread_customer_message=True,
            unread_messages_count=0,
            extra_message_ids=pending_message_ids,
        )
        if pending_message_ids:
            customer.pending_chat_message_ids = []
            customer.save(update_fields=['pending_chat_message_ids', 'updated_at', 'last_seen_at'])
    else:
        if request_obj.request_type == TelegramCustomerRequest.TYPE_QUESTION and request_type:
            request_obj.request_type = request_type
        if matched_product and not request_obj.product:
            request_obj.product = matched_product
        if text and not request_obj.product_query:
            request_obj.product_query = (text or '').strip()
        request_obj.save(update_fields=['request_type', 'product', 'product_query', 'updated_at'])

    add_message_to_request(
        request_obj,
        text=text,
        sender_type=TelegramCustomerMessage.SENDER_CUSTOMER,
        telegram_message_id=telegram_message_id,
    )

    customer.state = TelegramCustomer.STATE_IDLE
    customer.pending_request_type = ''
    customer.save(update_fields=['state', 'pending_request_type', 'updated_at', 'last_seen_at'])

    if is_new_request:
        transaction.on_commit(lambda: _notify_operator_safely(request_obj))
    return request_obj


def handle_callback_query(callback_query):
    data = callback_query.get('data', '')
    data = {
        'minimal:price': 'menu:price', 'minimal:close': 'menu:close_request',
        'minimal:menu': 'menu:main', 'minimal:price_request': 'request:price',
        'minimal:availability': 'request:availability', 'minimal:order': 'request:order',
        'minimal:operator': 'request:question',
    }.get(data, data)
    from_user = callback_query.get('from', {})
    customer = get_or_create_telegram_customer(from_user)

    if data == 'menu:price':
        answer_telegram_callback(callback_query['id'])
        send_latest_price_to_customer(customer)
        return

    if data == 'menu:close_request':
        closed_request = get_active_customer_request(customer)
        answer_telegram_callback(callback_query['id'])
        if closed_request:
            close_request_by_customer(
                closed_request,
                text=f'✅ Заявка #{closed_request.id} закрыта. Сообщения будут удалены при ближайшей очистке.',
            )
        else:
            send_telegram_message_to_chat(
                customer.telegram_id,
                'ℹ️ У вас нет открытых заявок.',
            )
        return

    if data.startswith('request:'):
        request_type = data.split(':', 1)[1]
        if request_type not in REQUEST_TYPE_LABELS:
            answer_telegram_callback(callback_query['id'])
            return
        answer_telegram_callback(callback_query['id'], text='Напишите сообщение для оператора.')
        start_request_flow(customer, request_type)
        return

    if data == 'menu:main':
        answer_telegram_callback(callback_query['id'])
        send_welcome(customer.telegram_id, customer)
        return

    answer_telegram_callback(callback_query['id'])


def handle_message(message):
    from_user = message.get('from', {})
    chat_id = message.get('chat', {}).get('id') or from_user.get('id')
    text = (message.get('text') or '').strip()

    if not chat_id or not from_user.get('id') or from_user.get('is_bot') or message.get('chat', {}).get('type') != 'private':
        return
    customer = get_or_create_telegram_customer(from_user)
    command = text.split(maxsplit=1)[0].split('@')[0] if text else ''
    if command in {'/start', '/menu'}:
        parameter = text.split(maxsplit=1)[1] if ' ' in text else ''
        request_type = {'price': 'price', 'order': 'order', 'question': 'question'}.get(parameter)
        if request_type:
            start_request_flow(customer, request_type)
        send_welcome(chat_id, customer)
        add_pending_chat_message_id(customer, message.get('message_id'))
        transaction.on_commit(lambda: delete_customer_message(chat_id, message.get('message_id')))
        return
    active = get_active_customer_request(customer)
    if command == '/close':
        if active:
            if message.get('message_id'):
                active.extra_message_ids = [*(active.extra_message_ids or []), message['message_id']]
                active.save(update_fields=['extra_message_ids'])
            close_request_by_customer(active, CLOSE_REQUEST_TEXT)
        else:
            send_telegram_message_to_chat(chat_id, 'Сейчас нет открытой заявки.')
        return
    if not text:
        send_telegram_message_to_chat(chat_id, 'Пока поддерживаются текстовые сообщения. Опишите вопрос текстом.')
        return
    if active or customer.state == TelegramCustomer.STATE_AWAITING_REQUEST_TEXT:
        created = active is None
        request_obj = create_or_append_customer_request(customer, text, message.get('message_id'))
        if created:
            sent = send_telegram_message_to_chat(chat_id, f'Заявка #{request_obj.pk} передана оператору. Ответ придет в этот чат.')
            request_obj.extra_message_ids = [*(request_obj.extra_message_ids or []), sent['message_id']]
            request_obj.save(update_fields=['extra_message_ids'])
        return
    send_welcome(chat_id, customer)


def handle_telegram_update(update):
    update_id = update.get('update_id')
    if not isinstance(update_id, int):
        raise ValueError('Telegram update_id must be an integer')
    with transaction.atomic():
        record, _ = TelegramIncomingUpdate.objects.get_or_create(update_id=update_id)
        record = TelegramIncomingUpdate.objects.select_for_update().get(pk=record.pk)
        if record.processed_at:
            return False
        callback = update.get('callback_query')
        message = update.get('message')
        user = (callback or message or {}).get('from') or {}
        chat = ((callback or {}).get('message') or message or {}).get('chat') or {}
        if user.get('id') and not user.get('is_bot') and chat.get('type') == 'private':
            customer = get_or_create_telegram_customer(user)
            # Serialize different updates from the same customer, not only identical update IDs.
            TelegramCustomer.objects.select_for_update().get(pk=customer.pk)
            if callback:
                handle_callback_query(callback)
            elif message:
                handle_message(message)
        record.processed_at = timezone.now()
        record.save(update_fields=['processed_at'])
    return True


def _notify_operator_safely(request_obj, closed=False):
    try:
        if closed:
            notify_operator_about_request_closed(request_obj)
        else:
            notify_operator_about_request(request_obj)
    except Exception:
        logging.getLogger(__name__).exception('Operator notification failed for request %s', request_obj.pk)
