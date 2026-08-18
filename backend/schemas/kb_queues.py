from pydantic import BaseModel, Field


class QueueGroupOut(BaseModel):
    key: str
    label: str


class AgentQueueAccessOut(BaseModel):
    account_id: int
    user_id: int
    available_queues: list[QueueGroupOut]
    assigned_queues: list[str]
    allowed_queues: list[str]


class AgentQueueAccessUpdate(BaseModel):
    queue_keys: list[str] = Field(min_length=1)


class AgentQueueSummaryItem(BaseModel):
    user_id: int
    queues: list[QueueGroupOut]
    is_restricted: bool


class AgentQueueSummaryOut(BaseModel):
    account_id: int
    agents: list[AgentQueueSummaryItem]


class ChatQueueAccessOut(BaseModel):
    account_id: int
    available_queues: list[QueueGroupOut]
    allowed_queues: list[str]
    default_active_queues: list[str]
