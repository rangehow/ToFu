"""Tests for lib/tasks_pkg/conv_message_builder.py — server-side message building."""

import json

import pytest

from lib.tasks_pkg.conv_message_builder import _transform_messages


class TestTransformMessages:
    """Test _transform_messages (server-side equivalent of buildApiMessages)."""

    def test_basic_user_assistant(self):
        raw = [
            {'role': 'user', 'content': 'Hello'},
            {'role': 'assistant', 'content': 'Hi there'},
            {'role': 'user', 'content': 'Follow up'},
        ]
        result = _transform_messages(raw, {})
        assert len(result) == 3
        assert result[0] == {'role': 'user', 'content': 'Hello'}
        assert result[1] == {'role': 'assistant', 'content': 'Hi there'}
        assert result[2] == {'role': 'user', 'content': 'Follow up'}

    def test_system_prompt_injection(self):
        raw = [{'role': 'user', 'content': 'Hi'}]
        result = _transform_messages(raw, {'systemPrompt': 'Be helpful.'})
        assert len(result) == 2
        assert result[0] == {'role': 'system', 'content': 'Be helpful.'}
        assert result[1] == {'role': 'user', 'content': 'Hi'}

    def test_empty_system_prompt_not_injected(self):
        raw = [{'role': 'user', 'content': 'Hi'}]
        result = _transform_messages(raw, {'systemPrompt': '  '})
        assert len(result) == 1
        assert result[0]['role'] == 'user'

    def test_strip_notranslate_tags(self):
        raw = [{'role': 'user', 'content': 'Hello <notranslate>code</notranslate> world'}]
        result = _transform_messages(raw, {})
        assert '<notranslate>' not in result[0]['content']
        assert 'Hello code world' == result[0]['content']

    def test_strip_nt_tags(self):
        raw = [{'role': 'user', 'content': 'Hello <nt>code</nt> world'}]
        result = _transform_messages(raw, {})
        assert '<nt>' not in result[0]['content']

    def test_reply_quotes_single(self):
        raw = [{'role': 'user', 'content': 'My reply', 'replyQuotes': ['quoted text']}]
        result = _transform_messages(raw, {})
        assert '[引用]' in result[0]['content']
        assert 'quoted text' in result[0]['content']
        assert 'My reply' in result[0]['content']

    def test_reply_quotes_multiple(self):
        raw = [{'role': 'user', 'content': 'My reply', 'replyQuotes': ['quote1', 'quote2']}]
        result = _transform_messages(raw, {})
        assert '[引用1]' in result[0]['content']
        assert '[引用2]' in result[0]['content']

    def test_legacy_reply_quote(self):
        raw = [{'role': 'user', 'content': 'Reply', 'replyQuote': 'old quote'}]
        result = _transform_messages(raw, {})
        assert '[引用]' in result[0]['content']
        assert 'old quote' in result[0]['content']

    def test_conversation_references(self):
        raw = [{'role': 'user', 'content': 'See this',
                'convRefTexts': [{'id': 'abc', 'title': 'Old Conv', 'text': 'prev discussion'}]}]
        result = _transform_messages(raw, {})
        assert 'REFERENCED_CONVERSATION' in result[0]['content']
        assert 'prev discussion' in result[0]['content']
        assert 'Old Conv' in result[0]['content']

    def test_pdf_text_inline(self):
        raw = [{'role': 'user', 'content': 'Analyze this',
                'pdfTexts': [{'name': 'doc.pdf', 'pages': 5, 'textLength': 1000, 'text': 'PDF body'}]}]
        result = _transform_messages(raw, {})
        assert 'PDF Document: doc.pdf' in result[0]['content']
        assert 'PDF body' in result[0]['content']

    def test_multimodal_images(self):
        raw = [{'role': 'user', 'content': 'What is this?',
                'images': [{'base64': 'abc123', 'mediaType': 'image/png'}]}]
        result = _transform_messages(raw, {})
        content = result[0]['content']
        assert isinstance(content, list)
        assert content[0]['type'] == 'image_url'
        assert 'abc123' in content[0]['image_url']['url']
        assert content[1] == {'type': 'text', 'text': 'What is this?'}

    def test_image_with_url_fallback(self):
        raw = [{'role': 'user', 'content': 'Describe',
                'images': [{'url': '/api/images/test.png'}]}]
        result = _transform_messages(raw, {})
        content = result[0]['content']
        assert isinstance(content, list)
        assert content[0]['image_url']['url'] == '/api/images/test.png'

    def test_image_with_caption(self):
        raw = [{'role': 'user', 'content': 'Read this',
                'images': [{'base64': 'x', 'mediaType': 'image/png',
                            'caption': 'Figure 1', 'pdfPage': 3}]}]
        result = _transform_messages(raw, {})
        content = result[0]['content']
        assert any('[PDF p3: Figure 1]' in b.get('text', '') for b in content if b.get('type') == 'text')

    def test_empty_assistant_uses_tool_summary(self):
        raw = [
            {'role': 'user', 'content': 'Search'},
            {'role': 'assistant', 'content': '', 'toolSummary': 'searched for X'},
        ]
        result = _transform_messages(raw, {})
        assert result[1]['content'] == 'searched for X'

    def test_empty_assistant_uses_tool_rounds_fallback(self):
        raw = [
            {'role': 'user', 'content': 'Search'},
            {'role': 'assistant', 'content': '',
             'toolRounds': [{'toolName': 'web_search', 'query': 'test'}]},
        ]
        result = _transform_messages(raw, {})
        assert 'web_search' in result[1]['content']

    def test_skip_endpoint_planner(self):
        raw = [
            {'role': 'user', 'content': 'Do X'},
            {'role': 'assistant', 'content': 'Plan...', '_isEndpointPlanner': True},
            {'role': 'assistant', 'content': 'Done'},
        ]
        result = _transform_messages(raw, {})
        assert len(result) == 2
        assert result[1]['content'] == 'Done'

    def test_skip_endpoint_worker_iteration(self):
        """Worker turns with _epIteration should be filtered out."""
        raw = [
            {'role': 'user', 'content': 'Do X'},
            {'role': 'assistant', 'content': 'Plan...', '_isEndpointPlanner': True},
            {'role': 'assistant', 'content': 'Worker output', '_epIteration': 1},
            {'role': 'user', 'content': 'Feedback', '_isEndpointReview': True},
            {'role': 'assistant', 'content': 'Worker rev2', '_epIteration': 2},
        ]
        result = _transform_messages(raw, {})
        # All endpoint messages filtered → only user(Do X) remains
        assert len(result) == 1
        assert result[0]['content'] == 'Do X'

    def test_skip_endpoint_review(self):
        raw = [
            {'role': 'user', 'content': 'Do X'},
            {'role': 'assistant', 'content': 'Done'},
            {'role': 'user', 'content': 'Feedback', '_isEndpointReview': True},
            {'role': 'assistant', 'content': 'Revised'},
        ]
        result = _transform_messages(raw, {})
        # After merge: user, assistant(Done+Revised)
        assert len(result) == 2

    def test_merge_consecutive_same_role(self):
        raw = [
            {'role': 'user', 'content': 'A'},
            {'role': 'assistant', 'content': 'B', '_isEndpointPlanner': True},
            {'role': 'assistant', 'content': 'C'},
            {'role': 'user', 'content': 'D', '_isEndpointReview': True},
            {'role': 'assistant', 'content': 'E'},
        ]
        result = _transform_messages(raw, {})
        # After filtering: user(A), assistant(C), assistant(E)
        # After merge: user(A), assistant(C\n\nE)
        assert len(result) == 2
        assert 'C' in result[1]['content']
        assert 'E' in result[1]['content']

    def test_trailing_empty_assistant_stripped(self):
        raw = [
            {'role': 'user', 'content': 'Hello'},
            {'role': 'assistant', 'content': '', 'toolRounds': [], 'timestamp': 123},
        ]
        result = _transform_messages(raw, {})
        assert len(result) == 1
        assert result[0]['content'] == 'Hello'

    def test_trailing_nonempty_assistant_kept(self):
        raw = [
            {'role': 'user', 'content': 'Hello'},
            {'role': 'assistant', 'content': 'Response'},
        ]
        result = _transform_messages(raw, {})
        assert len(result) == 2

    def test_exclude_last(self):
        raw = [
            {'role': 'user', 'content': 'Q1'},
            {'role': 'assistant', 'content': 'A1'},
            {'role': 'user', 'content': 'Q2'},
        ]
        result = _transform_messages(raw, {}, exclude_last=True)
        assert len(result) == 2
        assert result[-1]['content'] == 'A1'

    def test_metadata_not_leaked(self):
        """Ensure frontend metadata fields don't leak into API messages."""
        raw = [
            {'role': 'user', 'content': 'Hi',
             'timestamp': 123, 'images': [], 'pdfTexts': [],
             'originalContent': 'original', '_translateDone': True},
            {'role': 'assistant', 'content': 'Hello',
             'thinking': 'thoughts', 'translatedContent': 'translated',
             'toolRounds': [], 'usage': {'tokens': 100},
             'model': 'gpt-4o', 'finishReason': 'stop'},
        ]
        result = _transform_messages(raw, {})
        for msg in result:
            assert 'timestamp' not in msg
            assert 'thinking' not in msg
            assert 'translatedContent' not in msg
            assert 'usage' not in msg
            assert 'model' not in msg
            assert 'finishReason' not in msg
            assert 'originalContent' not in msg
            assert '_translateDone' not in msg

    def test_empty_messages(self):
        result = _transform_messages([], {})
        assert result == []

    def test_multimodal_merge(self):
        """Consecutive user messages with mixed content types merge correctly."""
        raw = [
            {'role': 'user', 'content': 'Text only', '_isEndpointReview': False},
            {'role': 'user', 'content': 'More text'},
        ]
        result = _transform_messages(raw, {})
        assert len(result) == 1
        assert 'Text only' in result[0]['content']
        assert 'More text' in result[0]['content']
