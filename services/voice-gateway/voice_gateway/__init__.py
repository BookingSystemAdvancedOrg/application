"""Booqy voice gateway (Stage 2 of the phone agent, see ../DESIGN.md).

A streaming voice front for the EXISTING phone agent: caller audio in,
VAD + Amazon Transcribe streaming + turn and barge-in detection, the same
agent loop and tools (shared/features/phone_agent) streamed sentence by
sentence, Amazon Polly out, cheap rule based backchannels. Only AWS billed
services; open-source libraries run on CPU in the container.

Package layout and file ownership: DESIGN.md section 3.
"""

__version__ = "0.1.0"
