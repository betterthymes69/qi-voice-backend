# QI Voice Backend

This service powers the QuantumInnovate phone line (currently +1 413-366-3330).

It handles:
- Incoming calls from Twilio
- Playing a greeting
- Recording voicemails
- Receiving recording callbacks from Twilio
- Downloading recordings from Twilio
- Transcribing audio using OpenAI Whisper (`whisper-1`)
- Emailing voicemail details + transcript to me

## Tech Stack

- Python 3
- FastAPI
- Uvicorn
- Twilio Voice Webhooks
- OpenAI Whisper (`whisper-1`) for transcription
- SMTP (Gmail) for outgoing email
- ngrok for local HTTPS tunneling during development

## Endpoints

- `POST /incoming-call`
  - Called by Twilio when a call comes in.
  - Returns TwiML:
    - Plays a greeting
    - Starts a `<Record>` operation
    - Sets `recordingStatusCallback` to `/recording-complete`.

- `POST /recording-complete`
  - Called by Twilio when a voicemail recording finishes.
  - Reads `RecordingUrl`, `From`, `To`, `RecordingDuration`, `CallSid`.
  - Downloads the recording from Twilio using Account SID + Auth Token.
  - Sends the audio to OpenAI Whisper (`whisper-1`) for transcription.
  - Emails the caller info + transcript + Twilio recording URL.

- `POST /fallback-voice`
  - Called if the main handler fails.
  - Plays a generic apology message and hangs up.

- `POST /call-status`
  - Receives Twilio call lifecycle webhooks.
  - Currently just responds 200 OK.

## Environment Variables

See `.env.example` for required configuration.

Key vars:
- `OPENAI_API_KEY`
- `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`
- `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASS`
- `VOICEMAIL_TO`
- `NGROK_BASE_URL` (set each time ngrok starts)

## Running Locally (Dev)

```bash
# 1. Create and activate venv
python3 -m venv venv
source venv/bin/activate

# 2. Install dependencies
pip install -r requirements.txt  # (optional if you build this later)
# or:
pip install fastapi uvicorn requests openai python-dotenv python-multipart

# 3. Start FastAPI
export NGROK_BASE_URL="https://your-ngrok-url.ngrok-free.dev"
uvicorn main:app --host 0.0.0.0 --port 8000

# 4. In another terminal, start ngrok
ngrok http 8000
