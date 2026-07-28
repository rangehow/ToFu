package com.tofu.client.session

import com.tofu.client.data.AuthType
import com.tofu.client.data.Profile
import com.tofu.client.data.ProfileDao
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.flowOf
import kotlinx.coroutines.test.runTest
import okhttp3.Cookie
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * Guards the orchestration invariants that would silently break the
 * "authenticate once / survive re-provision / no orphaned credential" promise.
 */
class SessionControllerTest {

    private val oldHost = "5665bc99-vscode-zw05.mlp.sankuai.com"
    private val newHost = "aaaa1111-vscode-zw05.mlp.sankuai.com"

    private class FakeCookieSink : CookieSink {
        val purged = mutableListOf<String>()
        override fun inject(origin: String, cookies: List<Cookie>) {}
        override fun purgeHost(host: String) { purged += host }
        override fun cookieHeader(origin: String): String? = null
    }

    private class FakeVault : SecretVault {
        val map = mutableMapOf<String, String>()
        override fun secretFor(alias: String) = map[alias]
        override fun putSecret(alias: String, secret: String) { map[alias] = secret }
        override fun removeSecret(alias: String) { map.remove(alias) }
    }

    private class FakeDao(seed: Profile? = null) : ProfileDao {
        val rows = mutableMapOf<Long, Profile>()
        val deleted = mutableListOf<Long>()
        var nextId = 1L
        init { if (seed != null) rows[seed.id] = seed }
        override fun observeAll(): Flow<List<Profile>> = flowOf(rows.values.toList())
        override suspend fun getById(id: Long) = rows[id]
        override suspend fun getAllOnce(): List<Profile> = rows.values.toList()
        override suspend fun getByAlias(alias: String) = rows.values.firstOrNull { it.alias == alias }
        override suspend fun insert(profile: Profile): Long {
            val id = nextId++; rows[id] = profile.copy(id = id); return id
        }
        override suspend fun update(profile: Profile): Int { rows[profile.id] = profile; return 1 }
        override suspend fun deleteById(id: Long) { rows.remove(id); deleted += id }
        override suspend fun setCookieHost(id: Long, host: String?): Int {
            val row = rows[id] ?: return 0
            rows[id] = row.copy(cookieHost = host)
            return 1
        }
    }

    private fun mgr(dao: ProfileDao, vault: SecretVault, sink: FakeCookieSink) =
        // No secret path exercised through the network: login short-circuits on
        // NONE / NoCredential. We assert side-effects (purge, secret moves), not HTTP.
        SessionManager(dao, vault, sink)

    private fun profile(alias: String, host: String, id: Long = 1) = Profile(
        id = id, alias = alias, baseUrl = "https://$host/proxy/15000/",
        authType = AuthType.NONE, instanceUuid = host.substringBefore("-vscode"),
        cookieHost = host,
    )

    @Test
    fun add_stores_secret_before_insert_and_logs_in() = runTest {
        val dao = FakeDao(); val vault = FakeVault(); val sink = FakeCookieSink()
        val ctl = SessionController(dao, vault, mgr(dao, vault, sink), clock = { 42L })

        val res = ctl.addProfile("zw05", "https://$oldHost/proxy/15000/",
            AuthType.CODE_SERVER_PASSWORD, "sekret")

        assertTrue(res is SessionController.AddResult.Added)
        // Secret stored under the alias, row inserted.
        assertEquals("sekret", vault.map["zw05"])
        assertEquals(1, dao.rows.size)
    }

    @Test
    fun add_rejects_duplicate_alias() = runTest {
        val dao = FakeDao(profile("zw05", oldHost)); val vault = FakeVault(); val sink = FakeCookieSink()
        val ctl = SessionController(dao, vault, mgr(dao, vault, sink))
        val res = ctl.addProfile("zw05", "https://$oldHost/proxy/15000/", AuthType.NONE, "")
        assertTrue(res is SessionController.AddResult.DuplicateAlias)
    }

    @Test
    fun edit_url_host_change_purges_old_jar() = runTest {
        val seed = profile("zw05", oldHost)
        val dao = FakeDao(seed); val vault = FakeVault(); val sink = FakeCookieSink()
        val ctl = SessionController(dao, vault, mgr(dao, vault, sink))

        ctl.editProfile(seed, "zw05", "https://$newHost/proxy/15000/", AuthType.NONE, "")

        // NEUTER: if editProfile didn't route host-changes through
        // updateUrlAndReauth, the old Domain-pinned jar would survive.
        assertEquals(listOf(oldHost), sink.purged)
    }

    @Test
    fun edit_same_host_does_not_purge() = runTest {
        val seed = profile("zw05", oldHost)
        val dao = FakeDao(seed); val vault = FakeVault(); val sink = FakeCookieSink()
        val ctl = SessionController(dao, vault, mgr(dao, vault, sink))
        ctl.editProfile(seed, "zw05", "https://$oldHost/proxy/16000/", AuthType.NONE, "")
        assertTrue(sink.purged.isEmpty())
    }

    @Test
    fun rename_moves_alias_keyed_secret() = runTest {
        val seed = profile("old", oldHost)
        val dao = FakeDao(seed); val vault = FakeVault(); val sink = FakeCookieSink()
        vault.putSecret("old", "sekret")
        val ctl = SessionController(dao, vault, mgr(dao, vault, sink))

        ctl.editProfile(seed, "new", seed.baseUrl, AuthType.CODE_SERVER_PASSWORD, "")

        // NEUTER: if rename didn't move the secret, login under "new" would find
        // no credential — the credential would be orphaned under "old".
        assertEquals("sekret", vault.map["new"])
        assertNull(vault.map["old"])
    }

    @Test
    fun add_with_blank_secret_reuses_same_host_password() = runTest {
        // Existing profile on oldHost:15000 with a stored password.
        val seed = profile("port15000", oldHost).copy(
            baseUrl = "https://$oldHost/proxy/15000/", authType = AuthType.CODE_SERVER_PASSWORD)
        val dao = FakeDao(seed); val vault = FakeVault(); val sink = FakeCookieSink()
        vault.putSecret("port15000", "sharedpw")
        val ctl = SessionController(dao, vault, mgr(dao, vault, sink))

        // Add a SECOND profile on the SAME host, different port, BLANK password.
        ctl.addProfile("port8080", "https://$oldHost/proxy/8080/",
            AuthType.CODE_SERVER_PASSWORD, secret = "")

        // NEUTER: without host-based reuse the new alias would have NO secret.
        assertEquals("sharedpw", vault.map["port8080"])
    }

    @Test
    fun add_with_blank_secret_does_NOT_borrow_from_a_different_host() = runTest {
        val seed = profile("other", oldHost).copy(authType = AuthType.CODE_SERVER_PASSWORD)
        val dao = FakeDao(seed); val vault = FakeVault(); val sink = FakeCookieSink()
        vault.putSecret("other", "sharedpw")
        val ctl = SessionController(dao, vault, mgr(dao, vault, sink))

        // New profile on a DIFFERENT host, blank password → must NOT borrow.
        ctl.addProfile("newone", "https://$newHost/proxy/15000/",
            AuthType.CODE_SERVER_PASSWORD, secret = "")

        assertNull(vault.map["newone"])
    }

    @Test
    fun findSharedSecret_matches_host_ignoring_port() = runTest {
        val seed = profile("p15000", oldHost).copy(
            baseUrl = "https://$oldHost/proxy/15000/")
        val dao = FakeDao(seed); val vault = FakeVault(); val sink = FakeCookieSink()
        vault.putSecret("p15000", "sharedpw")
        val ctl = SessionController(dao, vault, mgr(dao, vault, sink))

        assertEquals("sharedpw", ctl.findSharedSecret("https://$oldHost/proxy/9999/"))
        assertNull(ctl.findSharedSecret("https://$newHost/proxy/15000/"))
        // excludeAlias skips the profile itself (edit case).
        assertNull(ctl.findSharedSecret("https://$oldHost/proxy/15000/", excludeAlias = "p15000"))
    }

    @Test
    fun delete_removes_secret_and_row() = runTest {
        val seed = profile("zw05", oldHost)
        val dao = FakeDao(seed); val vault = FakeVault(); val sink = FakeCookieSink()
        vault.putSecret("zw05", "sekret")
        val ctl = SessionController(dao, vault, mgr(dao, vault, sink))

        ctl.deleteProfile(seed)

        assertNull("secret must be removed", vault.map["zw05"])
        assertTrue("row must be deleted", dao.deleted.contains(seed.id))
    }

    @Test
    fun migrate_flips_stale_proxy_none_leaves_others_untouched() = runTest {
        // Three rows: a stale /proxy/ profile on NONE (must flip), a bare-host
        // NONE (leave), and a proxy profile explicitly on INTERACTIVE_SSO (leave).
        val dao = FakeDao(); val vault = FakeVault(); val sink = FakeCookieSink()
        val proxyNone = Profile(id = 1, alias = "shanghai",
            baseUrl = "https://abc-vscode-shxstraining.mlp.sankuai.com/proxy/15000/",
            authType = AuthType.NONE)
        val bareNone = Profile(id = 2, alias = "bare",
            baseUrl = "https://tofu.example.com/", authType = AuthType.NONE)
        val proxySso = Profile(id = 3, alias = "sso",
            baseUrl = "https://h/proxy/8080/", authType = AuthType.INTERACTIVE_SSO)
        dao.rows[1] = proxyNone; dao.rows[2] = bareNone; dao.rows[3] = proxySso
        val ctl = SessionController(dao, vault, mgr(dao, vault, sink))

        val fixed = ctl.migrateProxyAuthDefaults()

        // NEUTER: make ServerUrl.needsProxyAuthFix return false and this fails
        // — the stale Shanghai row stays on NONE and can't headless-login.
        assertEquals("only the stale proxy row is fixed", 1, fixed)
        assertEquals(AuthType.CODE_SERVER_PASSWORD, dao.rows[1]!!.authType)
        assertEquals("bare host untouched", AuthType.NONE, dao.rows[2]!!.authType)
        assertEquals("explicit SSO untouched", AuthType.INTERACTIVE_SSO, dao.rows[3]!!.authType)

        // Idempotent: a second run fixes nothing.
        assertEquals(0, ctl.migrateProxyAuthDefaults())
    }
}
