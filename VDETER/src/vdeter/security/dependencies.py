"""Explicit secure or legacy Presentation execution mode."""

from dataclasses import dataclass
from enum import Enum

from .policy import PayloadSecurityPolicy


class E2eSecurityMode(str, Enum):
    DISABLED = "DISABLED"
    ENFORCED = "ENFORCED"


@dataclass(frozen=True, slots=True)
class E2eSecurityDependencies:
    mode: E2eSecurityMode
    presentation_security: object

    def __post_init__(self):
        from .presentation import PresentationSecurity
        if (type(self.mode) is not E2eSecurityMode
                or not isinstance(self.presentation_security, PresentationSecurity)
                or (self.mode is E2eSecurityMode.DISABLED) != (self.presentation_security.manager is None)):
            raise ValueError("Invalid explicit E2E security mode/dependencies")
        self.presentation_security.metrics.increment("security_mode_start_total")

    @classmethod
    def disabled(cls):
        from .presentation import PresentationSecurity
        return cls(E2eSecurityMode.DISABLED,
                   PresentationSecurity(PayloadSecurityPolicy.legacy_all_disabled(), None))

    @classmethod
    def enforced(cls, manager):
        from cryptography import __version__
        from .presentation import PresentationSecurity
        from .session.manager import SecuritySessionManager
        if not isinstance(manager, SecuritySessionManager):
            raise ValueError("ENFORCED requires a directional security session manager")
        if __version__.split(".")[0] != "50":
            raise ValueError("ENFORCED requires cryptography major version 50")
        return cls(E2eSecurityMode.ENFORCED,
                   PresentationSecurity(PayloadSecurityPolicy.enforced(), manager))
