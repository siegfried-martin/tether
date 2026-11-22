package com.marstol.tether.data

import android.content.Context
import android.security.keystore.KeyGenParameterSpec
import android.security.keystore.KeyProperties
import android.util.Base64
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock
import kotlinx.coroutines.withContext
import org.json.JSONObject
import java.security.KeyStore
import javax.crypto.Cipher
import javax.crypto.KeyGenerator
import javax.crypto.SecretKey
import javax.crypto.spec.GCMParameterSpec

/**
 * The app's OAuth tokens, encrypted with a key that never leaves the Android Keystore.
 *
 * Refresh tokens rotate (each works once), so refreshes are serialized: two at
 * once would spend the same refresh token and sign the app out.
 */
object Tokens {
    private const val PREFS = "auth"
    private const val KEY_ALIAS = "tether-tokens"
    private val lock = Mutex()

    private data class Set(val access: String, val refresh: String, val expiresAt: Long)

    fun signedIn(context: Context): Boolean = load(context) != null

    /** A live access token, refreshed when it is about to expire; null when signed out. */
    suspend fun access(context: Context, forceRefresh: Boolean = false): String? = withContext(Dispatchers.IO) {
        lock.withLock {
            val set = load(context) ?: return@withLock null
            if (!forceRefresh && set.expiresAt > System.currentTimeMillis() + 60_000) return@withLock set.access
            val json = Api.tokenRequest(mapOf(
                "grant_type" to "refresh_token", "refresh_token" to set.refresh, "client_id" to Api.CLIENT_ID))
            if (json == null) {
                clear(context)
                return@withLock null
            }
            save(context, json)
            load(context)?.access
        }
    }

    /** Store a token response from /token. */
    fun save(context: Context, json: JSONObject) {
        val set = JSONObject()
            .put("access", json.getString("access_token"))
            .put("refresh", json.getString("refresh_token"))
            .put("expires_at", System.currentTimeMillis() + json.optLong("expires_in", 3600) * 1000)
        prefs(context).edit().putString("tokens", encrypt(set.toString())).apply()
    }

    fun clear(context: Context) {
        prefs(context).edit().remove("tokens").apply()
    }

    private fun load(context: Context): Set? {
        val sealed = prefs(context).getString("tokens", null) ?: return null
        return try {
            val json = JSONObject(decrypt(sealed))
            Set(json.getString("access"), json.getString("refresh"), json.getLong("expires_at"))
        } catch (e: Exception) {
            clear(context)   // a key reset (e.g. restored device) makes old tokens unreadable
            null
        }
    }

    private fun prefs(context: Context) = context.getSharedPreferences(PREFS, Context.MODE_PRIVATE)

    // ------------------------------------------------------------------ Keystore AES-GCM

    private fun key(): SecretKey {
        val store = KeyStore.getInstance("AndroidKeyStore").apply { load(null) }
        (store.getKey(KEY_ALIAS, null) as SecretKey?)?.let { return it }
        return KeyGenerator.getInstance(KeyProperties.KEY_ALGORITHM_AES, "AndroidKeyStore").apply {
            init(KeyGenParameterSpec.Builder(KEY_ALIAS, KeyProperties.PURPOSE_ENCRYPT or KeyProperties.PURPOSE_DECRYPT)
                .setBlockModes(KeyProperties.BLOCK_MODE_GCM)
                .setEncryptionPaddings(KeyProperties.ENCRYPTION_PADDING_NONE)
                .build())
        }.generateKey()
    }

    private fun encrypt(plain: String): String {
        val cipher = Cipher.getInstance("AES/GCM/NoPadding").apply { init(Cipher.ENCRYPT_MODE, key()) }
        return Base64.encodeToString(cipher.iv + cipher.doFinal(plain.toByteArray()), Base64.NO_WRAP)
    }

    private fun decrypt(sealed: String): String {
        val bytes = Base64.decode(sealed, Base64.NO_WRAP)
        val cipher = Cipher.getInstance("AES/GCM/NoPadding").apply {
            init(Cipher.DECRYPT_MODE, key(), GCMParameterSpec(128, bytes, 0, 12))
        }
        return String(cipher.doFinal(bytes, 12, bytes.size - 12))
    }
}
