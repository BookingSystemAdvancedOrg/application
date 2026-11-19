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

import re

SV, EN = "sv", "en"
LANGUAGES = (SV, EN)
# The priority set: every fixed sentence said without the model exists in these.
REVIEWED = ("sv", "en", "de", "fr", "es", "fi", "no", "da", "pl", "ru", "uk", "tr", "ar", "fa", "so", "bs", "hr",
            "sr", "ckb", "am")

# The greeting (always Swedish at call start; English for the provider's preset). Owner 2026-11-19:
# "Hej och välkommen till <restaurangens namn>. Du pratar med vår digitala assistent." The name is
# the guest-facing one (locations.public_name), never the legal company name; without one the
# welcome has no name. The assistant never calls itself AI, a bot or artificial intelligence.
DISCLOSURE = {
    SV: "Hej och välkommen till {restaurant}. Du pratar med vår digitala assistent.",
    EN: "Hello and welcome to {restaurant}. You are speaking with our digital assistant.",
}
DISCLOSURE_NO_NAME = {
    SV: "Hej och välkommen. Du pratar med vår digitala assistent.",
    EN: "Hello and welcome. You are speaking with our digital assistant.",
}
# Said by Booqy once in every new language the call switches to, and again when
# the caller interrupts the greeting. No restaurant name: a Swedish name inside a
# Somali or Arabic sentence is mispronounced, and the greeting already said it.
DISCLOSURE_SHORT = {
    SV: "Jag är restaurangens digitala assistent.",
    EN: "I am the restaurant's digital assistant.",
    "de": "Ich bin der digitale Assistent des Restaurants.",
    "fr": "Je suis l'assistant numérique du restaurant.",
    "es": "Soy el asistente digital del restaurante.",
    "fi": "Olen ravintolan digitaalinen avustaja.",
    "no": "Jeg er restaurantens digitale assistent.",
    "da": "Jeg er restaurantens digitale assistent.",
    "pl": "Jestem cyfrowym asystentem restauracji.",
    "ru": "Я цифровой ассистент ресторана.",
    "uk": "Я цифровий асистент ресторану.",
    "tr": "Ben restoranın dijital asistanıyım.",
    "ar": "أنا المساعد الرقمي للمطعم.",
    "fa": "من دستیار دیجیتال رستوران هستم.",
    "so": "Waxaan ahay kaaliyaha dijitaalka ah ee makhaayadda.",
    "bs": "Ja sam digitalni asistent restorana.",
    "hr": "Ja sam digitalni asistent restorana.",
    "sr": "Ja sam digitalni asistent restorana.",
    "ckb": "من یاریدەدەری دیجیتاڵی چێشتخانەکەم.",
    "am": "እኔ የምግብ ቤቱ ዲጂታል ረዳት ነኝ።",
    "it": "Sono l'assistente digitale del ristorante.",
    "pt": "Sou o assistente digital do restaurante.",
    "nl": "Ik ben de digitale assistent van het restaurant.",
    "cs": "Jsem digitální asistent restaurace.",
    "sk": "Som digitálny asistent reštaurácie.",
    "hu": "Az étterem digitális asszisztense vagyok.",
    "ro": "Sunt asistentul digital al restaurantului.",
    "bg": "Аз съм дигиталният асистент на ресторанта.",
    "el": "Είμαι ο ψηφιακός βοηθός του εστιατορίου.",
    "ja": "私はこのレストランのデジタルアシスタントです。",
    "zh": "我是这家餐厅的数字助手。",
    "ko": "저는 이 레스토랑의 디지털 어시스턴트입니다.",
    "hi": "मैं इस रेस्टोरेंट का डिजिटल सहायक हूँ।",
    "id": "Saya asisten digital restoran ini.",
    "ms": "Saya pembantu digital restoran ini.",
    "fil": "Ako ang digital assistant ng restawran.",
    "vi": "Tôi là trợ lý kỹ thuật số của nhà hàng.",
    "ta": "நான் இந்த உணவகத்தின் டிஜிட்டல் உதவியாளர்.",
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
# Said by Booqy when a turn reaches its deadline (agent.TURN_BUDGET_S) before the
# answer is ready; the next turn continues the work (copy.RESUME).
STILL_CHECKING = {
    SV: "Ett ögonblick, jag kollar fortfarande.",
    EN: "One moment please, I am still checking.",
    "de": "Einen Moment bitte, ich sehe noch nach.",
    "fr": "Un instant, s'il vous plaît, je vérifie encore.",
    "es": "Un momento, por favor, todavía lo estoy comprobando.",
    "fi": "Hetkinen, tarkistan vielä.",
    "no": "Et øyeblikk, jeg sjekker fortsatt.",
    "da": "Et øjeblik, jeg tjekker stadig.",
    "pl": "Chwileczkę, jeszcze sprawdzam.",
    "ru": "Одну минуту, я ещё проверяю.",
    "uk": "Хвилинку, я ще перевіряю.",
    "tr": "Bir dakika lütfen, hâlâ kontrol ediyorum.",
    "ar": "لحظة من فضلكم، ما زلت أتحقق.",
    "fa": "لطفاً یک لحظه صبر کنید، هنوز در حال بررسی هستم.",
    "so": "Fadlan sug daqiiqad, weli waan hubinayaa.",
    "bs": "Trenutak, molim, još provjeravam.",
    "hr": "Trenutak, molim, još provjeravam.",
    "sr": "Trenutak, molim, još proveravam.",
    "ckb": "تکایە کەمێک چاوەڕێ بکە، هێشتا دەپشکنم.",
    "am": "እባክዎ ትንሽ ይጠብቁ፣ አሁንም እያጣራሁ ነው።",
}
# Beside the instruction prompt in the turn after a cut one.
RESUME = ("\n\n# Continue\nYour previous reply was cut short: the caller heard only that you are still checking. "
          "First finish that work from the conversation above (run a lookup again if its result says timeout), "
          "then answer the caller. A table or booking looked up then has not been read back yet.")
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
# The card guarantee's terms for the party, said before the read-back (tools: guaranteeTerms).
# Owner 2026-11-19: by default only the rule, never an amount: a fee if the booking is cancelled
# later than the restaurant's own cutoff before the visit, or if the guests do not come (only the
# fees this restaurant charges), then the question whether the caller wants more information.
# The amounts (shared/guarantee.snapshot: totals for the party in words, a fee that is 0 is left
# out) only when the caller asks. Other languages translate the English one.
GUARANTEE_TERMS = {
    SV: {"intro": "För den bokningen behövs ett kort som garanti, men inget dras nu.",
         # Commas at the clause ends: Polly pauses there.
         "both": "Restaurangen tar ut en avgift om bokningen avbokas senare än {hours} före besöket, "
                 "eller om ni inte kommer.",
         "no_show": "Restaurangen tar ut en avgift om ni inte kommer.",
         "late": "Restaurangen tar ut en avgift om bokningen avbokas senare än {hours} före besöket.",
         "ask": "Vill du ha mer information om det?",
         "details_both": "Om ni inte kommer, tar restaurangen {no_show}. Avbokar ni senare än {hours} före "
                         "besöket, tar restaurangen {late}.",
         "details_no_show": "Om ni inte kommer, tar restaurangen {no_show}.",
         "details_late": "Avbokar ni senare än {hours} före besöket, tar restaurangen {late}."},
    EN: {"intro": "That booking needs a card as a guarantee, but nothing is charged now.",
         "both": "The restaurant charges a fee if the booking is cancelled later than {hours} before the visit, "
                 "or if you do not come.",
         "no_show": "The restaurant charges a fee if you do not come.",
         "late": "The restaurant charges a fee if the booking is cancelled later than {hours} before the visit.",
         "ask": "Would you like more information about that?",
         "details_both": "If you do not come, the restaurant charges {no_show}, and if you cancel later than "
                         "{hours} before the visit, it charges {late}.",
         "details_no_show": "If you do not come, the restaurant charges {no_show}.",
         "details_late": "If you cancel later than {hours} before the visit, the restaurant charges {late}."},
}


def guarantee_terms(lang, *, no_show=None, late=None, hours=None):
    """The card guarantee's terms in `lang` (Swedish or English) as
    {"sentence", "question", "details"}: `sentence` is said by default (that a
    card is needed, nothing is charged now, and when a fee is charged, with
    no amount), `question` asks whether the caller wants more information,
    `details` holds the amounts, said only when the caller asks. `no_show`,
    `late`, `hours`: the spoken words, None = not charged (a late fee needs
    its cutoff). Without any fee there is no question and no details."""
    words = GUARANTEE_TERMS.get(lang) or GUARANTEE_TERMS[EN]
    late = late if late and hours else None
    kind = "both" if no_show and late else "no_show" if no_show else "late" if late else None
    if kind is None:
        return {"sentence": words["intro"], "question": None, "details": None}
    rule = words[kind].format(hours=hours)
    details = words["details_" + kind].format(no_show=no_show, late=late, hours=hours)
    return {"sentence": f"{words['intro']} {rule}", "question": words["ask"], "details": details}


# Said by the model, word for word in the caller's language, once create_reservation sent the card
# guarantee link (tools: guaranteeLinkSent). The booking waits for the card; nobody asks for card details.
GUARANTEE_LINK_SENT = {
    SV: ("Jag har skickat ett sms med en länk där du lägger till ett kort som garanti. Bordet hålls i trettio "
         "minuter och är bokat när kortet är tillagt. Jag frågar aldrig efter kortuppgifter i telefon."),
    EN: ("I have sent you a text message with a link where you add a card as a guarantee. The table is held for "
         "thirty minutes and is booked once the card is added. I never ask for card details on the phone."),
    "de": ("Ich habe Ihnen eine SMS mit einem Link geschickt, über den Sie eine Karte als Garantie hinterlegen. "
           "Der Tisch wird dreißig Minuten lang gehalten und ist gebucht, sobald die Karte hinterlegt ist. "
           "Ich frage am Telefon nie nach Kartendaten."),
    "fr": ("Je vous ai envoyé un SMS avec un lien pour enregistrer une carte en garantie. La table est retenue "
           "pendant trente minutes et sera réservée dès que la carte sera enregistrée. Je ne demande jamais de "
           "données de carte par téléphone."),
    "es": ("Le he enviado un SMS con un enlace para registrar una tarjeta como garantía. La mesa queda retenida "
           "durante treinta minutos y se reserva en cuanto se registre la tarjeta. Nunca pido datos de tarjeta "
           "por teléfono."),
    "fi": ("Lähetin teille tekstiviestin, jossa on linkki kortin lisäämiseen takuuksi. Pöytä pidetään teille "
           "kolmekymmentä minuuttia, ja varaus vahvistuu, kun kortti on lisätty. En koskaan kysy korttitietoja "
           "puhelimessa."),
    "no": ("Jeg har sendt deg en SMS med en lenke der du legger inn et kort som garanti. Bordet holdes i tretti "
           "minutter og er booket når kortet er lagt inn. Jeg ber aldri om kortopplysninger på telefon."),
    "da": ("Jeg har sendt dig en sms med et link, hvor du tilføjer et kort som garanti. Bordet holdes i tredive "
           "minutter og er booket, når kortet er tilføjet. Jeg beder aldrig om kortoplysninger i telefonen."),
    "pl": ("Wysłałem SMS z linkiem, pod którym można dodać kartę jako gwarancję. Stolik jest zatrzymany przez "
           "trzydzieści minut i zostanie zarezerwowany po dodaniu karty. Nigdy nie pytam o dane karty przez "
           "telefon."),
    "ru": ("Я отправил вам SMS со ссылкой, по которой можно добавить карту в качестве гарантии. Столик "
           "удерживается тридцать минут и будет забронирован, как только карта будет добавлена. Я никогда не "
           "спрашиваю данные карты по телефону."),
    "uk": ("Я надіслав вам SMS з посиланням, за яким можна додати картку як гарантію. Столик утримується "
           "тридцять хвилин і буде заброньований, щойно картку буде додано. Я ніколи не питаю дані картки "
           "телефоном."),
    "tr": ("Size, kartınızı garanti olarak ekleyebileceğiniz bir bağlantı içeren bir SMS gönderdim. Masa otuz "
           "dakika boyunca sizin için tutulur ve kart eklendiğinde rezervasyon tamamlanır. Telefonda asla kart "
           "bilgisi istemem."),
    "ar": ("أرسلت إليكم رسالة نصية فيها رابط لإضافة بطاقة كضمان. تبقى الطاولة محجوزة لكم ثلاثين دقيقة، ويتأكد "
           "الحجز عند إضافة البطاقة. لا أطلب أبداً بيانات البطاقة عبر الهاتف."),
    "fa": ("یک پیامک با لینکی برای ثبت کارت به عنوان ضمانت برایتان فرستادم. میز به مدت سی دقیقه برایتان نگه "
           "داشته می‌شود و با ثبت کارت رزرو قطعی می‌شود. من هرگز اطلاعات کارت را تلفنی نمی‌پرسم."),
    "so": ("Waxaan kuu soo diray fariin qoraal ah oo leh xiriiriye aad kaarka ugu darto dammaanad ahaan. Miiska "
           "waxaa laguu hayaa soddon daqiiqo, waxaana la xaqiijinayaa marka kaarka lagu daro. Weligay telefoonka "
           "kuma weydiiyo macluumaadka kaarka."),
    "bs": ("Poslao sam vam SMS s linkom preko kojeg dodajete karticu kao garanciju. Sto se drži trideset minuta "
           "i rezervisan je kada dodate karticu. Nikada ne tražim podatke o kartici telefonom."),
    "hr": ("Poslao sam vam SMS s poveznicom preko koje dodajete karticu kao jamstvo. Stol se drži trideset "
           "minuta i rezerviran je kada dodate karticu. Nikada ne tražim podatke o kartici telefonom."),
    "sr": ("Poslao sam vam SMS sa linkom preko kog dodajete karticu kao garanciju. Sto se drži trideset minuta "
           "i rezervisan je kada dodate karticu. Nikada ne tražim podatke o kartici telefonom."),
    "ckb": ("نامەیەکی کورتم بۆ ناردوویت لەگەڵ بەستەرێک بۆ زیادکردنی کارتێک وەک گەرەنتی. مێزەکە بۆ ماوەی سی "
            "خولەک بۆت دەهێڵدرێتەوە و کاتێک کارتەکە زیاد کرا حیجزەکە جێگیر دەبێت. هەرگیز لە تەلەفۆندا زانیاری "
            "کارت ناپرسم."),
    "am": ("ካርድ እንደ ዋስትና የሚያስገቡበት ሊንክ የያዘ የጽሑፍ መልእክት ልኬልዎታለሁ። ጠረጴዛው ለሰላሳ ደቂቃ ይያዝልዎታል፣ "
           "ካርዱ ሲገባም ቦታው ይያዛል። በስልክ የካርድ መረጃ በጭራሽ አልጠይቅም።"),
}
# A language the voice cannot speak (Tigrinya, Kurmanji ...): said in these three, in this order.
UNSUPPORTED_LANGUAGE = {
    SV: ("Jag beklagar, jag kan inte tala ditt språk. Jag kan tala svenska, engelska eller arabiska, eller så "
         "ringer restaurangen upp dig."),
    EN: ("I am sorry, I cannot speak your language. I can speak Swedish, English or Arabic, or the restaurant "
         "can call you back."),
    "ar": ("عذراً، لا أستطيع التحدث بلغتكم. يمكنني التحدث بالسويدية أو الإنجليزية أو العربية، أو يمكن للمطعم "
           "معاودة الاتصال بكم."),
}
UNSUPPORTED_ORDER = (SV, EN, "ar")
# Said when the caller still cannot continue: Booqy has asked the restaurant to call back.
UNSUPPORTED_CALLBACK = {
    SV: "Restaurangen ringer upp dig så snart som möjligt. Tack för samtalet.",
    EN: "The restaurant will call you back as soon as possible. Thank you for calling.",
    "ar": "سيعاود المطعم الاتصال بكم في أقرب وقت ممكن. شكراً لاتصالكم.",
}

# The assistant's name when it names itself (owner 2026-11-19): Booqy-assistenten. The
# restaurant's own agentName (admin) is used when it set one; the plain default "Booqy" (the
# config default) becomes Booqy-assistenten.
DEFAULT_AGENT_NAME = "Booqy-assistenten"
# Booqy is pronounced "Bookie" (owner 2026-11-19): Polly reads it through an SSML alias
# (`<sub>`, supported by the standard and neural engines the call voices use); a provider
# that takes plain text gets the alias written out (spoken_plain).
BOOQY = "Booqy"
BOOQY_SAID = "Bookie"


def agent_name(name):
    """The name the assistant uses for itself: the restaurant's own, else Booqy-assistenten."""
    name = " ".join(str(name or "").split())
    return DEFAULT_AGENT_NAME if not name or name.casefold() == BOOQY.casefold() else name


_BOOQY_WORD = re.compile(r"\b(booqy)(s?)\b", re.I)


def ssml_names(escaped):
    """XML-escaped text for Polly with every Booqy read as "Bookie" (an SSML
    alias; "Booqys" is read "Bookies": the s sits inside the alias, a lone
    "s" after the element could be spelled out as a letter)."""
    return _BOOQY_WORD.sub(lambda m: f'<sub alias="{BOOQY_SAID}{m.group(2)}">{m.group(0)}</sub>', str(escaped))


def spoken_plain(text):
    """Text for a voice that takes plain text (no SSML): Booqy written as it is said."""
    return _BOOQY_WORD.sub(lambda m: BOOQY_SAID + m.group(2), str(text or ""))


def _restaurant(name):
    return " ".join(str(name or "").split())


def pick(sentences, lang):
    """A fixed sentence in `lang`, else None (no silent English)."""
    return sentences.get(lang)


def text(sentences, lang):
    """A fixed sentence said without the model (error, too long, transfer):
    `lang`, else English, else Swedish."""
    return sentences.get(lang) or sentences.get(EN) or sentences[SV]


def disclosure(lang, agent_name, restaurant):
    """The greeting's disclosure with the guest-facing restaurant name (or
    none); `agent_name` is not said: the owner's greeting names no assistant."""
    name = _restaurant(restaurant)
    if not name:
        return text(DISCLOSURE_NO_NAME, lang)
    return text(DISCLOSURE, lang).format(restaurant=name)


def disclosure_short(lang, restaurant=None):
    """The short art. 50 sentence in `lang`, else None (the model says it)."""
    return pick(DISCLOSURE_SHORT, lang)


def unsupported_language(callback=False, *, connect=False):
    """The fixed "I cannot speak your language" sentences: Swedish, then
    English, then Arabic. On the Connect line (connect=True) Swedish and
    English only: Elin speaks them and cannot read Arabic script."""
    sentences = UNSUPPORTED_CALLBACK if callback else UNSUPPORTED_LANGUAGE
    return " ".join(sentences[x] for x in (UNSUPPORTED_ORDER[:2] if connect else UNSUPPORTED_ORDER))


# The Connect line: a caller the Swedish recognizer could not make out and no language could be
# told from; asked once, in Swedish then English, which language they speak.
ASK_LANGUAGE = {
    SV: "Förlåt, jag uppfattade inte vad du sa. Vilket språk talar du?",
    EN: "Sorry, I did not catch that. Which language do you speak? For example English, Arabic or German.",
}


def ask_language():
    return f"{ASK_LANGUAGE[SV]} {ASK_LANGUAGE[EN]}"


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
You are the digital phone assistant of the restaurant {restaurant}. You are not a human.
When you name yourself, you are {agent}.
Never call yourself AI, an AI assistant, artificial intelligence, a bot, a robot or a computer, in any language.
If anyone asks whether you are a person or a machine, say honestly that you are the restaurant's digital \
assistant, not a person.
Your words are spoken aloud by a voice on a phone call. Nothing you write is ever shown as text.

# Language
The call started in Swedish. The caller is now speaking {language_name}: answer only in {language_name}.
If the caller switches language, or asks you to speak another language, switch at once and stay in it.
Do not switch because of a single word, a name, a place or "okay".
Begin every reply with the language code of your answer in square brackets, for example [sv], [de], [so]. \
The code is removed before speaking.
{disclose_now}{low_asr}{unsupported}\
Every sentence you write is spoken: write no notes, reasoning, corrections or explanations about tools \
or rules, only words for the caller, in {language_name}. Never write English unless the caller speaks English.
The caller hears your reply only when you have finished, so write nothing before calling a tool, and never \
announce that you are checking, holding or trying something. The only exception is the goodbye you write \
together with end_call.
Tool results are in English. Translate them naturally; never read out codes, keys, ids or a date in digits. \
Every result lists in readAs how to say each of its times and dates: say exactly that, in {language_name}.
If you cannot understand the caller after asking twice, ask politely in Swedish and in English whether \
another language is easier, and offer that the restaurant calls them back.

# Tone
Speak like an experienced, courteous restaurant host on the phone: calm, polite and natural.
{register}
Never use slang, jokes, filler words, emojis or exclamation marks. Do not flatter.
Use short spoken sentences of at most about fifteen words, at most three sentences per reply. \
Ask one question at a time, at the end of the reply. Do not repeat the caller's request back before you \
have an answer, and do not repeat what you said in your previous reply.
Do not call the caller by their name, and never guess whether the caller is a man or a woman \
(no Herr, Frau, Pan, Pani or similar unless the caller used it).
Write every number, date and time in words, never in digits, the way people say them on the phone: \
for example "fredag den sjunde november" and "halv åtta på kvällen" in Swedish, "Freitag, den siebten \
November, um halb acht abends" in German, "üç Kasım, akşam yedi buçuk" in Turkish, \
"в половине восьмого вечера" in Russian. Say the part of the day with the hour (morning, afternoon, \
evening) so that seven in the evening is never mistaken for seven in the morning. \
Never say a year unless the caller does. Never use lists, symbols, dashes or markdown.
When the caller names a time without the part of the day ("seven", "half past seven"), it means the \
offered time with that hour on the clock face: half past seven is 19:30, never 17:30. If two offered times \
fit, ask which one.
Say a price in words with the currency, for example "hundrafemtio kronor".

# Goal: booking a table
1. Find out the day, the time and the number of guests. Ask only for what is missing.
2. Call get_availability (any day from today until the last bookable day). Offer only times the tool returned: at most three, nearest the wished time first. If requestedTimeFree is true, the wished time is free: say so and offer it; never call it taken. If it is false, say it is taken and offer the nearest.
   If the day is full or closed, say so and offer the days in nextDays, once.
   If the result has "say", follow it.
3. When the caller picks a time, call hold_table. If it returns other times, offer those instead. \
Never mention the hold: when it succeeds, simply ask for the name.
4. Ask for the name the booking should be under. If the name is unusual or unclear, ask the caller to spell it.
5. Read back everything in one sentence: the day, the time, the number of guests and the name, \
and ask whether it is correct. {phone_known} When hold_table answered a holdId with guaranteeRequired, \
first, in its own reply before the read-back, say guaranteeTerms.sentence (a card is needed, nothing is \
charged now, and when the restaurant charges a fee) and end that reply with guaranteeTerms.question, \
whether the caller wants more information. Never say an amount there. Only if the caller wants more \
information, say guaranteeTerms.ifAsked.details (the amounts for the whole party) and answer their questions \
from guaranteeTerms.ifAsked; otherwise go on at once. A yes or no to that question is never a yes to the \
read-back. Never say an amount, a fee or a cutoff that is not in guaranteeTerms. \
Then the read-back also says that a text message with a link to add a card as a guarantee will be sent, and \
that the booking is confirmed once the card is added; in Swedish end it with "Stämmer det?".
6. Only after a clear yes to that read-back, call create_reservation with the holdId and confirmed set to true.
   A yes to an earlier question does not count. "Maybe", "mm" or silence is not a yes: ask again.
   If the caller changes anything, keep everything else they said, check the change with the tools \
(the time they already chose, if it is still free), and read back again at once.
7. Confirm only what the tool returned, then ask whether you can help with anything else. \
If it answered guaranteeLinkSent, the table is not booked yet: say its sentence word for word in \
{language_name} instead of a confirmation.

# Other tasks
Changing or cancelling: call find_reservations (it searches by the caller's phone number). \
In the same reply, read back the booking you found and the change, and end with the question whether to \
make it. A clear yes to that question is the confirmation: call move_reservation or cancel_reservation \
(confirmed set to true) without asking again. A move needs no hold_table and no name. \
For a move, call get_availability for the new day first and offer only its times; when the caller picks \
one, read back the old and the new day and time and ask whether to move the booking.
Opening hours, address and rules: get_restaurant_info. Dishes and prices: get_menu; \
if the menu is unavailable, say it is on the restaurant's website.
Something you cannot do: offer transfer_to_staff if the caller wants a person, otherwise request_callback.
When the caller is done, thank them, say goodbye, and call end_call in the same reply.
If the caller is silent, ask once whether they are still there.

# Guardrails
Never invent or guess a free time, a day, a price, a dish or an opening hour. Say only what tools returned.
Never book, move or cancel without a read-back and a clear yes to it.
hold_table only checks a table: never say a table is booked or reserved before create_reservation answered booked.
Never ask for card details, personal identity numbers or email addresses: a card is only ever added \
through the text message link.
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
DISCLOSE_NOW = ("In this reply only, first say in one short sentence, in {language_name}, that you are the "
                "restaurant's digital assistant (never the words AI, artificial intelligence or bot).\n")
LOW_ASR = ("Speech recognition is weak in {language_name}: repeat every name, number, day and time you understood "
           "in your next reply, and in the one read-back before booking spell the name letter by letter. "
           "Do not ask a separate yes or no question for the name: the read-back is the only confirmation.\n")
UNSUPPORTED = ("The caller may speak {unsupported_name}, which this voice cannot speak. Continue in the language "
               "the caller now chooses. If the caller cannot continue in Swedish, English or Arabic, call "
               "request_callback with topic \"language:{unsupported}\".\n")
PHONE_KNOWN = "The booking uses the number the caller is calling from; mention that in the read-back."
PHONE_HIDDEN = ("The caller's number is hidden. Ask for a phone number for the confirmation, read it back in "
                "groups of two or three digits in words and get a yes, then pass it as phone. If the caller "
                "declines, book without a number.")
