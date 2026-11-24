from finengine.auth.db import User
from finengine.models.document import DocumentMeta


def user_can_see(doc: DocumentMeta, user: User) -> bool:
    if user.is_admin:
        return True
    return getattr(doc, "owner_id", 0) == user.id
