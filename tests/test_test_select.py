"""P2-3 针:scripts/test_select.py 的索引与选择逻辑(纯函数,不依赖真仓库)。

设计稿 docs/TESTING_STRATEGY.md §4。单元层全量实测 19m58s 超 15min 阈值,
迭代内环改「静态反向索引选择」:测试文件 → 它引用的源文件(AST import +
字面量路径),改动 ∩ 引用 = 入选。不上 ML(透明映射可审计,15k 规模用不上
预测模型)。本套件钉纯函数语义;脚本加载走 spec_from_file_location(scripts/
无 __init__.py,与 server.py 同款载入)。
"""

import importlib.util
import os

import pytest

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
_SCRIPT = os.path.join(ROOT, 'scripts', 'test_select.py')


def _load():
    spec = importlib.util.spec_from_file_location('test_select', _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ts = _load()


class TestRefsOfTestFile:
    def test_import_maps_to_repo_path(self):
        src = "import lib.foo.bar\nfrom routes.api_v1 import chat\n"
        refs = ts.refs_of_test_file(src)
        assert 'lib/foo/bar.py' in refs
        assert 'routes/api_v1/__init__.py' in refs or 'routes/api_v1.py' in refs

    def test_from_import_of_module_maps_to_module_path(self):
        src = "from lib.tasks_pkg.manager import get_task\n"
        refs = ts.refs_of_test_file(src)
        assert 'lib/tasks_pkg/manager.py' in refs or \
               'lib/tasks_pkg/manager/__init__.py' in refs

    def test_tests_helper_import_maps(self):
        src = "from tests._jsdom import run_harness\n"
        refs = ts.refs_of_test_file(src)
        assert 'tests/_jsdom.py' in refs

    def test_literal_path_refs_captured(self):
        src = "JS = os.path.join(ROOT, 'static', 'js', 'api.js')\n" \
              "X = 'static/js/core/api.js'\n" \
              "DOC = 'docs/API_CONTRACT.md'\n"
        refs = ts.refs_of_test_file(src)
        assert 'static/js/core/api.js' in refs
        assert 'docs/API_CONTRACT.md' in refs

    def test_external_packages_not_mapped(self):
        src = "import pytest\nimport tofu_search\nimport requests\n"
        refs = ts.refs_of_test_file(src)
        assert refs == set() or all(
            r.startswith(('lib/', 'routes/', 'tests/', 'static/', 'docs/'))
            for r in refs)


class TestSelectTests:
    INDEX = {
        'tests/test_a.py': {'lib/x.py'},
        'tests/test_b.py': {'static/js/ui/y.js'},
        'tests/test_frontend_c.py': {'static/js/core/z.js'},
        'tests/test_d.py': {'lib/x.py', 'lib/w.py'},
    }

    def test_changed_source_selects_importers(self):
        selected, _ = ts.select_tests(self.INDEX, ['lib/x.py'])
        assert selected >= {'tests/test_a.py', 'tests/test_d.py'}
        assert 'tests/test_b.py' not in selected

    def test_changed_test_file_selects_itself(self):
        selected, _ = ts.select_tests(self.INDEX, ['tests/test_b.py'])
        assert 'tests/test_b.py' in selected

    def test_blast_radius_jsdom_helper_pulls_frontend_family(self):
        selected, _ = ts.select_tests(self.INDEX, ['tests/_jsdom.py'])
        assert 'tests/test_frontend_c.py' in selected

    def test_blast_radius_api_js_pulls_frontend_family(self):
        selected, _ = ts.select_tests(self.INDEX, ['static/js/api.js'])
        assert 'tests/test_frontend_c.py' in selected

    def test_guard_core_always_runs(self):
        selected, _ = ts.select_tests(self.INDEX, ['lib/x.py'])
        assert any('contract' in os.path.basename(f) for f in selected), (
            'guard core must always be in the selection')

    def test_unknown_change_falls_back_to_guard_core(self):
        selected, reasons = ts.select_tests(self.INDEX, ['README.md'])
        direct = {f for f in selected if f in self.INDEX}
        assert not direct, 'unrelated change must not pull behaviour suites'
        assert selected, 'guard core must still run as the smoke floor'

    def test_conftest_change_selects_everything(self):
        selected, _ = ts.select_tests(self.INDEX, ['tests/conftest.py'])
        assert selected >= set(self.INDEX), (
            'conftest touches every session — the blast radius is the whole suite')
