from django.db import transaction
from django.db.models import F
from ..models import TallyInstance, Ledger, Voucher, VoucherBreakdown, EMDLedger


class PersistenceService:
    """
    Takes parsed Tally API responses (dicts from xml_parser.py) and
    upserts them into Postgres. No duplicate rows regardless of how
    many times the same API is called.
    """

    def __init__(self, tally_tag):
        self.tally_instance, _ = TallyInstance.objects.get_or_create(
            tag=tally_tag,
            defaults={"name": tally_tag.title(), "url": ""}
        )

    def save_ledgers(self, ledger_list):
        """
        ledger_list: output of LedgerService.get_all()
        e.g. [{"name": "ABC Contractors"}, ...]
        """
        saved = []
        for item in ledger_list:
            name = item.get("name") or item.get("ledger_name")
            if not name:
                continue
            ledger, _ = Ledger.objects.update_or_create(
                tally_instance=self.tally_instance,
                ledger_name=name,
                defaults={
                    "parent": item.get("parent", ""),
                    "closing_balance": item.get("closing_balance") or 0,
                },
            )
            saved.append(ledger)
        return saved

    def save_vouchers(self, ledger_name, voucher_list, financial_year=""):
        """
        voucher_list: output of VoucherService.get_vouchers()
        Each item has 'guid', 'breakdown': {'entries': [...]}
        """
        ledger, _ = Ledger.objects.get_or_create(
            tally_instance=self.tally_instance,
            ledger_name=ledger_name,
        )

        saved = []
        with transaction.atomic():
            for item in voucher_list:
                guid = item.get("guid")
                if not guid:
                    continue  # can't dedupe without a stable identity

                voucher, created = Voucher.objects.update_or_create(
                    ledger=ledger,
                    guid=guid,
                    defaults={
                        "master_id": item.get("master_id", ""),
                        "voucher_no": item.get("voucher_no", ""),
                        "voucher_type": item.get("voucher_type", ""),
                        "party_ledger": item.get("party_ledger", ""),
                        "date": item.get("date", ""),
                        "amount": self._safe_decimal(item.get("amount")),
                        "narration": item.get("narration", ""),
                        "financial_year": financial_year,
                    },
                )

                # replace breakdown lines wholesale (simplest safe approach)
                breakdown = item.get("breakdown", {})
                entries = breakdown.get("entries", [])
                VoucherBreakdown.objects.filter(voucher=voucher).delete()
                VoucherBreakdown.objects.bulk_create([
                    VoucherBreakdown(
                        voucher=voucher,
                        ledger_name=e.get("ledger", ""),
                        amount=self._safe_decimal(e.get("amount")),
                    )
                    for e in entries
                ])

                saved.append(voucher)

            # keep voucher_count in sync (atomic F() expression, no race condition)
            Ledger.objects.filter(pk=ledger.pk).update(
                voucher_count=Voucher.objects.filter(ledger=ledger).count()
            )

        return saved

    def save_emd_ledgers(self, emd_list, financial_year=""):
        """
        emd_list: output of EMDService.get_group_ledgers_with_status()
        Each item: {ledger_name, parent, opening_balance, nett_transactions,
                    closing_balance, emd_status}
        """
        saved = []
        for item in emd_list:
            print(f"[DEBUG] Saving: {item.get('ledger_name')} -> opening={item.get('opening_balance')}, closing={item.get('closing_balance')}")
            name = item.get("ledger_name")
            if not name:
                continue

            ledger, _ = Ledger.objects.update_or_create(
                tally_instance=self.tally_instance,
                ledger_name=name,
                defaults={
                    "parent": item.get("parent", ""),
                    "closing_balance": self._safe_decimal(item.get("closing_balance")),
                },
            )

            emd, _ = EMDLedger.objects.update_or_create(
                ledger=ledger,
                defaults={
                    "group_name":        item.get("parent", "Deposits - EMD"),
                    "opening_balance":   self._safe_decimal(item.get("opening_balance")),
                    "nett_transactions": self._safe_decimal(item.get("nett_transactions")),
                    "closing_balance":   self._safe_decimal(item.get("closing_balance")),
                    "emd_status":        item.get("emd_status", "Not Closed"),
                    "financial_year":    financial_year,
                },
            )
            saved.append(emd)
        return saved

    def save_all_ledgers_with_vouchers(self, grouped_data, financial_year=""):
        """
        grouped_data: output of VoucherService.get_all_ledgers_with_vouchers()
        Shape: [{"ledger_name": ..., "voucher_count": ..., "vouchers": [...]}, ...]
        """
        total_vouchers_saved = 0

        with transaction.atomic():
            for group in grouped_data:
                ledger_name = group.get("ledger_name")
                if not ledger_name or ledger_name == "Unknown":
                    continue

                ledger, _ = Ledger.objects.update_or_create(
                    tally_instance=self.tally_instance,
                    ledger_name=ledger_name,
                    defaults={},  # closing_balance not present in this response shape
                )

                for item in group.get("vouchers", []):
                    guid = item.get("guid")
                    if not guid:
                        continue  # skip — can't dedupe without a stable identity

                    voucher, _ = Voucher.objects.update_or_create(
                        ledger=ledger,
                        guid=guid,
                        defaults={
                            "master_id": item.get("master_id", ""),
                            "voucher_no": item.get("voucher_no", ""),
                            "voucher_type": item.get("voucher_type", ""),
                            "party_ledger": item.get("party_ledger", ""),
                            "date": item.get("date", ""),
                            "amount": self._safe_decimal(item.get("amount")),
                            "narration": item.get("narration", ""),
                            "financial_year": financial_year,
                        },
                    )

                    # breakdown lines — replace wholesale on every sync
                    entries = item.get("breakdown", {}).get("entries", [])
                    VoucherBreakdown.objects.filter(voucher=voucher).delete()
                    VoucherBreakdown.objects.bulk_create([
                        VoucherBreakdown(
                            voucher=voucher,
                            ledger_name=e.get("ledger", ""),
                            amount=self._safe_decimal(e.get("amount")),
                        )
                        for e in entries
                    ])
                    total_vouchers_saved += 1

                # sync voucher_count for this ledger
                Ledger.objects.filter(pk=ledger.pk).update(
                    voucher_count=Voucher.objects.filter(ledger=ledger).count()
                )

        return total_vouchers_saved

    @staticmethod
    def _safe_decimal(value):
        try:
            return round(float(value), 2) if value not in (None, "") else 0
        except (TypeError, ValueError):
            return 0