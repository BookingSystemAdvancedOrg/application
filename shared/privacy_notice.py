"""The restaurant's privacy notice for its guests (integritetspolicy).

The restaurant is the controller and owns the text: it starts from the
template below (filled with its name, org. number, address, contact and the
retention periods) and can edit it in admin (Inställningar > Integritet).
Stored on the tenant PROFILE as `privacyNotice {body, version, updatedAt,
updatedBy}`; every save is a new version. site-config serves the saved text,
or the template when none is saved.

Format: plain text, no HTML. "## " starts a heading, "- " a list item, a
blank line a new paragraph. The site renders it as text (never as HTML), and
always appends the generated cookie and storage table after it.
"""

from shared import retention

MAX_LENGTH = 20_000
TEMPLATE_VERSION = "template_v1"


def _contact(row):
    parts = [row.get("contactEmail") or row.get("replyToEmail"), row.get("contactPhone")]
    return ", ".join(p for p in parts if p)


def _address(row):
    value = row.get("address")
    if isinstance(value, dict):
        street = value.get("street") or ""
        city = " ".join(p for p in (value.get("postalCode"), value.get("city")) if p)
        return ", ".join(p for p in (street, city) if p)
    return value if isinstance(value, str) else ""


def template(row, periods=None):
    """The default text, filled with the restaurant's details."""
    periods = periods or retention.public_periods()
    name = row.get("name") or "Restaurangen"
    legal = row.get("legalName") or name
    org = f" (org.nr {row['orgNumber']})" if row.get("orgNumber") else ""
    address = _address(row)
    contact = _contact(row)
    marketing = bool(((row.get("entitlements") or {}).get("features") or {}).get("marketing"))
    lines = [
        f"## Personuppgiftsansvarig",
        f"{legal}{org}{', ' + address if address else ''} ansvarar för behandlingen av dina personuppgifter."
        + (f" Kontakt: {contact}." if contact else ""),
        "",
        "## Vilka uppgifter vi behandlar och varför",
        "- Bokning: namn, e-post, telefonnummer, antal gäster, datum och tid samt ditt eventuella meddelande "
        "(till exempel allergier). Vi behöver uppgifterna för att ta emot och hantera bokningen och kontakta dig "
        "om den (rättslig grund: avtal).",
        "- Beställning: namn, e-post, telefonnummer, det du beställer och betalningen. Vi behöver uppgifterna för "
        "att laga, lämna ut och ta betalt för beställningen (avtal) och för bokföringen (rättslig förpliktelse).",
        "- Catering: kontaktuppgifter, eventuellt företag och leveransadress samt det du frågar efter. Vi behöver "
        "uppgifterna för att ta fram en offert på din begäran och leverera (åtgärder före avtal och avtal).",
        "- Meddelanden om bokningar och beställningar: bekräftelser, kvitton, påminnelser och ändringar skickas "
        "till din e-post eller ditt telefonnummer (avtal). De påverkas inte av om du vill ha erbjudanden.",
        "- Allergier och liknande som du själv skriver i meddelandet används bara för att förbereda ditt besök.",
        "- Uppgifterna sparas först när du skickar formuläret. Det du skriver innan dess sparas inte.",
    ]
    if marketing:
        lines += [
            "- Erbjudanden: bara om du har kryssat i rutan för erbjudanden och valt e-post, sms eller båda. Vi "
            "sparar då ditt samtycke (vad du godkände, när och var) och skickar nyheter och erbjudanden via den "
            "kanal du valde (rättslig grund: samtycke). Du kan ta tillbaka samtycket när som helst via länken i "
            "våra mejl eller genom att kontakta oss.",
            "- Besöksstatistik: vi räknar besök per dag och varifrån de kom, utan cookies och utan att spara din "
            "IP-adress eller något id (berättigat intresse).",
        ]
    lines += [
        "",
        "## Hur länge uppgifterna sparas",
        f"- Bokningar raderas cirka {periods['bookingMonths']} månader efter besöket.",
        f"- Namn och kontaktuppgifter på beställningar och cateringförfrågningar tas bort efter "
        f"{periods['guestMonths']} månader. Förfrågningar som inte blev av raderas efter "
        f"{periods['cancelledMonths']} månader.",
        f"- Kvitton och annat bokföringsunderlag sparas i {periods['accountingYears']} år enligt "
        "bokföringslagen, men utan dina kontaktuppgifter.",
        "- Samtycke till erbjudanden sparas så länge det gäller. Tar du tillbaka det sparar vi bara en "
        "anteckning om att du inte vill ha utskick, så att du inte får några av misstag.",
        "",
        "## Vem uppgifterna delas med",
        "Bokningssystemet levereras av IT Hjälparna Nordic AB, som behandlar uppgifterna för vår räkning "
        "(personuppgiftsbiträde) på servrar inom EU (Amazon Web Services, Stockholm). Betalningar och "
        "kortgarantier hanteras av Stripe; vi ser aldrig ditt kortnummer. Vi säljer aldrig dina uppgifter.",
        "",
        "## Dina rättigheter",
        "Du har rätt att få veta vilka uppgifter vi har om dig, få dem rättade eller raderade, begränsa "
        "behandlingen, invända och få ut dina uppgifter (dataportabilitet)."
        + (f" Kontakta oss på {contact}." if contact else " Kontakta oss.")
        + " Du kan också klaga hos Integritetsskyddsmyndigheten (IMY), www.imy.se.",
    ]
    return "\n".join(lines)


def validate(body):
    if not isinstance(body, str):
        raise ValueError("body must be text")
    text = body.replace("\r\n", "\n").strip()
    if not text:
        raise ValueError("body is required")
    if len(text) > MAX_LENGTH:
        raise ValueError(f"body must be at most {MAX_LENGTH} characters")
    if "<" in text and ">" in text and any(tag in text.lower() for tag in ("<script", "<iframe", "<style")):
        raise ValueError("body must be plain text")
    return text


def public(row):
    """What site-config serves: the saved text or the filled template."""
    saved = row.get("privacyNotice") if isinstance(row.get("privacyNotice"), dict) else None
    if saved and isinstance(saved.get("body"), str) and saved["body"].strip():
        return {"body": saved["body"], "version": int(saved.get("version") or 1),
                "updatedAt": saved.get("updatedAt"), "custom": True}
    return {"body": template(row), "version": 0, "updatedAt": None, "custom": False}
