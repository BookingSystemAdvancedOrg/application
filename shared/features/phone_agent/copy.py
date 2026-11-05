"""Fixed sentences of the phone agent (ADR-L decisions 5, 6, 7, 8), Swedish
first, English second. The EU AI Act art. 50 disclosure is assembled here
and is never owner-editable: the owner's greeting text is appended after it.

Wording **[NEEDS YOUR OK: lawyer]**: the disclosure, the recording notice and
the refusal sentence."""

SV, EN = "sv", "en"
LANGUAGES = (SV, EN)

# "<Hej|Hello>, du pratar med <agentName>, en AI-assistent för <restaurant>."
DISCLOSURE = {
    SV: "Hej, du pratar med {agent}, en AI-assistent för {restaurant}.",
    EN: "Hello, you are speaking with {agent}, an AI assistant for {restaurant}.",
}
# Said again when the caller interrupts the disclosure, and once in a new language.
DISCLOSURE_SHORT = {
    SV: "Jag är en AI-assistent för {restaurant}.",
    EN: "I am an AI assistant for {restaurant}.",
}
RECORDING_NOTICE = {SV: "Samtalet spelas in.", EN: "This call is recorded."}
DEFAULT_EXTRA = {SV: "Vad kan jag hjälpa dig med?", EN: "How can I help you?"}
REFUSED = {
    SV: "Tyvärr kan vi inte ta emot bokningar per telefon just nu, besök vår hemsida.",
    EN: "Unfortunately we cannot take bookings by phone right now, please visit our website.",
}
TRANSFERRING = {SV: "Jag kopplar dig till restaurangen, ett ögonblick.", EN: "I am putting you through, one moment."}
TRANSFER_UNANSWERED = {
    SV: "Ingen kunde svara just nu. Vill du att de ringer upp dig?",
    EN: "Nobody could answer right now. Would you like them to call you back?",
}
MAX_LENGTH = {SV: "Samtalet blev för långt, jag avslutar nu. Ring gärna igen.",
              EN: "This call has become too long, I will end it now. Please call again."}
LARGE_PARTY = {SV: "För så stora sällskap ringer restaurangen upp dig.",
               EN: "For a party that size the restaurant will call you back."}

DEFAULT_AGENT_NAME = "Booqy-assistenten"  # [NEEDS YOUR OK: default name]


def _restaurant(name):
    return " ".join(str(name or "restaurangen").split())


def disclosure(lang, agent_name, restaurant):
    return DISCLOSURE[lang].format(agent=" ".join(str(agent_name or DEFAULT_AGENT_NAME).split()),
                                   restaurant=_restaurant(restaurant))


def disclosure_short(lang, restaurant):
    return DISCLOSURE_SHORT[lang].format(restaurant=_restaurant(restaurant))


def greeting(lang, agent_name, restaurant, extra=None, *, recording=False):
    """The first utterance: [recording notice] + disclosure + the owner's text."""
    parts = []
    if recording:
        parts.append(RECORDING_NOTICE[lang])
    parts.append(disclosure(lang, agent_name, restaurant))
    parts.append(" ".join(str(extra).split()) if extra else DEFAULT_EXTRA[lang])
    return " ".join(parts)


SYSTEM_PROMPT = {
    SV: ("Du är {agent}, en AI-assistent som svarar i telefon för {restaurant}. Du är inte en människa: "
         "säg alltid att du är en AI om någon frågar. Svara kort och vänligt på svenska. Boka, flytta och avboka "
         "bara med verktygen och bara efter ett tydligt ja. Upprepa antal, dag, tid och namn innan du bokar. "
         "Fråga aldrig efter kortuppgifter. Hitta aldrig på tider: erbjud bara det verktygen svarar. "
         "Kan du inte hjälpa till, erbjud att någon ringer upp."),
    EN: ("You are {agent}, an AI assistant answering the phone for {restaurant}. You are not a human: always "
         "say you are an AI when asked. Answer briefly and kindly in English. Book, move and cancel only with "
         "the tools and only after a clear yes. Repeat party size, day, time and name before booking. Never ask "
         "for card details. Never invent times: only offer what the tools return. If you cannot help, offer a "
         "callback."),
}
