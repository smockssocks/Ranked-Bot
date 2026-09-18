"""Import every model so Base.metadata knows all tables."""
from bot.models.player import Player  # noqa: F401
from bot.models.rating import PlayerRating, RoleBaseline  # noqa: F401
from bot.models.game import Game, GameParticipant  # noqa: F401
from bot.models.lobby import Lobby, LobbyPlayer  # noqa: F401
from bot.models.smurf import SmurfFlag  # noqa: F401
from bot.models.chat import ChatMessage, ChatMemory, ChatUsage  # noqa: F401
from bot.models.settings import GuildSetting  # noqa: F401
