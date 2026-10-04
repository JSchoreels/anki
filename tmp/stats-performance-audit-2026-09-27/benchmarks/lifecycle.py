# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

"""Exercise actual refresh code against Qt signals, without loading a profile."""
import ast
import json
from pathlib import Path
from time import perf_counter
from types import SimpleNamespace as NS

from PyQt6.QtCore import QObject, pyqtSignal

HERE = Path(__file__).resolve().parent
SSE = Path('/Users/jschoreels/workspace/Anki-Search-Stats-Extended')
module = ast.parse((SSE / '__init__.py').read_text())
function = next(node for node in module.body if isinstance(node, ast.FunctionDef) and node.name == 'new_refresh')
original = ast.unparse(function)
needle = 'self.form.web.loadFinished.connect(lambda: self.form.web.eval(setVars + innerJs))'
assert needle in original
candidate = original.replace(needle, '''previous = getattr(self, '_benchmark_load_callback', None)
    if previous is not None:
        self.form.web.loadFinished.disconnect(previous)
    self._benchmark_load_callback = lambda: self.form.web.eval(setVars + innerJs)
    self.form.web.loadFinished.connect(self._benchmark_load_callback)''')


class Web(QObject):
    loadFinished = pyqtSignal(bool)

    def __init__(self):
        super().__init__()
        self.calls = 0
        self.characters = 0

    def eval(self, script):
        self.calls += 1
        self.characters += len(script)


def run(source):
    col = NS(get_preferences=lambda: NS(scheduling=NS(rollover=4, learn_ahead_secs=1200)),
             decks=NS(current=lambda: {'id':1}, all_config=lambda: [], all=lambda: []), sched=NS(today=100))
    mw = NS(col=col, addonManager=NS(getConfig=lambda _: {'forceLang':'en_GB'}))
    namespace = dict(NewDeckStats=object, addon_dir=SSE, mw=mw, fallback_lang='en_GB',
                     getLocale=lambda _: '', getAvailableLangs=lambda: [], json=json, __name__='benchmark')
    exec(compile(source, 'sse-refresh', 'exec'), namespace)
    stats = NS(form=NS(web=Web()))
    per_load = []
    start = perf_counter()
    for _ in range(10):
        before = stats.form.web.calls
        namespace['new_refresh'](stats)
        stats.form.web.loadFinished.emit(True)
        per_load.append(stats.form.web.calls - before)
    return dict(callback_dispatch_ms=(perf_counter()-start)*1000, script_injections=stats.form.web.calls,
                injected_characters=stats.form.web.characters, per_load=per_load,
                final_connections=stats.form.web.receivers(stats.form.web.loadFinished))


rows = []
for round_no in range(5):
    for variant in (['baseline','single_callback'] if round_no % 2 == 0 else ['single_callback','baseline']):
        row = run(original if variant == 'baseline' else candidate)
        row.update(round=round_no+1, variant=variant)
        rows.append(row)
        assert row['per_load'] == (list(range(1,11)) if variant=='baseline' else [1]*10)
(HERE / 'lifecycle-results.json').write_text(json.dumps(rows, indent=2))
for row in rows:
    print(json.dumps(row))
