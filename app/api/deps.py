from typing import Annotated

from fastapi import Depends, Path, Query, Request

from app.http import TransfermarktClient


def get_client(request: Request) -> TransfermarktClient:
    """The shared upstream client created in the app lifespan."""
    return request.app.state.client


Client = Annotated[TransfermarktClient, Depends(get_client)]

PlayerId = Annotated[
    str, Path(pattern=r"^[0-9]+$", max_length=12, description="Transfermarkt player id.", examples=["28003"])
]
ClubId = Annotated[
    str, Path(pattern=r"^[0-9]+$", max_length=12, description="Transfermarkt club id.", examples=["131"])
]
GameId = Annotated[
    str, Path(pattern=r"^[0-9]+$", max_length=12, description="Transfermarkt game (match) id.", examples=["4359338"])
]
CompetitionId = Annotated[
    str, Path(pattern=r"^[A-Za-z0-9]{1,12}$", description="Transfermarkt competition id.", examples=["GB1"])
]
SeasonId = Annotated[
    str | None,
    Query(
        pattern=r"^[0-9]{4}$",
        description="Season by its starting year (2024 = 24/25). Defaults to the current season.",
        examples=["2024"],
    ),
]
PageNumber = Annotated[int, Query(ge=1, description="Page of results to return, starting at 1.")]
SearchQuery = Annotated[str, Path(description="Text to search for.")]
