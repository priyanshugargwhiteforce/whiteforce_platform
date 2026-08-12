from django.db import models
from django.utils import timezone


class TallyInstance(models.Model):
    tag = models.SlugField(max_length=50, primary_key=True)  # e.g. "payroll", "offroll"
    name = models.CharField(max_length=100)
    url = models.URLField(max_length=500)
    company = models.CharField(max_length=150, blank=True, default="")
    location = models.CharField(max_length=150, blank=True, default="")
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["tag"]

    def __str__(self):
        return f"{self.tag} -> {self.url}"

    def to_dict(self):
        return {
            "id": self.tag,
            "tag": self.tag,
            "name": self.name,
            "company": self.company,
            "location": self.location,
            "url": self.url,
            "is_active": self.is_active,
            "updated_at": self.updated_at.isoformat(),
        }


class Ledger(models.Model):
    """
    One row per unique ledger per tally instance.
    Dedup key: (tally_instance, ledger_name)
    """
    id = models.BigAutoField(primary_key=True)
    tally_instance = models.ForeignKey(
        TallyInstance, on_delete=models.PROTECT, related_name="ledgers"
    )
    ledger_name = models.CharField(max_length=255)
    parent = models.CharField(max_length=255, blank=True, default="")
    closing_balance = models.DecimalField(max_digits=15, decimal_places=2, default=0)
    voucher_count = models.IntegerField(default=0)
    last_synced = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["tally_instance", "ledger_name"], name="uniq_ledger_per_instance"
            )
        ]
        indexes = [
            models.Index(fields=["tally_instance", "ledger_name"]),
        ]

    def __str__(self):
        return f"{self.ledger_name} ({self.tally_instance_id})"


class Voucher(models.Model):
    """
    One row per unique voucher. Dedup key: (ledger, guid)
    guid = Tally's REMOTEID/GUID — stable even if voucher_no changes.
    """
    id = models.BigAutoField(primary_key=True)
    ledger = models.ForeignKey(Ledger, on_delete=models.PROTECT, related_name="vouchers")
    guid = models.CharField(max_length=150)
    master_id = models.CharField(max_length=50, blank=True, default="")
    voucher_no = models.CharField(max_length=100, blank=True, default="")
    voucher_type = models.CharField(max_length=100, blank=True, default="")
    party_ledger = models.CharField(max_length=255, blank=True, default="")
    date = models.CharField(max_length=8, blank=True, default="")  # Tally format YYYYMMDD
    amount = models.DecimalField(max_digits=15, decimal_places=2, default=0)
    narration = models.TextField(blank=True, default="")
    financial_year = models.CharField(max_length=9, blank=True, default="")
    last_synced = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["ledger", "guid"], name="uniq_voucher_per_ledger")
        ]
        indexes = [
            models.Index(fields=["ledger", "financial_year"]),
            models.Index(fields=["guid"]),
        ]

    def __str__(self):
        return f"{self.voucher_no} ({self.guid})"


class VoucherBreakdown(models.Model):
    """
    Ledger-entry lines within a voucher (debit/credit split).
    Replaced wholesale on every sync of the parent voucher (see services).
    """
    id = models.BigAutoField(primary_key=True)
    voucher = models.ForeignKey(Voucher, on_delete=models.CASCADE, related_name="breakdown_lines")
    ledger_name = models.CharField(max_length=255)
    amount = models.DecimalField(max_digits=15, decimal_places=2, default=0)

    class Meta:
        indexes = [
            models.Index(fields=["voucher"]),
        ]


class EMDLedger(models.Model):
    """
    Subset of ledgers that belong to the EMD group. Linked 1:1 to Ledger.
    """
    id = models.BigAutoField(primary_key=True)
    ledger = models.OneToOneField(Ledger, on_delete=models.CASCADE, related_name="emd_info")
    group_name = models.CharField(max_length=150, default="Deposits - EMD")
    opening_balance = models.DecimalField(max_digits=15, decimal_places=2, default=0)
    nett_transactions = models.DecimalField(max_digits=15, decimal_places=2, default=0)
    closing_balance = models.DecimalField(max_digits=15, decimal_places=2, default=0)
    emd_status = models.CharField(max_length=20, default="Not Closed")  # "Closed" / "Not Closed" / "No Activity"
    financial_year = models.CharField(max_length=9, blank=True, default="")
    last_synced = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(fields=["emd_status"]),
        ]

    def __str__(self):
        return f"EMD: {self.ledger.ledger_name} [{self.emd_status}]"