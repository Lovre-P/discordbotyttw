import discord
from discord.ext import commands
import yt_dlp as youtube_dl
import asyncio
import os
import random

intents = discord.Intents.all()
bot = commands.Bot(command_prefix='!', intents=intents, help_command=None)

youtube_dl.utils.bug_reports_message = lambda: ''

ytdl_format_options = {
    'format': 'bestaudio/best',
    'noplaylist': True,
    'extract_flat': False,  # Ensure we get full data
    'restrictfilenames': True,
    'nocheckcertificate': True,
    'ignoreerrors': True,
    'logtostderr': False,
    'quiet': True,
    'no_warnings': True,
    'default_search': 'auto',
    'source_address': '0.0.0.0',  # Bind to IPv4
    'forceipv4': True,
}

ffmpeg_options = {
    'before_options': '-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5',
    'options': '-vn',
}

ytdl = youtube_dl.YoutubeDL(ytdl_format_options)

class YTDLSource(discord.PCMVolumeTransformer):
    def __init__(self, source, *, data, volume=0.5):
        super().__init__(source, volume)
        self.data = data
        self.title = data.get('title')
        self.url = data.get('url') or data.get('webpage_url')
        self.thumbnail = data.get('thumbnail')
        self.video_id = data.get('id')  # Stores the video ID
        self.uploader = data.get('uploader')

    @classmethod
    async def create_source(cls, ctx, search: str, *, loop=None):
        loop = loop or asyncio.get_event_loop()

        to_run = lambda: ytdl.extract_info(search, download=False)

        data = await loop.run_in_executor(None, to_run)

        if data is None:
            return None

        if 'entries' in data:
            data = data['entries'][0]

        if data:
            return cls(discord.FFmpegPCMAudio(data['url'], **ffmpeg_options), data=data)
        else:
            return None

class MusicPlayer:
    def __init__(self, ctx):
        self.bot = ctx.bot
        self.guild = ctx.guild
        self.channel = ctx.channel
        self.cog = ctx.cog

        self.queue = asyncio.Queue()
        self.next = asyncio.Event()

        self.np = None  # Now playing message
        self.volume = 0.5
        self.current = None
        self.autoplay = False  # Initial state of autoplay
        self.played_song_ids = []  # Stores previously played song IDs

        ctx.bot.loop.create_task(self.player_loop())

    async def get_next_song(self):
        loop = self.bot.loop
        if not self.played_song_ids:
            return None

        last_song_video_id = self.played_song_ids[-1]
        last_song_url = f"https://www.youtube.com/watch?v={last_song_video_id}"

        to_run = lambda: ytdl.extract_info(last_song_url, download=False)
        data = await loop.run_in_executor(None, to_run)

        if data is None:
            return None

        related_videos = data.get('related_videos', [])
        potential_videos = [
            vid for vid in related_videos if vid.get('id') not in self.played_song_ids
        ]

        if potential_videos:
            next_video = random.choice(potential_videos)
            video_url = f"https://www.youtube.com/watch?v={next_video['id']}"
            to_run = lambda: ytdl.extract_info(video_url, download=False)
            next_song_data = await loop.run_in_executor(None, to_run)
            if next_song_data is None:
                return None
            return YTDLSource(
                discord.FFmpegPCMAudio(next_song_data['url'], **ffmpeg_options),
                data=next_song_data
            )
        else:
            # Optional fallback to a default search
            search_query = "popular music"
            to_run = lambda: ytdl.extract_info(f"ytsearch:{search_query}", download=False)
            data = await loop.run_in_executor(None, to_run)

            if data and 'entries' in data:
                potential_songs = [
                    entry for entry in data['entries']
                    if entry.get('id') not in self.played_song_ids
                ]
                if potential_songs:
                    next_song_data = random.choice(potential_songs)
                    return YTDLSource(
                        discord.FFmpegPCMAudio(next_song_data['url'], **ffmpeg_options),
                        data=next_song_data
                    )
            return None

    async def player_loop(self):
        await self.bot.wait_until_ready()

        while not self.bot.is_closed():
            self.next.clear()

            try:
                # Try to get the next song from the queue with a timeout
                source = await asyncio.wait_for(self.queue.get(), timeout=300)
            except asyncio.TimeoutError:
                # If the queue is empty for 5 minutes, disconnect
                return await self.destroy(self.guild)

            if not isinstance(source, YTDLSource):
                try:
                    source = await YTDLSource.create_source(self.channel, source, loop=self.bot.loop)
                except Exception as e:
                    await self.channel.send(f'Error processing song: {str(e)}')
                    continue

            if source is None:
                await self.channel.send("Unable to find or play the song.")
                continue

            source.volume = self.volume
            self.current = source

            # Add current song's video ID to playback history
            self.played_song_ids.append(source.video_id)
            if len(self.played_song_ids) > 100:
                self.played_song_ids.pop(0)  # Maintain the size of the list

            self.guild.voice_client.play(
                source,
                after=lambda e: self.bot.loop.call_soon_threadsafe(self.play_next, e)
            )

            # Display "Now Playing" message
            embed = discord.Embed(
                title="Now Playing",
                description=f"**{source.title}**",
                color=discord.Color.blue()
            )
            embed.set_thumbnail(url=source.thumbnail)
            await self.channel.send(embed=embed)

            await self.next.wait()

            # Clean up resources
            source.cleanup()
            self.current = None

            # Autoplay
            if self.autoplay and self.queue.empty():
                next_song = await self.get_next_song()
                if next_song:
                    await self.queue.put(next_song)
                else:
                    await self.channel.send("No next song found for autoplay.")

    def play_next(self, error):
        if error:
            print(f'Player error: {error}')
        self.next.set()

    def destroy(self, guild):
        return self.bot.loop.create_task(self.cog.cleanup(guild))

class Music(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.players = {}

    async def cleanup(self, guild):
        try:
            await guild.voice_client.disconnect()
        except AttributeError:
            pass

        try:
            del self.players[guild.id]
        except KeyError:
            pass

    def get_player(self, ctx):
        try:
            player = self.players[ctx.guild.id]
        except KeyError:
            player = MusicPlayer(ctx)
            self.players[ctx.guild.id] = player

        return player

    @commands.command(name='join', help='Tells the bot to join the voice channel')
    async def join(self, ctx):
        if not ctx.author.voice:
            return await ctx.send(f"{ctx.author.name}, you must be in a voice channel to summon me.")

        channel = ctx.author.voice.channel

        if ctx.voice_client is not None:
            return await ctx.voice_client.move_to(channel)

        await channel.connect()

    @commands.command(name='play', help='Adds a song to the queue or searches for it on YouTube')
    async def play(self, ctx, *, search: str):
        player = self.get_player(ctx)

        if ctx.voice_client is None:
            await ctx.invoke(self.join)

        async with ctx.typing():
            try:
                source = await YTDLSource.create_source(ctx, search, loop=self.bot.loop)
            except Exception as e:
                return await ctx.send(f'An error occurred: {str(e)}')

            if source is None:
                return await ctx.send("No song found.")

            await player.queue.put(source)
            await ctx.send(f'Added to queue: **{source.title}**')

    @commands.command(name='playnow', help='Immediately plays a song, interrupting the current one')
    async def playnow(self, ctx, *, search: str):
        player = self.get_player(ctx)

        if ctx.voice_client is None:
            await ctx.invoke(self.join)

        async with ctx.typing():
            try:
                source = await YTDLSource.create_source(ctx, search, loop=self.bot.loop)
            except Exception as e:
                return await ctx.send(f'An error occurred: {str(e)}')

            if source is None:
                return await ctx.send("No song found.")

            # Clear the queue and stop the current song
            player.queue._queue.clear()
            if ctx.voice_client.is_playing():
                ctx.voice_client.stop()
            await player.queue.put(source)
            await ctx.send(f"Now playing: **{source.title}**")

    @commands.command(name='autoplay', help='Toggles autoplay of related songs')
    async def autoplay(self, ctx):
        player = self.get_player(ctx)
        player.autoplay = not player.autoplay
        state = "enabled" if player.autoplay else "disabled"
        await ctx.send(f"Autoplay is now {state}.")

    @commands.command(name='stop', help='Stops the current song and clears the queue')
    async def stop(self, ctx):
        player = self.get_player(ctx)
        player.queue._queue.clear()
        if ctx.voice_client is not None:
            ctx.voice_client.stop()
        await ctx.send("Music stopped and queue cleared.")

    @commands.command(name='leave', help='Tells the bot to leave the voice channel')
    async def leave(self, ctx):
        await self.cleanup(ctx.guild)
        await ctx.send("Leaving the voice channel.")

    @commands.command(name='skip', help='Skips the current song')
    async def skip(self, ctx):
        if ctx.voice_client is None:
            return await ctx.send("I'm not connected to a voice channel.")

        if ctx.voice_client.is_playing():
            ctx.voice_client.stop()
            await ctx.send("Song skipped.")
        else:
            await ctx.send("I'm not playing anything right now.")

    @commands.command(name='vol', help='Sets the volume (1-100)')
    async def set_volume(self, ctx, volume: int):
        if ctx.voice_client is None:
            return await ctx.send("I'm not connected to a voice channel.")

        if volume < 1 or volume > 100:
            return await ctx.send("Volume must be between 1 and 100.")

        player = self.get_player(ctx)
        player.volume = volume / 100
        if ctx.voice_client.source:
            ctx.voice_client.source.volume = volume / 100
        await ctx.send(f"Volume set to {volume}%.")

    @commands.command(name='pause', help='Pauses playback')
    async def pause(self, ctx):
        if ctx.voice_client.is_playing():
            ctx.voice_client.pause()
            await ctx.send("Playback paused.")
        else:
            await ctx.send("I'm not playing anything right now.")

    @commands.command(name='resume', help='Resumes playback')
    async def resume(self, ctx):
        if ctx.voice_client.is_paused():
            ctx.voice_client.resume()
            await ctx.send("Playback resumed.")
        else:
            await ctx.send("Playback is not paused.")

    @commands.command(name='queue', help='Displays the current queue')
    async def queue_info(self, ctx):
        player = self.get_player(ctx)

        if player.queue.empty():
            return await ctx.send('The queue is empty.')

        queue_list = list(player.queue._queue)

        embed = discord.Embed(
            title="Playback Queue",
            description="Current songs in the queue:",
            color=discord.Color.green()
        )

        for i, song in enumerate(queue_list, 1):
            embed.add_field(name=f"{i}. {song.title}", value=song.url, inline=False)

        await ctx.send(embed=embed)

    @commands.command(name='nowplaying', help='Displays the currently playing song')
    async def now_playing(self, ctx):
        player = self.get_player(ctx)
        if player.current:
            embed = discord.Embed(
                title="Now Playing",
                description=f"**{player.current.title}**",
                color=discord.Color.blue()
            )
            embed.set_thumbnail(url=player.current.thumbnail)
            await ctx.send(embed=embed)
        else:
            await ctx.send("I'm not playing anything right now.")

@bot.event
async def on_ready():
    print(f'Bot is ready. Logged in as {bot.user.name}')
    await bot.add_cog(Music(bot))

@bot.command(name='help', help='Displays this help message')
async def help_command(ctx):
    embed = discord.Embed(
        title="Music God's Commands",
        description="I am the almighty Music God, ruler of sound and rhythm. Here are my divine commands:",
        color=discord.Color.gold()
    )

    commands_list = {
        "!join": "Summons me to your voice sanctuary",
        "!play <search term or URL>": "Adds a song to the queue or searches for it on YouTube",
        "!playnow <search term or URL>": "Immediately plays a song, interrupting the current one",
        "!autoplay": "Toggles autoplay of related songs",
        "!stop": "Silences my divine music and clears the queue",
        "!queue": "Displays the current playback queue",
        "!nowplaying": "Displays the currently playing song",
        "!leave": "Sends me back to my celestial realm",
        "!skip": "Skips the current song",
        "!pause": "Pauses playback",
        "!resume": "Resumes playback",
        "!vol <1-100>": "Sets the volume (1-100)",
    }

    for cmd, desc in commands_list.items():
        embed.add_field(name=cmd, value=desc, inline=False)

    embed.set_footer(text="Use these commands wisely, mortal!")

    await ctx.send(embed=embed)

if __name__ == "__main__":
    TOKEN = os.getenv('DISCORD_BOT_TOKEN')
    if not TOKEN:
        print("Bot token not found! Please set the DISCORD_BOT_TOKEN environment variable.")
    else:
        bot.run(TOKEN)
