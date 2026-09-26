import json
from dataclasses import dataclass, field
from typing import ClassVar, Self

from app.http import TransfermarktClient
from app.services.base import TransfermarktBase, parse_html
from app.utils.utils import extract_from_url, safe_split
from app.utils.xpath import Players


@dataclass
class TransfermarktPlayerTransfers(TransfermarktBase):
    """
    A class for retrieving and parsing the player's transfer history and youth club details from Transfermarkt.

    Args:
        player_id (str): The unique identifier of the player.
        transfer_history (dict): The player's transfer history (JSON).

    Attributes:
        URL_TEMPLATE (str): The URL template for the player's transfers page on Transfermarkt.
        URL_TRANSFERS (str): The URL template for the player's transfer history JSON.
    """

    player_id: str
    transfer_history: dict = field(default_factory=dict)
    URL_TEMPLATE: ClassVar[str] = "https://www.transfermarkt.com/-/transfers/spieler/{player_id}"
    URL_TRANSFERS: ClassVar[str] = "https://www.transfermarkt.com/ceapi/transferHistory/list/{player_id}"

    def __post_init__(self) -> None:
        """Validate that the page is a player transfers page."""
        self.raise_exception_if_not_found(xpath=Players.Profile.NAME)

    @classmethod
    def from_bytes(cls, html: bytes, transfer_history: bytes | None = None, *, player_id: str) -> Self:
        """Build the service from an already fetched transfers page and, optionally, its transfer history JSON."""
        url = cls.URL_TEMPLATE.format(player_id=player_id)
        return cls(
            URL=url,
            page=parse_html(url, html),
            player_id=player_id,
            transfer_history=json.loads(transfer_history) if transfer_history is not None else {},
        )

    @classmethod
    async def fetch(cls, client: TransfermarktClient, *, player_id: str) -> Self:
        """Fetch and parse the transfers page, then (for a valid player) its transfer history."""
        response = await client.get(cls.URL_TEMPLATE.format(player_id=player_id))
        tfmkt = cls.from_bytes(response.content, player_id=player_id)
        history = await client.get(cls.URL_TRANSFERS.format(player_id=player_id))
        tfmkt.transfer_history = history.json()
        return tfmkt

    def __parse_player_transfer_history(self) -> list:
        """
        Parse and retrieve the transfer history of the specified player from Transfermarkt,
        including the unique identifier of each transfer, source club information (ID and name),
        destination club information (ID and name), transfer date, upcoming status, season, market
        value at the time of transfer, and transfer fee.

        Returns:
            list: A list of dictionaries, each containing details of the player's transfer history,
        """
        transfers = self.transfer_history["transfers"]

        return [
            {
                "id": extract_from_url(transfer["url"], "transfer_id"),
                "clubFrom": {
                    "id": extract_from_url(transfer["from"]["href"]),
                    "name": transfer["from"]["clubName"],
                },
                "clubTo": {
                    "id": extract_from_url(transfer["to"]["href"]),
                    "name": transfer["to"]["clubName"],
                },
                "date": transfer["date"],
                "upcoming": transfer["upcoming"],
                "season": transfer["season"],
                "marketValue": transfer["marketValue"],
                "fee": transfer["fee"],
            }
            for transfer in transfers
        ]

    def get_player_transfers(self) -> dict:
        """
        Retrieve and parse the transfer history and youth clubs of the specified player from Transfermarkt.

        Returns:
            dict: A dictionary containing the player's unique identifier, parsed transfer history, youth clubs,
                  and the timestamp of when the data was last updated.
        """
        self.response["id"] = self.player_id
        self.response["transfers"] = self.__parse_player_transfer_history()
        self.response["youthClubs"] = safe_split(self.get_text_by_xpath(Players.Transfers.YOUTH_CLUBS), ",")

        return self.response
