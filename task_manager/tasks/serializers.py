from django.contrib.auth.models import User
from rest_framework import serializers

from .models import Task


class TaskSerializer(serializers.ModelSerializer):
    assigned_to = serializers.PrimaryKeyRelatedField(
        queryset=User.objects.all(), allow_null=True, required=False
    )
    created_by_username = serializers.CharField(source="created_by.username", read_only=True)
    assigned_to_username = serializers.CharField(
        source="assigned_to.username", read_only=True, default=None
    )
    is_owner = serializers.SerializerMethodField()

    class Meta:
        model = Task
        fields = [
            "id",
            "task_name",
            "task_desc",
            "assigned_to",
            "assigned_to_username",
            "created_by",
            "created_by_username",
            "is_owner",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["created_by", "created_at", "updated_at"]

    def get_is_owner(self, obj):
        return obj.created_by_id == self.context["request"].user.id

    def validate_assigned_to(self, value):
        """Only the creator may change who a task is assigned to."""
        request = self.context["request"]
        task = self.instance
        if task is not None and task.created_by_id != request.user.id and value != task.assigned_to:
            raise serializers.ValidationError("Only the task creator can reassign this task.")
        return value
