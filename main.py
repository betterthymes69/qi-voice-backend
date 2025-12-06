import os
import tempfile
import requests
import smtplib
import base64
import json
import wave
import time
import websockets
ws_connect = websockets.connect
from datetime import datetime

from email.message import EmailMessage
from dotenv import load_dotenv
from fastapi import FastAPI, Request, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import PlainTextResponse

from openai import OpenAI
from websocket import create_connection
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

OPENAI_REALTIME_MODEL = os.getenv("OPENAI_REALTIME_MODEL", "gpt-4o-realtime-preview")
OPENAI_REALTIME_VOICE = os.getenv("OPENAI_REALTIME_VOICE", "verse")

if not OPENAI_API_KEY:
    raise RuntimeError("OPENAI_API_KEY is not set in environment or .env file")

client = OpenAI(api_key=OPENAI_API_KEY)

app = FastAPI()

def test_openai_realtime():
    """
    Simple test: connect to OpenAI Realtime, send a request,
    and print messages we get back.
    """
    model = "gpt-4o-realtime-preview"  # adjust if needed
    url = f"wss://api.openai.com/v1/realtime?model={model}"

    headers = [
        f"Authorization: Bearer {OPENAI_API_KEY}",
        "OpenAI-Beta: realtime=v1",
    ]

    print("Connecting to OpenAI Realtime at:", url)

    # Blocking WebSocket connection (fine for a test endpoint)
    ws = create_connection(url, header=headers)
    print("Connected to OpenAI Realtime.")

    try:
        # Ask the model to say hi to you
        event = {
            "type": "response.create",
            "response": {
                "instructions": (
                    "You are QuantumInnovate's AI phone brain. "
                    "Say a short, friendly greeting to Ernie in one sentence."
                )
            },
        }

        ws.send(json.dumps(event))
        print("Sent response.create event to OpenAI Realtime.")

        # Read messages until the response is complete
        while True:
            message = ws.recv()
            data = json.loads(message)
            print("From OpenAI Realtime:", data)

            if data.get("type") == "response.completed":
                print("OpenAI Realtime response completed.")
                break

    finally:
        ws.close()
        print("Closed OpenAI Realtime WebSocket.")

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

@app.post("/test-realtime")
def test_realtime_endpoint():
    """
    HTTP endpoint to trigger a simple OpenAI Realtime test.
    Hit this with curl to verify connectivity.
    """
    test_openai_realtime()
    return PlainTextResponse(
        content="Realtime test finished. Check server logs.",
        media_type="text/plain",
    )

@app.post("/test-realtime-from-file")
async def test_realtime_from_file():
    """
    Test endpoint: take the latest .ulaw recording from Twilio
    and send it through OpenAI Realtime for transcription.
    """
    path = get_latest_ulaw_recording()
    if not path:
        raise HTTPException(status_code=404, detail="No .ulaw recordings found in recordings/")

    transcript = await transcribe_ulaw_with_realtime(path)

    return {
        "file": path,
        "transcript": transcript,
    }

@app.post("/incoming-stream-test")
async def incoming_stream_test(request: Request):
    """
    Twilio hits this endpoint when a call comes in (for streaming tests).
    We respond with TwiML that tells Twilio to open a media stream
    to our /media-stream WebSocket.
    """
    ngrok_base = os.getenv("NGROK_BASE_URL", "").rstrip("/")
    if not ngrok_base:
        print("WARNING: NGROK_BASE_URL is not set; cannot build WSS stream URL correctly.")
        wss_url = "wss://example.com/media-stream"
    else:
        # Convert https://... to wss://... and append /media-stream
        if ngrok_base.startswith("https://"):
            wss_url = "wss://" + ngrok_base[len("https://"):] + "/media-stream"
        else:
            cleaned = ngrok_base.replace("http://", "").replace("https://", "")
            wss_url = f"wss://{cleaned}/media-stream"

    print("Using WSS stream URL:", wss_url)

    twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Connect>
        <Stream url="{wss_url}" />
    </Connect>
</Response>
"""
    return PlainTextResponse(content=twiml, media_type="application/xml")

import glob

def get_latest_ulaw_recording() -> str | None:
    """
    Find the most recent .ulaw file in the recordings/ directory.
    Returns the file path or None if no files found.
    """
    os.makedirs("recordings", exist_ok=True)
    paths = glob.glob("recordings/*.ulaw")
    if not paths:
        return None
    # Sort by modification time (newest last)
    paths.sort(key=os.path.getmtime)
    return paths[-1]

import json

async def transcribe_ulaw_with_realtime(path: str) -> str:
    """
    Send a saved μ-law audio file to OpenAI Realtime and return the transcript.
    """
    # Read the raw μ-law bytes from disk
    with open(path, "rb") as f:
        ulaw_bytes = f.read()

    # Base64-encode for Realtime input_audio_buffer.append
    import base64
    ulaw_b64 = base64.b64encode(ulaw_bytes).decode("ascii")

    url = "wss://api.openai.com/v1/realtime?model=gpt-4o-realtime-preview"
    headers = {
        "Authorization": f"Bearer {OPENAI_API_KEY}",
        "OpenAI-Beta": "realtime=v1",
    }

    transcript_chunks: list[str] = []

    import json
    import websockets

    async with websockets.connect(url, extra_headers=headers) as ws:
        print("Connected to OpenAI Realtime (file transcription).")

        # Configure the session for μ-law input and text-only transcription
        session_update = {
            "type": "session.update",
            "session": {
                # We only care about text output for this test
                "modalities": ["text"],
                # Turn detection off for offline file transcription
                "turn_detection": None,
                # Tell Realtime we're sending μ-law audio
                "input_audio_format": "g711_ulaw",
                # We don't need audio output here, but we set something valid
                "output_audio_format": "g711_ulaw",
                # Ask Whisper to transcribe the input audio
                "input_audio_transcription": {
                    "model": "whisper-1"
                },
                "instructions": (
                    "You are transcribing a phone call recording. "
                    "Return only the transcript text, no commentary."
                ),
            },
        }

        await ws.send(json.dumps(session_update))
        print("Sent session.update to configure Realtime.")

        # Send the μ-law audio as a single buffer
        await ws.send(
            json.dumps(
                {
                    "type": "input_audio_buffer.append",
                    "audio": ulaw_b64,
                }
            )
        )
        print("Sent input_audio_buffer.append with ulaw data.")

        # Commit the buffer so the model knows the audio is complete
        await ws.send(json.dumps({"type": "input_audio_buffer.commit"}))
        print("Sent input_audio_buffer.commit.")

        # Ask the model to create a response (transcription)
        await ws.send(json.dumps({"type": "response.create"}))
        print("Sent response.create.")

        # Listen for events until we see response.done
        while True:
            msg = await ws.recv()
            event = json.loads(msg)

            event_type = event.get("type")
            print("From OpenAI (file):", event_type, event)

            if event_type == "error":
                print("Realtime error event:", event)

            # Text streaming events – in your logs, delta is a plain string
            if event_type == "response.text.delta":
                delta = event.get("delta")
                if isinstance(delta, str) and delta:
                    transcript_chunks.append(delta)

            # Done with the response
            if event_type == "response.done":
                break

    transcript = "".join(transcript_chunks).strip()
    print("Final transcript from Realtime:", transcript or "(empty)")
    return transcript or "(no transcript received)"

def clean_ai_text(s: str) -> str:
    """
    Strip any leading JSON-ish metadata the model might prepend,
    e.g. '{"name": "Ernie"}Hello there...' -> 'Hello there...'
    """
    if not s:
        return s

    s = s.strip()

    # If it starts with '{' and contains '}', treat that as a metadata block
    if s.startswith("{"):
        close_idx = s.find("}")
        if close_idx != -1 and close_idx + 1 < len(s):
            return s[close_idx + 1 :].lstrip()

    return s

@app.websocket("/media-stream")
async def media_stream(websocket: WebSocket):
    """
    Twilio <Stream> handler.

    - Receives JSON text frames from Twilio's Media Streams
    - Buffers μ-law audio from "media" events
    - On "stop", writes the raw audio to recordings/<streamSid>.ulaw
    """
    await websocket.accept()
    print("Twilio is connecting to /media-stream...")

    stream_sid = None
    audio_buffer = bytearray()

    try:
        while True:
            # Twilio sends JSON text frames
            message_text = await websocket.receive_text()
            data = json.loads(message_text)
            event_type = data.get("event")

            if event_type == "connected":
                print("Media stream connected:", data)

            elif event_type == "start":
                stream_sid = data.get("start", {}).get("streamSid")
                print(f"Media stream started. streamSid={stream_sid}")

            elif event_type == "media":
                media = data.get("media", {})
                payload_b64 = media.get("payload")
                if payload_b64:
                    # Base64 → μ-law bytes
                    ulaw_bytes = base64.b64decode(payload_b64)
                    audio_buffer.extend(ulaw_bytes)

            elif event_type == "stop":
                print("Media stream stop event received:", data)
                # End of this call's stream
                break

            else:
                # Other Twilio events we don't care about right now
                pass

    except WebSocketDisconnect:
        print("Twilio disconnected from /media-stream")

    # After the stream ends, persist audio to disk
    if not stream_sid:
        stream_sid = "unknown"

    os.makedirs("recordings", exist_ok=True)
    file_path = os.path.join("recordings", f"{stream_sid}.ulaw")
    with open(file_path, "wb") as f:
        f.write(audio_buffer)

    print(f"Saved media stream raw μ-law audio to {file_path}")

    # NEW: Transcribe the streamed call with OpenAI Realtime and email it
    try:
        transcript = await transcribe_ulaw_with_realtime(file_path)
        print("AI transcript:", transcript)

        subject = f"New QI streamed call ({stream_sid})"
        body = (
            f"Stream SID: {stream_sid}\n"
            f"File: {file_path}\n\n"
            f"Transcript:\n{transcript}\n"
        )
        send_email(subject, body)

    except Exception as e:
        print("Error transcribing streamed call:", e)
