"""Internal provider entry point; import performs no SSO/network work."""
from .provider import MlpProvider
from tofu_environments import EnvironmentError

def provider_factory(actor, context):
    from .transport import PlatformTransport
    source = context.get("credential_source")
    if not callable(source):
        raise EnvironmentError("provider_auth_unconfigured", "Configure an owner-bound MLP credential source through trusted host composition")
    return MlpProvider(PlatformTransport(source, cancelled=context.get("cancelled", lambda: False)))

class OfficialAgentCredentialSource:
    """One explicitly approved Agent identity binding, not a global account.

    Application composition supplies the actor authenticated for that Agent.
    Another owner/tenant fails before invoking the official authentication CLI.
    Recreate this binding from the application's identity authority on startup.
    """
    def __init__(self, actor): self.scope = actor.scope
    def __call__(self, actor, audience):
        if actor.scope != self.scope:
            raise EnvironmentError("credential_owner_mismatch", "Agent credential belongs to another owner")
        from tofu_mtsso_auth import user_token, OfficialAuthError
        try:
            return user_token("tofu-environment:" + actor.scope, [audience], environment="PROD")
        except OfficialAuthError as exc:
            raise EnvironmentError(exc.code, exc.message) from exc
__all__ = ["MlpProvider", "provider_factory", "OfficialAgentCredentialSource"]
