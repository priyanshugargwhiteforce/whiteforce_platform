from django.urls import path
from .views import (
    CreateNotificationView,
    SendNotificationView,
    ValidateTokensView, 
    BroadcastNotificationView,
    UpdateNotificationView,
    DeleteNotificationView,
    SendWiraNotificationView,
)

urlpatterns = [
    path('<str:domain>/notifications/create/', CreateNotificationView.as_view(), name='notification-create'),
    path('<str:domain>/notifications/validate-tokens/', ValidateTokensView.as_view(), name='notification-validate-tokens'),
    path('<str:domain>/notifications/send/', SendNotificationView.as_view(), name='notification-send'),
    path('<str:domain>/notifications/broadcast/', BroadcastNotificationView.as_view(), name='notification-broadcast'),
    path('<str:domain>/notifications/<int:pk>/update/', UpdateNotificationView.as_view(), name='notification-update'),
    path('<str:domain>/notifications/<int:pk>/delete/', DeleteNotificationView.as_view(), name='notification-delete'),
    path('<str:domain>/notifications/send-wira/', SendWiraNotificationView.as_view(), name='send-wira'),
 ]