from pydantic import BaseModel


class WidgetReleaseOut(BaseModel):
    id: int
    version: str
    original_filename: str
    file_size: int
    content_type: str | None = None
    notes: str | None = None
    uploaded_by: int
    uploaded_by_email: str | None = None
    uploaded_at: str | None = None
