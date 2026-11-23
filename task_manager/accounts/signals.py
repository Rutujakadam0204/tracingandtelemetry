from django.contrib.auth.models import User
from django.core.cache import cache
from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver

USER_LIST_CACHE_KEY = "accounts:user_list"


@receiver(post_save, sender=User)
@receiver(post_delete, sender=User)
def clear_user_list_cache(**kwargs):
    """A new signup (or deleted user) invalidates the cached 'assign to' list."""
    cache.delete(USER_LIST_CACHE_KEY)
