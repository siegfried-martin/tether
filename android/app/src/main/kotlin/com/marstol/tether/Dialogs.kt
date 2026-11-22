package com.marstol.tether

import android.app.Activity
import android.app.AlertDialog
import android.appwidget.AppWidgetManager
import android.content.Intent
import android.os.Bundle
import android.view.WindowManager
import android.view.inputmethod.EditorInfo
import android.widget.EditText
import android.widget.FrameLayout
import android.widget.Toast
import androidx.glance.appwidget.GlanceAppWidgetManager
import androidx.glance.appwidget.state.updateAppWidgetState
import com.marstol.tether.data.Api
import com.marstol.tether.data.Store
import com.marstol.tether.data.Sync
import com.marstol.tether.data.Tag
import com.marstol.tether.data.parseInbox
import com.marstol.tether.data.parseTasks
import com.marstol.tether.widget.ACCOUNT
import com.marstol.tether.widget.InboxWidget
import com.marstol.tether.widget.TAG_ID
import com.marstol.tether.widget.TasksWidget
import kotlinx.coroutines.launch
import org.json.JSONArray
import org.json.JSONObject

/** The Tasks widget's "+": a one-line dialog over the home screen that adds a task. */
class QuickAddActivity : Activity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val tag = intent.getStringExtra(EXTRA_TAG)
        val input = EditText(this).apply {
            hint = if (tag != null) "Add a task tagged $tag" else "Add a task"
            setSingleLine()
            imeOptions = EditorInfo.IME_ACTION_DONE
        }
        val box = FrameLayout(this).apply {
            val pad = (20 * resources.displayMetrics.density).toInt()
            setPadding(pad, pad / 2, pad, 0)
            addView(input)
        }
        val dialog = AlertDialog.Builder(this)
            .setTitle("New task")
            .setView(box)
            .setPositiveButton("Add") { _, _ -> add(input.text.toString(), tag) }
            .setNegativeButton("Cancel", null)
            .setOnDismissListener { finish() }
            .create()
        input.setOnEditorActionListener { _, action, _ ->
            if (action == EditorInfo.IME_ACTION_DONE) {
                add(input.text.toString(), tag)
                dialog.dismiss()
                true
            } else false
        }
        dialog.window?.setSoftInputMode(WindowManager.LayoutParams.SOFT_INPUT_STATE_VISIBLE)
        dialog.show()
        input.requestFocus()
    }

    private fun add(title: String, tag: String?) {
        if (title.isBlank()) return
        val app = applicationContext
        appScope.launch {
            val body = JSONObject().put("title", title.trim()).put("tags", JSONArray(listOfNotNull(tag)))
            val ok = runCatching { Api.post(app, "/api/tasks", body) }.isSuccess
            Toast.makeText(app, if (ok) "Added to Tether" else "Couldn't add the task. Check your connection.",
                           Toast.LENGTH_SHORT).show()
            if (ok) Sync.run(app)
        }
    }

    companion object {
        const val EXTRA_TAG = "tag"
    }
}

/** Set up or reconfigure an Inbox widget: every account's mail, or one account's. */
class InboxConfigActivity : Activity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val widgetId = intent.getIntExtra(AppWidgetManager.EXTRA_APPWIDGET_ID, AppWidgetManager.INVALID_APPWIDGET_ID)
        setResult(RESULT_CANCELED, Intent().putExtra(AppWidgetManager.EXTRA_APPWIDGET_ID, widgetId))
        if (widgetId == AppWidgetManager.INVALID_APPWIDGET_ID) {
            finish()
            return
        }
        appScope.launch {
            val accounts = loadAccounts()
            AlertDialog.Builder(this@InboxConfigActivity)
                .setTitle("Show in this widget")
                .setItems(arrayOf("All accounts") + accounts) { _, which -> choose(widgetId, accounts.getOrNull(which - 1)) }
                .setOnCancelListener { finish() }
                .show()
        }
    }

    private suspend fun loadAccounts(): List<String> {
        Store.read(this, Store.inboxName(null))?.let { body ->
            runCatching { return parseInbox(body).accounts.map { it.first } }
        }
        return runCatching { parseInbox(Api.get(this, "/api/widget/inbox?limit=1").body!!).accounts.map { it.first } }.getOrElse {
            Toast.makeText(this, "Couldn't load accounts; showing all mail", Toast.LENGTH_SHORT).show()
            emptyList()
        }
    }

    private fun choose(widgetId: Int, account: String?) {
        val app = applicationContext
        appScope.launch {
            val glanceId = GlanceAppWidgetManager(app).getGlanceIdBy(widgetId)
            updateAppWidgetState(app, glanceId) { prefs ->
                if (account == null) prefs.remove(ACCOUNT) else prefs[ACCOUNT] = account
            }
            InboxWidget().update(app, glanceId)
            Sync.now(app)
            setResult(RESULT_OK, Intent().putExtra(AppWidgetManager.EXTRA_APPWIDGET_ID, widgetId))
            finish()
        }
    }
}

/** Set up or reconfigure a Tasks widget: all tasks, or one tag's. */
class TasksConfigActivity : Activity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val widgetId = intent.getIntExtra(AppWidgetManager.EXTRA_APPWIDGET_ID, AppWidgetManager.INVALID_APPWIDGET_ID)
        setResult(RESULT_CANCELED, Intent().putExtra(AppWidgetManager.EXTRA_APPWIDGET_ID, widgetId))
        if (widgetId == AppWidgetManager.INVALID_APPWIDGET_ID) {
            finish()
            return
        }
        appScope.launch {
            val tags = loadTags()
            val names = arrayOf("All tasks") + tags.map { it.name }
            AlertDialog.Builder(this@TasksConfigActivity)
                .setTitle("Show in this widget")
                .setItems(names) { _, which -> choose(widgetId, tags.getOrNull(which - 1)) }
                .setOnCancelListener { finish() }
                .show()
        }
    }

    private suspend fun loadTags(): List<Tag> {
        Store.read(this, Store.tasksName(null))?.let { body ->
            runCatching { return parseTasks(body).tags }
        }
        return runCatching { parseTasks(Api.get(this, "/api/widget/tasks?limit=1").body!!).tags }.getOrElse {
            Toast.makeText(this, "Couldn't load tags; showing all tasks", Toast.LENGTH_SHORT).show()
            emptyList()
        }
    }

    private fun choose(widgetId: Int, tag: Tag?) {
        val app = applicationContext
        appScope.launch {
            val glanceId = GlanceAppWidgetManager(app).getGlanceIdBy(widgetId)
            updateAppWidgetState(app, glanceId) { prefs ->
                if (tag == null) prefs.remove(TAG_ID) else prefs[TAG_ID] = tag.id
            }
            TasksWidget().update(app, glanceId)
            Sync.now(app)
            setResult(RESULT_OK, Intent().putExtra(AppWidgetManager.EXTRA_APPWIDGET_ID, widgetId))
            finish()
        }
    }
}
