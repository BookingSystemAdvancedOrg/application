"""catering-document

TRIGGER:
    Lambda invoke (RequestResponse) from catering-offer only; no API route.
    {"tenantId", "requestId", "version", "document": <offer_document>,
     "restaurant": {"name", "address", "phone", "email"}}

PURPOSE:
    Renders one offer version as a PDF (WeasyPrint, PDF/A-3b) in Swedish:
    seller and customer, delivery, every line with its VAT rate, totals with
    the VAT breakdown, validity, terms, and that both parties sign it. Writes
    it to <tenantId>/offers/<requestId>/v<NNNN>.pdf, tagged retention=offer
    (the bucket's lifecycle expires unsigned offers after 13 months; a signed
    one is copied to signed/ with its lock by catering-customer).
    Answers {"key", "sha256", "size", "pdf": base64}: the SHA-256 of these
    exact bytes is what the parties sign.

ENV_VARS:
    ENVIRONMENT, CATERING_DOCUMENTS_BUCKET_NAME

AWS RESOURCE ACCESS:
    S3 catering-documents: PutObject, PutObjectTagging on */offers/*
"""

import base64
import hashlib
import html
import json
import logging
import os
from datetime import date

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

_MONTHS = ("januari", "februari", "mars", "april", "maj", "juni", "juli", "augusti", "september", "oktober",
           "november", "december")


def money(ore):
    text = f"{int(ore) / 100:,.2f}".replace(",", " ").replace(".", ",")
    return (text[:-3] if text.endswith(",00") else text) + " kr"


def day(value):
    try:
        d = date.fromisoformat(str(value))
    except ValueError:
        return html.escape(str(value or ""))
    return f"{d.day} {_MONTHS[d.month - 1]} {d.year}"


def _e(value):
    return html.escape(str(value or ""))


CSS = """
@page { size: A4; margin: 22mm 18mm 20mm; @bottom-right { content: "Sida " counter(page) " av " counter(pages);
  font-size: 8pt; color: #6b625c; } }
body { font-family: "DejaVu Sans", sans-serif; font-size: 9.5pt; color: #1d1a18; line-height: 1.45; }
h1 { font-size: 20pt; margin: 0 0 2mm; color: #350001; letter-spacing: -0.01em; }
h2 { font-size: 11pt; margin: 8mm 0 2mm; color: #350001; }
.muted { color: #6b625c; }
.parties { display: flex; gap: 10mm; margin-top: 6mm; }
.parties div { flex: 1; border-top: 0.6pt solid #b9aea6; padding-top: 2mm; }
table { width: 100%; border-collapse: collapse; margin-top: 2mm; }
th { text-align: left; font-size: 8pt; text-transform: uppercase; letter-spacing: 0.04em; color: #6b625c;
  border-bottom: 0.6pt solid #b9aea6; padding: 1.5mm 0; }
td { padding: 1.6mm 0; border-bottom: 0.3pt solid #e4ddd6; vertical-align: top; }
.num { text-align: right; white-space: nowrap; }
.total td { border-bottom: 0; font-weight: bold; font-size: 11pt; padding-top: 3mm; }
.box { border: 0.6pt solid #b9aea6; padding: 3mm 4mm; margin-top: 4mm; }
.sign { display: flex; gap: 10mm; margin-top: 10mm; }
.sign div { flex: 1; border-top: 0.6pt solid #1d1a18; padding-top: 2mm; font-size: 8.5pt; }
"""


def render_html(doc, restaurant):
    seller, customer, delivery = doc.get("seller") or {}, doc.get("customer") or {}, doc.get("delivery") or {}
    totals = doc.get("totals") or {}
    rate = doc.get("vatRate")
    who = _e(customer.get("companyName") or customer.get("name"))
    if customer.get("companyName"):
        who += f"<br>Org.nr {_e(customer.get('orgNumber'))}<br>Kontakt: {_e(customer.get('name'))}"
    signatory = doc.get("signatory")
    if signatory:
        who += f"<br>Signerar: {_e(signatory.get('name'))}, {_e(signatory.get('role'))}"
    address = delivery.get("address") or {}
    where = (f"Leverans till {_e(address.get('street'))}, {_e(address.get('postalCode'))} {_e(address.get('city'))}"
             if delivery.get("fulfilment") == "delivery" else f"Avhämtning hos {_e(restaurant.get('address'))}")
    rows = "".join(
        f"<tr><td>{_e(line['name'])}</td><td class='num'>{int(line['quantity'])}</td>"
        f"<td class='num'>{money(line['unitPrice'])}</td>"
        f"<td class='num'>{int(line['vatRate']) if line.get('vatRate') is not None else ''}"
        f"{' %' if line.get('vatRate') is not None else ''}</td>"
        f"<td class='num'>{money(int(line['unitPrice']) * int(line['quantity']))}</td></tr>"
        for line in doc.get("lines") or [])
    extra = ""
    if int(totals.get("discount") or 0):
        extra += f"<tr><td colspan='4'>Rabatt</td><td class='num'>-{money(totals['discount'])}</td></tr>"
    if int(totals.get("deliveryFee") or 0):
        extra += f"<tr><td colspan='4'>Leverans</td><td class='num'>{money(totals['deliveryFee'])}</td></tr>"
    if int(doc.get("cleaningFee") or 0):
        extra += (f"<tr><td colspan='4'>Rengöring av utrustning</td>"
                  f"<td class='num'>{money(doc['cleaningFee'])}</td></tr>")
    vat_text = f"varav moms {rate} %" if rate is not None else "varav moms"
    service = ""
    if doc.get("serviceIncluded") is not None:
        service = ("Servering ingår." if doc["serviceIncluded"] else "Mat utan servering.")
    payment = (f"Faktura, {int(doc['paymentTermsDays'])} dagar netto." if doc.get("paymentTermsDays")
               else "Betalas med kort eller Swish efter signering.")
    message = f"<div class='box'>{_e(doc.get('message'))}</div>" if doc.get("message") else ""
    terms = (f"<h2>Villkor</h2><p>{_e(doc.get('terms')).replace(chr(10), '<br>')}</p>" if doc.get("terms") else "")
    return f"""<!doctype html><html lang="sv"><head><meta charset="utf-8">
<title>Offert catering v{int(doc['version'])}</title><style>{CSS}</style></head><body>
<h1>Offert catering</h1>
<p class="muted">Version {int(doc['version'])} · Giltig till och med {day(doc.get('validUntil'))}</p>
<div class="parties">
  <div><strong>Säljare</strong><br>{_e(seller.get('name'))}<br>Org.nr {_e(seller.get('orgNumber'))}<br>
    {_e(seller.get('location'))}<br>{_e(restaurant.get('address'))}<br>{_e(restaurant.get('phone'))}
    {('<br>' + _e(restaurant.get('email'))) if restaurant.get('email') else ''}</div>
  <div><strong>Kund</strong><br>{who}<br>{_e(customer.get('email'))}</div>
</div>
<h2>Leverans</h2>
<p>{day(delivery.get('date'))} kl {_e(delivery.get('time'))}. {where}.
{f"Antal gäster: {int(doc['guests'])}." if doc.get('guests') else ''} {service}</p>
<h2>Innehåll</h2>
<table><thead><tr><th>Beskrivning</th><th class="num">Antal</th><th class="num">Á-pris</th>
<th class="num">Moms</th><th class="num">Summa</th></tr></thead>
<tbody>{rows}{extra}
<tr class="total"><td colspan="4">Totalt inkl. moms</td><td class="num">{money(totals.get('total') or 0)}</td></tr>
<tr><td colspan="4" class="muted">{vat_text}</td><td class="num muted">{money(totals.get('vat') or 0)}</td></tr>
</tbody></table>
<p class="muted">Alla priser i svenska kronor inklusive moms. {payment}</p>
{message}{terms}
<h2>Godkännande</h2>
<p>Avtalet blir bindande när både restaurangen och kunden har signerat den här versionen av offerten.
Restaurangen signerar först. Ändras offerten efter att den skickats skapas en ny version och tidigare
signaturer gäller inte.</p>
<div class="sign"><div>Restaurangen<br>{_e(seller.get('name'))}</div><div>Kunden<br>{who}</div></div>
<p class="muted" style="margin-top:8mm">Förfrågan {_e(doc.get('requestId'))}</p>
</body></html>"""


def render_pdf(page_html):
    from weasyprint import HTML  # imported here: the native Pango libraries exist only in the image

    return HTML(string=page_html).write_pdf(pdf_variant="pdf/a-3b")


def handler(event, context):
    tenant_id, request_id, version = event["tenantId"], event["requestId"], int(event["version"])
    if not (isinstance(tenant_id, str) and tenant_id and "/" not in tenant_id and isinstance(request_id, str)
            and request_id.isalnum()):
        raise ValueError("tenantId and requestId are required")
    pdf = render_pdf(render_html(event["document"], event.get("restaurant") or {}))
    key = f"{tenant_id}/offers/{request_id}/v{version:04d}.pdf"
    boto3.client("s3").put_object(Bucket=os.environ["CATERING_DOCUMENTS_BUCKET_NAME"], Key=key, Body=pdf,
                                  ContentType="application/pdf", ChecksumAlgorithm="SHA256",
                                  Tagging="retention=offer")
    sha = hashlib.sha256(pdf).hexdigest()
    logger.info(json.dumps({"document": "offer", "requestId": request_id, "version": version, "bytes": len(pdf)}))
    return {"key": key, "sha256": sha, "size": len(pdf), "pdf": base64.b64encode(pdf).decode("ascii")}
