"""Discord review messages attach stat-list images and retain workflow threads."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from flask import Flask
from webapp.integrations import discord


@pytest.mark.parametrize('existing', [False, True])
@pytest.mark.parametrize('has_image', [False, True])
def test_submission_sends_one_message(monkeypatch, tmp_path, existing, has_image):
    app = Flask(__name__)
    image = tmp_path / 'approval.png'
    image.write_bytes(b'image')
    namespace = SimpleNamespace(id=1, slug='pcm2026', name='PCM 2026')
    settings = SimpleNamespace(id=1, channel_id='channel', approver_role_id='approvers')
    client = Mock(bot_token='synthetic-token')
    client.create_thread.return_value = {'id': 'new-thread'}
    client.submission_message_reference.return_value = ('channel', 'existing-thread')
    client.send_message.return_value = {'id': 'starter-message'}
    client.send_message_with_file.return_value = {'id': 'starter-message'}
    monkeypatch.setattr(discord, 'DiscordClient', lambda: client)
    monkeypatch.setattr(discord, 'get_discord_settings_for_namespace_workflow', lambda *a: [settings])
    monkeypatch.setattr(discord, '_load_namespace_discord_scope_state', lambda *a, **kw: {'id': 1})
    monkeypatch.setattr(discord, '_build_stat_approval_image', lambda *a, **kw: str(image) if has_image else None)
    monkeypatch.setattr(discord, '_load_discord_workflow_thread', lambda *a, **kw: SimpleNamespace(thread_id='existing-thread') if existing else None)
    store = Mock()
    monkeypatch.setattr(discord, '_save_discord_workflow_thread', store)
    monkeypatch.setattr(discord, '_namespace_dashboard_url', lambda _: 'https://example.test/namespace')
    with app.app_context():
        assert discord.notify_discord_stat_scope_saved(
            namespace, 'Tour Test', 'https://example.test/review', 3,
            saved_by='editor', stat_list_id=42,
        )
    sender = client.send_message_with_file if has_image else client.send_message
    sender.assert_called_once()
    other = client.send_message if has_image else client.send_message_with_file
    other.assert_not_called()
    channel, message = sender.call_args.args[:2]
    assert channel == ('existing-thread' if existing else 'new-thread')
    assert 'Awaiting approval' in message
    assert 'Submitted by editor\n' in message
    if existing:
        client.remove_reaction.assert_called_once_with('channel', 'existing-thread', '\u2705')
        client.create_thread_from_message.assert_not_called()
        store.assert_not_called()
    else:
        client.remove_reaction.assert_not_called()
        client.create_thread.assert_called_once_with('channel', 'Stat Edits: Tour Test')
        assert sender.call_args.kwargs['silent'] is True
        client.create_thread_from_message.assert_not_called()
        store.assert_called_once()
        header = message.splitlines()[0]
        assert header.startswith('[pcm2026](')
        assert '): [' in header
        assert 'Tour Test' in header
        assert 'Namespace:' not in message
    if has_image:
        assert not image.exists()


def test_thread_is_created_from_the_submission_message(monkeypatch):
    client = discord.DiscordClient(bot_token='synthetic-token')
    request = Mock(return_value={'id': 'starter-message'})
    monkeypatch.setattr(client, '_request', request)
    assert client.create_thread_from_message('channel', 'starter-message', 'Review') == {'id': 'starter-message'}
    request.assert_called_once_with(
        'POST', '/channels/channel/messages/starter-message/threads',
        {'name': 'Review', 'auto_archive_duration': 10080},
    )
