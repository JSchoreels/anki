# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

"""Controlled 2-second warm-up experiment using the real graphs/preparation code.

Inference/state helpers are doubles; graph aggregation uses the copied collection.
This measures orchestration, not real RWKV warm-up or model inference.
"""
import ast
import enum
import json
import logging
import threading
import time
from pathlib import Path
from types import SimpleNamespace as NS

import flask
from anki.collection import Collection
from anki.stats_pb2 import GraphsRequest, GraphsResponse

HERE = Path(__file__).resolve().parent
ANKI = Path('/Users/jschoreels/workspace/anki')
COLLECTION = '/private/tmp/anki-stats-audit-eqv1ucpr/collection.anki2'
app = flask.Flask('stats_pending_benchmark')


def source_function(path, name):
    module = ast.parse(path.read_text())
    return next(node for node in module.body if isinstance(node, ast.FunctionDef) and node.name == name)


def install(function, namespace):
    tree = ast.Module(body=[ast.ImportFrom(module='__future__', names=[ast.alias(name='annotations')], level=0), function], type_ignores=[])
    exec(compile(ast.fix_missing_locations(tree), 'benchmark', 'exec'), namespace)


class Status(enum.Enum):
    PENDING = 'pending'
    READY = 'ready'
    UNAVAILABLE = 'unavailable'
    FAILED = 'failed'


def run(variant):
    col = Collection(COLLECTION)
    ready = threading.Event()
    lock = threading.Lock()
    pending, warmed = {1:1}, {}
    token = NS(state_generation=1)
    scores = NS(scores=[],input_build=NS(parsed_cards=0),target_retentions_by_card_id={},
                intervening_reviews_by_card_id={},curve_due_card_ids=[],curve_scores=[])
    namespace = dict(
        time=time, logger=logging.getLogger('benchmark'), RwkvStatsPreparationStatus=Status,
        _reviewer_backend=object(), _reviewer_backend_state_lock=lock,
        _reviewer_backend_warmup_pending_generations=pending, _reviewer_backend_warmup_states=warmed,
        _RWKV_STATS_WARMUP_WAIT_TIMEOUT_SECS=120, _RWKV_STATS_WARMUP_WAIT_INTERVAL_SECS=.05,
        _reviewer_backend_warmup_key=lambda _:1, _search_uses_rwkv_instant_due=lambda _:False,
        _search_uses_rwkv_curve_due=lambda _:False, _search_uses_rwkv_curve_retrievability=lambda _:False,
        _prepare_reviewer_backend_for_stats=lambda _:ready.is_set(),
        _reviewer_backend_warmup_pending=lambda _:not ready.is_set(),
        _capture_reviewer_backend_prediction_state_token=lambda _:token,
        _rwkv_stats_prepare_key=lambda *a,**kw:None,
        _rwkv_stats_graph_scores_for_search=lambda **kw:scores,
        _set_rwkv_stats_graph_scores_if_current=lambda *a,**kw:True,
        _set_rwkv_stats_graph_scores=lambda *a,**kw:None,
        _ReviewerBackendPredictionBusy=type('Busy',(Exception,),{}),
        _ReviewerBackendPredictionAborted=type('Aborted',(Exception,),{}),
    )
    scheduler_path = ANKI / 'qt/aqt/rwkv_scheduler.py'
    install(source_function(scheduler_path, '_wait_for_reviewer_backend_warmup'), namespace)
    prepare = source_function(scheduler_path, 'prepare_stats_retrievability_scores')
    if variant == 'return_pending':
        class PendingRewrite(ast.NodeTransformer):
            changed = 0
            def visit_If(self, node):
                if ast.unparse(node.test) == 'not warmed_up and _reviewer_backend_warmup_pending(reviewer)':
                    node.body = [ast.parse('return RwkvStatsPreparationStatus.PENDING').body[0]]
                    self.changed += 1
                return self.generic_visit(node)
        rewrite = PendingRewrite()
        prepare = rewrite.visit(prepare)
        assert rewrite.changed == 1
    install(prepare, namespace)
    namespace.update(
        aqt=NS(mw=NS(reviewer=object()),rwkv_scheduler=NS(
            prepare_stats_retrievability_scores=namespace['prepare_stats_retrievability_scores'],
            RwkvStatsPreparationStatus=Status)),
        request=flask.request, flask=flask, GraphsRequest=GraphsRequest,
        RWKV_STATS_PENDING_HEADER='X-Anki-Rwkv-Stats-Pending',
        raw_backend_request=lambda _:lambda:col._backend.graphs(search='',days=365).SerializeToString(),
    )
    install(source_function(ANKI/'qt/aqt/mediasrv.py','graphs'),namespace)

    def finish():
        with lock:
            pending.clear()
            warmed[1] = True
            ready.set()
    timer = threading.Timer(2.0, finish)
    start = time.perf_counter()
    timer.start()
    try:
        with app.test_request_context('/_anki/graphs',method='POST',data=GraphsRequest(search='',days=365).SerializeToString()):
            first = namespace['graphs']()
        first_ms = (time.perf_counter()-start)*1000
        first_pending = first.headers.get('X-Anki-Rwkv-Stats-Pending')=='1'
        if first_pending:
            # Preserve WithGraphData's current delay after a pending response.
            time.sleep(2.0)
            assert ready.is_set()
            with app.test_request_context('/_anki/graphs',method='POST',data=GraphsRequest(search='',days=365).SerializeToString()):
                final = namespace['graphs']()
        else:
            final = first
        ready_ms = (time.perf_counter()-start)*1000
        first_data = GraphsResponse.FromString(first.data)
        final_data = GraphsResponse.FromString(final.data)
        assert first_data.card_counts == final_data.card_counts
        assert first_data.intervals == final_data.intervals
        assert first_pending == (variant=='return_pending')
        return dict(variant=variant,synthetic_warmup_ms=2000,first_graphs_ms=first_ms,
                    retry_delay_ms=2000,first_pending=first_pending,ready_ms=ready_ms,
                    ordinary_histograms_equal=True)
    finally:
        timer.join()
        col.close()


rows=[]
for round_no in range(3):
    for variant in (['baseline','return_pending'] if round_no%2==0 else ['return_pending','baseline']):
        row=run(variant)
        row['round']=round_no+1
        rows.append(row)
        print(json.dumps(row),flush=True)
        (HERE/'pending-results.json').write_text(json.dumps(rows,indent=2))
