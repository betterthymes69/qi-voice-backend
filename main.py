import os
import tempfile
import requests
import smtplib

from email.message import EmailMessage
from dotenv import load_dotenv
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import PlainTextResponse

from openai import OpenAI

# Load environment variables from .env
load_dotenv()

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
SMTP_HOST = os.getenv("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER")
SMTP_PASS = os.getenv("SMTP_PASS")
VOICEMAIL_TO = os.getenv("VOICEMAIL_TO")
TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")

if not OPENAI_API_KEY:
    raise RuntimeError("OPENAI_API_KEY is not set in environment or .env file")

client = OpenAI(api_key=OPENAI_API_KEY)

app = FastAPI()


def send_email(subject: str, body: str):
    """Send a plain-text email with the given subject and body."""
    if not (SMTP_HOST and SMTP_PORT and SMTP_USER and SMTP_PASS and VOICEMAIL_TO):
        print("Email not configured properly; skipping email send.")
        print("Subject:", subject)
        print("Body:", body)
        return

    msg = EmailMessage()
    msg["From"] = SMTP_USER
    msg["To"] = VOICEMAIL_TO
    msg["Subject"] = subject
    msg.set_content(body)

    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
            server.starttls()
            server.login(SMTP_USER, SMTP_PASS)
            server.send_message(msg)
        print("Email sent successfully.")
    except Exception as e:
        print("Failed to send email:", e)


def transcribe_recording(recording_url: str) -> str:
    """
    Download the Twilio recording and send it to OpenAI Whisper
    for transcription. Returns the transcript text.
    """
    # Twilio recording URLs need an extension (e.g., .mp3)
    audio_url = recording_url + ".mp3"
    print(f"Downloading recording from: {audio_url}")
    resp = requests.get(audio_url, auth=(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN))
    resp.raise_for_status()

    # Save to a temporary file for OpenAI
    with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as tmp:
        tmp.write(resp.content)
        tmp.flush()
        tmp_path = tmp.name

    transcript_text = ""
    try:
        with open(tmp_path, "rb") as f:
            transcription = client.audio.transcriptions.create(
                model="whisper-1",
                file=f,
            )
        transcript_text = transcription.text
        print("Transcription complete.")
    finally:
        try:
            os.remove(tmp_path)
        except OSError:
            pass

    return transcript_text or "(No transcript text returned.)"


@app.post("/incoming-call")
async def incoming_call(request: Request):
    """
    Main call handler. Plays a greeting and records a voicemail.
    When the recording is complete, Twilio will POST to /recording-complete.
    """
    # IMPORTANT: update this base URL whenever ngrok URL changes
    ngrok_base = os.getenv("NGROK_BASE_URL", "").rstrip("/")
    if not ngrok_base:
        print("WARNING: NGROK_BASE_URL is not set; recording callback will not work correctly.")
        recording_callback_url = ""
    else:
        recording_callback_url = f"{ngrok_base}/recording-complete"

    # Basic Twilio response to prove the loop works
    # and to trigger recording + callback
    twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Say voice="alice">
        Thank you for calling QuantumInnovate.
        This line is currently in development as our new A I receptionist.
        Please leave a message after the tone, and we will get back to you.
    </Say>
    <Record maxLength="120" playBeep="true"
            recordingStatusCallback="{recording_callback_url}"
            recordingStatusCallbackMethod="POST" />
</Response>
"""
    return PlainTextResponse(content=twiml, media_type="application/xml")


@app.post("/recording-complete")
async def recording_complete(request: Request):
    """
    Called by Twilio when the voicemail recording is finished.
    We:
      - get the RecordingUrl, From, To, and duration
      - download and transcribe the audio
      - email the transcript and details
    """
    form = await request.form()
    recording_url = form.get("RecordingUrl")
    from_number = form.get("From")
    to_number = form.get("To")
    duration = form.get("RecordingDuration")
    call_sid = form.get("CallSid")

    if not recording_url:
        raise HTTPException(status_code=400, detail="Missing RecordingUrl")

    print(f"Received recording callback for CallSid={call_sid}, URL={recording_url}")

    transcript_text = ""
    try:
        transcript_text = transcribe_recording(recording_url)
    except Exception as e:
        print("Error during transcription:", e)
        transcript_text = f"(Transcription failed: {e})"

    subject = f"New QI Voicemail from {from_number or 'Unknown'}"
    body = (
        f"You have a new voicemail.\n\n"
        f"From: {from_number}\n"
        f"To: {to_number}\n"
        f"Call SID: {call_sid}\n"
        f"Duration: {duration} seconds\n"
        f"Recording URL (Twilio): {recording_url}\n\n"
        f"Transcript:\n{transcript_text}\n"
    )

    send_email(subject, body)

    # Twilio expects a 200 OK with valid TwiML or empty body here
    # We'll just acknowledge with a simple response.
    twiml = """<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Say voice="alice">Thank you. Your message has been received.</Say>
</Response>
"""
    return PlainTextResponse(content=twiml, media_type="application/xml")


@app.post("/fallback-voice")
async def fallback_voice(request: Request):
    # Called if the primary handler fails
    twiml = """<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Say voice="alice">
        We are sorry. We are unable to process your call at this time.
        Please try again later.
    </Say>
    <Hangup/>
</Response>
"""
    return PlainTextResponse(content=twiml, media_type="application/xml")


@app.post("/call-status")
async def call_status(request: Request):
    # Twilio sends call lifecycle events here.
    # For now we just return 200 OK.
    return PlainTextResponse(content="OK", media_type="text/plain")
