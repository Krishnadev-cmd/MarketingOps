"""Everything the drafter tells the model, in one readable file.

Brand voice and channel guides live here so marketing can change them in a
pull request without touching the drafting code. The same guides are shown to
writers in the studio, so humans and the model follow one rulebook.
"""

from __future__ import annotations

BRAND_VOICE = [
    "Plain English. Short sentences. Write to one reader as “you”.",
    "Calm and factual. Never promise, predict or imply returns.",
    "Sentence case for headlines. No exclamation marks. No emoji.",
    "Use numbers only if they appear in the brief. Never invent statistics.",
    "One clear call to action per piece.",
]

CHANNEL_GUIDES: dict[tuple[str, str], str] = {
    ("linkedin", "post"): "A LinkedIn company page post. Open with a one line hook under 150 characters. "
                          "Then two or three short paragraphs and one call to action. At most three "
                          "hashtags on the last line. Aim for 600 to 1,300 characters.",
    ("whatsapp", "status"): "A caption for a WhatsApp Status image. One idea, under 300 characters, "
                            "one call to action. The team posts it by hand from the business phone.",
    ("whatsapp", "template"): "A WhatsApp Business message template for customers who opted in. Under 900 "
                              "characters. Use {{1}} for the customer's first name. Helpful, not pushy. "
                              "End with: Reply STOP to opt out.",
    ("website", "banner"): "A website banner. Title under 60 characters. Body is one sentence under 140 "
                           "characters, then the disclaimer.",
    ("website", "hero"): "A website hero section. Title under 70 characters. Body under 200 characters "
                         "explaining who it is for and what they get, then the disclaimer.",
    ("website", "promo"): "A website promo block. Title under 60 characters. Two or three sentences, "
                          "then the disclaimer.",
}

# Starting points writers can pick in the studio. {disclaimer} is filled in by the API.
TEMPLATES = [
    {"id": "li-educational", "channel": "linkedin", "type": "post", "name": "Educational post",
     "title": "Explainer: ",
     "body": "[One line hook: the question your reader is already asking]\n\n"
             "[Two or three sentences that answer it plainly]\n\n"
             "[One call to action]\n\n{disclaimer}\n\n#PersonalFinance"},
    {"id": "li-event", "channel": "linkedin", "type": "post", "name": "Webinar invite",
     "title": "Webinar: ",
     "body": "[What the session covers, in one line]\n\n"
             "Date: [day, date, time and time zone]\nSpeaker: [name, role]\n\n"
             "Register: [link]\n\n{disclaimer}"},
    {"id": "wa-status", "channel": "whatsapp", "type": "status", "name": "Status caption",
     "title": "Status: ", "body": "[One idea in one sentence]. [Call to action].\n\n{disclaimer}"},
    {"id": "wa-template", "channel": "whatsapp", "type": "template", "name": "Customer reminder",
     "title": "Template: ",
     "body": "Hi {{1}}, [the helpful reminder in one sentence].\n\n[What to do next].\n\n"
             "{disclaimer}\n\nReply STOP to opt out."},
    {"id": "web-banner", "channel": "website", "type": "banner", "name": "Banner",
     "title": "[Headline under 60 characters]", "body": "[One sentence]. {disclaimer}"},
    {"id": "web-hero", "channel": "website", "type": "hero", "name": "Hero",
     "title": "[Headline under 70 characters]",
     "body": "[Who it is for and what they get, under 200 characters].\n\n{disclaimer}"},
    {"id": "web-promo", "channel": "website", "type": "promo", "name": "Promo block",
     "title": "[Headline under 60 characters]",
     "body": "[Two or three sentences about the offer].\n\n{disclaimer}"},
]

SYSTEM_PROMPT = (
    "You write marketing copy for a regulated investment firm. Compliance comes first: "
    "a draft that breaks a rule is worse than no draft. Answer with JSON only."
)


def draft_prompt(brief: str, channel: str, content_type: str, prohibited: list[str],
                 disclaimers: list[str], schema: str) -> str:
    voice = "\n".join(f"- {line}" for line in BRAND_VOICE)
    banned = ", ".join(f'"{p.rstrip("*")}"' for p in prohibited) or "none"
    required = "\n".join(f'- "{d}"' for d in disclaimers) or "- none"
    return f"""Write one piece of content.

Channel and format:
{CHANNEL_GUIDES.get((channel, content_type), "")}

Brand voice:
{voice}

Never use these words or phrases, or any form of them: {banned}

Include each of these disclaimers word for word at the end of the body:
{required}

The brief from the marketing team:
\"\"\"{brief.strip()}\"\"\"

Return JSON matching this schema, nothing else:
{schema}"""


def fix_prompt(title: str, body: str, problems: list[str], schema: str) -> str:
    listed = "\n".join(f"- {p}" for p in problems)
    return f"""This draft was blocked by the compliance checker.

Title: {title}
Body:
\"\"\"{body}\"\"\"

Problems:
{listed}

Rewrite it so every problem is fixed. Keep the meaning, length and tone otherwise.
Return JSON matching this schema, nothing else:
{schema}"""
