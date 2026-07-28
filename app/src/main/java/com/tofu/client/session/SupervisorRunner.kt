package com.tofu.client.session

import com.tofu.client.data.Profile
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.delay

/**
 * What the UI should apply once a supervisor call has finished.
 *
 * Returned as DATA rather than applied via callbacks so the whole execution
 * flow can be unit-tested off-device: previously this logic lived inline in a
 * Composable, where the staleness guard silently compared a captured local
 * against itself and could never be false.
 */
data class RunOutcome(
    /** New running state, or null to leave it unchanged. */
    val running: Boolean? = null,
    val failed: Boolean = false,
    val message: String? = null,
    /** Hand the user into the WebView (a start that came up while still current). */
    val handOff: Boolean = false,
)

/**
 * Executes one supervisor call — the login-then-act handshake, the call itself,
 * and the post-start poll — and reports what the UI should do.
 *
 * Every side-effecting dependency is a parameter so the flow is testable with
 * plain fakes:
 *  - [login] establishes a session (only consulted when [ProbePlan.mayLogIn]);
 *  - [call] performs the actual supervisor request;
 *  - [isCurrent] answers "does this result still belong to what's on screen?".
 *
 * [isCurrent] is deliberately a FUNCTION, not a captured value. The caller must
 * wire it to something later compositions mutate (a remembered ref), otherwise
 * it degenerates into comparing a value with itself — which is exactly the bug
 * this seam was extracted to make impossible to write silently.
 */
suspend fun executeSupervisorCall(
    profile: Profile,
    action: SupervisorAction,
    plan: ProbePlan,
    signedIn: Boolean,
    login: suspend (Profile) -> LoginResult,
    call: suspend (SupervisorAction, Profile) -> SupervisorClient.Result,
    isCurrent: () -> Boolean,
    pollAttempts: Int = ServerLifecycle.START_POLL_ATTEMPTS,
    pollIntervalMs: Long = ServerLifecycle.START_POLL_INTERVAL_MS,
): RunOutcome {
    return try {
        // Establish the session FIRST when we don't hold one. The supervisor
        // rides the code-server cookie, and code-server (the proxy) is up even
        // while Tofu is down — so this handshake works on a STOPPED server.
        if (plan.mayLogIn && !signedIn) {
            val result = login(profile)
            // Includes NeedsInteractiveSso: it yields no cookie, so pressing on
            // would 401 and misreport an un-completed sign-in as "the daemon
            // isn't responding".
            if (ServerLifecycle.isLoginBlocking(result)) {
                return RunOutcome(
                    failed = true,
                    message = ServerLifecycle.explainLoginBlock(result),
                )
            }
        }

        when (val res = call(action, profile)) {
            is SupervisorClient.Result.Ok -> {
                var running = res.running
                // /start returns before the port binds (by design), so poll
                // until the server reports itself up rather than leaving the
                // card claiming it is still stopped.
                if (action == SupervisorAction.START && !running) {
                    for (attempt in 0 until pollAttempts) {
                        delay(pollIntervalMs)
                        val s = call(SupervisorAction.STATUS, profile)
                        if (s is SupervisorClient.Result.Ok && s.running) {
                            running = true
                            break
                        }
                    }
                }
                val completion = ServerLifecycle.completionFor(
                    action = action,
                    running = running,
                    stillCurrent = isCurrent(),
                )
                RunOutcome(
                    running = running,
                    handOff = completion.handOff,
                    message = if (completion.showTimeout) {
                        ServerLifecycle.startTimeoutMessage()
                    } else {
                        null
                    },
                )
            }
            is SupervisorClient.Result.Failed -> RunOutcome(
                // An AUTO probe stays silent: "couldn't reach it just now" is
                // not worth painting the card red when nobody asked.
                failed = plan.reportFailure,
                message = if (plan.reportFailure) {
                    SupervisorUrl.explainFailure(res.code, res.message)
                } else {
                    null
                },
            )
        }
    } catch (c: CancellationException) {
        // Leaving the screen is not an error. Propagate so the coroutine dies
        // normally and the caller's finally still releases `busy`.
        throw c
    } catch (t: Throwable) {
        // A real transport failure. Without this the exception escaped to the
        // scope's handler and the card silently reverted to its previous state
        // — the user tapped Start and nothing visibly happened.
        RunOutcome(
            failed = plan.reportFailure,
            message = if (plan.reportFailure) {
                SupervisorUrl.explainFailure(0, t.message ?: t::class.java.simpleName)
            } else {
                null
            },
        )
    }
}
