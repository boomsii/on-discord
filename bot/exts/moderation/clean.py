import contextlib
import logging
import re
import time
from collections import defaultdict
from contextlib import suppress
from datetime import datetime
from itertools import islice
from typing import Any, Callable, Iterable, Literal, Optional, TYPE_CHECKING, Union

from discord import Colour, Message, NotFound, TextChannel, User, errors
from discord.ext.commands import Cog, Context, Converter, Greedy, group, has_any_role
from discord.ext.commands.converter import TextChannelConverter
from discord.ext.commands.errors import BadArgument

from bot.bot import Bot
from bot.constants import Channels, CleanMessages, Colours, Emojis, Event, Icons, MODERATION_ROLES
from bot.converters import Age, ISODateTime
from bot.exts.moderation.modlog import ModLog
from bot.utils.channel import is_mod_channel

log = logging.getLogger(__name__)

# Default number of messages to look at in each channel.
DEFAULT_TRAVERSE = 10
# Number of seconds before command invocations and responses are deleted in non-moderation channels.
MESSAGE_DELETE_DELAY = 5

# Type alias for checks for whether a message should be deleted.
Predicate = Callable[[Message], bool]
# Type alias for message lookup ranges.
CleanLimit = Union[Message, Age, ISODateTime]


class CleanChannels(Converter):
    """A converter that turns the given string to a list of channels to clean, or the literal `*` for all channels."""

    _channel_converter = TextChannelConverter()

    async def convert(self, ctx: Context, argument: str) -> Union[Literal["*"], list[TextChannel]]:
        """Converts a string to a list of channels to clean, or the literal `*` for all channels."""
        if argument == "*":
            return "*"
        return [await self._channel_converter.convert(ctx, channel) for channel in argument.split()]


class Regex(Converter):
    """A converter that takes a string in the form `.+` and returns the contents of the inline code compiled."""

    async def convert(self, ctx: Context, argument: str) -> re.Pattern:
        """Strips the backticks from the string and compiles it to a regex pattern."""
        match = re.fullmatch(r"`(.+?)`", argument)
        if not match:
            raise BadArgument("Regex pattern missing wrapping backticks")
        try:
            return re.compile(match.group(1), re.IGNORECASE + re.DOTALL)
        except re.error as e:
            raise BadArgument(f"Regex error: {e.msg}")


if TYPE_CHECKING:  # Used to allow method resolution in IDEs like in converters.py.
    CleanChannels = Union[Literal["*"], list[TextChannel]]  # noqa: F811
    Regex = re.Pattern  # noqa: F811


class Clean(Cog):
    """
    A cog that allows messages to be deleted in bulk while applying various filters.

    You can delete messages sent by a specific user, messages sent by bots, all messages, or messages that match a
    specific regular expression.

    The deleted messages are saved and uploaded to the database via an API endpoint, and a URL is returned which can be
    used to view the messages in the Discord dark theme style.
    """

    def __init__(self, bot: Bot):
        self.bot = bot
        self.cleaning = False

    @property
    def mod_log(self) -> ModLog:
        """Get currently loaded ModLog cog instance."""
        return self.bot.get_cog("ModLog")

    # region: Helper functions

    @staticmethod
    def _validate_input(
            traverse: int,
            channels: Optional[CleanChannels],
            bots_only: bool,
            users: Optional[list[User]],
            first_limit: Optional[CleanLimit],
            second_limit: Optional[CleanLimit],
    ) -> None:
        """Raise errors if an argument value or a combination of values is invalid."""
        # Is this an acceptable amount of messages to traverse?
        if traverse > CleanMessages.message_limit:
            raise BadArgument(f"Cannot traverse more than {CleanMessages.message_limit} messages.")

        if (isinstance(first_limit, Message) or isinstance(second_limit, Message)) and channels:
            raise BadArgument("Both a message limit and channels specified.")

        if isinstance(first_limit, Message) and isinstance(second_limit, Message):
            # Messages are not in same channel.
            if first_limit.channel != second_limit.channel:
                raise BadArgument("Message limits are in different channels.")

        if users and bots_only:
            raise BadArgument("Marked as bots only, but users were specified.")

        # This is an implementation error rather than user error.
        if second_limit and not first_limit:
            raise ValueError("Second limit specified without the first.")

    @staticmethod
    async def _send_expiring_message(ctx: Context, content: str) -> None:
        """Send `content` to the context channel. Automatically delete if it's not a mod channel."""
        delete_after = None if is_mod_channel(ctx.channel) else MESSAGE_DELETE_DELAY
        await ctx.send(content, delete_after=delete_after)

    @staticmethod
    def _build_predicate(
        bots_only: bool = False,
        users: Optional[list[User]] = None,
        regex: Optional[re.Pattern] = None,
        first_limit: Optional[datetime] = None,
        second_limit: Optional[datetime] = None,
    ) -> Predicate:
        """Return the predicate that decides whether to delete a given message."""
        def predicate_bots_only(message: Message) -> bool:
            """Return True if the message was sent by a bot."""
            return message.author.bot

        def predicate_specific_users(message: Message) -> bool:
            """Return True if the message was sent by the user provided in the _clean_messages call."""
            return message.author in users

        def predicate_regex(message: Message) -> bool:
            """Check if the regex provided in _clean_messages matches the message content or any embed attributes."""
            content = [message.content]

            # Add the content for all embed attributes
            for embed in message.embeds:
                content.append(embed.title)
                content.append(embed.description)
                content.append(embed.footer.text)
                content.append(embed.author.name)
                for field in embed.fields:
                    content.append(field.name)
                    content.append(field.value)

            # Get rid of empty attributes and turn it into a string
            content = "\n".join(attr for attr in content if attr)

            # Now let's see if there's a regex match
            return bool(regex.search(content))

        def predicate_range(message: Message) -> bool:
            """Check if the message age is between the two limits."""
            return first_limit <= message.created_at <= second_limit

        def predicate_after(message: Message) -> bool:
            """Check if the message is older than the first limit."""
            return message.created_at >= first_limit

        predicates = []
        # Set up the correct predicate
        if bots_only:
            predicates.append(predicate_bots_only)  # Delete messages from bots
        if users:
            predicates.append(predicate_specific_users)  # Delete messages from specific user
        if regex:
            predicates.append(predicate_regex)  # Delete messages that match regex
        # Add up to one of the following:
        if second_limit:
            predicates.append(predicate_range)  # Delete messages in the specified age range
        elif first_limit:
            predicates.append(predicate_after)  # Delete messages older than specific message

        if not predicates:
            return lambda m: True
        if len(predicates) == 1:
            return predicates[0]
        return lambda m: all(pred(m) for pred in predicates)

    async def _delete_invocation(self, ctx: Context) -> None:
        """Delete the command invocation if it's not in a mod channel."""
        if not is_mod_channel(ctx.channel):
            self.mod_log.ignore(Event.message_delete, ctx.message.id)
            try:
                await ctx.message.delete()
            except errors.NotFound:
                # Invocation message has already been deleted
                log.info("Tried to delete invocation message, but it was already deleted.")

    def _get_messages_from_cache(self, traverse: int, to_delete: Predicate) -> tuple[defaultdict[Any, list], list[int]]:
        """Helper function for getting messages from the cache."""
        message_mappings = defaultdict(list)
        message_ids = []
        for message in islice(self.bot.cached_messages, traverse):
            if not self.cleaning:
                # Cleaning was canceled
                return message_mappings, message_ids

            if to_delete(message):
                message_mappings[message.channel].append(message)
                message_ids.append(message.id)

        return message_mappings, message_ids

    async def _get_messages_from_channels(
        self,
        traverse: int,
        channels: Iterable[TextChannel],
        to_delete: Predicate,
        before: Optional[datetime] = None,
        after: Optional[datetime] = None
    ) -> tuple[defaultdict[Any, list], list]:
        message_mappings = defaultdict(list)
        message_ids = []

        for channel in channels:
            async for message in channel.history(limit=traverse, before=before, after=after):

                if not self.cleaning:
                    # Cleaning was canceled, return empty containers.
                    return defaultdict(list), []

                if to_delete(message):
                    message_mappings[message.channel].append(message)
                    message_ids.append(message.id)

        return message_mappings, message_ids

    @staticmethod
    def is_older_than_14d(message: Message) -> bool:
        """
        Precisely checks if message is older than 14 days, bulk deletion limit.

        Inspired by how purge works internally.
        Comparison on message age could possibly be less accurate which in turn would resort in problems
        with message deletion if said messages are very close to the 14d mark.
        """
        two_weeks_old_snowflake = int((time.time() - 14 * 24 * 60 * 60) * 1000.0 - 1420070400000) << 22
        return message.id < two_weeks_old_snowflake

    async def _delete_messages_individually(self, messages: list[Message]) -> list[Message]:
        """Delete each message in the list unless cleaning is cancelled. Return the deleted messages."""
        deleted = []
        for message in messages:
            # Ensure that deletion was not canceled
            if not self.cleaning:
                return deleted
            with contextlib.suppress(NotFound):  # Message doesn't exist or was already deleted
                await message.delete()
                deleted.append(message)
        return deleted

    async def _delete_found(self, message_mappings: dict[TextChannel, list[Message]]) -> list[Message]:
        """
        Delete the detected messages.

        Deletion is made in bulk per channel for messages less than 14d old.
        The function returns the deleted messages.
        If cleaning was cancelled in the middle, return messages already deleted.
        """
        deleted = []
        for channel, messages in message_mappings.items():
            to_delete = []

            delete_old = False
            for current_index, message in enumerate(messages):  # noqa: B007
                if not self.cleaning:
                    # Means that the cleaning was canceled
                    return deleted

                if self.is_older_than_14d(message):
                    # Further messages are too old to be deleted in bulk
                    delete_old = True
                    break

                to_delete.append(message)

                if len(to_delete) == 100:
                    # Only up to 100 messages can be deleted in a bulk
                    with suppress(NotFound):
                        await channel.delete_messages(to_delete)
                    deleted.extend(to_delete)
                    to_delete.clear()

            if not self.cleaning:
                return deleted
            if len(to_delete) > 0:
                # Deleting any leftover messages if there are any
                with suppress(NotFound):
                    await channel.delete_messages(to_delete)
                deleted.extend(to_delete)

            if not self.cleaning:
                return deleted
            if delete_old:
                old_deleted = await self._delete_messages_individually(messages[current_index:])
                deleted.extend(old_deleted)

        return deleted

    async def _modlog_cleaned_messages(self, messages: list[Message], channels: CleanChannels, ctx: Context) -> bool:
        """Log the deleted messages to the modlog. Return True if logging was successful."""
        if not messages:
            # Can't build an embed, nothing to clean!
            await self._send_expiring_message(ctx, ":x: No matching messages could be found.")
            return False

        # Reverse the list to have reverse chronological order
        log_messages = reversed(messages)
        log_url = await self.mod_log.upload_log(log_messages, ctx.author.id)

        # Build the embed and send it
        if channels == "*":
            target_channels = "all channels"
        else:
            target_channels = ", ".join(channel.mention for channel in channels)

        message = (
            f"**{len(messages)}** messages deleted in {target_channels} by "
            f"{ctx.author.mention}\n\n"
            f"A log of the deleted messages can be found [here]({log_url})."
        )

        await self.mod_log.send_log_message(
            icon_url=Icons.message_bulk_delete,
            colour=Colour(Colours.soft_red),
            title="Bulk message delete",
            text=message,
            channel_id=Channels.mod_log,
        )

        return True

    # endregion

    async def _clean_messages(
        self,
        ctx: Context,
        traverse: int,
        channels: Optional[CleanChannels],
        bots_only: bool = False,
        users: Optional[list[User]] = None,
        regex: Optional[re.Pattern] = None,
        first_limit: Optional[CleanLimit] = None,
        second_limit: Optional[CleanLimit] = None,
        use_cache: Optional[bool] = True
    ) -> None:
        """A helper function that does the actual message cleaning."""
        self._validate_input(traverse, channels, bots_only, users, first_limit, second_limit)

        # Are we already performing a clean?
        if self.cleaning:
            await self._send_expiring_message(
                ctx, ":x: Please wait for the currently ongoing clean operation to complete."
            )
            return
        self.cleaning = True

        # Default to using the invoking context's channel or the channel of the message limit(s).
        if not channels:
            # Input was validated - if first_limit is a message, second_limit won't point at a different channel.
            if isinstance(first_limit, Message):
                channels = [first_limit.channel]
            elif isinstance(second_limit, Message):
                channels = [second_limit.channel]
            else:
                channels = [ctx.channel]

        if isinstance(first_limit, Message):
            first_limit = first_limit.created_at
        if isinstance(second_limit, Message):
            second_limit = second_limit.created_at
        if first_limit and second_limit:
            first_limit, second_limit = sorted([first_limit, second_limit])

        # Needs to be called after standardizing the input.
        predicate = self._build_predicate(bots_only, users, regex, first_limit, second_limit)

        # Delete the invocation first
        await self._delete_invocation(ctx)

        if channels == "*" and use_cache:
            message_mappings, message_ids = self._get_messages_from_cache(traverse=traverse, to_delete=predicate)
        else:
            deletion_channels = channels
            if channels == "*":
                deletion_channels = [channel for channel in ctx.guild.channels if isinstance(channel, TextChannel)]
            message_mappings, message_ids = await self._get_messages_from_channels(
                traverse=traverse,
                channels=deletion_channels,
                to_delete=predicate,
                before=second_limit,
                after=first_limit  # Remember first is the earlier datetime.
            )

        if not self.cleaning:
            # Means that the cleaning was canceled
            return

        # Now let's delete the actual messages with purge.
        self.mod_log.ignore(Event.message_delete, *message_ids)
        deleted_messages = await self._delete_found(message_mappings)
        self.cleaning = False

        logged = await self._modlog_cleaned_messages(deleted_messages, channels, ctx)

        if logged and is_mod_channel(ctx.channel):
            with suppress(NotFound):  # Can happen if the invoker deleted their own messages.
                await ctx.message.add_reaction(Emojis.check_mark)

    # region: Commands

    @group(invoke_without_command=True, name="clean", aliases=["clear", "purge"])
    async def clean_group(
        self,
        ctx: Context,
        users: Greedy[User] = None,
        traverse: Optional[int] = None,
        first_limit: Optional[CleanLimit] = None,
        second_limit: Optional[CleanLimit] = None,
        use_cache: Optional[bool] = None,
        bots_only: Optional[bool] = False,
        regex: Optional[Regex] = None,
        *,
        channels: CleanChannels = None  # "Optional" with discord.py silently ignores incorrect input.
    ) -> None:
        """
        Commands for cleaning messages in channels.

        If arguments are provided, will act as a master command from which all subcommands can be derived.

        \u2003• `users`: A series of user mentions, ID's, or names.
        \u2003• `traverse`: The number of messages to look at in each channel. If using the cache, will look at the
        first `traverse` messages in the cache.
        \u2003• `first_limit` and `second_limit`: A message, a duration delta, or an ISO datetime.
        If a message is provided, cleaning will happen in that channel, and channels cannot be provided.
        If a limit is provided, multiple channels cannot be provided.
        If only one of them is provided, acts as `clean until`. If both are provided, acts as `clean between`.
        \u2003• `use_cache`: Whether to use the message cache.
        If not provided, will default to False unless an asterisk is used for the channels.
        \u2003• `bots_only`: Whether to delete only bots. If specified, users cannot be specified.
        \u2003• `regex`: A regex pattern the message must contain to be deleted.
        The pattern must be provided enclosed in backticks.
        If the pattern contains spaces, it still needs to be enclosed in double quotes on top of that.
        \u2003• `channels`: A series of channels to delete in, or an asterisk to delete from all channels.
        """
        if not any([traverse, users, first_limit, second_limit, regex, channels]):
            await ctx.send_help(ctx.command)
            return

        if not traverse:
            if first_limit:
                traverse = CleanMessages.message_limit
            else:
                traverse = DEFAULT_TRAVERSE
        if use_cache is None:
            use_cache = channels == "*"

        await self._clean_messages(
            ctx, traverse, channels, bots_only, users, regex, first_limit, second_limit, use_cache
        )

    @clean_group.command(name="user", aliases=["users"])
    async def clean_user(
        self,
        ctx: Context,
        user: User,
        traverse: Optional[int] = DEFAULT_TRAVERSE,
        use_cache: Optional[bool] = True,
        *,
        channels: CleanChannels = None
    ) -> None:
        """Delete messages posted by the provided user, stop cleaning after traversing `traverse` messages."""
        await self._clean_messages(ctx, traverse, users=[user], channels=channels, use_cache=use_cache)

    @clean_group.command(name="all", aliases=["everything"])
    async def clean_all(
        self,
        ctx: Context,
        traverse: Optional[int] = DEFAULT_TRAVERSE,
        use_cache: Optional[bool] = True,
        *,
        channels: CleanChannels = None
    ) -> None:
        """Delete all messages, regardless of poster, stop cleaning after traversing `traverse` messages."""
        await self._clean_messages(ctx, traverse, channels=channels, use_cache=use_cache)

    @clean_group.command(name="bots", aliases=["bot"])
    async def clean_bots(
        self,
        ctx: Context,
        traverse: Optional[int] = DEFAULT_TRAVERSE,
        use_cache: Optional[bool] = True,
        *,
        channels: CleanChannels = None
    ) -> None:
        """Delete all messages posted by a bot, stop cleaning after traversing `traverse` messages."""
        await self._clean_messages(ctx, traverse, bots_only=True, channels=channels, use_cache=use_cache)

    @clean_group.command(name="regex", aliases=["word", "expression", "pattern"])
    async def clean_regex(
        self,
        ctx: Context,
        regex: Regex,
        traverse: Optional[int] = DEFAULT_TRAVERSE,
        use_cache: Optional[bool] = True,
        *,
        channels: CleanChannels = None
    ) -> None:
        """
        Delete all messages that match a certain regex, stop cleaning after traversing `traverse` messages.

        The pattern must be provided enclosed in backticks.
        If the pattern contains spaces, it still needs to be enclosed in double quotes on top of that.
        For example: `[0-9]`
        """
        await self._clean_messages(ctx, traverse, regex=regex, channels=channels, use_cache=use_cache)

    @clean_group.command(name="until")
    async def clean_until(
        self,
        ctx: Context,
        until: CleanLimit,
        channel: TextChannel = None
    ) -> None:
        """
        Delete all messages until a certain limit.

        A limit can be either a message, and ISO date-time string, or a time delta.
        If a message is specified, `channel` cannot be specified.
        """
        await self._clean_messages(
            ctx,
            CleanMessages.message_limit,
            channels=[channel] if channel else None,
            first_limit=until,
        )

    @clean_group.command(name="between", aliases=["after-until", "from-to"])
    async def clean_between(
        self,
        ctx: Context,
        first_limit: CleanLimit,
        second_limit: CleanLimit,
        channel: TextChannel = None
    ) -> None:
        """
        Delete all messages within range.

        The range is specified through two limits.
        A limit can be either a message, and ISO date-time string, or a time delta.

        If two messages are specified, they both must be in the same channel.
        If a message is specified, `channel` cannot be specified.
        """
        await self._clean_messages(
            ctx,
            CleanMessages.message_limit,
            channels=[channel] if channel else None,
            first_limit=first_limit,
            second_limit=second_limit,
        )

    @clean_group.command(name="stop", aliases=["cancel", "abort"])
    async def clean_cancel(self, ctx: Context) -> None:
        """If there is an ongoing cleaning process, attempt to immediately cancel it."""
        if not self.cleaning:
            message = ":question: There's no cleaning going on."
        else:
            self.cleaning = False
            message = f"{Emojis.check_mark} Clean interrupted."

        await self._send_expiring_message(ctx, message)
        await self._delete_invocation(ctx)

    # endregion

    async def cog_check(self, ctx: Context) -> bool:
        """Only allow moderators to invoke the commands in this cog."""
        return await has_any_role(*MODERATION_ROLES).predicate(ctx)

    async def cog_command_error(self, ctx: Context, error: Exception) -> None:
        """Safely end the cleaning operation on unexpected errors."""
        self.cleaning = False


def setup(bot: Bot) -> None:
    """Load the Clean cog."""
    bot.add_cog(Clean(bot))
