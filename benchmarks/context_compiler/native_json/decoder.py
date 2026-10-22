"""Private native argument decoder with the canonical validator as fallback.

Entry: make_decoder. This experiment never patches production globals or selects
an application backend. Native parsing disables string caching; non-ASCII input
stays canonical to avoid attaching an extra UTF-8 cache to caller-owned strings.
"""
import json
import math
import sys

from lib.llm import conversion

_JSON_INIT = json.JSONDecoder.__init__
_JSON_COMPONENTS = (json.decoder.JSONObject, json.decoder.JSONArray,
                    json.decoder.scanstring, json.scanner.make_scanner)
_ISFINITE = math.isfinite
_GET_INT_LIMIT = getattr(sys, 'get_int_max_str_digits', None)


def eligible_native_input(value, minimum_characters=512):
    """Measured bridge eligibility; leaves costly Unicode paths canonical."""
    return (type(value) is str and len(value) >= minimum_characters
            and value.isascii() and '\\u' not in value)


class NativeArgumentDecoder:
    """Guarded callable plus private handoff after the owned eligibility check."""
    def __init__(self, decode_eligible, minimum_characters):
        self._decode_eligible = decode_eligible
        self._minimum_characters = minimum_characters

    def __call__(self, value):
        if not eligible_native_input(value, self._minimum_characters):
            return conversion.object_arguments(value)
        return self._decode_eligible(value)


def make_decoder(native_module, *, minimum_characters=512):
    """Explicit experiment activation; no optional import on ordinary requests."""
    import pydantic_core
    parse = pydantic_core.from_json
    finite = native_module.float_tokens_finite
    canonical = conversion.object_arguments
    def decode_eligible(value):
        if conversion.object_arguments is not canonical:
            return conversion.object_arguments(value)
        if (json.loads is not conversion._ARGUMENT_JSON_LOADS
                or json.JSONDecoder is not conversion._ARGUMENT_JSON_DECODER
                or json.JSONDecoder.decode is not conversion._ARGUMENT_JSON_DECODE
                or json.JSONDecoder.raw_decode is not conversion._ARGUMENT_JSON_RAW_DECODE
                or json.JSONDecoder.__init__ is not _JSON_INIT
                or (json.decoder.JSONObject, json.decoder.JSONArray,
                    json.decoder.scanstring, json.scanner.make_scanner) != _JSON_COMPONENTS
                or math.isfinite is not _ISFINITE
                or pydantic_core.from_json is not parse
                or native_module.float_tokens_finite is not finite):
            return canonical(value)
        # Jiter's 4300-digit native parser limit agrees with CPython's default.
        # A stricter explicit interpreter limit remains canonical; a larger or
        # disabled limit is recovered by the fallback on native parse rejection.
        limit = _GET_INT_LIMIT() if _GET_INT_LIMIT is not None else 0
        if limit and limit < 4300:
            return canonical(value)
        try:
            decoded = parse(value, allow_inf_nan=False, cache_strings=False,
                            allow_partial=False)
        except ValueError:
            return canonical(value)
        if type(decoded) is not dict:
            # Do not retain a rejected graph during the canonical retry.
            del decoded
            return canonical(value)
        if not finite(value):
            # Standard parse_float tracks every overflow, including overwritten
            # duplicate-key values. Release the graph before retaining an error.
            del decoded
            raise conversion.ProtocolConversionError('tool_arguments_invalid_json')
        return decoded
    return NativeArgumentDecoder(decode_eligible, minimum_characters)
