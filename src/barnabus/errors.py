"""Pipeline-specific exceptions."""


class PipelineError(RuntimeError):
    """Base error for an aborted pipeline run."""


class ContractViolation(PipelineError):
    """A hard data contract failed."""


class InjectedFailure(PipelineError):
    """A test-only failure injected after a durable checkpoint."""
