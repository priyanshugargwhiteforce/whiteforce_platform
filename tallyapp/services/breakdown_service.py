import re
from lxml import etree

from .tally_client import TallyClient
from .xml_builder import get_voucher_detail_xml


class BreakdownService:

    # ----------------------------
    # HELPERS
    # ----------------------------
    def __init__(self, client: TallyClient):
        self.client = client
    @staticmethod
    def safe_float(value):
        try:
            return float((value or "0").strip())
        except:
            return 0.0

    @staticmethod
    def clean_text(value):
        return (value or "").strip()

    @staticmethod
    def normalize(text):
        return (text or "").lower().strip()

    # ----------------------------
    # XML CLEANER
    # ----------------------------
    @staticmethod
    def clean_xml(xml: str) -> str:
        if not xml:
            return ""

        xml = re.sub(r"[\x00-\x08\x0B\x0C\x0E-\x1F]", "", xml)

        def filter_char_ref(match):
            ref = match.group(0)
            try:
                if ref.startswith("&#x") or ref.startswith("&#X"):
                    codepoint = int(ref[3:-1], 16)
                else:
                    codepoint = int(ref[2:-1])
                if (
                    codepoint in (0x9, 0xA, 0xD)
                    or (0x20 <= codepoint <= 0xD7FF)
                    or (0xE000 <= codepoint <= 0xFFFD)
                ):
                    return ref
                return ""
            except:
                return ""

        xml = re.sub(r"&#(?:x[0-9A-Fa-f]+|[0-9]+);", filter_char_ref, xml)
        return xml

    # ----------------------------
    # VALIDATION
    # ----------------------------
    @staticmethod
    def is_valid(xml):
        return isinstance(xml, str) and "<VOUCHER" in xml and "</VOUCHER>" in xml

    # ----------------------------
    # MAIN FUNCTION
    # ----------------------------
    # @staticmethod
    def get_breakdown(self, master_id):

        xml = get_voucher_detail_xml(master_id)
        response = self.client.send(xml)

        print(f"\n===== BREAKDOWN DEBUG (length={len(response)}) =====")
        print(response[:2000])
        print("==========================\n")

        response = BreakdownService.clean_xml(response)

        if not BreakdownService.is_valid(response):
            return {
                "error": "Invalid or incomplete XML from Tally",
                "raw_response": response[:500]
            }

        try:
            parser = etree.XMLParser(recover=True, encoding="utf-8")
            root = etree.fromstring(response.encode("utf-8"), parser)
        except Exception as e:
            return {
                "error": "XML parsing failed",
                "details": str(e),
                "raw_response": response[:500]
            }

        # -------------------------------------------------------
        # KEY FIX: find VOUCHER with REMOTEID attribute
        # root.find(".//VOUCHER") picks up <VOUCHER>77</VOUCHER>
        # inside <CMPINFO> which has zero children
        # -------------------------------------------------------
        voucher = root.find(".//VOUCHER[@REMOTEID]")

        if voucher is None:
            return {"error": "Voucher not found in XML"}

        # ----------------------------
        # BASE RESULT
        # ----------------------------
        result = {
            "master_id":    BreakdownService.clean_text(voucher.findtext("MASTERID")),
            "voucher_no":   BreakdownService.clean_text(voucher.findtext("VOUCHERNUMBER")),
            "date":         BreakdownService.clean_text(voucher.findtext("DATE")),
            "party":        BreakdownService.clean_text(voucher.findtext("PARTYLEDGERNAME")),
            "voucher_type": BreakdownService.clean_text(voucher.findtext("VOUCHERTYPENAME")),
            "narration":    BreakdownService.clean_text(voucher.findtext("NARRATION")),
            "total_amount": abs(BreakdownService.safe_float(voucher.findtext("AMOUNT"))),

            "net_amount":   0.0,
            "agency_fees":  0.0,
            "igst":         0.0,
            "round_off":    0.0,
        }

        # ----------------------------
        # LEDGER BREAKDOWN
        # ----------------------------
        entries = (
            voucher.xpath(".//ALLLEDGERENTRIES.LIST")
            or voucher.xpath(".//LEDGERENTRIES.LIST")
        )

        for entry in entries:
            ledger = BreakdownService.normalize(entry.findtext("LEDGERNAME"))
            amount = BreakdownService.safe_float(entry.findtext("AMOUNT"))

            if "outsourcing" in ledger:
                result["net_amount"] += amount
            elif "agency fees" in ledger:
                result["agency_fees"] += amount
            elif "igst" in ledger:
                result["igst"] += amount
            elif "round off" in ledger:
                result["round_off"] += amount

        return result