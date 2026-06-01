"""routes/translate_mt_test.py — Machine-translation provider test endpoint.

Extracted from ``routes/translate.py``. Registers on the same
``translate_bp`` Blueprint via side-effect import in ``routes/__init__.py``.
"""

from flask import jsonify, request

from lib.log import get_logger
from routes.translate import translate_bp

logger = get_logger(__name__)


@translate_bp.route('/api/translate/mt-test', methods=['POST'])
def mt_test():
    """Test a machine translation provider configuration.

    Accepts the MT config inline (not yet saved) and runs a test translation.
    """
    data = request.get_json(silent=True) or {}
    mt_config = data.get('mt_config', {})
    text = data.get('text', 'Hello, this is a test.')
    source = data.get('source', 'en')
    target = data.get('target', 'zh')

    if not mt_config.get('api_key'):
        return jsonify({'ok': False, 'error': 'API Key 未填写'})

    api_key = mt_config.get('api_key', '')
    app_id = mt_config.get('app_id', '')
    api_url = mt_config.get('api_url', '')

    try:
        from lib.mt_provider import _niutrans_v1, _niutrans_v2, _normalize_lang
        src_lang = _normalize_lang(source)
        tgt_lang = _normalize_lang(target)

        if app_id:
            result = _niutrans_v2(text, src_lang, tgt_lang, api_key, app_id, api_url)
        else:
            result = _niutrans_v1(text, src_lang, tgt_lang, api_key, api_url)

        logger.info('[MT-Test] Success: "%s" → "%s" (%s→%s)',
                    text[:50], result[:50], source, target)
        return jsonify({'ok': True, 'translated': result})
    except Exception as e:
        logger.warning('[MT-Test] Failed: %s', e)
        return jsonify({'ok': False, 'error': str(e)})
