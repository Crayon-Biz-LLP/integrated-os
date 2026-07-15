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


MAILBOX_CONTEXTS = {
    "personal": (
        "MAILBOX CONTEXT: This is Danny's PERSONAL Gmail inbox. "
        "It is scoped strictly to two labels:\n"
        "- inbox: personal correspondence, family, church-related work\n"
        "- Completed/Ashraya: Ashraya is a church ministry Danny leads\n\n"
        "This mailbox does NOT receive Crayon business emails, client work, "
        "or vendor communications. Those go to his Outlook work inbox."
    ),
    "work": (
        "MAILBOX CONTEXT: This is Danny's WORK Outlook inbox. "
        "It receives exclusively work-related emails. "
        "Personal and church emails do NOT arrive here."
    ),
}

ARRIVAL_CONTEXTS = {
    "personal": (
        "What legitimately arrives here:\n"
        "- Personal contacts: family, friends, personal relationships\n"
        "- Church contacts: pastors, ministry team, Ashraya volunteers, "
        "church admin, event coordination\n"
        "- Personal finances: CA, personal banking, insurance "
        "(human-sent, not automated alerts)\n"
        "- Government correspondence: direct human responses from officials "
        "(not automated portal emails)\n"
        "- Personal vendors: doctor, school, personal services"
    ),
    "work": (
        "What legitimately arrives here:\n"
        "- Clients: briefs, feedback, approvals, project questions\n"
        "- Vendors: quotes, invoices (human-sent), "
        "delivery confirmations requiring action\n"
        "- Team: employees, contractors, freelancers, collaborators\n"
        "- Business partners: legal, CA, compliance, banking (human-sent)\n"
        "- Business entities: Crayon, Solvstrat, Product Labs, Qhord"
    ),
}


def build_email_classify_prompt(
    mailbox_type: str,
    sender: str,
    subject: str,
    body: str,
    to_header: str = "",
    cc_header: str = "",
) -> str:
    """Build the email classification prompt for the given mailbox type.

    Args:
        mailbox_type: "personal" for Gmail, "work" for Outlook
        sender: From header value
        subject: Email subject
        body: Email body (first ~1000 chars)
        to_header: To header value
        cc_header: CC header value
    """
    mailbox_context = MAILBOX_CONTEXTS.get(mailbox_type, MAILBOX_CONTEXTS["personal"])
    arrival_context = ARRIVAL_CONTEXTS.get(mailbox_type, ARRIVAL_CONTEXTS["personal"])

    return f"""You are classifying an email for Danny (Yashwant Daniel), founder of Crayon, Chennai, India.

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
- Danny is in CC or BCC (not primary To: recipient)
- A real person is sharing information — a status update, report, or personal FYI — where no response is expected or needed

CLASSIFY AS "actionable" IF:
- Addressed directly To: Danny
- From a real individual (family, friend, church member, client, vendor, team member, colleague)
- Requires Danny to respond, approve, review, decide, schedule, or fulfill an obligation
- Bias toward actionable for direct messages from real people — when in doubt, surface it

─── OUTPUT RULES ───

suggested_task:
- Verb-first, specific action (e.g., "Confirm attendance for Ashraya prayer meeting with Elder Thomas", "Send revised proposal to Ananya at TechCorp")
- NULL if fyi or ignored
- NULL if action cannot be stated specifically

needs_draft:
- true if Danny needs to write a reply
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
  "linked_project_name": "project or ministry name if mentioned, else null",
  "is_human_sender": true or false,
  "has_memory_value": true or false
}}"""
