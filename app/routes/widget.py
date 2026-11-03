from pathlib import Path

from flask import Blueprint, Response, current_app, jsonify, redirect, render_template

from app.store_config import StoreConfigError, load_store_config

widget_bp = Blueprint("widget", __name__)

_WIDGET_JS_PATH = Path(__file__).resolve().parent.parent / "static" / "widget.js"

MARKETING_SITE_URL = "https://tillbird.co.uk"


@widget_bp.get("/")
def index():
    # This host (app.tillbird.co.uk) only serves the API/widget/demo routes —
    # there's no landing page here. "/" 404ing was getting hit by scanners;
    # a redirect to the actual marketing site is a more useful response than
    # a bare 404, and a 302 (not 301) keeps this cheap to change later.
    return redirect(MARKETING_SITE_URL, code=302)


@widget_bp.get("/widget.js")
def widget_js():
    js = _WIDGET_JS_PATH.read_text(encoding="utf-8")
    response = Response(js, mimetype="application/javascript")
    # Public and non-sensitive by design — the whole point is that this script
    # is embedded on arbitrary customer storefronts, unlike /api/chat which is
    # restricted per-store via allowed_origins.
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Cache-Control"] = "public, max-age=300"
    return response


@widget_bp.get("/api/widget-config/<store_id>")
def widget_config(store_id):
    app_config = current_app.extensions["app_config"]
    try:
        store = load_store_config(store_id, stores_dir=app_config.stores_dir)
    except StoreConfigError:
        return jsonify(error="unknown store"), 404

    payload = {
        "store_name": store.store_name,
        "brand_color": store.brand_color,
        "greeting": store.greeting,
        "hint_text": store.hint_text,
        "privacy_policy_url": store.privacy_policy_url,
        "widget_tagline": store.widget_tagline,
        # Tillbird's own privacy page (distinct from the store's own
        # privacy_policy_url above) — linked from the widget header's
        # small "AI assistant · may make mistakes" disclosure line.
        "tillbird_privacy_url": app_config.tillbird_privacy_url,
        # A trial past its end date, or a paused/cancelled live store —
        # see StoreConfig.is_access_blocked. The widget must not even show
        # its launcher bubble for one of these, not just refuse to answer
        # once clicked (see app/static/widget.js's init()); /api/chat has
        # its own matching refusal as defense in depth.
        "disabled": store.is_access_blocked(),
    }
    response = jsonify(payload)
    response.headers["Access-Control-Allow-Origin"] = "*"
    return response


@widget_bp.get("/demo/<store_id>")
def demo_page(store_id):
    app_config = current_app.extensions["app_config"]
    try:
        store = load_store_config(store_id, stores_dir=app_config.stores_dir)
    except StoreConfigError:
        return "store not found", 404

    # Demo links are meant to be shared privately with a prospect, not
    # discovered — never let them show up in search results.
    robots_header = {"X-Robots-Tag": "noindex, nofollow"}

    if store.is_demo_expired(app_config.demo_expiry_days):
        return render_template("demo_expired.html", store=store), 200, robots_header

    return render_template("demo.html", store=store), 200, robots_header
