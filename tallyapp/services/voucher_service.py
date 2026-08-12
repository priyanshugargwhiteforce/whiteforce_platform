from .tally_client import TallyClient
from .xml_builder import get_voucher_xml, get_all_ledgers_with_vouchers_xml
from .xml_parser import parse_vouchers


class VoucherService:

    def __init__(self, client: TallyClient):
        self.client = client

    def get_vouchers(self, ledger_name: str, fy: str = "2026-27", from_date=None, to_date=None):
        xml      = get_voucher_xml(ledger_name, fy)
        response = self.client.send(xml)
        vouchers = parse_vouchers(response)

        # Python side date filter
        if from_date and to_date:
            vouchers = [
                v for v in vouchers
                if from_date <= v["date"] <= to_date
            ]

        return vouchers

    def get_all_ledgers_with_vouchers(self, fy: str = "2026-27", from_date=None, to_date=None):
        xml      = get_all_ledgers_with_vouchers_xml(fy, from_date, to_date)
        response = self.client.send(xml)
        vouchers = parse_vouchers(response)

        print(f"TOTAL VOUCHERS FROM TALLY (already date-filtered): {len(vouchers)}")

        # Safety-net filter in case Tally still returns extra rows
        if from_date and to_date:
            vouchers = [
                v for v in vouchers
                if from_date <= v["date"] <= to_date
            ]

        print(f"TOTAL VOUCHERS AFTER DATE FILTER: {len(vouchers)}")

        ledger_map = {}

        for v in vouchers:
            ledger = v["party_ledger"] or "Unknown"

            if ledger not in ledger_map:
                ledger_map[ledger] = {
                    "ledger_name":   ledger,
                    "voucher_count": 0,
                    "total_amount":  0.0,
                    "vouchers":      []
                }

            try:
                amt = float(v["amount"]) if v["amount"] else 0.0
            except:
                amt = 0.0

            ledger_map[ledger]["voucher_count"] += 1
            ledger_map[ledger]["total_amount"]  += amt
            ledger_map[ledger]["vouchers"].append(v)

        # Sirf woh ledgers jo selected period mein vouchers hain
        result = [v for v in ledger_map.values() if v["voucher_count"] > 0]

        return result