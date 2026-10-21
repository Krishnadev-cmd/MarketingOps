"""The fixed vocabularies of the console. StrEnum values are what the DB and API store."""

from enum import StrEnum


class Role(StrEnum):
    CREATOR = "creator"                  # drafts content
    REVIEWER = "reviewer"                # comments and requests changes, cannot approve
    COMPLIANCE = "compliance_officer"    # approves or rejects, never their own content
    PUBLISHER = "publisher"              # schedules and publishes approved content
    ADMIN = "admin"                      # manages users and channel connections


class Channel(StrEnum):
    LINKEDIN = "linkedin"
    WHATSAPP = "whatsapp"
    WEBSITE = "website"


class ContentType(StrEnum):
    POST = "post"                # LinkedIn company page post
    STATUS = "status"            # WhatsApp Status image/video caption (posted by hand)
    TEMPLATE = "template"        # WhatsApp approved message template
    BANNER = "banner"            # website banner
    HERO = "hero"                # website hero text
    PROMO = "promo"              # website promo block


class Status(StrEnum):
    DRAFT = "draft"
    IN_REVIEW = "in_review"                  # waiting for a Compliance Officer
    CHANGES_REQUESTED = "changes_requested"  # rejected: back with the author
    APPROVED = "approved"
    SCHEDULED = "scheduled"
    PUBLISHED = "published"


class Decision(StrEnum):
    APPROVED = "approved"
    REJECTED = "rejected"
    COMMENT = "comment"


class JobStatus(StrEnum):
    PENDING = "pending"                  # waiting for its scheduled time
    PUBLISHING = "publishing"            # claimed by one process; nobody else may run it
    AWAITING_MANUAL = "awaiting_manual"  # channel has no API: a person must post it
    SUCCEEDED = "succeeded"
    BLOCKED = "blocked"                  # compliance re-check failed at publish time
    FAILED = "failed"
    CANCELLED = "cancelled"


# Which content types make sense on which channel. The studio offers only these.
CHANNEL_TYPES: dict[Channel, list[ContentType]] = {
    Channel.LINKEDIN: [ContentType.POST],
    Channel.WHATSAPP: [ContentType.STATUS, ContentType.TEMPLATE],
    Channel.WEBSITE: [ContentType.BANNER, ContentType.HERO, ContentType.PROMO],
}
