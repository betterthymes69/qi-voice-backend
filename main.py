import os
import tempfile
import requests
import smtplib
import base64
import json
import wave
import time
import websockets
import asyncio
# ws_connect = websockets.connect
from datetime import datetime

from email.message import EmailMessage
from dotenv import load_dotenv
from fastapi import FastAPI, Request, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import PlainTextResponse

from openai import OpenAI
from websocket import create_connection
from websockets.client import connect as ws_connect
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
def fetch_call_details(call_sid: str):
    """
    Look up a call in Twilio's REST API so we can get From/To, etc.
    Returns a dict on success, or None on failure.
    """
    if not (TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN and call_sid):
        return None

    url = f"https://api.twilio.com/2010-04-01/Accounts/{TWILIO_ACCOUNT_SID}/Calls/{call_sid}.json"
    try:
        resp = requests.get(url, auth=(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN), timeout=10)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        print("Failed to fetch call details from Twilio:", e)
        return None

OPENAI_REALTIME_MODEL = os.getenv("OPENAI_REALTIME_MODEL", "gpt-4o-realtime-preview")
OPENAI_REALTIME_VOICE = os.getenv("OPENAI_REALTIME_VOICE", "verse")

if not OPENAI_API_KEY:
    raise RuntimeError("OPENAI_API_KEY is not set in environment or .env file")

QI_RECEPTIONIST_PROMPT = """
You are the AI receptionist for QuantumInnovate (“QI”), an AI & automation consulting company.

Your job is to handle inbound phone calls professionally, warmly, and efficiently.

CORE IDENTITY
- Introduce yourself on the first turn as “the QuantumInnovate virtual receptionist.”
- You are an AI assistant, not a human. You speak naturally and conversationally, but never pretend to be human.
- Your primary goal is to understand why the caller is reaching out and make it easy for a human on the QI team to follow up.

CONVERSATION STYLE
- Keep responses short and spoken-friendly: usually 1–3 sentences.
- Ask one clear question at a time.
- Use plain language. Avoid jargon unless the caller clearly understands it.
- Be warm, calm, confident, and respectful. A bit of light, professional friendliness is good; don’t be cheesy.
- Briefly acknowledge emotions (stress, confusion, excitement), then move the conversation forward.

INFORMATION TO COLLECT (WHEN RELEVANT)
- Caller name (and spelling if unclear).
- Best callback number (confirm it out loud).
- Email address (if they’re comfortable sharing).
- Company or organization (if any).
- Topic of the call (e.g., “AI automation for my business,” “website inquiry,” “billing question”).
- How urgent the request is (low / normal / high).
- Any deadlines or important context (dates, timeframes, launch windows, etc.).
- Best time to reach them (time of day and time zone, if needed).

BOUNDARIES & SAFETY
- You are NOT allowed to give medical, legal, or tax advice. If asked, gently say you’re not qualified and that you’ll pass the question along to a human.
- If the caller mentions anything life-threatening, self-harm, a crime in progress, fire, or serious medical emergency:
  - Immediately tell them you cannot help with emergencies.
  - Tell them to hang up and call 911 or their local emergency number right away.
  - Do not try to handle the emergency yourself.
- Never promise specific outcomes or timelines you can’t guarantee. Use phrases like “someone from the team will follow up” rather than naming exact times unless the caller has been told them beforehand.

CALL FLOW GUIDELINES
- Open with a concise greeting, your role, and an invitation to share why they’re calling.
- Ask focused follow-up questions to clarify:
  - What they want
  - How soon they need it
  - Whether they’ve worked with QI or AI tools before
- If they ramble, gently steer them back: for example, “Got it, thank you for that context. To make sure we’re on the same page, what’s the main thing you’d like help with right now?”
- Before ending, clearly summarize:
  - What they’re looking for
  - What information you’ve captured
  - What happens next (“I’ll pass this along to Ernie and the team; someone will reach out to you.”)

JSON SUMMARY MODE (for later automation)
If you are explicitly asked to “summarize for the CRM” or “generate JSON summary”, respond ONLY with a single JSON object and no extra words.

The JSON object MUST have exactly these keys:
- name (string or null)
- phone (string or null)
- email (string or null)
- company (string or null)
- topic (string)
- urgency ("low", "normal", or "high")
- best_time_to_reach (string or null)
- notes (string)

When generating this summary JSON, do NOT include any commentary or explanation outside of the JSON itself.
"""

client = OpenAI(api_key=OPENAI_API_KEY)

QI_RECEPTIONIST_PROMPT = """
You are the AI receptionist for QuantumInnovate (QI), an AI and automation consulting business.

Your job is to read a voicemail transcript and give Ernie a concise, helpful summary of:
- Who called (if they identify themselves)
- What they want
- How urgent it sounds
- What Ernie should do next

Tone:
- Professional but warm and human.
- No fluff. Be clear and direct.
- Assume Ernie is busy; highlight only what actually matters.

When you respond, DO NOT apologize, DO NOT say you are an AI model, and DO NOT explain your reasoning.
Just give a short summary and recommended next steps in 3–6 sentences max.
"""

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
    Main call handler for PRODUCTION QI line.

    Plays a greeting and records a voicemail.
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
        Our AI receptionist is currently in training.
        Please leave your name, number, and a brief message after the tone.
    </Say>
    <Record maxLength="120" playBeep="true"
            recordingStatusCallback="{recording_callback_url}"
            recordingStatusCallbackMethod="POST" />
</Response>
"""
    return PlainTextResponse(content=twiml, media_type="application/xml")

from typing import Optional


def summarize_voicemail(transcript_text: str) -> str:
    """
    Use an OpenAI text model to summarize the voicemail transcript
    into something you can skim quickly.
    """
    if (
        not transcript_text
        or transcript_text.startswith("(Transcription failed")
    ):
        return "Summary unavailable (missing or failed transcript)."

    system_msg = (
        "You are an assistant for QuantumInnovate (QI), an AI and automation consulting firm. "
        "You receive voicemail transcripts from prospects and clients. "
        "Your job is to summarize the voicemail for the business owner."
    )

    user_msg = f"""
Voicemail transcript:

\"\"\"{transcript_text}\"\"\"

Produce a concise summary with this structure:

- Caller name (if present, otherwise 'Unknown')
- Organization (if mentioned)
- Reason for calling (1–2 sentences)
- Priority: one of [Low, Normal, High, Urgent]
- Suggested next action for Ernie (1 sentence)
"""

    try:
        completion = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": system_msg},
                {"role": "user", "content": user_msg},
            ],
            temperature=0.3,
            max_tokens=220,
        )
        summary = completion.choices[0].message.content.strip()
        return summary or "Summary generation returned empty text."
    except Exception as e:
        print("Error while summarizing voicemail:", e)
        return f"Summary failed: {e}"


def generate_qi_receptionist_notes(transcript_text: str, meta: dict) -> str:
    """
    Use OpenAI Responses API + the QI receptionist persona
    to produce a short, helpful summary of the voicemail.
    """
    if not transcript_text:
        return ""

    # Build a single prompt string that includes transcript + key metadata
    prompt = f"""
Voicemail transcript:
\"\"\" 
{transcript_text}
\"\"\"

Metadata:
- From: {meta.get("from") or "Unknown"}
- To: {meta.get("to") or "Unknown"}
- Duration (seconds): {meta.get("duration_seconds") or "Unknown"}
- Call SID: {meta.get("call_sid") or "Unknown"}
- Recording URL: {meta.get("recording_url") or "Unknown"}

Using the instructions you were given as the QI receptionist, 
summarize this voicemail and suggest what Ernie should do next.
"""

    try:
        response = client.responses.create(
            model="gpt-4o-mini",
            instructions=QI_RECEPTIONIST_PROMPT,
            input=prompt,
        )
        # New OpenAI SDK gives you output_text convenience:
        notes = (response.output_text or "").strip()
        return notes
    except Exception as e:
        print("Error generating QI receptionist notes:", e)
        return ""


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
    duration = form.get("RecordingDuration")
    call_sid = form.get("CallSid")

    # Try to enrich with Twilio REST API (for real from/to)
    twilio_call = fetch_call_details(call_sid) if call_sid else None

    from_number = form.get("From") or (twilio_call.get("from") if twilio_call else None)
    to_number = form.get("To") or (twilio_call.get("to") if twilio_call else None)

    if not recording_url:
        raise HTTPException(status_code=400, detail="Missing RecordingUrl")

    print(f"Received recording callback for CallSid={call_sid}, URL={recording_url}")

    transcript_text = ""
    try:
        transcript_text = transcribe_recording(recording_url)
    except Exception as e:
        print("Error during transcription:", e)
        transcript_text = f"(Transcription failed: {e})"
    ai_summary = summarize_voicemail(transcript_text)

    subject = f"New QI Voicemail from {from_number or 'Unknown'}"

    import json  # ensure this is at the top of the file

    # Build structured metadata for JSON
    metadata = {
        "from": from_number,
        "to": to_number,
        "duration_seconds": duration,
        "call_sid": call_sid,
        "recording_url": recording_url,
        }
    # Generate AI receptionist notes
    ai_notes = generate_qi_receptionist_notes(transcript_text, metadata)

    # Make pretty JSON
    metadata_json = json.dumps(metadata, indent=4)

    # Build email body including JSON block
    body = (
        f"You have a new voicemail.\n\n"
        f"From: {from_number}\n"
        f"To: {to_number}\n"
        f"Call SID: {call_sid}\n"
        f"Duration: {duration} seconds\n"
        f"Recording URL (Twilio): {recording_url}\n\n"
        f"Transcript:\n{transcript_text}\n\n"
        "----- JSON METADATA -----\n"
        f"{metadata_json}\n"
        )
    if ai_notes:
        body += (
            "\n----- AI RECEPTIONIST NOTES -----\n"
            f"{ai_notes}\n"
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

@app.post("/test-qi-receptionist")
async def test_qi_receptionist():
    """
    HTTP endpoint to trigger a QI receptionist Realtime test.

    Call this with:
        curl -X POST http://127.0.0.1:8000/test-qi-receptionist

    It returns the AI's reply as JSON and also prints it to the server logs.
    """
    reply = await run_qi_receptionist_test()
    return {"reply": reply}

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

async def run_qi_receptionist_test() -> str:
    """
    Connects to OpenAI Realtime, applies the QI receptionist persona,
    sends a fake 'caller message', and returns the AI's reply text.
    This does NOT involve Twilio at all; it's a pure Realtime test.
    """
    if not OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY is not set; cannot run QI receptionist test.")

    url = "wss://api.openai.com/v1/realtime?model=gpt-4o-realtime-preview"

    headers = {
        "Authorization": f"Bearer {OPENAI_API_KEY}",
        "OpenAI-Beta": "realtime=v1",
    }

    print("Connecting to OpenAI Realtime for QI receptionist test at:", url)

    final_text_chunks: list[str] = []

    async with websockets.connect(url, extra_headers=headers) as ws:
        print("Connected to OpenAI Realtime (QI receptionist test).")

        # 1) Configure the session with our receptionist persona
        session_update = {
            "type": "session.update",
            "session": {
                "instructions": QI_RECEPTIONIST_PROMPT,
                "modalities": ["text"],  # text only for this test
                "turn_detection": None,  # we are sending a single text "turn"
                "max_response_output_tokens": 256,
            },
        }
        await ws.send(json.dumps(session_update))
        print("Sent session.update with QI receptionist persona.")

        # 2) Send a fake "caller" message as input
        test_caller_message = (
            "Hi, this is Ernie. I'm calling as a new potential client. "
            "Give me a quick friendly greeting, confirm my name, "
            "and ask one clear question about what I need help with."
        )

        response_create = {
            "type": "response.create",
            "response": {
                "input": [
                    {
                        "type": "message",
                        "role": "user",
                        "content": [
                            {
                                "type": "input_text",
                                "text": test_caller_message,
                            }
                        ],
                    }
                ],
            },
        }
        await ws.send(json.dumps(response_create))
        print("Sent response.create with test caller message.")

        # 3) Read events until we get the full reply
        while True:
            message_text = await ws.recv()
            event = json.loads(message_text)
            rt_type = event.get("type")

            if rt_type == "response.text.delta":
                # Incremental text chunks from the assistant
                delta_text = event.get("delta", "")
                final_text_chunks.append(delta_text)

            elif rt_type == "response.text.done":
                # End of the text for this response
                print("OpenAI Realtime response.text.done received.")
                break

            elif rt_type == "error":
                print("Error from OpenAI Realtime:", event)
                break

            # Other events (response.created, output_item.added, etc.) are ignored for now

    final_text = "".join(final_text_chunks).strip()
    print("AI receptionist test reply:", final_text or "(empty)")

    return final_text or "(no text reply received)"

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
    Live Twilio <-> OpenAI Realtime bridge.

    - Receives Twilio media stream events (μ-law G.711 audio).
    - Streams caller audio into OpenAI Realtime.
    - Streams Verse audio from OpenAI back to Twilio in real-time.
    - Still saves the entire call to recordings/<streamSid>.ulaw for debugging.
    """
    print("Twilio is connecting to /media-stream...")
    await websocket.accept()

    # For debugging / archive
    audio_buffer = bytearray()
    stream_sid: str | None = None
    audio_sent_to_openai = False

    # OpenAI Realtime WebSocket URL + headers
    url = "wss://api.openai.com/v1/realtime?model=gpt-4o-realtime-preview"
    headers = {
        "Authorization": f"Bearer {OPENAI_API_KEY}",
        "OpenAI-Beta": "realtime=v1",
    }

    try:
        async with ws_connect(url, extra_headers=headers) as rt_ws:
            print("Connected to OpenAI Realtime for LIVE QI receptionist.")

            # Configure the Realtime session for:
            # - audio in (g711_ulaw from Twilio)
            # - audio out (g711_ulaw back to Twilio)
            # - QI receptionist persona
            session_update = {
                "type": "session.update",
                "session": {
                    "modalities": ["audio", "text"],
                    "voice": "verse",
                    "instructions": QI_RECEPTIONIST_PROMPT,
                    "input_audio_format": "g711_ulaw",
                    "output_audio_format": "g711_ulaw",
                    "turn_detection": {
                        "type": "server_vad",
                        "threshold": 0.5,
                        "silence_duration_ms": 600,
                        "prefix_padding_ms": 300,
                        "create_response": True,
                        "interrupt_response": True,
                    },
                },
            }

            await rt_ws.send(json.dumps(session_update))
            print("Sent session.update for live call.")

            # Send an initial greeting so the caller hears Verse right away
            initial_greeting = {
                "type": "response.create",
                "response": {
                    "instructions": (
                        "Greet the caller warmly as the QuantumInnovate virtual receptionist. "
                        "Say your name is Verse, briefly state what QuantumInnovate does in plain English, "
                        "and then ask how you can help today."
                    )
                },
            }

            await rt_ws.send(json.dumps(initial_greeting))
            print("Sent initial greeting request to OpenAI.")

            async def forward_twilio_to_openai():
                nonlocal stream_sid
                try:
                    while True:
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
                                # Save to local buffer for debugging/recording
                                try:
                                    ulaw_bytes = base64.b64decode(payload_b64)
                                    audio_buffer.extend(ulaw_bytes)
                                except Exception as e:
                                    print("Failed to decode Twilio payload:", e)

                                # Forward μ-law audio chunk to OpenAI
                                try:
                                    await rt_ws.send(
                                        json.dumps(
                                            {
                                                "type": "input_audio_buffer.append",
                                                "audio": payload_b64,
                                            }
                                        )
                                    )
                                    # Mark that we've actually sent audio
                                    nonlocal audio_sent_to_openai
                                    audio_sent_to_openai = True
                                except Exception as e:
                                    print("Error sending audio to OpenAI:", e)

                        elif event_type == "stop":
                            print("Media stream stop event received:", data)

                        # Only commit if we actually sent audio; otherwise Realtime complains
                        if audio_sent_to_openai:
                            try:
                                await rt_ws.send(
                                     json.dumps(
                                         {
                                             "type": "input_audio_buffer.commit",
                                         }
                                     )
                                 )
                            except Exception as e:
                                 print("Error committing audio buffer to OpenAI:", e)
                        else:
                            print("No audio was sent to OpenAI; skipping input_audio_buffer.commit.")

                        break

                except WebSocketDisconnect:
                    print("Twilio disconnected from /media-stream")
                except Exception as e:
                    print("Error in forward_twilio_to_openai:", e)

            async def forward_openai_to_twilio():
                try:
                    async for raw in rt_ws:
                        try:
                            rt_event = json.loads(raw)
                        except Exception:
                            print("Non-JSON event from OpenAI:", raw)
                            continue

                        rt_type = rt_event.get("type")

                        # Stream Verse audio chunks back to Twilio
                        if rt_type == "response.audio.delta":
                            delta_b64 = rt_event.get("delta")
                            if delta_b64 and stream_sid:
                                twilio_msg = {
                                    "event": "media",
                                    "streamSid": stream_sid,
                                    "media": {
                                        "payload": delta_b64,
                                    },
                                }
                                try:
                                    await websocket.send_text(json.dumps(twilio_msg))
                                except Exception as e:
                                    print("Error sending media back to Twilio:", e)

                        elif rt_type == "response.audio.done":
                            # One spoken reply finished - Twilio just keeps playing
                            pass

                        elif rt_type == "error":
                            print("Error from OpenAI Realtime (live):", rt_event)

                        # Optional: log text for debugging
                        elif rt_type == "response.text.delta":
                            delta_text = rt_event.get("delta", "")
                            if delta_text:
                                print("AI (text delta):", delta_text, flush=True)

                        elif rt_type == "response.text.done":
                            full_text = ""
                            # Some SDKs deliver the final text here; our logging above
                            # already prints deltas, so this is just a hook.
                            print("AI finished a text response.")

                except Exception as e:
                    print("Error in forward_openai_to_twilio:", e)

            # Run both directions concurrently
            await asyncio.gather(
                forward_twilio_to_openai(),
                forward_openai_to_twilio(),
            )

    finally:
        # Save the raw μ-law audio to a file for debugging
        if audio_buffer and stream_sid:
            try:
                os.makedirs("recordings", exist_ok=True)
                path = os.path.join("recordings", f"{stream_sid}.ulaw")
                with open(path, "wb") as f:
                    f.write(audio_buffer)
                print(f"Saved media stream raw μ-law audio to {path}")
            except Exception as e:
                print("Failed to save media stream audio:", e)

        try:
            await websocket.close()
        except Exception:
            pass

        print("Media stream closed.")
