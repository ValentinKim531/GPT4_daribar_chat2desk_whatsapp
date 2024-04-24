from fastapi import FastAPI, Request
from openai import AsyncOpenAI
import httpx
import asyncio
from environs import Env
import logging
import json
import re

logging.basicConfig(level=logging.INFO)

app = FastAPI()
env = Env()
env.read_env()

CHAT2DESK_API_URL = "https://api.chat2desk.com/v1/messages"
CHAT2DESK_CLIENTS_URL = "https://api.chat2desk.com/v1/clients"
CHAT2DESK_WEBHOOKS_URL = "https://api.chat2desk.com/v1/webhooks"
CHAT2DESK_TOKEN = env.str("CHAT2DESK_TOKEN")
OPENAI_API_KEY = env.str("OPENAI_API_KEY")
ASSISTANT_ID = env.str("ASSISTANT_ID")
WEBHOOK_URL = "https://gpt4daribarchat2deskwhatsapp-production.up.railway.app/receive-message/"

client = AsyncOpenAI(api_key=OPENAI_API_KEY)

ALLOWED_PHONE_NUMBERS = ['77073200049', '77759596671', '77073352450', '77017054477', '77775846961']


async def manage_webhook():
    headers = {"Authorization": CHAT2DESK_TOKEN, "Content-Type": "application/json"}
    payload = {"url": WEBHOOK_URL, "name": "MyAppWebhook", "events": ["inbox", "outbox"]}
    async with httpx.AsyncClient() as client:
        resp = await client.get(CHAT2DESK_WEBHOOKS_URL, headers=headers)
        if resp.status_code == 200:
            existing_webhooks = resp.json().get('data', [])
            for webhook in existing_webhooks:
                if webhook['url'] == WEBHOOK_URL:
                    delete_resp = await client.delete(f"{CHAT2DESK_WEBHOOKS_URL}/{webhook['id']}", headers=headers)
                    logging.info(f"Deleted existing webhook: {delete_resp.text}")
        response = await client.post(CHAT2DESK_WEBHOOKS_URL, headers=headers, json=payload)
        if response.status_code in [200, 201]:
            logging.info("Webhook successfully set up")
        else:
            logging.error(f"Failed to set up webhook: {response.text}")

@app.on_event("startup")
async def startup_event():
    await manage_webhook()

def remove_annotations(text: str) -> str:
    pattern = r'\【.*?\】'
    cleaned_text = re.sub(pattern, '', text)
    return cleaned_text

async def get_or_create_client(phone_number):
    if phone_number not in ALLOWED_PHONE_NUMBERS:
        logging.info(f"Access denied for phone number {phone_number}")
        return None
    headers = {"Authorization": CHAT2DESK_TOKEN, "Content-Type": "application/json"}
    json_data = {"phone": phone_number, "transport": "whatsapp"}
    async with httpx.AsyncClient() as http_client:
        response = await http_client.post(CHAT2DESK_CLIENTS_URL, headers=headers, json=json_data)
        if response.status_code == 200:
            return response.json()['data']['id']
        elif response.status_code == 400 and "Client already exist" in response.text:
            error_details = json.loads(response.text)
            client_id = json.loads(error_details['errors']['client'][1])['id']
            return client_id
        else:
            logging.error(f"Failed to create or find the client in Chat2Desk: {response.text}")
            return None

processed_messages = set()

@app.post("/receive-message/")
async def receive_message(request: Request):
    data = await request.json()
    message_id = data.get('message_id', None)
    hook_type = data.get('hook_type', None)

    if hook_type != 'inbox':
        logging.info(f"Ignored non-inbox message with message ID {message_id}")
        return {"status": "ignored", "message": "Non-inbox message ignored."}

    if message_id in processed_messages:
        logging.info(f"Skipping duplicate processing for message ID: {message_id}")
        return {"status": "skipped", "message": "Duplicate message, processing skipped."}

    processed_messages.add(message_id)
    logging.info(f"Processing new message ID: {message_id}")

    client_info = data.get('client', {})
    phone_number = client_info.get('phone', '')
    user_message = data.get('text', 'No text provided')
    client_id = await get_or_create_client(phone_number)
    if not client_id:
        return {"status": "error", "message": "Could not identify or create client in Chat2Desk"}

    logging.info(f"Received message from WhatsApp user ID {client_id}: '{user_message}', {message_id}")

    thread = await client.beta.threads.create()
    await client.beta.threads.messages.create(thread_id=thread.id, role="user", content=user_message)

    run = await client.beta.threads.runs.create(thread_id=thread.id, assistant_id=ASSISTANT_ID)
    while run.status in ['queued', 'in_progress', 'cancelling']:
        await asyncio.sleep(1)
        run = await client.beta.threads.runs.retrieve(thread_id=thread.id, run_id=run.id)

    if run.status == 'completed':
        messages = await client.beta.threads.messages.list(thread_id=thread.id)
        assistant_message = ' '.join([remove_annotations(msg.content[0].text.value) for msg in messages.data if msg.role == 'assistant'])
        logging.info(f"Answer from Assistant: '{assistant_message}'")

        headers = {"Authorization": CHAT2DESK_TOKEN}
        async with httpx.AsyncClient() as http_client:
            response = await http_client.post(
                f"{CHAT2DESK_API_URL}?text={assistant_message}&client_id={client_id}&transport=whatsapp",
                headers=headers)
            if response.status_code == 200:
                logging.info(f"Message successfully sent to phone {phone_number}: {assistant_message}")
            else:
                logging.error(f"Failed to send message to phone {phone_number}: {response.status_code} {response.text}")
    else:
        logging.error(f"Failed to get a response from the assistant")
        assistant_message = "Unable to get a response from the assistant."

    return {"status": "sent", "response": assistant_message}


if len(processed_messages) > 1000:
    processed_messages.clear()

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8000)
