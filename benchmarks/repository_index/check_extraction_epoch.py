"""Automatic unchanged-source invalidation, local package facts and fail-closed schema."""
import copy
from pathlib import Path
import tempfile
from unittest.mock import patch

from extraction_epoch import LoadedExtractionEpoch, loaded_identity
from owned_scope import OwnedIndexScope
from index import outline

OWNER=900001


def check():
    with tempfile.TemporaryDirectory(prefix='tofu-extraction-epoch-') as folder:
        root=Path(folder);(root/'module.py').write_text('def original():\n    return source_reference\n')
        epoch=LoadedExtractionEpoch();scope=OwnedIndexScope(OWNER,str(root),1024*1024,epoch_source=epoch)
        try:
            assert scope.scan(OWNER)==outline.scan(str(root));before=epoch()
            assert epoch()==before
            def changed(path,text):return [outline.Tag(path,'new_backend',1,'def','def new_backend():',0)]
            with patch.object(outline,'_collect_source_tags',new=changed):
                assert epoch()>before
                assert scope.scan(OWNER)==outline.scan(str(root))
            assert scope.scan(OWNER)==outline.scan(str(root))
            before=epoch()
            with patch.object(outline,'STOP_WORDS',outline.STOP_WORDS|{'source_reference'}):
                assert epoch()>before and scope.scan(OWNER)==outline.scan(str(root))
            with patch('extraction_epoch.importlib.metadata.version',side_effect=lambda package:'observed-fixture-version'):
                before=epoch();assert scope.scan(OWNER)==outline.scan(str(root));assert epoch()==before
        finally:scope.close(OWNER)
    identity=loaded_identity();current=[identity]
    epoch=LoadedExtractionEpoch(identity_source=lambda:current[0]);assert epoch()==0
    current[0]=copy.deepcopy(identity);current[0]['tag_schema'].append('incompatible_field')
    try:epoch()
    except RuntimeError as error:assert 'rebuild cache codec' in str(error)
    else:raise AssertionError('incompatible tag schema reused')
    current[0]=identity;assert epoch()==0
    print('loaded code/config/local package witness invalidates unchanged source; stable identity reuses epoch; incompatible schema fails closed')


if __name__=='__main__':check()
