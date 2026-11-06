# Historical status retirement validation

Three alternating trials against `cf29ced` used isolated real PostgreSQL with
5,000 indexed PR/head associations and 100 eligible complete, clean status jobs.
Ninety-eight heads had been superseded on still-open PRs; two remained current.
Fixture upstreams made no GitHub requests. A current-head leader exercised the
existing bulk retirement path. This measures avoided background refresh work,
not newly hydrated missing histories or fleet throughput.

| Median | Before | Candidate |
| --- | ---: | ---: |
| Elapsed, ms | 38460 | 1661 |
| Writer admissions | 14 | 2 |
| Application driver round trips | 1364 | 43 |
| Upstream histories requested | 100 | 2 |
| Jobs remaining for current/periodic work | 100 | 2 |

All 100 stored complete collections remained available. Retired jobs are not
credited as completions. A dormant leader still retires individually through the
existing coordinator; this fixture does not claim bulk cost for every possible
leader order. Dirty/incomplete jobs still perform their normal reconciliation.
The change does not eliminate closed-PR file debt.

[Raw trials](validation/historical-status-dormancy-postgres.json) include source
fingerprint `0619202dfe4b1d65e1e7ceecfd7300d1ff239ea9bc8793129f20053340e4dbbd`, writer timings, SQL counts and metrics. The source
fingerprint covers every source TypeScript file, the catalog and the lockfile.
The script `scripts/benchmark-status-dormancy.mjs` recreates the fixture. Database
workloads must run serially with isolated test credentials.

Focused checks cover current/historical/shared/unknown and non-string heads,
escaped PR bodies, demand, dirty/incomplete/requested state, generation/lease
replacement, reopen/head-change recreation and native SQLite bulk retirement of
130 heads under its binding limit. Required final-head CI includes PostgreSQL,
service, package, publication memory and the unchanged 1,200-PR fixture. Production
observation is separate and must assess input lag and live missing-key progress.
