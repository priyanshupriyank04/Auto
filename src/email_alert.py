import os
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import logging
from dotenv import load_dotenv

logger = logging.getLogger(__name__)

# Ensure env is loaded
load_dotenv("secrets/.env")

def send_email_alert(subject: str, body: str) -> bool:
    """Send an email alert using SMTP."""
    smtp_server = os.getenv("SMTP_SERVER", "smtp.gmail.com")
    smtp_port = int(os.getenv("SMTP_PORT", 587))
    sender_email = os.getenv("SMTP_EMAIL")
    sender_password = os.getenv("SMTP_PASSWORD")
    receiver_email = os.getenv("ALERT_RECEIVER_EMAIL")
    
    if not sender_email or not sender_password or not receiver_email:
        logger.warning("Missing SMTP credentials in secrets/.env. Skipping email alert.")
        return False
        
    msg = MIMEMultipart()
    msg['From'] = sender_email
    msg['To'] = receiver_email
    msg['Subject'] = f"🚨 [Breakout Bot] {subject}"
    
    # Prepend basic timestamp to body
    from datetime import datetime
    from zoneinfo import ZoneInfo
    ist_now = datetime.now(ZoneInfo("Asia/Kolkata")).strftime("%Y-%m-%d %H:%M:%S IST")
    full_body = f"Time: {ist_now}\n\n{body}"
    
    msg.attach(MIMEText(full_body, 'plain'))
    
    try:
        server = smtplib.SMTP(smtp_server, smtp_port)
        server.starttls()
        server.login(sender_email, sender_password)
        server.send_message(msg)
        server.quit()
        logger.info(f"Email alert sent successfully: {subject}")
        return True
    except Exception as e:
        logger.error(f"Failed to send email alert: {e}")
        return False
