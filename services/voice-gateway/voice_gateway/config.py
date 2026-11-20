"""Every tunable of the gateway (FROZEN shape; builders may change a
DEFAULT value in their own section and say so in their summary, never a
field name or type without the design owner).

All durations are integer milliseconds. Defaults are starting values from
ASSESSMENT.md and MULTILINGUAL.md, tuned later on the replay harness. In
Fargate the values come from SSM (`/booqy/<env>/voice-gateway/config`, one
JSON document, read at task start) through `GatewayConfig.from_json`.
"""

from __future__ import annotations

import dataclasses
import json
import os
from dataclasses import dataclass, field
from typing import Literal


@dataclass(frozen=True)
class AudioConfig:
    sample_rate: int = 16000            # canonical internal rate (PCM16 mono)
    frame_ms: int = 20
    ring_seconds: int = 12              # caller audio kept for language-switch replay
    playout_lead_ms: int = 60           # assistant audio sent ahead of real time (small: barge-in clears less)
    inbound_jitter_ms: int = 60         # reorder window for transports that can reorder (WebSocket)


@dataclass(frozen=True)
class VadConfig:
    model: Literal["energy", "webrtc", "silero"] = "silero"   # webrtcvad has no arm64 py3.13 wheel (checked 2026-11-20)
    webrtc_aggressiveness: int = 2      # 0..3 (x86 dev machines only)
    silero_model_path: str = "/opt/models/silero_vad.onnx"
    speech_prob: float = 0.5            # frame is speech at or above this (silero / energy mapping)
    speech_prob_during_playback: float = 0.8   # stricter while the assistant plays (echo)
    energy_floor_dbfs: float = -50.0    # energy VAD: below this never speech
    energy_margin_db: float = 12.0      # energy VAD: above the tracked noise floor by this
    start_confirm_ms: int = 100         # voiced ms in a row before VadStart
    pause_confirm_ms: int = 80          # unvoiced ms in a row before VadPause
    resume_confirm_ms: int = 60
    preroll_ms: int = 300               # audio before the onset kept for STT reopen


@dataclass(frozen=True)
class SttConfig:
    region: str = "eu-central-1"        # Transcribe streaming is NOT served in eu-north-1 (401 NotAuthorized, checked 2026-11-20); Frankfurt streams sv-SE and en
    sample_rate: int = 16000
    partial_stability: Literal["low", "medium", "high"] = "high"
    vocabulary_name: str | None = None  # per tenant later (restaurant and street names)
    final_wait_ms: int = 350            # after the silence threshold, wait this long for the final result
    max_reopen_replay_ms: int = 10000   # replay at most this much buffered audio into a new stream
    replay_speed: float = 3.0           # replay pacing relative to real time (3x verified on booqy-dev 2026-11-20)
    idle_keepalive_ms: int = 10000      # send silence when nothing was sent for this long (stream times out at 15 s)


@dataclass(frozen=True)
class LanguageConfig:
    start_language: str = "sv"
    identify_first_utterance: bool = True
    always_candidates: tuple[str, ...] = ("sv", "en")       # our codes; Transcribe allows at most 5 per stream
    default_extra_candidates: tuple[str, ...] = ("ar", "de", "pl")
    country_candidates: dict = field(default_factory=lambda: {   # caller prefix -> extra candidates first
        "+98": ("fa",), "+90": ("tr",), "+48": ("pl",), "+49": ("de",), "+43": ("de",), "+41": ("de", "fr", "it"),
        "+33": ("fr",), "+34": ("es",), "+39": ("it",), "+62": ("id",), "+387": ("bs",), "+385": ("bs",),
        "+381": ("bs",), "+966": ("ar",), "+971": ("ar",), "+964": ("ar",), "+963": ("ar",), "+20": ("ar",),
        "+961": ("ar",), "+962": ("ar",), "+44": ("en",), "+1": ("en",), "+45": ("da",), "+358": ("fi",),
    })
    strong_p: float = 0.85              # rule 2
    accumulated_p: float = 0.6          # rule 3, two utterances in a row
    min_speech_ms: int = 1500           # rule 4
    min_content_words: int = 3          # rule 4
    uncertain_low: float = 0.5          # rule 5 band
    uncertain_margin: float = 0.2
    stay_turns_after_switch: int = 2    # rule 6
    max_auto_switches: int = 3          # rule 6 (same as language.MAX_CONNECT_SWITCHES)
    poor_audio_p: float = 0.5           # rule 7
    poor_audio_misses_before_ask: int = 2
    poor_audio_misses_before_callback: int = 3
    low_asr_confidence: float = 0.4     # same as GARBLED_CONFIDENCE on the Connect line
    probe_on_hint: bool = True          # open a LID probe stream when the text hint or the model tag disagrees
    # A second identify-language stream beside the first utterance's (Transcribe takes at most 5 options per
    # stream): sv, en and these, minus what the first stream has. Billed like the main stream while it runs (call
    # start to the first turn end, about 10 s). Empty: none.
    second_identify_candidates: tuple[str, ...] = ("tr", "fi", "es")
    # While the call is away from start_language, a fixed start_language stream runs beside the live one, so a
    # caller going back to Swedish is heard as Swedish (the away stream writes it as nonsense). Billed like the
    # main stream for the minutes away from Swedish.
    shadow_start_language: bool = True
    shadow_min_confidence: float = 0.75  # the start language stream's words this sure, the away stream's not


@dataclass(frozen=True)
class TurnConfig:
    base_silence_ms: int = 550          # a complete-sounding clause (voice-B: 450 ms sentence pauses in a monologue)
    answer_silence_ms: int = 450        # a complete clause of two words or more answering the assistant's question
    short_answer_silence_ms: int = 350  # "ja", "nej", "fyra" right after a yes/no or number question
    open_question_silence_ms: int = 900  # after an open question ("vilket namn?") people think
    filler_silence_ms: int = 1600       # tail is a filler: "ehm", "öh", "um"
    dangling_silence_ms: int = 1400     # tail is a preposition or conjunction: "för", "och", "till"
    number_silence_ms: int = 1100       # tail is a number or a digit group (phone numbers, times)
    max_silence_ms: int = 2200          # never wait longer than this after speech
    max_utterance_ms: int = 20000
    min_speech_ms: int = 120            # shorter voiced bursts are noise, never a turn
    covered_slack_ms: int = 150         # a partial whose last word ends this close to the end of speech covers it


@dataclass(frozen=True)
class BargeInConfig:
    enabled: bool = True
    # The greeting (recording notice, disclosure, the owner's text) plays to its end: a "hallå?", a
    # cough or the line's echo never cuts it (owner 2026-11-20). What the caller said meanwhile is
    # still heard and becomes the first turn.
    during_greeting: bool = False
    min_speech_ms: int = 350            # sustained caller speech over playback (shorter: backchannel or cough)
    word_min_speech_ms: int = 150       # a partial with a non-backchannel word may barge in this early
    stop_words: dict = field(default_factory=lambda: {
        "sv": ("nej", "vänta", "stopp", "förlåt", "ursäkta"), "en": ("no", "wait", "stop", "sorry", "hold on"),
    })
    # Look this far back in the playback energy for echo: on a phone line the echo returns after the
    # playout lead plus the network round trip (often 400 to 900 ms), not within 300 ms.
    echo_delay_max_ms: int = 900
    echo_return_loss_db: float = 10.0   # caller energy this far under playback energy = echo
    ignore_first_playback_ms: int = 150  # line settling at the start of each item


@dataclass(frozen=True)
class BackchannelConfig:
    # Off (owner 2026-11-20): the assistant says only real answers, no "mm" in the caller's pauses.
    enabled: bool = False
    min_call_ms: int = 1000             # never in the first second of the call
    min_turn_speech_ms: int = 2500      # caller held the turn at least this long (ASSESSMENT)
    pause_min_ms: int = 160             # fire window inside a caller pause (a 220 ms "mm" fits a 450 ms pause)
    pause_max_ms: int = 600             # past this the turn detector may take the turn: never fire
    cooldown_ms: int = 4000             # never twice within this
    max_per_turn: int = 2
    min_words_since_last: int = 4       # a meaningful clause since the last backchannel
    after_assistant_ms: int = 1500      # not right after the assistant stopped speaking
    no_number_tail: bool = True         # never after a number that could be misheard
    no_during_confirmation: bool = True  # never while a read-back awaits the caller's yes
    clip_volume_db: float = -4.0        # backchannels a little softer than speech


@dataclass(frozen=True)
class SpeakConfig:
    polly_region: str = "eu-central-1"  # Elin neural exists there only (checked 2026-11-20)
    sample_rate: int = 16000
    speech_rate: str = "100%"           # SSML prosody rate (Connect line uses 92%; A/B later)
    first_chunk_min_chars: int = 18     # do not send a tiny first fragment to TTS
    clause_split_chars: int = 90        # a sentence longer than this may be split at a comma
    synth_ahead: int = 2                # sentences synthesised ahead of playback
    tts_timeout_ms: int = 2500
    tts_retry: int = 1
    # Fixed lines are off (owner 2026-11-20: only real answers, never "Ett ögonblick, jag kollar" or
    # "Då ska vi se" after a plain yes). Both stay switchable for A/B tests.
    filler: bool = False                # a fixed filler while a slow tool runs
    filler_delay_ms: int = 700          # tool started and no assistant audio this long: play a filler
    filler_cooldown_ms: int = 5000
    backchannel_max_wait_ms: int = 150  # assistant audio waits at most this for a playing backchannel to end
    first_clause: bool = True           # the first chunk of a turn may end at a comma (Polly starts on a clause)
    opener: bool = False                # a short pre-rendered opener ("Mm.", "Okej.") right at the turn end
    opener_chance: float = 0.65         # chance of an opener on a turn without a certain lookup (varied, not always)


@dataclass(frozen=True)
class BrainConfig:
    call_ref_prefix: str = "connect-gw-"   # see DESIGN.md 6.2: Polly language list and switch cap via is_connect()
    model_id_env: str = "PHONE_AGENT_MODEL_ID"
    stream_sentences: bool = True       # False: non-streaming fallback (whole reply after the turn)
    hold_unknown_tag: bool = True       # a reply tagged with a language outside the voice table waits for TurnDone
    prompt_cache: bool = True           # Bedrock cachePoint after system and tools (verify model support)
    prefetch: bool = True               # read-only availability lookups from partial transcripts (no hold, no write)
    one_round: bool = True              # a booking request with day, time and party: the availability lookup runs
    #                                     before the first model round (the model answers in one Bedrock round)
    warm_connections: bool = True       # one Bedrock client per call, Bedrock and Polly connections opened at start


@dataclass(frozen=True)
class CallConfig:
    still_there_after_ms: int = 8000    # silence in LISTENING: copy.STILL_THERE
    hangup_after_still_there: int = 2   # then the goodbye and hang up
    continuation_grace_ms: int = 900    # caller resumes this soon after TurnEnd, before first audio: merge
    transfer_timeout_s: int = 25
    overlap_tolerance_ms: int = 500     # a yes over the last this-many ms of a read-back still counts


@dataclass(frozen=True)
class GatewayConfig:
    audio: AudioConfig = field(default_factory=AudioConfig)
    vad: VadConfig = field(default_factory=VadConfig)
    stt: SttConfig = field(default_factory=SttConfig)
    language: LanguageConfig = field(default_factory=LanguageConfig)
    turn: TurnConfig = field(default_factory=TurnConfig)
    barge_in: BargeInConfig = field(default_factory=BargeInConfig)
    backchannel: BackchannelConfig = field(default_factory=BackchannelConfig)
    speak: SpeakConfig = field(default_factory=SpeakConfig)
    brain: BrainConfig = field(default_factory=BrainConfig)
    call: CallConfig = field(default_factory=CallConfig)

    @classmethod
    def from_json(cls, text: str | None) -> "GatewayConfig":
        """Overrides from a JSON document {"turn": {"base_silence_ms": 600}, ...};
        unknown sections or keys raise ValueError (a typo must not pass silently)."""
        data = json.loads(text) if text else {}
        sections = {}
        for f in dataclasses.fields(cls):
            section_cls = type(getattr(cls(), f.name))
            values = data.pop(f.name, {}) or {}
            known = {x.name for x in dataclasses.fields(section_cls)}
            unknown = set(values) - known
            if unknown:
                raise ValueError(f"unknown {f.name} keys: {sorted(unknown)}")
            values = {k: tuple(v) if isinstance(v, list) else v for k, v in values.items()}
            sections[f.name] = section_cls(**values)
        if data:
            raise ValueError(f"unknown sections: {sorted(data)}")
        return cls(**sections)

    @classmethod
    def from_env(cls) -> "GatewayConfig":
        """VG_CONFIG_JSON (local runs, tests); the SSM read is server.py's."""
        return cls.from_json(os.environ.get("VG_CONFIG_JSON"))
