"""Fillers while a real tool call runs. Owner: builder 'converse'.

The lines are the owner-approved Connect line fillers
(infrastructure/phone-test-line/lex-texts.json, "start"; keep in sync,
add the other languages from that file). Booqy-authored, never the model's.

FillerPolicy (pure): a filler is played only when
  * a ToolStarted arrived for a tool other than end_call and
    transfer_to_staff (those say their own fixed sentence),
  * no assistant audio is playing or queued and none arrived within
    SpeakConfig.filler_delay_ms after the tool started,
  * no filler in the last filler_cooldown_ms, at most one per turn,
  * the clip exists in the ClipCache for the call language.
Rotation: next index per language, never the same line twice in a row.
"""

from __future__ import annotations

from voice_gateway.config import SpeakConfig
from voice_gateway.events import ToolStarted

FILLERS: dict[str, tuple[str, ...]] = {
    "sv": ("Ett ögonblick, jag kollar.", "Absolut, jag tittar på det.", "Jag kollar det direkt."),
    "en": ("One moment, let me check.", "Of course, I am looking into it.", "I will check that right away."),
}
NO_FILLER_TOOLS = frozenset({"end_call", "transfer_to_staff"})


class FillerPolicy:
    def __init__(self, cfg: SpeakConfig) -> None:
        self.cfg = cfg

    def on_tool_started(self, ev: ToolStarted) -> None:
        raise NotImplementedError

    def on_assistant_audio(self, now_ms: int) -> None:
        """Assistant audio was queued or started: cancels a pending filler."""
        raise NotImplementedError

    def due(self, now_ms: int, language: str, *, assistant_busy: bool) -> int | None:
        """The filler index to play now, or None."""
        raise NotImplementedError

    def new_turn(self) -> None:
        raise NotImplementedError
