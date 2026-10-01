"""Configuration errors: invalid configuration stops the process with exit code 2."""


class ConfigError(Exception):
    """Invalid configuration detected after settings loaded (exit code 2)."""
