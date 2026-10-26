"""Voice questions: the device records a short clip, streams it in small chunks, the backend builds a WAV
and asks the configured provider's speech model for the text (docs/VOICE_INPUT.md). Audio lives in memory
for at most a minute and is deleted after the transcription; nothing is written to disk."""
