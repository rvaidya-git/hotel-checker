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
        hotel_cards = ""

        for m in matches:
            price_text = ", ".join([f"${p:,.2f}" for p in m["prices"]]) if m["prices"] else "No visible price found"

            hotel_cards += f"""
            <div style="margin-bottom:18px;border:1px solid #e5e7eb;border-radius:14px;padding:20px;background:#ffffff;">
              <div style="font-size:22px;font-weight:700;margin-bottom:6px;color:#0f172a;">
                {m['name']}
              </div>

              <div style="font-size:15px;color:#475569;margin-bottom:14px;">
                Possible two-queen availability detected.
              </div>

              <div style="font-size:14px;color:#334155;margin-bottom:16px;">
                <strong>Prices detected:</strong> {price_text}
              </div>

              <a href="{m['url']}" style="display:inline-block;padding:13px 22px;background:#2563eb;color:white;text-decoration:none;border-radius:10px;font-weight:700;">
                Open Booking Page
              </a>
            </div>
            """

        html = f"""
        <!DOCTYPE html>
        <html>
        <body style="margin:0;padding:0;background:#f4f6f8;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Arial,sans-serif;">
          <table width="100%" cellpadding="0" cellspacing="0" style="background:#f4f6f8;padding:28px;">
            <tr>
              <td align="center">
                <table width="680" cellpadding="0" cellspacing="0" style="background:#ffffff;border-radius:18px;overflow:hidden;box-shadow:0 8px 28px rgba(15,23,42,.12);">
                  
                  <tr>
                    <td style="background:#0f172a;padding:30px;color:white;">
                      <div style="font-size:28px;font-weight:800;line-height:1.2;">
                        Santa Cruz Hotel Alert
                      </div>
                      <div style="margin-top:8px;font-size:15px;color:#cbd5e1;">
                        Possible two-queen room found
                      </div>
                    </td>
                  </tr>

                  <tr>
                    <td style="padding:26px;">
                      <div style="padding:18px;background:#f8fafc;border:1px solid #e2e8f0;border-radius:14px;margin-bottom:24px;">
                        <div style="font-size:14px;color:#64748b;margin-bottom:4px;">Stay</div>
                        <div style="font-size:18px;font-weight:700;color:#0f172a;margin-bottom:14px;">June 19 → June 21, 2026</div>

                        <div style="font-size:14px;color:#64748b;margin-bottom:4px;">Guests</div>
                        <div style="font-size:18px;font-weight:700;color:#0f172a;margin-bottom:14px;">2 adults · 2 children</div>

                        <div style="font-size:14px;color:#64748b;margin-bottom:4px;">Room requirement</div>
                        <div style="font-size:18px;font-weight:700;color:#0f172a;margin-bottom:14px;">Two real queen beds only</div>

                        <div style="font-size:14px;color:#64748b;margin-bottom:4px;">Budget</div>
                        <div style="font-size:18px;font-weight:700;color:#0f172a;">Max $2,000 total stay</div>
                      </div>

                      {hotel_cards}

                      <div style="padding:18px;background:#fff7ed;border:1px solid #fed7aa;border-radius:14px;color:#7c2d12;">
                        <div style="font-weight:800;font-size:16px;margin-bottom:6px;">Action needed</div>
                        <div style="font-size:14px;line-height:1.5;">
                          Inventory can disappear fast. Open the booking page and reserve immediately if the room actually matches the requirements.
                        </div>
                      </div>
                    </td>
                  </tr>

                  <tr>
                    <td style="padding:18px;background:#f8fafc;color:#64748b;font-size:12px;text-align:center;">
                      Generated by Santa Cruz Hotel Checker
                    </td>
                  </tr>

                </table>
              </td>
            </tr>
          </table>
        </body>
        </html>
        """

        send_email(
            "Santa Cruz Hotel Alert: Possible 2-Queen Room Found",
            html,
        )
        print("Alert sent.")
    else:
        print("No matches found.")


if __name__ == "__main__":
    main()
