package com.marstol.tether.data

import android.content.Context
import android.net.Uri
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import org.json.JSONObject
import java.io.IOException
import java.net.HttpURLConnection
import java.net.URL

/** Tether's API, called with the app's bearer token (see Tokens). */
object Api {
    const val BASE = "https://tether.marstol.com"
    const val CLIENT_ID = "tether-android"
    const val REDIRECT_URI = "com.marstol.tether:/oauth2redirect"

    /** No usable sign-in: the widgets show "Sign in" until the owner signs in again. */
    class SignedOut : Exception("signed out")

    /** An answer from the API. `body` is null for a 304 (unchanged since `etag`). */
    data class Response(val code: Int, val body: String?, val etag: String?)

    suspend fun get(context: Context, path: String, etag: String? = null): Response =
        authorized(context) { token -> request("GET", path, token, etag = etag) }

    suspend fun post(context: Context, path: String, json: JSONObject? = null): Response =
        authorized(context) { token -> request("POST", path, token, body = json?.toString()) }

    /** Calls with the current token; on a 401, refreshes once and retries. */
    private suspend fun authorized(context: Context, call: (String) -> Response): Response =
        withContext(Dispatchers.IO) {
            val token = Tokens.access(context) ?: throw SignedOut()
            val first = call(token)
            if (first.code != 401) return@withContext check(first)
            val fresh = Tokens.access(context, forceRefresh = true) ?: throw SignedOut()
            val second = call(fresh)
            if (second.code == 401) {
                Tokens.clear(context)
                throw SignedOut()
            }
            check(second)
        }

    private fun check(resp: Response): Response {
        if (resp.code >= 400) throw IOException("HTTP ${resp.code}")
        return resp
    }

    private fun request(method: String, path: String, token: String, etag: String? = null,
                        body: String? = null): Response {
        val conn = URL(BASE + path).openConnection() as HttpURLConnection
        try {
            conn.requestMethod = method
            conn.connectTimeout = 15_000
            conn.readTimeout = 20_000
            conn.setRequestProperty("Authorization", "Bearer $token")
            conn.setRequestProperty("Accept", "application/json")
            if (etag != null) conn.setRequestProperty("If-None-Match", etag)
            if (body != null) {
                conn.doOutput = true
                conn.setRequestProperty("Content-Type", "application/json")
                conn.outputStream.use { it.write(body.toByteArray()) }
            }
            val code = conn.responseCode
            val text = when {
                code == 304 -> null
                code >= 400 -> conn.errorStream?.bufferedReader()?.use { it.readText() }
                else -> conn.inputStream.bufferedReader().use { it.readText() }
            }
            return Response(code, text, conn.getHeaderField("ETag"))
        } finally {
            conn.disconnect()
        }
    }

    /** POST a form to the OAuth token endpoint (no bearer). Returns the JSON, or null when refused. */
    fun tokenRequest(form: Map<String, String>): JSONObject? {
        val conn = URL("$BASE/token").openConnection() as HttpURLConnection
        try {
            conn.requestMethod = "POST"
            conn.connectTimeout = 15_000
            conn.readTimeout = 20_000
            conn.doOutput = true
            conn.setRequestProperty("Content-Type", "application/x-www-form-urlencoded")
            val encoded = form.entries.joinToString("&") { (k, v) -> "$k=${Uri.encode(v)}" }
            conn.outputStream.use { it.write(encoded.toByteArray()) }
            val code = conn.responseCode
            if (code in 400..499) return null          // invalid_grant and friends: sign in again
            if (code >= 500) throw IOException("HTTP $code")
            return JSONObject(conn.inputStream.bufferedReader().use { it.readText() })
        } finally {
            conn.disconnect()
        }
    }
}
