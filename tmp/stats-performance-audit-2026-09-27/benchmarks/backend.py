# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

"""Benchmark only copied collection data through existing Anki APIs."""
from __future__ import annotations

import ast
import json
import sys
from pathlib import Path
from time import perf_counter

import orjson
from anki.collection import Collection

HERE = Path(__file__).resolve().parent
INPUT = Path('/private/tmp/anki-stats-audit-eqv1ucpr')
COLLECTION = INPUT / 'collection.anki2'
SSE = Path('/Users/jschoreels/workspace/Anki-Search-Stats-Extended')
fixture = orjson.loads((INPUT / 'fixture.json').read_bytes())
reviewed_ids = list(dict.fromkeys(row['cid'] for row in fixture['revlogs']))
module = ast.parse((SSE / '__init__.py').read_text())
conversion = next(node for node in module.body if isinstance(node, ast.FunctionDef) and node.name == '_fsrs_preset_to_dict')
namespace = {}
exec(compile(ast.Module(body=[conversion], type_ignores=[]), 'sse_conversion', 'exec'), namespace)
convert = namespace['_fsrs_preset_to_dict']


def save(results):
    (HERE / 'backend-results.json').write_text(json.dumps(results, indent=2))


def encode_presets(presets, compact):
    if not compact:
        return orjson.dumps(presets)
    unique, indices, assignments = [], {}, {}
    for cid, preset in presets.items():
        key = orjson.dumps(preset)
        if key not in indices:
            indices[key] = len(unique)
            unique.append(preset)
        assignments[cid] = indices[key]
    return orjson.dumps({'presets': unique, 'cards': assignments})


def preset_run(variant, warm):
    col = Collection(str(COLLECTION))
    try:
        if warm:
            # Both variants start with exactly the same per-card resolver cache.
            for cid in reviewed_ids:
                col.fsrs_preset_for_card(cid)
        start = perf_counter()
        prewarm_ms = 0
        if variant == 'batch_prewarm_compact':
            before = perf_counter()
            # This public API invokes the native batch resolver for stateful cards.
            # Its full overhead is included, along with subsequent per-card calls.
            col.card_memory_metrics(reviewed_ids, include_retrievability=True)
            prewarm_ms = (perf_counter() - before) * 1000
        before = perf_counter()
        presets = {str(cid): convert(col.fsrs_preset_for_card(cid)) for cid in reviewed_ids}
        resolve_ms = (perf_counter() - before) * 1000
        before = perf_counter()
        payload = encode_presets(presets, variant != 'baseline')
        encode_ms = (perf_counter() - before) * 1000
        total_ms = (perf_counter() - start) * 1000
        decoded = orjson.loads(payload)
        if variant != 'baseline':
            decoded = {cid: decoded['presets'][index] for cid, index in decoded['cards'].items()}
        assert decoded == fixture['presets'], 'Preset semantics changed'
        return dict(variant=variant, warm=warm, total_ms=total_ms, prewarm_ms=prewarm_ms,
                    resolve_ms=resolve_ms, encode_ms=encode_ms, payload_bytes=len(payload),
                    equal=True, cards=len(presets))
    finally:
        col.close()


def graph_run(variant, force_all):
    col = Collection(str(COLLECTION))
    try:
        queries = [('', 365)]
        if force_all:
            queries.append(('', 0))
        if variant == 'baseline':
            queries.append(('-is:suspended', 0 if force_all else 365))
        before = perf_counter()
        outputs = [col._backend.graphs(search=search, days=days) for search, days in queries]
        elapsed_ms = (perf_counter() - before) * 1000
        if variant == 'baseline':
            expected = {str(k): v for k, v in outputs[-1].intervals.intervals.items()}
            browser_histogram = json.loads((HERE / 'browser-results.json').read_text())['lightweight']['intervals']['histogram']
            assert expected == browser_histogram, 'Interval histograms differ'
        return dict(variant=variant, force_all=force_all, requests=len(queries), backend_ms=elapsed_ms,
                    response_bytes=sum(len(output.SerializeToString()) for output in outputs))
    finally:
        col.close()


def main():
    results = {'input': str(COLLECTION), 'cards': len(fixture['cards']), 'reviewed_cards': len(reviewed_ids), 'presets': [], 'graphs': []}
    # Cold collection-level resolver cache; OS disk pages may already be cached.
    for round_no in range(3):
        order = ['baseline', 'batch_prewarm_compact'] if round_no % 2 == 0 else ['batch_prewarm_compact', 'baseline']
        for variant in order:
            row = preset_run(variant, warm=False)
            row['round'] = round_no + 1
            results['presets'].append(row)
            save(results)
            print(json.dumps(row), flush=True)
    # Warm runs reuse one collection to avoid repeatedly paying cold warm-up outside timing.
    col = Collection(str(COLLECTION))
    try:
        col.card_memory_metrics(reviewed_ids, include_retrievability=True)
        for cid in reviewed_ids:
            col.fsrs_preset_for_card(cid)
        for round_no in range(5):
            for variant in (['baseline','compact'] if round_no % 2 == 0 else ['compact','baseline']):
                start = perf_counter()
                presets = {str(cid): convert(col.fsrs_preset_for_card(cid)) for cid in reviewed_ids}
                resolve_ms = (perf_counter() - start) * 1000
                before = perf_counter()
                payload = encode_presets(presets, variant != 'baseline')
                encode_ms = (perf_counter() - before) * 1000
                assert presets == fixture['presets']
                row = dict(variant=variant,warm=True,round=round_no+1,total_ms=(perf_counter()-start)*1000,
                           resolve_ms=resolve_ms,encode_ms=encode_ms,payload_bytes=len(payload),equal=True)
                results['presets'].append(row)
                print(json.dumps(row), flush=True)
    finally:
        col.close()
    save(results)
    for force_all in [False, True]:
        for round_no in range(5):
            for variant in (['baseline','reuse_cards'] if round_no % 2 == 0 else ['reuse_cards','baseline']):
                row = graph_run(variant, force_all)
                row['round'] = round_no + 1
                results['graphs'].append(row)
                print(json.dumps(row), flush=True)
        save(results)


if __name__ == '__main__':
    for compact, name in [(False, 'baseline-presets.json'), (True, 'compact-presets.json')]:
        (INPUT / name).write_bytes(encode_presets(fixture['presets'], compact))
    if '--payload-only' not in sys.argv:
        main()
