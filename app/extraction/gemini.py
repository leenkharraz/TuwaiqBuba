"""Live extraction through the official Google Gen AI SDK (``google-genai``)."""
from __future__ import annotations

import json
import logging
from typing import Any, Optional

from ..schemas import DOC_TYPES, DocumentExtraction
from ..storage import prepare_for_model
from .prompt import SYSTEM_INSTRUCTION, USER_PROMPT

log = logging.getLogger("claims.extraction")


class ExtractionError(Exception):
    """Raised with a short code that maps to an honest, actionable UI message."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _error_code(exc: Exception) -> str:
    try:
        from google.genai import errors as genai_errors
    except Exception:  # pragma: no cover
        genai_errors = None  # type: ignore
    try:
        import httpx

        if isinstance(exc, httpx.TimeoutException):
            return "timeout"
        if isinstance(exc, httpx.TransportError):
            return "network_error"
    except Exception:  # pragma: no cover
        pass
    if genai_errors is not None and isinstance(exc, genai_errors.APIError):
        code = getattr(exc, "code", None)
        message = str(getattr(exc, "message", "") or "").lower()
        if code in (401, 403) or "api key" in message or "permission" in message:
            return "auth_failed"
        if code == 404:
            return "model_not_found"
        if code == 429:
            return "quota_exceeded" if "quota" in message else "rate_limited"
        if code == 413:
            return "too_large"
        if code and int(code) >= 500:
            return "service_unavailable"
        return "request_rejected"
    return "unknown_error"


# Errors where trying the next configured model is worthwhile.
_FALLBACK_ON = {"service_unavailable", "rate_limited", "quota_exceeded", "model_not_found", "timeout"}


class GeminiExtractor:
    mode = "gemini"

    def __init__(self, api_key: str, model: str, timeout_s: int = 90, client: Any = None,
                 fallback_models: Optional[list] = None, retry_attempts: int = 3):
        self.api_key = api_key
        self.model = model
        self.models = [model] + [m for m in (fallback_models or []) if m and m != model]
        self.timeout_s = timeout_s
        self.retry_attempts = retry_attempts
        self._client = client
        self.used_model: Optional[str] = None

    def _get_client(self) -> Any:
        if self._client is None:
            from google import genai
            from google.genai import types

            http = types.HttpOptions(
                timeout=int(self.timeout_s * 1000),
                # Transient overload/rate-limit responses are retried with exponential backoff.
                retry_options=types.HttpRetryOptions(
                    attempts=self.retry_attempts, initial_delay=2.0, max_delay=12.0,
                    http_status_codes=[429, 500, 503, 504],
                ),
            )
            self._client = genai.Client(api_key=self.api_key, http_options=http)
        return self._client

    def extract(self, data: bytes, mime_type: str) -> DocumentExtraction:
        from google.genai import types

        payload, mime = prepare_for_model(data, mime_type)
        config = types.GenerateContentConfig(
            system_instruction=SYSTEM_INSTRUCTION,
            response_mime_type="application/json",
            response_schema=DocumentExtraction,
            temperature=0,
        )
        response = None
        last_code = "unknown_error"
        for model in self.models:
            try:
                response = self._get_client().models.generate_content(
                    model=model,
                    contents=[types.Part.from_bytes(data=payload, mime_type=mime), USER_PROMPT],
                    config=config,
                )
                self.used_model = model
                break
            except Exception as exc:  # map SDK/network errors to safe codes; never log content or keys
                last_code = _error_code(exc)
                log.warning("Gemini extraction failed on %s: %s (%s)", model, last_code, type(exc).__name__)
                if last_code not in _FALLBACK_ON:
                    break
        if response is None:
            raise ExtractionError(last_code)

        text: Optional[str]
        try:
            text = response.text
        except Exception:
            text = None
        if not text:
            feedback = getattr(response, "prompt_feedback", None)
            raise ExtractionError("blocked" if getattr(feedback, "block_reason", None) else "empty_response")
        return parse_model_output(text)


def parse_model_output(text: str) -> DocumentExtraction:
    """Validate the model's JSON. Anything that is not a well-formed extraction is rejected."""
    try:
        obj = json.loads(text)
    except (TypeError, ValueError):
        raise ExtractionError("invalid_response") from None
    if isinstance(obj, list) and len(obj) == 1:
        obj = obj[0]
    if not isinstance(obj, dict):
        raise ExtractionError("invalid_response")
    if obj.get("document_type") not in DOC_TYPES:
        raise ExtractionError("invalid_response")
    if obj.get("readability") not in ("readable", "partially_readable", "unreadable"):
        raise ExtractionError("invalid_response")
    try:
        return DocumentExtraction.model_validate(obj)
    except Exception:
        raise ExtractionError("invalid_response") from None
