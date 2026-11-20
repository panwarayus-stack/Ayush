import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, PropertyMock, patch

from fastapi.testclient import TestClient

import api
import bot


class RegressionTests(unittest.IsolatedAsyncioTestCase):
    def test_api_requires_token(self):
        with patch.object(api, 'BACKEND_API_TOKEN', 'test-token'):
            client = TestClient(api.app)
            self.assertEqual(client.get('/health').status_code, 200)
            self.assertEqual(client.get('/accounts').status_code, 401)
            response = client.get('/accounts', headers={'Authorization': 'Bearer test-token'})
            self.assertEqual(response.status_code, 200)

    async def test_invalid_restore_preserves_running_clients(self):
        shutdown = AsyncMock()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'db.json'
            path.write_text('{"version":1,"accounts":[]}')
            with patch.object(api, 'DB_PATH', path), patch.object(api, '_shutdown_clients', shutdown):
                self.assertFalse((await api.storage_restore())['restore_completed'])
        shutdown.assert_not_awaited()

    async def test_voice_join_cannot_start_a_new_call(self):
        tg = AsyncMock()
        tg.resolve_chat_id.return_value = -10012345
        type(tg).calls = PropertyMock(side_effect=lambda: AsyncMock(return_value={})())
        tg.connected_to.side_effect = [False, True, True]
        with patch.object(api, '_get_tgcalls', AsyncMock(return_value=tg)):
            self.assertTrue((await api._do_join('acct', '@example', True, True))['ok'])
        self.assertFalse(tg.play.await_args.kwargs['config'].auto_start)
        self.assertIsNone(tg.play.await_args.kwargs['stream'])

    def test_chat_normalization(self):
        self.assertEqual(bot._normalize_chat_ref("https://t.me/c/1234567890/42?single"), "-1001234567890")
        self.assertEqual(bot._normalize_chat_ref('https://t.me/example?voicechat'), '@example')
        self.assertEqual(bot._normalize_chat_ref('-10012345'), '-10012345')
        self.assertEqual(bot._normalize_chat_ref('https://t.me/+invite'), '+invite')
        self.assertEqual(bot._normalize_chat_ref('https://t.me/joinchat/invite'), '+invite')
        for text in ('https://example.com/group', '12345', '@'):
            self.assertIsNone(bot._normalize_chat_ref(text))

    async def test_repeated_drops_rejoin_until_explicit_leave(self):
        from unittest.mock import Mock

        tg = AsyncMock()
        type(tg).calls = PropertyMock(side_effect=lambda: AsyncMock(return_value={})())
        chat_id = -10012345
        tg.resolve_chat_id.return_value = chat_id
        tg.connected_to.side_effect = [False, False, True, True] * 2
        client = Mock()
        client.is_connected.return_value = True
        state = {'restoring': False, 'voice_targets': {'acct': {chat_id: (0, 0.0)}}, 'tgcalls': {'acct': tg}, 'clients': {'acct': client}}
        with patch.object(api, 'app_state', state):
            await api._recover_voice_calls('acct')
            await api._recover_voice_calls('acct')
            self.assertEqual(tg.play.await_count, 2)
            await api._do_leave('acct', '@example')
            await api._recover_voice_calls('acct')
            self.assertEqual(tg.play.await_count, 2)
            self.assertEqual(state['voice_targets']['acct'], {})

    async def test_join_fails_when_play_returns_without_a_connected_transport(self):
        tg = AsyncMock()
        type(tg).calls = PropertyMock(side_effect=lambda: AsyncMock(return_value={})())
        tg.resolve_chat_id.return_value = -10012345
        tg.connected_to.return_value = False
        with patch.object(api, '_get_tgcalls', AsyncMock(return_value=tg)):
            result = await api._do_join('acct', '@example', True, True)
        self.assertFalse(result['ok'])
        tg.leave_call.assert_awaited_once_with(-10012345)

    async def test_native_disconnected_event_invalidates_transport(self):
        from types import SimpleNamespace

        client = api.TelegramClient(api.StringSession(), 1, 'offline')
        tg = api.VoiceEngine(client, 'offline')
        chat_id = -10012345
        with patch.object(api.PyTgCalls, '_handle_connection_changed', new_callable=AsyncMock):
            await tg._handle_connection_changed(chat_id, SimpleNamespace(
                kind=api.ConnectionKind.NORMAL, state=api.ConnectionState.CONNECTED,
            ))
            self.assertIn(chat_id, tg.connected_chats)
            await tg._handle_connection_changed(chat_id, SimpleNamespace(
                kind=api.ConnectionKind.NORMAL, state=api.ConnectionState.TIMEOUT,
            ))
        self.assertFalse(await tg.connected_to(chat_id))

    async def test_registered_disconnected_account_is_not_a_successful_join(self):
        account = {'account_id': 'offline', 'status': 'pending_login'}
        with patch.dict(api.app_state, {'accounts': {'offline': account}, 'clients': {}, 'tgcalls': {}}):
            self.assertFalse(api._account_public(account)['connected'])
            result = await api._do_join('offline', '@example', True, True)
        self.assertEqual(result['status_code'], 409)
        self.assertIn('login required', result['error'])

    async def test_join_conflict_is_reported_without_automatic_retries(self):
        import httpx

        response = httpx.Response(409, json={'detail': 'Telegram login required'})
        with patch.object(bot, 'api_post', AsyncMock(return_value=response)) as post:
            result = await bot._voice_op_one({'account_id': 'offline'}, '@example', 'join')
        self.assertEqual(result, ('offline', 'offline', False, 'Telegram login required'))
        post.assert_awaited_once()

    async def test_bot_http_login_2fa_backup_and_logout(self):
        import time
        from types import SimpleNamespace
        from unittest.mock import Mock

        import httpx

        session = 'offline-session-string'
        telegram = Mock()
        telegram.sign_in = AsyncMock(side_effect=[api.SessionPasswordNeededError(request=None), None])
        telegram.connect = AsyncMock()
        telegram.send_code_request = AsyncMock(return_value=SimpleNamespace(phone_code_hash='hash'))
        telegram.disconnect = AsyncMock()
        telegram.session.save.return_value = session
        telegram.is_connected.return_value = True
        state = dict(api.app_state, accounts={}, clients={}, pending_logins={}, tgcalls={}, voice_targets={})
        client_class = httpx.AsyncClient
        transport = httpx.ASGITransport(app=api.app)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'db.json'
            with (patch.object(api, 'app_state', state), patch.object(api, 'DB_PATH', path),
                  patch.object(api, 'ENCRYPTION_KEY_HEX', '01' * 32),
                  patch.object(api, 'BACKEND_API_TOKEN', 'integration'),
                  patch.object(bot, 'BACKEND_API_TOKEN', 'integration'),
                  patch.object(api, '_build_client', return_value=telegram),
                  patch.object(bot.httpx, 'AsyncClient', side_effect=lambda **kw: client_class(transport=transport, **kw))):
                response = await bot.api_post('/accounts', {'account_id': 'demo', 'phone': '+15551234567'})
                self.assertEqual(response.status_code, 201)
                self.assertFalse(response.json()['connected'])
                self.assertEqual(response.json()['phone_hint'], '••••4567')
                self.assertEqual((await bot.api_post('/accounts/demo/login/send_code', {'phone': '+15551234567'})).status_code, 200)
                self.assertLess(time.monotonic() - state['pending_logins']['demo']['created_at'], api.LOGIN_TTL)
                self.assertEqual((await bot.api_post('/accounts/demo/login/verify', {'code': '12345'})).status_code, 401)
                response = await bot.api_post('/accounts/demo/login/verify', {'password': ' secret '})
                self.assertEqual(response.status_code, 200)
                self.assertTrue(response.json()['connected'])
                telegram.sign_in.assert_awaited_with(password=' secret ')
                self.assertNotIn(session, path.read_text())
                response = await bot.api_post('/storage/backup')
                self.assertEqual(response.status_code, 200)
                self.assertTrue(path.with_suffix('.json.backup').exists())
                self.assertEqual((await bot.api_post('/accounts/demo/logout')).status_code, 200)
                self.assertFalse((await bot.api_get('/accounts')).json()[0]['session_available'])

    def test_validation_does_not_echo_sensitive_values(self):
        with patch.object(api, 'BACKEND_API_TOKEN', 'test'):
            client = TestClient(api.app, headers={'Authorization': 'Bearer test'})
            secret = 'private-password-' * 10
            response = client.post('/accounts/example/login/verify', json={'password': secret})
            self.assertEqual(response.status_code, 422)
            self.assertNotIn(secret, response.text)

    async def test_stale_voice_confirmation_does_not_call_api(self):
        from types import SimpleNamespace

        query = SimpleNamespace(message=SimpleNamespace(message_id=1), data='join_confirm', answer=AsyncMock())
        context = SimpleNamespace(user_data={'join_prompt': 2, 'join_link': '@example'})
        with patch.object(bot, 'api_get', new_callable=AsyncMock) as get:
            result = await bot._voice_confirm(SimpleNamespace(callback_query=query), context, 'join')
        self.assertEqual(result, bot.JOINVC_CONFIRM)
        get.assert_not_awaited()

    async def test_private_invite_joins_channel_before_voice_and_leave_does_not_import(self):
        from types import SimpleNamespace

        check_result = object()
        joined = api.types.Chat(id=42, title='Private group', photo=api.types.ChatPhotoEmpty(),
                                participants_count=1, date=None, version=1)
        client = AsyncMock(side_effect=[check_result, SimpleNamespace(chats=[joined])])
        tg = SimpleNamespace(telegram_client=client)
        self.assertEqual(await api._resolve_voice_target(tg, '+invite', join_channel=True), -42)
        requests = [call.args[0] for call in client.await_args_list]
        self.assertIsInstance(requests[0], api.functions.messages.CheckChatInviteRequest)
        self.assertIsInstance(requests[1], api.functions.messages.ImportChatInviteRequest)
        client.reset_mock(side_effect=True)
        client.return_value = check_result
        with self.assertRaises(api.HTTPException) as caught:
            await api._resolve_voice_target(tg, '+invite', join_channel=False)
        self.assertEqual(caught.exception.status_code, 403)
        client.assert_awaited_once()

    async def test_exclusive_command_across_owners(self):
        from types import SimpleNamespace

        self.assertEqual(bot._parse_owner_ids('123', '456, 789'), {'123', '456', '789'})
        with self.assertRaises(ValueError):
            bot._parse_owner_ids('123, nope')
        shared = {}

        def fixture(owner, text):
            msg = SimpleNamespace(text=text, reply_text=AsyncMock())
            update = SimpleNamespace(message=msg, callback_query=None, effective_message=msg,
                                     effective_user=SimpleNamespace(id=owner), effective_chat=SimpleNamespace(id=owner))
            return update, SimpleNamespace(bot_data=shared, user_data={})

        first, first_context = fixture(123, '🔐 Login')
        second, second_context = fixture(456, '☁️ Backup')
        await bot._dispatch_command(first, first_context)
        self.assertEqual(shared['flow']['state'], bot.LOGIN_LABEL)
        deadline = shared['flow']['deadline']
        await bot._dispatch_command(second, second_context)
        self.assertIn('Another owner', second.message.reply_text.await_args.args[0])
        first.message.text = '📊 Status'
        await bot._dispatch_command(first, first_context)
        self.assertIn('Finish the current command', first.message.reply_text.await_args.args[0])
        self.assertEqual(shared['flow']['deadline'], deadline)
        first.message.text = '/cancel'
        await bot._dispatch_command(first, first_context)
        self.assertNotIn('flow', shared)

    async def test_deadline_expires_even_without_an_update(self):
        import asyncio
        from types import SimpleNamespace

        notice = asyncio.Event()
        context = SimpleNamespace(bot_data={}, user_data={}, bot=SimpleNamespace(
            send_message=AsyncMock(side_effect=lambda *args, **kwargs: notice.set())),
            application=SimpleNamespace(create_task=lambda coro, **kwargs: asyncio.create_task(coro)))
        update = SimpleNamespace(message=SimpleNamespace(text='🔐 Login', reply_text=AsyncMock()),
                                 callback_query=None, effective_user=SimpleNamespace(id=123),
                                 effective_chat=SimpleNamespace(id=123))
        with patch.object(bot, 'COMMAND_TIMEOUT', 0.05):
            await bot._dispatch_command(update, context)
            await asyncio.wait_for(notice.wait(), 1)
        self.assertNotIn('flow', context.bot_data)
