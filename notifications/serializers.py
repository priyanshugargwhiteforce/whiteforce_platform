from rest_framework import serializers
from .models import Notification


class NotificationCreateSerializer(serializers.ModelSerializer):
    """Used by the 'create notification content' API.
    domain is NOT accepted here -- it comes from the URL, so a colleague
    can never accidentally create jobs-content under the attendance
    domain (or vice-versa) by sending the wrong field."""

    class Meta:
        model = Notification
        fields = ['id', 'title', 'body', 'regarding', 'image', 'image_url', 'created_at']
        read_only_fields = ['id', 'created_at']

    def validate(self, attrs):
        if not attrs.get('image') and not attrs.get('image_url') and self.instance is None:
            # Image is optional overall, so we only check nothing
            # contradictory was sent. No error raised here by design --
            # image truly is optional, per the requirements.
            pass
        return attrs


class NotificationUpdateSerializer(serializers.ModelSerializer):
    """Used by the 'update notification content' API (PATCH). All fields
    optional (partial=True is passed by the view) -- send only what
    changed. domain is intentionally excluded: you can't move a
    notification between domains after the fact."""

    class Meta:
        model = Notification
        fields = ['title', 'body', 'regarding', 'image', 'image_url']


class NotificationSendSerializer(serializers.Serializer):
    """Used by the 'send notification' API. Takes the id of previously
    created content plus the array of destination FCM tokens."""

    notification_id = serializers.IntegerField()
    fcm_tokens = serializers.ListField(
        child=serializers.CharField(max_length=512, allow_blank=False),
        allow_empty=False,
        min_length=1,
    )
class ValidateTokensSerializer(serializers.Serializer):
    """Used by the 'validate FCM tokens' API. Just the tokens -- no
    notification_id, since this never sends real content, only checks
    whether FCM would accept each token."""

    fcm_tokens = serializers.ListField(
        child=serializers.CharField(max_length=512, allow_blank=False),
        allow_empty=False,
        min_length=1,
        max_length=500,  # FCM's own multicast hard limit per request
    )

class SendWiraSerializer(serializers.Serializer):
    fcm_token = serializers.JSONField(required=True)  # accepts string OR list
    title = serializers.CharField(required=True, allow_blank=False)
    body = serializers.CharField(required=True, allow_blank=False)
    metadata = serializers.DictField(required=False, default=dict)
    regarding = serializers.CharField(required=False, allow_blank=True, default="")
    imageUrl = serializers.URLField(required=False, allow_blank=True, default="")
    tag = serializers.CharField(required=False, allow_blank=True, default="")

    def validate_fcm_token(self, value):
        if isinstance(value, str):
            if not value.strip():
                raise serializers.ValidationError("fcm_token cannot be blank.")
            return [value]  # normalize to list internally
        if isinstance(value, list):
            if not value:
                raise serializers.ValidationError("fcm_token list cannot be empty.")
            if not all(isinstance(t, str) and t.strip() for t in value):
                raise serializers.ValidationError("All fcm_token entries must be non-empty strings.")
            return value
        raise serializers.ValidationError("fcm_token must be a string or a list of strings.")