from django.contrib import admin

from .models import Task


@admin.register(Task)
class TaskAdmin(admin.ModelAdmin):
    list_display = ("id", "task_name", "created_by", "assigned_to", "updated_at")
    search_fields = ("task_name",)
