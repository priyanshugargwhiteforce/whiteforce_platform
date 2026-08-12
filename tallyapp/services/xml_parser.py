import re
from lxml import etree


def clean_xml(xml: str) -> str:
    if not xml:
        return ""
    xml = re.sub(r"[\x00-\x08\x0B\x0C\x0E-\x1F]", "", xml)
    def filter_char_ref(match):
        ref = match.group(0)
        try:
            cp = int(ref[3:-1], 16) if ref.startswith(("&#x", "&#X")) else int(ref[2:-1])
            if cp in (0x9, 0xA, 0xD) or (0x20 <= cp <= 0xD7FF) or (0xE000 <= cp <= 0xFFFD):
                return ref
            return ""
        except:
            return ""
    return re.sub(r"&#(?:x[0-9A-Fa-f]+|[0-9]+);", filter_char_ref, xml)


def safe_float(val):
    try:
        return float((val or "0").strip())
    except:
        return 0.0


# ==========================================
# Parse Ledger List
# ==========================================
def parse_ledgers(xml_response):
    xml_response = clean_xml(xml_response)
    parser = etree.XMLParser(recover=True, encoding="utf-8")
    root = etree.fromstring(xml_response.encode("utf-8"), parser)

    ledgers = []
    for ledger in root.xpath("//LEDGER"):
        ledgers.append({
            "name": ledger.get("NAME", "")
        })

    return ledgers

def parse_group_ledgers(xml_response):
    xml_response = clean_xml(xml_response)
    parser = etree.XMLParser(recover=True, encoding="utf-8")
    root = etree.fromstring(xml_response.encode("utf-8"), parser)

    ledgers = []
    for ledger in root.xpath("//LEDGER[@NAME]"):
        opening = safe_float(ledger.findtext("OPENINGBALANCE"))
        closing = safe_float(ledger.findtext("CLOSINGBALANCE"))
        ledgers.append({
            "ledger_name":       ledger.get("NAME", "").strip(),
            "parent":            (ledger.findtext("PARENT") or "").strip(),
            "opening_balance":   opening,
            "closing_balance":   closing,
            "nett_transactions": round(closing - opening, 2),
        })

    return ledgers
def parse_ledger_opening_balance(xml_response):
    xml_response = clean_xml(xml_response)
    parser = etree.XMLParser(recover=True, encoding="utf-8")
    root = etree.fromstring(xml_response.encode("utf-8"), parser)

    ledger = root.find(".//LEDGER[@NAME]")
    if ledger is None:
        return 0.0
    return safe_float(ledger.findtext("OPENINGBALANCE"))
# ==========================================
# Parse Voucher List (with breakdown)
# ==========================================
def parse_vouchers(xml_response):
    xml_response = clean_xml(xml_response)
    parser = etree.XMLParser(recover=True, encoding="utf-8")
    root = etree.fromstring(xml_response.encode("utf-8"), parser)

    voucher_nodes = root.xpath("//VOUCHER[@REMOTEID]")
    print("\nTOTAL VOUCHERS:", len(voucher_nodes))

    vouchers = []

    for voucher in voucher_nodes:

        # --- Breakdown ---
        breakdown = {
            "net_amount":  0.0,
            "agency_fees": 0.0,
            "igst":        0.0,
            "round_off":   0.0,
            "entries":     []
        }

        entries = (
            voucher.xpath(".//ALLLEDGERENTRIES.LIST")
            or voucher.xpath(".//LEDGERENTRIES.LIST")
        )

        for entry in entries:
            ledger = (entry.findtext("LEDGERNAME") or "").lower().strip()
            amt    = safe_float(entry.findtext("AMOUNT"))

            breakdown["entries"].append({
                "ledger": (entry.findtext("LEDGERNAME") or "").strip(),
                "amount": amt
            })

            if "outsourcing" in ledger:
                breakdown["net_amount"]  += amt
            elif "agency fees" in ledger:
                breakdown["agency_fees"] += amt
            elif "igst" in ledger:
                breakdown["igst"]        += amt
            elif "round off" in ledger:
                breakdown["round_off"]   += amt

        vouchers.append({
            "master_id":    voucher.xpath("string(MASTERID)").strip(),
            "guid":         voucher.xpath("string(GUID)").strip(),
            "voucher_no":   voucher.xpath("string(VOUCHERNUMBER)").strip(),
            "date":         voucher.xpath("string(DATE)").strip(),
            "voucher_type": voucher.xpath("string(VOUCHERTYPENAME)").strip(),
            "party_ledger": voucher.xpath("string(PARTYLEDGERNAME)").strip(),
            "amount":       voucher.xpath("string(AMOUNT)").strip(),
            "narration":    voucher.xpath("string(NARRATION)").strip(),
            "breakdown":    breakdown,
        })

    if vouchers:
        print("\n===== FIRST VOUCHER =====")
        print(vouchers[0])
        print("=========================\n")

    return vouchers


# ==========================================
# Parse Voucher Breakdown (standalone)
# ==========================================
def parse_breakdown(xml_response):
    xml_response = clean_xml(xml_response)
    parser = etree.XMLParser(recover=True, encoding="utf-8")
    root = etree.fromstring(xml_response.encode("utf-8"), parser)

    voucher = root.find(".//VOUCHER[@REMOTEID]")

    if voucher is None:
        return {"error": "Voucher not found"}

    ledger_entries = []
    for entry in (
        voucher.findall(".//ALLLEDGERENTRIES.LIST")
        or voucher.findall(".//LEDGERENTRIES.LIST")
    ):
        ledger_entries.append({
            "ledger_name": (entry.findtext("LEDGERNAME") or "").strip(),
            "amount":      (entry.findtext("AMOUNT") or "").strip()
        })

    return {
        "master_id":     (voucher.findtext("MASTERID")       or "").strip(),
        "guid":          (voucher.findtext("GUID")           or "").strip(),
        "voucher_no":    (voucher.findtext("VOUCHERNUMBER")  or "").strip(),
        "date":          (voucher.findtext("DATE")           or "").strip(),
        "voucher_type":  (voucher.findtext("VOUCHERTYPENAME")or "").strip(),
        "party_ledger":  (voucher.findtext("PARTYLEDGERNAME")or "").strip(),
        "amount":        (voucher.findtext("AMOUNT")         or "").strip(),
        "ledger_entries": ledger_entries
    }