import logging

from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from firebase_admin import messaging
from firebase_admin.exceptions import FirebaseError

from .fcm_service import get_firebase_app
from .broadcast import (
    broadcast_to_tag,
    broadcast_update_to_tag,
    broadcast_delete_to_tag,
)
from .models import Notification, NotificationDeliveryLog
from .serializers import (
    NotificationCreateSerializer,
    NotificationSendSerializer,
    NotificationUpdateSerializer,
    ValidateTokensSerializer,
    SendWiraSerializer,
)
from .throttles import (
    CreateNotificationThrottle,
    SendNotificationThrottle,
    ValidateTokensThrottle,
    SendWiraThrottle,
)

logger = logging.getLogger('notifications')

VALID_DOMAINS = ('jobs', 'attendance')


class CreateNotificationView(APIView):
    """
    POST /api/<domain>/notifications/create/

    Body (multipart/form-data or JSON):
        title        (required)
        body         (required)
        regarding    (optional)
        image        (optional file upload)
        image_url    (optional external URL)

    Protected by:  X-API-KEY header
    Rate limited:  30/minute per IP  (create_notification scope)
    """
    throttle_classes = [CreateNotificationThrottle]

    def post(self, request, domain):
        if domain not in VALID_DOMAINS:
            logger.warning(
                "CreateNotification | invalid domain=%s | ip=%s",
                domain, _get_client_ip(request),
            )
            return Response(
                {"error": f"Invalid domain '{domain}'. Must be one of {VALID_DOMAINS}."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        serializer = NotificationCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        notification = serializer.save(domain=domain)

        notification.html_content = notification.build_html_content(request=request)
        notification.save(update_fields=['html_content'])

        logger.info(
            "Notification created | id=%s | domain=%s | title=%r | ip=%s",
            notification.id, domain, notification.title, _get_client_ip(request),
        )

        return Response(
            {
                "message": "Notification content created successfully.",
                "notification_id": notification.id,
                "domain": notification.domain,
                "title": notification.title,
                "body": notification.body,
                "regarding": notification.regarding,
                "image_url": notification.get_image_url(request),
                "html_content": notification.html_content,
                "created_at": notification.created_at,
            },
            status=status.HTTP_201_CREATED,
        )


class SendNotificationView(APIView):
    """
    POST /api/<domain>/notifications/send/

    Body (JSON):
        { "notification_id": 12, "fcm_tokens": ["token1", "token2", ...] }

    Protected by:  X-API-KEY header
    Rate limited:  20/minute per IP  (send_notification scope)
    """
    throttle_classes = [SendNotificationThrottle]

    def post(self, request, domain):
        if domain not in VALID_DOMAINS:
            logger.warning(
                "SendNotification | invalid domain=%s | ip=%s",
                domain, _get_client_ip(request),
            )
            return Response(
                {"error": f"Invalid domain '{domain}'. Must be one of {VALID_DOMAINS}."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        serializer = NotificationSendSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        notification_id = serializer.validated_data['notification_id']
        fcm_tokens = serializer.validated_data['fcm_tokens']

        notification = get_object_or_404(Notification, id=notification_id, domain=domain, is_deleted=False)

        logger.info(
            "SendNotification started | id=%s | domain=%s | token_count=%d | ip=%s",
            notification_id, domain, len(fcm_tokens), _get_client_ip(request),
        )

        try:
            app = get_firebase_app(domain)
        except (ValueError, FileNotFoundError) as exc:
            logger.error(
                "FCM app init failed | domain=%s | error=%s", domain, exc
            )
            return Response({"error": str(exc)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

        image_url = notification.get_image_url(request)
        notif_kwargs = {"title": notification.title, "body": notification.body}
        if image_url:
            notif_kwargs["image"] = image_url

        multicast_message = messaging.MulticastMessage(
            tokens=fcm_tokens,
            notification=messaging.Notification(**notif_kwargs),
            data={
                "notification_id": str(notification.id),
                "regarding": notification.regarding or "",
                "html_content": notification.html_content or "",
                "image_url": image_url or "",
            },
        )

        try:
            batch_response = messaging.send_each_for_multicast(multicast_message, app=app)
        except FirebaseError as exc:
            logger.error(
                "FCM dispatch error | id=%s | domain=%s | error=%s",
                notification_id, domain, exc,
            )
            return Response(
                {"error": f"FCM dispatch failed: {exc}"},
                status=status.HTTP_502_BAD_GATEWAY,
            )

        logs = []
        for token, result in zip(fcm_tokens, batch_response.responses):
            if result.success:
                logs.append(NotificationDeliveryLog(
                    notification=notification, fcm_token=token, status='success',
                ))
            else:
                logs.append(NotificationDeliveryLog(
                    notification=notification, fcm_token=token, status='failed',
                    error_message=str(result.exception) if result.exception else 'Unknown error',
                ))
        NotificationDeliveryLog.objects.bulk_create(logs)

        notification.is_sent = True
        notification.sent_at = timezone.now()
        notification.success_count += batch_response.success_count
        notification.failure_count += batch_response.failure_count
        notification.save(update_fields=['is_sent', 'sent_at', 'success_count', 'failure_count'])

        logger.info(
            "SendNotification done | id=%s | domain=%s | success=%d | failed=%d",
            notification_id, domain,
            batch_response.success_count, batch_response.failure_count,
        )
        if batch_response.failure_count:
            logger.warning(
                "FCM failures | id=%s | domain=%s | failed_tokens=%s",
                notification_id, domain,
                [
                    {"token": t[:16] + "…", "error": lg.error_message}
                    for t, lg in zip(fcm_tokens, logs) if lg.status == 'failed'
                ],
            )

        failed_tokens = [
            {"token": token, "error": log.error_message}
            for token, log in zip(fcm_tokens, logs) if log.status == 'failed'
        ]

        return Response(
            {
                "message": "Notification dispatch completed.",
                "notification_id": notification.id,
                "domain": domain,
                "total_tokens": len(fcm_tokens),
                "success_count": batch_response.success_count,
                "failure_count": batch_response.failure_count,
                "failed_tokens": failed_tokens,
            },
            status=status.HTTP_200_OK,
        )


class ValidateTokensView(APIView):
    """
    POST /api/<domain>/notifications/validate-tokens/

    Body (JSON):
        { "fcm_tokens": ["token1", "token2", ...] }   -- max 500 per call

    What it does:
        Runs a dry-run FCM multicast send (dry_run=True) against every
        token. This never delivers a real notification to any device --
        FCM validates the token and message shape and reports success/
        failure without pushing anything to the phone.

    Response:
        {
            "domain": "jobs",
            "total": 3,
            "results": {
                "token1": true,
                "token2": false,
                "token3": true
            }
        }

    Protected by:  X-API-KEY header
    Rate limited:  15/minute per IP  (validate_tokens scope)
    """
    throttle_classes = [ValidateTokensThrottle]

    def post(self, request, domain):
        if domain not in VALID_DOMAINS:
            return Response(
                {"error": f"Invalid domain '{domain}'. Must be one of {VALID_DOMAINS}."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        serializer = ValidateTokensSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        fcm_tokens = serializer.validated_data['fcm_tokens']

        try:
            app = get_firebase_app(domain)
        except (ValueError, FileNotFoundError) as exc:
            logger.error("Token validation | FCM app init failed | domain=%s | error=%s", domain, exc)
            return Response({"error": str(exc)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

        # A minimal, harmless message body -- required by the API shape,
        # but dry_run=True means FCM never actually delivers it anywhere.
        multicast_message = messaging.MulticastMessage(
            tokens=fcm_tokens,
            notification=messaging.Notification(
                title="token-validation-check",
                body="This is a dry-run check and is never delivered.",
            ),
        )

        try:
            batch_response = messaging.send_each_for_multicast(
                multicast_message, dry_run=True, app=app,
            )
        except FirebaseError as exc:
            logger.error("Token validation | FCM dry-run error | domain=%s | error=%s", domain, exc)
            return Response(
                {"error": f"FCM validation request failed: {exc}"},
                status=status.HTTP_502_BAD_GATEWAY,
            )

        # token -> True/False only, no error messages
        results = {}
        for token, result in zip(fcm_tokens, batch_response.responses):
            results[token] = result.success

        logger.info(
            "Token validation | domain=%s | total=%d | valid=%d | invalid=%d | ip=%s",
            domain, len(fcm_tokens),
            sum(1 for v in results.values() if v),
            sum(1 for v in results.values() if not v),
            _get_client_ip(request),
        )

        return Response(
            {
                "domain": domain,
                "total": len(fcm_tokens),
                "results": results,
            },
            status=status.HTTP_200_OK,
        )


class SendWiraNotificationView(APIView):
    """
    POST /api/<domain>/notifications/send-wira/

    Body (JSON):
        {
            "fcm_token": "single_token_here",         -- OR --
            "fcm_token": ["token1", "token2", ...],
            "title": "New Application",
            "body": "You have a new candidate",
            "metadata": {"candidate_id": "123", "job_id": "45"},
            "regarding": "candidate_application",   -- optional
            "imageUrl": "https://...",               -- optional
            "tag": "candidate_123"                   -- optional
        }

    Accepts either a single fcm_token (string) or multiple (list).
    Internally always dispatches via FCM multicast.

    Protected by:  X-API-KEY header
    Rate limited:  per send_wira scope (SendWiraThrottle)
    """
    throttle_classes = [SendWiraThrottle]

    def post(self, request, domain, *args, **kwargs):
        if domain not in VALID_DOMAINS:
            logger.warning(
                "SendWira | invalid domain=%s | ip=%s",
                domain, _get_client_ip(request),
            )
            return Response(
                {
                    "success": False,
                    "message": "Notification sent unsuccessfully",
                },
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

        serializer = SendWiraSerializer(data=request.data)
        if not serializer.is_valid():
            logger.warning(
                "SendWira | validation failed | domain=%s | errors=%s | ip=%s",
                domain, serializer.errors, _get_client_ip(request),
            )
            return Response(
                {
                    "success": False,
                    "message": "Notification sent unsuccessfully",
                },
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

        data = serializer.validated_data
        fcm_tokens = data["fcm_token"]  # always a list now, thanks to validate_fcm_token
        title = data["title"]
        body = data["body"]
        metadata = data.get("metadata") or {}
        regarding = data.get("regarding") or ""
        image_url = data.get("imageUrl") or ""
        tag = data.get("tag") or ""

        try:
            app = get_firebase_app(domain)
        except (ValueError, FileNotFoundError) as exc:
            logger.error("SendWira | FCM app init failed | domain=%s | error=%s", domain, exc)
            return Response(
                {
                    "success": False,
                    "message": "Notification sent unsuccessfully",
                },
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

        try:
            fcm_data = {str(k): str(v) for k, v in metadata.items()}
            if regarding:
                fcm_data["regarding"] = regarding
            if tag:
                fcm_data["tag"] = tag

            notif_kwargs = {"title": title, "body": body}
            if image_url:
                notif_kwargs["image"] = image_url

            multicast_message = messaging.MulticastMessage(
                tokens=fcm_tokens,
                notification=messaging.Notification(**notif_kwargs),
                data=fcm_data,
                android=messaging.AndroidConfig(
                    notification=messaging.AndroidNotification(
                        tag=tag if tag else None,
                        image=image_url if image_url else None,
                    )
                ),
                apns=messaging.APNSConfig(
                    payload=messaging.APNSPayload(
                        aps=messaging.Aps(
                            thread_id=tag if tag else None,
                        )
                    ),
                    fcm_options=messaging.APNSFCMOptions(
                        image=image_url if image_url else None,
                    ),
                ),
            )

            batch_response = messaging.send_each_for_multicast(multicast_message, app=app)

            logger.info(
                "SendWira done | domain=%s | total=%d | success=%d | failed=%d | tag=%s | ip=%s",
                domain, len(fcm_tokens),
                batch_response.success_count, batch_response.failure_count,
                tag, _get_client_ip(request),
            )

            # Agar SAB tokens fail ho gaye, poore request ko unsuccessful treat karo
            if batch_response.success_count == 0:
                failed_tokens = [
                    {"token": t, "error": str(r.exception) if r.exception else "Unknown error"}
                    for t, r in zip(fcm_tokens, batch_response.responses) if not r.success
                ]
                logger.warning(
                    "SendWira | all tokens failed | domain=%s | tag=%s | failures=%s",
                    domain, tag, failed_tokens,
                )
                return Response(
                    {
                        "success": False,
                        "message": "Notification sent unsuccessfully",
                    },
                    status=status.HTTP_500_INTERNAL_SERVER_ERROR,
                )

            response_payload = {
                "success": True,
                "message": "Notification sent successfully",
                "title": title,
                "metadata": metadata,
                "body": body,
                "total_tokens": len(fcm_tokens),
                "success_count": batch_response.success_count,
                "failure_count": batch_response.failure_count,
            }
            if regarding:
                response_payload["regarding"] = regarding
            if image_url:
                response_payload["imageUrl"] = image_url
            if tag:
                response_payload["tag"] = tag

            return Response(response_payload, status=status.HTTP_200_OK)

        except FirebaseError as exc:
            logger.error(
                "SendWira | FCM send failed | domain=%s | tag=%s | error=%s",
                domain, tag, exc,
            )
            return Response(
                {
                    "success": False,
                    "message": "Notification sent unsuccessfully",
                },
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )
        except Exception as exc:
            logger.error(
                "SendWira | unexpected error | domain=%s | tag=%s | error=%s",
                domain, tag, exc, exc_info=True,
            )
            return Response(
                {
                    "success": False,
                    "message": "Notification sent unsuccessfully",
                },
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )
def _get_client_ip(request):
    x_forwarded = request.META.get('HTTP_X_FORWARDED_FOR')
    if x_forwarded:
        return x_forwarded.split(',')[0].strip()
    return request.META.get('REMOTE_ADDR', 'unknown')


class BroadcastNotificationView(APIView):
    """
    POST /api/<domain>/notifications/broadcast/

    Body (JSON):
        {
            "notification_id": 12,
            "tags": ["user_123", "branch_5"],   <- required, WS tag groups
            "fcm_tokens": ["token1", "token2"]  <- optional, FCM fallback
        }

    What it does:
        1. Pushes the notification INSTANTLY over WebSocket to every
           client currently connected and subscribed to any of `tags`.
        2. (Optional) Also dispatches via FCM to `fcm_tokens`, so users
           whose app is closed / socket disconnected still get a push.

    This is the recommended endpoint going forward for anything that
    needs to reach "all online users watching tag X" -- e.g. a new job
    posted (tag="job_alerts"), or a single user's attendance update
    (tag=f"user_{user_id}").

    Protected by:  X-API-KEY header
    Rate limited:  20/minute per IP  (reuses send_notification scope)
    """
    throttle_classes = [SendNotificationThrottle]

    def post(self, request, domain):
        if domain not in VALID_DOMAINS:
            return Response(
                {"error": f"Invalid domain '{domain}'. Must be one of {VALID_DOMAINS}."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        notification_id = request.data.get('notification_id')
        tags = request.data.get('tags') or []
        fcm_tokens = request.data.get('fcm_tokens') or []

        if not notification_id:
            return Response({"error": "notification_id is required."}, status=status.HTTP_400_BAD_REQUEST)
        if not tags:
            return Response({"error": "tags (list) is required and cannot be empty."}, status=status.HTTP_400_BAD_REQUEST)

        notification = get_object_or_404(Notification, id=notification_id, domain=domain, is_deleted=False)

        image_url = notification.get_image_url(request)
        payload = {
            "notification_id": notification.id,
            "domain": notification.domain,
            "title": notification.title,
            "body": notification.body,
            "regarding": notification.regarding,
            "html_content": notification.html_content,
            "image_url": image_url,
            "created_at": notification.created_at.isoformat(),
        }

        # ── 1) WebSocket broadcast (instant, only reaches online sockets) ──
        ws_results = {}
        for tag in tags:
            sent = broadcast_to_tag(tag, payload)
            ws_results[tag] = "dispatched" if sent else "channel_layer_unavailable"

        logger.info(
            "Broadcast (WS) | id=%s | domain=%s | tags=%s | ip=%s",
            notification_id, domain, tags, _get_client_ip(request),
        )

        # ── 2) Optional FCM fallback for offline devices ───────────────────
        fcm_result = None
        if fcm_tokens:
            try:
                app = get_firebase_app(domain)
                notif_kwargs = {"title": notification.title, "body": notification.body}
                if image_url:
                    notif_kwargs["image"] = image_url

                multicast_message = messaging.MulticastMessage(
                    tokens=fcm_tokens,
                    notification=messaging.Notification(**notif_kwargs),
                    data={
                        "notification_id": str(notification.id),
                        "regarding": notification.regarding or "",
                        "html_content": notification.html_content or "",
                        "image_url": image_url or "",
                    },
                )
                batch_response = messaging.send_each_for_multicast(multicast_message, app=app)

                logs = []
                for token, result in zip(fcm_tokens, batch_response.responses):
                    logs.append(NotificationDeliveryLog(
                        notification=notification, fcm_token=token,
                        status='success' if result.success else 'failed',
                        error_message=None if result.success else (
                            str(result.exception) if result.exception else 'Unknown error'
                        ),
                    ))
                NotificationDeliveryLog.objects.bulk_create(logs)

                fcm_result = {
                    "success_count": batch_response.success_count,
                    "failure_count": batch_response.failure_count,
                }
            except (ValueError, FileNotFoundError, FirebaseError) as exc:
                logger.error("Broadcast FCM fallback failed | id=%s | error=%s", notification_id, exc)
                fcm_result = {"error": str(exc)}

        notification.is_sent = True
        notification.sent_at = timezone.now()
        if fcm_result and "success_count" in fcm_result:
            notification.success_count += fcm_result["success_count"]
            notification.failure_count += fcm_result["failure_count"]

        # Remember every tag this notification has ever gone out to (merge,
        # don't overwrite) so a later update()/delete() can reach all of
        # them without the caller having to re-supply the tag list.
        existing_tags = set(notification.last_broadcast_tags or [])
        notification.last_broadcast_tags = sorted(existing_tags | set(tags))

        notification.save(update_fields=[
            'is_sent', 'sent_at', 'success_count', 'failure_count', 'last_broadcast_tags',
        ])

        return Response(
            {
                "message": "Broadcast completed.",
                "notification_id": notification.id,
                "domain": domain,
                "websocket": ws_results,
                "fcm": fcm_result,
            },
            status=status.HTTP_200_OK,
        )


def _build_ws_payload(notification, request=None):
    """Shared payload shape for 'notification' / 'notification_updated' events."""
    return {
        "notification_id": notification.id,
        "domain": notification.domain,
        "title": notification.title,
        "body": notification.body,
        "regarding": notification.regarding,
        "html_content": notification.html_content,
        "image_url": notification.get_image_url(request),
        "created_at": notification.created_at.isoformat(),
        "updated_at": notification.updated_at.isoformat(),
    }


class UpdateNotificationView(APIView):
    """
    PATCH /api/<domain>/notifications/<id>/update/

    Body (JSON or multipart):
        title / body / regarding / image / image_url   (any subset)
        tags   (optional list) -- extra tags to also push the update to,
               beyond the ones this notification was already broadcast to.

    What it does:
        1. Updates the Notification row (content + regenerated html_content).
        2. Re-broadcasts a "notification_updated" WS event to every tag this
           notification was ever sent to (`last_broadcast_tags`), plus any
           extra `tags` passed in this call. Clients should find the
           existing card by notification_id and replace its content --
           NOT append a new one.

    Use this when you sent something and then noticed a mistake in the
    title/body/image and want everyone who already has it on screen to
    see the corrected version immediately.

    Protected by:  X-API-KEY header
    Rate limited:  20/minute per IP  (reuses send_notification scope)
    """
    throttle_classes = [SendNotificationThrottle]

    def patch(self, request, domain, pk):
        if domain not in VALID_DOMAINS:
            return Response(
                {"error": f"Invalid domain '{domain}'. Must be one of {VALID_DOMAINS}."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        notification = get_object_or_404(Notification, id=pk, domain=domain, is_deleted=False)

        serializer = NotificationUpdateSerializer(notification, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        notification = serializer.save()

        notification.html_content = notification.build_html_content(request=request)
        notification.save(update_fields=['html_content', 'updated_at'])

        extra_tags = request.data.get('tags') or []
        all_tags = sorted(set(notification.last_broadcast_tags or []) | set(extra_tags))

        payload = _build_ws_payload(notification, request=request)
        ws_results = {}
        for tag in all_tags:
            sent = broadcast_update_to_tag(tag, payload)
            ws_results[tag] = "dispatched" if sent else "channel_layer_unavailable"

        if extra_tags:
            notification.last_broadcast_tags = all_tags
            notification.save(update_fields=['last_broadcast_tags'])

        logger.info(
            "Notification updated | id=%s | domain=%s | tags=%s | ip=%s",
            notification.id, domain, all_tags, _get_client_ip(request),
        )

        return Response(
            {
                "message": "Notification updated and re-broadcast.",
                "notification_id": notification.id,
                "domain": domain,
                "title": notification.title,
                "body": notification.body,
                "regarding": notification.regarding,
                "image_url": notification.get_image_url(request),
                "html_content": notification.html_content,
                "websocket": ws_results,
            },
            status=status.HTTP_200_OK,
        )


class DeleteNotificationView(APIView):
    """
    DELETE /api/<domain>/notifications/<id>/delete/

    Body (JSON, optional):
        { "tags": ["job_alerts"], "reason": "sent to wrong tag" }

    What it does:
        1. Soft-deletes the Notification row (is_deleted=True). The row and
           its delivery logs are kept for history -- nothing is actually
           removed from the DB.
        2. Pushes a "notification_deleted" WS event so every client
           currently showing this notification removes it from their live
           feed right now.

    Which tags get the delete event:
        - Every tag in `last_broadcast_tags` (everywhere this notification
          was ever sent), PLUS
        - Any extra tags passed in the request body's `tags` field.

    Typical use case: you broadcast to "job_alerts" by mistake when you
    meant "new_job". Call this to pull it out of "job_alerts" immediately,
    then POST /broadcast/ again with the correct tag.

    Protected by:  X-API-KEY header
    Rate limited:  20/minute per IP  (reuses send_notification scope)
    """
    throttle_classes = [SendNotificationThrottle]

    def delete(self, request, domain, pk):
        if domain not in VALID_DOMAINS:
            return Response(
                {"error": f"Invalid domain '{domain}'. Must be one of {VALID_DOMAINS}."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        notification = get_object_or_404(Notification, id=pk, domain=domain, is_deleted=False)

        extra_tags = request.data.get('tags') or []
        reason = request.data.get('reason', '')
        all_tags = sorted(set(notification.last_broadcast_tags or []) | set(extra_tags))

        ws_results = {}
        for tag in all_tags:
            sent = broadcast_delete_to_tag(tag, notification.id, reason=reason)
            ws_results[tag] = "dispatched" if sent else "channel_layer_unavailable"

        notification.is_deleted = True
        notification.deleted_at = timezone.now()
        notification.save(update_fields=['is_deleted', 'deleted_at'])

        logger.info(
            "Notification deleted (soft) | id=%s | domain=%s | tags=%s | reason=%r | ip=%s",
            notification.id, domain, all_tags, reason, _get_client_ip(request),
        )

        return Response(
            {
                "message": "Notification soft-deleted and removal broadcast.",
                "notification_id": notification.id,
                "domain": domain,
                "websocket": ws_results,
            },
            status=status.HTTP_200_OK,
        )