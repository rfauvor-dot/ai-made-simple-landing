import os
import stripe
from flask import Flask, render_template, redirect, request, jsonify

app = Flask(__name__)

# Stripe keys come from environment variables set in Render (never hardcoded)
stripe.api_key = os.environ.get("STRIPE_SECRET_KEY")
STRIPE_PUBLISHABLE_KEY = os.environ.get("STRIPE_PUBLISHABLE_KEY")

# Where the course itself lives (Payhip product download / member link)
COURSE_DELIVERY_URL = os.environ.get("COURSE_DELIVERY_URL", "https://aimadesimple40plus.com/access")

DOMAIN = os.environ.get("DOMAIN", "https://ai-made-simple-landing.onrender.com")

COURSE_PRICE_CENTS = 8700  # $87.00
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
    if session_id:
        try:
            session = stripe.checkout.Session.retrieve(session_id)
            customer_email = session.get("customer_details", {}).get("email")
        except Exception:
            pass
    return render_template("success.html", email=customer_email, course_url=COURSE_DELIVERY_URL)


if __name__ == "__main__":
    app.run(debug=True)
