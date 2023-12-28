import asyncio
import time
from enum import Enum
from dataclasses import dataclass
import openai
# from openai import AsyncOpenAI
from openai import OpenAI

from typing import Optional, List

from src.constants import (
    BOT_INSTRUCTIONS,
    BOT_NAME,
    EXAMPLE_CONVOS,
    ASSISTANT_ID,
)
import discord
from src.base import Message, Prompt, Conversation, ThreadConfig
from src.utils import split_into_shorter_messages, close_thread, logger
from src.moderation import (
    send_moderation_flagged_message,
    send_moderation_blocked_message,
)

MY_BOT_NAME = BOT_NAME
MY_BOT_EXAMPLE_CONVOS = EXAMPLE_CONVOS


class CompletionResult(Enum):
    OK = 0
    TOO_LONG = 1
    INVALID_REQUEST = 2
    OTHER_ERROR = 3
    MODERATION_FLAGGED = 4
    MODERATION_BLOCKED = 5


@dataclass
class CompletionData:
    status: CompletionResult
    reply_text: Optional[str]
    status_text: Optional[str]


client = OpenAI()
# client = AsyncOpenAI()

async def submit_message(assistant_id, thread_id, user_message):
    client.beta.threads.messages.create(
        thread_id=thread_id, role="user", content=user_message
    )
    return client.beta.threads.runs.create(
        thread_id=thread_id,
        assistant_id=assistant_id,
    )

def create_thread_and_run(user_input):
    thread = client.beta.threads.create()
    run = submit_message(ASSISTANT_ID, thread, user_input)
    return thread, run

def get_response(thread):
    return client.beta.threads.messages.list(thread_id=thread.id, order="asc")

def generate_assistant_response( thread_id, user_message):
    try:
        client.beta.threads.messages.create(thread_id=thread_id, role="user", content=user_message)
        run =  client.beta.threads.runs.create(thread_id=thread_id, assistant_id=ASSISTANT_ID)
        run =  client.beta.threads.runs.retrieve(
            thread_id=thread_id,
            run_id=run.id,
        )
        while run.status == "queued" or run.status == "in_progress":
            run =  client.beta.threads.runs.retrieve(
                thread_id=thread_id,
                run_id=run.id,
            )
            # await asyncio.sleep(10)
            time.sleep(2)

        reply = client.beta.threads.messages.list(thread_id=thread_id, order="desc")

        return CompletionData(
            status=CompletionResult.OK, reply_text=reply.data[0].content[0].text.value, status_text=None
        )
    except openai.BadRequestError as e:
        if "This model's maximum context length" in str(e):
            return CompletionData(
                status=CompletionResult.TOO_LONG, reply_text=None, status_text=str(e)
            )
        else:
            logger.exception(e)
            return CompletionData(
                status=CompletionResult.INVALID_REQUEST,
                reply_text=None,
                status_text=str(e),
            )
    except Exception as e:
        logger.exception(e)
        return CompletionData(
            status=CompletionResult.OTHER_ERROR, reply_text=None, status_text=str(e)
        )


async def process_response(
    user: str, thread: discord.Thread, response_data: CompletionData
):
    status = response_data.status
    reply_text = response_data.reply_text
    status_text = response_data.status_text
    if status is CompletionResult.OK or status is CompletionResult.MODERATION_FLAGGED:
        sent_message = None
        if not reply_text:
            sent_message = await thread.send(
                embed=discord.Embed(
                    description=f"**Invalid response** - empty response",
                    color=discord.Color.yellow(),
                )
            )
        else:
            shorter_response = split_into_shorter_messages(reply_text)
            for r in shorter_response:
                sent_message = await thread.send(r)
    elif status is CompletionResult.TOO_LONG:
        await close_thread(thread)
    elif status is CompletionResult.INVALID_REQUEST:
        await thread.send(
            embed=discord.Embed(
                description=f"**Invalid request** - {status_text}",
                color=discord.Color.yellow(),
            )
        )
    else:
        await thread.send(
            embed=discord.Embed(
                description=f"**Error** - {status_text}",
                color=discord.Color.yellow(),
            )
        )
