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
# Said again when the caller interrupts the disclosure, and once in every new
# language the call switches to (language.CALL_LANGUAGES).
DISCLOSURE_SHORT = {
    SV: "Jag är en AI-assistent för {restaurant}.",
    EN: "I am an AI assistant for {restaurant}.",
    "no": "Jeg er en AI-assistent for {restaurant}.",
    "da": "Jeg er en AI-assistent for {restaurant}.",
    "fi": "Olen ravintolan {restaurant} tekoälyavustaja.",
    "de": "Ich bin ein KI-Assistent von {restaurant}.",
    "fr": "Je suis un assistant IA de {restaurant}.",
    "es": "Soy un asistente de IA de {restaurant}.",
    "it": "Sono un assistente IA di {restaurant}.",
    "pl": "Jestem asystentem AI restauracji {restaurant}.",
    "ar": "أنا مساعد ذكاء اصطناعي لمطعم {restaurant}.",
    "pt": "Sou um assistente de IA do restaurante {restaurant}.",
    "nl": "Ik ben een AI-assistent van {restaurant}.",
    "tr": "{restaurant} için bir yapay zekâ asistanıyım.",
    "ru": "Я ИИ-ассистент ресторана {restaurant}.",
    "uk": "Я ШІ-асистент ресторану {restaurant}.",
    "cs": "Jsem AI asistent restaurace {restaurant}.",
    "sk": "Som AI asistent reštaurácie {restaurant}.",
    "hu": "A(z) {restaurant} mesterséges intelligencia asszisztense vagyok.",
    "ro": "Sunt un asistent AI al restaurantului {restaurant}.",
    "bg": "Аз съм AI асистент на ресторант {restaurant}.",
    "hr": "Ja sam AI asistent restorana {restaurant}.",
    "el": "Είμαι βοηθός τεχνητής νοημοσύνης του εστιατορίου {restaurant}.",
    "ja": "私は{restaurant}のAIアシスタントです。",
    "zh": "我是{restaurant}的AI助手。",
    "ko": "저는 {restaurant}의 AI 어시스턴트입니다.",
    "hi": "मैं {restaurant} का एआई सहायक हूँ।",
    "id": "Saya asisten AI untuk {restaurant}.",
    "ms": "Saya pembantu AI untuk {restaurant}.",
    "fil": "Ako ay isang AI assistant ng {restaurant}.",
    "vi": "Tôi là trợ lý AI của {restaurant}.",
    "ta": "நான் {restaurant} உணவகத்தின் AI உதவியாளர்.",
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
    return pick(DISCLOSURE, lang).format(agent=" ".join(str(agent_name or DEFAULT_AGENT_NAME).split()),
                                   restaurant=_restaurant(restaurant))


def pick(sentences, lang):
    """A fixed sentence in `lang`, English when it has no translation of its own."""
    return sentences.get(lang) or sentences[EN]


def disclosure_short(lang, restaurant):
    return pick(DISCLOSURE_SHORT, lang).format(restaurant=_restaurant(restaurant))


# A shared number (one number for several locations): the location is asked first.
WHICH_LOCATION = {
    SV: "Vilken av våra restauranger gäller det, {names}?",
    EN: "Which of our restaurants is it about, {names}?",
}
WHICH_LOCATION_AGAIN = {
    SV: "Förlåt, jag hörde inte vilken. Är det {names}?",
    EN: "Sorry, I didn't catch which one. Is it {names}?",
}


def joined(names, lang):
    names = [n for n in names if n]
    if len(names) < 2:
        return names[0] if names else ""
    return ", ".join(names[:-1]) + (" eller " if lang == SV else " or ") + names[-1]


LOCATION_CHOSEN = {SV: "{name}, toppen. Vad kan jag hjälpa dig med?", EN: "{name}, great. How can I help you?"}


def location_chosen(lang, name):
    return pick(LOCATION_CHOSEN, lang).format(name=name)


def which_location(lang, names, *, again=False):
    return pick(WHICH_LOCATION_AGAIN if again else WHICH_LOCATION, lang).format(names=joined(names, lang))


def greeting(lang, agent_name, restaurant, extra=None, *, recording=False):
    """The first utterance: [recording notice] + disclosure + the owner's text."""
    parts = []
    if recording:
        parts.append(pick(RECORDING_NOTICE, lang))
    parts.append(disclosure(lang, agent_name, restaurant))
    parts.append(" ".join(str(extra).split()) if extra else pick(DEFAULT_EXTRA, lang))
    return " ".join(parts)


# The brain's instructions (ADR-L decisions 2, 6, 11, 13; owner 2026-11-13: a
# natural tone, short sentences, warm and relaxed, the caller's language).
# Written for speech: the reply is read aloud by the voice provider.
SYSTEM_PROMPT = {
    SV: ("Du är {agent}, en AI-assistent som svarar i telefon för {restaurant}. Du är inte en människa: "
         "säg alltid att du är en AI om någon frågar. "
         "Prata som en varm och avslappnad värd i telefon: korta meningar, en fråga i taget, vardagligt språk. "
         "Inga listor, inga punkter, inga id-nummer. Säg tider som man säger dem, till exempel halv åtta eller "
         "klockan sju, och datum som fredag den 7 november. Använd gärna uppringarens namn. "
         "Om uppringaren pratar ett annat språk byter du till det språket direkt. Börja varje svar med "
         "språkkoden för språket du svarar på inom hakparentes, till exempel [sv] eller [de]. Meningen om att du "
         "är en AI säger systemet åt dig när språket byts: upprepa den inte. "
         "Boka, flytta och avboka bara med verktygen och bara efter ett tydligt ja. Upprepa antal, dag, tid "
         "och namn innan du bokar. Fråga aldrig efter kortuppgifter. Hitta aldrig på tider: erbjud bara det "
         "verktygen svarar, högst tre tider åt gången, närmast önskad tid först. Är dagen full, fråga om du ska "
         "kolla dagen efter, en gång. Svara bara om den här restaurangen; du känner inte till andra "
         "restauranger eller bokningar. Säg öppettider, meny och regler precis som verktygen svarar, utan att "
         "kommentera eller gissa om de stämmer, och berätta aldrig om system eller verktyg. "
         "Kan du inte hjälpa till, erbjud att någon ringer upp."),
    EN: ("You are {agent}, an AI assistant answering the phone for {restaurant}. You are not a human: always "
         "say you are an AI when asked. "
         "Talk like a warm, relaxed host on the phone: short sentences, one question at a time, everyday words. "
         "No lists, no bullet points, no id numbers. Say times the way people say them, like half past seven, "
         "and dates like Friday the 7th of November. Use the caller's name now and then. "
         "If the caller speaks another language, switch to it straight away. Start every reply with the "
         "language code of the language you answer in, in square brackets, like [en] or [de]. The system says "
         "the sentence that you are an AI when the language changes: do not repeat it. "
         "Book, move and cancel only with the tools and only after a clear yes. Repeat party size, day, time "
         "and name before booking. Never ask for card details. Never invent times: only offer what the tools "
         "return, at most three times at once, closest to the asked time first. If the day is full, offer to "
         "check the next day, once. Only talk about this restaurant; you know nothing about other restaurants "
         "or bookings. Give opening hours, menu and rules exactly as the tools return them, without commenting "
         "on or guessing whether they are right, and never mention systems or tools. "
         "If you cannot help, offer a callback."),
}
TONE = {
    "formal": {SV: "Tilltala uppringaren artigt och lite formellt.", EN: "Address the caller politely and a little formally."},
    "friendly": {SV: "Var personlig och vänlig, gärna med lite värme.", EN: "Be personal and friendly, with a bit of warmth."},
}
ANSWER_IN = "The caller speaks {name}. Answer in {name}; tool results are in Swedish or English, translate them."
