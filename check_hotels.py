import base64
import os
import re
from datetime import datetime
from pathlib import Path

import pytz
import requests
import yaml
from playwright.sync_api import sync_playwright


CONFIG_PATH = "config.yaml"
SCREENSHOT_DIR = Path("screenshots")


def load_config():
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def normalize(text):
    return re.sub(r"\s+", " ", text.lower()).strip()


def now_in_timezone(timezone):
    tz = pytz.timezone(timezone)
    return datetime.now(tz)


def should_stop(config):
    timezone = config["search"].get("timezone", "America/Los_Angeles")
    tz = pytz.timezone(timezone)
    stop_at = config["search"].get("stop_at")

    if not stop_at:
        return False

    stop_time = tz.localize(datetime.strptime(stop_at, "%Y-%m-%d %H:%M"))
    return now_in_timezone(timezone) > stop_time


def extract_prices(text):
    prices = []

    for match in re.findall(r"\$[\s]*([0-9,]+(?:\.\d{2})?)", text):
        try:
            prices.append(float(match.replace(",", "")))
        except ValueError:
            pass

    return prices


def is_under_budget(text, max_total_stay_usd):
    prices = extract_prices(text)

    # If no price is visible, still alert.
    # Better to get a false positive than miss a scarce room.
    if not prices:
        return True

    return any(price <= max_total_stay_usd for price in prices)


def contains_any(text, terms):
    return any(term.lower() in text for term in terms)


def missing_hard_amenity(text, hard_amenities):
    for amenity in hard_amenities:
        if amenity.lower() not in text:
            return amenity
    return None


def build_search_targets(config):
    targets = []

    for hotel in config.get("hotels", []):
        targets.append(hotel)

    for search in config.get("broad_searches", []):
        targets.append(search)

    return targets


def take_screenshot(page, target_name):
    SCREENSHOT_DIR.mkdir(exist_ok=True)

    safe_name = re.sub(r"[^a-zA-Z0-9_-]", "_", target_name)[:60]
    screenshot_path = SCREENSHOT_DIR / f"{safe_name}.png"

    page.screenshot(path=str(screenshot_path), full_page=True)
    return screenshot_path


def screenshot_to_base64(path):
    if not path or not Path(path).exists():
        return None

    with open(path, "rb") as f:
        encoded = base64.b64encode(f.read()).decode("utf-8")

    return encoded


def check_target(page, target, config):
    search_config = config["search"]
    room_rules = config["room_rules"]
    availability_rules = config["availability_rules"]
    amenities = config.get("amenities", {})
    budget = config["budget"]
    email_config = config["email"]

    name = target["name"]
    url = target["search_url"]

    print(f"Checking: {name}")
    print(f"URL: {url}")

    page.goto(url, wait_until="networkidle", timeout=70000)
    page.wait_for_timeout(5000)

    body_text = page.inner_text("body")
    text = normalize(body_text)

    required_bed_terms = room_rules.get("required_bed_terms", [])
    rejected_bed_terms = room_rules.get("rejected_bed_terms", [])
    reject_terms = availability_rules.get("reject_terms", [])

    hard_amenities = amenities.get("hard_requirements", [])
    nice_amenities = amenities.get("nice_to_have", [])

    has_required_bed = contains_any(text, required_bed_terms)
    has_rejected_bed = contains_any(text, rejected_bed_terms)
    has_unavailable_term = contains_any(text, reject_terms)
    missing_amenity = missing_hard_amenity(text, hard_amenities)
    under_budget = is_under_budget(text, budget["max_total_stay_usd"])

    nice_amenities_found = [
        amenity for amenity in nice_amenities if amenity.lower() in text
    ]

    prices = extract_prices(text)

    screenshot_path = None
    if email_config.get("include_screenshot", True):
        try:
            screenshot_path = take_screenshot(page, name)
        except Exception as e:
            print(f"Screenshot failed for {name}: {e}")

    print({
        "name": name,
        "has_required_bed": has_required_bed,
        "has_rejected_bed": has_rejected_bed,
        "has_unavailable_term": has_unavailable_term,
        "missing_amenity": missing_amenity,
        "under_budget": under_budget,
        "nice_amenities_found": nice_amenities_found,
        "prices_found": prices[:20],
    })

    is_match = (
        has_required_bed
        and not has_rejected_bed
        and not has_unavailable_term
        and not missing_amenity
        and under_budget
    )

    if not is_match:
        return None

    return {
        "name": name,
        "type": target.get("type", "unknown"),
        "url": url,
        "notes": target.get("notes", ""),
        "prices": prices[:20],
        "nice_amenities_found": nice_amenities_found,
        "screenshot_path": str(screenshot_path) if screenshot_path else None,
    }


def build_email_html(matches, config):
    search = config["search"]
    guests = config["guests"]
    budget = config["budget"]
    room_rules = config["room_rules"]
    amenities = config.get("amenities", {})

    cards = ""

    for m in matches:
        price_text = (
            ", ".join([f"${p:,.2f}" for p in m["prices"]])
            if m["prices"]
            else "No visible price found"
        )

        nice_amenities = (
            ", ".join(m["nice_amenities_found"])
            if m["nice_amenities_found"]
            else "None detected"
        )

        screenshot_html = ""
        encoded_screenshot = screenshot_to_base64(m.get("screenshot_path"))

        if encoded_screenshot:
            screenshot_html = f"""
            <div style="margin-top:18px;">
              <div style="font-size:13px;color:#64748b;margin-bottom:8px;">Page screenshot</div>
              <img src="data:image/png;base64,{encoded_screenshot}" style="width:100%;max-width:620px;border-radius:12px;border:1px solid #e2e8f0;" />
            </div>
            """

        cards += f"""
        <div style="margin-bottom:20px;border:1px solid #e5e7eb;border-radius:16px;padding:22px;background:#ffffff;">
          <div style="font-size:23px;font-weight:800;margin-bottom:6px;color:#0f172a;">
            {m['name']}
          </div>

          <div style="font-size:14px;color:#64748b;margin-bottom:14px;">
            {m.get('notes', '')}
          </div>

          <div style="font-size:15px;color:#334155;line-height:1.6;margin-bottom:16px;">
            <div><strong>Status:</strong> Possible match</div>
            <div><strong>Prices detected:</strong> {price_text}</div>
            <div><strong>Nice amenities detected:</strong> {nice_amenities}</div>
          </div>

          <a href="{m['url']}" style="display:inline-block;padding:13px 22px;background:#2563eb;color:white;text-decoration:none;border-radius:10px;font-weight:800;">
            Open Booking Page
          </a>

          {screenshot_html}
        </div>
        """

    hard_amenities = ", ".join(amenities.get("hard_requirements", [])) or "None"
    nice_amenities = ", ".join(amenities.get("nice_to_have", [])) or "None"
    required_beds = ", ".join(room_rules.get("required_bed_terms", []))

    html = f"""
    <!DOCTYPE html>
    <html>
    <body style="margin:0;padding:0;background:#f4f6f8;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Arial,sans-serif;">
      <table width="100%" cellpadding="0" cellspacing="0" style="background:#f4f6f8;padding:28px;">
        <tr>
          <td align="center">
            <table width="720" cellpadding="0" cellspacing="0" style="background:#ffffff;border-radius:18px;overflow:hidden;box-shadow:0 8px 28px rgba(15,23,42,.12);">

              <tr>
                <td style="background:#0f172a;padding:30px;color:white;">
                  <div style="font-size:28px;font-weight:900;line-height:1.2;">
                    Hotel Match Found
                  </div>
                  <div style="margin-top:8px;font-size:15px;color:#cbd5e1;">
                    {search['name']}
                  </div>
                </td>
              </tr>

              <tr>
                <td style="padding:26px;">
                  <div style="padding:18px;background:#f8fafc;border:1px solid #e2e8f0;border-radius:14px;margin-bottom:24px;">
                    <div style="font-size:14px;color:#64748b;margin-bottom:4px;">Stay</div>
                    <div style="font-size:18px;font-weight:800;color:#0f172a;margin-bottom:14px;">
                      {search['check_in']} → {search['check_out']}
                    </div>

                    <div style="font-size:14px;color:#64748b;margin-bottom:4px;">Guests</div>
                    <div style="font-size:18px;font-weight:800;color:#0f172a;margin-bottom:14px;">
                      {guests['adults']} adults · {guests['children']} children · {guests['rooms']} room
                    </div>

                    <div style="font-size:14px;color:#64748b;margin-bottom:4px;">Required beds</div>
                    <div style="font-size:16px;font-weight:700;color:#0f172a;margin-bottom:14px;">
                      {required_beds}
                    </div>

                    <div style="font-size:14px;color:#64748b;margin-bottom:4px;">Hard amenities</div>
                    <div style="font-size:16px;font-weight:700;color:#0f172a;margin-bottom:14px;">
                      {hard_amenities}
                    </div>

                    <div style="font-size:14px;color:#64748b;margin-bottom:4px;">Nice-to-have amenities</div>
                    <div style="font-size:16px;font-weight:700;color:#0f172a;margin-bottom:14px;">
                      {nice_amenities}
                    </div>

                    <div style="font-size:14px;color:#64748b;margin-bottom:4px;">Budget</div>
                    <div style="font-size:18px;font-weight:800;color:#0f172a;">
                      Max ${budget['max_total_stay_usd']:,.0f} total stay
                    </div>
                  </div>

                  {cards}

                  <div style="padding:18px;background:#fff7ed;border:1px solid #fed7aa;border-radius:14px;color:#7c2d12;">
                    <div style="font-weight:900;font-size:16px;margin-bottom:6px;">Action needed</div>
                    <div style="font-size:14px;line-height:1.5;">
                      This is an automated detection, not a guaranteed booking result. Open the booking page and verify bed type, dates, occupancy, taxes, fees, cancellation policy, and total price before booking.
                    </div>
                  </div>
                </td>
              </tr>

              <tr>
                <td style="padding:18px;background:#f8fafc;color:#64748b;font-size:12px;text-align:center;">
                  Generated by Hotel Checker
                </td>
              </tr>

            </table>
          </td>
        </tr>
      </table>
    </body>
    </html>
    """

    return html


def send_email(subject, html, config):
    resend_api_key = os.environ["RESEND_API_KEY"]
    from_email = os.environ["FROM_EMAIL"]
    alert_email = config["email"]["alert_email"]

    response = requests.post(
        "https://api.resend.com/emails",
        headers={
            "Authorization": f"Bearer {resend_api_key}",
            "Content-Type": "application/json",
        },
        json={
            "from": from_email,
            "to": [alert_email],
            "subject": subject,
            "html": html,
        },
        timeout=30,
    )

    if response.status_code >= 300:
        print("Email failed:", response.status_code, response.text)
        response.raise_for_status()


def main():
    config = load_config()

    if should_stop(config):
        print("Past stop time. Exiting.")
        return

    targets = build_search_targets(config)
    matches = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            viewport={"width": 1365, "height": 900},
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/125.0.0.0 Safari/537.36"
            ),
        )

        page = context.new_page()

        for target in targets:
            try:
                result = check_target(page, target, config)
                if result:
                    matches.append(result)
            except Exception as e:
                print(f"Error checking {target.get('name', 'unknown')}: {e}")

        browser.close()

    if matches:
        subject_prefix = config["email"].get("subject_prefix", "Hotel Alert")
        subject = f"{subject_prefix}: {len(matches)} possible match(es) found"
        html = build_email_html(matches, config)
        send_email(subject, html, config)
        print("Alert sent.")
    else:
        print("No matches found.")


if __name__ == "__main__":
    main()