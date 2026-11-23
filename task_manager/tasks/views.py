import logging

from django.db.models import Q
from rest_framework import viewsets
from rest_framework.permissions import IsAuthenticated

from .models import Task
from .permissions import IsCreatorOrAssignee
from .serializers import TaskSerializer
from .tasks import send_assignment_notification

logger = logging.getLogger(__name__)


class TaskViewSet(viewsets.ModelViewSet):
    """
    GET    /api/tasks/        -> tasks I created + tasks assigned to me
    POST   /api/tasks/        -> create
    GET    /api/tasks/{id}/   -> detail
    PUT/PATCH /api/tasks/{id}/ -> creator or assignee
    DELETE /api/tasks/{id}/   -> creator only
    """

    serializer_class = TaskSerializer
    permission_classes = [IsAuthenticated, IsCreatorOrAssignee]

    def get_queryset(self):
        user = self.request.user
        return (
            Task.objects.filter(Q(created_by=user) | Q(assigned_to=user))
            .select_related("created_by", "assigned_to")
            .distinct()
        )

    def perform_create(self, serializer):
        task = serializer.save(created_by=self.request.user)
        logger.info("Task %s created by %s", task.id, self.request.user.username)
        if task.assigned_to_id:
            send_assignment_notification.delay(task.id)

    def perform_update(self, serializer):
        old_assignee = serializer.instance.assigned_to_id
        task = serializer.save()
        logger.info("Task %s updated by %s", task.id, self.request.user.username)
        if task.assigned_to_id and task.assigned_to_id != old_assignee:
            send_assignment_notification.delay(task.id)

    def perform_destroy(self, instance):
        logger.info("Task %s deleted by %s", instance.id, self.request.user.username)
        instance.delete()
