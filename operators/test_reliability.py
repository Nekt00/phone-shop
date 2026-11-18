from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, TransactionTestCase, override_settings, skipUnlessDBFeature
from django.urls import reverse
from django.utils import timezone
from django.db.models.query import QuerySet

from .import_service import import_saved_file, import_text
from .models import (
    PriceList, Product, Supplier, TelegramIncomingUpdate, TelegramCustomer,
    TelegramCustomerMessage, TelegramCustomerRequest, TelegramChatCleanupTask,
    TelegramPublication,
)
from .telegram_bot import handle_telegram_update
from .telegram_cleanup import process_due_cleanup_tasks


class ImportReliabilityTests(TestCase):
    def setUp(self):
        self.supplier = Supplier.objects.create(name='Supplier')
        self.user = get_user_model().objects.create_user(username='operator')
        self.client.force_login(self.user)
        self.rows = [dict(category='phone', brand='iPhone', model='17', memory='256', price=Decimal('80000'))]

    @patch('operators.parser.SmartParser')
    def test_file_is_imported_only_once(self, parser):
        parser.return_value.parse.return_value = self.rows
        pricelist = PriceList.objects.create(supplier=self.supplier, file='demo.txt')
        self.assertEqual(import_saved_file(pricelist.pk), 1)
        self.assertEqual(import_saved_file(pricelist.pk), 0)
        self.assertEqual(Product.objects.filter(pricelist=pricelist).count(), 1)
        self.assertEqual(parser.call_count, 1)
        self.assertEqual(self.client.get(reverse('operators:process_pricelist', args=[pricelist.pk])).status_code, 405)

    @patch('operators.parser.SmartParser')
    def test_invalid_row_rolls_back_entire_import(self, parser):
        parser.return_value.parse.return_value = [*self.rows, dict(model='bad', price=-1)]
        pricelist = PriceList.objects.create(supplier=self.supplier, file='demo.txt')
        with self.assertRaises(ValueError):
            import_saved_file(pricelist.pk)
        pricelist.refresh_from_db()
        self.assertEqual(pricelist.status, 'new')
        self.assertFalse(Product.objects.exists())

    @patch('operators.import_service.Aggregator.aggregate_all', return_value={'errors': 1})
    def test_aggregation_error_rolls_back_text_and_products(self, aggregate):
        with self.assertRaises(ValueError):
            import_text(self.supplier, 'iPhone 17 256 blue 80000', self.user)
        self.assertFalse(PriceList.objects.exists())
        self.assertFalse(Product.objects.exists())


@override_settings(TELEGRAM_WEBHOOK_SECRET='test-secret')
class TelegramReliabilityTests(TestCase):
    def setUp(self):
        self.send = patch('operators.telegram_bot.send_telegram_message_to_chat', return_value={'message_id': 700}).start()
        self.addCleanup(patch.stopall)
        patch('operators.views.request_cleanup').start()

    def update(self, update_id, text):
        return {'update_id': update_id, 'message': {'message_id': update_id, 'chat': {'id': 12345, 'type': 'private'},
                                                   'from': {'id': 12345, 'first_name': 'Client'}, 'text': text}}

    def test_completed_update_is_persistently_deduplicated(self):
        payload = self.update(1, '/start price')
        self.assertTrue(handle_telegram_update(payload))
        self.assertFalse(handle_telegram_update(payload))
        self.send.assert_called_once()
        self.assertIsNotNone(TelegramIncomingUpdate.objects.get(pk=1).processed_at)

    def test_failed_update_can_retry_without_duplicate_request(self):
        handle_telegram_update(self.update(1, '/start price'))
        self.send.side_effect = ValueError('Telegram temporarily unavailable')
        with self.assertRaises(ValueError):
            handle_telegram_update(self.update(2, 'iPhone 17'))
        self.assertFalse(TelegramIncomingUpdate.objects.filter(pk=2).exists())
        self.assertFalse(TelegramCustomerRequest.objects.exists())
        self.send.side_effect = None
        handle_telegram_update(self.update(2, 'iPhone 17'))
        self.assertEqual(TelegramCustomerRequest.objects.count(), 1)
        self.assertEqual(TelegramCustomerMessage.objects.count(), 1)

    def test_followups_keep_one_request_and_send_no_chat_noise(self):
        handle_telegram_update(self.update(1, '/start price'))
        handle_telegram_update(self.update(2, 'iPhone 17'))
        self.send.reset_mock()
        handle_telegram_update(self.update(3, 'Синий есть?'))
        handle_telegram_update(self.update(3, 'Синий есть?'))
        self.assertEqual(TelegramCustomerRequest.objects.count(), 1)
        self.assertEqual(TelegramCustomerMessage.objects.count(), 2)
        self.assertEqual(TelegramCustomerRequest.objects.get().request_type, 'price')
        self.send.assert_not_called()

    @patch('operators.telegram_bot.answer_telegram_callback')
    @patch('operators.telegram_bot.send_telegram_text_to_chat')
    def test_old_price_button_returns_actual_publication(self, send_text, answer):
        TelegramPublication.objects.create(kind='price', text='Current price')
        payload = {'update_id': 4, 'callback_query': {'id': 'callback', 'data': 'minimal:price',
                   'from': {'id': 12345}, 'message': {'chat': {'id': 12345, 'type': 'private'}}}}
        handle_telegram_update(payload)
        self.assertEqual(send_text.call_args.args, (12345, 'Current price'))
        self.assertTrue(callable(send_text.call_args.kwargs['on_sent']))

    def test_client_close_schedules_cleanup_and_notification_event(self):
        handle_telegram_update(self.update(1, '/start price'))
        handle_telegram_update(self.update(2, 'iPhone 17'))
        handle_telegram_update(self.update(3, '/close'))
        request = TelegramCustomerRequest.objects.get()
        self.assertEqual(request.status, 'done')
        self.assertTrue(request.messages.filter(text='[system] customer_closed_request').exists())
        self.assertIn(3, TelegramChatCleanupTask.objects.get().message_ids)

    def test_bad_webhook_payload_is_rejected(self):
        response = self.client.post(reverse('operators:telegram_bot_webhook_plain'), data='[]',
                                    content_type='application/json', HTTP_X_TELEGRAM_BOT_API_SECRET_TOKEN='test-secret')
        self.assertEqual(response.status_code, 400)

    def test_read_snapshot_does_not_clear_later_message(self):
        from .views import _prepare_telegram_requests_data
        from .telegram_bot import add_message_to_request
        handle_telegram_update(self.update(1, '/start price'))
        handle_telegram_update(self.update(2, 'iPhone 17'))
        original_update = QuerySet.update
        injected = False

        def racing_update(queryset, **kwargs):
            nonlocal injected
            if queryset.model is TelegramCustomerRequest and kwargs.get('has_unread_customer_message') is False and not injected:
                injected = True
                request = TelegramCustomerRequest.objects.get()
                add_message_to_request(request, 'Новое сообщение', TelegramCustomerMessage.SENDER_CUSTOMER)
            return original_update(queryset, **kwargs)

        with patch.object(QuerySet, 'update', racing_update):
            _prepare_telegram_requests_data(mark_read=True)
        self.assertTrue(injected)
        request = TelegramCustomerRequest.objects.get()
        self.assertTrue(request.has_unread_customer_message)
        self.assertEqual(request.unread_messages_count, 2)

    @patch('operators.telegram_service._telegram_request', side_effect=ValueError('Bad Request: query is too old and response timeout expired'))
    def test_expired_callback_does_not_block_business_action(self, api):
        handle_telegram_update({'update_id': 1, 'callback_query': {'id': 'expired', 'data': 'minimal:price_request',
                              'from': {'id': 12345}, 'message': {'chat': {'id': 12345, 'type': 'private'}}}})
        self.assertEqual(TelegramCustomer.objects.get().pending_request_type, 'price')
        self.assertIsNotNone(TelegramIncomingUpdate.objects.get(pk=1).processed_at)


class PollingReliabilityTests(TestCase):
    @patch('operators.management.commands.run_telegram_polling.time.sleep')
    @patch('operators.management.commands.run_telegram_polling.process_due_cleanup_tasks')
    @patch('operators.management.commands.run_telegram_polling.handle_telegram_update')
    @patch('operators.management.commands.run_telegram_polling.get_telegram_updates')
    def test_failed_update_is_not_acknowledged_by_next_poll(self, updates, handle, cleanup, sleep):
        from django.core.management import call_command
        updates.side_effect = [[{'update_id': 1}, {'update_id': 2}], [{'update_id': 2}], KeyboardInterrupt]
        handle.side_effect = [True, ValueError('failure'), True]
        call_command('run_telegram_polling', skip_delete_webhook=True)
        self.assertIsNone(updates.call_args_list[0].kwargs['offset'])
        self.assertEqual(updates.call_args_list[1].kwargs['offset'], 2)
        self.assertEqual(updates.call_args_list[2].kwargs['offset'], 3)


class CleanupReliabilityTests(TestCase):
    @patch('operators.telegram_cleanup.delete_telegram_messages_from_chat')
    def test_failed_task_backs_off_and_other_task_can_finish(self, delete):
        customer = TelegramCustomer.objects.create(telegram_id=12345)
        request = TelegramCustomerRequest.objects.create(customer=customer, request_type='question')
        due = timezone.now() - timedelta(minutes=1)
        first = TelegramChatCleanupTask.objects.create(customer=customer, request=request, chat_id=12345, message_ids=[1], due_at=due)
        second = TelegramChatCleanupTask.objects.create(customer=customer, request=request, chat_id=12345, message_ids=[2], due_at=due)
        delete.side_effect = [ValueError('network failure'), 1]
        with self.assertLogs('operators.telegram_cleanup', level='ERROR'):
            self.assertEqual(process_due_cleanup_tasks(limit=1), (0, 1))
        self.assertEqual(process_due_cleanup_tasks(limit=1), (1, 0))
        first.refresh_from_db()
        second.refresh_from_db()
        self.assertGreater(first.due_at, timezone.now())
        self.assertEqual(first.failed_attempts, 1)
        self.assertIsNotNone(second.processed_at)


@skipUnlessDBFeature('has_select_for_update')
class PostgreSQLConcurrencyTests(TransactionTestCase):
    def test_catalog_rebuild_waits_for_shared_lock(self):
        from concurrent.futures import ThreadPoolExecutor, TimeoutError
        from threading import Event
        from django.db import transaction, close_old_connections
        from .aggregator import Aggregator
        from .models import CatalogRebuildMutex

        CatalogRebuildMutex.objects.get_or_create(pk=1)
        started = Event()

        def rebuild():
            close_old_connections()
            try:
                started.set()
                return Aggregator.aggregate_all()
            finally:
                close_old_connections()

        with ThreadPoolExecutor(max_workers=1) as executor:
            with transaction.atomic():
                CatalogRebuildMutex.objects.select_for_update().get(pk=1)
                future = executor.submit(rebuild)
                self.assertTrue(started.wait(5))
                with self.assertRaises(TimeoutError):
                    future.result(timeout=0.2)
            self.assertEqual(future.result(timeout=10)['errors'], 0)

    @patch('operators.telegram_bot.delete_customer_message')
    @patch('operators.telegram_bot.send_telegram_message_to_chat', return_value={'message_id': 700})
    def test_same_update_from_two_workers_is_processed_once(self, send, delete):
        from concurrent.futures import ThreadPoolExecutor
        from threading import Barrier
        from django.db import close_old_connections

        barrier = Barrier(2)
        payload = {'update_id': 42, 'message': {'message_id': 1, 'text': '/start',
                   'chat': {'id': 12345, 'type': 'private'}, 'from': {'id': 12345}}}

        def process():
            close_old_connections()
            try:
                barrier.wait(timeout=5)
                return handle_telegram_update(payload)
            finally:
                close_old_connections()

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda _: process(), range(2)))
        self.assertCountEqual(results, [True, False])
        send.assert_called_once()
