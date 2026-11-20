import pytest

from lib.tasks_pkg.compaction._resume_projection import (
    CHECKPOINT_IMPLEMENTATION, validated_archive_post_messages,
)

pytestmark = pytest.mark.unit


def test_competing_archive_receipt_cannot_use_earlier_selection():
    selected = {'implementation': CHECKPOINT_IMPLEMENTATION, 'resumeWitness': {'turnId': 't'}}
    post = [{'role': 'user', 'content': 'fixture'}]
    item = {'archive': {'receipt': selected.copy()}, 'postMessages': post}
    assert validated_archive_post_messages(item, selected) == post
    item['archive']['receipt']['resumeWitness'] = {'turnId': 'competing-turn'}
    assert validated_archive_post_messages(item, selected) is None
    assert post == [{'role': 'user', 'content': 'fixture'}]
