import os
import time
import threading
import logging
from collections import defaultdict, deque

from dotenv import load_dotenv
load_dotenv()  # local dev only -- Render sets real env vars directly

import stripe
import anthropic
from flask import Flask, render_template, redirect, request, jsonify, Response

app = Flask(__name__)
logger = logging.getLogger(__name__)

# Stripe keys come from environment variables set in Render (never hardcoded)
stripe.api_key = os.environ.get("STRIPE_SECRET_KEY")
STRIPE_PUBLISHABLE_KEY = os.environ.get("STRIPE_PUBLISHABLE_KEY")

# Claude API key stays server-side only -- the frontend never sees it, it
# just calls our own /api/prompt-demo route below, which proxies to Claude.
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
anthropic_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY) if ANTHROPIC_API_KEY else None

# The two demo prompts live ONLY here, server-side -- the frontend can only
# ask for "bad" or "good" by name, never supply arbitrary prompt text. That
# keeps this from being repurposable as a free-form Claude proxy.
DEMO_PROMPTS = {
    "bad": "How do I use AI?",
    "good": (
        "I'm 62 years old, retired, and I've never used AI before. I want to learn how to ask "
        "it questions so I can help manage my health and stay connected with my grandkids. "
        "What's the first skill I should learn, and give me a concrete example of how I'd "
        "actually ask it a question differently than I'd ask Google?"
    ),
}
DEMO_SYSTEM_PROMPT = (
    "You are a helpful AI assistant teaching seniors how to use AI effectively. "
    "Keep answers concise, practical, and encouraging."
)
DEMO_MODEL = "claude-sonnet-5"  # current latest Sonnet as of this build
DEMO_MAX_TOKENS = 300
# Spec called for temperature=0.7, but the live API rejects it for this
# model ("temperature is deprecated for this model") -- confirmed against
# the real API, not guessed. Omitted rather than forced.

# Simple in-memory per-IP rate limit. Not distributed-safe (resets per
# dyno/restart, doesn't share state across workers) but this endpoint calls
# a metered paid API with zero auth barrier, so some cap beats none for a
# low-traffic demo widget.
_rate_limit_lock = threading.Lock()
_rate_limit_hits = defaultdict(deque)  # ip -> deque of call timestamps
RATE_LIMIT_MAX_CALLS = 6  # ~3 full comparisons (2 calls each) per window
RATE_LIMIT_WINDOW_SECONDS = 600


def _is_rate_limited(ip):
    now = time.time()
    with _rate_limit_lock:
        hits = _rate_limit_hits[ip]
        while hits and now - hits[0] > RATE_LIMIT_WINDOW_SECONDS:
            hits.popleft()
        if len(hits) >= RATE_LIMIT_MAX_CALLS:
            return True
        hits.append(now)
        return False

# Where the course itself lives -- the ai-made-simple-portal login page.
# A Stripe webhook (registered on this same Stripe account, pointed at that
# portal's /stripe/webhook) creates the account and emails credentials on
# checkout.session.completed; this link is a fallback for students who go
# looking for it instead of checking email.
COURSE_DELIVERY_URL = os.environ.get("COURSE_DELIVERY_URL", "https://ai-made-simple-portal.onrender.com/login")

DOMAIN = os.environ.get("DOMAIN", "https://ai-made-simple-landing.onrender.com")

COURSE_PRICE_CENTS = 3700  # $37.00 -- founding member launch price (was $87.00)
COURSE_NAME = "AI Made Simple 40+"


@app.route("/")
def index():
    return render_template("index.html", publishable_key=STRIPE_PUBLISHABLE_KEY)


@app.route("/create-checkout-session", methods=["POST"])
def create_checkout_session():
    try:
        checkout_session = stripe.checkout.Session.create(
            payment_method_types=["card"],
            line_items=[{
                "price_data": {
                    "currency": "usd",
                    "product_data": {
                        "name": COURSE_NAME,
                        "description": "8 lessons with videos and PDFs — AI made simple for adults 40+",
                    },
                    "unit_amount": COURSE_PRICE_CENTS,
                },
                "quantity": 1,
            }],
            mode="payment",
            success_url=DOMAIN + "/success?session_id={CHECKOUT_SESSION_ID}",
            cancel_url=DOMAIN + "/",
        )
        return jsonify({"id": checkout_session.id, "url": checkout_session.url})
    except Exception as e:
        return jsonify(error=str(e)), 400


@app.route("/success")
def success():
    session_id = request.args.get("session_id")
    customer_email = None
    payment_verified = False
    if session_id:
        try:
            session = stripe.checkout.Session.retrieve(session_id)
            customer_email = session.get("customer_details", {}).get("email")
            # Gates the Meta Pixel Purchase event -- only fire it for a
            # session Stripe confirms was actually paid, not just anyone
            # who loads this URL, so ad conversion tracking isn't inflated.
            payment_verified = session.get("payment_status") == "paid"
        except Exception:
            pass
    return render_template(
        "success.html",
        email=customer_email,
        course_url=COURSE_DELIVERY_URL,
        payment_verified=payment_verified,
        amount=COURSE_PRICE_CENTS / 100,
    )


@app.route("/api/prompt-demo/<variant>", methods=["POST"])
def prompt_demo(variant):
    if variant not in DEMO_PROMPTS:
        return jsonify(error="unknown variant"), 404

    if not anthropic_client:
        logger.error("prompt_demo called but ANTHROPIC_API_KEY is not configured")
        return jsonify(error="demo not configured"), 503

    ip = request.headers.get("X-Forwarded-For", request.remote_addr or "unknown").split(",")[0].strip()
    if _is_rate_limited(ip):
        return jsonify(error="rate limited, try again later"), 429

    def generate():
        try:
            with anthropic_client.messages.stream(
                model=DEMO_MODEL,
                max_tokens=DEMO_MAX_TOKENS,
                system=DEMO_SYSTEM_PROMPT,
                messages=[{"role": "user", "content": DEMO_PROMPTS[variant]}],
            ) as stream:
                for text in stream.text_stream:
                    yield text
        except Exception as exc:
            # Never leak the raw exception into the streamed body -- the
            # frontend already shows its own generic error message on any
            # non-2xx or thrown response, this is just a safety net for
            # failures that happen mid-stream, after headers are already sent.
            logger.error("prompt_demo stream failed for variant=%s: %s", variant, exc)
            yield "\n\n[Sorry, something went wrong generating this response.]"

    return Response(generate(), mimetype="text/plain")


if __name__ == "__main__":
    app.run(debug=True)
