from concurrent.futures import ThreadPoolExecutor, as_completed
from unittest import result

from .tally_client import TallyClient
from .xml_builder import get_group_ledgers_xml, get_ledger_opening_balance_xml
from .xml_parser import parse_group_ledgers, parse_ledger_opening_balance
from .voucher_service import VoucherService

MONTH_KEYS = [
    "04", "05", "06", "07", "08", "09",
    "10", "11", "12", "01", "02", "03",
]

MONTH_LABELS = {
    "01": "January",  "02": "February", "03": "March",
    "04": "April",    "05": "May",      "06": "June",
    "07": "July",     "08": "August",   "09": "September",
    "10": "October",  "11": "November", "12": "December",
}


class EMDService:
    """
    Drill-down chain:
    Balance Sheet -> Current Assets -> Deposits (Asset) ->
    Security Deposit (SD) in Form of Other -> Deposits - EMD -> party ledgers
    (+ nested "EMD" sub-group ledgers) -> monthly breakdown per party
    """

    def __init__(self, client: TallyClient):
        self.client = client
        self.voucher_service = VoucherService(client)

    # ----------------------------------------------------------------
    # Group ledgers (single group)
    # ----------------------------------------------------------------
    def get_group_ledgers(self, group_name="Deposits - EMD"):
        xml = get_group_ledgers_xml(group_name)
        response = self.client.send(xml)
        return parse_group_ledgers(response)

    # ----------------------------------------------------------------
    # Closed EMD -> original amount + closing month
    # (searches a wide date range since the closing voucher may belong
    # to an earlier FY than the one currently selected)
    # ----------------------------------------------------------------
    def _get_closed_emd_details(self, ledger_name):
        vouchers = self.voucher_service.get_vouchers(
            ledger_name, from_date="20200401", to_date="20270331"
        )

        if not vouchers:
            return None, None

        sorted_vouchers = sorted(vouchers, key=lambda v: v.get("date", ""))
        last_voucher = sorted_vouchers[-1]

        date_str = last_voucher.get("date", "")
        month_key = date_str[4:6] if len(date_str) >= 6 else None
        closing_month = MONTH_LABELS.get(month_key, "Unknown")

        try:
            emd_amount = abs(float(last_voucher["amount"])) if last_voucher["amount"] else 0.0
        except (TypeError, ValueError):
            emd_amount = 0.0

        return emd_amount, closing_month

    # ----------------------------------------------------------------
    # Main list: merges direct ledgers under "Deposits - EMD" with the
    # nested "EMD" sub-group's ledgers, dedupes, and attaches
    # closed/not-closed status + amount + closing month.
    # ----------------------------------------------------------------
    def get_group_ledgers_with_status(self, group_name="Deposits - EMD"):
        direct_ledgers = self.get_group_ledgers(group_name)
        sub_group_ledgers = self.get_group_ledgers("EMD")

        all_ledgers = direct_ledgers + sub_group_ledgers

        seen = set()
        unique_ledgers = []
        for ledger in all_ledgers:
            if ledger["ledger_name"] not in seen:
                seen.add(ledger["ledger_name"])
                unique_ledgers.append(ledger)

        result = []
        for ledger in unique_ledgers:
            opening = ledger["opening_balance"]
            closing = ledger["closing_balance"]
            nett = ledger["nett_transactions"]

            if abs(opening) < 0.01 and abs(nett) < 0.01 and abs(closing) < 0.01:
                status = "No Activity"
            elif abs(closing) < 0.01:
                status = "Closed"
            else:
                status = "Not Closed"

            result.append({
                "ledger_name":       ledger["ledger_name"],
                "parent":            ledger["parent"],
                "opening_balance":   opening,
                "nett_transactions": nett,
                "closing_balance":   closing,
                "emd_status":        status,
        })

        return result
    # ----------------------------------------------------------------
    # Single-ledger detailed monthly breakdown (used by EMDBreakdownView)
    # ----------------------------------------------------------------
    def get_ledger_monthly_breakdown(self, ledger_name, fy="2026-27", from_date=None, to_date=None):
        """
        Tally XML sign convention: negative amount = Debit, positive amount = Credit.
        """
        ob_xml = get_ledger_opening_balance_xml(ledger_name)
        ob_response = self.client.send(ob_xml)
        opening_balance = parse_ledger_opening_balance(ob_response)

        vouchers = self.voucher_service.get_vouchers(ledger_name, fy, from_date, to_date)

        buckets = {m: {"debit": 0.0, "credit": 0.0, "voucher_count": 0} for m in MONTH_KEYS}

        if opening_balance < 0:
            buckets["04"]["debit"] += abs(opening_balance)
        elif opening_balance > 0:
            buckets["04"]["credit"] += opening_balance

        for v in vouchers:
            date_str = v.get("date", "")
            if len(date_str) < 6:
                continue
            month_key = date_str[4:6]
            if month_key not in buckets:
                continue

            try:
                amt = float(v["amount"]) if v["amount"] else 0.0
            except (TypeError, ValueError):
                amt = 0.0

            if amt < 0:
                buckets[month_key]["debit"] += abs(amt)
            else:
                buckets[month_key]["credit"] += amt
            buckets[month_key]["voucher_count"] += 1

        monthly = [
            {
                "month": MONTH_LABELS[m],
                "debit": round(buckets[m]["debit"], 2),
                "credit": round(buckets[m]["credit"], 2),
                "voucher_count": buckets[m]["voucher_count"],
            }
            for m in MONTH_KEYS
        ]

        return {
            "ledger_name": ledger_name,
            "opening_balance": round(opening_balance, 2),
            "monthly_breakdown": monthly,
            "vouchers": vouchers,
        }