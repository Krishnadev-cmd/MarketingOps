"""The AI drafter: turns a brief into a first draft, and fixes blocked drafts.

    drafter.draft(brief, channel, type, rules) -> Draft
    drafter.fix(title, body, channel, type, rules) -> Draft

The model never has the last word. Every draft goes through the compliance
checker; if it still breaks a blocking rule, the model gets one retry with the
exact problems listed. Whatever comes back, a person edits it and a Compliance
Officer approves it before anything is published.
"""

from __future__ import annotations

import json
import logging

from pydantic import BaseModel, Field, ValidationError

from ..config import Settings
from ..services import compliance
from . import prompts

log = logging.getLogger(__name__)


class DraftText(BaseModel):
    """The JSON shape the model must return."""

    title: str = Field(min_length=1, max_length=200)
    body: str = Field(min_length=1, max_length=3000)


class Draft(BaseModel):
    title: str
    body: str
    by_ai: bool
    note: str = ""
    report: dict = {}


class Drafter:
    def __init__(self, settings: Settings) -> None:
        self.model = settings.gemini_model
        self.client = None
        if settings.llm_enabled and settings.gemini_api_key:
            # Imported here so tests and template-only deployments never initialize Gemini.
            from google import genai
            from google.genai import types

            self.client = genai.Client(
                api_key=settings.gemini_api_key,
                http_options=types.HttpOptions(timeout=40_000),
            )
        else:
            log.info("AI drafting is off: drafts come from channel templates")

    @property
    def enabled(self) -> bool:
        return self.client is not None

    # ------------------------------------------------------------------
    def draft(self, brief: str, channel: str, content_type: str, rules: list[compliance.Rule]) -> Draft:
        applicable = [r for r in rules if r.applies_to(channel)]
        prohibited = [r.text for r in applicable if r.kind == "prohibited"]
        disclaimers = [r.text for r in applicable if r.kind == "disclaimer" and r.severity == "block"]

        if self.enabled:
            schema = json.dumps(DraftText.model_json_schema())
            try:
                text = self._ask(prompts.draft_prompt(brief, channel, content_type, prohibited, disclaimers, schema))
                return self._check_and_retry(text, channel, content_type, rules, "Drafted by AI")
            except Exception as exc:   # no login, quota, network, bad JSON twice: never break the studio
                log.warning("AI draft failed, using the template: %s", exc)

        return self._template(brief, channel, content_type, disclaimers, rules)

    def fix(self, title: str, body: str, channel: str, content_type: str,
            rules: list[compliance.Rule]) -> Draft:
        report = compliance.scan(title, body, channel, content_type, rules)
        if report["passed"] or not self.enabled:
            note = "Nothing to fix." if report["passed"] else "AI is off. Fix the highlighted issues by hand."
            return Draft(title=title, body=body, by_ai=False, note=note, report=report)
        try:
            text = self._ask(prompts.fix_prompt(title, body, _problems(report),
                                                json.dumps(DraftText.model_json_schema())))
            return self._check_and_retry(text, channel, content_type, rules, "Fixed by AI")
        except Exception as exc:
            log.warning("AI fix failed: %s", exc)
            return Draft(title=title, body=body, by_ai=False, report=report,
                         note="The AI could not fix this. Edit the highlighted issues by hand.")

    # ------------------------------------------------------------------
    def _check_and_retry(self, text: DraftText, channel: str, content_type: str,
                         rules: list[compliance.Rule], note: str) -> Draft:
        report = compliance.scan(text.title, text.body, channel, content_type, rules)
        if not report["passed"]:
            log.info("AI draft broke a rule, asking once more")
            text = self._ask(prompts.fix_prompt(text.title, text.body, _problems(report),
                                                json.dumps(DraftText.model_json_schema())))
            report = compliance.scan(text.title, text.body, channel, content_type, rules)
        if not report["passed"]:
            note += ". The checker still found issues: they are highlighted for you to fix"
        return Draft(title=text.title, body=text.body, by_ai=True, note=note, report=report)

    def _ask(self, user_prompt: str) -> DraftText:
        from google.genai import types

        response = self.client.models.generate_content(
            model=self.model,
            contents=user_prompt,
            config=types.GenerateContentConfig(
                system_instruction=prompts.SYSTEM_PROMPT,
                temperature=0.4,
                max_output_tokens=900,
                response_mime_type="application/json",
            ),
        )
        raw = response.text or ""
        try:
            return DraftText.model_validate_json(strip_fences(raw))
        except ValidationError as exc:
            raise ValueError(f"Model returned JSON in the wrong shape: {exc}") from exc

    def _template(self, brief: str, channel: str, content_type: str, disclaimers: list[str],
                  rules: list[compliance.Rule]) -> Draft:
        """No model: fill the channel template with the brief, so the writer still has a start."""
        template = next(t for t in prompts.TEMPLATES if t["channel"] == channel and t["type"] == content_type)
        first_line = brief.strip().splitlines()[0][:70] if brief.strip() else ""
        title = template["title"] + first_line if template["title"].endswith(": ") else first_line or template["title"]
        body = template["body"].replace("{disclaimer}", "\n".join(disclaimers))
        report = compliance.scan(title, body, channel, content_type, rules)
        return Draft(title=title, body=body, by_ai=False, report=report,
                     note="AI is off, so this is the channel template. Replace the [brackets].")


def _problems(report: dict) -> list[str]:
    return [f["message"] for f in report["findings"] if f["severity"] == "block"]


def strip_fences(text: str) -> str:
    """Models wrap JSON in ```json fences however firmly you ask them not to."""
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1].rsplit("```", 1)[0]
    return text.strip()
