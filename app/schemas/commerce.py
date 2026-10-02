"""Shared commerce schemas used by agent tools and analytics."""
from __future__ import annotations

from decimal import Decimal

from pydantic import BaseModel, Field


class ProductResult(BaseModel):
    sku: str
    name: str
    description: str = ""
    price: Decimal
    stock: int
    tags: list[str] = Field(default_factory=list)
    image_url: str | None = None
    video_url: str | None = None
    images: list[str] = Field(default_factory=list)
    options: dict[str, list[str]] = Field(default_factory=dict)
    variants: list[dict] = Field(default_factory=list)
    compare_at_price: Decimal | None = None
    currency: str | None = None
    source_url: str | None = None

    def _option_lines(self, cur: str) -> list[str]:
        lines = []
        available = [v for v in self.variants if v.get("available", True)]
        for name, values in self.options.items():
            shown = []
            for value in values[:25]:
                offered = [v for v in self.variants if (v.get("options") or {}).get(name) == value]
                sold_out = offered and not any(v in available for v in offered)
                shown.append(f"{value} (sold out)" if sold_out else value)
            lines.append(f"{name}: {', '.join(shown)}")

        prices = {v.get("price") for v in self.variants if v.get("price")}
        if len(prices) > 1:
            parts = [
                f"{v.get('name')}: {cur} {Decimal(v['price']):,.0f}"
                for v in available[:12]
                if v.get("price")
            ]
            if parts:
                lines.append("Price by option: " + " · ".join(parts))
        return lines

    def display(self) -> str:
        cur = self.currency or "PKR"
        photo_count = len(self.images) or (1 if self.image_url else 0)
        media_hint = ""
        if photo_count:
            label = f"{photo_count} photos" if photo_count > 1 else "Photo"
            media_hint = f"\n📷 {label} available — call send_product_media(sku='{self.sku}') to show the customer"
        elif self.video_url:
            media_hint = f"\n🎬 Video available — call send_product_media(sku='{self.sku}') to show the customer"
        availability = "Last few left" if self.stock <= 5 else "In Stock"
        price = f"{cur} {self.price:,.2f}"
        if self.compare_at_price and self.compare_at_price > self.price:
            price += f" (on sale, was {cur} {self.compare_at_price:,.2f})"
        option_lines = "".join(f"\n{line}" for line in self._option_lines(cur))
        link = f"\n🔗 {self.source_url}" if self.source_url else ""
        return (
            f"*{self.name}* (SKU: {self.sku})\n"
            f"Price: {price} | {availability}"
            f"{option_lines}\n"
            f"{self.description[:200]}{'…' if len(self.description) > 200 else ''}"
            f"{media_hint}"
            f"{link}"
        )


class CartItem(BaseModel):
    sku: str
    quantity: int = Field(ge=1)
    # Chosen size/colour etc. for products with options, e.g. "M / Black" or "Size: M".
    variant: str | None = None


class OrderSummary(BaseModel):
    order_ref: str
    mode: str
    line_items: list[dict]
    subtotal: Decimal
    delivery_charge: Decimal
    total: Decimal
    payment_method: str = "bank_transfer"
    delivery_estimate: str | None = None  # e.g. "by Thu, 03 Jul 2026"
    external_ref: str | None = None  # Shopify checkout URL (website mode)

    def display(self) -> str:
        _SEP = "─────────────────────"

        item_lines = []
        for i in self.line_items:
            name = i.get("name") or i.get("sku", "?")
            qty = int(i.get("quantity", 1))
            unit = Decimal(str(i.get("unit_price", "0")))
            line_total = Decimal(str(i.get("line_total", "0")))
            if qty > 1:
                item_lines.append(
                    f"• {name}\n"
                    f"  Qty: {qty} × PKR {unit:,.2f} = *PKR {line_total:,.2f}*"
                )
            else:
                item_lines.append(
                    f"• {name}\n"
                    f"  *PKR {unit:,.2f}*"
                )

        items_block = "\n".join(item_lines)

        delivery_line = (
            f"Delivery:     PKR {self.delivery_charge:,.2f}"
            if self.delivery_charge > 0
            else "Delivery:     Free"
        )

        pay_label = {
            "bank_transfer": "Bank Transfer",
            "cod": "Cash on Delivery (COD)",
        }.get(self.payment_method, self.payment_method)

        checkout = f"\n\n🛒 Pay here: {self.external_ref}" if self.external_ref else ""
        eta_line = f"\nEst. Delivery: {self.delivery_estimate}" if self.delivery_estimate else ""

        return (
            f"🧾 *Order Receipt*\n"
            f"*Order:* {self.order_ref}\n\n"
            f"*Items:*\n"
            f"{items_block}\n\n"
            f"{_SEP}\n"
            f"Subtotal:     PKR {self.subtotal:,.2f}\n"
            f"{delivery_line}\n"
            f"{_SEP}\n"
            f"*TOTAL:       PKR {self.total:,.2f}*\n"
            f"Payment:      {pay_label}"
            f"{eta_line}"
            f"{checkout}"
        )
