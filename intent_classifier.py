"""Lightweight intent classifier for inbound customer messages.

Keyword-based (English + Roman Urdu), zero-cost and deterministic. Every
inbound message gets exactly one intent tag; anything unmatched falls back
to "general". Upgrade path: replace classify_intent with an AI-backed
implementation later without changing any caller.
"""

INTENT_KEYS = (
    "human_request",
    "complaint",
    "refund_return",
    "order_tracking",
    "shipping",
    "appointment",
    "pricing",
    "availability",
    "purchase_intent",
    "general",
)

_KEYWORDS = {
    "human_request": (
        "insaan", "insan", "human", "real person", "agent se baat",
        "bande se", "bandey se", "representative", "manager se", "operator",
    ),
    "complaint": (
        "shikayat", "complaint", "kharab", "masla", "problem",
        "broken", "kaam nahi", "faulty", "damaged",
    ),
    "refund_return": (
        "refund", "paise wapas", "paisay wapas", "wapas pais", "return",
        "exchange", "replace kar", "wapsi",
    ),
    "order_tracking": (
        "mera order", "order kahan", "order ka status", "order status",
        "tracking", "track kar", "kahan pohnch", "kahan pahunch",
        "kahan reach",
    ),
    "shipping": (
        "delivery", "shipping", "kab aayega", "kab milega",
        "kitne din", "courier", "dispatch",
    ),
    "appointment": (
        "appointment", "booking", "book kar", "slot", "milna",
        "visit", "timing available",
    ),
    "pricing": (
        "price", "kitna", "kitne ka", "kitne paise", "qemat", "keemat",
        "rate", "cost", "charges", "fee",
    ),
    "availability": (
        "available", "stock", "mojood", "mawjood", "mil sakta",
    ),
    "purchase_intent": (
        "order karna", "kharidna", "kharid", "buy", "purchase",
        "lena hai", "chahiye",
    ),
}


def classify_intent(body):
    """Return exactly one intent key for a customer message body."""
    text = " " + " ".join(str(body or "").lower().split()) + " "
    if not text.strip():
        return "general"
    for intent in INTENT_KEYS:
        if intent == "general":
            continue
        for keyword in _KEYWORDS[intent]:
            if keyword in text:
                return intent
    return "general"
