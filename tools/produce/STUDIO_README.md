# tools/produce — studio-side components (EXECUTION PLANE)

The production control plane is NOT here. As of the Prompt 1 closeout (2026-09-13) the orchestrator,
canonical job state, window plan, editorial sender + credential, payload staging, response archive,
continuity and resume logic all live on the **ops host**:

    ops (.22):  /opt/production/          -> video-studio CLI, pipeline.py, jobspec.py, window_planner.py
    studio (.28): this directory          -> execution components only

What still belongs in this directory (the studio executes these when the ops orchestrator calls over ssh):

    coverage_governor.py    coverage package construction (renders matched A/B evidence)
    qc_proof.py             basis-driven QC of a rendered proof (decodes media)
    coverage_review_prompt.md

Everything else here (candidate generation, evidence building, signals, renderers, manifest validation)
lives in the sibling tool directories and is invoked by path.

`jobspec.py`, `window_planner.py` and `pipeline.py` copies in this directory are superseded by the ops
copies. Do not run the orchestrator from here: it cannot reach the sender or the credential, and the
studio has no ssh route back to ops by design. `video-studio` on the studio is only a leftover symlink
target — the operator CLI is `/usr/local/bin/video-studio` on ops.
