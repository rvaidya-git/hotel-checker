import json
import os
import re
import sys
from datetime import datetime
import pytz
import requests
from playwright.sync_api import sync_playwright

ALERT_EMAIL = os.environ["ALERT_EMAIL"]
RESEND_API_KEY = os.environ["RESEND_API_KEY"]
FROM_EMAIL = os.environ["FROM_EMAIL"]

CHECK_IN = "2026-06-19"
CHECK_OUT = "2026-06-21"
STOP_AT_PT = "2026-06-16 15:00"

PT = pytz.timezone("America/Los_Angeles")


def now_pt():
    return datetime.now(PT)


def should_stop():
    stop_time = PT.localize(datetime.strptime(STOP_AT_PT, "%Y-%m-%d %H:%M"))
    return now_pt() > stop_time


def normalize(text):
    return re.sub(r"\s+", " ", text.lower()).strip()


def extract_prices(text):
    """
    Pulls rough dollar amounts from page text.
    This is imperfect but useful for catching obviously too-expensive rooms.
    """
    prices = []
    for match in re.findall(r"\$[\s]*([0-9,]+(?:\.\d{2})?)", text):
        try:
            prices.append(float(match.replace(",", "")))
        except ValueError:
            pass
    return prices


def likely_under_budget(text, max_total_usd):
    prices = extract_prices(text)

    # If no price is visible, still alert as "possible match."
    # Better false positive than missing La Bahia availability.
    if not prices:
        return True

    # Look for any visible price <= max total.
    # Booking pages vary: some show nightly, some total.
    return any(price <= max_total_usd for price in prices)


def send_email(subject, html):
    response = requests.post(
        "https://api.resend.com/emails",
        headers={
            "Authorization": f"Bearer {RESEND_API_KEY}",
            "Content-Type": "application/json",
        },
        json={
            "from": FROM_EMAIL,
            "to": [ALERT_EMAIL],
            "subject": subject,
            "html": html,
        },
        timeout=20,
    )

    if response.status_code >= 300:
        print("Email failed:", response.status_code, response.text)
        response.raise_for_status()


def check_hotel(page, hotel):
    name = hotel["name"]
    url = hotel["search_url"]
    must_have_terms = [t.lower() for t in hotel["must_have_terms"]]
    reject_terms = [t.lower() for t in hotel["reject_terms"]]
    max_total_usd = hotel["max_total_usd"]

    print(f"Checking {name}: {url}")

    page.goto(url, wait_until="networkidle", timeout=60000)
    page.wait_for_timeout(5000)

    text = normalize(page.inner_text("body"))

    has_required_bed = any(term in text for term in must_have_terms)
    has_reject_term = any(term in text for term in reject_terms)
    under_budget = likely_under_budget(text, max_total_usd)

    print({
        "hotel": name,
        "has_required_bed": has_required_bed,
        "has_reject_term": has_reject_term,
        "under_budget": under_budget,
        "prices_found": extract_prices(text)[:20],
    })

    if has_required_bed and not has_reject_term and under_budget:
        return {
            "name": name,
            "url": url,
            "status": "possible_match",
            "prices": extract_prices(text)[:20],
        }

    return None


def main():
    if should_stop():
        print("Past stop time. Exiting.")
        return

    with open("hotels.json", "r") as f:
        hotels = json.load(f)

    matches = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            viewport={"width": 1280, "height": 900},
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/125.0.0.0 Safari/537.36"
            ),
        )
        page = context.new_page()

        for hotel in hotels:
            try:
                result = check_hotel(page, hotel)
                if result:
                    matches.append(result)
            except Exception as e:
                print(f"Error checking {hotel['name']}: {e}")

        browser.close()

    if matches:
        rows = ""
        for m in matches:
            rows += f"""
            <tr>
              <td>{m['name']}</td>
              <td><a href="{m['url']}">Open booking page</a></td>
              <td>{m['prices']}</td>
            </tr>
            """

        html = f"""
        <h2>Possible Santa Cruz hotel match found</h2>
        <p>
          Dates: {CHECK_IN} to {CHECK_OUT}<br>
          Guests: 2 adults, 2 children<br>
          Required: two real queen beds<br>
          Max total: $2,000 all-in
        </p>
        <table border="1" cellpadding="8" cellspacing="0">
          <tr>
            <th>Hotel</th>
            <th>Booking Page</th>
            <th>Prices Found</th>
          </tr>
          {rows}
        </table>
        <p>
          Book manually immediately. This alert is intentionally conservative and may include false positives.
        </p>
        """

        send_email(
            "Possible Santa Cruz 2-Queen Hotel Match Found",
            html,
        )
        print("Alert sent.")
    else:
        print("No matches found.")


if __name__ == "__main__":
    main()