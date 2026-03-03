import os
import httpx

WHATSAPP_API_URL = "https://graph.facebook.com/v22.0"

async def send_whatsapp(phone_number_id: str, to: str, text: str):
    """Send a text message via WhatsApp Business API."""
    url = f"{WHATSAPP_API_URL}/{phone_number_id}/messages"
    headers = {
        "Authorization": f"Bearer {os.getenv('WHATSAPP_ACCESS_TOKEN')}",
        "Content-Type": "application/json"
    }
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "text",
        "text": {"body": text}
    }
    async with httpx.AsyncClient() as client:
        response = await client.post(url, json=payload, headers=headers)
        if not response.is_success:
            print(f"[WA ERROR] Failed to send to {to}: {response.text}")
        return response

async def process_whatsapp_webhook(update: dict):
    """
    Parse a Meta WhatsApp webhook payload and handle the incoming message.
    
    Meta payload structure:
    {
      "object": "whatsapp_business_account",
      "entry": [{
        "id": "...",
        "changes": [{
          "value": {
            "messaging_product": "whatsapp",
            "metadata": {"phone_number_id": "..."},
            "contacts": [{"profile": {"name": "..."}, "wa_id": "..."}],
            "messages": [{"from": "...", "type": "text", "text": {"body": "..."}}]
          },
          "field": "messages"
        }]
      }]
    }
    """
    try:
        # Validate it's a WhatsApp Business Account event
        if update.get("object") != "whatsapp_business_account":
            return

        for entry in update.get("entry", []):
            for change in entry.get("changes", []):
                # Only handle "messages" field events
                if change.get("field") != "messages":
                    continue

                value = change.get("value", {})
                messages = value.get("messages", [])
                metadata = value.get("metadata", {})

                phone_number_id = metadata.get("phone_number_id")
                if not phone_number_id:
                    print("[WA] No phone_number_id in metadata")
                    continue

                for message in messages:
                    # Only process text messages for now
                    if message.get("type") != "text":
                        continue

                    from_number = message.get("from")  # user's WhatsApp number
                    body = message.get("text", {}).get("body", "").strip()

                    print(f"[WA INCOMING] From: {from_number} | Message: {body}")

                    # Route the message through our state machine
                    await handle_whatsapp_message(
                        phone_number_id=phone_number_id,
                        from_number=from_number,
                        body=body,
                        value=value
                    )

    except Exception as e:
        print(f"[WA CRITICAL] Error processing webhook: {str(e)}")


async def handle_whatsapp_message(phone_number_id: str, from_number: str, body: str, value: dict):
    """Route incoming WhatsApp messages and send appropriate replies."""

    lower_body = body.lower().strip()

    # --- INITIALIZE COMMAND (equivalent to Telegram's /start) ---
    if lower_body in ["initialize", "start", "hi", "hello", "/start"]:
        # Get the user's display name from contacts if available
        contacts = value.get("contacts", [])
        first_name = "Leader"
        if contacts:
            first_name = contacts[0].get("profile", {}).get("name", "Leader").split()[0]

        welcome_msg = (
            f"🎯 *Welcome to your 14-Day Sprint, {first_name}.*\n\n"
            "I am your Digital 2iC. Let's configure your engine.\n\n"
            "Reply with your Persona:\n\n"
            "1️⃣ *Commander* — Direct and urgent. Rapid execution.\n\n"
            "2️⃣ *Architect* — Methodical and structured. Systems-focused.\n\n"
            "3️⃣ *Nurturer* — Balanced and proactive. Team-focused."
        )
        await send_whatsapp(phone_number_id, from_number, welcome_msg)
        return

    # --- FALLBACK: Capture raw dump (active user sending thoughts) ---
    # For now, acknowledge receipt — full state machine integration comes next
    await send_whatsapp(
        phone_number_id,
        from_number,
        "✅ Received. Your engine is processing this input."
    )
