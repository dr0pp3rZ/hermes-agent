"""Surface-agnostic core for the ``/subscription`` TUI screen.

Companion to :mod:`agent.billing_view` — same fail-open philosophy: when not
logged in or the portal is unreachable, return a struct with ``logged_in=False``
and let the surface degrade gracefully (never crash). Money is decimal end-to-end
(server emits decimal strings); we only format for display.

The TUI ``SubscriptionOverlay`` is **deep-link only** — it never charges
in-terminal. :func:`get_subscription_manage_link` returns a Stripe-hosted URL
the TUI opens in the browser.

WS1 dependency: ``GET /api/billing/subscription`` and
``POST /api/billing/subscription/manage-link`` are NAS endpoints (WS1 Phase A/C).
Until they ship, the fail-open contract handles 404s — the builder returns
``logged_in=False`` and the manage-link raises a :class:`BillingError` the
gateway serializes into a typed error envelope.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Optional

from agent.billing_view import format_money, parse_money

logger = logging.getLogger(__name__)


# =============================================================================
# Parsed sub-structures
# =============================================================================


@dataclass(frozen=True)
class SubscriptionTier:
    """A plan tier in the catalog."""

    tier_id: str
    name: str
    tier_order: int
    dollars_per_month: Optional[Decimal] = None
    monthly_credits: Optional[Decimal] = None
    is_current: bool = False
    is_enabled: bool = True


@dataclass(frozen=True)
class CurrentSubscription:
    """The user's active subscription (None fields = free / no active sub)."""

    tier_id: Optional[str] = None
    tier_name: Optional[str] = None
    monthly_credits: Optional[Decimal] = None
    credits_remaining: Optional[Decimal] = None
    cycle_ends_at: Optional[str] = None  # ISO
    pending_downgrade_tier_name: Optional[str] = None
    pending_downgrade_at: Optional[str] = None  # ISO
    is_past_due: bool = False


@dataclass(frozen=True)
class SubscriptionState:
    """Parsed ``GET /api/billing/subscription`` — the overview screen's data.

    Fail-open: ``logged_in=False`` (and empty fields) when not logged in or the
    portal is unreachable.
    """

    logged_in: bool
    org_name: Optional[str] = None
    role: Optional[str] = None  # "OWNER" | "ADMIN" | "MEMBER"
    current: Optional[CurrentSubscription] = None
    tiers: tuple[SubscriptionTier, ...] = ()
    portal_url: Optional[str] = None
    # When the fetch failed (vs cleanly not-logged-in), the message for the surface.
    error: Optional[str] = None

    @property
    def is_admin(self) -> bool:
        """True for OWNER/ADMIN — the roles that can change plans."""
        return (self.role or "").upper() in ("OWNER", "ADMIN")

    @property
    def can_change_plan(self) -> bool:
        """True when the UI should offer plan-change actions (role gate from NAS)."""
        return self.is_admin


# =============================================================================
# Payload parsing
# =============================================================================


def _parse_tier(raw: Any) -> Optional[SubscriptionTier]:
    if not isinstance(raw, dict):
        return None
    tier_id = raw.get("tierId") or raw.get("id")
    name = raw.get("name")
    if not (isinstance(tier_id, str) and isinstance(name, str)):
        return None
    return SubscriptionTier(
        tier_id=tier_id,
        name=name,
        tier_order=int(raw.get("tierOrder") or raw.get("order") or 0),
        dollars_per_month=parse_money(raw.get("dollarsPerMonth") or raw.get("priceUsd")),
        monthly_credits=parse_money(raw.get("monthlyCredits")),
        is_current=bool(raw.get("isCurrent")),
        is_enabled=bool(raw.get("isEnabled", True)),
    )


def _parse_current(raw: Any) -> Optional[CurrentSubscription]:
    if not isinstance(raw, dict):
        return None
    return CurrentSubscription(
        tier_id=raw.get("tierId") or raw.get("id"),
        tier_name=raw.get("tierName") or raw.get("name"),
        monthly_credits=parse_money(raw.get("monthlyCredits")),
        credits_remaining=parse_money(raw.get("creditsRemaining")),
        cycle_ends_at=raw.get("cycleEndsAt"),
        pending_downgrade_tier_name=raw.get("pendingDowngradeTierName"),
        pending_downgrade_at=raw.get("pendingDowngradeAt"),
        is_past_due=bool(raw.get("isPastDue")),
    )


def subscription_state_from_payload(
    payload: dict[str, Any], *, portal_url: Optional[str] = None
) -> SubscriptionState:
    """Map a raw ``/api/billing/subscription`` JSON dict into :class:`SubscriptionState`."""
    raw_org = payload.get("org")
    org: dict[str, Any] = raw_org if isinstance(raw_org, dict) else {}

    tiers: list[SubscriptionTier] = []
    for item in payload.get("tiers") or ():
        parsed = _parse_tier(item)
        if parsed is not None:
            tiers.append(parsed)

    return SubscriptionState(
        logged_in=True,
        org_name=org.get("name"),
        role=org.get("role"),
        current=_parse_current(payload.get("current")),
        tiers=tuple(tiers),
        portal_url=portal_url,
    )


# =============================================================================
# Fail-open builders (the surface front doors)
# =============================================================================


def build_subscription_state(*, timeout: float = 15.0) -> SubscriptionState:
    """Fetch + parse ``GET /api/billing/subscription``. Fail-open.

    Returns ``SubscriptionState(logged_in=False)`` when not logged in. On a
    portal/HTTP failure, returns ``logged_in=False`` with ``error`` set so the
    surface can show a clear message rather than crashing.
    """
    try:
        from hermes_cli.nous_billing import (
            BillingAuthError,
            BillingError,
            _absolutize_portal_url,
            get_subscription_state,
            resolve_portal_base_url,
        )
    except Exception:
        return SubscriptionState(logged_in=False, error="billing client unavailable")

    try:
        payload = get_subscription_state(timeout=timeout)
    except BillingAuthError:
        return SubscriptionState(logged_in=False)
    except BillingError as exc:
        logger.debug("subscription ▸ /state fetch failed (fail-open)", exc_info=True)
        return SubscriptionState(logged_in=False, error=str(exc))
    except Exception:
        logger.debug("subscription ▸ /state unexpected error (fail-open)", exc_info=True)
        return SubscriptionState(logged_in=False, error="could not load subscription state")

    raw_portal = payload.get("portalUrl") if isinstance(payload, dict) else None
    portal_url = _absolutize_portal_url(raw_portal) if raw_portal else None
    if not portal_url:
        try:
            portal_url = resolve_portal_base_url()
        except Exception:
            portal_url = None

    return subscription_state_from_payload(payload, portal_url=portal_url)


def get_subscription_manage_link(
    *, target_tier_id: Optional[str] = None, timeout: float = 15.0
) -> dict[str, Any]:
    """Fetch the Stripe-hosted manage-link URL from NAS.

    Returns ``{"kind": "checkout"|"portal", "url": "https://..."}``. Raises
    :class:`BillingScopeRequired` when the Remote-Spending grant is missing
    (Phase 4 step-up trigger) — the caller (gateway RPC) serializes that into
    ``error:"insufficient_scope"``.
    """
    from hermes_cli.nous_billing import post_subscription_manage_link

    return post_subscription_manage_link(target_tier_id=target_tier_id, timeout=timeout)
