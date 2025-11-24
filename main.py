import asyncio
import markdown
import json
import os
import time
import logging
import aiohttp
import mimetypes
import httpx
from io import BytesIO
from dotenv import load_dotenv
import requests
from uvicorn import Config, Server
from threading import Thread
from fastapi import FastAPI, File, UploadFile, Form, Body, Query
from nio import AsyncClient, MatrixRoom, RoomMessageText, UploadResponse, RoomSendResponse, UploadError
from pydantic import BaseModel
from datetime import datetime, timedelta

log = logging.getLogger("uvicorn")

load_dotenv()

MATRIX_TOKEN = os.getenv("MATRIX_TOKEN")
MATRIX_ROOM = os.getenv("MATRIX_ROOM")
MATRIX_SERVER = os.getenv("MATRIX_SERVER")
MATRIX_USER = os.getenv("MATRIX_USER")
HOMEASSISTANT_TOKEN = os.getenv("HOMEASSISTANT_TOKEN")

class PostMessageWithTitle(BaseModel):
    title: str
    message: str

class PostMessage(BaseModel):
    message: str

class MatrixApi:
    def __init__(self):
        self.config_path = "config/config.json"
        self.collated_messages_path = "config/collated_messages.json"
        self.collated_messages = {}
        self.last_reply = ""
        self.load_config()
        self.load_collated_messages()
        self.setup_client()

    async def schedule_collated_messages(self):
        def get_next_send(time_list: list):
            next_time = None
            for element in time_list:
                send_hour = element[0]
                send_minute = element[1]
                now = datetime.now()
                target = now.replace(hour=send_hour, minute=send_minute, second=0, microsecond=0)

                if target <= now:
                    # if the target time already passed today, schedule for tomorrow
                    target += timedelta(days=1)

                wait_time = (target - now).total_seconds()
                if next_time is None:
                    next_time = wait_time
                elif wait_time < next_time:
                    next_time = wait_time
            # log.info(f"Next wait time: {next_time}")
            return next_time

        while True:
            await self.send_collated_messages()
            await asyncio.sleep(get_next_send(self.config['homeassistant']['collate_settings']['time']))

    async def send_collated_messages(self):
        log.info("Sending collated messages")
        if all(messages == [] for _, messages in self.collated_messages.items()):
            await self.send_message("**Collated Messages:**<br>No new messages")
            return

        for title, messages in self.collated_messages.items():
            if messages == []:
                continue
            log.info(f"Sending collated messages with title: {title}")
            message = f"""**{title.title()} (collated)**<br>{'<br>-----<br>'.join(messages)}"""
            await self.send_message(message),
            self.collated_messages[title] = []
            self.write_collated_messages()

    def setup_client(self):
        self.client = AsyncClient(MATRIX_SERVER, MATRIX_USER)
        self.client.access_token = MATRIX_TOKEN

    def load_config(self):
        if os.path.exists(self.config_path):
            with open(self.config_path, "r") as cf:
                self.config = json.load(cf)
        else:
            self.config = {}
            self.write_config()

    def write_config(self):
        with open(self.config_path, "w") as cf:
            json.dump(self.config, cf, indent=4)

    def write_collated_messages(self):
        with open(self.collated_messages_path, 'w') as cmp:
            json.dump(self.collated_messages, cmp, indent=4)

    def load_collated_messages(self):
        if os.path.exists(self.collated_messages_path):
            with open(self.collated_messages_path, 'r') as cmp:
                self.collated_messages = json.load(cmp)

    def get_homeassistant_input_boolean_state(self, title):
        '''
        gets the state of an input-boolean from homeassistant
        '''
        if 'homeassistant' in self.config:
            value_list = [(url, key) for key, url in self.config['homeassistant']['toggles'].items() if title.lower().startswith(key)]
            if value_list == []:
                return True, None
            url, key = next(iter(value_list))

            log.info(url)
            if url is None:
                return True, None
            headers = {
                "Authorization": f"Bearer {HOMEASSISTANT_TOKEN}",
                "Content-Type": "application/json"
            }
            try:
                response = requests.get(url, headers=headers)
            except:
                log.error("Error getting HomeAssistant response. Returning default (true).")
                return True, None
            try:
                return response.json()['state'] == 'on', key
            except KeyError:
                log.error("KeyError reading HomeAssistant response. Returning default (true).")
                return True, None
        else:
            return True, None

    async def api_send_message(self, json_message: PostMessage):
        message = f"""{json_message.message}"""
        await self.send_message(message)

    async def api_send_message_with_title(self, json_message: PostMessageWithTitle):
        homeassistant_state, key = self.get_homeassistant_input_boolean_state(json_message.title)
        if homeassistant_state:
            message = f"""**{json_message.title}**<br>{json_message.message}"""
            await self.send_message(message)
        else:
            log.info("Message not send, but stored for collated messages")
            if not key in self.collated_messages:
                self.collated_messages[key] = []
            self.collated_messages[key].append(f'{self.timestamp()}<br>{json_message.title}<br>{json_message.message}')
            self.write_collated_messages()

    def timestamp(self):
        return datetime.now().strftime('%d.%m.%Y %H:%M')

    async def api_send_image_url(self, url: str = Query(..., description="Image URL to fetch and send")):
        resp = await self.send_image_url(url)
        return {"status": "ok", "event_id": resp.event_id}

    async def send_image_url(self, url: str) -> RoomSendResponse:
        # Step 1: Fetch the image bytes
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as resp:
                if resp.status != 200:
                    raise Exception(f"Failed to fetch image: {resp.status}")
                data = await resp.read()
                mime_type = resp.headers.get("Content-Type") or mimetypes.guess_type(url)[0]
                filename = url.split("/")[-1] or "image"

        # Step 2: Upload to Matrix
        bio = BytesIO(data)
        upload_resp, upload_id = await self.client.upload(
            bio,
            content_type=mime_type,
            filename=filename,
            filesize=len(data)
        )

        if isinstance(upload_resp, UploadError):
            raise Exception(f"Upload failed: {upload_resp}")

        # if not isinstance(upload_resp, UploadResponse) or not upload_resp.content_uri:
        #     raise Exception(f"Upload failed: {upload_resp}")

        log.info(f"Uploaded image to {upload_resp.content_uri}")
        # Step 3: Send message to the room
        content = {
            "msgtype": "m.image",
            "body": filename,
            "url": upload_resp.content_uri,
            "info": {
                "mimetype": mime_type,
                "size": len(data),
            },
        }

        return await self.client.room_send(
            room_id=MATRIX_ROOM,
            message_type="m.room.message",
            content=content,
        )

    async def send_message(self, message):
        await self.client.room_send(
            room_id=MATRIX_ROOM,
            message_type="m.room.message",
            content={"msgtype": "m.text",
                     "body": message,
                     "format": "org.matrix.custom.html",
                     "formatted_body": markdown.markdown(message)}
        )

    async def run(self):
        asyncio.create_task(self.schedule_collated_messages())  # background task
        await asyncio.gather(
            self.start_api(),
            self.client.sync_forever(timeout=30000)
        )

    async def start_api(self):
        api = FastAPI()
        api.post("/api/send_message")(self.api_send_message)
        api.post("/api/send_message_with_title")(self.api_send_message_with_title)
        api.post("/api/send_image_url")(self.api_send_image_url)
        api.get("/api/send_collated_messages")(self.send_collated_messages)
        # uvicorn.run(api, host="0.0.0.0", port=8000)
        config = Config(api, host="0.0.0.0", port=8000, loop="asyncio")
        server = Server(config)
        await server.serve()

if __name__ == "__main__":
    matrix_api = MatrixApi()
    asyncio.run(matrix_api.run())
