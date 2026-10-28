from django.db import transaction
from django.db.models import Count, OuterRef, Subquery
from django.db.models.functions import Coalesce
from ..models import TallyInstance, Ledger, Voucher, VoucherBreakdown, EMDLedger

# Keeps every INSERT / IN (...) well under Postgres' 65535 bind-parameter limit.
BATCH_SIZE = 1000

VOUCHER_UPDATE_FIELDS = [
    "master_id", "voucher_no", "voucher_type", "party_ledger",
    "date", "amount", "narration", "financial_year", "last_synced",
]


class PersistenceService:
    """
    Takes parsed Tally API responses (dicts from xml_parser.py) and
    upserts them into Postgres. No duplicate rows regardless of how
    many times the same API is called.

    Writes are batched (bulk upsert on the unique constraints) instead of
    one update_or_create per row, so large syncs don't run tens of
    thousands of queries.
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
        rows = {}
        for item in ledger_list:
            name = item.get("name") or item.get("ledger_name")
            if not name:
                continue
            rows[name] = Ledger(
                tally_instance=self.tally_instance,
                ledger_name=name,
                parent=item.get("parent", ""),
                closing_balance=item.get("closing_balance") or 0,
            )

        return Ledger.objects.bulk_create(
            list(rows.values()),
            batch_size=BATCH_SIZE,
            update_conflicts=True,
            unique_fields=["tally_instance", "ledger_name"],
            update_fields=["parent", "closing_balance", "last_synced"],
        )

    def save_vouchers(self, ledger_name, voucher_list, financial_year=""):
        """
        voucher_list: output of VoucherService.get_vouchers()
        Each item has 'guid', 'breakdown': {'entries': [...]}
        """
        ledger, _ = Ledger.objects.get_or_create(
            tally_instance=self.tally_instance,
            ledger_name=ledger_name,
        )

        with transaction.atomic():
            saved = self._upsert_vouchers([(ledger, voucher_list)], financial_year)
            self._sync_voucher_counts([ledger.pk])

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
        groups = {
            g.get("ledger_name"): g.get("vouchers", [])
            for g in grouped_data
            if g.get("ledger_name") and g.get("ledger_name") != "Unknown"
        }
        if not groups:
            return 0

        with transaction.atomic():
            # closing_balance not present in this response shape — only touch last_synced
            ledgers = Ledger.objects.bulk_create(
                [Ledger(tally_instance=self.tally_instance, ledger_name=name) for name in groups],
                batch_size=BATCH_SIZE,
                update_conflicts=True,
                unique_fields=["tally_instance", "ledger_name"],
                update_fields=["last_synced"],
            )

            saved = self._upsert_vouchers(
                [(ledger, groups[ledger.ledger_name]) for ledger in ledgers],
                financial_year,
            )
            self._sync_voucher_counts([ledger.pk for ledger in ledgers])

        return len(saved)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _upsert_vouchers(self, ledger_vouchers, financial_year):
        """
        ledger_vouchers: [(Ledger, [voucher dicts]), ...]
        Upserts vouchers on (ledger, guid) and replaces their breakdown
        lines wholesale. Returns the saved Voucher objects.
        """
        vouchers = {}   # (ledger_id, guid) -> Voucher; dedupes, last one wins
        entries  = {}   # (ledger_id, guid) -> breakdown entries

        for ledger, items in ledger_vouchers:
            for item in items:
                guid = item.get("guid")
                if not guid:
                    continue  # can't dedupe without a stable identity

                key = (ledger.pk, guid)
                vouchers[key] = Voucher(
                    ledger=ledger,
                    guid=guid,
                    master_id=item.get("master_id", ""),
                    voucher_no=item.get("voucher_no", ""),
                    voucher_type=item.get("voucher_type", ""),
                    party_ledger=item.get("party_ledger", ""),
                    date=item.get("date", ""),
                    amount=self._safe_decimal(item.get("amount")),
                    narration=item.get("narration", ""),
                    financial_year=financial_year,
                )
                entries[key] = item.get("breakdown", {}).get("entries", [])

        if not vouchers:
            return []

        saved = Voucher.objects.bulk_create(
            list(vouchers.values()),
            batch_size=BATCH_SIZE,
            update_conflicts=True,
            unique_fields=["ledger", "guid"],
            update_fields=VOUCHER_UPDATE_FIELDS,
        )

        voucher_ids = [v.pk for v in saved]
        for i in range(0, len(voucher_ids), BATCH_SIZE):
            VoucherBreakdown.objects.filter(voucher_id__in=voucher_ids[i:i + BATCH_SIZE]).delete()

        VoucherBreakdown.objects.bulk_create(
            [
                VoucherBreakdown(
                    voucher=voucher,
                    ledger_name=e.get("ledger", ""),
                    amount=self._safe_decimal(e.get("amount")),
                )
                for voucher in saved
                for e in entries[(voucher.ledger_id, voucher.guid)]
            ],
            batch_size=BATCH_SIZE,
        )

        return saved

    @staticmethod
    def _sync_voucher_counts(ledger_ids):
        counts = (
            Voucher.objects.filter(ledger=OuterRef("pk"))
            .values("ledger")
            .annotate(c=Count("id"))
            .values("c")
        )
        for i in range(0, len(ledger_ids), BATCH_SIZE):
            Ledger.objects.filter(pk__in=ledger_ids[i:i + BATCH_SIZE]).update(
                voucher_count=Coalesce(Subquery(counts), 0)
            )

    @staticmethod
    def _safe_decimal(value):
        try:
            return round(float(value), 2) if value not in (None, "") else 0
        except (TypeError, ValueError):
            return 0
