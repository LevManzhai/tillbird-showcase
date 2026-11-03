import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

# A demo's scraped catalog is refreshed daily (scripts/refresh_demo_catalogs.py).
# If a refresh has failed or hasn't run yet and the data is older than this,
# search results are flagged stale so the agent hedges on stock certainty
# instead of stating it as current fact.
STALE_THRESHOLD_HOURS = 24.0


class CatalogError(Exception):
    """Raised when a product catalog can't be searched (bad config, API error, etc.)."""


def _meta_path(products_path: Path) -> Path:
    # acme.products.json -> acme.products.meta.json
    return products_path.with_suffix("").with_suffix(".products.meta.json")


def read_fetched_at(products_path: Path) -> datetime | None:
    """Read the last-scraped timestamp for a demo catalog, if recorded."""
    meta_path = _meta_path(products_path)
    if not meta_path.is_file():
        return None
    try:
        with meta_path.open("r", encoding="utf-8") as f:
            raw = json.load(f).get("fetched_at")
        return datetime.fromisoformat(raw) if raw else None
    except (ValueError, OSError, json.JSONDecodeError):
        return None


def write_fetched_at(products_path: Path, when: datetime | None = None) -> None:
    """Record the moment a demo catalog was last (re)scraped."""
    when = when or datetime.now(timezone.utc)
    with _meta_path(products_path).open("w", encoding="utf-8") as f:
        json.dump({"fetched_at": when.isoformat()}, f)


def parse_price(raw) -> float | None:
    """Parse a price value (Shopify's raw "24.99", "£24.99", 24.99, ...) to a float."""
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        return float(raw)
    cleaned = str(raw).strip().replace("£", "").replace(",", "")
    if not cleaned:
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def format_price_summary(prices: list[float]) -> str:
    """A single price if every variant matches, else a "£lo–£hi" range.

    Real bug this fixes: previously we always quoted the *first* variant's
    price, so a candle with a £18/£26/£34 size range would get quoted as
    "£18" regardless of which size the customer actually asked about.
    """
    if not prices:
        return ""
    lo, hi = min(prices), max(prices)
    if abs(lo - hi) < 0.005:
        return f"£{lo:.2f}"
    return f"£{lo:.2f}–£{hi:.2f}"


@dataclass
class ProductVariant:
    title: str
    price: str
    available: bool

    def to_dict(self) -> dict:
        return {"title": self.title, "price": self.price, "available": self.available}


@dataclass
class ProductResult:
    title: str
    price: str
    variants: list[ProductVariant] = field(default_factory=list)
    available: bool = True
    url: str = ""
    description: str = ""

    def to_dict(self) -> dict:
        return {
            "title": self.title,
            "price": self.price,
            "variants": [v.to_dict() for v in self.variants],
            "available": self.available,
            "url": self.url,
            "description": self.description,
        }


def pick_demo_order_line_items(products: list[dict], max_items: int = 2) -> list[dict]:
    """Pick 1-2 real products (with a real variant name, if the product has
    one) from a store's own catalog to use as its demo order's fixed line
    items — see StoreConfig.demo_order_line_items and
    app.agent.order_lookup.DEFAULT_DEMO_LINE_ITEMS, whose hardcoded "Merino
    Crew Jumper" fixture showed up for every demo regardless of what the
    store actually sells (nonsensical for e.g. a pet shop or candle store).

    `products` is the raw JSON shape written to stores/<id>.products.json
    (each a dict with "title", "available", "variants": [{"title",
    "available"}, ...]) — works directly from that file, or from
    [p.to_dict() for p in fetch_all_products(...)], without needing a fresh
    scrape. Prefers in-stock products and in-stock variants, but falls back
    to whatever's there rather than returning nothing. A single-variant
    product's placeholder "Default Title" variant is treated as no variant
    (None) rather than a real size/style name.
    """
    if not products:
        return []
    available_products = [p for p in products if p.get("available", True)] or list(products)

    items = []
    for product in available_products[:max_items]:
        variants = product.get("variants") or []
        available_variants = [v for v in variants if v.get("available", True)]
        chosen_variant = (available_variants or variants or [None])[0]

        variant_title = None
        if chosen_variant:
            raw_title = (chosen_variant.get("title") or "").strip()
            if raw_title and raw_title.lower() not in ("default title", "default"):
                variant_title = raw_title

        items.append({"title": product.get("title", "Item"), "variant": variant_title, "quantity": 1})
    return items


class StaticProductCatalog:
    """Demo-mode catalog backed by a local JSON fixture.

    Written by scripts/create_demo.py (and refreshed daily by
    scripts/refresh_demo_catalogs.py) from a store's public /products.json.
    Each product's price/availability is derived from its `variants` list
    (each {"title", "price", "available"}), not stored redundantly at the
    top level — a single source of truth avoids the two silently drifting
    out of sync.
    """

    def __init__(self, path: Path):
        self._path = path

    def is_stale(self, max_age_hours: float = STALE_THRESHOLD_HOURS) -> bool:
        """True if the catalog's last scrape is unknown or older than max_age_hours.

        Unknown age (no meta file — e.g. a demo created before daily refresh
        existed) is treated as stale by default: better to have the agent
        hedge on stock certainty than assume freshness it can't verify.
        """
        fetched_at = read_fetched_at(self._path)
        if fetched_at is None:
            return True
        age = datetime.now(timezone.utc) - fetched_at
        return age.total_seconds() > max_age_hours * 3600

    def search(self, query: str, limit: int = 5) -> list[ProductResult]:
        """query may be a single keyword or several comma-separated ones —
        a product matches if ANY term appears in its title or description.
        This lets a single call check several synonyms at once (e.g. an
        allergen search across "citrus, lime, lemon, bergamot, ...") instead
        of needing one tool call per synonym, which previously burned enough
        output tokens on repeated tool-call JSON to truncate the model's
        actual reply before it could write one.
        """
        if not self._path.is_file():
            return []
        with self._path.open("r", encoding="utf-8") as f:
            products = json.load(f)

        terms = [t.strip().lower() for t in query.split(",") if t.strip()]
        if not terms:
            return []

        def _matches(p: dict) -> bool:
            haystack = f"{p.get('title', '')} {p.get('description', '')}".lower()
            return any(term in haystack for term in terms)

        matches = [p for p in products if _matches(p)]
        results = []
        for p in matches[:limit]:
            variants = [
                ProductVariant(
                    title=v.get("title", ""),
                    price=v.get("price", ""),
                    available=bool(v.get("available", True)),
                )
                for v in p.get("variants", [])
            ]
            prices = [parsed for v in variants if (parsed := parse_price(v.price)) is not None]
            price_summary = format_price_summary(prices) or p.get("price", "")
            available = any(v.available for v in variants) if variants else p.get("available", True)
            results.append(
                ProductResult(
                    title=p["title"],
                    price=price_summary,
                    variants=variants,
                    available=available,
                    url=p.get("url", ""),
                    description=p.get("description", ""),
                )
            )
        return results


class ShopifyProductCatalog:
    """Live-mode catalog backed by the Shopify Admin GraphQL API — always
    queried fresh, so it's never stale in the sense StaticProductCatalog is.
    """

    def __init__(self, client):
        self._client = client

    def search(self, query: str, limit: int = 5) -> list[ProductResult]:
        return self._client.search_products(query, limit=limit)

    def is_stale(self, max_age_hours: float = STALE_THRESHOLD_HOURS) -> bool:
        return False


def get_product_catalog(store_config, app_config):
    if store_config.mode == "demo":
        path = Path(app_config.stores_dir) / f"{store_config.store_id}.products.json"
        return StaticProductCatalog(path)

    from app.shopify_client import build_client_for_store

    client = build_client_for_store(store_config, app_config)
    return ShopifyProductCatalog(client)
