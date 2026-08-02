import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock, patch

import requests

from provider_http import (
    AuthenticationError,
    BACKOFF_CAP_SECONDS,
    CancellationToken,
    InvalidModelError,
    InvalidRequestError,
    InvalidResponseError,
    NetworkError,
    ProviderCancelledError,
    ProviderHttpClient,
    ProviderTimeoutError,
    QuotaError,
    RateLimitError,
    SafeRotatingLogger,
    ServiceUnavailableError,
    TIMEOUTS,
    export_diagnostics,
    localized_error_message,
    redact_sensitive,
    _retry_after_seconds,
)
from version import __version__


class FakeResponse:
    def __init__(self, status_code=200, payload=None, *, text="", headers=None):
        self.status_code = status_code
        self._payload = {} if payload is None else payload
        self.text = text
        self.headers = headers or {}
        self.closed = False

    def json(self):
        if isinstance(self._payload, BaseException):
            raise self._payload
        return self._payload

    def close(self):
        self.closed = True


class ProviderHttpPolicyTests(unittest.TestCase):
    def make_client(self, *, get=None, post=None, sleeper=None, random_fn=None,
                    logger=None):
        session = Mock()
        session.get.side_effect = get
        session.post.side_effect = post
        client = ProviderHttpClient(
            session=session,
            sleeper=sleeper,
            random_fn=random_fn or (lambda: 0.0),
            logger=logger,
        )
        return client, session

    def test_operation_owns_explicit_connect_and_read_timeout(self):
        response = FakeResponse()
        client, session = self.make_client(get=[response])

        self.assertIs(client.request(
            "GET", "https://api.example/models", provider="openai",
            operation="model_discovery"), response)

        self.assertEqual(
            session.get.call_args.kwargs["timeout"],
            TIMEOUTS["model_discovery"],
        )

    def test_callers_cannot_override_the_common_timeout(self):
        client, session = self.make_client(get=[FakeResponse()])

        with self.assertRaisesRegex(ValueError, "timeouts are owned"):
            client.request(
                "GET", "https://api.example/models", provider="openai",
                operation="validation", timeout=999)

        session.get.assert_not_called()

    def test_safe_get_retries_transient_connection_failures_with_capped_backoff(self):
        sleeps = []
        client, session = self.make_client(
            get=[requests.ConnectionError(), requests.ConnectionError(), FakeResponse()],
            sleeper=sleeps.append,
        )

        response = client.request(
            "GET", "https://api.example/models", provider="openai",
            operation="model_discovery")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(session.get.call_count, 3)
        self.assertEqual(sleeps, [0.25, 0.5])

    def test_retry_after_is_respected_and_capped(self):
        sleeps = []
        first = FakeResponse(429, headers={"Retry-After": "30"})
        client, session = self.make_client(
            get=[first, FakeResponse()], sleeper=sleeps.append)

        client.request(
            "GET", "https://api.example/models", provider="groq",
            operation="validation")

        self.assertEqual(session.get.call_count, 2)
        self.assertEqual(sleeps, [BACKOFF_CAP_SECONDS])
        self.assertTrue(first.closed)

    def test_retry_after_http_date_is_supported(self):
        now = datetime(2026, 8, 2, 12, 0, tzinfo=timezone.utc)

        self.assertEqual(
            _retry_after_seconds(
                "Sun, 02 Aug 2026 12:00:03 GMT", now=now),
            3.0,
        )

    def test_backoff_uses_bounded_jitter_without_retry_after(self):
        client, _session = self.make_client(random_fn=lambda: 1.0)

        self.assertEqual(client._backoff(1), 0.3125)
        self.assertEqual(client._backoff(10), BACKOFF_CAP_SECONDS)

    def test_permanent_authentication_failure_fails_immediately(self):
        client, session = self.make_client(
            get=[FakeResponse(401, {"error": {"message": "bad key"}})])

        with self.assertRaises(AuthenticationError):
            client.request(
                "GET", "https://api.example/models", provider="openai",
                operation="validation")

        self.assertEqual(session.get.call_count, 1)

    def test_exhausted_quota_is_not_retried_even_when_reported_as_429(self):
        client, session = self.make_client(get=[
            FakeResponse(429, {"error": {"code": "insufficient_quota"}}),
            FakeResponse(200),
        ])

        with self.assertRaises(QuotaError):
            client.request(
                "GET", "https://api.example/models", provider="openai",
                operation="validation")

        self.assertEqual(session.get.call_count, 1)

    def test_transient_resource_exhausted_429_is_retried_for_safe_operations(self):
        for operation in ("validation", "model_discovery"):
            with self.subTest(operation=operation):
                sleeps = []
                first = FakeResponse(
                    429, {"error": {"status": "RESOURCE_EXHAUSTED"}},
                    headers={"Retry-After": "2"})
                client, session = self.make_client(
                    get=[first, FakeResponse(200)], sleeper=sleeps.append)

                response = client.request(
                    "GET", "https://api.example/models", provider="gemini",
                    operation=operation)

                self.assertEqual(response.status_code, 200)
                self.assertEqual(session.get.call_count, 2)
                self.assertEqual(sleeps, [2.0])
                self.assertTrue(first.closed)

    def test_resource_exhausted_429_is_not_retried_for_unsafe_post(self):
        first = FakeResponse(
            429, {"error": {"status": "RESOURCE_EXHAUSTED"}})
        client, session = self.make_client(
            post=[first, FakeResponse(200)])

        with self.assertRaises(RateLimitError):
            client.request(
                "POST", "https://api.example/generate", provider="gemini",
                operation="text_generation")

        self.assertEqual(session.post.call_count, 1)
        self.assertTrue(first.closed)

    def test_generic_quota_text_is_rate_limit_not_permanent_quota(self):
        first = FakeResponse(
            429, {"error": {"message": "quota temporarily exceeded; retry later"}})
        client, session = self.make_client(
            get=[first, FakeResponse(200)], sleeper=lambda _delay: None)

        response = client.request(
            "GET", "https://api.example/models", provider="gemini",
            operation="validation")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(session.get.call_count, 2)

    def test_billing_or_credit_hard_limit_is_permanent_quota(self):
        cases = (
            (
                {"error": {"message": "billing hard limit reached"}},
                "billing hard limit reached",
            ),
            (
                {"error": {"message": "credit hard limit reached"}},
                "credit hard limit reached",
            ),
        )
        for payload, text in cases:
            with self.subTest(payload=payload):
                client, session = self.make_client(
                    get=[FakeResponse(429, payload, text=text), FakeResponse(200)])

                with self.assertRaises(QuotaError):
                    client.request(
                        "GET", "https://api.example/models", provider="openai",
                        operation="validation")

                self.assertEqual(session.get.call_count, 1)

    def test_unsafe_post_is_never_retried_for_transient_http_failure(self):
        client, session = self.make_client(
            post=[FakeResponse(503), FakeResponse(200)])

        with self.assertRaises(ServiceUnavailableError):
            client.request(
                "POST", "https://api.example/generate", provider="gemini",
                operation="text_generation", json={"contents": ["private"]})

        self.assertEqual(session.post.call_count, 1)

    def test_caller_cannot_force_retry_for_an_unsafe_method(self):
        client, session = self.make_client(
            post=[FakeResponse(503), FakeResponse(200)])

        with self.assertRaises(ServiceUnavailableError):
            client.request(
                "POST", "https://api.example/generate", provider="gemini",
                operation="text_generation", safe_to_retry=True)

        self.assertEqual(session.post.call_count, 1)

    def test_unsafe_post_is_never_retried_for_connection_failure(self):
        client, session = self.make_client(
            post=[requests.ConnectionError(), FakeResponse(200)])

        with self.assertRaises(NetworkError):
            client.request(
                "POST", "https://api.example/transcribe", provider="groq",
                operation="transcription", files={"file": object()})

        self.assertEqual(session.post.call_count, 1)

    def test_permanent_request_exception_is_not_retried(self):
        client, session = self.make_client(get=[
            requests.exceptions.InvalidURL("invalid endpoint"),
            FakeResponse(200),
        ])

        with self.assertRaises(NetworkError):
            client.request(
                "GET", "invalid-endpoint", provider="openai",
                operation="validation")

        self.assertEqual(session.get.call_count, 1)

    def test_http_failures_are_classified_without_exposing_response_text(self):
        cases = (
            (FakeResponse(429, {"error": {"code": "rate_limit"}}), RateLimitError),
            (FakeResponse(429, {"error": {"code": "insufficient_quota"}}), QuotaError),
            (FakeResponse(404, {"error": {"code": "model_not_found"}}), InvalidModelError),
            (FakeResponse(422, {"error": {"message": "bad"}}), InvalidRequestError),
            (FakeResponse(504), ServiceUnavailableError),
        )
        for response, expected in cases:
            with self.subTest(expected=expected.__name__):
                client, _session = self.make_client(post=[response])
                with self.assertRaises(expected):
                    client.request(
                        "POST", "https://api.example/generate",
                        provider="openai", operation="text_generation")

    def test_model_list_route_404_is_not_an_invalid_model(self):
        for operation in ("validation", "model_discovery"):
            with self.subTest(operation=operation):
                response = FakeResponse(
                    404,
                    {"error": {
                        "code": "model_not_found",
                        "status": "NOT_FOUND",
                        "message": "model endpoint not found",
                    }},
                    text="model endpoint not found",
                )
                client, session = self.make_client(get=[response])

                with self.assertRaises(InvalidRequestError):
                    client.request(
                        "GET", "https://custom.example/provider/models",
                        provider="openai", operation=operation)

                self.assertEqual(session.get.call_count, 1)

    def test_model_request_404_is_invalid_model_only_for_specific_signals(self):
        cases = (
            ("transcription", {"code": "model_not_found"}, ""),
            ("text_generation", {"code": "invalid_model"}, ""),
            ("text_generation", {}, "model gpt-test not found"),
            (
                "text_generation",
                {
                    "code": 404,
                    "status": "NOT_FOUND",
                    "message": (
                        "models/gemini-2.5-flash is not found for API version v1beta"
                    ),
                },
                "models/gemini-2.5-flash is not found for API version v1beta",
            ),
        )
        for operation, error, text in cases:
            with self.subTest(operation=operation, error=error, text=text):
                response = FakeResponse(
                    404, {"error": error}, text=text)
                client, session = self.make_client(post=[response])

                with self.assertRaises(InvalidModelError):
                    client.request(
                        "POST", "https://api.example/model",
                        provider="openai", operation=operation)

                self.assertEqual(session.post.call_count, 1)

    def test_timeouts_and_other_request_failures_have_distinct_types(self):
        timeout_client, _session = self.make_client(post=[requests.ReadTimeout()])
        with self.assertRaises(ProviderTimeoutError):
            timeout_client.request(
                "POST", "https://api.example/generate",
                provider="openai", operation="text_generation")

        network_client, _session = self.make_client(
            post=[requests.exceptions.ProxyError()])
        with self.assertRaises(NetworkError):
            network_client.request(
                "POST", "https://api.example/generate",
                provider="openai", operation="text_generation")

    def test_logging_failure_before_retry_preserves_attempts(self):
        logger = Mock()
        logger.write.side_effect = OSError("disk full")
        sleeps = []
        client, session = self.make_client(
            get=[FakeResponse(503), FakeResponse(200)],
            logger=logger,
            sleeper=sleeps.append,
        )

        response = client.request(
            "GET", "https://api.example/models", provider="openai",
            operation="validation")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(session.get.call_count, 2)
        self.assertEqual(sleeps, [0.25])

    def test_log_creation_failure_preserves_typed_error(self):
        with tempfile.TemporaryDirectory() as directory:
            logger = SafeRotatingLogger(Path(directory) / "logs")
            client, session = self.make_client(
                get=[FakeResponse(401)], logger=logger)

            with patch("provider_http.Path.mkdir",
                       side_effect=OSError("profile is read-only")):
                with self.assertRaises(AuthenticationError):
                    client.request(
                        "GET", "https://api.example/models",
                        provider="openai", operation="validation")

            self.assertEqual(session.get.call_count, 1)

    def test_logging_failure_preserves_typed_error(self):
        cases = (
            (FakeResponse(401), AuthenticationError),
            (requests.ReadTimeout(), ProviderTimeoutError),
            (requests.ConnectionError(), NetworkError),
        )
        for failure, expected_error in cases:
            with self.subTest(expected_error=expected_error.__name__):
                logger = Mock()
                logger.write.side_effect = OSError("read-only profile")
                method_responses = (
                    {"get": [failure]}
                    if isinstance(failure, FakeResponse)
                    else {"post": [failure]}
                )
                client, session = self.make_client(
                    logger=logger, **method_responses)

                with self.assertRaises(expected_error):
                    client.request(
                        "GET" if isinstance(failure, FakeResponse) else "POST",
                        "https://api.example/models",
                        provider="openai", operation="validation")

                sender = session.get if isinstance(failure, FakeResponse) else session.post
                self.assertEqual(sender.call_count, 1)

    def test_log_rotation_failure_preserves_retry_policy_and_typed_result(self):
        with tempfile.TemporaryDirectory() as directory:
            logger = SafeRotatingLogger(Path(directory), max_bytes=1)
            sleeps = []
            client, session = self.make_client(
                get=[FakeResponse(503), FakeResponse(503), FakeResponse(503)],
                logger=logger,
                sleeper=sleeps.append,
            )

            with patch("provider_http.RotatingFileHandler.doRollover",
                       side_effect=OSError("disk full")):
                with self.assertRaises(ServiceUnavailableError):
                    client.request(
                        "GET", "https://api.example/models",
                        provider="openai", operation="validation")

            logger.close()
            self.assertEqual(session.get.call_count, 3)
            self.assertEqual(sleeps, [0.25, 0.5])

    def test_invalid_json_has_a_typed_content_free_error(self):
        response = FakeResponse(payload=ValueError("private response body"))
        client, _session = self.make_client(get=[response])

        result = client.request(
            "GET", "https://api.example/models", provider="gemini",
            operation="model_discovery")
        with self.assertRaises(InvalidResponseError) as raised:
            client.json(result, provider="gemini", operation="model_discovery")

        self.assertNotIn("private response body", str(raised.exception))

    def test_invalid_response_preserves_operation_metadata_and_logs(self):
        response = FakeResponse(payload={"unexpected": "shape"})
        response._clarify_operation_id = "abc123"
        logger = Mock()
        client, _session = self.make_client(get=[response], logger=logger)

        error = client.invalid_response(
            response, provider="openai", operation="transcription")

        self.assertIsInstance(error, InvalidResponseError)
        self.assertEqual(error.operation_id, "abc123")
        self.assertEqual(error.status_code, 200)
        logger.write.assert_called_once_with({
            "event": "provider_http_error",
            "provider": "openai",
            "operation": "transcription",
            "operation_id": "abc123",
            "status_code": 200,
            "error_type": "invalid_response",
        })

    def test_cancellation_before_send_prevents_the_request(self):
        token = CancellationToken()
        token.cancel()
        client, session = self.make_client(get=[FakeResponse()])

        with self.assertRaises(ProviderCancelledError):
            client.request(
                "GET", "https://api.example/models", provider="openai",
                operation="validation", cancel_token=token)

        session.get.assert_not_called()

    def test_cancellation_after_response_prevents_a_late_result(self):
        token = CancellationToken()
        late_response = FakeResponse(200, {"data": ["late result"]})

        def return_after_cancel(*_args, **_kwargs):
            token.cancel()
            return late_response

        client, session = self.make_client(get=return_after_cancel)

        with self.assertRaises(ProviderCancelledError):
            client.request(
                "GET", "https://api.example/models", provider="openai",
                operation="validation", cancel_token=token)

        self.assertEqual(session.get.call_count, 1)
        self.assertTrue(late_response.closed)

    def test_cancellation_during_backoff_prevents_the_retry(self):
        token = CancellationToken()

        def cancel_in_sleep(_delay):
            token.cancel()

        client, session = self.make_client(
            get=[FakeResponse(503), FakeResponse(200)],
            sleeper=cancel_in_sleep)

        with self.assertRaises(ProviderCancelledError):
            client.request(
                "GET", "https://api.example/models", provider="openai",
                operation="validation", cancel_token=token)

        self.assertEqual(session.get.call_count, 1)

    def test_errors_have_localized_actionable_messages(self):
        error = InvalidModelError()

        self.assertIn("Choose another model", localized_error_message(error, "en"))
        self.assertIn("Escolha outro modelo", localized_error_message(error, "pt"))


class DiagnosticsTests(unittest.TestCase):
    def test_log_sink_creation_failure_is_best_effort(self):
        logger = SafeRotatingLogger(Path("/read-only/provider-logs"))

        with patch("provider_http.Path.mkdir",
                   side_effect=OSError("profile is read-only")):
            logger.write({"event": "provider_http_error"})

    def test_log_sink_write_failure_is_best_effort(self):
        logger = SafeRotatingLogger(Path("unused"))
        sink = Mock()
        sink.info.side_effect = OSError("disk full")

        with patch.object(logger, "_get_logger", return_value=sink):
            logger.write({"event": "provider_http_error"})

    def test_log_sink_rotation_failure_is_best_effort(self):
        with tempfile.TemporaryDirectory() as directory:
            logger = SafeRotatingLogger(Path(directory), max_bytes=1)
            logger.write({"event": "seed"})

            with patch("provider_http.RotatingFileHandler.doRollover",
                       side_effect=OSError("cannot rotate")):
                logger.write({"event": "provider_http_error"})

            logger.close()

    def test_recursive_redaction_removes_all_prohibited_content(self):
        sensitive = {
            "headers": {
                "Authorization": "Bearer top-secret",
                "x-goog-api-key": "gemini-secret",
            },
            "source_text": "private selected text",
            "transcript": "private transcript",
            "rewritten_text": "private rewrite",
            "audio_path": "C:\\Users\\me\\recording.wav",
            "message": "token=another-secret /tmp/private/audio.flac",
            "safe": "openai",
        }

        redacted = json.dumps(redact_sensitive(sensitive), sort_keys=True)

        for prohibited in (
                "top-secret", "gemini-secret", "private selected text",
                "private transcript", "private rewrite", "recording.wav",
                "another-secret", "audio.flac"):
            self.assertNotIn(prohibited, redacted)
        self.assertIn("openai", redacted)

    def test_rotating_log_contains_only_safe_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            logger = SafeRotatingLogger(
                Path(directory), max_bytes=180, backup_count=2)
            for index in range(12):
                logger.write({
                    "event": "provider_http_error",
                    "provider": "openai",
                    "attempt": index,
                    "Authorization": "Bearer secret-token",
                    "source_text": "never log me",
                    "audio_path": "/tmp/private.wav",
                })
            logger.close()

            paths = list(Path(directory).glob("provider.log*"))
            combined = "\n".join(
                path.read_text(encoding="utf-8") for path in paths)

        self.assertGreater(len(paths), 1)
        self.assertNotIn("secret-token", combined)
        self.assertNotIn("never log me", combined)
        self.assertNotIn("private.wav", combined)
        self.assertIn("[REDACTED]", combined)

    def test_http_logger_never_records_request_payload_or_url_path(self):
        with tempfile.TemporaryDirectory() as directory:
            logger = SafeRotatingLogger(Path(directory))
            client = ProviderHttpClient(
                session=Mock(), logger=logger, sleeper=lambda _delay: None)
            client.session.post.side_effect = [FakeResponse(
                401, {"error": {"message": "echoed private source"}},
                text="echoed private source",
            )]

            with self.assertRaises(AuthenticationError):
                client.request(
                    "POST", "https://api.example/v1/private/model:generate?key=secret",
                    provider="gemini", operation="text_generation",
                    headers={"Authorization": "Bearer secret"},
                    json={"source_text": "private source"})
            logger.close()
            contents = (Path(directory) / "provider.log").read_text(encoding="utf-8")

        self.assertIn('"host":"api.example"', contents)
        for prohibited in (
                "echoed private source", "private source", "model:generate",
                "key=secret", "Bearer secret"):
            self.assertNotIn(prohibited, contents)

    def test_user_initiated_export_contains_only_safe_metadata_and_errors(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            logger = SafeRotatingLogger(root / "logs")
            logger.write({
                "event": "provider_http_error",
                "provider": "groq",
                "error_type": "rate_limit",
                "api_key": "secret",
                "transcript": "private words",
            })
            logger.close()

            destination = export_diagnostics(
                root / "diagnostics.json", log_directory=root / "logs",
                application_version=__version__)
            payload = json.loads(destination.read_text(encoding="utf-8"))
            serialized = json.dumps(payload, sort_keys=True)

        self.assertEqual(payload["schema_version"], 1)
        self.assertEqual(payload["application"]["version"], __version__)
        self.assertIn("python_version", payload["environment"])
        self.assertEqual(payload["recent_errors"][0]["error_type"], "rate_limit")
        self.assertNotIn("secret", serialized)
        self.assertNotIn("private words", serialized)


if __name__ == "__main__":
    unittest.main()
