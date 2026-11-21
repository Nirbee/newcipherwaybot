"""Domain exception hierarchy.

Layers raise these; the web/bot presentation maps them to HTTP codes / user messages.
Keep messages developer-facing — user-facing text comes from i18n.
"""

from __future__ import annotations


class DomainError(Exception):
    """Base class for all expected, handled domain errors."""


class ConfigError(DomainError):
    """Invalid or unsafe configuration (rejected at startup)."""


# --- access / auth ---------------------------------------------------------
class AccessDenied(DomainError):
    """Actor lacks the required role/permission."""


class NotFound(DomainError):
    """A referenced entity does not exist."""


# --- subscriptions / purchases --------------------------------------------
class PurchaseError(DomainError):
    """A purchase could not be completed (invalid plan, no active sub, etc.)."""


class TrialNotAvailable(PurchaseError):
    """User is not eligible for a trial."""


class InsufficientBalance(PurchaseError):
    """Wallet balance is too low for a balance-funded operation."""


# --- payments --------------------------------------------------------------
class PaymentError(DomainError):
    """Generic payment failure."""


class GatewayNotConfigured(PaymentError):
    """Requested gateway type has no active DB configuration."""


class WebhookVerificationError(PaymentError):
    """Signature / IP / secret verification failed (maps to HTTP 403)."""


class InvalidStateTransition(PaymentError):
    """Attempted a transaction status change not allowed from the current state."""


# --- panel (Remnawave) -----------------------------------------------------
class RemnawaveError(DomainError):
    """Base for all Remnawave panel client errors."""


class RemnawaveAuthError(RemnawaveError):
    """Panel rejected our credentials (hard fail, do not retry)."""


class RemnawaveTransientError(RemnawaveError):
    """Timeout / 5xx / connection error — safe to retry with backoff."""


class RemnawaveVersionError(RemnawaveError):
    """Panel version is below the supported minimum."""


# --- LAN (Headscale) ---------------------------------------------------------
class HeadscaleError(DomainError):
    """Headscale is unreachable, rejected the key or answered with an error. The LAN API
    maps every one of these to a clean 503 — the caller's change is not applied. The message
    never carries the server's response body (that goes to the logs only)."""

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class HeadscaleNotFound(HeadscaleError):
    """The addressed user / node does not exist on the Headscale server (HTTP 404)."""


class HeadscaleAuthError(HeadscaleError):
    """Headscale rejected the API key (401/403)."""


class HeadscalePolicyReadOnly(HeadscaleError):
    """PUT /policy refused: the server runs ``policy.mode: file``, not ``database``."""
