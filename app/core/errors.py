"""Typed exceptions mapped to dashboard error types."""

from __future__ import annotations

from typing import Any

from app.core.enums import ErrorType


class PipelineError(Exception):
    error_type: ErrorType = ErrorType.INTERNAL_ERROR

    def __init__(self, message: str, *, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.message = message
        self.details = details or {}


class DiscoveryError(PipelineError):
    error_type = ErrorType.DISCOVERY_ERROR


class DownloadError(PipelineError):
    error_type = ErrorType.DOWNLOAD_ERROR


class ParserError(PipelineError):
    error_type = ErrorType.PARSER_ERROR


class EncodingError(PipelineError):
    error_type = ErrorType.ENCODING_ERROR


class SchemaChangedError(PipelineError):
    error_type = ErrorType.SCHEMA_CHANGED


class MissingConfigurationError(PipelineError):
    error_type = ErrorType.MISSING_CONFIGURATION


class SourceUnsupportedError(PipelineError):
    error_type = ErrorType.UNSUPPORTED_SOURCE
