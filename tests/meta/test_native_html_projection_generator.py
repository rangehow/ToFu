"""Native HTML vocabulary/oracle remain derived from the declared Python owner."""
import json

import pytest

pytestmark = pytest.mark.unit


def test_generated_profile_entities_and_oracle_match_current_owner():
    from lib.knowledge.generated_native_html_profile import PROFILE
    from scripts.gen_native_html_projection import (
        TOKENIZER_DERIVED_LINES, owner_sha256, outputs, profiled_tokenizer)
    generated = outputs()
    if not profiled_tokenizer():
        # Another CPython patch release ships a different html.parser (e.g. its
        # CDATA element set), so the profiled oracle cannot be reproduced here.
        # Owner- and Unicode-derived data are still checked exactly.
        assert PROFILE['owner_sha256'] == owner_sha256()
        for path, expected in generated.items():
            if path.suffix == '.rs':
                def independent(text):
                    return [line for line in text.splitlines()
                            if not line.startswith(TOKENIZER_DERIVED_LINES)]
                assert independent(path.read_text()) == independent(expected), str(path)
        pytest.skip('tokenizer-derived outputs are certified only on Python '
                    + PROFILE['python_version'])
    for path, expected in generated.items():
        assert path.read_text() == expected, str(path)
        if path.suffix == '.json':
            rows = json.loads(expected)['cases']
            assert any(row.get('error') == 'numeric_reference' for row in rows)
            assert any(row['source'] == '<a b= "unterminated>tail' and
                       row['expected'] == 'tail' for row in rows)
