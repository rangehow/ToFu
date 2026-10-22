"""Actual uncached task/publication parity for reference/cursor implementations."""
from pathlib import Path
import tempfile
from published_extraction import ReferenceExtractionPort,CursorExtractionPort
from publication_port import TransientChatRuntime
from task_measure import task_operation,end_task
from lib.agent_core.execution_session import bind_tool_environment
from lib.tasks_pkg.manager.runtime import chat_task_runtime
from index import outline


def main():
    original=outline._extract_treesitter_tags
    with tempfile.TemporaryDirectory(prefix='tofu-public-extraction-check-') as temporary:
        root=Path(temporary)
        (root/'module.js').write_text('class Example { method(){ return dependency; } }\nfunction javascriptSymbol() {}')
        (root/'module.py').write_text('def python_symbol():\n    return reference\n')
        (root/'module.rs').write_text('struct Data {}\nimpl Data { fn method() {} }')
        (root/'module.ts').write_text('interface Shape { count: number; }\nexport function typedSymbol(): void {}')
        runtime=TransientChatRuntime();task=runtime.create(user_id=900001);session=task['_executionSession']
        bind_tool_environment(session,lambda:None);session.mark_routed();session.mark_reserved();session.mark_admitted();assert runtime.mark_running(task['id'])
        reference=ReferenceExtractionPort(root);cursor=CursorExtractionPort(root)
        try:
            for _ in range(3):assert task_operation(reference,task)==task_operation(cursor,task)
            (root/'module.js').write_text('class Edited { changed() { return updated; } }')
            assert task_operation(reference,task)==task_operation(cursor,task)
            events=chat_task_runtime.poll(task['id'])['events'];assert len([e for e in events if e['type']=='tool_result'])==8
        finally:end_task(runtime,task)
        assert chat_task_runtime.get(task['id']) is None and runtime.task_count==0
    assert outline._extract_treesitter_tags is original
    print('Python/JS/TS/Rust, repeated/edit exact metadata;8 canonical publications;uncached lifecycle;production extractor global unchanged')


if __name__=='__main__':main()
