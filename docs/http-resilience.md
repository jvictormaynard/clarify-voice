# Provider HTTP reliability and diagnostics

`provider_http.py` is the common transport policy for provider adapters. It
owns the HTTP session, connect/read timeouts, retry decisions, typed failures,
cooperative cancellation, redacted rotating logs, and diagnostic exports.
Provider adapters own request payload construction and successful response
parsing; they must not add a second retry layer or log request/response bodies.

## Timeout and retry policy

Timeouts are `(connect, read)` seconds:

| Operation | Timeout |
| --- | --- |
| Model discovery | `(3.05, 12)` |
| Credential validation | `(3.05, 12)` |
| Audio transcription | `(5, 90)` |
| Text generation | `(5, 60)` |

Only GET, HEAD, and OPTIONS requests are retryable by default. They use at most
three attempts for connection failures and HTTP 429, 502, 503, or 504. The
client honors `Retry-After` in seconds or HTTP-date form. Otherwise, it uses
exponential backoff starting at 250 ms with up to 25% jitter. Every delay is
capped at four seconds.

Discovery and validation therefore have a documented worst-case client budget
of 53.15 seconds: three `(3.05 + 12)`-second attempts plus two capped
four-second waits. Cancellation can end retry waits earlier.

Transcription and generation POSTs use one attempt. Without an idempotency key,
repeating them could duplicate billed work even when the first response was
lost. Authentication errors, invalid models, malformed requests, and all other
permanent failures also fail immediately.

## Error contract

The transport maps failures to typed exceptions:

- `AuthenticationError`
- `RateLimitError` and `QuotaError`
- `ProviderTimeoutError`
- `ServiceUnavailableError`
- `InvalidModelError`
- `InvalidRequestError`
- `InvalidResponseError`
- `ProviderCancelledError`
- `NetworkError`

Localized messages tell users what action to take. Safe diagnostics include the
provider, operation, HTTP status, and a locally generated operation ID. Provider
response bodies and headers are treated as untrusted: they are never included
in an exception, log record, diagnostic export, or interface message.

Cancellation is cooperative. Adapters pass a `CancellationToken`; the client
checks it before a request, after the response, and around retry waits. A result
received after cancellation raises `ProviderCancelledError` instead of being
returned to the caller. The underlying synchronous request cannot be interrupted
mid-socket; cancellation becomes effective at the next check or configured
timeout, while the UI immediately detaches from that request and ignores its
late result.

## Local logs and export

Provider errors are written as JSON lines to a rotating `provider.log`. The
default rotation is 512 KiB with three backups. Log records contain only
transport metadata: timestamp, provider, operation, method, host, attempt,
status, local operation ID, error type, and selected retry delay.
The URL path/query, headers, request/response bodies, audio path, source text,
transcript, and rewritten text are not logged.

`export_diagnostics()` creates a JSON file only when the user requests it. The
export contains application/runtime versions, coarse platform metadata, and
recent already-redacted error records. It does not contain configuration,
environment variables, credentials, URLs, audio, or user text. The recursive
redaction pass is applied again while exporting as defense in depth.

Remote telemetry and automatic upload are intentionally unsupported.
