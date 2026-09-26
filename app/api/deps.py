from typing import Annotated

from fastapi import Depends, Request

from app.http import TransfermarktClient


def get_client(request: Request) -> TransfermarktClient:
    """The shared upstream client created in the app lifespan."""
    return request.app.state.client


Client = Annotated[TransfermarktClient, Depends(get_client)]
