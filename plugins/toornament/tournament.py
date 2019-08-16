import logging
from dataclasses import dataclass, field, fields, asdict, is_dataclass
from datetime import datetime
from enum import IntEnum
from typing import Optional, List, Tuple

import discord
from errbot import BotPlugin, botcmd, Message, arg_botcmd

from backends.discord.discord import DiscordPerson
from clients.toornament_api_client import ToornamentAPIClient

logger = logging.getLogger(__name__)


class MatchStatus(IntEnum):
    PENDING = 1
    ONGOING = 2
    COMPLETED = 3


@dataclass
class BaseDataClass:
    def __post_init__(self):
        """
        Convert all fields of type `dataclass` into an instance of the
        specified data class if the current value is of type dict.

        There's some real sketchy stuff here bro.
        """

        def unpack_values(field_type, val):
            """ unpack dict if field exists for this type """
            return field_type(
                **{
                    k: v
                    for k, v in val.items()
                    if k in {ft.name for ft in fields(field_type)}
                }
            )

        cls = type(self)
        for f in fields(cls):
            is_list_of_dataclass = (
                hasattr(f.type, "_name")
                and f.type._name == "List"
                and is_dataclass(next(iter(f.type.__args__), None))
            )
            # if the field is not a dataclass, OR is not a List of dataclasses
            if not is_dataclass(f.type) and not is_list_of_dataclass:
                continue

            value = getattr(self, f.name)

            if isinstance(value, dict):
                new_value = unpack_values(f.type, value)
                setattr(self, f.name, new_value)
            elif isinstance(value, list):
                new_value = []
                for v in value:
                    if isinstance(v, dict):
                        new_value.append(unpack_values(f.type.__args__[0], v))
                setattr(self, f.name, new_value)

    @classmethod
    def from_dict(cls, values: dict):
        """ Ignore dict keys if they're not a field of the dataclass """
        class_fields = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in values.items() if k in class_fields})

    def to_dict(self):
        return asdict(self)


@dataclass
class ScoreSubmission(BaseDataClass):
    match_name: str
    team_name: str
    screenshot_links: List[str] = field(default_factory=list)
    position: Optional[int] = field(default=None)
    eliminations: Optional[int] = field(default=None)
    last_updated: str = field(default=datetime.now().strftime("%m/%d/%Y %H:%M:%S"))

    def get_screenshots(self) -> str:
        return ", ".join(f"<{u}>" for u in self.screenshot_links)

    def show_card(self) -> dict:
        return {
            "title": self.team_name,
            "fields": (
                ("Match", self.match_name),
                ("Position", self.position),
                ("Eliminations", self.eliminations),
                ("Last update", self.last_updated),
                ("Screenshots", self.get_screenshots()),
            ),
            "color": "grey",
        }

    def __str__(self):
        return (
            f"```Position:      {self.position}\n"
            f"Eliminations:  {self.eliminations}\n"
            f"Last update:   {self.last_updated}````" + "\n".join(self.screenshot_links)
        )


@dataclass
class Player(BaseDataClass):
    name: str
    custom_fields: List[str] = field(default_factory=list)
    email: Optional[str] = field(default=None)


@dataclass
class Team(BaseDataClass):
    id: int
    name: str
    custom_fields: List[str] = field(default_factory=list)
    lineup: List[Player] = field(default_factory=list)
    captain: Optional[str] = field(default=None)
    checked_in: Optional[bool] = field(default=None)
    score_submissions: List[ScoreSubmission] = field(default_factory=list)

    def find_submission_by_match(self, match_name: str) -> Optional[ScoreSubmission]:
        for s in self.score_submissions:
            if s.match_name == match_name:
                return s
        return None


@dataclass
class Match(BaseDataClass):
    name: str
    created_by: str
    password: Optional[str] = field(default=None)
    status: MatchStatus = field(default=MatchStatus.PENDING)
    teams_ready: List[Team] = field(default_factory=list)

    def find_team_by_name(self, team_name: str) -> Optional[Team]:
        for team in self.teams_ready:
            if team.name == team_name:
                return team
        return None

    def get_match_scores(self, tournament_teams: List[Team]) -> List[ScoreSubmission]:
        submissions = []
        for team_r in self.teams_ready:
            team = next((t for t in tournament_teams if t.id == team_r.id), None)
            if not team:
                raise Exception("Sync unmatch between match teams and tournament")
            submission = team.find_submission_by_match(self.name)
            if submission:
                submissions.append(submission)

        return submissions

    def __str__(self):
        return (
            f"**Status:** {self.status.name}\n"
            f"**Teams Ready:** {len(self.teams_ready)}"
            f"**Created by:** {self.created_by}\n",
        )


@dataclass
class ToornamentInfo(BaseDataClass):
    id: int
    country: str
    discipline: str
    location: str
    name: str
    scheduled_date_end: str
    scheduled_date_start: str
    size: int
    status: str
    team_max_size: int
    team_min_size: int
    rule: Optional[str] = field(default=None)
    prize: Optional[str] = field(default=None)
    platforms: List[str] = field(default_factory=list)


@dataclass
class Tournament(BaseDataClass):
    id: int
    info: ToornamentInfo = field(default=None)
    administrator_roles: List[str] = field(default_factory=list)
    channels: List[str] = field(default_factory=list)
    matches: List[Match] = field(default_factory=list)
    teams: List[Team] = field(default_factory=list)
    url: Optional[str] = field(default=None)

    def count_registered_participants(self) -> int:
        return sum([p.captain is not None for p in self.teams])

    def find_match_by_name(self, name: str) -> Optional[Match]:
        for m in self.matches:
            if m.name == name:
                return m
        return None

    def find_team_by_captain(self, captain_name: str) -> Optional[Team]:
        for p in self.teams:
            if p.captain == captain_name:
                return p
        return None

    def find_team_by_id(self, participant_id: int) -> Optional[Team]:
        for p in self.teams:
            if p.id == participant_id:
                return p
        return None

    def find_team_by_name(self, team_name: str) -> Optional[Team]:
        for p in self.teams:
            if p.name == team_name:
                return p
        return None


class TournamentBotPlugin(BotPlugin):
    """
    TODO:
    - Optional tournament ID if wrote in right channel
    """

    toornament_api_client = None

    def activate(self):
        """ Triggers on plugin activation """
        super(TournamentBotPlugin, self).activate()
        self.toornament_api_client = ToornamentAPIClient()
        if "tournaments" not in self:
            self["tournaments"] = {}

    def callback_attachment(self, msg: Message, discord_msg: discord.Message):
        if (
            hasattr(msg.to, "fullname")
            and msg.to.fullname == self.bot_identifier.fullname
        ):
            msg_parts = msg.body.strip().split(" ")
            if msg_parts and msg_parts[0] == "submit":
                if len(msg_parts) == 6:
                    _, match_name, _, position, _, eliminations = msg_parts

                    if not position.isdigit() or not eliminations.isdigit():
                        self.send(
                            msg.frm,
                            "Invalid entries for position and eliminations. "
                            "A number was expected",
                        )
                        return

                    with self.mutable("tournaments") as tournaments:
                        team, tournament = self._find_captain_team(
                            msg.frm.fullname, tournaments
                        )
                        if not team:
                            self.send(msg.frm, "You are not the captain of a team.")
                            return

                        # update previous score submission
                        score = team.find_submission_by_match(match_name)
                        if score:
                            score.screenshot_links = [
                                a.url for a in discord_msg.attachments
                            ]
                            score.position = int(position)
                            score.eliminations = int(eliminations)
                            score.last_updated = datetime.now().strftime(
                                "%m/%d/%Y %H:%M:%S"
                            )
                            self.send(msg.frm, "Score successfully updated!")
                            self.send_card(in_reply_to=msg, **score.show_card())
                            # Save tournament changes to db
                            tournaments.update({tournament.id: tournament.to_dict()})
                            return

                        if not tournament.find_match_by_name(match_name):
                            # We also allow teams that haven't joined the match to
                            # submit their score in case something happened, we still
                            # want them to enjoy this feature.
                            self.send(
                                msg.frm,
                                f"There is no match with the name `{match_name}` for "
                                f"the `{tournament.info.name}` tournament. Can't submit "
                                f"score.",
                            )
                            return

                        score = ScoreSubmission(
                            match_name=match_name,
                            team_name=team.name,
                            screenshot_links=[a.url for a in discord_msg.attachments],
                            position=int(position),
                            eliminations=int(eliminations),
                        )
                        team.score_submissions.append(score)
                        self.send_card(in_reply_to=msg, **score.show_card())
                        # Save tournament changes to db
                        tournaments.update({tournament.id: tournament.to_dict()})
                        return

                else:
                    self.send(
                        msg.frm,
                        """
                        Looks like you're trying to submit your score!
                        \nYour screenshot must be followed with the following
                        information in text:
                        `submit MATCH_NAME position X eliminations Y`
                        \nFor example:
                        `submit game_1 position 2 eliminations 5` to submit a score
                        where your position is `2`nd and number of eliminations `5`
                        for the match named `game_1`.
                        \n\nSubmitting a different score than what is displayed on your
                        screenshot is forbidden and might result in a sanction
                        """,
                    )
            else:
                self.send(
                    msg.frm,
                    """
                    Wow! Thank you so much for this beautiful attachment,
                    but unfortunately, I'm unsure what I'm supposed to do with that.
                    Are you trying to submit a match score screenshot? \n\n
                    Try to send me this again with the text
                    `submit MATCH_NAME position X eliminations Y`.\nFor example:
                    `submit game_1 position 2 eliminations 5` to submit a score
                    where your position is `2`nd and number of eliminations `5`
                    for the match named `game_1`.
                    """,
                )

    @arg_botcmd("tournament_id", type=int)
    def show_tournament(self, msg, tournament_id):
        if tournament_id not in self["tournaments"]:
            return "Tournament not found."
        tournament = Tournament.from_dict(self["tournaments"][tournament_id])
        self._show_tournament(msg, tournament)

    @botcmd
    def show_tournaments(self, msg, args):
        for tournament in self["tournaments"].values():
            tournament = Tournament.from_dict(tournament)
            self._show_tournament(msg, tournament)

    @botcmd
    def show_status(self, msg, args):
        team, tournament = self._find_captain_team(msg.frm.fullname, self["tournaments"])
        if not team:
            return "You are not the captain of a team."

        self.send_card(
            title=f"{tournament.info.name} " f"({tournament.id})",
            body=f"**Team ID:** {team.id}\n"
            f"**Team Name:** {team.name}\n"
            f"**Team Captain:** {team.captain}\n"
            f"**Team Players:** " + ", ".join(pl.name for pl in team.lineup),
            summary=f"To unregister, type:\n"
            f"!unregister "
            f"{tournament.id} "
            f"{team.name}",
            in_reply_to=msg,
        )

    @arg_botcmd("tournament_id", type=int)
    def show_teams(self, msg: Message, tournament_id):
        if tournament_id not in self["tournaments"]:
            return "Tournament doesn't exists"

        tournament = Tournament.from_dict(self["tournaments"][tournament_id])
        participants = sorted(tournament.teams, key=lambda k: getattr(k, "name"))
        participants = [p for p in participants if p.captain is not None]
        if len(participants) == 0:
            return "No team registered for this tournament"

        count = 1
        participants_chunks = chunks(participants, 100)
        for i, chunk in enumerate(participants_chunks):
            team_names = ""
            for team in chunk:
                team_names += f"{count}. {team.name}\n"
                count += 1

            self.send_card(
                title=f"{tournament.info.name} Registered Participants"
                f"({i + 1}/{len(participants_chunks)})",
                body=(
                    f"Number of teams: "
                    f"{len(tournament.teams)} \n"
                    f"Number of registrations: "
                    f"{tournament.count_registered_participants()}\n\n"
                    f"{team_names}"
                ),
                color="green",
                in_reply_to=msg,
            )

    @arg_botcmd("tournament_id", type=int)
    def show_missing_teams(self, msg: Message, tournament_id):
        if tournament_id not in self["tournaments"]:
            return "Tournament doesn't exists"

        tournament = Tournament.from_dict(self["tournaments"][tournament_id])
        participants = sorted(tournament.teams, key=lambda k: getattr(k, "name"))
        participants = [p for p in participants if p.captain is None]
        if len(participants) == 0:
            return "Every team registered for this tournament"

        missing_participants_count = (
            len(tournament.teams) - tournament.count_registered_participants()
        )
        count = 1
        participants_chunks = chunks(participants, 100)
        for i, chunk in enumerate(participants_chunks):
            team_names = ""
            for team in chunk:
                team_names += f"{count}. {team.name}\n"
                count += 1

            self.send_card(
                title=f"{tournament.info.name} Missing Registrations "
                f"({i + 1}/{len(participants_chunks)})",
                body=(
                    f"Number of teams: "
                    f"{len(tournament.teams)} \n"
                    f"Number of missing registrations: "
                    f"{missing_participants_count}\n\n"
                    f"{team_names}"
                ),
                color="green",
                in_reply_to=msg,
            )

    """
    Tournament Management
    """

    @arg_botcmd(
        "--channels",
        type=str,
        nargs="+",
        default=[],
        help="Default channels to use some bot features",
    )
    @arg_botcmd("--roles", type=str, nargs="+", default=[], help="Administrator roles")
    @arg_botcmd("tournament_id", type=int)
    def add_tournament(self, msg: Message, tournament_id: int, channels, roles):
        with self.mutable("tournaments") as tournaments:
            if tournament_id in tournaments:
                return "Tournament already added."

            tournament = Tournament(
                id=tournament_id,
                url=f"https://www.toornament.com/en_US/tournaments/"
                f"{tournament_id}/information",
            )

            for channel in channels:
                if not self.query_room(channel):
                    return "Invalid channel name"
                tournament.channels.append(channel)

            for role in roles:
                if not self._bot.find_role(role):
                    return "Invalid role name"
                tournament.administrator_roles.append(role)

            # Toornament info
            toornament_info = self.toornament_api_client.get_tournament(tournament_id)
            if not toornament_info:
                return "Tournament not found."
            tournament.info = ToornamentInfo.from_dict(toornament_info)

            # Toornament participants
            toornament_participants = self.toornament_api_client.get_participants(
                tournament_id
            )
            tournament.teams = [Team.from_dict(p) for p in toornament_participants]

            # Add to tournaments db
            tournaments.update({tournament_id: tournament.to_dict()})
            self._show_tournament(msg, tournament)
            self.send(msg.frm, f"Tournament `{tournament.info.name}` successfully added")

    @arg_botcmd("tournament_id", type=int)
    def refresh_tournament(self, msg: Message, tournament_id: int):
        """ Refresh a tournament's information """
        if tournament_id not in self["tournaments"]:
            return "Tournament not found"

        with self.mutable("tournaments") as tournaments:
            tournament = Tournament.from_dict(tournaments[tournament_id])

            # update data fetched on Toornament
            info = self.toornament_api_client.get_tournament(tournament_id)
            if not info:
                return "Tournament not found on Toornament."

            # Override current tournament info
            tournament.info = ToornamentInfo.from_dict(info)

            # update participants list
            participants = self.toornament_api_client.get_participants(tournament_id)
            for participant in participants:
                team = tournament.find_team_by_id(participant["id"])
                if team:
                    # Update participant name and lineup
                    team.name = participant["name"]
                    team.lineup = [Player(**pl) for pl in participant.get("lineup", [])]
                    team.checked_in = participant.get("checked_in")
                else:
                    # Add new participant
                    tournament.teams.append(Team.from_dict(participant))

            # Save tournament changes to db
            tournaments.update({tournament_id: tournament.to_dict()})
            return f"Tournament {tournament.info.name} successfully refreshed."

    @arg_botcmd("discord_channels", type=str, nargs="+")
    @arg_botcmd("tournament_id", type=int)
    def add_channels(self, msg, tournament_id, discord_channels):
        """ Associate a Discord role to a tournament """
        if tournament_id not in self["tournaments"]:
            return "Tournament not found."

        for discord_channel in discord_channels:
            if not self.query_room(discord_channel):
                return "Invalid channel name"

        with self.mutable("tournaments") as tournaments:
            tournament = Tournament.from_dict(tournaments[tournament_id])

            for channel in discord_channels:
                if channel in tournament.channels:
                    return (
                        f"Channel `{channel}` is already set for the tournament "
                        f"`{tournament.info.name}`"
                    )
                tournament.channels.append(channel)

            # Save tournament changes to db
            tournaments.update({tournament_id: tournament.to_dict()})
            return f"Channels successfully added to the tournament"

    @arg_botcmd("discord_channels", type=str, nargs="+")
    @arg_botcmd("tournament_id", type=int)
    def remove_channels(self, msg, tournament_id, discord_channels):
        """ Associate a Discord role to a tournament """
        if tournament_id not in self["tournaments"]:
            return "Tournament not found."

        with self.mutable("tournaments") as tournaments:
            tournament = Tournament.from_dict(tournaments[tournament_id])
            for channel in discord_channels:
                if channel not in tournament.channels:
                    return (
                        f"Channel `{channel}` doesn't exists for the tournament "
                        f"`{tournament.info.name}`"
                    )
                tournament.channels.remove(channel)

            # Save tournament changes to db
            tournaments.update({tournament_id: tournament.to_dict()})
            return f"Channels successfully removed from the tournament"

    @arg_botcmd("discord_roles", type=str, nargs="+")
    @arg_botcmd("tournament_id", type=int)
    def add_roles(self, msg, tournament_id, discord_roles):
        """ Associate a Discord role to a tournament """
        if tournament_id not in self["tournaments"]:
            return "Tournament not found."

        for discord_role in discord_roles:
            if not self._bot.find_role(discord_role):
                return f"Role `{discord_role}` not found"

        with self.mutable("tournaments") as tournaments:
            tournament = Tournament.from_dict(tournaments[tournament_id])

            for role in discord_roles:
                if role in tournament.administrator_roles:
                    return (
                        f"Role `{role}` is already a tournament administrator role "
                        f"of `{tournament.info.name}`"
                    )
                tournament.administrator_roles.append(role)

            # Save tournament changes to db
            tournaments.update({tournament_id: tournament.to_dict()})
            return f"Roles successfully added to the tournament `{tournament.info.name}`"

    @arg_botcmd("discord_roles", type=str, nargs="+")
    @arg_botcmd("tournament_id", type=int)
    def remove_role(self, msg, tournament_id, discord_roles):
        """ Remove an associated Discord role from a tournament """
        if tournament_id not in self["tournaments"]:
            return "Tournament not found."

        with self.mutable("tournaments") as tournaments:
            tournament = Tournament.from_dict(tournaments[tournament_id])

            for role in discord_roles:
                if role.name not in tournament.administrator_roles:
                    return f"Role `{role.name}` is not a role of `{tournament.info.name}`"
                tournament.administrator_roles.remove(role.name)

            # Save tournament changes to db
            tournaments.update({tournament_id: tournament.to_dict()})
            return (
                f"Roles successfully removed from the tournament `{tournament.info.name}`"
            )

    @arg_botcmd("team_name", type=str, nargs="+")
    @arg_botcmd("tournament_id", type=int)
    def reset_team(self, msg: Message, tournament_id: int, team_name: List[str]):
        if tournament_id not in self["tournaments"]:
            return "Tournament not found"

        team_name = " ".join(team_name)
        with self.mutable("tournaments") as tournaments:
            tournament = Tournament.from_dict(tournaments[tournament_id])
            team = tournament.find_team_by_name(team_name)
            if not team:
                return (
                    f"Team {team_name} not found in the tournament {tournament.info.name}"
                    f". Try to refresh the tournament instead."
                )

            participant = self.toornament_api_client.get_participant(
                tournament_id, team.id
            )
            if not participant:
                return "Could not retrieve participant's info"

            team.captain = None
            team.lineup = [Player.from_dict(pl) for pl in participant["lineup"]]
            team.custom_fields = participant["custom_fields"]
            team.checked_in = participant["checked_in"]

            # Save tournament changes to db
            tournaments.update({tournament_id: tournament.to_dict()})
            return f"Team {team_name} successfully updated."

    @arg_botcmd("team_name", type=str, nargs="+")
    @arg_botcmd("tournament_id", type=int)
    def remove_team(self, msg: Message, tournament_id: int, team_name: List[str]):
        if tournament_id not in self["tournaments"]:
            return "Tournament not found"

        team_name = " ".join(team_name)
        with self.mutable("tournaments") as tournaments:
            tournament = Tournament.from_dict(tournaments[tournament_id])
            team = tournament.find_team_by_name(team_name)
            if not team:
                return (
                    f"Team {team_name} not found in the tournament {tournament.info.name}"
                )

            tournament.teams.remove(team)

            # Save tournament changes to db
            tournaments.update({tournament_id: tournament.to_dict()})
            return f"Team {team_name} successfully removed."

    """
    Registration Commands
    """

    @arg_botcmd("team_name", type=str, nargs="+")
    @arg_botcmd("tournament_id", type=int)
    def register(self, msg: Message, tournament_id: int, team_name: List[str]):
        """ Todo: handle duplicates """
        if tournament_id not in self["tournaments"]:
            return "Tournament not found"

        team_name = " ".join(team_name)
        with self.mutable("tournaments") as tournaments:
            team, tournament = self._find_captain_team(msg.frm.fullname, tournaments)
            if team:
                if team.name == team_name:
                    return "You are already this team"
                return (
                    "You can't be the captain of more than one team.\n"
                    f"You are currently the captain of the team `{team.name}` for the "
                    f"tournament {tournament.name}"
                )

            tournament = Tournament.from_dict(tournaments[tournament_id])
            team = tournament.find_team_by_name(team_name)
            if not team:
                return (
                    f"Team {team_name} not found in the tournament {tournament.info.name}"
                )

            if team.captain is not None:
                return (
                    f"Team `{team_name}` is already registered with the "
                    f"captain `{team.captain}`"
                )

            team.captain = msg.frm.fullname

            # Save tournament changes to db
            tournaments.update({tournament_id: tournament.to_dict()})
            return (
                f"You are now the captain of the team `{team_name}` and "
                f"successfully checked-in for this tournament!"
            )

    @arg_botcmd("team_name", type=str, nargs="+")
    def unregister(self, msg: Message, team_name: List[str]):
        """ TODO: Swap captains instead """

        team_name = " ".join(team_name)
        with self.mutable("tournaments") as tournaments:
            team, tournament = self._find_captain_team(msg.frm.fullname, tournaments)
            if not team:
                return "You are not the captain of a team."

            if team.name != team_name:
                return (
                    f"Tournament's team name `{team.name}` is different from "
                    f"your entry `{team_name}`. To confirm your unregistration, please "
                    f"type the right team name."
                )

            team.captain = None

            # Save tournament changes to db
            tournaments.update({tournament.id: tournament.to_dict()})
            return f"You are no longer the captain of the team `{team_name}`."

    """
    Matches Commands
    """

    @arg_botcmd("match_name", type=str, help="Name of the match to join")
    def join(self, msg: Message, match_name: str):
        with self.mutable("tournaments") as tournaments:
            team, tournament = self._find_captain_team(msg.frm.fullname, tournaments)
            if not team:
                return "You are not a team captain."

            match = tournament.find_match_by_name(match_name)
            if not match:
                return "Match not found"

            if match.find_team_by_name(team.name):
                return f"Team `{team.name}` is already ready for this match"

            if match.status != MatchStatus.PENDING:
                return f"Can't join match with status {match.status.name}"

            match.teams_ready.append(team)

            # Save tournament changes to db
            tournaments.update({tournament.id: tournament.to_dict()})
            self.send(
                msg.frm, f"Team `{team.name}` is now ready for the match {match.name}!"
            )
            self._show_match(msg, tournament, match, public=False)

    @arg_botcmd("tournament_id", type=int)
    def show_matches(self, msg, tournament_id):
        if tournament_id not in self["tournaments"]:
            return "Tournament not found"

        tournament = Tournament.from_dict(self["tournaments"][tournament_id])
        self.send_card(
            title=tournament.info.name,
            fields=(*((str(match.name), str(match)) for match in tournament.matches),),
            in_reply_to=msg,
        )

    @arg_botcmd("match_name", type=str, help="Name of the match to join")
    @arg_botcmd("tournament_id", type=int)
    def show_match_scores(self, msg, tournament_id: int, match_name: str):
        if not self._is_tournament_admin(msg.frm, tournament_id):
            return "You are not allowed to perform this action"

        if tournament_id not in self["tournaments"]:
            return "Tournament not found"

        tournament = Tournament.from_dict(self["tournaments"][tournament_id])
        match = tournament.find_match_by_name(match_name)
        if match.status != MatchStatus.COMPLETED:
            return f"Can't see match score if the status is not COMPLETED"

        match_scores = match.get_match_scores(tournament.teams)
        match_scores = sorted(match_scores, key=lambda k: getattr(k, "team_name"))
        match_scores_chunk = chunks(match_scores, 75)

        for i, chunk in enumerate(match_scores_chunk):
            self.send_card(
                title=f"{match.name} @ {tournament.info.name} Score Submissions "
                f"({i + 1}/{len(match_scores_chunk)})",
                fields=((str(score.team_name), str(score)) for score in chunk),
                in_reply_to=msg,
                color="white",
            )

    @arg_botcmd("password", type=str, help="Match lobby password")
    @arg_botcmd("match_name", type=str)
    @arg_botcmd("tournament_id", type=int)
    def create_match(self, msg, tournament_id, match_name, password):
        if not self._is_tournament_admin(msg.frm, tournament_id):
            return "You are not allowed to perform this action"

        if tournament_id not in self["tournaments"]:
            return "Tournament not found"

        with self.mutable("tournaments") as tournaments:
            tournament = Tournament.from_dict(tournaments[tournament_id])
            match = tournament.find_match_by_name(match_name)
            if match:
                return "Match name already exists"

            match = Match(name=match_name, created_by=msg.frm.fullname, password=password)
            tournament.matches.append(match)

            # Save tournament changes to db
            tournaments.update({tournament_id: tournament.to_dict()})
            self.send(msg.frm, f"Match `{match_name}` successfully created.")
            self._show_match(msg, tournament, match, public=False)

    @arg_botcmd("match_name", type=str, help="Name of the match to join")
    @arg_botcmd("tournament_id", type=int)
    def start_match(self, msg, tournament_id: int, match_name: str):
        if not self._is_tournament_admin(msg.frm, tournament_id):
            return "You are not allowed to perform this action"

        if tournament_id not in self["tournaments"]:
            return "Tournament not found"

        with self.mutable("tournaments") as tournaments:
            tournament = Tournament.from_dict(tournaments[tournament_id])
            match = tournament.find_match_by_name(match_name)
            if not match:
                return "Match not found"
            if match.status != MatchStatus.PENDING:
                return f"Can't start match with status {match.status.name}"

            for channel in tournament.channels:
                room = self.query_room(channel)
                self.send(room, f"The match `{match_name}` will start in ~30 seconds!")

            for team in match.teams_ready:
                captain_user = self.build_identifier(team.captain)
                self.send(
                    captain_user,
                    f"The match `{match_name}` for the team `{team.name}` "
                    f"will start in ~30 seconds!",
                )

            match.status = MatchStatus.ONGOING

            # Save tournament changes to db
            tournaments.update({tournament_id: tournament.to_dict()})
            self.send(msg.frm, f"Match `{match_name}` status set to ONGOING.")

    @arg_botcmd("match_name", type=str, help="Name of the match to join")
    @arg_botcmd("tournament_id", type=int)
    def end_match(self, msg, tournament_id: int, match_name: str):
        if not self._is_tournament_admin(msg.frm, tournament_id):
            return "You are not allowed to perform this action"

        if tournament_id not in self["tournaments"]:
            return "Tournament not found"

        with self.mutable("tournaments") as tournaments:
            tournament = Tournament.from_dict(tournaments[tournament_id])
            match = tournament.find_match_by_name(match_name)
            if not match:
                return "Match not found"
            if match.status != MatchStatus.ONGOING:
                return f"Can't end match with status `{match.status.name}`"

            match.status = MatchStatus.COMPLETED

            # Save tournament changes to db
            tournaments.update({tournament_id: tournament.to_dict()})
            return self.send(msg.frm, f"Match `{match_name}` status set to COMPLETED.")

    """
    Utils
    """

    def _show_match(self, msg, tournament: Tournament, match: Match, public=True):
        additional_fields = ()
        if not public:
            additional_fields = ("Password", f"{match.password}\n")

        self.send_card(
            title=f"{match.name} @ {tournament.info.name}",
            fields=(
                ("Status", f"{match.status.name}\n"),
                (
                    "Teams Ready",
                    f"{len(match.teams_ready)}/"
                    f"{tournament.count_registered_participants()}\n",
                ),
                ("Created by", f"{match.created_by}\n"),
                additional_fields if additional_fields else (".", "."),
            ),
            in_reply_to=msg,
        )

    def _show_tournament(self, msg, tournament: Tournament):
        team_status_text = "**You are not the captain of a team in this tournament*"
        team = tournament.find_team_by_captain(msg.frm.fullname)
        if team is not None:
            team_players = ", ".join(pl.name for pl in team.lineup) or None
            team_status_text = (
                f"**Team Name:** {team.name}\n" f"**Team Players:** {team_players}"
            )

        administrators = []
        for admin_role in tournament.administrator_roles:
            administrators.extend(self._bot.get_role_members(admin_role))
        admins = ", ".join(administrators)

        self.send_card(
            title=tournament.info.name,
            link=tournament.url,
            body=f"{tournament.url}\n\n{team_status_text}",
            summary=f"Tournament Administrators:\t{admins}",
            fields=(
                ("Tournament ID", str(tournament.info.id)),
                ("Participants", str(len(tournament.teams))),
                ("Active Captains", str(tournament.count_registered_participants())),
                ("Default channels", "\n".join(tournament.channels) or None),
                ("Game", tournament.info.discipline),
            ),
            color="green" if team else "red",
            in_reply_to=msg,
        )

    def _is_tournament_admin(self, user: DiscordPerson, tournament_id) -> bool:
        if tournament_id not in self["tournaments"]:
            logger.error("Tournament not found")
            return False

        tournament = Tournament.from_dict(self["tournaments"][tournament_id])
        # is superuser
        if user.fullname in self.bot_config.BOT_ADMINS:
            return True

        return any(user.has_guild_role(r) for r in tournament.administrator_roles)

    @staticmethod
    def _find_captain_team(
        username: str, tournaments: dict
    ) -> Tuple[Optional[Team], Optional[Tournament]]:
        for t in tournaments.values():
            tournament = Tournament.from_dict(t)
            team = tournament.find_team_by_captain(username)
            if team:
                return team, tournament
        return None, None


def chunks(l, n):
    """Yield successive n-sized chunks from l."""
    return list(l[i : i + n] for i in range(0, len(l), n))
