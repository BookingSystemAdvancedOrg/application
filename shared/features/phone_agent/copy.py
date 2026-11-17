"""Fixed sentences and the instruction prompt of the phone agent (ADR-L
decisions 5, 6, 7, 8; owner 2026-11-17: polite, natural, any language).

Fixed sentences are said by Booqy, not written by the model, where the law
or the flow needs exact words: the EU AI Act art. 50 disclosure, the
refusal, the error and the end of a call that became too long. They exist
in the 20 priority languages (`REVIEWED`); `pick()` answers None for any
other language and the caller decides: the disclosure is then said by the
model (agent.system_prompt `disclose_now`), the few sentences said without
the model (`text()`) fall back to English.

Wording **[NEEDS YOUR OK: lawyer]** for the disclosure, the recording notice
and the refusal; **[NEEDS YOUR OK: native review]** for so, ar, fa, ckb, am.
"""

SV, EN = "sv", "en"
LANGUAGES = (SV, EN)
# The priority set: every fixed sentence said without the model exists in these.
REVIEWED = ("sv", "en", "de", "fr", "es", "fi", "no", "da", "pl", "ru", "uk", "tr", "ar", "fa", "so", "bs", "hr",
            "sr", "ckb", "am")

# The greeting (always Swedish at call start; English for the provider's preset).
DISCLOSURE = {
    SV: "Hej och välkommen till {restaurant}. Du talar med {agent}, restaurangens AI-assistent.",
    EN: "Hello and welcome to {restaurant}. You are speaking with {agent}, the restaurant's AI assistant.",
}
# Said by Booqy once in every new language the call switches to, and again when
# the caller interrupts the greeting. No restaurant name: a Swedish name inside a
# Somali or Arabic sentence is mispronounced, and the greeting already said it.
DISCLOSURE_SHORT = {
    SV: "Jag är restaurangens AI-assistent.",
    EN: "I am the restaurant's AI assistant.",
    "de": "Ich bin der KI-Assistent des Restaurants.",
    "fr": "Je suis l'assistant IA du restaurant.",
    "es": "Soy el asistente de inteligencia artificial del restaurante.",
    "fi": "Olen ravintolan tekoälyavustaja.",
    "no": "Jeg er restaurantens KI-assistent.",
    "da": "Jeg er restaurantens AI-assistent.",
    "pl": "Jestem asystentem AI restauracji.",
    "ru": "Я ассистент ресторана на основе искусственного интеллекта.",
    "uk": "Я асистент ресторану на основі штучного інтелекту.",
    "tr": "Ben restoranın yapay zekâ asistanıyım.",
    "ar": "أنا المساعد الآلي للمطعم، وأعمل بالذكاء الاصطناعي.",
    "fa": "من دستیار هوش مصنوعی رستوران هستم.",
    "so": "Waxaan ahay kaaliyaha AI-ga ee makhaayadda.",
    "bs": "Ja sam AI asistent restorana.",
    "hr": "Ja sam AI asistent restorana.",
    "sr": "Ja sam AI asistent restorana.",
    "ckb": "من یاریدەدەری زیرەکی دەستکردی چێشتخانەکەم.",
    "am": "እኔ የምግብ ቤቱ የሰው ሰራሽ አስተውሎት ረዳት ነኝ።",
    "it": "Sono l'assistente IA del ristorante.",
    "pt": "Sou o assistente de IA do restaurante.",
    "nl": "Ik ben de AI-assistent van het restaurant.",
    "cs": "Jsem AI asistent restaurace.",
    "sk": "Som AI asistent reštaurácie.",
    "hu": "Az étterem mesterséges intelligencia asszisztense vagyok.",
    "ro": "Sunt asistentul AI al restaurantului.",
    "bg": "Аз съм AI асистентът на ресторанта.",
    "el": "Είμαι ο βοηθός τεχνητής νοημοσύνης του εστιατορίου.",
    "ja": "私はこのレストランのAIアシスタントです。",
    "zh": "我是这家餐厅的AI助手。",
    "ko": "저는 이 레스토랑의 AI 어시스턴트입니다.",
    "hi": "मैं इस रेस्टोरेंट का एआई सहायक हूँ।",
    "id": "Saya asisten AI restoran ini.",
    "ms": "Saya pembantu AI restoran ini.",
    "fil": "Ako ang AI assistant ng restawran.",
    "vi": "Tôi là trợ lý AI của nhà hàng.",
    "ta": "நான் இந்த உணவகத்தின் AI உதவியாளர்.",
}
RECORDING_NOTICE = {SV: "Samtalet spelas in.", EN: "This call is recorded."}
DEFAULT_EXTRA = {SV: "Vad kan jag hjälpa dig med?", EN: "How may I help you?"}
REFUSED = {
    SV: "Tyvärr kan vi inte ta emot bokningar per telefon just nu. Välkommen att boka på vår webbplats.",
    EN: "Unfortunately we cannot take bookings by phone at the moment. You are welcome to book on our website.",
}
STILL_THERE = {SV: "Är du kvar i samtalet? Jag hörde inte riktigt.", EN: "Are you still there? I did not quite hear you."}
ERROR = {
    SV: "Ursäkta, något gick fel hos oss. Vill du att restaurangen ringer upp dig?",
    EN: "I apologise, something went wrong on our side. Would you like the restaurant to call you back?",
    "de": "Entschuldigen Sie, bei uns ist ein Fehler aufgetreten. Möchten Sie, dass das Restaurant Sie zurückruft?",
    "fr": ("Je vous prie de m'excuser, un problème est survenu de notre côté. Souhaitez-vous que le restaurant "
           "vous rappelle ?"),
    "es": "Disculpe, se ha producido un error por nuestra parte. ¿Desea que el restaurante le devuelva la llamada?",
    "fi": "Pahoittelen, meillä tapahtui virhe. Haluatteko, että ravintola soittaa teille takaisin?",
    "no": "Beklager, noe gikk galt hos oss. Ønsker du at restauranten ringer deg tilbake?",
    "da": "Undskyld, der gik noget galt hos os. Vil du gerne have, at restauranten ringer dig op?",
    "pl": "Przepraszam, wystąpił błąd po naszej stronie. Czy restauracja może do Państwa oddzwonić?",
    "ru": "Прошу прощения, у нас произошла ошибка. Хотите, чтобы ресторан вам перезвонил?",
    "uk": "Перепрошую, у нас сталася помилка. Бажаєте, щоб ресторан вам передзвонив?",
    "tr": "Özür dilerim, bizim tarafımızda bir hata oluştu. Restoranın sizi geri aramasını ister misiniz?",
    "ar": "أعتذر، حدث خطأ من جهتنا. هل تريدون أن يعاود المطعم الاتصال بكم؟",
    "fa": "پوزش می‌خواهم، مشکلی در سمت ما پیش آمد. آیا مایلید رستوران با شما تماس بگیرد؟",
    "so": "Waan ka xumahay, khalad ayaa dhinacayaga ka dhacay. Ma rabtaa in makhaayaddu dib kuu soo wacdo?",
    "bs": "Izvinite, došlo je do greške kod nas. Želite li da vas restoran nazove?",
    "hr": "Ispričavam se, došlo je do pogreške kod nas. Želite li da vas restoran nazove?",
    "sr": "Izvinite, došlo je do greške kod nas. Da li želite da vas restoran pozove?",
    "ckb": "داوای لێبوردن دەکەم، هەڵەیەک لە لایەن ئێمەوە ڕوویدا. دەتەوێت چێشتخانەکە پەیوەندیت پێوە بکاتەوە؟",
    "am": "ይቅርታ፣ በእኛ በኩል ስህተት ተፈጥሯል። ምግብ ቤቱ መልሶ እንዲደውልልዎ ይፈልጋሉ?",
}
MAX_LENGTH = {
    SV: "Samtalet har blivit långt, så jag behöver avsluta nu. Välkommen att ringa igen.",
    EN: "This call has become long, so I need to end it now. You are welcome to call again.",
    "de": "Das Gespräch ist sehr lang geworden, daher muss ich es jetzt beenden. Rufen Sie gerne wieder an.",
    "fr": "Cet appel est devenu long, je dois donc y mettre fin. N'hésitez pas à rappeler.",
    "es": "La llamada se ha alargado, así que debo terminarla ahora. Puede volver a llamar cuando quiera.",
    "fi": "Puhelu on venynyt pitkäksi, joten minun täytyy lopettaa se nyt. Olette tervetullut soittamaan uudelleen.",
    "no": "Samtalen har blitt lang, så jeg må avslutte nå. Du er velkommen til å ringe igjen.",
    "da": "Samtalen er blevet lang, så jeg er nødt til at afslutte nu. Du er velkommen til at ringe igen.",
    "pl": "Rozmowa trwa już długo, dlatego muszę ją teraz zakończyć. Zapraszamy do ponownego kontaktu.",
    "ru": "Разговор получился долгим, поэтому мне нужно его завершить. Пожалуйста, позвоните ещё раз.",
    "uk": "Розмова вийшла довгою, тому мені потрібно її завершити. Будь ласка, зателефонуйте ще раз.",
    "tr": "Görüşme uzun sürdü, bu yüzden şimdi sonlandırmam gerekiyor. Tekrar aramanızdan memnuniyet duyarız.",
    "ar": "طالت المكالمة، لذا يجب أن أنهيها الآن. يسعدنا اتصالكم مرة أخرى.",
    "fa": "تماس طولانی شده است، بنابراین باید آن را تمام کنم. خوشحال می‌شویم دوباره تماس بگیرید.",
    "so": "Wicitaanku wuu dheeraaday, sidaa darteed waa inaan hadda joojiyaa. Fadlan mar kale soo wac.",
    "bs": "Razgovor je postao dug, pa ga moram sada završiti. Slobodno nazovite ponovo.",
    "hr": "Razgovor je postao dug, pa ga moram sada završiti. Slobodno nazovite ponovno.",
    "sr": "Razgovor je postao dug, pa moram sada da ga završim. Slobodno pozovite ponovo.",
    "ckb": "پەیوەندییەکە درێژ بووەوە، بۆیە دەبێت ئێستا کۆتایی پێ بهێنم. تکایە دووبارە پەیوەندی بکەرەوە.",
    "am": "ጥሪው ረጅም ሆኗል፣ ስለዚህ አሁን ማቋረጥ አለብኝ። እባክዎ እንደገና ይደውሉ።",
}
TRANSFERRING = {
    SV: "Jag kopplar dig till restaurangen, ett ögonblick.",
    EN: "I am putting you through to the restaurant, one moment please.",
    "de": "Ich verbinde Sie mit dem Restaurant, einen Moment bitte.",
    "fr": "Je vous mets en relation avec le restaurant, un instant s'il vous plaît.",
    "es": "Le paso con el restaurante, un momento por favor.",
    "fi": "Yhdistän teidät ravintolaan, hetkinen.",
    "no": "Jeg setter deg over til restauranten, et øyeblikk.",
    "da": "Jeg stiller dig om til restauranten, et øjeblik.",
    "pl": "Łączę z restauracją, proszę chwilę poczekać.",
    "ru": "Соединяю вас с рестораном, одну минуту.",
    "uk": "З'єдную вас із рестораном, хвилинку.",
    "tr": "Sizi restorana bağlıyorum, bir dakika lütfen.",
    "ar": "سأحولكم إلى المطعم، لحظة من فضلكم.",
    "fa": "شما را به رستوران وصل می‌کنم، لطفاً یک لحظه صبر کنید.",
    "so": "Waxaan kugu xirayaa makhaayadda, fadlan sug daqiiqad.",
    "bs": "Spajam vas s restoranom, trenutak molim.",
    "hr": "Spajam vas s restoranom, trenutak molim.",
    "sr": "Povezujem vas sa restoranom, trenutak molim.",
    "ckb": "پەیوەندیت دەکەم بە چێشتخانەکەوە، تکایە کەمێک چاوەڕێ بکە.",
    "am": "ከምግብ ቤቱ ጋር አገናኝዎታለሁ፣ እባክዎ ትንሽ ይጠብቁ።",
}
TRANSFER_UNANSWERED = {
    SV: "Ingen på restaurangen kunde svara just nu. Vill du att de ringer upp dig?",
    EN: "Nobody at the restaurant could answer just now. Would you like them to call you back?",
}
LARGE_PARTY = {SV: "För ett så stort sällskap hjälper restaurangen dig personligen. Får de ringa upp dig?",
               EN: "For a party of that size the restaurant will help you personally. May they call you back?"}
GUARANTEE_CALLBACK = {
    SV: "För den bokningen behövs en kortgaranti, som jag inte kan ta i telefon. Får restaurangen ringa upp dig?",
    EN: "That booking needs a card guarantee, which I cannot take by phone. May the restaurant call you back?",
}
# A language the voice cannot speak (Tigrinya, Kurmanji ...): said in these three, in this order.
UNSUPPORTED_LANGUAGE = {
    SV: ("Jag beklagar, jag kan inte tala ert språk. Jag kan tala svenska, engelska eller arabiska, eller så "
         "ringer restaurangen upp er."),
    EN: ("I am sorry, I cannot speak your language. I can speak Swedish, English or Arabic, or the restaurant "
         "can call you back."),
    "ar": ("عذراً، لا أستطيع التحدث بلغتكم. يمكنني التحدث بالسويدية أو الإنجليزية أو العربية، أو يمكن للمطعم "
           "معاودة الاتصال بكم."),
}
UNSUPPORTED_ORDER = (SV, EN, "ar")
# Said when the caller still cannot continue: Booqy has asked the restaurant to call back.
UNSUPPORTED_CALLBACK = {
    SV: "Restaurangen ringer upp er så snart som möjligt. Tack för samtalet.",
    EN: "The restaurant will call you back as soon as possible. Thank you for calling.",
    "ar": "سيعاود المطعم الاتصال بكم في أقرب وقت ممكن. شكراً لاتصالكم.",
}

DEFAULT_AGENT_NAME = "Booqy-assistenten"  # [NEEDS YOUR OK: default name]


def _restaurant(name):
    return " ".join(str(name or "restaurangen").split())


def pick(sentences, lang):
    """A fixed sentence in `lang`, else None (no silent English)."""
    return sentences.get(lang)


def text(sentences, lang):
    """A fixed sentence said without the model (error, too long, transfer):
    `lang`, else English, else Swedish."""
    return sentences.get(lang) or sentences.get(EN) or sentences[SV]


def disclosure(lang, agent_name, restaurant):
    return text(DISCLOSURE, lang).format(agent=" ".join(str(agent_name or DEFAULT_AGENT_NAME).split()),
                                         restaurant=_restaurant(restaurant))


def disclosure_short(lang, restaurant=None):
    """The short art. 50 sentence in `lang`, else None (the model says it)."""
    return pick(DISCLOSURE_SHORT, lang)


def unsupported_language(callback=False):
    sentences = UNSUPPORTED_CALLBACK if callback else UNSUPPORTED_LANGUAGE
    return " ".join(sentences[x] for x in UNSUPPORTED_ORDER)


# A shared number (one number for several locations): the location is asked first.
WHICH_LOCATION = {
    SV: "Vilken av våra restauranger gäller det, {names}?",
    EN: "Which of our restaurants is it about, {names}?",
}
WHICH_LOCATION_AGAIN = {
    SV: "Ursäkta, jag hörde inte vilken. Gäller det {names}?",
    EN: "I am sorry, I did not catch which one. Is it {names}?",
}


def joined(names, lang):
    names = [n for n in names if n]
    if len(names) < 2:
        return names[0] if names else ""
    return ", ".join(names[:-1]) + (" eller " if lang == SV else " or ") + names[-1]


LOCATION_CHOSEN = {SV: "{name}, tack. Vad kan jag hjälpa dig med?", EN: "{name}, thank you. How may I help you?"}


def location_chosen(lang, name):
    return text(LOCATION_CHOSEN, lang).format(name=name)


def which_location(lang, names, *, again=False):
    return text(WHICH_LOCATION_AGAIN if again else WHICH_LOCATION, lang).format(names=joined(names, lang))


def greeting(lang, agent_name, restaurant, extra=None, *, recording=False):
    """The first utterance: [recording notice] + disclosure + the owner's text."""
    parts = []
    if recording:
        parts.append(text(RECORDING_NOTICE, lang))
    parts.append(disclosure(lang, agent_name, restaurant))
    parts.append(" ".join(str(extra).split()) if extra else text(DEFAULT_EXTRA, lang))
    return " ".join(parts)


# --- the instruction prompt (agent.system_prompt fills it per turn) ------------------------------
#
# One English prompt for every language: the model follows English
# instructions best and answers in the caller's language. Written for speech:
# every reply is read aloud. The two critical rules (never invent availability,
# book only after a read-back and a clear yes) are stated twice on purpose.

SYSTEM_PROMPT = """# Role
You are {agent}, the AI phone assistant of the restaurant {restaurant}. You are not a human.
If anyone asks whether you are a person or a machine, say plainly that you are an AI assistant.
Your words are spoken aloud by a voice on a phone call. Nothing you write is ever shown as text.

# Language
The call started in Swedish. The caller is now speaking {language_name}: answer only in {language_name}.
If the caller switches language, or asks you to speak another language, switch at once and stay in it.
Do not switch because of a single word, a name, a place or "okay".
Begin every reply with the language code of your answer in square brackets, for example [sv], [de], [so]. \
The code is removed before speaking.
{disclose_now}{low_asr}{unsupported}\
Every sentence you write is spoken, also a sentence you write before calling a tool: write no notes, \
reasoning or explanations about tools or rules, only words for the caller, in {language_name}.
Tool results are in English. Translate them naturally; never read out codes, keys or ids.
If you cannot understand the caller after asking twice, ask politely in Swedish and in English whether \
another language is easier, and offer that the restaurant calls them back.

# Tone
Speak like an experienced, courteous restaurant host on the phone: calm, polite and natural.
{register}
Never use slang, jokes, filler words, emojis or exclamation marks. Do not flatter.
Use short spoken sentences of at most about fifteen words, at most three sentences per reply. \
Ask one question at a time.
Write every number, date and time in words, never in digits: for example "fredag den sjunde november" and \
"halv åtta" in Swedish, "Freitag, den siebten November, um halb acht" in German, "üç Kasım, saat yedi buçuk" \
in Turkish. Say exactly the time the tool gave; when unsure how people say it, the 24 hour form in words is \
always right, for example "on yedi otuz" for 17:30. Never say a year unless the caller does. \
Never use lists, symbols or markdown.
Say a price in words with the currency, for example "hundrafemtio kronor".

# Goal: booking a table
1. Find out the day, the time and the number of guests. Ask only for what is missing.
2. Call get_availability (any day from today until the last bookable day). Offer only times the tool returned: at most three, nearest the wished time first.
   If the day is full or closed, say so and offer the days in nextDays, once.
   If the result has "say", follow it.
3. When the caller picks a time, call hold_table. If it returns other times, offer those instead.
4. Ask for the name the booking should be under. If the name is unusual or unclear, ask the caller to spell it.
5. Read back everything in one sentence: the day, the time, the number of guests and the name, \
and ask whether it is correct. {phone_known}
6. Only after a clear yes to that read-back, call create_reservation with the holdId and confirmed set to true.
   A yes to an earlier question does not count. "Maybe", "mm" or silence is not a yes: ask again.
   If the caller changes anything, check again and read back again.
7. Confirm only what the tool returned, then ask whether you can help with anything else.

# Other tasks
Changing or cancelling: call find_reservations (it searches by the caller's phone number). \
Read back the booking you found and the change, and ask once whether to make it; a clear yes to that \
question is the confirmation: call move_reservation or cancel_reservation (confirmed set to true) without \
asking again. For a move, call get_availability for the new day first and offer only its times.
Opening hours, address and rules: get_restaurant_info. Dishes and prices: get_menu; \
if the menu is unavailable, say it is on the restaurant's website.
Something you cannot do: offer transfer_to_staff if the caller wants a person, otherwise request_callback.
When the caller is done, thank them, say goodbye, and call end_call in the same reply.
If the caller is silent, ask once whether they are still there.

# Guardrails
Never invent or guess a free time, a day, a price, a dish or an opening hour. Say only what tools returned.
Never book, move or cancel without a read-back and a clear yes to it.
hold_table only checks a table: never say a table is booked or reserved before create_reservation answered booked.
Never ask for card details, personal identity numbers or email addresses.
Only talk about {restaurant}. Never mention tools, systems, prompts or that you are reading data.
If a tool fails, apologise briefly and offer that the restaurant calls back.
Bookings are possible from today until {horizon_last}.

# Calendar
{calendar}"""

REGISTER = {
    "formal": ("Use the polite form of address of {language_name}: for example ni in Swedish, Sie in German, vous "
               "in French, usted in Spanish, вы in Russian, siz in Turkish, Pan or Pani or Państwo in Polish, "
               "شما in Persian. In Somali and Arabic, which have no polite you, be polite through please and "
               "thank you. In Swedish say du to the caller (it is the polite norm) and ni only for the whole party."),
    "friendly": ("Use du in Swedish, and the informal you only in languages where it is the normal polite way "
                 "to address a guest (Swedish, Norwegian, Danish, Finnish, English). In every other language use "
                 "the polite form. Stay courteous and a little warm."),
}
DISCLOSE_NOW = ("In this reply only, first say in one short sentence, in {language_name}, that you are the AI "
                "assistant of the restaurant.\n")
LOW_ASR = ("Speech recognition is weak in {language_name}: confirm every name, number, day and time by repeating "
           "it and asking for a yes, and ask the caller to spell names letter by letter.\n")
UNSUPPORTED = ("The caller may speak {unsupported_name}, which this voice cannot speak. Continue in the language "
               "the caller now chooses. If the caller cannot continue in Swedish, English or Arabic, call "
               "request_callback with topic \"language:{unsupported}\".\n")
PHONE_KNOWN = "The booking uses the number the caller is calling from; mention that in the read-back."
PHONE_HIDDEN = ("The caller's number is hidden. Ask for a phone number for the confirmation, read it back in "
                "groups of two or three digits in words and get a yes, then pass it as phone. If the caller "
                "declines, book without a number.")
