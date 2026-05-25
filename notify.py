"""Email notifications via Brevo SMTP. Matches the setformoney.com pattern.

Two notification types:
- send_failure_alert(subject, body_text): ASAP on batch failure
- send_daily_cost_summary(date_iso): tallies cost_log.csv for the date and emails total
"""
import datetime
import os
import pathlib
import smtplib
import ssl
from email.message import EmailMessage

import pandas as pd
from dotenv import load_dotenv

ROOT = pathlib.Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")


def can_send() -> bool:
    return bool(os.environ.get("SMTP_USER") and os.environ.get("SMTP_PASS") and os.environ.get("ALERT_EMAIL_TO"))


def _send(subject: str, body_html: str, body_text: str = "") -> None:
    if not can_send():
        print(f"[notify] SMTP not configured — would have sent: {subject}")
        return

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = os.environ.get("EMAIL_FROM", "hello@setformoney.com")
    msg["To"] = os.environ["ALERT_EMAIL_TO"]
    msg.set_content(body_text or _html_to_plain(body_html))
    msg.add_alternative(body_html, subtype="html")

    host = os.environ.get("SMTP_HOST", "smtp-relay.brevo.com")
    port = int(os.environ.get("SMTP_PORT", "587"))
    context = ssl.create_default_context()
    with smtplib.SMTP(host, port, timeout=30) as s:
        s.ehlo()
        s.starttls(context=context)
        s.login(os.environ["SMTP_USER"], os.environ["SMTP_PASS"])
        s.send_message(msg)


def _html_to_plain(html: str) -> str:
    import re
    text = re.sub(r"<[^>]+>", "", html)
    text = re.sub(r"\s+\n", "\n", text)
    return text.strip()


def _email_layout(body_inner_html: str) -> str:
    """Branded wrapper. Plain editorial style matching the admin UI vibe."""
    return f"""
    <div style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; max-width: 540px; margin: 0 auto; padding: 32px 20px; background: #FAF6EC; color: #3A4451;">
      <div style="text-align: left; margin-bottom: 24px;">
        <div style="display: inline-block; width: 40px; height: 40px; background: rgba(135,168,120,0.15); border-radius: 10px; text-align: center; line-height: 40px;">
          <span style="font-family: Georgia, serif; font-weight: 600; color: #87A878; font-size: 18px;">fb</span>
        </div>
        <span style="display: inline-block; margin-left: 10px; font-family: Georgia, serif; font-weight: 600; font-size: 16px; color: #3A4451; vertical-align: 8px;">fb-content-engine</span>
      </div>
      <div style="background: #fff; border: 1px solid #E8E0CD; border-radius: 12px; padding: 24px;">
        {body_inner_html}
      </div>
      <p style="font-size: 11px; color: #8a8678; text-align: center; margin-top: 18px;">Automated by the fb-content-engine pipeline.</p>
    </div>
    """


def send_failure_alert(stage: str, error_text: str, niche: str = "", extra_context: str = "") -> None:
    """Fires ASAP when a batch run exits non-zero. stage = "scrape" | "generate" | "images" | "unknown"."""
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    inner = f"""
      <h2 style="font-family: Georgia, serif; font-size: 22px; color: #B91C1C; margin: 0 0 12px 0;">Batch failed: {stage}</h2>
      <p style="color: #5A6472; font-size: 14px; line-height: 1.5;">A run of <code>run_batch.py</code> exited with an error at <strong>{ts}</strong>{(" for niche <strong>" + niche + "</strong>") if niche else ""}.</p>
      <p style="font-family: ui-monospace, monospace; font-size: 12px; color: #3A4451; background: #FAF6EC; padding: 12px; border-radius: 6px; border-left: 3px solid #B91C1C; overflow-x: auto;">{error_text}</p>
      {f'<p style="color: #5A6472; font-size: 13px;">{extra_context}</p>' if extra_context else ''}
      <p style="color: #8a8678; font-size: 12px; margin-top: 18px;">Check VPS logs at <code>/home/delicto/apps/fb-content-engine/logs/</code>.</p>
    """
    _send(f"[fb-content-engine] Batch failed: {stage}", _email_layout(inner))


def send_daily_cost_summary(date_iso: str = None) -> None:
    """Reads cost_log.csv, filters for the given date, emails a breakdown.
    If date_iso is None, uses today's date in the server's local timezone."""
    if date_iso is None:
        date_iso = datetime.date.today().isoformat()

    log_path = ROOT / "cost_log.csv"
    if not log_path.exists():
        _send(
            f"[fb-content-engine] No cost data for {date_iso}",
            _email_layout(f'<p style="color: #5A6472;">No <code>cost_log.csv</code> file exists yet. Either no batches have run, or the file path is wrong.</p>'),
        )
        return

    df = pd.read_csv(log_path)
    df["date"] = df["timestamp"].str[:10]
    day_df = df[df["date"] == date_iso]

    if day_df.empty:
        inner = f"""
          <h2 style="font-family: Georgia, serif; font-size: 22px; color: #C4A86B; margin: 0 0 12px 0;">No batches ran on {date_iso}</h2>
          <p style="color: #5A6472; font-size: 14px;">If a batch was expected today, check the cron logs on the VPS.</p>
        """
        _send(f"[fb-content-engine] No spend on {date_iso}", _email_layout(inner))
        return

    apify_total = day_df["apify_usd"].astype(float).sum()
    anthropic_total = day_df["anthropic_usd"].astype(float).sum()
    kie_total = day_df["kie_usd"].astype(float).sum()
    total = day_df["total_usd"].astype(float).sum()
    batches = len(day_df)
    a_input = day_df["anthropic_input_tokens"].astype(int).sum()
    a_output = day_df["anthropic_output_tokens"].astype(int).sum()
    a_search = day_df["anthropic_web_searches"].astype(int).sum()
    posts_scraped = day_df["apify_posts"].astype(int).sum()
    images = day_df["kie_images"].astype(int).sum()

    rows_html = ""
    for _, r in day_df.iterrows():
        rows_html += f"""
          <tr>
            <td style="padding: 6px 8px; border-bottom: 1px solid #E8E0CD; color: #5A6472; font-size: 12px;">{r['timestamp'][11:16]}</td>
            <td style="padding: 6px 8px; border-bottom: 1px solid #E8E0CD; color: #3A4451; font-size: 12px;">{r['niche']}</td>
            <td style="padding: 6px 8px; border-bottom: 1px solid #E8E0CD; color: #5A6472; font-size: 12px;">{'cache' if r['scrape_used_cache']=='yes' else 'fresh'}</td>
            <td style="padding: 6px 8px; border-bottom: 1px solid #E8E0CD; color: #3A4451; font-size: 12px; text-align: right;">${float(r['total_usd']):.4f}</td>
          </tr>
        """

    inner = f"""
      <h2 style="font-family: Georgia, serif; font-size: 22px; color: #3A4451; margin: 0 0 4px 0;">Daily spend — {date_iso}</h2>
      <p style="color: #8a8678; font-size: 13px; margin: 0 0 20px 0;">{batches} batch{'es' if batches != 1 else ''} ran today</p>

      <div style="background: #FAF6EC; border-radius: 8px; padding: 16px; margin-bottom: 18px;">
        <div style="font-family: Georgia, serif; font-size: 32px; font-weight: 600; color: #87A878;">${total:.4f}</div>
        <div style="font-size: 12px; color: #8a8678; margin-top: 4px;">total today</div>
      </div>

      <table style="width: 100%; border-collapse: collapse; margin-bottom: 18px;">
        <tr>
          <td style="padding: 8px 0; color: #5A6472; font-size: 13px;">Apify scrapes</td>
          <td style="padding: 8px 0; text-align: right; color: #3A4451; font-size: 13px; font-weight: 500;">${apify_total:.4f}</td>
          <td style="padding: 8px 0 8px 12px; color: #8a8678; font-size: 11px; text-align: right;">{posts_scraped:,} posts</td>
        </tr>
        <tr>
          <td style="padding: 8px 0; color: #5A6472; font-size: 13px;">Anthropic (text)</td>
          <td style="padding: 8px 0; text-align: right; color: #3A4451; font-size: 13px; font-weight: 500;">${anthropic_total:.4f}</td>
          <td style="padding: 8px 0 8px 12px; color: #8a8678; font-size: 11px; text-align: right;">{a_input:,} in / {a_output:,} out / {a_search} searches</td>
        </tr>
        <tr>
          <td style="padding: 8px 0; color: #5A6472; font-size: 13px;">Kie.ai (images)</td>
          <td style="padding: 8px 0; text-align: right; color: #3A4451; font-size: 13px; font-weight: 500;">${kie_total:.4f}</td>
          <td style="padding: 8px 0 8px 12px; color: #8a8678; font-size: 11px; text-align: right;">{images} images</td>
        </tr>
      </table>

      <h3 style="font-family: Georgia, serif; font-size: 14px; color: #5A6472; margin: 16px 0 8px 0;">Per-batch breakdown</h3>
      <table style="width: 100%; border-collapse: collapse; font-size: 12px;">
        <thead><tr><th style="padding: 6px 8px; text-align: left; color: #8a8678; font-size: 11px; text-transform: uppercase; letter-spacing: 0.5px;">Time</th><th style="padding: 6px 8px; text-align: left; color: #8a8678; font-size: 11px;">Niche</th><th style="padding: 6px 8px; text-align: left; color: #8a8678; font-size: 11px;">Scrape</th><th style="padding: 6px 8px; text-align: right; color: #8a8678; font-size: 11px;">Cost</th></tr></thead>
        <tbody>{rows_html}</tbody>
      </table>
    """
    _send(f"[fb-content-engine] Daily spend — {date_iso} — ${total:.4f}", _email_layout(inner))


# Allow CLI invocation: python -m notify daily | python -m notify test-fail
if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("Usage: python -m notify [daily|test-fail|test-summary]")
        sys.exit(1)
    cmd = sys.argv[1]
    if cmd == "daily":
        send_daily_cost_summary()
    elif cmd == "test-fail":
        send_failure_alert("test", "This is a test failure alert. If you receive this, Brevo SMTP is wired correctly.", niche="kenji-mori-retirement")
        print("Test failure alert sent.")
    elif cmd == "test-summary":
        send_daily_cost_summary()
        print("Test summary sent.")
    else:
        print(f"Unknown command: {cmd}")
        sys.exit(1)
