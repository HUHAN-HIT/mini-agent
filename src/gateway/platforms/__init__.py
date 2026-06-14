"""Platform adapter implementations.

Shared utilities live in ``_utils`` (TTL set, message dedup, rate-limit
circuit, text debouncer). Each adapter owns its protocol state and emits
normalized MessageEvents to the runner.
"""
