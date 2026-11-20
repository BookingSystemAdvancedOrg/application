"""Fillers while a real tool call runs. Owner: builder 'converse'.

The lines are the owner-approved Connect line fillers
(infrastructure/phone-test-line/lex-texts.json, "start"; keep in sync,
add the other languages from that file). Booqy-authored, never the model's.
(2026-11-20: lex-texts.json is not in the infrastructure repo yet; the
lines below are the ADR-L wording "Ett ögonblick, jag kollar". Languages
without a row get no filler: silence beats a wrong-language line, and the
model's own preamble usually covers the gap.)

FillerPolicy (pure): a filler is played only when
  * a ToolStarted arrived for a tool other than end_call and
    transfer_to_staff (those say their own fixed sentence),
  * no assistant audio is playing or queued and none arrived within
    SpeakConfig.filler_delay_ms after the tool started,
  * no filler in the last filler_cooldown_ms, at most one per turn,
  * the clip exists in the ClipCache for the call language.
Rotation: next index per language, never the same line twice in a row.
The clip check is the mixer's (request_filler returns False without one).

Measured as silence on the line: the filler waits until the line has been
quiet for filler_delay_ms since the later of the tool start and the end of
the last assistant audio. When the model already said its own preamble in
this turn ("Då kollar jag."), the silence must be twice as long: right
after "Då kollar jag" a "Ett ögonblick, jag kollar" sounds like a robot, a
slow tool after a long quiet moment does not. The pending filler is
dropped when assistant audio arrives after the tool finished (that is the
answer), and when the turn is done.
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
        self.pending_since: int | None = None     # the tool start a filler may cover
        self.played_this_turn = False
        self.spoke_this_turn = False               # assistant audio in this turn (a preamble)
        self.last_audio_ms: int | None = None      # last moment assistant audio was queued or playing
        self.tool_done_ms: int | None = None
        self.last_ms: int | None = None
        self._last_index: dict[str, int] = {}
        self._prev_last_ms: int | None = None

    def on_tool_started(self, ev: ToolStarted) -> None:
        if ev.name in NO_FILLER_TOOLS:
            self.pending_since = None
            return
        self.tool_done_ms = None
        if not self.played_this_turn and self.pending_since is None:
            self.pending_since = ev.at_ms

    def on_tool_finished(self, at_ms: int) -> None:
        self.tool_done_ms = at_ms

    def on_assistant_audio(self, now_ms: int) -> None:
        """Assistant audio was queued or started: the line is not quiet; after
        the tool finished it is the answer, which cancels a pending filler."""
        self.spoke_this_turn = True
        self.last_audio_ms = now_ms if self.last_audio_ms is None else max(self.last_audio_ms, now_ms)
        if self.pending_since is not None and self.tool_done_ms is not None and now_ms >= self.tool_done_ms:
            self.pending_since = None

    def due(self, now_ms: int, language: str, *, assistant_busy: bool) -> int | None:
        """The filler index to play now, or None."""
        if self.pending_since is None or self.played_this_turn:
            return None
        if assistant_busy:
            self.last_audio_ms = now_ms
            return None
        quiet_from = max(self.pending_since, self.last_audio_ms if self.last_audio_ms is not None else -1)
        needed = self.cfg.filler_delay_ms * (2 if self.spoke_this_turn else 1)
        if now_ms - quiet_from < needed:
            return None
        if self.last_ms is not None and now_ms - self.last_ms < self.cfg.filler_cooldown_ms:
            return None
        lines = FILLERS.get(str(language or "").split("-")[0])
        if not lines:
            self.pending_since = None
            return None
        index = (self._last_index.get(language, -1) + 1) % len(lines)
        self._last_index[language] = index
        self._prev_last_ms = self.last_ms
        self.pending_since, self.played_this_turn, self.last_ms = None, True, now_ms
        return index

    def refused(self, language: str) -> None:
        """The mixer could not play the filler `due` returned (no clip, audio
        arrived): it does not count as played, the rotation stays."""
        self.played_this_turn, self.last_ms = False, self._prev_last_ms
        if language in self._last_index:
            self._last_index[language] = (self._last_index[language] - 1) % len(FILLERS.get(language, ("",)))

    def turn_done(self) -> None:
        """The brain's turn ended (AgentTurnDone): nothing left to cover."""
        self.pending_since = None

    def new_turn(self) -> None:
        self.pending_since, self.played_this_turn, self.spoke_this_turn = None, False, False
        self.tool_done_ms = None
