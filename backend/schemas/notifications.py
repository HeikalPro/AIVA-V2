from typing import Literal

from pydantic import BaseModel

DeveloperNotifyStatus = Literal["disabled", "no_recipients", "sent", "failed"]


class DeveloperNotifyOut(BaseModel):
    status: DeveloperNotifyStatus
    message: str
    recipients: list[str] = []
