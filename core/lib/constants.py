class EmailStatus:
    NEW         = "new"
    PROCESSING  = "processing"
    PROCESSED   = "processed"
    NEEDS_REPLY = "needs_reply"
    SNOOZED     = "snoozed"
    ERROR       = "error"
    IGNORED     = "ignored"


# Sender names to exclude from context queries — these are Rhodey's own responses
# that should never be fed back as current context (causes hallucination loops)
BOT_SENDERS = {'rhodey_bot', 'rhodey', 'assistant', 'bot'}

