from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated

from notifications.authentication import ApiKeyAuthentication
from .models import TallyInstance, Ledger, Voucher, VoucherBreakdown, EMDLedger
from  tally_config import get_instance, get_all_instances
from .services.tally_client import TallyClient
from .services.ledger_service import LedgerService
from .services.voucher_service import VoucherService
from .services.breakdown_service import BreakdownService
from .services.pending_bills_service import PendingBillsService
from .services.emd_service import EMDService
from .services.persistence_service import PersistenceService

VALID_FYS = ["2023-24", "2024-25", "2025-26", "2026-27"]

FINANCIAL_YEARS = {
    "2023-24": ("20230401", "20240331"),
    "2024-25": ("20240401", "20250331"),
    "2025-26": ("20250401", "20260331"),
    "2026-27": ("20260401", "20270331"),
}

DEFAULT_TALLY_TAG = "office"  # fallback tag when none is provided in URL


class SecureAPIView(APIView):
    """
    Base class for every view in this app. Reuses the project-wide
    X-API-KEY authentication (notifications/authentication.py) so the
    same key already used by other apps works here too.
    """
    authentication_classes = [ApiKeyAuthentication]
    permission_classes = [IsAuthenticated]


def make_client(tally_tag=None) -> TallyClient:
    instance = get_instance(tally_tag) if tally_tag is not None else get_instance(DEFAULT_TALLY_TAG)
    return TallyClient(instance["url"])


class TallyInstanceListView(SecureAPIView):
    """
    GET  /python/api/tally-instances/           -> list all active instances
    POST /python/api/tally-instances/           -> register/update an instance by tag
    """

    def get(self, request):
        return Response({
            "success":   True,
            "instances": get_all_instances(),
            "note":      "Use tag in URL: /python/{tag}/api/...",
        })

    def post(self, request):
        data = request.data
        tag  = data.get("tag")
        url  = data.get("url")

        if not tag or not url:
            return Response(
                {"success": False, "error": "'tag' and 'url' are required fields"},
                status=400,
            )

        tag = tag.strip().lower().replace(" ", "-")

        instance, created = TallyInstance.objects.update_or_create(
            tag=tag,
            defaults={
                "name":      data.get("name", tag.title()),
                "url":       url.strip(),
                "company":   data.get("company", ""),
                "location":  data.get("location", ""),
                "is_active": True,
            },
        )

        return Response({
            "success":  True,
            "created":  created,
            "instance": instance.to_dict(),
        }, status=201 if created else 200)


class TallyInstanceDetailView(SecureAPIView):
    """
    DELETE /python/api/tally-instances/<tag>/   -> deactivate an instance
    """

    def delete(self, request, tag):
        try:
            instance = TallyInstance.objects.get(tag=tag)
        except TallyInstance.DoesNotExist:
            return Response({"success": False, "error": f"Tag '{tag}' not found"}, status=404)

        instance.is_active = False
        instance.save(update_fields=["is_active"])
        return Response({"success": True, "message": f"Tag '{tag}' deactivated"})


class PendingBillsView(SecureAPIView):
    def get(self, request, tally_tag=None):
        try:
            client = make_client(tally_tag)
            result = PendingBillsService(client).get_all()
            return Response({
                "success":      True,
                "tally_tag":    tally_tag or DEFAULT_TALLY_TAG,
                "company_name": result["company_name"],
                "count":        result["count"],
                "data":         result["data"],
            })
        except ValueError as e:
            return Response({"success": False, "error": str(e)}, status=404)
        except Exception as e:
            return Response({"success": False, "error": str(e)}, status=500)


class LedgerListView(SecureAPIView):
    def get(self, request, tally_tag=None):
        try:
            client = make_client(tally_tag)
            data   = LedgerService(client).get_all()
            PersistenceService(tally_tag or DEFAULT_TALLY_TAG).save_ledgers(data)
            return Response({
                "success":   True,
                "tally_tag": tally_tag or DEFAULT_TALLY_TAG,
                "count":     len(data),
                "data":      data
            })
        except ValueError as e:
            return Response({"success": False, "error": str(e)}, status=404)
        except Exception as e:
            return Response({"success": False, "error": str(e)}, status=500)


class VoucherListView(SecureAPIView):
    def get(self, request, ledger_name, tally_tag=None):
        try:
            fy        = request.query_params.get("fy", "2026-27")
            from_date = request.query_params.get("from_date", None)
            to_date   = request.query_params.get("to_date", None)

            if fy not in VALID_FYS:
                return Response({
                    "success": False,
                    "error": f"Invalid FY. Choose from: {VALID_FYS}"
                }, status=400)

            if (from_date and not to_date) or (to_date and not from_date):
                return Response({
                    "success": False,
                    "error": "Both from_date and to_date required. Format: YYYYMMDD"
                }, status=400)

            client = make_client(tally_tag)
            data   = VoucherService(client).get_vouchers(ledger_name, fy, from_date, to_date)
            PersistenceService(tally_tag or DEFAULT_TALLY_TAG).save_vouchers(ledger_name, data, fy)
            return Response({
                "success":   True,
                "tally_tag": tally_tag or DEFAULT_TALLY_TAG,
                "fy":        fy,
                "from_date": from_date or FINANCIAL_YEARS.get(fy, ("", ""))[0],
                "to_date":   to_date   or FINANCIAL_YEARS.get(fy, ("", ""))[1],
                "count":     len(data),
                "data":      data
            })
        except ValueError as e:
            return Response({"success": False, "error": str(e)}, status=404)
        except Exception as e:
            return Response({"success": False, "error": str(e)}, status=500)


class BreakdownView(SecureAPIView):
    def get(self, request, master_id, tally_tag=None):
        try:
            client = make_client(tally_tag)
            data   = BreakdownService(client).get_breakdown(master_id)
            return Response({"success": True, "tally_tag": tally_tag or DEFAULT_TALLY_TAG, "data": data})
        except ValueError as e:
            return Response({"success": False, "error": str(e)}, status=404)
        except Exception as e:
            return Response({"success": False, "error": str(e)}, status=500)


class EMDListView(SecureAPIView):
    """
    GET /python/api/emd/                          -> default group "Deposits - EMD"
    GET /python/api/emd/?group=<custom group name> -> any other group's ledgers
    GET /python/<tally_tag>/api/emd/               -> tag-specific Tally instance
    """
    def get(self, request, tally_tag=None):
        try:
            group_name = request.query_params.get("group", "Deposits - EMD")

            client = make_client(tally_tag)
            data   = EMDService(client).get_group_ledgers_with_status(group_name)
            PersistenceService(tally_tag or DEFAULT_TALLY_TAG).save_emd_ledgers(data)
            return Response({
                "success":   True,
                "tally_tag": tally_tag or DEFAULT_TALLY_TAG,
                "group":     group_name,
                "count":     len(data),
                "data":      data,
            })
        except ValueError as e:
            return Response({"success": False, "error": str(e)}, status=404)
        except Exception as e:
            return Response({"success": False, "error": str(e)}, status=500)


class DBEmdListView(SecureAPIView):
    """
    GET /python/<tally_tag>/api/db/emd/               -> EMD ledgers already saved in Postgres
    GET /python/<tally_tag>/api/db/emd/?status=Closed  -> filter by status
    """

    def get(self, request, tally_tag=None):
        tag = tally_tag or DEFAULT_TALLY_TAG
        qs = EMDLedger.objects.filter(ledger__tally_instance_id=tag).select_related("ledger")

        status_filter = request.query_params.get("status")
        if status_filter:
            qs = qs.filter(emd_status=status_filter)

        qs = qs.order_by("ledger__ledger_name")

        data = [
            {
                "ledger_name":       emd.ledger.ledger_name,
                "parent":            emd.ledger.parent,
                "opening_balance":   str(emd.opening_balance),
                "nett_transactions": str(emd.nett_transactions),
                "closing_balance":   str(emd.closing_balance),
                "emd_status":        emd.emd_status,
                "last_synced":       emd.last_synced.isoformat(),
            }
            for emd in qs
        ]

        return Response({
            "success":   True,
            "tally_tag": tag,
            "count":     len(data),
            "data":      data,
        })


class EMDBreakdownView(SecureAPIView):
    def get(self, request, ledger_name, tally_tag=None):
        try:
            fy        = request.query_params.get("fy", "2026-27")
            from_date = request.query_params.get("from_date", None)
            to_date   = request.query_params.get("to_date", None)

            if fy not in VALID_FYS:
                return Response({"success": False, "error": f"Invalid FY. Choose from: {VALID_FYS}"}, status=400)

            if (from_date and not to_date) or (to_date and not from_date):
                return Response({"success": False, "error": "Both from_date and to_date required. Format: YYYYMMDD"}, status=400)

            client = make_client(tally_tag)
            data   = EMDService(client).get_ledger_monthly_breakdown(ledger_name, fy, from_date, to_date)

            return Response({
                "success":   True,
                "tally_tag": tally_tag or DEFAULT_TALLY_TAG,
                "fy":        fy,
                "from_date": from_date or FINANCIAL_YEARS.get(fy, ("", ""))[0],
                "to_date":   to_date   or FINANCIAL_YEARS.get(fy, ("", ""))[1],
                "data":      data,
            })
        except ValueError as e:
            return Response({"success": False, "error": str(e)}, status=404)
        except Exception as e:
            return Response({"success": False, "error": str(e)}, status=500)


# ---------------------------------------------------------------------------
# DB-only (offline) views — read straight from Postgres, never touch Tally.
# Used by the local test frontend so it works even when the Tally server
# is unreachable / you just want to browse whatever's already synced.
# ---------------------------------------------------------------------------

class DBLedgerListView(SecureAPIView):
    """
    GET /python/<tally_tag>/api/db/ledgers/   -> ledgers already saved in Postgres
    Optional query params: ?search=abc  (filter by name, case-insensitive contains)
    """

    def get(self, request, tally_tag=None):
        tag = tally_tag or DEFAULT_TALLY_TAG
        qs = Ledger.objects.filter(tally_instance_id=tag)

        search = request.query_params.get("search")
        if search:
            qs = qs.filter(ledger_name__icontains=search)

        qs = qs.order_by("ledger_name")

        data = [
            {
                "id": ledger.id,
                "ledger_name": ledger.ledger_name,
                "parent": ledger.parent,
                "closing_balance": str(ledger.closing_balance),
                "voucher_count": ledger.voucher_count,
                "last_synced": ledger.last_synced.isoformat(),
            }
            for ledger in qs
        ]

        return Response({
            "success":   True,
            "tally_tag": tag,
            "count":     len(data),
            "data":      data,
        })


class DBVoucherListView(SecureAPIView):
    """
    GET /python/<tally_tag>/api/db/ledgers/<ledger_id>/vouchers/
    -> vouchers already saved in Postgres for that ledger.
    Optional query params: ?fy=2025-26
    """

    def get(self, request, ledger_id, tally_tag=None):
        try:
            ledger = Ledger.objects.get(pk=ledger_id)
        except Ledger.DoesNotExist:
            return Response({"success": False, "error": f"Ledger id {ledger_id} not found"}, status=404)

        qs = Voucher.objects.filter(ledger=ledger)

        fy = request.query_params.get("fy")
        if fy:
            qs = qs.filter(financial_year=fy)

        qs = qs.order_by("-date")

        data = [
            {
                "id": v.id,
                "guid": v.guid,
                "master_id": v.master_id,
                "voucher_no": v.voucher_no,
                "voucher_type": v.voucher_type,
                "party_ledger": v.party_ledger,
                "date": v.date,
                "amount": str(v.amount),
                "narration": v.narration,
                "financial_year": v.financial_year,
            }
            for v in qs
        ]

        return Response({
            "success":     True,
            "ledger_id":   ledger.id,
            "ledger_name": ledger.ledger_name,
            "count":       len(data),
            "data":        data,
        })


class DBVoucherBreakdownView(SecureAPIView):
    """
    GET /python/<tally_tag>/api/db/vouchers/<voucher_id>/breakdown/
    -> breakdown lines already saved in Postgres for that voucher.
    """

    def get(self, request, voucher_id, tally_tag=None):
        try:
            voucher = Voucher.objects.select_related("ledger").get(pk=voucher_id)
        except Voucher.DoesNotExist:
            return Response({"success": False, "error": f"Voucher id {voucher_id} not found"}, status=404)

        lines = VoucherBreakdown.objects.filter(voucher=voucher)

        data = [
            {"ledger_name": line.ledger_name, "amount": str(line.amount)}
            for line in lines
        ]

        return Response({
            "success": True,
            "voucher": {
                "id": voucher.id,
                "voucher_no": voucher.voucher_no,
                "voucher_type": voucher.voucher_type,
                "date": voucher.date,
                "amount": str(voucher.amount),
                "party_ledger": voucher.party_ledger,
                "narration": voucher.narration,
                "ledger_name": voucher.ledger.ledger_name,
            },
            "count": len(data),
            "data": data,
        })


class AllLedgersWithVouchersView(SecureAPIView):
    def get(self, request, tally_tag=None):
        try:
            fy        = request.query_params.get("fy", "2026-27")
            from_date = request.query_params.get("from_date", None)
            to_date   = request.query_params.get("to_date", None)
            page      = int(request.query_params.get("page", 1))
            per_page  = int(request.query_params.get("per_page", 20))

            if fy not in VALID_FYS:
                return Response({
                    "success": False,
                    "error": f"Invalid FY. Choose from: {VALID_FYS}"
                }, status=400)

            if (from_date and not to_date) or (to_date and not from_date):
                return Response({
                    "success": False,
                    "error": "Both from_date and to_date required. Format: YYYYMMDD"
                }, status=400)

            client = make_client(tally_tag)
            data   = VoucherService(client).get_all_ledgers_with_vouchers(fy, from_date, to_date)
            PersistenceService(tally_tag or DEFAULT_TALLY_TAG).save_all_ledgers_with_vouchers(data, fy)
            total_ledgers  = len(data)
            start          = (page - 1) * per_page
            end            = start + per_page
            paginated_data = data[start:end]

            return Response({
                "success":       True,
                "tally_tag":     tally_tag or DEFAULT_TALLY_TAG,
                "fy":            fy,
                "from_date":     from_date or FINANCIAL_YEARS.get(fy, ("", ""))[0],
                "to_date":       to_date   or FINANCIAL_YEARS.get(fy, ("", ""))[1],
                "total_ledgers": total_ledgers,
                "page":          page,
                "per_page":      per_page,
                "total_pages":   (total_ledgers + per_page - 1) // per_page,
                "data":          paginated_data
            })

        except ValueError as e:
            return Response({"success": False, "error": str(e)}, status=404)
        except Exception as e:
            return Response({"success": False, "error": str(e)}, status=500)