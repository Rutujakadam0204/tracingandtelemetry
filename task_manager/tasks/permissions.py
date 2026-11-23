from rest_framework.permissions import SAFE_METHODS, BasePermission


class IsCreatorOrAssignee(BasePermission):
    """
    - Creator: full CRUD.
    - Assignee: can view and edit, but cannot delete.
    """

    def has_object_permission(self, request, view, obj):
        user_id = request.user.id
        is_creator = obj.created_by_id == user_id
        is_assignee = obj.assigned_to_id == user_id

        if request.method == "DELETE":
            return is_creator
        if request.method in SAFE_METHODS or request.method in ("PUT", "PATCH"):
            return is_creator or is_assignee
        return False
