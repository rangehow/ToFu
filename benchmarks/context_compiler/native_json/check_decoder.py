"""Compare native argument preparation against canonical values and fixed errors."""
import argparse
import importlib.util
import json
from pathlib import Path
import random
import struct
import sys
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from lib.llm import conversion
from check import load
from decoder import make_decoder


def witness(value):
    if type(value) is float: return ('float', struct.pack('!d', value).hex())
    if type(value) is dict: return ('dict', [(key, witness(v)) for key,v in value.items()])
    if type(value) is list: return ('list', [witness(v) for v in value])
    return (type(value).__name__, value)


def receipt(function, value):
    try: return ('value', witness(function(value)))
    except conversion.ProtocolConversionError as exc: return ('error', exc.code)


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--library', type=Path, required=True);p.add_argument('--output', type=Path, required=True)
    args=p.parse_args();module=load(args.library);native=make_decoder(module, minimum_characters=0)
    canonical=conversion.object_arguments;rng=random.Random(20261009)
    fixtures=['null','[]','1e999','[1e999]','{"n":1e999}','{"n":1e999,"n":0}',
              '{"outer":{"n":1e999,"n":0}}','{"n":NaN}',
              '{"n":Infinity}','{"s":"\\ud800"}','{"a":1,"b":2,"a":3}',
              '{"s":"界😀"}','{"n":-0.0}','{"n":'+str(2**256)+'}',
              '{"n":'+'9'*4301+'}', '{"missing":','{"n":01}',
              '{"n":1} trailing', '\ufeff{"n":1}']
    fixtures.extend(json.dumps({'s':text,'n':1.0}) for text in [
        'quoted "1e999"', 'backslash\\', 'escaped \\" quote 1e999',
        '\\uD800', '1e999', 'brackets [1e999]'])
    fixtures.extend('{"n":'+number+',"n":0}' for number in [
        '1.7976931348623157e308','1.7976931348623158e308',
        '1.7976931348623159e308','-1e999','9'*600+'.0'])
    for _ in range(10000):
        digits=''.join(str(rng.randrange(10)) for _ in range(rng.randrange(1,60)))
        fixtures.append('{"n":'+('-' if rng.randrange(2) else '')+'0.'+digits+'e'+str(rng.randrange(-350,350))+'}')
    for value in fixtures:
        assert receipt(native,value)==receipt(canonical,value), repr(value)
    old_limit=sys.get_int_max_str_digits()
    try:
        for limit,digits in [(640,641),(0,5000),(5000,4500)]:
            sys.set_int_max_str_digits(limit);value='{"n":'+'9'*digits+'}'
            assert receipt(native,value)==receipt(canonical,value)
    finally:sys.set_int_max_str_digits(old_limit)
    for attribute in ('loads',):
        original=getattr(json,attribute);observed=[]
        def custom(value,*a,**kw):observed.append(value);return original(value,*a,**kw)
        with patch.object(json,attribute,custom):
            assert receipt(native,'{"n":1}')==receipt(canonical,'{"n":1}')
        assert len(observed)==2
    original=json.JSONDecoder.__init__;observed=[]
    def custom_init(self,*a,**kw):observed.append(True);return original(self,*a,**kw)
    with patch.object(json.JSONDecoder,'__init__',custom_init):
        assert receipt(native,'{"n":1}')==receipt(canonical,'{"n":1}')
    assert len(observed)==2
    with patch.object(conversion,'object_arguments',lambda raw:{'hook':True}):
        assert native('{"n":1}')=={'hook':True}
    with patch.object(module,'float_tokens_finite',side_effect=MemoryError('fixture native failure')):
        faulty=make_decoder(module,minimum_characters=0)
        try:faulty('{"n":1}')
        except MemoryError:pass
        else:raise AssertionError('native resource failure was hidden')
    report={'format':'tofu.native-json-decoder-check/v1','seed':20261009,
            'exact_value_or_fixed_error_cases':len(fixtures),
            'integer_limits_constructor_decoder_validator_resource_ports':'pass'}
    args.output.write_text(json.dumps(report,indent=2)+'\n');print(json.dumps(report))


if __name__=='__main__':main()
