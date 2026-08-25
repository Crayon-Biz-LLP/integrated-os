"""
Shared email classification prompt template.

Single source of truth for Gmail and Outlook email classification.
Mailbox-specific context is injected via parameters, preventing prompt drift.

Usage:
    prompt = build_email_classify_prompt(
        mailbox_type="personal",  # or "work"
        sender=sender_name,
        subject=subject,
        body=body[:1000],
        to_header=to_header or "",
        cc_header=cc_header or "",
    )
"""


def _mailbox_context(mailbox_type: str, user_name: str, domain_names: list[str]) -> str:
    """Mailbox-scope context, templated with the user's name + domains."""
    domains = ", ".join(domain_names) if domain_names else "the user's work domains"
    if mailbox_type == "work":
        return (
            f"MAILBOX CONTEXT: This is {user_name}'s WORK Outlook inbox. "
            "It receives exclusively work-related emails. "
            "Personal and community emails do NOT arrive here. "
            f"Business domains: {domains}."
        )
    return (
        f"MAILBOX CONTEXT: This is {user_name}'s PERSONAL Gmail inbox. "
        "It is scoped strictly to personal correspondence, family, "
        "and community-related work.\n"
        "This mailbox does NOT receive business emails, client work, "
        f"or vendor communications. Those go to the work inbox ({domains})."
    )


def _arrival_context(mailbox_type: str, domain_names: list[str]) -> str:
    """What legitimately arrives in this mailbox (templated domains)."""
    if mailbox_type == "work":
        # Neutral fallback — NEVER tenant #1's company names in a shared
        # prompt (tenant #2 with no domains row would see Danny's companies
        # as its legitimate work entities). Fresh tenants get domains seeded
        # at onboarding; the generic phrase is the safe unseeded default.
        domains = ", ".join(domain_names) if domain_names else "the user's work domains"
        return (
            "What legitimately arrives here:\n"
            "- Clients: briefs, feedback, approvals, project questions\n"
            "- Vendors: quotes, invoices (human-sent), "
            "delivery confirmations requiring action\n"
            "- Team: employees, contractors, freelancers, collaborators\n"
            "- Business partners: legal, CA, compliance, banking (human-sent)\n"
            f"- Business entities: {domains}"
        )
    return (
        "What legitimately arrives here:\n"
        "- Personal contacts: family, friends, personal relationships\n"
        "- Community contacts: volunteers, local groups, event coordination\n"
        "- Personal finances: CA, personal banking, insurance "
        "(human-sent, not automated alerts)\n"
        "- Government correspondence: direct human responses from officials "
        "(not automated portal emails)\n"
        "- Personal vendors: doctor, school, personal services"
    )


def build_email_classify_prompt(
    mailbox_type: str,
    sender: str,
    subject: str,
    body: str,
    to_header: str = "",
    cc_header: str = "",
    user_name: str | None = None,
    user_context: str | None = None,
) -> str:
    """Build the email classification prompt for the given mailbox type.

    Args:
        mailbox_type: "personal" for Gmail, "work" for Outlook
        sender: From header value
        subject: Email subject
        body: Email body (first ~1000 chars)
        to_header: To header value
        cc_header: CC header value
        user_name: from user_settings (fallback: env USER_NAME / "Danny")
        user_context: one-line 'who they are' (fallback: Danny-era identity)
    """
    from core.services.user_settings import resolve_user_name, resolve_context, resolve_user_orgs
    user_name = user_name or resolve_user_name()
    user_context = user_context or resolve_context()
    domain_names = [d.get("name", "") for d in resolve_user_orgs() if d.get("name")]

    mailbox_context = _mailbox_context(mailbox_type, user_name, domain_names)
    arrival_context = _arrival_context(mailbox_type, domain_names)

    return f"""You are classifying an email for {user_name}. {user_context}

{mailbox_context}

{arrival_context}

Sender: {sender}
To: {to_header}
CC: {cc_header}
Subject: {subject}
Body:
{body[:1000]}

─── CLASSIFICATION RULES ───

CLASSIFY AS "ignored" IF ANY of these are true:
- Sender contains: noreply, no-reply, donotreply, mailer-daemon, bounce, notifications@, automated@, alert@, update@
- It is an OTP, verification code, payment alert, bank notification, delivery update, or booking confirmation
- It is from a SaaS platform, e-commerce site, or any automated system
- It is a newsletter, promotional offer, or bulk mail
- Subject starts with FW: or Fwd: with no new content added

CLASSIFY AS "fyi" IF:
- {user_name} is in CC or BCC (not primary To: recipient)
- A real person is sharing information — a status update, report, or personal FYI — where no response is expected or needed

CLASSIFY AS "actionable" IF:
- Addressed directly To: {user_name}
- From a real individual (family, friend, community member, client, vendor, team member, colleague)
- Requires {user_name} to respond, approve, review, decide, schedule, or fulfill an obligation
- Bias toward actionable for direct messages from real people — when in doubt, surface it

─── OUTPUT RULES ───

suggested_task:
- Verb-first, specific action (e.g., "Confirm attendance for the community meeting", "Send revised proposal to Ananya at TechCorp")
- NULL if fyi or ignored
- NULL if action cannot be stated specifically

needs_draft:
- true if {user_name} needs to write a reply
- true if is_human_sender = true AND the sender is waiting for acknowledgement,
  confirmation, or an update — even if the task itself is an offline action
- false ONLY if the task is a call, meeting, or internal action where
  the sender has no expectation of a response

is_human_sender:
- true if sender is a real individual person
- false for any automated system, platform, or bulk sender

has_memory_value:
- true if the email contains a decision, commitment, project update, relationship context, or information worth remembering weeks later
- false for transactional or routine correspondence
- Can only be true if is_human_sender is also true

Return ONLY valid JSON, NO markdown, NO explanation:
{{
  "classification": "ignored|fyi|actionable",
  "summary": "2 sentences max. Who sent it, what they want or shared.",
  "suggested_task": "verb-first task or null",
  "needs_draft": true or false,
  "linked_person_name": "full name if identifiable, else null",
  "linked_organization_name": "organization or community name if mentioned, else null",
  "is_human_sender": true or false,
  "has_memory_value": true or false
}}"""
