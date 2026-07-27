package com.tofu.client.session

import com.tofu.client.data.AuthType
import com.tofu.client.data.Profile
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * Locks the server-lifecycle rules that decide which controls the UI offers.
 *
 * These matter because getting them wrong is silently bad UX rather than a
 * crash: offering Stop on a stopped server, or — the real bug this guards —
 * treating a profile whose URL changed as still signed in, which would enable
 * Start/Stop against a dead host and surface an opaque 401.
 */
class ServerLifecycleTest {

    private fun profile(
        alias: String = "s",
        baseUrl: String = "https://abc-vscode-zw05.mlp.sankuai.com/proxy/15000/",
        cookieHost: String? = null,
        projectPath: String? = "/home/dev/chatui",
    ) = Profile(
        id = 1, alias = alias, baseUrl = baseUrl,
        authType = AuthType.CODE_SERVER_PASSWORD,
        cookieHost = cookieHost, projectPath = projectPath,
    )

    @Test
    fun `no project path means unmanaged`() {
        val p = profile(projectPath = null)
        assertFalse(ServerLifecycle.isManaged(p))
        assertEquals(ServerState.UNMANAGED, ServerLifecycle.resolve(p, null))
    }

    @Test
    fun `blank project path is also unmanaged`() {
        assertFalse(ServerLifecycle.isManaged(profile(projectPath = "   ")))
    }

    @Test
    fun `managed but no cookie is locked`() {
        val p = profile(cookieHost = null)
        assertEquals(ServerState.LOCKED, ServerLifecycle.resolve(p, null))
    }

    /**
     * The guarded regression: a cookie pinned to a PREVIOUS host must not count
     * as signed in. The cookie is Domain-pinned, so after a re-provision the
     * cached jar is bound to a dead host and every supervisor call would 401.
     */
    @Test
    fun `cookie from a different host does not count as signed in`() {
        val p = profile(cookieHost = "old-vscode-zw05.mlp.sankuai.com")
        assertFalse(ServerLifecycle.isSignedIn(p))
        assertEquals(ServerState.LOCKED, ServerLifecycle.resolve(p, null))
    }

    @Test
    fun `matching cookie host counts as signed in`() {
        val p = profile(cookieHost = "abc-vscode-zw05.mlp.sankuai.com")
        assertTrue(ServerLifecycle.isSignedIn(p))
        assertEquals(ServerState.UNKNOWN, ServerLifecycle.resolve(p, null))
    }

    @Test
    fun `unparseable url is never signed in`() {
        assertFalse(ServerLifecycle.isSignedIn(profile(baseUrl = "not a url", cookieHost = "x")))
    }

    @Test
    fun `running and stopped resolve from the poll result`() {
        val p = profile(cookieHost = "abc-vscode-zw05.mlp.sankuai.com")
        assertEquals(ServerState.RUNNING, ServerLifecycle.resolve(p, running = true))
        assertEquals(ServerState.STOPPED, ServerLifecycle.resolve(p, running = false))
    }

    @Test
    fun `busy outranks a stale poll result`() {
        val p = profile(cookieHost = "abc-vscode-zw05.mlp.sankuai.com")
        assertEquals(
            ServerState.TRANSITIONING,
            ServerLifecycle.resolve(p, running = true, busy = true),
        )
    }

    @Test
    fun `failure surfaces as unreachable`() {
        val p = profile(cookieHost = "abc-vscode-zw05.mlp.sankuai.com")
        assertEquals(
            ServerState.UNREACHABLE,
            ServerLifecycle.resolve(p, running = null, failed = true),
        )
    }

    @Test
    fun `running offers stop but not start`() {
        val caps = ServerLifecycle.capabilities(ServerState.RUNNING)
        assertFalse(caps.canStart)
        assertTrue(caps.canStop)
    }

    @Test
    fun `stopped offers start but not stop`() {
        val caps = ServerLifecycle.capabilities(ServerState.STOPPED)
        assertTrue(caps.canStart)
        assertFalse(caps.canStop)
    }

    /**
     * Open must stay enabled while LOCKED — opening is what establishes the
     * cookie, so gating it would make the locked state unescapable.
     */
    @Test
    fun `locked still allows open`() {
        assertTrue(ServerLifecycle.capabilities(ServerState.LOCKED).canOpen)
        assertFalse(ServerLifecycle.capabilities(ServerState.LOCKED).canStart)
    }

    @Test
    fun `transitioning disables every control`() {
        val caps = ServerLifecycle.capabilities(ServerState.TRANSITIONING)
        assertFalse(caps.canStart)
        assertFalse(caps.canStop)
        assertFalse(caps.canRefresh)
    }

    /** Unreachable must stay actionable — retrying is the only way out. */
    @Test
    fun `unreachable can retry`() {
        val caps = ServerLifecycle.capabilities(ServerState.UNREACHABLE)
        assertTrue(caps.canStart)
        assertTrue(caps.canRefresh)
    }

    @Test
    fun `every state has a non-empty label`() {
        ServerState.values().forEach {
            assertTrue(it.name, ServerLifecycle.label(it).isNotBlank())
        }
    }
}
