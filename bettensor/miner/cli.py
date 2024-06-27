import argparse
import datetime
import json
import signal
import sqlite3
import traceback
import uuid
import pytz
import subprocess
import bittensor as bt
import rich
import prompt_toolkit
from rich.console import Console
from rich.table import Table
from prompt_toolkit.application import Application
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import Layout
from prompt_toolkit.widgets import Frame, TextArea, Label
from prompt_toolkit.styles import Style
from prompt_toolkit.application import Application as PTApplication
from argparse import ArgumentParser
import time  # Import time module for handling timeout
import threading  # Import threading for non-blocking delay
from prompt_toolkit.layout.containers import Window, HSplit
import logging
import os

log_dir = "./logs"
if not os.path.exists(log_dir):
    os.makedirs(log_dir)

logging.basicConfig(
    filename="./logs/cli_errors.log",
    level=logging.DEBUG,
    format="%(asctime)s %(levelname)s %(message)s",
)


global_style = Style.from_dict(
    {
        "text-area": "fg:green",
        "frame": "fg:green",
        "label": "fg:green",
        "wager-input": "fg:green",
    }
)


class Application:
    def __init__(self):
        parser = ArgumentParser()
        # User must specify by UID.
        parser.add_argument("--uid", type=str, default=None, help="UID of miner")
        args = parser.parse_args()

        if args.uid:
            self.miner_uid = args.uid
            with open("data/miner_env.txt", "r") as f:
                for line in f:
                    parts = line.strip().split(", ")
                    if f"UID={self.miner_uid}" in parts[0]:
                        self.miner_uid = parts[0].split("=")[1]
                        self.db_path = parts[1].split("=")[1]
                        self.miner_hotkey = parts[2].split("=")[1]
                        break
        else:
            print("No UID specified, choosing first miner in list")
            with open("data/miner_env.txt", "r") as f:
                line = f.readline().strip()
                parts = line.split(", ")
                self.miner_uid = parts[0].split("=")[1]
                self.db_path = parts[1].split("=")[1]
                self.miner_hotkey = parts[2].split("=")[1]

        print(
            f"Miner UID: {self.miner_uid}, Miner Hotkey: {self.miner_hotkey}, DB Path: {self.db_path}"
        )

        self.db, self.cursor = get_database(self.db_path)
        self.predictions = get_predictions(self.cursor)
        self.games = get_game_data(self.cursor)
        self.miner_stats = get_miner_stats(self.cursor, self.miner_uid)
        
        if self.miner_stats is None:
            print(f"No miner stats found for UID: {self.miner_uid}. Initializing with default values.")
            self.miner_stats = {
                "miner_uid": self.miner_uid,
                "miner_hotkey": self.miner_hotkey,
                "miner_cash": 1000,  # Default starting cash
                "miner_status": "active",
                "miner_last_prediction_date": None,
                "miner_lifetime_earnings": 0,
                "miner_lifetime_wager": 0,
                "miner_lifetime_predictions": 0,
                "miner_lifetime_wins": 0,
                "miner_lifetime_losses": 0,
                "miner_win_loss_ratio": 0,
                "miner_rank": 0,
                "miner_current_incentive": 0,
            }
            # Insert these default stats into the database
            columns = ", ".join(self.miner_stats.keys())
            placeholders = ", ".join("?" * len(self.miner_stats))
            values = tuple(self.miner_stats.values())
            self.cursor.execute(f"INSERT INTO miner_stats ({columns}) VALUES ({placeholders})", values)
            self.db.commit()

        self.active_games = {}
        self.unsubmitted_predictions = {}
        self.miner_cash = self.miner_stats["miner_cash"]
        self.current_view = MainMenu(self)  # Initialize current_view first
        root_container = self.current_view.box  # Use the box directly
        self.layout = Layout(root_container)
        self.style = global_style  # Apply the global style
        self.check_db_init()

    def reload_data(self):
        self.predictions = get_predictions(self.cursor)
        self.games = get_game_data(self.cursor)

    def change_view(self, new_view):
        self.current_view = new_view
        self.layout.container = new_view.box

    def run(self):
        self.app = PTApplication(
            layout=self.layout,
            key_bindings=bindings,
            full_screen=True,
            style=self.style,  # Apply the global style
        )
        self.app.custom_app = self
        self.app.run()

    def check_db_init(self):
        try:
            self.cursor.execute("SELECT * FROM predictions")
        except Exception as e:
            print(e)
            print("Database not initialized properly, restart your miner first")

        """ miner_stats = {
        "miner_hotkey": "1234567890",
        "miner_coldkey": "1234567890",
        "miner_cash": 750,
        "miner_status": "active",
        "miner_last_wager": "2024-06-08 12:00:00",
        "miner_lifetime_earnings": 2756,
        "miner_lifetime_wins": 6,
        "miner_lifetime_losses": 3,
        "miner_win_loss_ratio": 0.66666666666666666,
        "miner_rank": 1,
        "last_reward": 1.5
        """


class InteractiveTable:
    """
    Base class for interactive tables
    """

    def __init__(self, app):
        self.app = app
        self.text_area = TextArea(
            focusable=True,
            read_only=True,
            width=prompt_toolkit.layout.Dimension(preferred=70),
            height=prompt_toolkit.layout.Dimension(
                preferred=20
            ),  # Use Dimension for height
        )
        self.frame = Frame(self.text_area, style="class:frame")
        self.box = HSplit([self.frame])  # Use HSplit to wrap Frame
        self.selected_index = 0
        self.options = []

    def update_text_area(self):
        lines = [
            f"> {option}" if i == self.selected_index else f"  {option}"
            for i, option in enumerate(self.options)
        ]
        self.text_area.text = "\n".join(lines)

    def handle_enter(self):
        pass  # To be overridden

    def move_up(self):
        if self.selected_index > 0:
            self.selected_index -= 1
        self.update_text_area()

    def move_down(self):
        if self.selected_index < len(self.options) - 1:
            self.selected_index += 1
        self.update_text_area()


class MainMenu(InteractiveTable):
    """
    Main menu for the miner CLI - 1st level menu

    """

    def __init__(self, app):
        super().__init__(app)

        self.header = Label(
            " BetTensor Miner Main Menu", style="bold"
        )  # Added leading space
        self.options = [
            "View Submitted Predictions",
            "View Games and Make Predictions",
            "Quit",
        ]
        self.update_text_area()

    def update_text_area(self):
        header_text = self.header.text
        divider = "-" * len(header_text)

        # Miner stats
        miner_stats_text = (
            f" Miner Hotkey: {self.app.miner_stats['miner_hotkey']}\n"
            f" Miner Coldkey: {self.app.miner_stats['miner_coldkey']}\n"
            f" Miner Rank: {self.app.miner_stats['miner_rank']}\n"
            f" Miner Cash: {self.app.miner_stats['miner_cash']}\n"
            f" Current Incentive: {self.app.miner_stats['miner_current_incentive']} τ per day\n"
            f" Last Prediction: {self.app.miner_stats['miner_last_prediction_date']}\n"
            f" Lifetime Earnings: ${self.app.miner_stats['miner_lifetime_earnings']}\n"
            f" Lifetime Wager Amount: {self.app.miner_stats['miner_lifetime_wager']}\n"
            f" Lifetime Wins: {self.app.miner_stats['miner_lifetime_wins']}\n"
            f" Lifetime Losses: {self.app.miner_stats['miner_lifetime_losses']}\n"
            f" Win/Loss Ratio: {self.app.miner_stats['miner_win_loss_ratio']:.2f}\n"
            f" Miner Status: {self.app.miner_stats['miner_status']}\n"
        )

        options_text = "\n".join(
            f"> {option}" if i == self.selected_index else f"  {option}"
            for i, option in enumerate(self.options)
        )

        self.text_area.text = (
            f"{header_text}\n{divider}\n{miner_stats_text}\n{divider}\n{options_text}"
        )

    def handle_enter(self):
        if self.selected_index == 0:
            self.app.change_view(PredictionsList(self.app))
        elif self.selected_index == 1:
            self.app.change_view(GamesList(self.app))
        elif self.selected_index == 2:
            graceful_shutdown(self.app, submit=True)

    def move_up(self):
        super().move_up()
        self.update_text_area()

    def move_down(self):
        super().move_down()
        self.update_text_area()


class PredictionsList(InteractiveTable):
    def __init__(self, app):
        super().__init__(app)
        app.reload_data()
        self.message = ""
        self.update_options()
        self.update_text_area()

    def update_options(self):
        # Combine predictions and unsubmitted_predictions for formatting calculation
        all_predictions = self.app.predictions

        if not all_predictions:
            # Set default lengths if there are no predictions
            max_teamA_len = len("Team A")
            max_teamB_len = len("Team B")
            max_teamAodds_len = len("Team A Odds")
            max_teamBodds_len = len("Team B Odds")
            max_tieOdds_len = len("Tie Odds")
            max_prediction_len = len("Prediction")
            max_status_len = len("Status")
            max_wager_amount_len = len("Wager Amount")
            max_outcome_len = len("Outcome")
        else:
            # Calculate maximum widths for each column based on data and header
            max_teamA_len = max(
                max(len(pred.get("teamA", "")) for pred in all_predictions.values()),
                len("Team A"),
            )
            max_teamB_len = max(
                max(len(pred.get("teamB", "")) for pred in all_predictions.values()),
                len("Team B"),
            )
            max_teamAodds_len = max(
                max(
                    len(str(pred.get("teamAodds", "")))
                    for pred in all_predictions.values()
                ),
                len("Team A Odds"),
            )
            max_teamBodds_len = max(
                max(
                    len(str(pred.get("teamBodds", "")))
                    for pred in all_predictions.values()
                ),
                len("Team B Odds"),
            )
            max_tieOdds_len = max(
                max(
                    len(str(pred.get("tieOdds", "")))
                    for pred in all_predictions.values()
                ),
                len("Tie Odds"),
            )
            max_prediction_len = max(
                max(
                    len(pred.get("predictedOutcome", ""))
                    for pred in all_predictions.values()
                ),
                len("Prediction"),
            )
            max_wager_amount_len = max(
                max(
                    len(str(pred.get("wager", ""))) for pred in all_predictions.values()
                ),
                len("Wager Amount"),
            )
            max_outcome_len = max(
                max(len(pred.get("outcome", "")) for pred in all_predictions.values()),
                len("Outcome"),
            )

        # Define the header with calculated widths
        self.header = Label(
            f"  {'Team A':<{max_teamA_len}} | {'Team B':<{max_teamB_len}} | {'Team A Odds':<{max_teamAodds_len}} | {'Team B Odds':<{max_teamBodds_len}} | {'Tie Odds':<{max_tieOdds_len}} | {'Prediction':<{max_prediction_len}} | {'Wager Amount':<{max_wager_amount_len}} | {'Outcome':<{max_outcome_len}} ",
            style="bold",
        )

        # Generate options for the scrollable list
        self.options = [
            f"{pred.get('teamA', ''):<{max_teamA_len}} | {pred.get('teamB', ''):<{max_teamB_len}} | {str(pred.get('teamAodds', '')):<{max_teamAodds_len}} | {str(pred.get('teamBodds', '')):<{max_teamBodds_len}} | {str(pred.get('tieOdds', '')):<{max_tieOdds_len}} | {pred.get('predictedOutcome', ''):<{max_prediction_len}} | {str(pred.get('wager', '')):<{max_wager_amount_len}} | {pred.get('outcome', ''):<{max_outcome_len}}"
            for pred in self.app.predictions.values()
        ]

        self.options.append("Go Back")  # Add "Go Back" option

    def update_text_area(self):
        # Update the text area to include the header, divider, options, and space before "Go Back"
        header_text = self.header.text
        divider = "-" * len(header_text)
        if len(self.options) <= 1:  # Only "Go Back" is present
            options_text = "No predictions available."
        else:
            options_text = "\n".join(
                f"> {option}" if i == self.selected_index else f"  {option}"
                for i, option in enumerate(self.options[:-1])
            )
        go_back_text = (
            f"\n\n  {self.options[-1]}"
            if self.selected_index != len(self.options) - 1
            else f"\n\n> {self.options[-1]}"
        )
        self.text_area.text = (
            f"{header_text}\n{divider}\n{options_text}{go_back_text}\n\n{self.message}"
        )

    def handle_enter(self):
        if self.selected_index == len(self.options) - 1:  # Go Back
            self.app.change_view(MainMenu(self.app))
        else:
            selected_prediction = list(self.app.predictions.values())[
                self.selected_index
            ]

            # Find the corresponding game data
            game_id = selected_prediction.get("teamGameID")
            game_data = next(
                (game for game in self.app.games.values() if game["gameID"] == game_id),
                None,
            )

            # TODO - switch to prediction view (can't edit)

    def clear_message(self):
        self.message = ""
        self.update_text_area()

    def move_up(self):
        if self.selected_index > 0:
            self.selected_index -= 1
            self.update_text_area()

    def move_down(self):
        if self.selected_index < len(self.options) - 1:
            self.selected_index += 1
            self.update_text_area()


class GamesList(InteractiveTable):
    def __init__(self, app):
        super().__init__(app)
        app.reload_data()

        # Sort games by date in descending order
        sorted_games = sorted(
            self.app.games.values(),
            key=lambda x: datetime.datetime.fromisoformat(x["eventStartDate"]),
        )

        if not self.app.games:
            # Set default lengths if there are no games
            max_sport_len = len("Sport")
            max_teamA_len = len("Team A")
            max_teamB_len = len("Team B")
            max_eventStartDate_len = len("Event Start Date (UTC)")
            max_teamAodds_len = len("Team A Odds")
            max_teamBodds_len = len("Team B Odds")
            max_tieOdds_len = len("Tie Odds")
        else:
            # Calculate maximum widths for each column based on data and header
            max_sport_len = max(
                max(len(game["sport"]) for game in self.app.games.values()),
                len("Sport"),
            )
            max_teamA_len = max(
                max(len(game["teamA"]) for game in self.app.games.values()),
                len("Team A"),
            )
            max_teamB_len = max(
                max(len(game["teamB"]) for game in self.app.games.values()),
                len("Team B"),
            )
            max_eventStartDate_len = max(
                max(len(game["eventStartDate"]) for game in self.app.games.values()),
                len("Event Start Date"),
            )
            max_teamAodds_len = max(
                max(
                    len(str(game.get("teamAodds", "")))
                    for game in self.app.games.values()
                ),
                len("Team A Odds"),
            )
            max_teamBodds_len = max(
                max(
                    len(str(game.get("teamBodds", "")))
                    for game in self.app.games.values()
                ),
                len("Team B Odds"),
            )
            max_tieOdds_len = max(
                max(
                    len(str(game.get("tieOdds", "")))
                    for game in self.app.games.values()
                ),
                len("Tie Odds"),
            )

        # Define the header with calculated widths
        self.header = Label(
            f"  {'Sport':<{max_sport_len}} | {'Team A':<{max_teamA_len}} | {'Team B':<{max_teamB_len}} | {'Event Start Date (UTC)':<{max_eventStartDate_len}} | {'Team A Odds':<{max_teamAodds_len}} | {'Team B Odds':<{max_teamBodds_len}} | {'Tie Odds':<{max_tieOdds_len}} ",
            style="bold",
        )

        # Generate options for the scrollable list using sorted games
        self.options = [
            f"{game['sport']:<{max_sport_len}} | {game['teamA']:<{max_teamA_len}} | {game['teamB']:<{max_teamB_len}} | {self.format_event_start_date(game['eventStartDate']):<{max_eventStartDate_len}} | {str(game.get('teamAodds', '')):<{max_teamAodds_len}} | {str(game.get('teamBodds', '')):<{max_teamBodds_len}} | {str(game.get('tieOdds', '')):<{max_tieOdds_len}}"
            for game in sorted_games
        ]
        self.options.append("Go Back")  # Add "Go Back" option
        self.update_text_area()

    def format_event_start_date(self, event_start_date):
        # Parse the ISO format date
        dt = datetime.datetime.fromisoformat(event_start_date)
        # Convert to UTC
        dt = dt.astimezone(pytz.utc)
        # Format it to the desired format
        return dt.strftime("%Y-%m-%d %H:%M")

    def update_text_area(self):
        # Update the text area to include the header, divider, options, and space before "Go Back"
        header_text = self.header.text
        divider = "-" * len(header_text)
        if len(self.options) == 1:  # Only "Go Back" is present
            options_text = "No games available."
        else:
            options_text = "\n".join(
                f"> {option}" if i == self.selected_index else f"  {option}"
                for i, option in enumerate(self.options[:-1])
            )
        current_time_text = f"\n\nCurrent Time (UTC): {datetime.datetime.now(pytz.utc).strftime('%Y-%m-%d %H:%M')}"
        go_back_text = (
            f"\n\n  {self.options[-1]}"
            if self.selected_index != len(self.options) - 1
            else f"\n\n> {self.options[-1]}"
        )
        self.text_area.text = (
            f"{header_text}\n{divider}\n{options_text}{go_back_text}{current_time_text}"
        )

    def handle_enter(self):
        if self.selected_index == len(self.options) - 1:  # Go Back
            self.app.change_view(MainMenu(self.app))
        else:
            # Get the currently selected game data from the sorted list
            sorted_games = sorted(
                self.app.games.values(),
                key=lambda x: datetime.datetime.fromisoformat(x["eventStartDate"]),
            )
            selected_game_data = sorted_games[self.selected_index]
            # Change view to WagerConfirm, passing the selected game data
            self.app.change_view(WagerConfirm(self.app, selected_game_data, self))

    def move_up(self):
        if self.selected_index > 0:
            self.selected_index -= 1
            self.update_text_area()

    def move_down(self):
        if self.selected_index < len(self.options) - 1:
            self.selected_index += 1
            self.update_text_area()


class WagerConfirm(InteractiveTable):
    """
    Wager confirmation view
    """

    def __init__(self, app, game_data, previous_view, wager_amount=""):
        super().__init__(app)
        self.game_data = game_data
        self.previous_view = previous_view
        self.miner_cash = app.miner_cash
        self.selected_team = game_data["teamA"]  # Default to teamA
        self.wager_input = TextArea(
            text=str(wager_amount),  # Set the initial wager amount
            multiline=False,
            password=False,
            focusable=True,
        )
        self.options = [
            "Change Selected Team",
            "Enter Wager Amount",
            "Confirm Wager",
            "Go Back",
        ]
        self.confirmation_message = ""
        self.update_text_area()

    def update_text_area(self):
        game_info = (
            f" {self.game_data['sport']} | {self.game_data['teamA']} vs {self.game_data['teamB']} | {self.game_data['eventStartDate']} | "
            f"Team A Odds: {self.game_data['teamAodds']} | Team B Odds: {self.game_data['teamBodds']} | Tie Odds: {self.game_data['tieOdds']}"
        )
        cash_info = f"Miner's Cash: ${self.miner_cash}"
        selected_team_text = f"Selected Team: {self.selected_team}"
        wager_input_text = f"Wager Amount: {self.wager_input.text}"
        options_text = "\n".join(
            f"> {option}" if i == self.selected_index else f"  {option}"
            for i, option in enumerate(self.options)
        )
        self.text_area.text = f"{game_info}\n{cash_info}\n{selected_team_text}\n{wager_input_text}\n{options_text}\n\n{self.confirmation_message}"
        self.box = HSplit([self.text_area, self.wager_input])

    def handle_enter(self):
        if self.selected_index == 0:  # Change Selected Team
            self.toggle_selected_team()
        elif self.selected_index == 1:  # Enter Wager Amount
            self.focus_wager_input()
        elif self.selected_index == 2:  # Confirm Wager
            try:
                wager_amount = float(self.wager_input.text.strip())
                if wager_amount <= 0 or wager_amount > self.miner_cash:
                    raise ValueError("Invalid wager amount")
                # Add the prediction to unsubmitted_predictions
                prediction_id = str(uuid.uuid4())
                self.app.unsubmitted_predictions[prediction_id] = {
                    "predictionID": prediction_id,
                    "teamGameID": self.game_data["externalID"],
                    "sport": self.game_data["sport"],
                    "teamA": self.game_data["teamA"],
                    "teamB": self.game_data["teamB"],
                    "eventStartDate": self.game_data["eventStartDate"],
                    "predictionDate": datetime.datetime.now(
                        datetime.timezone.utc
                    ).isoformat(),
                    "teamAodds": self.game_data["teamAodds"],
                    "teamBodds": self.game_data["teamBodds"],
                    "tieOdds": self.game_data["tieOdds"],
                    "predictedOutcome": self.selected_team,
                    "wager": wager_amount,
                    "canOverwrite": True,
                    "outcome": "",
                }
                self.app.miner_cash -= (
                    wager_amount  # Deduct wager amount from miner's cash
                )
                self.confirmation_message = (
                    "Wager confirmed! Submitting Prediction to Validators..."
                )
                self.update_text_area()
                submit_predictions(self.app)
                threading.Timer(
                    2.0, lambda: self.app.change_view(self.previous_view)
                ).start()  # Delay for 2 seconds
            except ValueError:
                self.wager_input.text = "Invalid amount. Try again."
                self.update_text_area()
        elif self.selected_index == 3:  # Go Back
            self.app.change_view(self.previous_view)

    def move_up(self):
        if self.selected_index > 0:
            self.selected_index -= 1
            self.update_text_area()

    def move_down(self):
        if self.selected_index < len(self.options) - 1:
            self.selected_index += 1
            self.update_text_area()

    def focus_wager_input(self):
        self.app.layout.focus(self.wager_input)

    def blur_wager_input(self):
        self.app.layout.focus(self.text_area)

    def handle_wager_input_enter(self):
        self.blur_wager_input()
        self.selected_index = 2  # Move focus to "Confirm Wager"
        self.update_text_area()
        self.app.layout.focus(self.text_area)  # Ensure focus is back on the text area

    def toggle_selected_team(self):
        if self.selected_team == self.game_data["teamA"]:
            self.selected_team = self.game_data["teamB"]
        elif self.selected_team == self.game_data["teamB"] and self.game_data.get(
            "canTie", False
        ):
            self.selected_team = "Tie"
        else:
            self.selected_team = self.game_data["teamA"]
        self.update_text_area()


bindings = KeyBindings()


@bindings.add("up")
def _(event):
    try:
        custom_app = event.app.custom_app
        if hasattr(custom_app, "current_view"):
            custom_app.current_view.move_up()
            if isinstance(custom_app.current_view, WagerConfirm):
                custom_app.current_view.blur_wager_input()
    except AttributeError as e:
        logging.error(f"Failed to move up: {e}")
        print("Failed to move up:", str(e))


@bindings.add("down")
def _(event):
    try:
        custom_app = event.app.custom_app
        if hasattr(custom_app, "current_view"):
            custom_app.current_view.move_down()
            if isinstance(custom_app.current_view, WagerConfirm):
                custom_app.current_view.blur_wager_input()
    except AttributeError as e:
        logging.error(f"Failed to move down: {e}")
        print("Failed to move down:", str(e))


@bindings.add("enter")
def _(event):
    try:
        custom_app = event.app.custom_app
        if hasattr(custom_app, "current_view"):
            if isinstance(custom_app.current_view, WagerConfirm):
                if custom_app.current_view.app.layout.has_focus(
                    custom_app.current_view.wager_input
                ):
                    custom_app.current_view.handle_wager_input_enter()
                else:
                    custom_app.current_view.handle_enter()
            else:
                custom_app.current_view.handle_enter()
    except AttributeError as e:
        logging.error(f"Failed to handle enter: {e}")
        print("Failed to handle enter:", str(e))


@bindings.add("q")
def _(event):
    custom_app = event.app.custom_app
    graceful_shutdown(custom_app, submit=False)


@bindings.add("c-z", eager=True)
def _(event):
    custom_app = event.app.custom_app
    graceful_shutdown(custom_app, submit=False)


@bindings.add("c-c", eager=True)
def _(event):
    custom_app = event.app.custom_app
    graceful_shutdown(custom_app, submit=False)


def graceful_shutdown(app, submit: bool):
    if submit:
        print("Submitting predictions...")
        logging.info("Submitting predictions...")
        submit_predictions(app)
    app.app.exit()


def signal_handler(signal, frame):
    logging.info(f"Signal received, shutting down gracefully...")
    graceful_shutdown(app)


signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)


def submit_predictions(app):
    for prediction in app.unsubmitted_predictions.values():
        try:
            app.cursor.execute(
                """INSERT INTO predictions (predictionID, teamGameID, predictionDate, teamA, teamB, teamAodds, teamBodds, tieOdds, predictedOutcome, wager, outcome, canOverwrite) 
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    prediction["predictionID"],
                    prediction["teamGameID"],
                    prediction["predictionDate"],
                    prediction["teamA"],
                    prediction["teamB"],
                    prediction["teamAodds"],
                    prediction["teamBodds"],
                    prediction["tieOdds"],
                    prediction["predictedOutcome"],
                    prediction["wager"],
                    prediction["outcome"],
                    prediction["canOverwrite"],
                ),
            )
            app.db.commit()
            logging.debug(f"miner_uid: {app.miner_stats['miner_uid']}")
            update_miner_stats(
                app,
                prediction["wager"],
                prediction["predictionDate"],
                app.miner_stats["miner_uid"],
            )

        except Exception as e:
            logging.error(f"Failed to submit prediction: {e}")
            print(f"Failed to submit prediction: {e}")
            print(f"Stacktrace: {traceback.format_exc()}")
            # Print error to console for immediate feedback
    # clear unsubmitted_predictions
    app.unsubmitted_predictions = {}


def update_miner_stats(app, wager, prediction_date, miner_uid):
    # get miner_stats
    logging.info(f"Updating miner stats for miner_uid: {miner_uid}")
    logging.debug(f"Miner stats: {app.miner_stats}")
    new_miner_cash = app.miner_stats["miner_cash"] - wager
    new_miner_last_prediction_date = prediction_date
    new_miner_lifetime_wager = app.miner_stats["miner_lifetime_wager"] + wager
    new_miner_lifetime_predictions = app.miner_stats["miner_lifetime_predictions"] + 1

    app.cursor.execute(
        """UPDATE miner_stats SET miner_cash = ?,
                        miner_last_prediction_date = ?, 
                        miner_lifetime_wager = ?, 
                        miner_lifetime_predictions = ? 
                        WHERE miner_uid = ?""",
        (
            new_miner_cash,
            new_miner_last_prediction_date,
            new_miner_lifetime_wager,
            new_miner_lifetime_predictions,
            miner_uid,
        ),
    )
    app.db.commit()
    app.miner_stats = get_miner_stats(app.cursor, miner_uid)
    logging.info(f"Updated miner stats: {app.miner_stats}")


def get_database(db_path):
    try:
        db = sqlite3.connect(db_path)
        return db, db.cursor()
    except Exception as e:
        print(f"Failed to get database: {e}")
        return None, None


def get_predictions(cursor):
    predictions = {}
    cursor.execute("SELECT * FROM predictions")
    columns = [
        column[0] for column in cursor.description
    ]  # Get column names from the cursor description
    for row in cursor.fetchall():
        predictions[row[0]] = {columns[i]: row[i] for i in range(len(columns))}
    return predictions


def get_game_data(cursor):
    game_data = {}
    cursor.execute("SELECT * FROM games WHERE active = 0")
    columns = [
        column[0] for column in cursor.description
    ]  # Get column names from the cursor description
    for row in cursor.fetchall():
        game_data[row[0]] = {columns[i]: row[i] for i in range(len(columns))}
    return game_data


def get_miner_stats(cursor, uid=None):
    logging.info(f"Getting miner stats for uid: {uid}")

    if uid is not None:
        cursor.execute("SELECT * FROM miner_stats WHERE miner_uid = ?", (str(uid),))
        columns = [column[0] for column in cursor.description]
        row = cursor.fetchone()
        if row is None:
            logging.warning(f"No miner stats found for uid: {uid}")
            return None
        miner_stats = {columns[i]: row[i] for i in range(len(columns))}
        logging.info(f"Miner stats: {miner_stats}")
        return miner_stats
    else:
        logging.error("No UID specified")
        return None


if __name__ == "__main__":
    app = Application()
    app.run()
