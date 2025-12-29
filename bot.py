import asyncio
import json
import os
from telethon import TelegramClient, events, Button
from telethon.errors import SessionPasswordNeededError, FloodWaitError
from telethon.tl.types import User
from aiohttp import web

# Configuration
API_ID = '32882051'
API_HASH = 'e4a6bb647206b2c37fbcfd0231e083a8'
SESSION_NAME = 'forwarder_session'
DATA_FILE = 'channels_data.json'

class ChannelForwarder:
    def __init__(self):
        self.client = TelegramClient(SESSION_NAME, API_ID, API_HASH)
        self.data = self.load_data()
        self.monitored_channels = set(self.data.get('channels', []))
        self.dump_channel = self.data.get('dump_channel', None)
        self.me = None
        self.pending_forwards = []
        self.bulk_forward_queue = []
        self.bulk_in_progress = False
        
    def load_data(self):
        """Load channels data from JSON file"""
        if os.path.exists(DATA_FILE):
            with open(DATA_FILE, 'r') as f:
                return json.load(f)
        return {'channels': [], 'dump_channel': None}
    
    def save_data(self):
        """Save channels data to JSON file"""
        self.data['channels'] = list(self.monitored_channels)
        self.data['dump_channel'] = self.dump_channel
        with open(DATA_FILE, 'w') as f:
            json.dump(self.data, f, indent=2)
    
    async def send_message_to_me(self, text):
        """Send message to yourself (bot owner)"""
        if self.me:
            await self.client.send_message(self.me.id, text)
    
    async def start(self):
        """Start the client and authenticate"""
        await self.client.start()
        
        # Check if already authorized
        if not await self.client.is_user_authorized():
            await self.client.send_message('me', '🤖 **Channel Forwarder Bot**\n\nPlease authenticate to start.')
            
            async with self.client.conversation('me') as conv:
                await conv.send_message('Enter your phone number (with country code, e.g., +1234567890):')
                phone_msg = await conv.get_response()
                phone = phone_msg.text.strip()
                
                try:
                    await self.client.send_code_request(phone)
                    await conv.send_message('OTP sent! Enter the code:')
                    code_msg = await conv.get_response()
                    code = code_msg.text.strip()
                    
                    try:
                        await self.client.sign_in(phone, code)
                        await conv.send_message('✅ Authentication successful!')
                    except SessionPasswordNeededError:
                        await conv.send_message('🔐 2FA enabled. Enter your password:')
                        pwd_msg = await conv.get_response()
                        password = pwd_msg.text.strip()
                        await self.client.sign_in(password=password)
                        await conv.send_message('✅ Authentication successful!')
                except Exception as e:
                    await conv.send_message(f'❌ Authentication failed: {e}')
                    return
        
        self.me = await self.client.get_me()
        await self.send_message_to_me(
            f"✅ **Bot is running!**\n\n"
            f"Logged in as: {self.me.first_name}\n"
            f"Username: @{self.me.username}\n\n"
            f"**Commands:**\n"
            f"/add <channel_id> - Add channel to monitor\n"
            f"/rem <channel_id> - Remove channel\n"
            f"/list - Show all channels\n"
            f"/dump <channel_id> - Set dump channel\n"
            f"/all <channel_id> - Forward all messages from channel\n"
            f"/status - Bot status"
        )
        
        @self.client.on(events.NewMessage(chats=list(self.monitored_channels)))
        async def message_handler(event):
            await self.forward_message(event)
        
        @self.client.on(events.NewMessage(from_users='me', pattern=r'^/'))
        async def command_handler(event):
            await self.handle_command(event)
        
        asyncio.create_task(self.process_pending_forwards())
        asyncio.create_task(self.process_bulk_forwards())
        
        await self.client.run_until_disconnected()
    
    async def forward_message(self, event):
        """Forward messages from monitored channels to dump channel"""
        if not self.dump_channel:
            await self.send_message_to_me("⚠️ No dump channel set! Use /dump <channel_id>")
            return
        
        forward_data = {
            'dump_channel': self.dump_channel,
            'message': event.message,
            'chat_id': event.chat_id
        }
        
        try:
            await self.client.forward_messages(
                self.dump_channel,
                event.message,
                event.chat_id
            )
        except FloodWaitError as e:
            self.pending_forwards.append(forward_data)
            await self.send_message_to_me(
                f"⏳ **FloodWait detected!**\n"
                f"Waiting {e.seconds} seconds...\n"
                f"Message queued and will be forwarded automatically."
            )
            await asyncio.sleep(e.seconds)
            await self.retry_forward(forward_data)
        except Exception as e:
            await self.send_message_to_me(f"❌ Error forwarding message from {event.chat_id}: {e}")
    
    async def retry_forward(self, forward_data):
        """Retry forwarding a message"""
        try:
            await self.client.forward_messages(
                forward_data['dump_channel'],
                forward_data['message'],
                forward_data['chat_id']
            )
            if forward_data in self.pending_forwards:
                self.pending_forwards.remove(forward_data)
            await self.send_message_to_me("✅ Queued message forwarded successfully!")
        except FloodWaitError as e:
            await self.send_message_to_me(f"⏳ Still in FloodWait. Waiting {e.seconds} more seconds...")
            await asyncio.sleep(e.seconds)
            await self.retry_forward(forward_data)
        except Exception as e:
            await self.send_message_to_me(f"❌ Failed to forward queued message: {e}")
            if forward_data in self.pending_forwards:
                self.pending_forwards.remove(forward_data)
    
    async def process_pending_forwards(self):
        """Background task to process pending forwards"""
        while True:
            await asyncio.sleep(10)
            if self.pending_forwards and not self.bulk_in_progress:
                forward_data = self.pending_forwards[0]
                await self.retry_forward(forward_data)
    
    async def process_bulk_forwards(self):
        """Background task to process bulk forwards"""
        while True:
            await asyncio.sleep(5)
            if self.bulk_forward_queue and not self.bulk_in_progress:
                self.bulk_in_progress = True
                await self.continue_bulk_forward()
                self.bulk_in_progress = False
    
    async def continue_bulk_forward(self):
        """Continue forwarding bulk messages"""
        if not self.bulk_forward_queue:
            return
        
        task = self.bulk_forward_queue[0]
        channel_id = task['channel_id']
        messages = task['messages']
        current_index = task['current_index']
        total = task['total']
        
        try:
            while current_index < len(messages):
                msg = messages[current_index]
                
                try:
                    await self.client.forward_messages(
                        self.dump_channel,
                        msg.id,
                        channel_id
                    )
                    current_index += 1
                    task['current_index'] = current_index
                    
                    if current_index % 10 == 0:
                        await self.send_message_to_me(
                            f"📤 **Bulk Forward Progress**\n\n"
                            f"Progress: {current_index}/{total} messages\n"
                            f"Channel: `{channel_id}`"
                        )
                    
                    await asyncio.sleep(1)
                    
                except FloodWaitError as e:
                    await self.send_message_to_me(
                        f"⏳ **FloodWait during bulk forward!**\n\n"
                        f"Waiting {e.seconds} seconds...\n"
                        f"Progress: {current_index}/{total} messages\n"
                        f"Will resume automatically after wait."
                    )
                    await asyncio.sleep(e.seconds)
                    continue
                    
                except Exception as e:
                    await self.send_message_to_me(
                        f"❌ Error forwarding message {current_index}/{total}: {e}\n"
                        f"Skipping and continuing..."
                    )
                    current_index += 1
                    task['current_index'] = current_index
                    continue
            
            await self.send_message_to_me(
                f"✅ **Bulk forward completed!**\n\n"
                f"Total forwarded: {total} messages\n"
                f"Channel: `{channel_id}`"
            )
            self.bulk_forward_queue.pop(0)
            
        except Exception as e:
            await self.send_message_to_me(f"❌ Bulk forward failed: {e}")
            self.bulk_forward_queue.pop(0)
    
    async def handle_command(self, event):
        """Handle commands from user"""
        text = event.text.strip()
        parts = text.split()
        cmd = parts[0].lower()
        
        try:
            if cmd == '/add' and len(parts) == 2:
                channel_id = int(parts[1])
                result = await self.add_channel(channel_id)
                await event.reply(result)
            
            elif cmd == '/rem' and len(parts) == 2:
                channel_id = int(parts[1])
                result = await self.remove_channel(channel_id)
                await event.reply(result)
            
            elif cmd == '/list':
                result = await self.list_channels()
                await event.reply(result)
            
            elif cmd == '/dump' and len(parts) == 2:
                channel_id = int(parts[1])
                result = await self.set_dump_channel(channel_id)
                await event.reply(result)
            
            elif cmd == '/status':
                status = (
                    f"🤖 **Bot Status**\n\n"
                    f"📊 Monitored channels: {len(self.monitored_channels)}\n"
                    f"📤 Dump channel: {'Set' if self.dump_channel else 'Not set'}\n"
                    f"⏳ Pending forwards: {len(self.pending_forwards)}\n"
                    f"📦 Bulk tasks in queue: {len(self.bulk_forward_queue)}\n"
                    f"✅ Status: Active"
                )
                await event.reply(status)
            
            elif cmd == '/all' and len(parts) == 2:
                channel_id = int(parts[1])
                await self.bulk_forward_all(event, channel_id)
            
            else:
                await event.reply(
                    "❌ **Invalid command**\n\n"
                    "**Available commands:**\n"
                    "/add <channel_id>\n"
                    "/rem <channel_id>\n"
                    "/list\n"
                    "/dump <channel_id>\n"
                    "/all <channel_id>\n"
                    "/status"
                )
        
        except ValueError:
            await event.reply("❌ Invalid channel ID. Must be a number.")
        except Exception as e:
            await event.reply(f"❌ Error: {e}")
    
    async def add_channel(self, channel_id):
        """Add a channel to monitor"""
        try:
            entity = await self.client.get_entity(channel_id)
            
            if channel_id in self.monitored_channels:
                return f"⚠️ Channel already being monitored: {getattr(entity, 'title', channel_id)}"
            
            self.monitored_channels.add(channel_id)
            self.save_data()
            
            self.client.remove_event_handler(self.forward_message)
            
            @self.client.on(events.NewMessage(chats=list(self.monitored_channels)))
            async def message_handler(event):
                await self.forward_message(event)
            
            return f"✅ **Channel added successfully!**\n\nChannel: {getattr(entity, 'title', channel_id)}\nID: {channel_id}"
        except ValueError:
            return f"❌ **Invalid channel ID**\n\nMake sure:\n• You're a member of the channel\n• The ID is correct\n• You have access to view messages"
        except Exception as e:
            return f"❌ **Error adding channel**\n\n{e}"
    
    async def remove_channel(self, channel_id):
        """Remove a channel from monitoring"""
        if channel_id in self.monitored_channels:
            self.monitored_channels.remove(channel_id)
            self.save_data()
            
            self.client.remove_event_handler(self.forward_message)
            
            @self.client.on(events.NewMessage(chats=list(self.monitored_channels)))
            async def message_handler(event):
                await self.forward_message(event)
            
            return f"✅ **Channel removed**\n\nChannel ID: {channel_id}"
        return f"❌ Channel {channel_id} is not in the monitored list"
    
    async def list_channels(self):
        """List all monitored channels"""
        if not self.monitored_channels and not self.dump_channel:
            return "📋 **No channels configured**\n\nUse /add to add channels\nUse /dump to set dump channel"
        
        result = "📋 **Channel Configuration**\n\n"
        
        if self.monitored_channels:
            result += "**Monitored Channels:**\n"
            for ch_id in self.monitored_channels:
                try:
                    entity = await self.client.get_entity(ch_id)
                    result += f"• {getattr(entity, 'title', 'Unknown')} (`{ch_id}`)\n"
                except:
                    result += f"• ID: `{ch_id}`\n"
        else:
            result += "**Monitored Channels:** None\n"
        
        result += "\n"
        
        if self.dump_channel:
            try:
                dump_entity = await self.client.get_entity(self.dump_channel)
                result += f"**Dump Channel:**\n• {getattr(dump_entity, 'title', 'Unknown')} (`{self.dump_channel}`)"
            except:
                result += f"**Dump Channel:** `{self.dump_channel}`"
        else:
            result += "**Dump Channel:** Not set ⚠️"
        
        return result
    
    async def set_dump_channel(self, channel_id):
        """Set the dump channel"""
        try:
            entity = await self.client.get_entity(channel_id)
            self.dump_channel = channel_id
            self.save_data()
            return f"✅ **Dump channel set!**\n\nChannel: {getattr(entity, 'title', channel_id)}\nID: {channel_id}\n\nAll messages will be forwarded here."
        except Exception as e:
            return f"❌ **Error setting dump channel**\n\n{e}\n\nMake sure you have access to this channel."
    
    async def bulk_forward_all(self, event, channel_id):
        """Forward all messages from a channel to dump channel"""
        if not self.dump_channel:
            await event.reply("❌ **No dump channel set!**\n\nUse /dump <channel_id> first.")
            return
        
        if self.bulk_in_progress:
            await event.reply("⚠️ **Another bulk forward is in progress!**\n\nPlease wait for it to complete.")
            return
        
        try:
            entity = await self.client.get_entity(channel_id)
            channel_name = getattr(entity, 'title', str(channel_id))
            
            await event.reply(
                f"🔍 **Starting bulk forward...**\n\n"
                f"Channel: {channel_name}\n"
                f"Fetching all messages, please wait..."
            )
            
            messages = []
            async for message in self.client.iter_messages(channel_id):
                messages.append(message)
            
            total = len(messages)
            
            if total == 0:
                await event.reply("⚠️ No messages found in this channel.")
                return
            
            messages.reverse()
            
            await event.reply(
                f"📦 **Found {total} messages!**\n\n"
                f"Channel: {channel_name}\n"
                f"Starting forward process...\n\n"
                f"This may take a while. You'll get progress updates every 10 messages."
            )
            
            self.bulk_forward_queue.append({
                'channel_id': channel_id,
                'messages': messages,
                'current_index': 0,
                'total': total
            })
            
        except ValueError:
            await event.reply(
                f"❌ **Invalid channel ID**\n\n"
                f"Make sure:\n"
                f"• You're a member of the channel\n"
                f"• The ID is correct\n"
                f"• You have access to view messages"
            )
        except Exception as e:
            await event.reply(f"❌ **Error starting bulk forward**\n\n{e}")


# Health check server for Render
async def health_check(request):
    return web.Response(text="Bot is running! ✅")

async def start_health_server():
    """Start HTTP server for Render health checks"""
    app = web.Application()
    app.router.add_get('/', health_check)
    app.router.add_get('/health', health_check)
    
    port = int(os.environ.get('PORT', 10000))
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', port)
    await site.start()
    print(f"✅ Health check server running on port {port}")


async def main():
    # Start health check server for Render
    await start_health_server()
    
    # Start the bot
    bot = ChannelForwarder()
    await bot.start()


if __name__ == '__main__':
    asyncio.run(main())
