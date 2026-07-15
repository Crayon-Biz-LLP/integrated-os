"""Unit tests for build_email_classify_prompt.

Verifies that the shared email classify prompt template correctly injects
mailbox-specific context for both personal (Gmail) and work (Outlook) mailboxes.

No DB or Gemini dependencies — pure string assertions.
"""

from core.prompts.email_classify import build_email_classify_prompt


class TestEmailClassifyPrompt:

    SENDER = "Test Sender <test@example.com>"
    SUBJECT = "Test Subject"
    BODY = "This is a test email body."

    def test_u0_personal_contains_mailbox_context(self):
        """U0a: build_email_classify_prompt('personal') contains PERSONAL Gmail context."""
        prompt = build_email_classify_prompt(
            mailbox_type="personal",
            sender=self.SENDER,
            subject=self.SUBJECT,
            body=self.BODY,
        )
        assert "PERSONAL Gmail" in prompt, (
            "Personal prompt should contain 'PERSONAL Gmail' mailbox context"
        )
        assert "Ashraya" in prompt, (
            "Personal prompt should mention Ashraya for church context"
        )
        # Note: 'Outlook' may appear in the personal prompt context
        # (it mentions that work emails go to 'his Outlook work inbox')

    def test_u0b_work_contains_mailbox_context(self):
        """U0b: build_email_classify_prompt('work') contains WORK Outlook context."""
        prompt = build_email_classify_prompt(
            mailbox_type="work",
            sender=self.SENDER,
            subject=self.SUBJECT,
            body=self.BODY,
        )
        assert "WORK Outlook" in prompt or "Outlook" in prompt, (
            "Work prompt should contain Outlook context"
        )
        assert "PERSONAL Gmail" not in prompt, (
            "Work prompt should not contain PERSONAL Gmail context"
        )

    def test_u0c_personal_arrival_context(self):
        """U0c: Personal prompt includes church/family arrival context."""
        prompt = build_email_classify_prompt(
            mailbox_type="personal",
            sender=self.SENDER,
            subject=self.SUBJECT,
            body=self.BODY,
        )
        assert "family" in prompt.lower() or "church" in prompt.lower(), (
            "Personal prompt should mention family/church arrival context"
        )

    def test_u0d_work_arrival_context(self):
        """U0d: Work prompt includes client/vendor arrival context."""
        prompt = build_email_classify_prompt(
            mailbox_type="work",
            sender=self.SENDER,
            subject=self.SUBJECT,
            body=self.BODY,
        )
        assert "clients" in prompt.lower() or "vendors" in prompt.lower() or "team" in prompt.lower(), (
            "Work prompt should mention clients/vendors/team arrival context"
        )

    def test_u0e_valid_json_instruction(self):
        """U0e: Prompt instructs valid JSON output format."""
        prompt = build_email_classify_prompt(
            mailbox_type="personal",
            sender=self.SENDER,
            subject=self.SUBJECT,
            body=self.BODY,
        )
        assert "classification" in prompt
        assert "summary" in prompt
        assert "suggested_task" in prompt
        assert "is_human_sender" in prompt
        assert "has_memory_value" in prompt

    def test_u0f_sender_subject_body_included(self):
        """U0f: Prompt includes sender, subject, and body fields."""
        prompt = build_email_classify_prompt(
            mailbox_type="personal",
            sender=self.SENDER,
            subject=self.SUBJECT,
            body=self.BODY,
        )
        assert self.SENDER in prompt
        assert self.SUBJECT in prompt
        assert self.BODY in prompt

    def test_u0g_body_truncation(self):
        """U0g: Long body is truncated to ~1000 chars."""
        long_body = "A" * 2000
        prompt = build_email_classify_prompt(
            mailbox_type="personal",
            sender=self.SENDER,
            subject=self.SUBJECT,
            body=long_body,
        )
        # The full 2000 chars won't be there — should be truncated
        assert "A" * 2000 not in prompt
        # But the JSON output instruction should still be at the end
        assert "Return ONLY valid JSON" in prompt
