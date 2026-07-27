package com.tofu.client.session

import com.tofu.client.data.Profile

/**
 * Pure (Android-free) model of a server's lifecycle state, and the rules for
 * which controls are legal in each state.
 *
 * This exists because "can the app run the server itself?" is the feature the
 * whole Start/Stop surface answers, and until now that logic lived inline in a
 * Composable — untestable, and the reason the list could never show a server's
 * state without the user tapping Refresh. Keeping it pure means the state
 * machine is unit-tested off-device while the Composable stays a thin renderer.
 */
enum class ServerState {
    /** No projectPath configured — this profile is open-only. */
    UNMANAGED,

    /**
     * Managed, but we have no authoritative answer yet — either we haven't
     * polled, or we hold no session cookie. These are deliberately the SAME
     * state: a missing cookie does not restrict what the user may do, because
     * the supervisor rides the code-server session and code-server (the proxy)
     * stays up while Tofu is down, so any control can do login-then-act.
     *
     * An earlier version modelled "no cookie" as a separate LOCKED state with
     * Start disabled. That deadlocked: a stopped server has no cookie (Open
     * cannot succeed against it), so Start stayed greyed forever — exactly the
     * chicken-and-egg the supervisor exists to break.
     */
    UNKNOWN,

    RUNNING,
    STOPPED,

    /** A start/stop is in flight, or we're polling for the port to bind. */
    TRANSITIONING,

    /** The last supervisor call failed (daemon down, path not allow-listed…). */
    UNREACHABLE,
}

/** What the UI may offer in a given [ServerState]. */
data class ServerCapabilities(
    val canStart: Boolean,
    val canStop: Boolean,
    val canRefresh: Boolean,
    /** Opening the WebView is pointless while the server is known-stopped. */
    val canOpen: Boolean,
)

object ServerLifecycle {

    /** True when [profile] opted into supervisor control by setting a project path. */
    fun isManaged(profile: Profile): Boolean = !profile.projectPath.isNullOrBlank()

    /**
     * Whether the profile currently holds a session cookie valid for its OWN
     * host. A stale cookieHost from a previous URL must NOT count as signed in.
     *
     * This gates whether an action needs a login handshake FIRST — it does not
     * gate whether the action is allowed at all (see [capabilities]).
     */
    fun isSignedIn(profile: Profile): Boolean {
        val host = ServerUrl.parse(profile.baseUrl)?.host ?: return false
        return profile.cookieHost == host
    }

    /**
     * Resolve the displayable state from the profile plus the last known poll
     * result. [running] is null when never polled; [busy] marks an in-flight
     * call; [failed] marks the last call having errored.
     */
    fun resolve(
        profile: Profile,
        running: Boolean?,
        busy: Boolean = false,
        failed: Boolean = false,
    ): ServerState = when {
        !isManaged(profile) -> ServerState.UNMANAGED
        busy -> ServerState.TRANSITIONING
        failed -> ServerState.UNREACHABLE
        // A poll result OUTRANKS the cookie check: if the supervisor answered,
        // we demonstrably reached it, so report the truth. Without this, a
        // login-then-act that succeeds still renders LOCKED until Room re-emits
        // the profile carrying the freshly-stamped cookieHost.
        running == true -> ServerState.RUNNING
        running == false -> ServerState.STOPPED
        else -> ServerState.UNKNOWN
    }

    fun capabilities(state: ServerState): ServerCapabilities = when (state) {
        // An unmanaged profile has no supervisor at all — Open is all there is.
        ServerState.UNMANAGED ->
            ServerCapabilities(canStart = false, canStop = false, canRefresh = false, canOpen = true)
        // No authoritative state yet (never polled, or no session cookie).
        // Every control stays live: the supervisor rides the code-server
        // session, and code-server — the PROXY — is up even while Tofu is down,
        // so `POST /login` succeeds regardless and a tap can log in then act.
        ServerState.UNKNOWN ->
            ServerCapabilities(canStart = true, canStop = true, canRefresh = true, canOpen = true)
        ServerState.RUNNING ->
            ServerCapabilities(canStart = false, canStop = true, canRefresh = true, canOpen = true)
        // Deliberately still openable when stopped: opening is how a user
        // discovers the server is down, and the WebView shows the failure.
        ServerState.STOPPED ->
            ServerCapabilities(canStart = true, canStop = false, canRefresh = true, canOpen = true)
        ServerState.TRANSITIONING ->
            ServerCapabilities(canStart = false, canStop = false, canRefresh = false, canOpen = false)
        ServerState.UNREACHABLE ->
            ServerCapabilities(canStart = true, canStop = true, canRefresh = true, canOpen = true)
    }

    /**
     * Why a login-then-act attempt could not reach the supervisor. Only the
     * outcomes that genuinely block control are mapped; a `Success` or an
     * SSO hand-off is not a block and must not reach here.
     */
    fun explainLoginBlock(result: LoginResult): String = when (result) {
        is LoginResult.BadCredentials ->
            "Wrong password for this server — edit it and try again."
        is LoginResult.NoCredential ->
            "No saved password for this server, so it can't be controlled from here."
        is LoginResult.Error ->
            "Can't reach this server: ${result.message}"
        else -> "Couldn't sign in to this server."
    }

    /** Short status word for the state chip. */
    fun label(state: ServerState): String = when (state) {
        ServerState.UNMANAGED -> "Open only"
        ServerState.UNKNOWN -> "Tap to check"
        ServerState.RUNNING -> "Running"
        ServerState.STOPPED -> "Stopped"
        ServerState.TRANSITIONING -> "Working…"
        ServerState.UNREACHABLE -> "Unreachable"
    }
}
