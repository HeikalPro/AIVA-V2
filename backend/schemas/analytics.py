from pydantic import BaseModel


class DashboardStats(BaseModel):
    total_sessions: int
    total_messages: int
    total_ai_requests: int
    avg_response_time_ms: float | None
    total_input_tokens: int
    total_output_tokens: int
    total_cost: float | None


class AgentMetricOut(BaseModel):
    user_id: int
    account_id: int
    avg_response_time: float | None
    ai_usage_count: int | None
    successful_answers: int | None
    escalation_count: int | None
    calculated_at: str | None
