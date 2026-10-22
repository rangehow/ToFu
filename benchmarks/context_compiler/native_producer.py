"""Actual settled Turn writer and owner-bound conversation-to-model producer.

Entry: NativeContextCorpus, make_project. Reuses explicitly owned native engine
lifecycle; never selects or opens the application's authority. Final body/token/
protocol owners remain the existing admission comparison in both arms.
"""
import copy
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from benchmarks.knowledge_html.durable import DurableHtmlCorpus
from lib.conversations.repository import ConversationRepository
from lib.tasks_pkg.conv_message_builder.api import build_api_messages_from_db
from check_provider import OWNER
from admission_project import make_project as make_admitted_project


class NativeContextCorpus(DurableHtmlCorpus):
    def __init__(self, root: Path, engine: Path):
        super().__init__(root, engine, variant='candidate', owner_user_id=OWNER)
        self.conversation_id = 'native-context-fixture'
        self.source_fingerprint = None
        self.conversations = ConversationRepository(client_factory=lambda **_: self.client)

    def populate(self, rows):
        fingerprint = json.dumps(rows, sort_keys=True, ensure_ascii=True)
        if self.source_fingerprint is not None:
            if fingerprint != self.source_fingerprint:
                raise RuntimeError('static producer fixture changed without a new authority epoch')
            return
        self.client.command('conversation.create',
                            {'conv_id': self.conversation_id, 'user_id': OWNER}, 'create-context-fixture')
        for index, row in enumerate(rows):
            projection = copy.deepcopy(row)
            role = projection.pop('role')
            actor = 'human' if role == 'user' else role
            for wire, stored in (('_responses_items', '_responsesItems'),
                                 ('_anthropic_content_blocks', '_anthropicContentBlocks')):
                if wire in projection:
                    projection[stored] = projection.pop(wire)
            command_id = f'append-context-{index}'
            self.client.command('turn.append_settled',
                {'conversation_id': self.conversation_id, 'user_id': OWNER,
                 'turn_id': f'context-turn-{index}', 'command_id': command_id,
                 'actor': actor, 'status': 'completed', 'lane_id': 'main',
                 'created_at': 1700000000000 + index * 1000,
                 'now': 1700000000000 + index * 1000,
                 'projection': projection}, command_id)
        self.source_fingerprint = fingerprint

    def messages(self):
        snapshot = self.conversations.get(self.conversation_id, user_id=OWNER)
        if snapshot is None:
            raise RuntimeError('committed context conversation is missing')
        return snapshot.messages


def make_project(variant, corpus):
    admitted = make_admitted_project(variant)
    def project(profile):
        if corpus.source_fingerprint is None:
            raise RuntimeError('context fixture must be committed before projection')
        committed = corpus.messages()
        messages = build_api_messages_from_db(corpus.conversation_id, {},
                    preloaded_messages=committed, user_id=OWNER)
        if messages is None:
            raise RuntimeError('native conversation producer failed')
        return admitted(messages, profile)
    return project
