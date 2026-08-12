from django.contrib import admin
from .models import TallyInstance, Ledger, Voucher, VoucherBreakdown, EMDLedger


@admin.register(TallyInstance)
class TallyInstanceAdmin(admin.ModelAdmin):
    list_display = ["tag", "name", "company", "is_active", "updated_at"]
    search_fields = ["tag", "name", "company"]


@admin.register(Ledger)
class LedgerAdmin(admin.ModelAdmin):
    list_display = ["ledger_name", "tally_instance", "closing_balance", "voucher_count", "last_synced"]
    search_fields = ["ledger_name"]
    list_filter = ["tally_instance"]


@admin.register(Voucher)
class VoucherAdmin(admin.ModelAdmin):
    list_display = ["voucher_no", "ledger", "voucher_type", "amount", "financial_year", "date"]
    search_fields = ["voucher_no", "guid", "party_ledger"]
    list_filter = ["financial_year", "voucher_type"]


@admin.register(VoucherBreakdown)
class VoucherBreakdownAdmin(admin.ModelAdmin):
    list_display = ["voucher", "ledger_name", "amount"]


@admin.register(EMDLedger)
class EMDLedgerAdmin(admin.ModelAdmin):
    list_display = [
        "ledger", "emd_status", "opening_balance",
        "nett_transactions", "closing_balance", "financial_year", "last_synced"
    ]
    list_filter = ["emd_status", "financial_year"]
    search_fields = ["ledger__ledger_name"]
    ordering = ["ledger__ledger_name"]