#!/usr/bin/env python3
"""Diagnose a live store's Shopify OAuth access: fetch its 3 most recent
orders and currently-granted access scopes, using the stored token.

Prints ONLY: each order's name, created date, and whether its email field
is non-null (true/false) — plus the granted scopes. NEVER prints emails,
names, addresses, or any token/secret.

Usage:
    python scripts/diagnose_store.py tillbird-test
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv  # noqa: E402

load_dotenv()

from app import create_app  # noqa: E402
from app.catalog import CatalogError  # noqa: E402
from app.shopify_client import build_client_for_store  # noqa: E402
from app.store_config import StoreConfigError, load_store_config  # noqa: E402

# Synthesized from shopify.dev/docs/apps/launch/protected-customer-data and
# Shopify Developer Community reports (checked 2026-10), which don't fully
# agree with each other — printed as-is rather than asserting one single
# definitive fix:
#   - Shopify's own docs describe a formal Partner Dashboard request/review
#     flow (Apps -> select app -> API access requests -> Protected customer
#     data access) for Level 1/2 access, with development stores bypassing
#     that review entirely.
#   - For apps distributed as "Custom distribution" (what this project
#     uses, one Dev Dashboard app per client — see docs/OAUTH_PLAN.md and
#     docs/DEPLOY.md section 14), Shopify staff and current docs say access
#     is automatic: only PUBLIC apps need to request/be approved. This is
#     also why some apps created in the new Dev Dashboard
#     (dev.shopify.com/dashboard) show no "API access requests"/"Protected
#     customer data" section at all in their dashboard (only Monitoring,
#     Logs, Versions, Configuration) — there's nothing to request.
#   - The most commonly reported real cause of a null email / access error
#     on a custom-distribution app is a STALE grant: a scope was added to
#     the app's Configuration after it was already installed, and the
#     install was never refreshed to pick it up.
PCD_GUIDANCE = """\
A field came back null, or the query reported an access error. This app
uses Shopify's "Custom distribution" app type (one Dev Dashboard app per
client), which is supposed to get automatic protected-customer-data access
(Shopify: "custom apps get Level 1 and Level 2 PCD automatically, so
there's no request, approval, or data-selection step") — unlike PUBLIC
apps, which must request and be approved. In practice this usually means
one of:

  1. The scope grant is stale. This is the most common real cause: a
     scope (e.g. read_orders) was added to the app's Configuration in the
     Dev Dashboard AFTER the app was already installed on this store, and
     the install was never refreshed. Fix:
       a. Confirm the scope is listed in this app's Configuration in the
          Dev Dashboard (dev.shopify.com/dashboard).
       b. Deploy the updated app version: `shopify app deploy`
       c. Reinstall the app on this store via the Dev Dashboard's
          "Install app" button, to refresh the authorized grant.
       d. Re-run this script.

  2. The app's Configuration is genuinely missing the scope. Add it,
     deploy, and reinstall as above.

  3. (Less likely for a custom-distribution app, but worth ruling out if
     1-2 don't fix it) The store's Shopify plan doesn't include the data
     type requested — Shopify ties some protected fields (e.g. customer
     name/address/phone on Level 2) to the Grow plan or higher; a store on
     a lower plan, or one that downgraded below Grow, loses that access.

Development stores bypass the FORMAL Partner Dashboard review that public
apps go through for this — but that review doesn't apply to a
custom-distribution app in the first place, so being a development store
isn't itself the fix here; items 1-3 above are.

If an order's email is null above and none of this applies, it may simply
be a genuine guest order recorded without an email — not every null is a
protected-data issue.\
"""


def format_diagnostics_report(diagnostics: dict) -> str:
    lines = []
    lines.append(f"Granted access scopes: {', '.join(diagnostics['scopes']) or '(none)'}")
    lines.append("")
    orders = diagnostics["orders"]
    if not orders:
        lines.append("No orders returned.")
    else:
        lines.append(f"{len(orders)} most recent order(s):")
        for order in orders:
            lines.append(f"  - {order['name']} | created {order['created_at']} | email present: {order['email_present']}")

    missing_email = any(not order["email_present"] for order in orders)
    if diagnostics["errors"] or missing_email:
        lines.append("")
        if diagnostics["errors"]:
            lines.append("GraphQL reported error(s) alongside the data above:")
            for error in diagnostics["errors"]:
                lines.append(f"  - {error.get('message', error)}")
            lines.append("")
        lines.append(PCD_GUIDANCE)

    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("store_id")
    args = parser.parse_args()

    app = create_app()
    app_config = app.extensions["app_config"]
    with app.app_context():
        try:
            store = load_store_config(args.store_id, stores_dir=app_config.stores_dir)
        except StoreConfigError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1

        try:
            client = build_client_for_store(store, app_config)
            diagnostics = client.get_diagnostics()
        except CatalogError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1

    print(format_diagnostics_report(diagnostics))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
