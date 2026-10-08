class SkillsPapaError(Exception):
    """Expected operational error shown without a traceback."""


class ValidationError(SkillsPapaError):
    """A skill, manifest, or configuration is invalid."""


class ConflictError(SkillsPapaError):
    """The requested change conflicts with unmanaged or divergent state."""


class StateError(SkillsPapaError):
    """The managed state is missing, corrupt, or internally inconsistent."""
