import nodriver

async def send_cdp(
    connection: nodriver.Tab | nodriver.Connection,
    method: str,
    params: dict = {},
    session_id: str = None,
):
    """thin helper/patch to send a raw devtools command.

    yields a dict suitable for nodriver's generator-based send() made specifically for this `nodriver` patch: 
    
    https://github.com/twarped/nodriver/commit/bf1dfda6cb16a31d2fd302f370f130dda3a3413b

    :param method: full method name (e.g. "Runtime.evaluate").
    :param params: payload dict; omit if none.
    :param session_id: target session if addressing a child target.
    :param connection: override connection (tab or underlying connection).
    :return: protocol response pair (varies by method) or None.
    """
    def c():
        yield {"method": method, "params": params, "sessionId": session_id}

    return await connection.send(c())