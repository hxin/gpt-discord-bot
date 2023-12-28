from collections import defaultdict
from typing import Literal, Optional, Union

import discord
from discord import Message as DiscordMessage, app_commands
import logging
from src.base import Message, Conversation, ThreadConfig
from src.constants import (
    BOT_INVITE_URL,
    DISCORD_BOT_TOKEN,
    EXAMPLE_CONVOS,
    ACTIVATE_THREAD_PREFX,
    MAX_THREAD_MESSAGES,
    SECONDS_DELAY_RECEIVING_MSG,
    AVAILABLE_MODELS,
    DEFAULT_MODEL,
    ASSISTANT_ID
)
import asyncio
from src.utils import (
    logger,
    should_block,
    close_thread,
    is_last_message_stale,
    discord_message_to_message,
)
# from src import completion
from src import assistant
# from src.completion import generate_completion_response, process_response
from src.assistant import client as openai_client
from src.assistant import generate_assistant_response, process_response


from src.moderation import (
    moderate_message,
    send_moderation_blocked_message,
    send_moderation_flagged_message,
)

logging.basicConfig(
    format="[%(asctime)s] [%(filename)s:%(lineno)d] %(message)s", level=logging.INFO
)

intents = discord.Intents.default()
intents.message_content = True

client = discord.Client(intents=intents)
tree = discord.app_commands.CommandTree(client)
thread_data = defaultdict()




@client.event
async def on_ready():
    logger.info(f"We have logged in as {client.user}. Invite URL: {BOT_INVITE_URL}")
    assistant.MY_BOT_NAME = client.user.name
    assistant.MY_BOT_EXAMPLE_CONVOS = []
    for c in EXAMPLE_CONVOS:
        messages = []
        for m in c.messages:
            if m.user == "Lenard":
                messages.append(Message(user=client.user.name, text=m.text))
            else:
                messages.append(m)
        assistant.MY_BOT_EXAMPLE_CONVOS.append(Conversation(messages=messages))
    await tree.sync()


# /chat message:
@tree.command(name="chat", description="Create a new thread for conversation")
@discord.app_commands.checks.has_permissions(send_messages=True)
@discord.app_commands.checks.has_permissions(view_channel=True)
@discord.app_commands.checks.bot_has_permissions(send_messages=True)
@discord.app_commands.checks.bot_has_permissions(view_channel=True)
@discord.app_commands.checks.bot_has_permissions(manage_threads=True)
@app_commands.describe(message="The first prompt to start the chat with")
async def chat_command(
        int: discord.Interaction,
        message: str,
        model: AVAILABLE_MODELS = DEFAULT_MODEL,
        temperature: Optional[float] = 1.0,
        max_tokens: Optional[int] = 512,
):
    try:
        # only support creating thread in text channel
        if not isinstance(int.channel, discord.TextChannel):
            return

        # block servers not in allow list
        if should_block(guild=int.guild):
            return

        user = int.user
        logger.info(f"Chat command by {user} {message[:20]}")

        # Check for valid temperature
        if temperature is not None and (temperature < 0 or temperature > 1):
            await int.response.send_message(
                f"You supplied an invalid temperature: {temperature}. Temperature must be between 0 and 1.",
                ephemeral=True,
            )
            return

        # Check for valid max_tokens
        if max_tokens is not None and (max_tokens < 1 or max_tokens > 4096):
            await int.response.send_message(
                f"You supplied an invalid max_tokens: {max_tokens}. Max tokens must be between 1 and 4096.",
                ephemeral=True,
            )
            return

        try:
            embed = discord.Embed(
                description=f"<@{user.id}> wants to ask a question! 🤖💬",
                color=discord.Color.green(),
            )
            embed.add_field(name="model", value=model)
            embed.add_field(name="temperature", value=temperature, inline=True)
            embed.add_field(name="max_tokens", value=max_tokens, inline=True)
            embed.add_field(name=user.name, value=message)

            await int.response.send_message(embed=embed)
            response = await int.original_response()

        except Exception as e:
            logger.exception(e)
            await int.response.send_message(
                f"Failed to start chat {str(e)}", ephemeral=True
            )
            return

        # create the thread
        openai_thread = openai_client.beta.threads.create()
        thread = await response.create_thread(
            name=f"{ACTIVATE_THREAD_PREFX} {user.name[:20]} - {message[:30]} - {openai_thread.id}",
            slowmode_delay=1,
            reason='gpt',
            auto_archive_duration=60,
        )
        thread_data[thread.id] = ThreadConfig(
            model=model, max_tokens=max_tokens, temperature=temperature
        )



        async with thread.typing():
            # openai_client.beta.threads.messages.create(thread_id=openai_thread.id, role="user", content=message)
            # run = await openai_client.beta.threads.runs.create(thread_id=openai_thread.id, assistant_id=ASSISTANT_ID)

            # fetch completion
            response_data = generate_assistant_response(
                thread_id=openai_thread.id, user_message=message)
            # send the result
            await process_response(
                user=user, thread=thread, response_data=response_data
            )
    except Exception as e:
        logger.exception(e)
        await int.response.send_message(
            f"Failed to start chat {str(e)}", ephemeral=True
        )


# calls for each message
@client.event
async def on_message(message: DiscordMessage):
    try:
        # block servers not in allow list
        if should_block(guild=message.guild):
            return

        # ignore messages from the bot
        if message.author == client.user:
            return

        # ignore messages not in a thread
        channel = message.channel
        if not isinstance(channel, discord.Thread):
            return

        # ignore threads not created by the bot
        thread = channel
        if thread.owner_id != client.user.id:
            return

        # ignore threads that are archived locked or title is not what we want
        if (
                thread.archived
                or thread.locked
                or not thread.name.startswith(ACTIVATE_THREAD_PREFX)
        ):
            # ignore this thread
            return

        if thread.message_count > MAX_THREAD_MESSAGES:
            # too many messages, no longer going to reply
            await close_thread(thread=thread)
            return


        # wait a bit in case user has more messages
        if SECONDS_DELAY_RECEIVING_MSG > 0:
            await asyncio.sleep(SECONDS_DELAY_RECEIVING_MSG)
            if is_last_message_stale(
                    interaction_message=message,
                    last_message=thread.last_message,
                    bot_id=client.user.id,
            ):
                # there is another message, so ignore this one
                return

        logger.info(
            f"Thread message to process - {message.author}: {message.content[:50]} - {thread.name} {thread.jump_url}"
        )

        channel_messages = [
            discord_message_to_message(message)
            async for message in thread.history(limit=MAX_THREAD_MESSAGES)
        ]
        channel_messages = [x for x in channel_messages if x is not None]
        channel_messages.reverse()

        # generate the response
        async with thread.typing():
            openai_thread_id = thread.name.split('-')[2].strip()  # Get the name part and remove leading/trailing spaces
            response_data = generate_assistant_response(
                thread_id=openai_thread_id, user_message=message.content)


        if is_last_message_stale(
                interaction_message=message,
                last_message=thread.last_message,
                bot_id=client.user.id,
        ):
            # there is another message and its not from us, so ignore this response
            return

        # send the result
        await process_response(
            user=message.author.name, thread=thread, response_data=response_data
        )
    except Exception as e:
        logger.exception(e)


client.run(DISCORD_BOT_TOKEN)
