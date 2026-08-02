# PeerReviewBench Markdown example

This repository-only example runs Scriptorium's real four-role review workflow on the Markdown workspaces from
[PeerReviewBench](https://huggingface.co/datasets/prometheus-eval/peerreview-bench), then exports the stored findings
to the benchmark's BYOJ format and invokes its recall and precision evaluators.

It is a manual observation channel, not a CI or release gate. The example does not implement LaTeX input, manuscript
revision, verification, or simulated human decisions. Every successful paper stops at Scriptorium's
`awaiting_decision` state.

## Environment

Run the example from a Scriptorium repository checkout. Use Python 3.12 or 3.13 for the complete workflow: the
precision evaluator's OpenHands version is pinned by the upstream benchmark and requires that range.

```bash
python -m pip install -e '.[all]'
python -m pip install -r egs/peerreviewbench/requirements.txt

docker build --pull \
  --file egs/peerreviewbench/precision.Dockerfile \
  --tag scriptorium-peerreviewbench-precision:openhands-1.7.0 \
  egs/peerreviewbench

mkdir -p egs/peerreviewbench/.scriptorium
cp egs/peerreviewbench/routes.example.toml \
  egs/peerreviewbench/.scriptorium/config.toml
```

Edit the copied route file before running. All four review roles are used, and `visual_transcription` must point to an
explicitly configured vision-capable route. Revision and verification routes are also present because the core
configuration freeze validates them, but this benchmark never invokes those stages. Replace the example's zero token
prices if model cost should be meaningful; those values are placeholders, not a claim that the configured models are
free.

Docker is required only for precision evaluation. The image name is locked in `benchmark.lock.toml`, and the exact
local image ID is frozen in each evaluation manifest. Evaluation fails before invoking either judge if the image has
not been built.

The code and data revisions are frozen in `benchmark.lock.toml`: upstream
[`a48fe631`](https://github.com/prometheus-eval/cmu-paper-reviewer/commit/a48fe6316d21735d15ba11c92c480e9a239e83c1)
and dataset
[`b782d667`](https://huggingface.co/datasets/prometheus-eval/peerreview-bench/tree/b782d6676fcece1ed33f1eb2c0e27241d19f9f63).
Preparation fails closed if the live Hugging Face dataset revision, Dataset Viewer response revision, archive SHA256,
dataset paths, blob sizes, or blob hashes differ from the lock.

## 1. Prepare Markdown workspaces

The default smoke set is the first five locked papers:

```bash
python egs/peerreviewbench/prepare.py
```

Select papers explicitly by repeating `--paper-id`, or acknowledge the full 78-paper download with `--all`:

```bash
python egs/peerreviewbench/prepare.py --paper-id 7 --paper-id 12
python egs/peerreviewbench/prepare.py --all
```

Only `preprint/` files referenced by the locked reviewer rows are reconstructed. Human reviews, rubrics, and model
reviews are never written into the prepared manuscript workspace. Data is written atomically under
`.cache/dataset/<revision>/paper<ID>/` and an existing paper is reused only after a full manifest, size, and SHA256
check.

Blob retrieval uses the Hugging Face Dataset Viewer at its verified `x-revision`, so selected papers can be prepared
without downloading every submitted-paper Parquet shard. Some papers include large code or supplementary trees; even
the five-paper smoke set is not small. In the pinned revision, paper 3 alone is about 363 MB across 8,329 files, and
isolated snapshots, bundles, and per-mode evaluation copies require additional disk space.

## 2. Run Scriptorium

```bash
python egs/peerreviewbench/run.py
```

The defaults again select five prepared papers. Use the same explicit selection options as preparation:

```bash
python egs/peerreviewbench/run.py --paper-id 7 --paper-id 12
python egs/peerreviewbench/run.py --all
```

Each paper receives an isolated generated project, SQLite database, artifact store, and Scriptorium run. The adapter
recursively exposes the Markdown, figures, code, and supplementary files as normal bundle sources. It also produces a
neutral PDF rendering of `preprint.md` and the images listed by the dataset so the existing PDF/page bundle contract
and figure-review role remain active.

When a prepared paper has appended figures, their raster pages trigger one independent `visual_transcription` task
before the four reviewers. It transcribes visible labels, legends, axes, and table text for PDF evidence validation;
the transcription is not copied into reviewer workspaces and cannot create findings. This is a separately metered
model call recorded with the other task attempts and artifacts. The benchmark does not install or use local OCR or
Tesseract.

The locked dataset contains dangling Markdown crop placeholders such as `page_1012_172_388_388.png`; these files are
not present in the benchmark blobs. The adapter ignores only missing root-level names matching that exact conversion
pattern, records the count in the build log, and still rejects every other missing or unsafe image reference.
`images_list.json` entries and raster files under the manuscript's top-level `images/` directory remain authoritative
figure inputs.

The generated `benchmark.tex` is only a configuration sentinel: the core project parser currently requires a `.tex`
`manuscript.main`, but the file is never compiled or exposed as a manuscript source.

The command prints the new run directory. Its atomic `run_manifest.json` freezes the Scriptorium commit, hashes of the
relevant core, prompt, and benchmark source files, dataset and upstream revisions, a secret-free route summary and
digest, paper selection, per-paper core run IDs, attempt provenance, prompt/schema/artifact digests, token use, cost,
duration, finding payload digest, and status. A paper is complete only after its pre-review visual-transcription task
(when raster pages exist) and all four review tasks complete, leaving the core run at `awaiting_decision`. Findings
and per-role selection remain limited to the four configured reviewer roles; the transcription task contributes only
provenance, artifacts, token usage, duration, and estimated cost.

Before a completed paper is accepted on resume, the benchmark rechecks the frozen bundle directory digest, its source
map, PDF, rendered pages, and every content-addressed artifact in the paper's Scriptorium store, including build
evidence and task artifacts.

If a run is interrupted or a task fails, correct the external problem and resume the same directory:

```bash
python egs/peerreviewbench/run.py \
  --resume egs/peerreviewbench/runs/<run-id>
```

Resume rejects changed code, installed package versions, data, routes, budget, or paper selection. It does not
advance an `awaiting_decision` run into revision.

## 3. Export and evaluate

The default finding policy retains at most five findings from each role, following Scriptorium's canonical severity
and creation ordering:

```bash
python egs/peerreviewbench/evaluate.py \
  --run-dir egs/peerreviewbench/runs/<run-id> \
  --finding-mode per-role-5
```

The same stored review can be scored with every deduplicated finding without rerunning the reviewer:

```bash
python egs/peerreviewbench/evaluate.py \
  --run-dir egs/peerreviewbench/runs/<run-id> \
  --finding-mode all
```

The two modes use the exact BYOJ slugs `scriptorium_per_role_5` and `scriptorium_all`. For each selected finding:

- `main_point` is the claim;
- `claim_full` combines the claim and explanation;
- `evidence_full` contains quoted evidence with source locations;
- `text` combines those judgeable fields and deliberately excludes the suggested action;
- the nested `scriptorium` object preserves the finding ID, role, severity, category, confidence, and anchors.

Evaluation calls the pinned `evaluate_recall.py` and `evaluate_precision.py` components separately through a small
launcher that forces every upstream Hugging Face load to the locked dataset revision; it does not call the upstream
unified wrapper whose arguments have drifted. Recall concurrency and temperature can be overridden with
`--concurrency` and `--temperature`. Judge models can be fixed with `--similarity-model` and `--judge-model`. Do not
evaluate a 78-paper run until judge credentials and budget have been reviewed explicitly.

Set the credentials expected by the pinned upstream components in the environment:

```bash
export LITELLM_API_KEY=...
export LITELLM_BASE_URL=...
```

Secrets are not copied into manifests. The evaluation manifest records a one-way digest and host for the effective
judge endpoint so a resumed run cannot mix caches from different providers. Each mode has an immutable evaluation
manifest, manifest-verified isolated copies of the prepared preprints, separate upstream resume caches, raw
`recall.json` and `precision.json`, component logs, and a combined `summary.json`. If a complete component output is
invalid, that component's selected-paper cache files are removed so a later retry can make progress; ordinary
interruption caches remain available. The summary reports overall recall, precision, F1, axis and role breakdowns,
selection counts, reviewer cost, timing, and errors. Upstream judge cost is recorded as unavailable because the
pinned components do not expose it.

Before judging, the wrapper independently derives and freezes each selected paper's rubric-item count from the locked
upstream code and dataset revision. Only counts enter the evaluation manifest; this preflight does not copy rubric
text or reviewer or item identities into the BYOJ paper view, prepared manuscript workspace, or agent bundle.

The terminal-capable OpenHands precision evaluator never runs directly on the host. It runs in a read-only container
with all Linux capabilities dropped: the pinned upstream source and prepared papers are mounted read-only, while only
`precision.json`, the precision trajectory directory, and a dedicated evaluator cache are writable. No host directory
outside those explicit mounts, and no host environment, is passed through. The two LiteLLM settings are delivered to
the evaluator over one-shot standard input and injected directly into the locked evaluator's in-memory configuration;
they never enter the container or agent-terminal environment and are not written to component logs. Use a dedicated,
limited judge credential even with this isolation; the container still requires outbound network access to the judge
endpoint.

Missing papers, missing judge decisions, errored recall pairs, component failures, dataset drift, or any prepared-input
mutation make the evaluation `incomplete` rather than silently contributing a zero score.

## Local artifacts

All downloaded data, route configuration, Scriptorium state, review runs, and evaluation runs are ignored locally:

```text
egs/peerreviewbench/.cache/
egs/peerreviewbench/.scriptorium/
egs/peerreviewbench/runs/
egs/peerreviewbench/evaluations/
```

Do not compare scores without the corresponding review and evaluation manifests: reviewer and visual-transcription
code, routes, models, prompts, parser/schema digests, dataset revision, judge models, finding policy, cost, and latency
are part of the result contract. The manifest's Scriptorium cost includes any pre-review transcription call.
