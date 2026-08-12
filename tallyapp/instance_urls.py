from django.urls import path
from .views import TallyInstanceListView, TallyInstanceDetailView

urlpatterns = [
    path("tally-instances/", TallyInstanceListView.as_view()),
    path("tally-instances/<str:tag>/", TallyInstanceDetailView.as_view()),
]