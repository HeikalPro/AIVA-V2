from fastapi import HTTPException, status


class NotFoundError(HTTPException):
    def __init__(self, detail: str = "Resource not found") -> None:
        super().__init__(status_code=status.HTTP_404_NOT_FOUND, detail=detail)


class ForbiddenError(HTTPException):
    def __init__(self, detail: str = "Forbidden") -> None:
        super().__init__(status_code=status.HTTP_403_FORBIDDEN, detail=detail)


class UnauthorizedError(HTTPException):
    def __init__(self, detail: str = "Unauthorized") -> None:
        super().__init__(status_code=status.HTTP_401_UNAUTHORIZED, detail=detail)


class ConflictError(HTTPException):
    def __init__(self, detail: str = "Conflict") -> None:
        super().__init__(status_code=status.HTTP_409_CONFLICT, detail=detail)


class BadRequestError(HTTPException):
    def __init__(self, detail: str = "Bad request") -> None:
        super().__init__(status_code=status.HTTP_400_BAD_REQUEST, detail=detail)
