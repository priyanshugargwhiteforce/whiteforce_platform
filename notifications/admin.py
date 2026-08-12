from django.contrib import admin
from django.utils import timezone

from .broadcast import broadcast_delete_to_tag
from .models import Notification, NotificationDeliveryLog


class NotificationDeliveryLogInline(admin.TabularInline):
    model = NotificationDeliveryLog
    extra = 0
    readonly_fields = ('fcm_token', 'status', 'error_message', 'sent_at')
    can_delete = False


@admin.action(description="Soft-delete selected + notify connected clients in real time")
def soft_delete_and_broadcast(modeladmin, request, queryset):
    notified = 0
    for notification in queryset.filter(is_deleted=False):
        for tag in (notification.last_broadcast_tags or []):
            broadcast_delete_to_tag(tag, notification.id, reason='deleted via admin')
        notification.is_deleted = True
        notification.deleted_at = timezone.now()
        notification.save(update_fields=['is_deleted', 'deleted_at'])
        notified += 1
    modeladmin.message_user(request, f"Soft-deleted {notified} notification(s) and pushed removal to connected clients.")


@admin.register(Notification)
class NotificationAdmin(admin.ModelAdmin):
    list_display = (
        'id', 'domain', 'title', 'regarding', 'is_sent', 'is_deleted',
        'success_count', 'failure_count', 'created_at',
    )
    list_filter = ('domain', 'is_sent', 'is_deleted')
    search_fields = ('title', 'body', 'regarding')
    readonly_fields = (
        'html_content', 'created_at', 'sent_at', 'updated_at', 'deleted_at',
        'success_count', 'failure_count', 'last_broadcast_tags',
    )
    inlines = [NotificationDeliveryLogInline]
    actions = [soft_delete_and_broadcast]


@admin.register(NotificationDeliveryLog)
class NotificationDeliveryLogAdmin(admin.ModelAdmin):
    list_display = ('id', 'notification', 'fcm_token', 'status', 'sent_at')
    list_filter = ('status',)
    search_fields = ('fcm_token',)