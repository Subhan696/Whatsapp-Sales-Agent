"""Per-tenant ordering channel.

``order_channel`` (AppSetting) decides where customers place orders:

  - "whatsapp"      (default) — the agent takes orders in chat: create_order,
                     bank transfer / COD, receipt screenshots verified in the CRM.
  - "website_link"  — customers order on the shop's own website. The agent helps
                     them choose, then sends the product page link; it never
                     creates orders, quotes bank details, collects addresses or
                     processes payment screenshots.

This is independent of the global ``commerce_mode`` (whatsapp_only | website),
which selects the catalog/order *backend* (website = Shopify Admin API).
"""
from __future__ import annotations

ORDER_CHANNEL_WHATSAPP = "whatsapp"
ORDER_CHANNEL_WEBSITE_LINK = "website_link"
ORDER_CHANNELS = (ORDER_CHANNEL_WHATSAPP, ORDER_CHANNEL_WEBSITE_LINK)

# Prefix the webhook puts in receipt_status when an image arrives in website mode.
WEBSITE_SCREENSHOT_STATUS = "WEBSITE_ORDER_SCREENSHOT"


def is_website_link(state: dict | None) -> bool:
    return (state or {}).get("order_channel") == ORDER_CHANNEL_WEBSITE_LINK


def website_refusal(state: dict | None, action: str) -> str:
    """Tool result when an in-chat ordering/payment action is attempted in website mode."""
    url = (state or {}).get("website_url") or "our website"
    return (
        f"NOT_AVAILABLE: {action} is not done in WhatsApp for this shop — customers order "
        f"and pay on the website ({url}). Do not create orders, quote bank details, ask for "
        "an address or accept payment screenshots in chat. Instead call share_order_link for "
        "the product and size/design they want, and tell them to add it to the bag and check "
        "out on the website."
    )
