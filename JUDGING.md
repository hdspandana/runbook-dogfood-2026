# RUNBOOK judging, normalization and CSV — as implemented

This document describes **the actual code** in `app/judging/service.py`, `app/judging/normalization.py`, `app/exports/service.py`, and `app/runbook/service.py`. It is not a proposal.

## Scope and assignments

A judge's current identity always comes from the opaque, server-side session. The project review route requires an assignment for that exact `(project_id, judge_id)` pair. A judge cannot retrieve another judge's review by guessing an id or supplying `judge_id` in a query/body; they receive 403 with no score values. A participant also receives 403. The track-progress endpoint returns **only that judge's own assignment counts** in tracks they hold assignments in, with no peer scores or aggregate peer progress. The organizer/admin view has event-wide access.

Assignment is deterministic round-robin **within each track**. For a track, let submitted projects be sorted by database id, eligible invited/accepted judges sorted by user id, and `i` be the project's zero-based position within its track. For `k = 0 .. judges_per_project - 1`, assign `judges[(i + k) % judge_count]`. Existing `(project, judge)` pairs remain in place; repeats are blocked by `UNIQUE(project_id, judge_id)`. Projects with no eligible judge stay unassigned, and the Runbook reports the gap. A judge invited with a particular track is eligible only there; an invite with no track is all-track. Assignment rows carry `event_id` and `track_id` and are validated on protected reads.

**Completion policy:** a judging stage is complete only if every submitted project has **at least one** assignment, no assignment points at a mismatched track, and *every required assignment* has a completed review. This differs from merely counting completed reviews among the assignments that happened to be made: an unassigned submission must never silently look finished. Duplicate assignments are also detected (though the schema prohibits them).

## Rubric and raw totals

Each event has one common rubric across all its tracks. An event can have different maximums from a different event, but all projects *within that event* are compared on the same rubric. A rubric is valid when it has at least one criterion, every weight is nonnegative, every maximum is positive, and weights total **100%** (within floating-point tolerance 0.01). A complete review must supply a finite score in `[0, criterion.max_score]` for *every* criterion. A draft review is `pending` and can hold partial scores; it is never a zero-valued complete review. Judges can overwrite their own review while `judging_starts_at <= server_UTC_time < judging_ends_at`. At or after the end, writes are rejected.

The server computes a weighted raw total for a completed review:

```text
raw_total = Σ (criterion_score_i × criterion_weight_pct_i / 100)
maximum raw total = Σ (criterion_max_score_i × criterion_weight_pct_i / 100)
```

The official fixture's three criteria are functionality (40%), quality (35%) and innovation (25%), each scored from 0 to 5. The second, live pilot event's four criteria are Innovation (25%), Execution (30%), Impact (25%) and Presentation (20%), each scored from 0 to 10. The server rounds raw totals to four decimal places for reproducibility. It does **not** normalize each criterion independently.

## Cross-judge normalization

For one event, use *completed* reviews of its submitted projects only. Compute the weighted raw total from the **current criterion-score rows**. Group review totals by judge. For judge `j`:

```text
mean_j  = arithmetic_mean(raw_1, …, raw_n)
sigma_j = sqrt(Σ(raw_i − mean_j)² / n)    # population standard deviation
```

For a completed review of that judge, with raw total `raw`:

```text
z = (raw − mean_j) / sigma_j   if n >= 3 and sigma_j > 0
z = 0                          otherwise (neutral fallback)
```

The minimum stable sample size is **3 completed reviews**, configurable via `RUNBOOK_MIN_REVIEWS_FOR_NORM`. A judge with fewer completed reviews receives neutral `z = 0` for their reviews, not an estimate from a one- or two-item sample. When `sigma_j == 0`, all of that judge's totals are identical and supply no relative-variance signal, so all their `z` values are **0**. The mean/sigma displayed in JSON and CSV are rounded to 4 d.p., but z is computed from the *unrounded* mean/sigma and only then rounded to 4 d.p.

If no judge in an event has a meaningful z-score (all have `< 3` reviews or zero variance), RUNBOOK enters `raw-fallback` mode: the per-project **final score** is the arithmetic mean of its completed raw totals. In normal mode, the per-project final score is the arithmetic mean of its completed z-scores. The `normalized_score` field retains the mean z-score; `score` is the final score under the chosen mode. In CSV, `normalized_score` for an individual review equals its z-score in normal mode and its raw total in raw-fallback mode. The export always declares whether normalization is current, stale or not run.

Projects with no completed reviews remain **provisional/unranked**; pending reviews are never treated as zeros. Projects with one or more completed reviews are ranked, even if some of their assigned reviews remain pending. They are visible with their completed-review count, but that ranking is provisional until judging coverage is complete. There is no invented score for a pending review.

### Ranking and ties

Rank only projects with at least one completed review, ordering by:

1. final score, descending;
2. raw mean, descending;
3. project id, ascending.

Numbers are rounded to 4 d.p. for reported scores and deterministic tie-breaking. Ranks are sequential from 1; there is no shared-rank convention. The unranked-project list follows separately.

## Freshness and STALE

A normalization **run** persists a JSON result snapshot plus a SHA-256 fingerprint of the completed-review set, including each review's criterion scores, the submitted-project roster (id/team/track/name), and the rubric fingerprint (criterion id/name/weight/max/order). `normalization_runs` is the **only** persisted workflow-history table. The Runbook statuses are recomputed from current database facts on every read. If the current fingerprint matches the latest run, normalization is `COMPLETE`; if there is no run it is `UPCOMING`/`ACTIVE` depending on completed reviews; if the fingerprint differs it is `STALE` with the change in completed-review count (which can be zero if a score changed without changing the count). This is an *integrity signal*, not an authorization decision. It never blocks CSV export.

The official fixture is seeded with one snapshot deliberately computed before the latest six complete reviews, then those reviews are restored. The initial Runbook therefore genuinely reports STALE with a delta of six, not a fabricated dashboard flag. An organizer can re-run normalization; the new snapshot matches the current review set and the card becomes COMPLETE. Completing or editing a review afterward makes it STALE again. This is the demo's causal chain.

## CSV export

`GET /api/events/<event_id>/export.csv` and `/organizer/events/<event_id>/export.csv` are **organizer/admin-only**. Both routes are available at *all supported judging stages*: no review yet, mid-judging, after normalization, and after later review edits. They do **not** check Runbook status. A pending review gets its own `review_status=pending` row; unavailable criterion/score/statistics fields are empty, not zero. An unassigned submitted project gets an `unassigned` row. A complete review gets its current criterion scores, weighted raw total, current live judge mean/sigma/z, and current live per-project final score. `normalization_status` (`not_run`, `current`, or `stale`) and `normalization_generated_at` disclose the state and timestamp of the **last persisted run**, if any; live computed score values do not masquerade as a current persisted result when the snapshot is stale. The CSV is UTF-8, comma-delimited, and disables caching. Export does not publish scores to participants, visitors or judges.

## Limitations and fairness caveats

- Z-scores are *relative to a judge's own distribution*, not an absolute measure of project quality. Different assignment mixes can create selection bias. The simple round-robin algorithm does not optimise for bias or variance.
- A judge with fewer than three completed reviews contributes neutral z, so a project reviewed only by sparse judges may tie at zero (raw mean is the deterministic tiebreaker).
- Zero variance is not “poor” judging; it only means normalization extracts no relative signal from that judge.
- Projects with unfinished review slots may appear in a provisional ranking based only on completed slots. Publication is rejected until assignment/review coverage is complete and the persisted normalization is current.
- The common event rubric keeps all tracks on the same mathematical scale; the app does not offer per-track rubrics or cross-event comparisons.
- Once results are published, public gallery pages still withhold per-judge details; publication is recorded, but this implementation does not expose a public ranked leaderboard. Organizers may export the current ranking. This avoids accidental disclosure while the judging state is partial or stale.
- In CSV, computed live z/project scores may differ from the last stored result when `normalization_status=stale`; use the normalization timestamp/status to distinguish provisional computation from a published snapshot.
