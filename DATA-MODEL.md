# Data model

The schema is implemented in [`app/schema.sql`](app/schema.sql). SQLite foreign keys are enabled on every connection. All event-owned rows carry `event_id`; route queries also filter by event, and protected resource reads additionally verify the specific team/assignment/track relationship.

```text
users ───< sessions
  │
  ├──< team_members >── teams >── events
  │                         │
  │                         └──< projects >── tracks >── events
  │                                  │
  └──< judge_invites                  └──< assignments >── users (judge)
                                              │
                                              └── reviews ───< review_scores >── rubric_criteria

prizes >── events (required)  and optionally >── tracks
normalization_runs >── events
```

## Entities

| Table | Purpose / key columns | Constraints that matter |
|---|---|---|
| `users` | Email, PBKDF2 password hash, display name, role (`participant`, `judge`, `organizer`, `admin`). Visitor is unauthenticated. | `UNIQUE(email)`; role CHECK. |
| `sessions` | Opaque bearer token, `user_id`, creation/expiry timestamps, user agent. | Token is primary key; foreign key to user; server-side expiry. |
| `events` | Event name/slug, UTC start, submission deadline, judging start/end, publication time and `results_published_run_id` (the normalization run the publication certified; NULL on legacy volumes). | Unique slug; UTC windows validated on create/edit and constrained in schema. Publication timestamp ≠ current published result: the derived publication state is recomputed on read. |
| `tracks` | Event-scoped name/slug/description/order. | `UNIQUE(event_id, slug)`. |
| `prizes` | Event-wide (`track_id NULL`) or track-scoped name/description/rank. | Minimal by design; not a scoring factor. |
| `teams` | Event-scoped team, lead, random invite token. | `UNIQUE(event_id, name)`; token unique. New invite tokens use `secrets.token_urlsafe(24)`; fixture demo tokens are deterministic data only. |
| `team_members` | Event, team, user, lead flag. | `UNIQUE(event_id, user_id)` enforces at most one team per user per event. |
| `projects` | Event, team, track, public fields, state `draft`/`submitted`, timestamps. | `UNIQUE(event_id, team_id)` enforces one project per team per event. Drafts are never public or judged. |
| `rubric_criteria` | Event, name, description, weight percent, maximum, order. | `UNIQUE(event_id, name)`; the service checks that weights sum to 100%. |
| `judge_invites` | Event, email, random token, optional track, issuer, accepted judge and time. | `UNIQUE(event_id, email)`. An accepted invite is required to participate in round-robin assignment for that event. |
| `assignments` | Event, submitted project, project track, judge. | `UNIQUE(project_id, judge_id)` prevents duplicate judge/project pairs. Each should have one review. |
| `reviews` | Event, project, judge, assignment, `pending`/`complete`, weighted raw total, private comment, timestamps. | `UNIQUE(project_id, judge_id)`. `pending` is not a zero score. |
| `review_scores` | Review, criterion, entered numeric score. | `UNIQUE(review_id, criterion_id)`. |
| `normalization_runs` | Event, generator, method, counts, rubric fingerprint, review-set fingerprint, JSON payload snapshot. | The **only** persisted Runbook history; statuses are derived on read. |

## Fixture import

The official `fixtures.json` contains 41 project entries for 40 teams, including a 41st entry for a team that already submitted. Since `UNIQUE(event_id, team_id)` is a product requirement, the loader preserves the first canonical project for that team and records the duplicate fixture ID and its skipped score entries in adapter metadata (`app/fixtures/official.py`). Distinct teams with identical display names get a stable suffix (their official fixture team ID) before insertion; the suffix changes only display text, not membership. The official 30 judge accounts, 40 legal projects, eight tracks and the non-duplicate official review scores are loaded; generated pending assignments exercise incomplete batches. A separate pilot event has its own `event_id`, tracks, prizes, teams, projects, rubric and assignments, so it cannot contaminate the official event's gallery or judging results.

## Privacy boundaries

Public gallery queries select only submitted-project, team and track fields. They do not join assignment, review, score, session, invite or normalization tables. A review endpoint checks the authenticated judge's own id or organizer capability before fetching criterion scores. An event-scoped organizer CSV serializes private values only after organizer/admin authorization. `event_id` is carried through every review/assignment/rubric row so that both query scoping and stale-result detection remain local to one event.
