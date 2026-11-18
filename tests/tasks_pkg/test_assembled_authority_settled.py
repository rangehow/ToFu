"""Assembly freezes the large per-Turn authority maps the call identity digests."""
import pytest

from lib.conversation_sync.settled_rows import SettledRowMutation, is_settled
from lib.tasks_pkg.chat_mode import apply_chat_mode
from lib.tasks_pkg.model_config import _assemble_tool_list

pytestmark = pytest.mark.unit


def test_assembled_catalog_contracts_and_search_text_are_settled():
    cfg = apply_chat_mode({'chatMode': 'chat', 'searchMode': 'multi'})
    _assemble_tool_list(
        cfg, project_path='', project_enabled=False, task_id='t-settled',
        search_mode='multi', search_enabled=True, fetch_enabled=True,
        code_exec_enabled=False, browser_enabled=False, desktop_enabled=False,
        messages=[])
    catalog = cfg['_executableToolCatalog']
    assert catalog, 'assembly admitted no executable tool'
    for key in ('_executableToolCatalog', '_toolContractDocumentsByName',
                '_executableToolSearchTextByName'):
        assert is_settled(cfg[key]), key
    with pytest.raises(SettledRowMutation):
        catalog.append({})
