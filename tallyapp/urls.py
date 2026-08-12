from django.urls import path
from .views import (
    PendingBillsView, LedgerListView, VoucherListView,
    BreakdownView, AllLedgersWithVouchersView,
    EMDListView, EMDBreakdownView, DBEmdListView,
    DBLedgerListView, DBVoucherListView, DBVoucherBreakdownView,
)

urlpatterns = [
    path("pending-bills/", PendingBillsView.as_view()),
    path("ledgers/", LedgerListView.as_view()),
    path("vouchers/<str:ledger_name>/", VoucherListView.as_view()),
    path("breakdown/<str:master_id>/", BreakdownView.as_view()),
    path("all-ledgers-vouchers/", AllLedgersWithVouchersView.as_view()),
    path("emd/", EMDListView.as_view()),
    path("emd/<str:ledger_name>/breakdown/", EMDBreakdownView.as_view()),
    path("db/emd/", DBEmdListView.as_view()),
    path("db/ledgers/", DBLedgerListView.as_view()),
    path("db/ledgers/<int:ledger_id>/vouchers/", DBVoucherListView.as_view()),
    path("db/vouchers/<int:voucher_id>/breakdown/", DBVoucherBreakdownView.as_view()),
]