"""Model-provisioning errors shared with the vendored Re-ID engine."""


class ModelMissingError(RuntimeError):
    """A required model file is not on disk and downloads are disabled."""
