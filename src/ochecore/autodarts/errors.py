class ConnectionProblem(Exception):
    """Safe, actionable message. Never include upstream bodies or credentials."""


class LoginRequired(ConnectionProblem):
    pass


class OAuthProblem(ConnectionProblem):
    def __init__(self, code: str):
        self.code = code
        messages = {
            "authorization_pending": "Waiting for AutoDarts approval.",
            "slow_down": "AutoDarts requested a longer polling interval.",
            "access_denied": "The connection was declined on AutoDarts.",
            "expired_token": "The code expired. Start the connection again.",
            "invalid_client": "Unknown OAuth client. Check the client_id assigned to OcheCore.",
            "unauthorized_client": "AutoDarts must enable the device grant for this client.",
            "invalid_grant": "The session expired or was revoked. Reconnect your account.",
        }
        super().__init__(messages.get(code, "AutoDarts rejected the OAuth request."))
