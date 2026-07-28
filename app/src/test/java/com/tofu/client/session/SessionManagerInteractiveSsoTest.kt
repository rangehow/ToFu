package com.tofu.client.session

import com.tofu.client.data.AuthType
import com.tofu.client.data.Profile
import com.tofu.client.data.ProfileDao
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.flowOf
import kotlinx.coroutines.test.runTest
import okhttp3.Cookie
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * Flow-level guard for the SSO stamp — driven through the DAO + cookie seams,
 * not by calling the pure rule directly.
 *
 * This distinction is the lesson from the staleness-guard bugs: a pure decision
 * function tested in isolation proves the RULE is right and says nothing about
 * whether the values wired into it are computed correctly. That gap is exactly
 * where the previous three bugs lived, so the WIRING gets its own seam.
 */
class SessionManagerInteractiveSsoTest {

    private val host = "5665bc99-vscode-zw05.mlp.sankuai.com"
    private val base = "https://$host/proxy/15000/"

    private class FakeCookieSink(private val header: String?) : CookieSink {
        val queried = mutableListOf<String>()
        override fun inject(origin: String, cookies: List<Cookie>) {}
        override fun purgeHost(host: String) {}
        override fun cookieHeader(origin: String): String? {
            queried += origin
            return header
        }
    }

    private class FakeSecrets : SecretLookup {
        override fun secretFor(alias: String): String? = null
    }

    private class FakeDao(private var current: Profile) : ProfileDao {
        val updates = mutableListOf<Profile>()
        override fun observeAll(): Flow<List<Profile>> = flowOf(listOf(current))
        override suspend fun getById(id: Long): Profile? = current
        override suspend fun getAllOnce(): List<Profile> = listOf(current)
        override suspend fun getByAlias(alias: String): Profile? = current
        override suspend fun insert(profile: Profile): Long = 1
        override suspend fun update(profile: Profile): Int {
            current = profile; updates += profile; return 1
        }
        override suspend fun deleteById(id: Long) {}
    }

    private fun profile(cookieHost: String? = null) = Profile(
        id = 1, alias = "sso server", baseUrl = base,
        authType = AuthType.INTERACTIVE_SSO, cookieHost = cookieHost,
        projectPath = "/home/dev/chatui",
    )

    /**
     * THE CORE GUARD. Landing back on our own host with a session cookie must
     * write cookieHost — that write is the ONLY thing that makes `isSignedIn`
     * true, and therefore the only thing that ever unlocks Start/Stop for an
     * SSO profile.
     *
     * NEUTER CHECK: delete the `dao.update(...)` in noteInteractiveSignIn and
     * this fails — the supervisor controls would stay locked forever.
     */
    @Test
    fun completing_sso_in_the_webview_stamps_cookie_host() = runTest {
        val cookies = FakeCookieSink("code-server-session=tok")
        val dao = FakeDao(profile())
        val mgr = SessionManager(dao, FakeSecrets(), cookies)

        val stamped = mgr.noteInteractiveSignIn(profile(), finishedUrl = base)

        assertTrue("must record the completed sign-in", stamped)
        assertEquals(1, dao.updates.size)
        assertEquals(host, dao.updates.single().cookieHost)
        // It must read the jar for OUR origin, not some other host's.
        assertEquals(listOf("https://$host"), cookies.queried)
    }

    /** Sitting on the IdP is not a completed sign-in. */
    @Test
    fun a_page_still_on_the_idp_stamps_nothing() = runTest {
        val cookies = FakeCookieSink("state=xyz")
        val dao = FakeDao(profile())
        val mgr = SessionManager(dao, FakeSecrets(), cookies)

        val stamped = mgr.noteInteractiveSignIn(
            profile(), finishedUrl = "https://sso.example.com/authorize?x=1",
        )

        assertFalse(stamped)
        assertTrue("must not stamp: ${dao.updates}", dao.updates.isEmpty())
    }

    /** An empty jar means the sign-in did not produce a session. */
    @Test
    fun no_cookie_stamps_nothing() = runTest {
        val cookies = FakeCookieSink(null)
        val dao = FakeDao(profile())
        val mgr = SessionManager(dao, FakeSecrets(), cookies)

        assertFalse(mgr.noteInteractiveSignIn(profile(), base))
        assertTrue(dao.updates.isEmpty())
    }

    /**
     * onPageDone fires on EVERY main-frame load, so a non-idempotent stamp would
     * write to Room on each navigation. Worse, cookieHost is part of the card's
     * CardKey — every write re-emits the profile and resets the card's polled
     * state, so a chatty stamp would make the supervisor status flicker.
     *
     * NEUTER CHECK: drop the `profile.cookieHost == host` early return and this
     * fails.
     */
    @Test
    fun an_already_stamped_profile_is_not_rewritten() = runTest {
        val cookies = FakeCookieSink("code-server-session=tok")
        val dao = FakeDao(profile(cookieHost = host))
        val mgr = SessionManager(dao, FakeSecrets(), cookies)

        val stamped = mgr.noteInteractiveSignIn(profile(cookieHost = host), base)

        assertFalse("already signed in → no write", stamped)
        assertTrue("must not re-write: ${dao.updates}", dao.updates.isEmpty())
    }

    /** A password profile is stamped by the headless login, never from here. */
    @Test
    fun a_password_profile_is_never_stamped_from_a_page_load() = runTest {
        val cookies = FakeCookieSink("code-server-session=tok")
        val pw = profile().copy(authType = AuthType.CODE_SERVER_PASSWORD)
        val dao = FakeDao(pw)
        val mgr = SessionManager(dao, FakeSecrets(), cookies)

        assertFalse(mgr.noteInteractiveSignIn(pw, base))
        assertTrue(
            "stamping here would paper over a real login failure: ${dao.updates}",
            dao.updates.isEmpty(),
        )
    }
}
