# Solution

This document explains the implementation changes made for the Stage 3 growth work, why each change was chosen, and the trade-offs involved. The implementation follows the system design we agreed on: keep the backend simple, improve read performance, avoid unnecessary infrastructure, and make CSV ingestion efficient and resilient.

## 1. Optimization Approach

### A. Query Performance and Database Efficiency

The main query bottlenecks were repeated reads, growing table size, and filter-heavy profile queries. The implementation improves this in three simple ways:

- Added indexes for the profile fields most commonly used in filters and sorting:
  - `gender`
  - `age_group`
  - `country_id`
  - `age`
  - `gender_probability`
  - `country_probability`
  - `created_at`
- Added small in-process TTL caching for:
  - `GET /api/profiles`
  - `GET /api/profiles/search`
  - `GET /api/profiles/{id}`
  - `GET /api/dashboard`
- Added broad cache invalidation after profile writes and admin updates so cached reads stay correct.

Why this approach:

- Indexes reduce scan work for the exact fields the API already exposes.
- Caching reduces repeated database work for hot read paths.
- Broad invalidation is easier to reason about than fine-grained dependency tracking.

What was intentionally not added:

- no new database
- no distributed cache
- no background queue
- no horizontal scaling logic

That keeps the solution aligned with the constraints and easy to maintain.

### B. Query Normalization and Cache Efficiency

The main problem here was that different natural-language phrasings could produce different cache keys even when the user intent was the same.

The implementation solves this by:

- parsing the query into structured filters using the existing rule-based parser
- normalizing the parsed filter object into a canonical form
- generating cache keys from the canonical filter object instead of the raw query text

Normalization rules include:

- lowercase for `gender` and `age_group`
- uppercase for `country_id`
- numeric conversion for ages and probabilities
- fixed field ordering in the normalized filter object
- normalization of punctuation variants like en dash vs hyphen in age ranges

Example:

- `"Nigerian females between ages 20 and 45"`
- `"Women aged 20-45 living in Nigeria"`

Both now normalize to the same filter shape and therefore the same cache key.

Why this approach:

- deterministic
- easy to test
- does not guess beyond the parser’s explicit rules
- prevents avoidable cache misses

### C. Large-Scale CSV Data Ingestion

The ingestion work was designed around the hard constraints:

- do not insert row by row
- do not load the whole file into memory
- bad rows must not fail the full upload
- uploads must coexist with normal read traffic

The implementation uses:

- streaming line-by-line CSV reading through a text wrapper
- chunked processing with a configurable chunk size
- validation per row
- batch inserts per chunk
- conflict-safe insert behavior to enforce duplicate-name idempotency
- partial success semantics, where successful rows remain even if later rows fail

The upload endpoint returns a final summary with:

- `total_rows`
- `inserted`
- `skipped`
- skip reasons grouped by category

Why this approach:

- chunking avoids large memory spikes
- batch inserts are much cheaper than one insert per row
- per-row validation keeps one bad row from failing the whole upload
- yielding between chunks helps reduce pressure on read traffic

## 2. Design Decisions and Trade-offs

| Decision | Reason | Trade-off |
|---|---|---|
| Use in-process TTL cache | Simplest way to reduce repeated DB reads under the “no horizontal scaling” constraint | Cache is local to one process and would not be shared if deployment topology changed later |
| Use broad cache invalidation on writes | Keeps correctness simple and predictable | Invalidates more cache entries than strictly necessary |
| Add only read-heavy indexes | Targets the current latency issue directly without redesigning storage | Slightly slower inserts and deletes because indexes must be maintained |
| Keep the existing API shape for query endpoints | Avoids breaking clients and keeps the rollout low risk | Some internal improvements must fit around the existing API contract |
| Use canonical normalized filters for cache keys | Prevents duplicate cache entries for equivalent queries | Only works as well as the deterministic parser rules |
| Add a dedicated CSV upload endpoint | Separates bulk ingestion from single-profile creation | Adds one new admin-only API surface to maintain |
| Batch insert by chunk with conflict-safe behavior | Much faster than row-by-row inserts and safe for concurrent uploads | Duplicate reporting may include race-based duplicates during concurrent uploads, which is acceptable and expected |
| Keep partial progress on upload failure | Matches the requirement that completed inserts must remain | Uploads are not all-or-nothing, so operators must rely on the summary to understand partial success |

## 3. Before/After Query Performance

The table below uses local validation measurements on a development setup with a seeded SQLite database of about 5,000 profiles. “Before” here means a cold request with cache cleared. “After” means repeated warm requests against the new implementation, where the cache is active.

These numbers are not production benchmarks, but they are enough to show the effect of the optimizations.

| Endpoint | Before: cold request | After: repeated warm request avg | Improvement |
|---|---:|---:|---:|
| `GET /api/profiles?gender=female&country_id=NG&sort_by=age` | 30.86 ms | 8.08 ms | 73.8% faster |
| `GET /api/profiles/search?q=Nigerian females between ages 20 and 45` | 23.40 ms | 7.96 ms | 66.0% faster |
| `GET /api/dashboard` | 18.32 ms | 6.06 ms | 66.9% faster |

What changed between the two states:

- the query uses indexes instead of relying only on broader scans
- repeated reads avoid repeated database work
- equivalent search phrasings reuse the same cached result instead of creating duplicate cache entries

## 4. Ingestion Failures and Edge Cases

The CSV upload path is intentionally tolerant. A bad row is skipped, counted, and reported, but it does not fail the full upload.

### Validation Rules

Rows are skipped when:

- required fields are missing
- age is invalid or negative
- gender is unrecognized
- probabilities are invalid
- `country_id` is unknown
- sample size is invalid
- the row is malformed
- the name already exists in the database
- the same name appears more than once in the same upload chunk

### Failure Handling

- malformed rows are counted as `malformed_row`
- encoding replacement characters are treated as malformed input
- duplicates already in the database are counted as `duplicate_name`
- duplicates caused by concurrent inserts are also counted as `duplicate_name`
- inserted rows stay inserted even if a later chunk has failures
- the upload summary is returned even when many rows are skipped

### Concurrency and Read Protection

To avoid uploads overwhelming normal reads:

- uploads are processed in chunks instead of as one large in-memory operation
- inserts happen in batches, which shortens database write time
- the upload loop yields between chunks
- concurrent upload slots are capped with a small semaphore

This is intentionally simple. It does not eliminate all contention, but it reduces the chance that a large upload monopolizes the application.

## 5. Summary

The implementation focused on practical improvements rather than new complexity:

- faster reads through indexing and caching
- better cache efficiency through deterministic query normalization
- scalable CSV ingestion through streaming, chunking, and batch insert behavior

The main trade-off is that the solution prefers simplicity over maximum sophistication:

- local in-process cache instead of shared cache infrastructure
- broad invalidation instead of precise cache dependency tracking
- partial-success upload semantics instead of full transaction rollback

Those trade-offs were intentional because they fit the project constraints and are realistic to implement and maintain in the current system.
