# Mandate Doctor — Production Edge Cases Gitbook

## Table of Contents

* [Introduction](chapters/01-introduction.md)
  * [System Summary](chapters/01-introduction.md#system-summary)
  * [Architecture at a Glance](chapters/01-introduction.md#architecture-at-a-glance)
  * [Codebase Map](chapters/01-introduction.md#codebase-map)
  * [How to Read This Book](chapters/01-introduction.md#how-to-read-this-book)

## Part I: Core System

* [Chapter 2: Core Layer](chapters/02-core-layer.md)
  * [models.py — Domain Models](chapters/02-core-layer.md#modelspy--domain-models)
    * [EDGE-001: datetime.now uses local time](chapters/02-core-layer.md#edge-001-datetimenow-uses-local-time-not-utc)
    * [EDGE-002: cycle_id defaults to "cycle_default"](chapters/02-core-layer.md#edge-002-cycle_id-defaults-to-cycle_default--cross-cycle-retry-leak)
    * [EDGE-003: Confidence field unused downstream](chapters/02-core-layer.md#edge-003-confidence-field-is-unused-downstream)
    * [EDGE-004: is_synthetic flag never set](chapters/02-core-layer.md#edge-004-is_synthetic-flag-is-never-set-to-true)
  * [classifier.py — Error Classifier](chapters/02-core-layer.md#classifierpy--error-classifier)
    * [EDGE-005: No error detail → AMBIGUOUS](chapters/02-core-layer.md#edge-005-no-error-detail--ambiguous-with-hardcoded-05-confidence)
    * [EDGE-006: Pattern matching false positives](chapters/02-core-layer.md#edge-006-pattern-matching-uses-substring--false-positives)
    * [EDGE-007: LLM layer never implemented](chapters/02-core-layer.md#edge-007-llm-layer-step-3-is-documented-but-never-implemented)
    * [EDGE-008: SCORER_WEIGHTS defined but unused](chapters/02-core-layer.md#edge-008-scorer_weights-and-thresholds-are-defined-but-never-used)
  * [codes.py — Error Code Lookup](chapters/02-core-layer.md#codespy--error-code-lookup)
    * [EDGE-009: 62 codes mapped — new codes unhandled](chapters/02-core-layer.md#edge-009-62-error-codes-mapped--new-codes-are-unhandled)
    * [EDGE-010: "payment_declined" maps to AMBIGUOUS](chapters/02-core-layer.md#edge-010-payment_declined-maps-to-ambiguous--most-common-error-is-unclassified)
    * [EDGE-011: Fixed confidence per bucket](chapters/02-core-layer.md#edge-011-confidence-scores-are-fixed-per-bucket--no-context-sensitivity)
    * [EDGE-012: No separator normalization](chapters/02-core-layer.md#edge-012-case-insensitive-matching--no-normalization-of-separators)
  * [policy.py — Retry Budget & Decisions](chapters/02-core-layer.md#policypy--retry-budget--decisions)
    * [EDGE-013: RetryBudget in-memory — lost on restart](chapters/02-core-layer.md#edge-013-retrybudget-is-in-memory--lost-on-server-restart)
    * [EDGE-014: Global singleton — no per-request isolation](chapters/02-core-layer.md#edge-014-global-singleton--no-per-request-isolation)
    * [EDGE-015: No automatic cycle reset](chapters/02-core-layer.md#edge-015-no-automatic-cycle-reset)
    * [EDGE-016: No fallback in bucket-to-action](chapters/02-core-layer.md#edge-016-bucket-to-action-mapping-has-no-fallback)
  * [idempotency.py — At-Most-Once Layer](chapters/02-core-layer.md#idempotencyepy--at-most-once-layer)
    * [EDGE-017: check_same_thread=False](chapters/02-core-layer.md#edge-017-check_same_threadfalse--unsafe-for-multi-threaded-access)
    * [EDGE-018: sweep_stale uses wall clock](chapters/02-core-layer.md#edge-018-sweep_stale-uses-wall-clock--ntp-jumps-cause-false-escalations)
    * [EDGE-019: No cleanup of old records](chapters/02-core-layer.md#edge-019-no-cleanup-of-old-records)
    * [EDGE-020: stats deduped count is misleading](chapters/02-core-layer.md#edge-020-stats-counts-are-misleading)

## Part II: API & Services

* [Chapter 3: API Layer](chapters/03-api-layer.md)
  * [app.py — Webhook Receiver & Batch Control](chapters/03-api-layer.md#apppy--webhook-receiver--batch-control)
    * [EDGE-021: bounce_evidence in-memory](chapters/03-api-layer.md#edge-021-bounce_evidence-dict-is-in-memory--lost-on-restart)
    * [EDGE-022: received_events unbounded](chapters/03-api-layer.md#edge-022-received_events-list-grows-unbounded)
    * [EDGE-023: HMAC malformed signature](chapters/03-api-layer.md#edge-023-hmac-verification-doesnt-handle-malformed-signature)
    * [EDGE-024: CORS hardcoded](chapters/03-api-layer.md#edge-024-cors-whitelist-is-hardcoded)
    * [EDGE-025: Single batch at a time](chapters/03-api-layer.md#edge-025-only-one-batch-can-run-at-a-time)
    * [EDGE-026: _batch_stop not cleared](chapters/03-api-layer.md#edge-026-_batch_stop-event-not-cleared-between-batches)
    * [EDGE-027: Periodic trainer warm-up](chapters/03-api-layer.md#edge-027-periodic-trainer-starts-after-90s)
    * [EDGE-028: WebSocket no backpressure](chapters/03-api-layer.md#edge-028-websocket-clients-dont-get-backpressure-signal)
    * [EDGE-029: No SQLite connection pooling](chapters/03-api-layer.md#edge-029-no-sqlite-connection-pooling)
    * [EDGE-030: Training failure not surfaced](chapters/03-api-layer.md#edge-030-post-batch-training-failure-not-surfaced)
  * [events.py — WebSocket Event Bus](chapters/03-api-layer.md#eventspy--websocket-event-bus)
    * [EDGE-031: EventBus in-memory](chapters/03-api-layer.md#edge-031-eventbus-is-in-memory--all-events-lost-on-restart)
    * [EDGE-032: Queue overflow silent drop](chapters/03-api-layer.md#edge-032-queue-overflow-silently-drops-events)
    * [EDGE-033: No WebSocket auth](chapters/03-api-layer.md#edge-033-no-authentication-on-websocket-connections)
    * [EDGE-034: subscriber_count race](chapters/03-api-layer.md#edge-034-subscriber_count-reads-without-lock)

* [Chapter 4: Services Layer](chapters/04-services-layer.md)
  * [razorpay.py — API Client](chapters/04-services-layer.md#razorpaypy--api-client)
    * [EDGE-035: No connection pooling](chapters/04-services-layer.md#edge-035-new-http-client-per-request--no-connection-pooling)
    * [EDGE-036: Backoff caps at ~32.5s](chapters/04-services-layer.md#edge-036-exponential-backoff-caps-at-325s)
    * [EDGE-037: _auth() recomputes](chapters/04-services-layer.md#edge-037-_auth-recomputes-on-every-call)
    * [EDGE-038: No attempt count in errors](chapters/04-services-layer.md#edge-038-non-retryable-errors-raise-without-attempt-count)
    * [EDGE-039: Fragile throttle detection](chapters/04-services-layer.md#edge-039-too-many-request-substring-match--fragile-throttle-detection)
    * [EDGE-040: Hardcoded customer details](chapters/04-services-layer.md#edge-040-hardcoded-customer-details)
    * [EDGE-041: Dead code path](chapters/04-services-layer.md#edge-041-dead-code-path)
    * [EDGE-042: PII in error str](chapters/04-services-layer.md#edge-042-razorpayerror-str-embeds-description)
    * [EDGE-043: No total timeout](chapters/04-services-layer.md#edge-043-no-total-timeout-on-retry-loop)

## Part III: Evaluation & ML

* [Chapter 5: Evaluation Layer](chapters/05-evaluation-layer.md)
  * [data_collector.py — Outcome Collector](chapters/05-evaluation-layer.md#data_collectorpy--outcome-collector)
    * [EDGE-044: Bank weights loaded once](chapters/05-evaluation-layer.md#edge-044-bank-weights-loaded-once-at-startup)
    * [EDGE-045: pays variable reused](chapters/05-evaluation-layer.md#edge-045-pays-variable-reused-and-overwritten)
    * [EDGE-046: Fixed 150s cooldown](chapters/05-evaluation-layer.md#edge-046-throttle-cooldown-is-fixed-150s)
    * [EDGE-047: Circuit breaker kills batch](chapters/05-evaluation-layer.md#edge-047-circuit-breaker-stops-entire-batch)
    * [EDGE-048: Shared SQLite across workers](chapters/05-evaluation-layer.md#edge-048-sqlite-connection-shared-across-concurrent-workers)
    * [EDGE-049: INSERT OR REPLACE overwrites](chapters/05-evaluation-layer.md#edge-049-insert-or-replace-overwrites-existing-rows)
    * [EDGE-050: No checkout retry](chapters/05-evaluation-layer.md#edge-050-checkout_timeout-errors-are-not-retried)
    * [EDGE-051: _poll_link returns None](chapters/05-evaluation-layer.md#edge-051-_poll_link-returns-none-on-timeout)
    * [EDGE-052: Design version filter](chapters/05-evaluation-layer.md#edge-052-design-version-filter-excludes-v1-rows)
  * [checkout_bot.py — Playwright Automation](chapters/05-evaluation-layer.md#checkout_botpy--playwright-automation)
    * [EDGE-053: Bank mapping hardcoded](chapters/05-evaluation-layer.md#edge-053-bank-name-mapping-hardcoded)
    * [EDGE-054: Unknown banks fallback](chapters/05-evaluation-layer.md#edge-054-unknown-banks-fallback-to-bank-of-baroda)
    * [EDGE-055: Fixed mobile number](chapters/05-evaluation-layer.md#edge-055-fixed-mobile-number)
    * [EDGE-056: Chrome UA hardcoded](chapters/05-evaluation-layer.md#edge-056-chrome-ua-hardcoded-to-version-151)
    * [EDGE-057: Same timeout for all steps](chapters/05-evaluation-layer.md#edge-057-bank_page_timeout-used-for-all-steps)
  * [train_model.py — ML Training](chapters/05-evaluation-layer.md#train_modelpy--ml-training)
    * [EDGE-058: Minimum 20 rows](chapters/05-evaluation-layer.md#edge-058-minimum-20-rows-required)
    * [EDGE-059: Non-atomic model write](chapters/05-evaluation-layer.md#edge-059-model-saved-directly--no-atomic-write)
    * [EDGE-060: No overfitting detection](chapters/05-evaluation-layer.md#edge-060-no-overfitting-detection)
    * [EDGE-061: Hardcoded hyperparameters](chapters/05-evaluation-layer.md#edge-061-hyperparameters-are-hardcoded)
    * [EDGE-062: File-based tracking](chapters/05-evaluation-layer.md#edge-062-file-based-last_rows-tracking)
    * [EDGE-063: Encoding rebuilt every run](chapters/05-evaluation-layer.md#edge-063-feature-encoding-rebuilt-every-run)

## Part IV: Operations

* [Chapter 6: Deployment & Infrastructure](chapters/06-deployment.md)
  * [config.py — Environment Config](chapters/06-deployment.md#configpy--environment-config)
    * [EDGE-064: No startup validation](chapters/06-deployment.md#edge-064-empty-api-keys--no-startup-validation)
    * [EDGE-065: Extra env vars ignored](chapters/06-deployment.md#edge-065-extra-env-vars-silently-ignored)
    * [EDGE-066: auto_recover silent no-op](chapters/06-deployment.md#edge-066-auto_recover-defaults-to-false--silent-no-op)
    * [EDGE-067: Unused database_url](chapters/06-deployment.md#edge-067-database_url-is-unused)
  * [scripts/serve.sh — Process Management](chapters/06-deployment.md#scriptsservesh--process-management)
    * [EDGE-068: No log rotation](chapters/06-deployment.md#edge-068-setsid-nohup--no-log-rotation)
    * [EDGE-069: No health check](chapters/06-deployment.md#edge-069-no-process-health-check-after-startup)
    * [EDGE-070: ngrok domain expires](chapters/06-deployment.md#edge-070-ngrok-static-domain-may-expire)
    * [EDGE-071: No graceful shutdown](chapters/06-deployment.md#edge-071-no-graceful-shutdown)
  * [Cross-Cutting Concerns](chapters/06-deployment.md#cross-cutting-deployment-edge-cases)
    * [EDGE-072: mypy strict blind spots](chapters/06-deployment.md#edge-072-mypy-strict-misses-runtime-errors)
    * [EDGE-073: Test lint ignores](chapters/06-deployment.md#edge-073-ruff-per-file-ignores-for-tests)
    * [EDGE-074: Single-process architecture](chapters/06-deployment.md#edge-074-single-process--no-horizontal-scaling)
    * [EDGE-075: No HTTPS termination](chapters/06-deployment.md#edge-075-no-https-termination)
    * [EDGE-076: SQLite not production-ready](chapters/06-deployment.md#edge-076-sqlite-not-suitable-for-production-concurrency)
    * [EDGE-077: No external logging](chapters/06-deployment.md#edge-077-no-structured-logging-to-external-system)
    * [EDGE-078: No API rate limiting](chapters/06-deployment.md#edge-078-no-rate-limiting-on-api-endpoints)
    * [EDGE-079: No API authentication](chapters/06-deployment.md#edge-079-no-authentication-on-any-endpoint)

---

## Summary: 79 Edge Cases by Severity

| Severity | Count | Categories |
|----------|-------|------------|
| **CRITICAL** | 1 | In-memory state loss (RetryBudget) |
| **HIGH** | 14 | Data loss, silent failures, concurrency, ML correctness |
| **MEDIUM** | 32 | Operational friction, security, accuracy, resource leaks |
| **LOW** | 32 | Code quality, dead code, minor performance |

## Priority Action List

1. **EDGE-013** (CRITICAL): Persist RetryBudget to SQLite
2. **EDGE-064** (HIGH): Validate API keys at startup
3. **EDGE-076** (HIGH): Migrate to PostgreSQL for production
4. **EDGE-048** (HIGH): Serialize SQLite writes in collector
5. **EDGE-007** (HIGH): Implement or remove LLM classifier layer
6. **EDGE-010** (HIGH): Improve "payment_declined" handling
7. **EDGE-021** (HIGH): Persist bounce_evidence to disk
8. **EDGE-053** (HIGH): Validate bank mapping periodically
