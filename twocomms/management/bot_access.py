META_REVIEWER_GROUP_NAME = "Meta Bot Reviewer"


def is_meta_bot_reviewer(user) -> bool:
    return bool(
        user.is_authenticated
        and user.groups.filter(name=META_REVIEWER_GROUP_NAME).exists()
    )
