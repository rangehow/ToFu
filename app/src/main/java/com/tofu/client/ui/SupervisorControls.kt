package com.tofu.client.ui

import androidx.compose.animation.AnimatedVisibility
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.defaultMinSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.PlayArrow
import androidx.compose.material.icons.filled.Refresh
import androidx.compose.material.icons.filled.Stop
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.FilledTonalButton
import androidx.compose.material3.Icon
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.SideEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.dp
import com.tofu.client.data.Profile
import com.tofu.client.session.LoginResult
import com.tofu.client.session.ServerLifecycle
import com.tofu.client.session.ServerState
import com.tofu.client.session.SessionManager
import com.tofu.client.session.SupervisorClient
import com.tofu.client.session.SupervisorUrl
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext

/**
 * Start / Stop / Refresh for the Tofu server behind [profile], driving the
 * host-side supervisor.
 *
 * Two behavioural changes over the first version, both about making "the app
 * runs the server" feel like a real feature rather than a debug panel:
 *
 *  1. **It polls itself on first composition** when the profile is signed in,
 *     so the card shows Running/Stopped without the user hunting for Refresh.
 *  2. Legal actions come from the pure [ServerLifecycle] state machine, so
 *     "Stop" is not offered on an already-stopped server and the rules are
 *     unit-tested off-device.
 */
@Composable
fun SupervisorControls(
    profile: Profile,
    scope: CoroutineScope,
    /**
     * REQUIRED and non-null on purpose. Controls do login-then-act, so a call
     * site that omitted the session would silently skip the handshake and 401
     * on every supervisor call — resurrecting the start-a-stopped-server
     * deadlock with no compile error and no failing test. Making it mandatory
     * turns that omission into a build failure.
     */
    session: SessionManager,
    client: SupervisorClient = SupervisorClient(),
    modifier: Modifier = Modifier,
    onStateChange: (ServerState) -> Unit = {},
    onServerReady: () -> Unit = {},
) {
    // Keyed on the fields the supervisor result actually depends on, NOT just
    // the row id: editing a profile's URL (which clears cookieHost) or its
    // project path leaves id unchanged, so keying on id alone would keep
    // showing the PREVIOUS server's Running/Stopped badge — and the auto-probe
    // below, guarded on `running == null`, would never re-fire to correct it.
    val stateKey = listOf(profile.id, profile.baseUrl, profile.cookieHost, profile.projectPath)
    var running by remember(stateKey) { mutableStateOf<Boolean?>(null) }
    var busy by remember(stateKey) { mutableStateOf(false) }
    var failed by remember(stateKey) { mutableStateOf(false) }
    var message by remember(stateKey) { mutableStateOf<String?>(null) }

    val state = ServerLifecycle.resolve(profile, running, busy, failed)
    val caps = ServerLifecycle.capabilities(state)

    // Report upward only when the resolved state actually CHANGES. Doing this
    // from a LaunchedEffect keyed on `state` would fire on every first
    // composition of every card, and each parent write recomposes the whole
    // list (the header reads the aggregate) — a full-list recompose per card.
    val lastReported = remember(stateKey) { arrayOfNulls<ServerState>(1) }
    if (lastReported[0] != state) {
        lastReported[0] = state
        SideEffect { onStateChange(state) }
    }

    fun run(action: String) {
        busy = true
        failed = false
        message = null
        scope.launch {
            // Establish the session FIRST when we don't hold one. The supervisor
            // rides the code-server cookie, and code-server (the proxy) is up
            // even while Tofu is down — so this handshake works on a STOPPED
            // server. Requiring an Open first was a deadlock: Open cannot
            // succeed against a server that is down.
            if (!ServerLifecycle.isSignedIn(profile)) {
                val login = withContext(Dispatchers.IO) { session.login(profile) }
                // Includes NeedsInteractiveSso: it yields no cookie, so pressing
                // on would 401 and misreport an un-completed sign-in as "the
                // daemon isn't responding".
                if (ServerLifecycle.isLoginBlocking(login)) {
                    failed = true
                    message = ServerLifecycle.explainLoginBlock(login)
                    busy = false
                    return@launch
                }
            }
            val res = withContext(Dispatchers.IO) {
                when (action) {
                    "start" -> client.start(profile)
                    "stop" -> client.stop(profile)
                    else -> client.status(profile)
                }
            }
            when (res) {
                is SupervisorClient.Result.Ok -> {
                    running = res.running
                    message = null
                    // /start returns before the port binds (by design), so poll
                    // until the server reports itself up rather than leaving the
                    // card lying that it's still stopped.
                    if (action == "start" && !res.running) {
                        for (i in 0 until ServerLifecycle.START_POLL_ATTEMPTS) {
                            delay(ServerLifecycle.START_POLL_INTERVAL_MS)
                            val s = withContext(Dispatchers.IO) { client.status(profile) }
                            if (s is SupervisorClient.Result.Ok && s.running) {
                                running = true
                                break
                            }
                        }
                    }
                    if (action == "start") {
                        if (running == true) {
                            // Starting a server is only ever a means to using it,
                            // so hand the user straight through once it's live.
                            onServerReady()
                        } else {
                            // The window expired. NOT silently: leaving a spinner
                            // with nothing to tap is the dead end we just removed
                            // elsewhere. Say what happened and what to do next.
                            message = ServerLifecycle.startTimeoutMessage()
                        }
                    }
                }
                is SupervisorClient.Result.Failed -> {
                    failed = true
                    message = SupervisorUrl.explainFailure(res.code, res.message)
                }
            }
            busy = false
        }
    }

    // Auto-probe on arrival so state is known without hunting for Refresh. This
    // no longer waits for a session: `run` establishes one when missing, which
    // is what makes a stopped server's state discoverable at all.
    LaunchedEffect(stateKey) {
        if (running == null && !busy) {
            run("status")
        }
    }

    Column(modifier.fillMaxWidth()) {
        Row(
            Modifier.fillMaxWidth(),
            horizontalArrangement = Arrangement.spacedBy(8.dp),
            verticalAlignment = Alignment.CenterVertically,
        ) {
            if (busy) {
                CircularProgressIndicator(
                    Modifier.size(16.dp),
                    strokeWidth = 2.dp,
                    color = MaterialTheme.colorScheme.primary,
                )
                Spacer(Modifier.width(2.dp))
            }
            if (caps.canStart) {
                FilledTonalButton(
                    onClick = { run("start") },
                    enabled = !busy,
                    contentPadding = CompactButtonPadding,
                    modifier = TouchTarget,
                ) {
                    Icon(Icons.Filled.PlayArrow, null, Modifier.size(17.dp))
                    Spacer(Modifier.width(5.dp))
                    Text("Start", style = MaterialTheme.typography.labelLarge)
                }
            }
            if (caps.canStop) {
                OutlinedButton(
                    onClick = { run("stop") },
                    enabled = !busy,
                    contentPadding = CompactButtonPadding,
                    modifier = TouchTarget,
                ) {
                    Icon(Icons.Filled.Stop, null, Modifier.size(17.dp))
                    Spacer(Modifier.width(5.dp))
                    Text("Stop", style = MaterialTheme.typography.labelLarge)
                }
            }
            if (caps.canRefresh) {
                TextButton(
                    onClick = { run("status") },
                    enabled = !busy,
                    contentPadding = CompactButtonPadding,
                    modifier = TouchTarget,
                ) {
                    Icon(Icons.Filled.Refresh, null, Modifier.size(16.dp))
                    Spacer(Modifier.width(4.dp))
                    Text("Check", style = MaterialTheme.typography.labelLarge)
                }
            }
        }

        AnimatedVisibility(message != null) {
            Text(
                message.orEmpty(),
                Modifier.padding(top = 8.dp),
                style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.error,
                textAlign = TextAlign.Start,
            )
        }
    }
}

private val CompactButtonPadding =
    androidx.compose.foundation.layout.PaddingValues(horizontal = 14.dp, vertical = 6.dp)

/**
 * Material's button minimum is 40dp, below the 48dp accessibility touch
 * target, and these are dense inline controls — so the minimum is raised
 * explicitly rather than relying on the default.
 */
private val TouchTarget = Modifier.defaultMinSize(minHeight = 48.dp)
