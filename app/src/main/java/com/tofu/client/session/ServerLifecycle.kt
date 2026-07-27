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

    /** Managed, but we haven't signed in yet, so the supervisor can't be asked. */
    LOCKED,

    /** Managed and signed in, but we haven't polled yet. */
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
     * host. The supervisor rides the same code-server cookie as Tofu, so every
     * control is dead until a successful Open has stamped `cookieHost`. A
     * stale cookieHost from a previous URL must NOT count as signed in.
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
        !isSignedIn(profile) -> ServerState.LOCKED
        busy -> ServerState.TRANSITIONING
        failed -> ServerState.UNREACHABLE
        running == null -> ServerState.UNKNOWN
        running -> ServerState.RUNNING
        else -> ServerState.STOPPED
    }

    fun capabilities(state: ServerState): ServerCapabilities = when (state) {
        // An unmanaged profile has no supervisor at all — Open is all there is.
        ServerState.UNMANAGED ->
            ServerCapabilities(canStart = false, canStop = false, canRefresh = false, canOpen = true)
        // Open is what ESTABLISHES the cookie, so it must stay enabled here —
        // gating it would make the locked state unescapable.
        ServerState.LOCKED ->
            ServerCapabilities(canStart = false, canStop = false, canRefresh = false, canOpen = true)
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

    /** Short status word for the state chip. */
    fun label(state: ServerState): String = when (state) {
        ServerState.UNMANAGED -> "Open only"
        ServerState.LOCKED -> "Sign in to manage"
        ServerState.UNKNOWN -> "Tap to check"
        ServerState.RUNNING -> "Running"
        ServerState.STOPPED -> "Stopped"
        ServerState.TRANSITIONING -> "Working…"
        ServerState.UNREACHABLE -> "Unreachable"
    }
}
