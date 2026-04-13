"""Tests for server-side message queue (lib/message_queue.py)."""

import json
import time
import pytest


def _qid():
    """Generate unique test conv ID to avoid cross-test pollution."""
    return f'test-queue-{time.time_ns()}'


class TestMessageQueueAPI:
    """Test queue API endpoints."""

    def test_enqueue_message(self, flask_client):
        """POST /api/chat/queue enqueues a message."""
        client = flask_client
        conv_id = _qid()
        client.put(f'/api/conversations/{conv_id}', json={
            'title': 'Queue Test',
            'messages': [{'role': 'user', 'content': 'Hello', 'timestamp': 1000}],
            'createdAt': 1000,
            'updatedAt': 1000,
        })

        resp = client.post('/api/chat/queue', json={
            'convId': conv_id,
            'message': {
                'text': 'Queued message 1',
                'timestamp': 2000,
            },
            'config': {'model': 'test-model'},
        })
        assert resp.status_code == 200
        data = resp.get_json()
        assert 'queueId' in data
        assert data['position'] == 1

    def test_enqueue_empty_rejected(self, flask_client):
        """POST /api/chat/queue rejects empty messages."""
        resp = flask_client.post('/api/chat/queue', json={
            'convId': 'test-conv',
            'message': {},
            'config': {},
        })
        assert resp.status_code == 400

    def test_enqueue_no_convid_rejected(self, flask_client):
        """POST /api/chat/queue rejects missing convId."""
        resp = flask_client.post('/api/chat/queue', json={
            'message': {'text': 'hello'},
        })
        assert resp.status_code == 400

    def test_get_queue(self, flask_client):
        """GET /api/chat/queue/<convId> returns queued messages."""
        client = flask_client
        conv_id = _qid()
        client.put(f'/api/conversations/{conv_id}', json={
            'title': 'Queue Test 2',
            'messages': [{'role': 'user', 'content': 'Hello', 'timestamp': 1000}],
            'createdAt': 1000,
            'updatedAt': 1000,
        })

        # Enqueue two messages
        client.post('/api/chat/queue', json={
            'convId': conv_id,
            'message': {'text': 'First', 'timestamp': 1000},
            'config': {},
        })
        client.post('/api/chat/queue', json={
            'convId': conv_id,
            'message': {'text': 'Second', 'timestamp': 2000},
            'config': {},
        })

        resp = client.get(f'/api/chat/queue/{conv_id}')
        assert resp.status_code == 200
        queue = resp.get_json()
        assert len(queue) == 2
        assert queue[0]['text'] == 'First'
        assert queue[1]['text'] == 'Second'
        assert queue[0]['position'] == 1
        assert queue[1]['position'] == 2

    def test_remove_from_queue(self, flask_client):
        """DELETE /api/chat/queue/<convId>/<queueId> removes one item."""
        client = flask_client
        conv_id = _qid()
        client.put(f'/api/conversations/{conv_id}', json={
            'title': 'Queue Test 3',
            'messages': [{'role': 'user', 'content': 'Hello', 'timestamp': 1000}],
            'createdAt': 1000,
            'updatedAt': 1000,
        })

        r1 = client.post('/api/chat/queue', json={
            'convId': conv_id,
            'message': {'text': 'Keep me', 'timestamp': 1000},
            'config': {},
        })
        r2 = client.post('/api/chat/queue', json={
            'convId': conv_id,
            'message': {'text': 'Remove me', 'timestamp': 2000},
            'config': {},
        })
        queue_id = r2.get_json()['queueId']

        resp = client.delete(f'/api/chat/queue/{conv_id}/{queue_id}')
        assert resp.status_code == 200

        # Verify only 1 left
        queue = client.get(f'/api/chat/queue/{conv_id}').get_json()
        assert len(queue) == 1
        assert queue[0]['text'] == 'Keep me'

    def test_clear_queue(self, flask_client):
        """DELETE /api/chat/queue/<convId> clears all items."""
        client = flask_client
        conv_id = _qid()
        client.put(f'/api/conversations/{conv_id}', json={
            'title': 'Queue Test 4',
            'messages': [{'role': 'user', 'content': 'Hello', 'timestamp': 1000}],
            'createdAt': 1000,
            'updatedAt': 1000,
        })

        client.post('/api/chat/queue', json={
            'convId': conv_id,
            'message': {'text': 'A', 'timestamp': 1000},
            'config': {},
        })
        client.post('/api/chat/queue', json={
            'convId': conv_id,
            'message': {'text': 'B', 'timestamp': 2000},
            'config': {},
        })

        resp = client.delete(f'/api/chat/queue/{conv_id}')
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['cleared'] == 2

        # Verify empty
        queue = client.get(f'/api/chat/queue/{conv_id}').get_json()
        assert len(queue) == 0

    def test_get_empty_queue(self, flask_client):
        """GET /api/chat/queue/<convId> returns empty list for unknown conv."""
        resp = flask_client.get('/api/chat/queue/nonexistent')
        assert resp.status_code == 200
        assert resp.get_json() == []
