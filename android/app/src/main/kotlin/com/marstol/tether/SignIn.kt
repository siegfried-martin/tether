package com.marstol.tether

import android.app.Activity
import android.content.Context
import android.content.Intent
import android.net.Uri
import android.os.Bundle
import android.util.Base64
import android.widget.Toast
import androidx.browser.customtabs.CustomTabsIntent
import com.marstol.tether.data.Api
import com.marstol.tether.data.Store
import com.marstol.tether.data.Sync
import com.marstol.tether.data.Tokens
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import java.security.MessageDigest
import java.security.SecureRandom

/** Work that outlives an activity: token exchange, posting a task. */
val appScope = CoroutineScope(SupervisorJob() + Dispatchers.Main)

/**
 * Signs the widgets in to Tether: OAuth with PKCE in a Chrome tab (RFC 8252).
 *
 * Chrome already holds the owner's Tether session, so this is one tap on the
 * consent page. The code comes back to RedirectActivity, which hands it here;
 * this activity stays under the tab meanwhile so the tab closes when it returns.
 */
class SignInActivity : Activity() {
    private var started = false

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        started = savedInstanceState?.getBoolean("started") ?: false
    }

    override fun onSaveInstanceState(outState: Bundle) {
        super.onSaveInstanceState(outState)
        outState.putBoolean("started", started)
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        setIntent(intent)
    }

    override fun onResume() {
        super.onResume()
        if (!started) {
            started = true
            openTab()
            return
        }
        val data = intent.data
        if (data == null) {
            finish()   // back from the tab without signing in
            return
        }
        finishSignIn(data)
    }

    private fun openTab() {
        val verifier = random(48)
        val state = random(16)
        getSharedPreferences("pkce", Context.MODE_PRIVATE).edit()
            .putString("verifier", verifier).putString("state", state).apply()
        val challenge = Base64.encodeToString(
            MessageDigest.getInstance("SHA-256").digest(verifier.toByteArray()),
            Base64.URL_SAFE or Base64.NO_PADDING or Base64.NO_WRAP)
        val url = Uri.parse("${Api.BASE}/authorize").buildUpon()
            .appendQueryParameter("response_type", "code")
            .appendQueryParameter("client_id", Api.CLIENT_ID)
            .appendQueryParameter("redirect_uri", Api.REDIRECT_URI)
            .appendQueryParameter("state", state)
            .appendQueryParameter("code_challenge", challenge)
            .appendQueryParameter("code_challenge_method", "S256")
            .build()
        CustomTabsIntent.Builder().build().launchUrl(this, url)
    }

    private fun finishSignIn(data: Uri) {
        val pkce = getSharedPreferences("pkce", Context.MODE_PRIVATE)
        val verifier = pkce.getString("verifier", null)
        val expected = pkce.getString("state", null)
        pkce.edit().clear().apply()
        val code = data.getQueryParameter("code")
        if (code == null || verifier == null || data.getQueryParameter("state") != expected) {
            Toast.makeText(this, "Tether sign-in didn't finish", Toast.LENGTH_LONG).show()
            finish()
            return
        }
        val app = applicationContext
        appScope.launch {
            val tokens = withContext(Dispatchers.IO) {
                runCatching {
                    Api.tokenRequest(mapOf(
                        "grant_type" to "authorization_code", "code" to code, "redirect_uri" to Api.REDIRECT_URI,
                        "client_id" to Api.CLIENT_ID, "code_verifier" to verifier))
                }.getOrNull()
            }
            if (tokens == null) {
                Toast.makeText(app, "Tether sign-in failed. Try again.", Toast.LENGTH_LONG).show()
            } else {
                Tokens.save(app, tokens)
                Store.clear(app)
                Toast.makeText(app, "Tether widgets signed in", Toast.LENGTH_SHORT).show()
                Sync.schedule(app)
                Sync.now(app)
            }
        }
        finish()
    }

    private fun random(bytes: Int): String {
        val b = ByteArray(bytes).also { SecureRandom().nextBytes(it) }
        return Base64.encodeToString(b, Base64.URL_SAFE or Base64.NO_PADDING or Base64.NO_WRAP)
    }
}

/** Receives com.marstol.tether:/oauth2redirect?code=… from Chrome and hands it to SignInActivity. */
class RedirectActivity : Activity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        startActivity(Intent(this, SignInActivity::class.java)
            .setData(intent.data)
            .addFlags(Intent.FLAG_ACTIVITY_CLEAR_TOP or Intent.FLAG_ACTIVITY_SINGLE_TOP))
        finish()
    }
}
