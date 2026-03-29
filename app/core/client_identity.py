import uuid
from fastapi import Request, Response

CLIENT_ID_HEADER = "X-Client-ID"
CLIENT_ID_COOKIE = "groove_client_id"


def get_or_create_client_id(request: Request, response: Response) -> str:
    # Check header first (API/wscat clients)
    client_id = request.headers.get(CLIENT_ID_HEADER)
    if client_id:
        return client_id

    # Check cookie (browsers)
    client_id = request.cookies.get(CLIENT_ID_COOKIE)
    if client_id:
        return client_id

    # Generate new client ID
    client_id = str(uuid.uuid4())
    response.set_cookie(
        key=CLIENT_ID_COOKIE,
        value=client_id,
        max_age=365 * 24 * 60 * 60,  # 1 year
        httponly=True,
        samesite="lax",
    )
    return client_id
