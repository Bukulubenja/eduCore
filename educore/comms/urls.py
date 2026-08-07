from django.urls import path

from . import views

app_name = "comms"

urlpatterns = [
    path("announcements", views.AnnouncementListView.as_view(),
         name="announcements"),
    path("announcements/<uuid:announcement_id>/publish",
         views.PublishAnnouncementView.as_view(), name="publish-announcement"),
    path("threads", views.ThreadListView.as_view(), name="threads"),
    path("threads/<uuid:thread_id>/messages", views.ThreadMessagesView.as_view(),
         name="thread-messages"),
    path("threads/<uuid:thread_id>/messages/<uuid:message_id>/retract",
         views.RetractMessageView.as_view(), name="retract-message"),
    path("me/inbox", views.InboxView.as_view(), name="inbox"),
    path("me/inbox/<uuid:notification_id>/read",
         views.MarkNotificationReadView.as_view(), name="mark-read"),
]
