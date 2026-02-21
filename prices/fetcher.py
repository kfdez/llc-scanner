"""prices/fetcher.py — Live market price fetching for LLC Scanner.

Fetches pricing from the TCGdex REST API (which aggregates TCGPlayer USD and
CardMarket EUR data) and converts to CAD using the Frankfurter forex API.

No API keys required. Forex rates are cached per session.
"""

import requests

TCGDEX_CARD_URL = "https://api.tcgdex.net/v2/en/cards/{}"
FRANKFURTER_URL = "https://api.frankfurter.app/latest?from={}&to=CAD"

# Finish label → TCGdex tcgplayer variant key
_FINISH_TO_TCGP: dict[str, str] = {
    "Holo":             "holofoil",
    "Poke Ball Holo":   "holofoil",
    "Master Ball Holo": "holofoil",
    "Reverse Holo":     "reverseHolofoil",
    "Non-Holo":         "normal",
}

# Module-level forex cache — fetched once per session to avoid hammering the API.
_forex_cache: dict[str, float] = {}


def _get_rate(currency: str) -> float | None:
    """Return the CAD exchange rate for *currency* (e.g. 'USD', 'EUR').

    Result is cached for the lifetime of the process. Returns None on error.
    """
    if currency in _forex_cache:
        return _forex_cache[currency]
    try:
        r = requests.get(FRANKFURTER_URL.format(currency), timeout=5)
        r.raise_for_status()
        rate = r.json()["rates"]["CAD"]
        _forex_cache[currency] = rate
        return rate
    except Exception:
        return None


def fetch_price(card_id: str, finish: str) -> tuple[float | None, str | None]:
    """Fetch the market price for a card in CAD, given its TCGdex ID and finish.

    Priority:
    1. TCGPlayer USD market price for the matching variant → converted to CAD
    2. CardMarket EUR average price → converted to CAD

    Returns:
        (price_cad, source_label)  if a price was found
        (None, None)               if no price data is available or on any error

    source_label is one of:
        "TCGPlayer (USD→CAD)"
        "CardMarket (EUR→CAD)"
    """
    if not card_id:
        return None, None

    # Fetch card data from TCGdex REST API (SDK v2.2.1 doesn't expose pricing yet)
    try:
        r = requests.get(TCGDEX_CARD_URL.format(card_id), timeout=8)
        r.raise_for_status()
        pricing = r.json().get("pricing") or {}
    except Exception:
        return None, None

    # ── TCGPlayer (USD) ──────────────────────────────────────────────────────
    tcgp_variant = _FINISH_TO_TCGP.get(finish, "normal")
    tcgp = pricing.get("tcgplayer") or {}
    variant_data = tcgp.get(tcgp_variant) or {}

    # Prefer marketPrice, fall back to midPrice
    usd_price = variant_data.get("marketPrice") or variant_data.get("midPrice")

    if usd_price:
        rate = _get_rate("USD")
        if rate:
            return round(float(usd_price) * rate, 2), "TCGPlayer (USD->CAD)"

    # ── CardMarket (EUR) fallback ─────────────────────────────────────────────
    cm = pricing.get("cardmarket") or {}

    # For holo finishes use the holo average if available
    if finish in ("Holo", "Poke Ball Holo", "Master Ball Holo"):
        eur_price = cm.get("avg-holo") or cm.get("avg")
    else:
        eur_price = cm.get("avg")

    if eur_price:
        rate = _get_rate("EUR")
        if rate:
            return round(float(eur_price) * rate, 2), "CardMarket (EUR->CAD)"

    return None, None
