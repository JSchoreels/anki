# RWKV model-size speed benchmark

The benchmark compares state building, cache loading, card scoring and stored
state for the Big, Small and Tiny model families. The latest measured results
and methodology are in [the 2026-11-14 report](docs/rwkv-speed-test-2026-11-14.MD).

Prepare a separate working collection from an existing backup in a temporary
directory; never point the benchmark at the live profile. The harness opens
its working collection read-write and writes temporary state stores. Follow
the project's collection safety instructions before running it.

On Windows, first run `just build` in the current checkout. Then run the
benchmark with a copied collection and an output directory:

```powershell
just rwkv-app-speed-bench C:/temp/anki-benchmark/collection.anki2 C:/temp/anki-benchmark/results
```

The runner copies the supplied collection again, checks CPU and GPU activity,
and records individual measurements in `results.jsonl`, summarized medians in
`summary.md`, and run details in `run.log`. It measures the Python-side work
when this checkout's `out/pyenv` is available. Default runs cover every size,
with both models, one model, and Instant alone. Keep collection data and raw
history dumps in the temporary output directory; report aggregate results only.
