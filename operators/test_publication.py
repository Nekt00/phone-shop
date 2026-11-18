from concurrent.futures import ThreadPoolExecutor
from threading import Event
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.db import close_old_connections, transaction
from django.test import SimpleTestCase, TransactionTestCase, override_settings, skipUnlessDBFeature

from .models import TelegramPublication, TelegramPublicationMutex
from .telegram_service import send_telegram_messages, send_telegram_text_to_chat


@override_settings(TELEGRAM_CHAT_ID='channel')
class TelegramSentCallbackTests(SimpleTestCase):
    @patch('operators.telegram_service._telegram_request')
    def test_callback_runs_before_next_part_and_survives_send_failure(self, request):
        sent = []

        def respond(method, payload):
            if sent:
                raise ValueError('send failed')
            return {'message_id': 10}

        request.side_effect = respond
        with self.assertRaisesMessage(ValueError, 'send failed'):
            send_telegram_messages('x' * 4001, on_sent=sent.append)
        self.assertEqual(sent, [{'message_id': 10, 'text': 'x' * 4000}])

    @patch('operators.telegram_service._telegram_request', return_value={'message_id': 10})
    def test_existing_api_and_markup_position_are_preserved(self, request):
        markup = {'inline_keyboard': []}
        result = send_telegram_text_to_chat('channel', 'x' * 4001, markup, 'first')
        self.assertEqual([item['message_id'] for item in result], [10, 10])
        self.assertEqual(request.call_args_list[0].args[1]['reply_markup'], markup)
        self.assertNotIn('reply_markup', request.call_args_list[1].args[1])


@override_settings(TELEGRAM_CHAT_ID='channel')
class PricelistPublicationTests(TransactionTestCase):
    def setUp(self):
        TelegramPublicationMutex.objects.get_or_create(pk=1)
        self.user = get_user_model().objects.create_user(username='publisher')
        self.old = TelegramPublication.objects.create(kind='price', text='Old', message_ids=[1])
        keyboard = patch('operators.telegram_bot.get_channel_entry_keyboard', return_value=None)
        keyboard.start()
        self.addCleanup(keyboard.stop)

    @patch('operators.telegram_service._telegram_request')
    def test_missing_mutex_fails_before_sending(self, request):
        from .publication_service import publish_pricelist

        TelegramPublicationMutex.objects.filter(pk=1).delete()
        with self.assertRaises(TelegramPublicationMutex.DoesNotExist):
            publish_pricelist('New', self.user)
        self.assertEqual(TelegramPublication.objects.count(), 1)
        request.assert_not_called()

    @patch('operators.telegram_service._telegram_request')
    def test_enclosing_transaction_is_rejected_before_sending(self, request):
        from .publication_service import publish_pricelist

        with transaction.atomic():
            with self.assertRaisesMessage(RuntimeError, 'durable atomic block'):
                publish_pricelist('New', self.user)
        self.assertEqual(TelegramPublication.objects.count(), 1)
        request.assert_not_called()

    @patch('operators.publication_service.delete_telegram_messages')
    @patch('operators.telegram_service._telegram_request')
    def test_partial_failure_is_committed_and_old_price_is_untouched(self, request, delete):
        from .publication_service import publish_pricelist

        def respond(method, payload):
            partial = TelegramPublication.objects.exclude(pk=self.old.pk).get()
            self.assertFalse(partial.is_active)
            if partial.message_ids:
                self.assertEqual(partial.message_ids, [10])
                raise ValueError('second part failed')
            return {'message_id': 10}

        request.side_effect = respond
        with self.assertRaisesMessage(ValueError, 'second part failed'):
            publish_pricelist('x' * 4001, self.user)
        self.assertFalse(transaction.get_connection().in_atomic_block)
        partial = TelegramPublication.objects.exclude(pk=self.old.pk).get()
        self.assertEqual(partial.message_ids, [10])
        self.assertEqual(partial.sent_by, self.user)
        self.assertFalse(partial.is_active)
        self.old.refresh_from_db()
        self.assertTrue(self.old.is_active)
        self.assertEqual(self.old.message_ids, [1])
        delete.assert_not_called()

    @patch('operators.publication_service.delete_telegram_messages')
    @patch('operators.telegram_service._telegram_request', side_effect=ValueError('first part failed'))
    def test_first_part_failure_keeps_inactive_record(self, request, delete):
        from .publication_service import publish_pricelist

        with self.assertRaisesMessage(ValueError, 'first part failed'):
            publish_pricelist('New', self.user)
        partial = TelegramPublication.objects.exclude(pk=self.old.pk).get()
        self.assertFalse(partial.is_active)
        self.assertEqual(partial.message_ids, [])
        delete.assert_not_called()

    @patch('operators.publication_service.delete_telegram_messages', return_value=1)
    @patch('operators.telegram_service._telegram_request')
    def test_success_cleans_old_and_partial_prices_but_not_manual(self, request, delete):
        from .publication_service import publish_pricelist

        partial = TelegramPublication.objects.create(
            kind='price', text='Partial', message_ids=[2], is_active=False,
        )
        manual = TelegramPublication.objects.create(kind='manual', text='Manual', message_ids=[3])

        def respond(method, payload):
            delete.assert_not_called()
            self.old.refresh_from_db()
            self.assertTrue(self.old.is_active)
            return {'message_id': request.call_count + 10}

        request.side_effect = respond
        publication = publish_pricelist('x' * 4001, self.user)
        self.assertEqual(publication.message_ids, [11, 12])
        self.assertEqual(publication.sent_by, self.user)
        self.assertTrue(publication.is_active)
        self.assertEqual(TelegramPublication.objects.filter(kind='price', is_active=True).get(), publication)
        self.assertCountEqual([call.args[0] for call in delete.call_args_list], [[1], [2]])
        for previous in (self.old, partial):
            previous.refresh_from_db()
            self.assertFalse(previous.is_active)
            self.assertEqual(previous.message_ids, [])
        manual.refresh_from_db()
        self.assertTrue(manual.is_active)
        self.assertEqual(manual.message_ids, [3])

    @patch('operators.publication_service.delete_telegram_messages')
    @patch('operators.telegram_service._telegram_request', return_value={'message_id': 10})
    def test_cleanup_failure_is_logged_and_retried_after_next_success(self, request, delete):
        from .publication_service import publish_pricelist

        delete.side_effect = ValueError('delete failed')
        with self.assertLogs('operators.publication_service', level='ERROR'):
            publication = publish_pricelist('New', self.user)
        self.assertTrue(publication.is_active)
        self.old.refresh_from_db()
        self.assertFalse(self.old.is_active)
        self.assertEqual(self.old.message_ids, [1])

        delete.reset_mock(side_effect=True)
        publish_pricelist('Newest', self.user)
        self.assertCountEqual([call.args[0] for call in delete.call_args_list], [[1], [10]])

    @skipUnlessDBFeature('has_select_for_update')
    @patch('operators.telegram_service._telegram_request', return_value={'message_id': 10})
    def test_existing_mutex_blocks_publication_on_another_connection(self, request):
        """PostgreSQL row locks serialize workers; SQLite cannot prove this guarantee."""
        from .publication_service import publish_pricelist

        started = Event()
        sent = Event()
        request.side_effect = lambda *args: sent.set() or {'message_id': 10}

        def publish():
            close_old_connections()
            try:
                started.set()
                return publish_pricelist('New', self.user).pk
            finally:
                close_old_connections()

        with ThreadPoolExecutor(max_workers=1) as executor:
            with transaction.atomic():
                TelegramPublicationMutex.objects.select_for_update().get(pk=1)
                future = executor.submit(publish)
                self.assertTrue(started.wait(5))
                self.assertFalse(sent.wait(0.2))
                self.assertFalse(future.done())
            self.assertIsNotNone(future.result(timeout=10))
        self.assertTrue(sent.is_set())
