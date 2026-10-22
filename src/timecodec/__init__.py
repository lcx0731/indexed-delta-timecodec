"""A dependency-free reference codec for sorted integer timestamps."""

from .codec import FormatError, QueryResult, TimestampIndex, encode

__all__ = ["FormatError", "QueryResult", "TimestampIndex", "encode"]
