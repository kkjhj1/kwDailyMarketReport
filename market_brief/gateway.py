"""Channel-agnostic delivery gateway with retry backoff, audit logging, and channel failover."""
import time
from market_brief.core import Briefing, DeliveryChannel
from market_brief.alert_store import AlertStore


class DeliveryGateway:
    """Dispatches a Briefing across primary/fallback channels with retries and audit logging."""

    def __init__(
        self,
        primary_channel: DeliveryChannel,
        *,
        primary_name: str = "telegram",
        fallback_channel: DeliveryChannel | None = None,
        fallback_name: str = "console",
        alert_store: AlertStore | None = None,
        max_retries: int = 3,
        backoff_seconds: float = 0.0,
    ):
        self.primary_channel = primary_channel
        self.primary_name = primary_name
        self.fallback_channel = fallback_channel
        self.fallback_name = fallback_name
        self.alert_store = alert_store
        self.max_retries = max(1, max_retries)
        self.backoff_seconds = backoff_seconds

    def dispatch(self, recipient: str, briefing: Briefing, *, event_key: str = "REGULAR") -> str:
        last_err: Exception | None = None
        for attempt in range(1, self.max_retries + 1):
            try:
                msg_id = self.primary_channel.send(recipient, briefing)
                if self.alert_store:
                    self.alert_store.log_delivery(
                        event_key, self.primary_name, recipient, "SENT",
                        provider_msg_id=msg_id, attempts=attempt
                    )
                    if ":" in event_key:
                        self.alert_store.update_reservation_status(event_key, "sent")
                return msg_id
            except Exception as exc:
                last_err = exc
                if attempt < self.max_retries and self.backoff_seconds > 0:
                    time.sleep(self.backoff_seconds * (2 ** (attempt - 1)))

        if self.alert_store:
            self.alert_store.log_delivery(
                event_key, self.primary_name, recipient, "FAILED",
                attempts=self.max_retries, error_detail=str(last_err)
            )

        if self.fallback_channel is not None:
            try:
                msg_id = self.fallback_channel.send(recipient, briefing)
                if self.alert_store:
                    self.alert_store.log_delivery(
                        event_key, self.fallback_name, recipient, "SENT_FALLBACK",
                        provider_msg_id=msg_id, attempts=1
                    )
                    if ":" in event_key:
                        self.alert_store.update_reservation_status(event_key, "sent")
                return msg_id
            except Exception as fb_exc:
                if self.alert_store:
                    self.alert_store.log_delivery(
                        event_key, self.fallback_name, recipient, "FAILED_FALLBACK",
                        attempts=1, error_detail=str(fb_exc)
                    )
                    if ":" in event_key:
                        self.alert_store.update_reservation_status(event_key, "failed_dead_letter")
                raise fb_exc from last_err

        if self.alert_store and ":" in event_key:
            self.alert_store.update_reservation_status(event_key, "failed_dead_letter")
        raise RuntimeError(f"Delivery failed after {self.max_retries} attempts: {last_err}") from last_err
