"""Exception types for sapfetch."""


class SapfetchError(Exception):
    """Base class for all sapfetch errors."""


class ConfigError(SapfetchError):
    """The configuration file is missing required fields or is malformed."""


class AuthError(SapfetchError):
    """No usable saved session, or the session has expired."""


class NavigationError(SapfetchError):
    """A report could not be located or opened in the portal."""


class PromptError(SapfetchError):
    """A prompt/variable value could not be entered."""


class ExportError(SapfetchError):
    """The report ran but no file could be exported/downloaded."""
